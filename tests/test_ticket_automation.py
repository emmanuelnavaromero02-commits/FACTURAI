import asyncio
import json
import tempfile
import threading
import time
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock, patch

from fastapi.testclient import TestClient

from facturador.app import create_app
from facturador.ai import AI
from facturador.auth import PlanError
from facturador.automation import TicketAutomation
from facturador.db import Store
from facturador.models import Profile, Receipt
from facturador.runner import Runner


PROFILE = Profile(rfc='XAXX010101000', name='PERSONA DE PRUEBA', postal_code='12345',
                  regime='612', email='persona@example.com')


def extracted(**changes):
    fields = dict(provider='capufe', merchant='CAPUFE', is_receipt=True, amount='40.50',
                  date='2026-06-15', ticket_number='PRUEBA-A', billing_codes=[], payment_code='28',
                  payment_evidence='Pago con tarjeta de débito', details='', uncertainties=[], billing_url=None)
    return SimpleNamespace(**{**fields, **changes})


class TicketAutomationTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.root = Path(directory.name)
        self.store = Store(self.root)
        self.plan = Mock()
        self.plan.status.return_value = {'status': 'ready'}
        self.runner = Runner(self.store, self.plan)
        self.worker = TicketAutomation(self.store, self.runner, self.plan, lambda: PROFILE)
        self.profile = Receipt(provider='capufe', merchant='CAPUFE', date='2026-06-15', amount='40.50',
                               ticket_number='PRUEBA-A', payment_code='28')
        self.worker.configure(True)

    def add(self, key='subida', source='archivo: ticket.txt', text='CAPUFE'):
        path = self.root / (key + '.txt')
        path.write_text(text, encoding='utf-8')
        return self.store.add(str(path), key, source)[0]['id']

    async def test_reader_prepares_known_receipt_once_and_never_auto_issues(self):
        key = self.add(source='Gmail: mensaje')
        self.add('demo', 'Ejemplo: demo')
        self.store.import_history([{'key': 'hist', 'receipt': self.profile.model_copy(
            update={'ticket_number':'ANTERIOR'}).model_dump(), 'note':'Historial'}])
        with patch('facturador.automation.AI.extract', return_value=extracted()) as extract:
            await self.worker.step()
            self.assertEqual(self.store.get(key)['status'], 'ready')
            source_event = next(event for event in self.store.events(key) if event['kind'] == 'fuentes_de_campos')
            self.assertEqual(json.loads(source_event['detail'])['fields']['payment_code'],
                             'vision_modelo_linea_pago')
            with patch.object(self.runner, 'run', new_callable=AsyncMock) as run:
                await self.worker.step()
                await asyncio.gather(*self.runner.job_tasks.values())
                run.assert_awaited_once_with(key, PROFILE, False, False)
            self.store.recover_after_restart()
            await self.worker.step()
            self.assertFalse(self.runner.has_active_jobs())
            self.assertEqual(self.store.get(key)['status'], 'ready')
            extract.assert_called_once()

    async def test_existing_review_names_missing_field_then_continues_on_user_save(self):
        key = self.add()
        self.store.save_receipt(key, self.profile.model_copy(update={'payment_code': ''}))
        with patch('facturador.automation.AI.extract', side_effect=AssertionError):
            await self.worker.step()
            self.assertIn('Falta forma de pago', self.store.get(key)['message'])
            self.assertEqual(self.store.get(key)['receipt']['payment_code'], '')
            self.store.save_receipt(key, self.profile, ready=True)
            self.runner.viewer.channel = 'manual-control'
            await self.worker.step()
            self.assertEqual(self.worker.status()['phase'], 'waiting_browser')
            self.runner.viewer.channel = ''
            self.worker.get_profile = Mock(side_effect=ValueError)
            await self.worker.step()
            self.assertEqual(self.worker.status()['phase'], 'waiting_profile')
            self.worker.get_profile = lambda: PROFILE
            with patch.object(self.runner, 'run', new_callable=AsyncMock):
                await self.worker.step()
                await self.runner.job_tasks[key]
            self.assertEqual(self.store.get(key)['status'], 'queued')

    async def test_unfamiliar_printed_portal_opens_without_catalog_entry(self):
        link = 'https://facturas.example.org/mi-ticket'
        key = self.add(source='WhatsApp: ticket.txt', text='Factura en ' + link)
        with patch('facturador.automation.AI.extract', return_value=extracted(provider='unknown', billing_url=link)):
            await self.worker.step()
        row = self.store.get(key)
        self.assertEqual(row['receipt']['portal_url'], link)
        self.assertEqual(row['receipt']['provider'], 'portal_ticket')
        self.assertEqual(row['status'], 'ready')
        with patch.object(self.runner, 'run', new_callable=AsyncMock) as run:
            await self.worker.step()
            await asyncio.gather(*self.runner.job_tasks.values())
            run.assert_awaited_once_with(key, PROFILE, False, False)
        self.assertEqual(self.store.get(key)['status'], 'queued')

    async def test_unprinted_domain_still_needs_review(self):
        link = 'https://facturas.example.org/mi-ticket'
        key = self.add(source='WhatsApp: ticket.txt', text='Compra pagada sin dirección de facturación')
        with patch('facturador.automation.AI.extract', return_value=extracted(provider='unknown', billing_url=None)), \
             patch('facturador.automation.AI.discover_portal', return_value=('', '')):
            await self.worker.step()
        row = self.store.get(key)
        self.assertEqual(row['status'], 'review')
        receipt = Receipt.model_validate(row['receipt']).model_copy(update={'provider':'portal_ticket', 'portal_url':link})
        self.store.save_receipt(key, receipt, ready=True)
        await self.worker.step()
        self.assertEqual(self.store.get(key)['status'], 'ready')
        self.assertIn('Confirma el dominio', self.store.get(key)['message'])
        self.assertFalse(self.runner.has_active_jobs())

    async def test_verified_public_research_prepares_unfamiliar_portal(self):
        billing = 'https://facturas.cafeteria.example.org/emitir'
        evidence = 'https://cafeestrella.mx/facturacion'
        key = self.add(source='archivo: consumo.txt', text='CAFETERIA\nTOTAL: 40.50\nFOLIO PRUEBA-A')
        with patch('facturador.automation.AI.extract', return_value=extracted(
                provider='unknown', merchant='Café Estrella', billing_url=None,
                uncertainties=['No aparece una liga de facturación.'])), \
             patch('facturador.automation.AI.discover_portal', return_value=(billing, evidence)) as search, \
             patch('facturador.automation.public_page_links_to', new_callable=AsyncMock, return_value=True) as linked:
            await self.worker.step()
        search.assert_called_once_with('Café Estrella')
        linked.assert_awaited_once_with(evidence, billing)
        row = self.store.get(key)
        self.assertEqual((row['status'], row['receipt']['portal_url']), ('ready', billing))
        with patch.object(self.runner, 'run', new_callable=AsyncMock) as run:
            await self.worker.step()
            await asyncio.gather(*self.runner.job_tasks.values())
            run.assert_awaited_once_with(key, PROFILE, False, False)
        self.assertEqual(self.store.get(key)['status'], 'queued')

    async def test_unrelated_search_source_needs_domain_review(self):
        billing = 'https://facturas.example.org/emitir'
        evidence = 'https://directorio.example.org/cafe-estrella'
        key = self.add(source='archivo: consumo.txt', text='CAFE ESTRELLA\nTOTAL: 40.50')
        with patch('facturador.automation.AI.extract', return_value=extracted(
                provider='unknown', merchant='Café Estrella', billing_url=None)), \
             patch('facturador.automation.AI.discover_portal', return_value=(billing, evidence)), \
             patch('facturador.automation.public_page_links_to', new_callable=AsyncMock) as linked:
            await self.worker.step()
        linked.assert_not_awaited()
        row = self.store.get(key)
        self.assertEqual(row['status'], 'review')
        self.assertEqual(row['receipt']['portal_url'], billing)


    async def test_bad_date_and_payment_do_not_discard_legible_ticket_fields(self):
        key = self.add()
        with patch('facturador.automation.AI.extract', return_value=extracted(date='ayer', payment_code='tarjeta')):
            await self.worker.step()
        row = self.store.get(key)
        self.assertEqual(row['receipt']['ticket_number'], 'PRUEBA-A')
        self.assertEqual(row['receipt']['date'], '')
        self.assertEqual(row['receipt']['payment_code'], '')
        self.assertIn('Falta fecha', row['message'])
        self.assertIn('Falta forma de pago', row['message'])

    async def test_only_one_read_and_concurrent_user_edit_wins(self):
        key = self.add()
        started, finish = threading.Event(), threading.Event()

        def slow_reader(*_):
            started.set()
            finish.wait(5)
            return extracted()

        with patch('facturador.automation.AI.extract', side_effect=slow_reader) as extract:
            task = asyncio.create_task(self.worker.step())
            try:
                self.assertTrue(await asyncio.to_thread(started.wait, 3))
                with self.assertRaisesRegex(ValueError, 'lectura en curso'):
                    await self.worker.analyze(key)
                self.store.save_receipt(key, self.profile.model_copy(update={'ticket_number':'EDITADO'}), ready=True)
            finally:
                finish.set()
            await task
            extract.assert_called_once()
        self.assertEqual(self.store.get(key)['receipt']['ticket_number'], 'EDITADO')

    async def test_paused_plan_then_uncertain_read_needs_operator(self):
        key = self.add()
        self.plan.status.return_value = {'status':'limit','message':'Cuota agotada'}
        with patch('facturador.automation.AI.extract') as extract:
            await self.worker.step()
            self.assertEqual(self.worker.status()['phase'], 'waiting_plan')
            extract.assert_not_called()
        self.plan.status.return_value = {'status':'ready'}
        with patch('facturador.automation.AI.extract', side_effect=PlanError('Conexión interrumpida.')) as extract:
            await self.worker.step()
            await self.worker.step()
            extract.assert_called_once()
        self.assertEqual(self.store.get(key)['status'], 'review')
        self.assertIn('interrumpida', self.store.get(key)['message'])

    async def test_restart_recovers_unstarted_but_does_not_rebill_an_interrupted_read(self):
        waiting = self.add()
        inflight = self.add('incidente')
        self.store.claim_read(inflight, automatic=True)
        self.store.recover_after_restart()
        reopened = TicketAutomation(Store(self.root), self.runner, self.plan, lambda: PROFILE)
        self.assertTrue(reopened.enabled)
        self.assertEqual(self.store.get(inflight)['status'], 'review')
        with patch('facturador.automation.AI.extract', return_value=extracted(uncertainties=['Confirma la fecha.'])) as reader:
            await reopened.step()
            await reopened.step()
            reader.assert_called_once()
        self.assertIn('Confirma la fecha', self.store.get(waiting)['message'])


