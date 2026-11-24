import asyncio
import hashlib
import io
import tempfile
import time
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock, patch

from fastapi.testclient import TestClient
from PIL import Image

from facturador.app import create_app
from facturador.automation import TicketAutomation
from facturador.chat import clearly_example, payment_correction
from facturador.db import Store
from facturador.ingest import save_input
from facturador.models import Receipt


def photo(color):
    output = io.BytesIO()
    Image.new('RGB', (28, 30), color).save(output, 'PNG')
    return output.getvalue()


class ChatFlowTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.app = create_app(self.root)

    def client(self):
        return TestClient(self.app)

    @staticmethod
    def post(client, token, text, photos=(), client_id='message_1234567890123456'):
        files = [('files', (name, data, 'image/png')) for name, data in photos]
        return client.post('/api/chat', headers={'x-app-token': token},
                           data={'text': text, 'client_id': client_id}, files=files)

    def test_explicit_example_never_creates_invoice_job(self):
        with patch.object(self.app.state.automation, 'start', lambda: None), self.client() as client:
            token = client.get('/api/state').json()['token']
            reply = self.post(client, token, 'Esta foto es solo un ejemplo; no la factures.',
                              [('example.png', photo('red'))])
            self.assertEqual(reply.status_code, 200, reply.text)
            body = reply.json()
            self.assertEqual(body['intent'], 'example')
            self.assertEqual(body['job_ids'], [])
            self.assertEqual(self.app.state.store.list(), [])
            url = body['messages'][0]['files'][0]['url']
            self.assertEqual(client.get(url).status_code, 200)

    def test_two_photos_one_job_and_idempotent_post(self):
        with patch.object(self.app.state.automation, 'start', lambda: None), self.client() as client:
            token = client.get('/api/state').json()['token']
            photos = [('top.png', photo('red')), ('bottom.png', photo('blue'))]
            first = self.post(client, token, 'Por ejemplo, este ticket está partido: factúralo.', photos)
            self.assertEqual(first.status_code, 200, first.text)
            first = first.json()
            self.assertEqual(first['intent'], 'invoice')
            self.assertEqual(len(first['job_ids']), 1)
            again = self.post(client, token, 'Por ejemplo, este ticket está partido: factúralo.', photos)
            self.assertEqual(again.status_code, 200, again.text)
            self.assertEqual(again.json()['messages'], first['messages'])
            jobs = self.app.state.store.list()
            self.assertEqual(len(jobs), 2)
            primary = next(row for row in jobs if not row['companion_of'])
            self.assertEqual(primary['id'], first['job_ids'][0])
            self.assertEqual(len(self.app.state.store.companion_paths(primary['id'])), 1)
            self.assertEqual([m['role'] for m in client.get('/api/chat').json()['messages']],
                             ['user', 'assistant'])

    def test_prefatory_example_message_applies_to_next_photo(self):
        with patch.object(self.app.state.automation, 'start', lambda: None), self.client() as client:
            token = client.get('/api/state').json()['token']
            first = self.post(client, token, 'Te voy a mandar una foto de ejemplo.', client_id='message_1234567890123457')
            self.assertEqual(first.status_code, 200, first.text)
            second = self.post(client, token, '', [('sample.png', photo('green'))],
                               client_id='message_1234567890123458')
            self.assertEqual(second.status_code, 200, second.text)
            self.assertEqual(second.json()['intent'], 'example')
            self.assertEqual(self.app.state.store.list(), [])

    def test_chat_upload_reads_automatically_with_legacy_toggle_off(self):
        with patch('facturador.automation.local_ticket', return_value=(Receipt(merchant='Café de prueba'),
                                                                    'Falta información.', 'lector_local')):
            with self.client() as client:
                token = client.get('/api/state').json()['token']
                self.assertFalse(self.app.state.automation.enabled)
                sent = self.post(client, token, '', [('ticket.png', photo('yellow'))])
                self.assertEqual(sent.status_code, 200, sent.text)
                key = sent.json()['job_ids'][0]
                deadline = time.monotonic() + 4
                while time.monotonic() < deadline and self.app.state.store.get(key)['status'] == 'new':
                    time.sleep(.05)
                row = self.app.state.store.get(key)
                self.assertEqual(row['status'], 'review')
                self.assertEqual(row['auto_stage'], 'offline')

    def test_future_retry_does_not_starve_next_chat_job(self):
        store = Store(self.root / 'queue')
        first = save_input(store, 'first.png', photo('red'))['id']
        second = save_input(store, 'second.png', photo('blue'))['id']
        store.queue_chat_job(first)
        store.queue_chat_job(second)
        with store.connect() as db:
            db.execute("UPDATE jobs SET status='review',auto_stage='retry_read',read_after=? WHERE id=?",
                       (time.time() + 3600, first))
        plan = Mock()
        plan.status.return_value = {'status': 'ready'}
        automation = TicketAutomation(store, Mock(), plan, Mock())
        automation.analyze = AsyncMock(return_value=store.get(second))
        self.assertTrue(asyncio.run(automation.chat_step()))
        automation.analyze.assert_awaited_once_with(second, automatic=True, offline=False)

    def test_restart_recovers_bundle_request_and_atomic_queue(self):
        store = Store(self.root / 'restart')
        names = [('top.png', photo('orange')), ('bottom.png', photo('black'))]
        originals = []
        directory = store.root / 'originales'
        directory.mkdir(exist_ok=True)
        for name, data in names:
            sha = hashlib.sha256(data).hexdigest()
            path = directory / (sha + '.png')
            path.write_bytes(data)
            originals.append((str(path), sha, name))
        key, created = store.add_chat_bundle(originals, request_id='message_1234567890123470')
        self.assertTrue(created)
        restarted = Store(store.root)
        same, created_again = restarted.add_chat_bundle(originals, request_id='message_1234567890123470')
        self.assertEqual(same, key)
        self.assertTrue(created_again)  # Same job still needs its chat turn and queue entry.
        messages = restarted.save_chat_turn('message_1234567890123470', '', [], [key], 'invoice',
                                            'Leyendo el ticket.', queue_job=created_again)
        self.assertEqual(len(messages), 2)
        again = Store(store.root)
        self.assertEqual([row['id'] for row in again.chat_jobs()], [key])
        self.assertEqual(len(again.list()), 2)

    def test_text_only_message_and_payment_classifier(self):
        self.assertTrue(clearly_example('Esta foto es solo un ejemplo, no la factures'))
        self.assertFalse(clearly_example('Por ejemplo un ticket partido'))
        self.assertEqual(payment_correction('Pagué con tarjeta de débito'), (True, '28'))
        self.assertEqual(payment_correction('Pagué con tarjeta'), (True, ''))
        self.assertEqual(payment_correction('No pagué en efectivo, fue con tarjeta'), (True, ''))
        self.assertEqual(payment_correction('Pago en efectivo o débito'), (True, ''))
        with patch.object(self.app.state.automation, 'start', lambda: None), self.client() as client:
            token = client.get('/api/state').json()['token']
            reply = self.post(client, token, '¿Cómo va mi ticket?')
            self.assertEqual(reply.status_code, 200, reply.text)
            self.assertEqual(reply.json()['intent'], 'conversation')
            self.assertEqual(reply.json()['job_ids'], [])

    def test_payment_message_updates_only_unissued_chat_ticket(self):
        with patch.object(self.app.state.automation, 'start', lambda: None), self.client() as client:
            token = client.get('/api/state').json()['token']
            uploaded = self.post(client, token, '', [('ticket.png', photo('purple'))],
                                 client_id='message_1234567890123459')
            key = uploaded.json()['job_ids'][0]
            ambiguous = self.post(client, token, 'Pagué con tarjeta.', client_id='message_1234567890123460')
            self.assertEqual(ambiguous.status_code, 200, ambiguous.text)
            self.assertEqual(self.app.state.store.get(key)['confirmed_payment'], '')
            corrected = self.post(client, token, 'Pagué con tarjeta de débito.',
                                  client_id='message_1234567890123461')
            self.assertEqual(corrected.status_code, 200, corrected.text)
            row = self.app.state.store.get(key)
            self.assertEqual(row['confirmed_payment'], '28')
            self.assertEqual(row['receipt']['payment_code'], '28')
            self.assertEqual(row['auto_stage'], 'retry_read')
            self.assertEqual(corrected.json()['job_ids'], [key])


if __name__ == '__main__':
    unittest.main()
