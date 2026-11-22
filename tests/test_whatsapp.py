import asyncio
import tempfile
import unittest
from datetime import timedelta
from pathlib import Path
from unittest.mock import patch

from fastapi.testclient import TestClient

from facturador.app import create_app
from facturador.ingest import save_input
from facturador.whatsapp import Outbox, WhatsAppWeb, whatsapp_router
from facturador.whatsapp_followup import time_utc
from test_core import FISCAL_UUID, PROFILE, RECEIPT, pdf_bytes, xml_bytes


class WhatsAppJobs(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        app = create_app(Path(self.tmp.name))
        self.store = app.state.store
        self.runner = app.state.runner
        self.client = TestClient(app)
        self.addCleanup(self.client.close)
        self.headers = {'X-App-Token': app.state.csrf_token}
        self.client.put('/api/profile', headers=self.headers, json=PROFILE.model_dump())
        info = save_input(self.store, 'ticket.pdf', pdf_bytes())
        self.key = info['id']
        self.store.save_receipt(self.key, RECEIPT, ready=True)
        self.base = f'/api/jobs/{self.key}/whatsapp'
        self.data = {'consent': 'true', 'phone': '+1 (202) 555-0123'}

    def post(self, suffix, **kwargs):
        return self.client.post(self.base+suffix, headers=self.headers, **kwargs)

    async def delivered(self, phone, reference, message, docs, arm, progress):
        self.assertEqual(phone, '12025550123')
        self.assertIn(reference, message)
        self.assertIn(PROFILE.email, message)
        arm()
        progress('Texto entregado.')
        self.assertEqual(self.store.get(self.key)['issue_attempted'], 1)
        self.assertEqual(docs[0]['buffer'], pdf_bytes())

    def mark_sent(self):
        with patch.object(WhatsAppWeb, 'send', side_effect=self.delivered):
            result = self.post('/send', data=self.data)
        self.assertEqual(result.status_code, 200, result.text)
        self.assertEqual(result.json()['status'], 'sent')
        return result.json()

    def test_send_content_consent_and_duplicate(self):
        captured = []
        async def send(phone, reference, message, docs, arm, progress):
            await self.delivered(phone, reference, message, docs, arm, progress)
            captured.extend(docs)
        files = {'csf': ('foo.pdf', pdf_bytes(), 'application/pdf'),
                 'voucher': ('bar.pdf', pdf_bytes(), 'application/pdf')}
        with patch.object(WhatsAppWeb, 'send', side_effect=send) as mocked:
            self.assertEqual(self.post('/send', data={**self.data, 'consent':'false'}).status_code, 422)
            self.assertEqual(self.post('/send', data={**self.data, 'phone':'0x123'}).status_code, 422)
            valid = self.post('/send', data=self.data, files=files)
            self.assertEqual(valid.status_code, 200, valid.text)
            self.assertEqual(self.post('/send', data=self.data).status_code, 422)
            self.assertEqual(mocked.call_count, 1)
        self.assertEqual([f['name'].split('-')[0] for f in captured], ['ticket','constancia','comprobante'])
        self.assertFalse(self.store.queue(self.key, PROFILE))
        self.assertFalse([p for p in self.store.root.rglob('*') if p.name in {'foo.pdf','bar.pdf'}])

    def test_preflight_failure_releases_but_uncertain_is_not_repeated(self):
        with patch.object(WhatsAppWeb, 'send', side_effect=ValueError('Primero vincula la sesión')):
            self.assertEqual(self.post('/send', data=self.data).status_code, 422)
        self.assertEqual(self.client.get(self.base).json()['status'], 'new')
        self.assertEqual(self.store.get(self.key)['status'], 'ready')
        async def uncertain(phone, reference, message, docs, arm, progress):
            arm()
            progress('Texto visible, adjuntos pendientes.')
            raise ValueError('Fallo de red')
        with patch.object(WhatsAppWeb, 'send', side_effect=uncertain) as mocked:
            reply = self.post('/send', data=self.data).json()
            self.assertEqual(reply['status'], 'uncertain')
            self.assertIn('adjuntos pendientes', reply['message'])
            self.assertEqual(self.post('/send', data=self.data).status_code, 422)
            self.assertEqual(mocked.call_count, 1)
        self.assertFalse(self.store.queue(self.key, PROFILE))
        self.assertEqual(Outbox(self.store).status(self.key)['status'], 'uncertain')

    def test_partial_reply_survives_and_uses_original_profile(self):
        req = self.mark_sent()
        self.client.put('/api/profile', headers=self.headers, json={**PROFILE.model_dump(), 'name':'OTRO RECEPTOR'})
        docs = [('parte.xml', xml_bytes()), ('factura.pdf', pdf_bytes())]
        async def recover(phone, reference, collect, *, ambiguous, background):
            self.assertFalse(ambiguous)
            self.assertFalse(background)
            self.assertEqual(reference, req['reference'])
            collect(*docs.pop(0))
            return 1
        with patch.object(WhatsAppWeb,'recover', side_effect=recover) as mock:
            partial = self.post('/recover')
            self.assertEqual(partial.status_code, 200, partial.text)
            self.assertEqual(partial.json()['status'], 'sent')
            self.assertEqual(partial.json()['count'], 1)
            second = self.post('/recover')
            self.assertEqual(second.status_code, 200, second.text)
            self.assertEqual(second.json()['status'], 'verified')
            self.assertEqual(self.post('/recover').json()['status'], 'verified')
            self.assertEqual(mock.call_count, 2)
        self.assertEqual(self.store.get(self.key)['uuid'], FISCAL_UUID)

    def test_restart_and_ambiguous_contact_even_when_prior_case_completed(self):
        outbox = Outbox(self.store)
        outbox.reserve(self.key, '12025550123')
        self.assertFalse(self.store.queue(self.key, PROFILE))
        outbox = Outbox(self.store)
        self.assertEqual(self.store.get(self.key)['status'],'ready')
        outbox.reserve(self.key,'12025550123')
        outbox.arm(self.key,PROFILE)
        outbox = Outbox(self.store)
        self.assertEqual(outbox.status(self.key)['status'], 'uncertain')
        self.store.update(self.key, status='verified')
        self.assertTrue(outbox.overlapping('different-ticket', '12025550123'))
        self.assertFalse(outbox.overlapping('different-ticket', '12025550124'))

    def test_reject_invoice_already_attempted(self):
        self.store.queue(self.key,PROFILE)
        with patch.object(WhatsAppWeb, 'send') as mocked:
            self.assertEqual(self.post('/send', data=self.data).status_code, 422)
            mocked.assert_not_called()

    def test_watch_preferences_are_explicit_and_persistent(self):
        self.data['watch'] = 'false'
        self.assertFalse(self.mark_sent()['watch'])
        enabled = self.client.put(self.base+'/watch', headers=self.headers, json={'watch':True})
        self.assertEqual(enabled.status_code, 200)
        self.assertIsNotNone(enabled.json()['next_check'])
        self.assertTrue(Outbox(self.store).status(self.key)['watch'])
        disabled = self.client.put(self.base+'/watch', headers=self.headers, json={'watch':False}).json()
        self.assertFalse(disabled['watch'])
        self.assertIsNone(disabled['next_check'])

    def test_background_recovers_split_replies_without_send_survives_restart(self):
        self.mark_sent()
        clock = time_utc() + timedelta(minutes=6)
        files = [('respuesta.xml', xml_bytes()), ('factura.pdf', pdf_bytes())]
        async def reply(phone, reference, collect, *, ambiguous, background):
            self.assertTrue(background)
            collect(*files.pop(0))
            return 1
        def restarted():
            watcher = whatsapp_router(self.store, self.runner, lambda:PROFILE).watcher
            watcher.clock = lambda:clock
            return watcher
        with patch.object(WhatsAppWeb, 'send') as send, patch.object(WhatsAppWeb, 'recover', side_effect=reply) as read:
            watcher = restarted()
            self.runner.viewer.channel = 'sesion-manual'
            asyncio.run(watcher.tick())
            self.assertEqual(read.call_count, 0)
            self.runner.viewer.channel = ''
            asyncio.run(watcher.tick())
            partial = self.client.get(self.base).json()
            self.assertEqual((partial['count'], partial['status'], partial['watch']), (1,'sent',True))
            self.assertIsNotNone(partial['last_check'])
            asyncio.run(watcher.tick())
            self.assertEqual(read.call_count, 1)
            clock += timedelta(minutes=15)
            asyncio.run(restarted().tick())
            done = self.client.get(self.base).json()
            self.assertEqual((done['count'], done['status'], done['watch'], done['next_check']), (2,'verified', False,None))
            send.assert_not_called()

    def test_watch_yields_when_foreground_is_taken_and_backs_off_on_errors(self):
        self.mark_sent()
        current = time_utc() + timedelta(minutes=6)
        watcher = whatsapp_router(self.store, self.runner, lambda:PROFILE).watcher
        watcher.clock = lambda:current
        cancelled = []
        async def takeover(*args, **kwargs):
            self.runner.viewer.channel = 'operador'
            try:
                await asyncio.Event().wait()
            finally:
                cancelled.append(True)
        with patch.object(WhatsAppWeb, 'recover', side_effect=takeover):
            asyncio.run(watcher.tick())
        self.assertEqual(cancelled,[True])
        self.assertIsNone(self.client.get(self.base).json()['last_check'])
        self.runner.viewer.channel = ''
        current += timedelta(minutes=2)
        with patch.object(WhatsAppWeb, 'recover', side_effect=ValueError('Abre sesión')) as failed:
            asyncio.run(watcher.tick())
            self.assertIn('Abre sesión', self.client.get(self.base).json()['watch_message'])
            current += timedelta(minutes=3)
            asyncio.run(watcher.tick())
            self.assertEqual(failed.call_count,1)