class AutomationAPITests(unittest.TestCase):
    def test_web_research_rejects_uncited_portal(self):
        candidate = 'https://facturas.example.org/emitir'
        source = 'https://cafeteria.example.org/facturacion'
        plan = Mock()
        plan.infer.return_value = (SimpleNamespace(billing_url=candidate, evidence_url=source),
                                   ['https://directorio.example.org/lista'])
        self.assertEqual(AI(plan).discover_portal('Cafetería Prueba'), ('', ''))
        plan.infer.return_value = (SimpleNamespace(billing_url=candidate, evidence_url=source), [source])
        self.assertEqual(AI(plan).discover_portal('Cafetería Prueba'), (candidate, source))

    def test_uploaded_ticket_is_read_by_background_worker(self):
        from PIL import Image
        from io import BytesIO

        with tempfile.TemporaryDirectory() as directory:
            app = create_app(Path(directory))
            with (patch.object(app.state.plan, 'status', return_value={'status':'ready'}),
                  patch('facturador.automation.AI.extract', return_value=extracted(payment_code=None)) as read,
                  TestClient(app) as client):
                headers = {'x-app-token': app.state.csrf_token}
                client.put('/api/automation', json={'enabled':True}, headers=headers).raise_for_status()
                image = BytesIO()
                Image.new('RGB', (160, 120), 'white').save(image, 'PNG')
                reply = client.post('/api/upload', files={'files':('ticket.png', image.getvalue(), 'image/png')},
                                    headers=headers)
                reply.raise_for_status()
                key = reply.json()[0]['id']
                until = time.monotonic() + 5
                while app.state.store.get(key)['status'] == 'new' and time.monotonic() < until:
                    time.sleep(.05)
                row = next(r for r in client.get('/api/state').json()['jobs'] if r['id'] == key)
                self.assertEqual(row['status'], 'review')
                self.assertIn('Falta forma de pago', row['message'])
                read.assert_called_once()

    def test_preferences_are_opt_in_and_persist_over_restart(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            with TestClient(create_app(root)) as client:
                state = client.get('/api/state').json()
                self.assertFalse(state['automation']['enabled'])
                self.assertIsNone(state['browser']['job_id'])
                response = client.put('/api/automation', json={'enabled': True}, headers={'x-app-token': state['token']})
                self.assertTrue(response.json()['enabled'])
            with TestClient(create_app(root)) as client:
                self.assertTrue(client.get('/api/state').json()['automation']['enabled'])
