import tempfile
import time
import unittest
from pathlib import Path
from unittest.mock import Mock, patch

import httpx

from facturador.auth import PlanError
from facturador.automation import TicketAutomation
from facturador.catalog import portal_suggestions
from facturador.db import Store
from facturador.receipt_evidence import alternate_portal_note, local_ticket
from facturador.runner import Runner
from test_fiscal_capture import synthetic_pdf
from test_ticket_automation import PROFILE, extracted


class ReceiptRetryTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.root = Path(self.enterContext(tempfile.TemporaryDirectory()))
        self.store = Store(self.root)
        self.plan = Mock()
        self.plan.status.return_value = {'status': 'ready'}
        self.runner = Runner(self.store, self.plan)
        self.worker = self.new_worker()
        self.worker.configure(True)

    def new_worker(self):
        return TicketAutomation(Store(self.root), self.runner, self.plan, lambda: PROFILE)

    def ticket(self, name, content):
        path = self.root/name
        if isinstance(content, str):
            path.write_text(content, encoding='utf-8')
        else:
            path.write_bytes(content)
        return self.store.add(str(path), name)[0]['id']

    async def test_retry_after_survives_restart_and_another_ticket_progresses(self):
        key = self.ticket('first.txt', 'CAPUFE')
        with patch('facturador.automation.AI.extract', side_effect=PlanError('Ocupado.', 'transient', 1200)):
            await self.worker.step()
        delayed = self.store.get(key)
        self.assertEqual((delayed['auto_stage'], delayed['read_attempts']), ('retry_read', 1))
        self.assertGreaterEqual(delayed['read_after'], time.time() + 1199)
        self.store.recover_after_restart()
        worker = self.new_worker()
        next_key = self.ticket('second.txt', 'CAPUFE')
        with patch('facturador.automation.AI.extract', return_value=extracted(payment_code=None)) as read:
            await worker.step()
            self.assertEqual(self.store.get(next_key)['status'], 'review')
            self.assertEqual(self.store.get(next_key)['auto_stage'], 'offline')
            self.assertFalse(await worker.step())
            self.assertEqual(worker.status()['phase'], 'waiting_read')
            read.assert_not_called()
            with patch('time.time', return_value=delayed['read_after'] + 1):
                await worker.step()
            read.assert_called_once()
        self.assertEqual(self.store.get(key)['read_after'], 0)

    async def test_repeated_failure_stops_and_no_manual_or_issued_jobs_are_restarted(self):
        key = self.ticket('repeat.txt', 'CAPUFE')
        with patch('facturador.automation.AI.extract', side_effect=httpx.ConnectTimeout('prueba')) as read:
            await self.worker.step()
            for attempt in range(3):
                row = self.store.get(key)
                self.assertEqual((row['auto_stage'], row['read_attempts']), ('retry_read', attempt + 1))
                with patch('time.time', return_value=row['read_after'] + 1):
                    await self.worker.step()
            self.assertEqual(self.store.get(key)['auto_stage'], 'review')
            self.assertIn('agotaron', self.store.get(key)['message'])
            self.assertFalse(await self.worker.step())
            self.assertEqual(read.call_count, 4)
        manual = self.ticket('manual.txt', 'texto')
        self.store.claim_read(manual, False)
        automatic = self.ticket('auto.txt', 'texto')
        self.store.claim_read(automatic, True)
        issued = self.ticket('issued.txt', 'texto')
        self.store.claim_read(issued, True)
        self.store.confirm_issued(issued)
        self.store.recover_after_restart()
        self.assertEqual(self.store.get(manual)['auto_stage'], 'review')
        self.assertEqual(self.store.get(automatic)['auto_stage'], 'retry_read')
        self.assertNotEqual(self.store.get(issued)['auto_stage'], 'retry_read')

    async def test_original_second_page_overrides_invented_model_url(self):
        first = 'TIENDA DEMO\nFecha: 01/07/2026\nFolio: ABC123\nTOTAL $45.00'
        second = ('Para solicitar tu factura:\nrecibos.tienda.com.mx/portal\n'
                  'Sitio oficial: www.tienda.com.mx\nEncuesta: https://opinion.tienda.com.mx/inicio\n'
                  'Soporte: ayuda@correo.tienda.com.mx\n')
        key = self.ticket('ticket.pdf', synthetic_pdf(first, second))
        with patch('facturador.automation.AI.extract', return_value=extracted(provider='unknown', merchant='Tienda',
                   billing_url='https://inventado.com/facturar')):
            await self.worker.step()
        row = self.store.get(key)
        self.assertEqual(row['receipt']['portal_url'], 'https://recibos.tienda.com.mx/portal')
        self.assertEqual(row['status'], 'ready')
        self.assertFalse(self.runner.has_active_jobs())
        alternative = alternate_portal_note(row)
        self.assertIn('www.tienda.com.mx', alternative)
        self.assertNotIn('opinion.', alternative)
        self.assertNotIn('correo.', alternative)
        self.assertEqual(local_ticket(row)[0].portal_url, row['receipt']['portal_url'])

    async def test_unprinted_and_ambiguous_links_cannot_schedule_a_portal(self):
        for name, content in [('one', 'Tienda\nWeb: venta.com.mx\n'), ('two', 'Facturar en uno.com.mx o dos.com.mx/factura')]:
            key = self.ticket(name+'.txt', content)
            with patch('facturador.automation.AI.extract', return_value=extracted(provider='unknown', billing_url='https://no-impreso.com/facturar')), \
                 patch('facturador.automation.AI.discover_portal', return_value=('', '')):
                await self.worker.step()
            self.assertEqual(self.store.get(key)['receipt']['portal_url'], '')
            self.assertIn(self.store.get(key)['status'], ('review', 'duplicate'))
        links = portal_suggestions(['https://verificacfdi.facturaelectronica.sat.gob.mx/qr'],
                                   'Sitio: http://inseguro.com.mx\nContacto: compras@dominio.com.mx\nWeb: ejemplo.com.es')
        self.assertEqual(links, [])
