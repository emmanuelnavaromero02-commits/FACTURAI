import io
import os
import tempfile
import unittest
from email.message import EmailMessage
from pathlib import Path
from unittest.mock import AsyncMock, Mock, patch

import zxingcpp
from fastapi.testclient import TestClient
from PIL import Image

from facturador.ai import AI, read_receipt
from facturador.app import create_app
from facturador.db import Store
from facturador.documents import job_candidates, reconcile, save_candidate
from facturador.local_reader import read_local_page
from facturador.sample import sample_ticket
from test_core import FISCAL_UUID, PROFILE, RECEIPT, pdf_bytes, xml_bytes


class RecoveryTests(unittest.TestCase):
    def setUp(self):
        folder = self.enterContext(tempfile.TemporaryDirectory())
        self.root = Path(folder)
        self.app = create_app(self.root)
        self.client = self.enterContext(TestClient(self.app))
        self.headers = {'x-app-token': self.app.state.csrf_token}
        self.store = self.app.state.store
        self.client.put('/api/profile', headers=self.headers, json=PROFILE.model_dump()).raise_for_status()
        self.key = self.new_ticket('primero')
        self.url = '/api/jobs/' + self.key

    def new_ticket(self, label):
        row, _ = self.store.add('original-' + label, label)
        self.store.save_receipt(row['id'], RECEIPT.model_copy(update={'billing_codes': [label]}), ready=True)
        return row['id']

    def browser_file(self, filename, content):
        folder = self.root / 'descargas-navegador'
        folder.mkdir(exist_ok=True)
        path = folder / ('0000000000000001_' + filename)
        path.write_bytes(content)
        return path.name

    def test_browser_xml_and_mailed_pdf_are_matched_across_sessions(self):
        xml = self.browser_file('primero.xml', xml_bytes())
        incomplete = self.client.post(self.url + '/recover-browser', headers=self.headers, json={'files': [xml]})
        self.assertEqual(incomplete.status_code, 422)
        self.assertIn('Falta el PDF', incomplete.text)
        self.assertEqual(len(job_candidates(Store(self.root), self.key)), 1)
        msg = EmailMessage()
        msg['Subject'] = 'Comprobantes de prueba'
        msg.add_attachment(pdf_bytes(), maintype='application', subtype='pdf', filename='comprobante.pdf')
        response = self.client.post(self.url + '/attachments', headers=self.headers,
                                    files={'files': ('comprobante.eml', msg.as_bytes(), 'message/rfc822')})
        self.assertEqual(response.json()['uuid'], FISCAL_UUID)
        self.assertEqual(self.client.get(self.url + '/files/pdf').content, pdf_bytes())
        self.assertEqual(self.client.get(self.url + '/files/xml').content, xml_bytes())
        self.client.post(self.url + '/recover-local', headers=self.headers).raise_for_status()
        self.assertEqual(len([e for e in self.store.events(self.key) if e['kind'] == 'verified']), 1)

    def test_uploaded_pdf_then_gmail_xml_recovers_same_ticket(self):
        pdf = self.client.post(self.url + '/attachments', headers=self.headers,
                               files={'files': ('f.pdf', pdf_bytes(), 'application/pdf')})
        self.assertIn('Falta el XML', pdf.text)

        def mail_fetch(root, key, query):
            self.assertEqual((root, key, query), (self.root, self.key, 'folio-de-prueba'))
            return [save_candidate(self.store, key, 'f.xml', xml_bytes())]

        with patch('facturador.app.recover_attachments', side_effect=mail_fetch):
            result = self.client.post(self.url + '/gmail', headers=self.headers, json={'query': 'folio-de-prueba'})
        self.assertEqual(result.json()['uuid'], FISCAL_UUID)
        self.assertFalse(self.store.queue(self.key, PROFILE))

    def test_browser_selection_must_participate_in_verified_pair(self):
        for filename, data in [('f.xml', xml_bytes()), ('f.pdf', pdf_bytes())]:
            save_candidate(self.store, self.key, filename, data)
        selected = self.browser_file('ajeno.pdf', pdf_bytes(uuid_value='FF345678-1234-4234-8234-1234567890AB'))
        result = self.client.post(self.url + '/recover-browser', headers=self.headers, json={'files': [selected]})
        self.assertEqual(result.status_code, 422)
        self.assertIsNone(self.store.get(self.key)['uuid'])
        right = self.browser_file('correcto.pdf', pdf_bytes())
        result = self.client.post(self.url + '/recover-browser', headers=self.headers, json={'files': [right]})
        self.assertEqual(result.json()['uuid'], FISCAL_UUID)

    def test_corrupt_candidates_do_not_hide_valid_pair_or_override_portal_uuid(self):
        for name, data in [('a.pdf', b'basura'), ('b.zip', b'incompleto'), ('c.pdf', pdf_bytes()), ('d.xml', xml_bytes())]:
            save_candidate(self.store, self.key, name, data)
        self.store.update(self.key, uuid='FF345678-1234-4234-8234-1234567890AB')
        with self.assertRaisesRegex(ValueError, 'otro UUID'):
            reconcile(self.store, self.key, PROFILE)
        self.store.update(self.key, uuid=FISCAL_UUID)
        self.assertEqual(reconcile(self.store, self.key, PROFILE)['uuid'], FISCAL_UUID)

    def test_eml_ticket_import_is_offline_and_idempotent(self):
        message = EmailMessage()
        message['Subject'] = 'Reserva de prueba'
        message.set_content('Reserva FA123')
        message.add_attachment(sample_ticket(), maintype='image', subtype='png', filename='ticket.png')
        files = {'files': ('viaje.eml', message.as_bytes(), 'message/rfc822')}
        items = self.client.post('/api/upload', headers=self.headers, files=files).json()
        self.assertEqual(len(items), 2)
        for item in items:
            self.assertTrue(item['created'])
        repeated = self.client.post('/api/upload', headers=self.headers, files=files).json()
        self.assertEqual([x['id'] for x in items], [x['id'] for x in repeated])
        self.assertFalse(any(x['created'] for x in repeated))
        text = self.client.post('/api/jobs/' + items[0]['id'] + '/read-local', headers=self.headers, json={}).json()
        self.assertEqual(text['text'], 'Reserva FA123')
        invalid = self.client.post('/api/upload', headers=self.headers, files={'files': ('f.eml', b'sin cabecera')})
        self.assertEqual(invalid.status_code, 422)

    def test_local_reader_retains_codes_and_pdf_text_when_tools_fail(self):
        code = zxingcpp.write_barcode_to_image(zxingcpp.create_barcode('FOLIO-O012', zxingcpp.BarcodeFormat.QRCode), scale=6)
        path = self.root / 'codigo.png'
        Image.frombytes('L', (code.shape[1], code.shape[0]), bytes(code)).save(path)
        with patch('facturador.local_reader.ocr_text', side_effect=ValueError('Sin OCR')):
            local = read_local_page(path)
        self.assertEqual((local['method'], local['codes']), ('codes', ['FOLIO-O012']))
        digital = self.root / 'digital.pdf'
        digital.write_bytes(pdf_bytes())
        with patch('facturador.local_reader.render_pdf_page', side_effect=ValueError('Sin imagen')), \
             patch('facturador.ai.render_pdf_page', side_effect=ValueError('Sin imagen')):
            self.assertIn(FISCAL_UUID, read_local_page(digital)['text'])
            self.assertIn(FISCAL_UUID, read_receipt(digital)[0]['text'])
        plan = Mock()
        AI(plan).next_action('instrucciones', 'árbol visible', None)
        self.assertEqual(plan.infer.call_args.args[1], [{'type': 'input_text', 'text': 'árbol visible'}])

    def test_offline_tax_document_is_transient_and_never_calls_model(self):
        files = {'file': ('constancia.pdf', pdf_bytes(), 'application/pdf')}
        previous = self.client.get('/api/state').json()['profile']
        with patch('facturador.ai.AI.tax_profile', side_effect=AssertionError('No debe invocar el plan')):
            reply = self.client.post('/api/profile/read-csf', files=files, data={'mode': 'local'}, headers=self.headers)
            self.assertEqual(reply.json()['method'], 'pdf')
            self.assertIn(PROFILE.rfc, reply.json()['text'])
            self.assertEqual(self.client.get('/api/state').json()['profile'], previous)
            self.assertFalse(list(self.root.rglob('constancia.*')))
            bad = self.client.post('/api/profile/read-csf', files=files, data={'mode': 'local', 'page': 2}, headers=self.headers)
            self.assertEqual(bad.status_code, 422)

    def test_scanned_pdf_is_cotejado_by_ocr_only_when_uuid_and_rfc_agree(self):
        doc = io.BytesIO()
        Image.new('RGB', (180, 100), 'white').save(doc, 'PDF')
        save_candidate(self.store, self.key, 'raster.pdf', doc.getvalue())
        save_candidate(self.store, self.key, 'f.xml', xml_bytes())
        with patch('facturador.cfdi.ocr_text', return_value=FISCAL_UUID + ' RFC AJENO'):
            with self.assertRaises(ValueError):
                reconcile(self.store, self.key, PROFILE)
        with patch('facturador.cfdi.ocr_text', return_value=FISCAL_UUID + ' RFC ' + PROFILE.rfc):
            self.assertEqual(reconcile(self.store, self.key, PROFILE)['uuid'], FISCAL_UUID)

    def test_restart_releases_only_pre_submit_work_and_remembers_confirmation(self):
        for kind, manual, confirmed, expected in [('issue', False, False, 'ready'), ('captcha', True, False, 'uncertain'),
                                                  ('plan', False, True, 'issued_waiting_files')]:
            with self.subTest(kind=kind):
                key = self.new_ticket(kind)
                self.assertTrue(self.store.queue(key, PROFILE))
                self.assertTrue(self.store.claim(key))
                if confirmed:
                    self.store.confirm_issued(key)
                self.store.enter_intervention(key, kind, 'prueba', manual)
                reopened = Store(self.root)
                reopened.recover_after_restart()
                self.assertEqual(reopened.get(key)['status'], expected)
                self.assertEqual(reopened.queue(key, PROFILE), expected == 'ready')

    def test_manual_mode_is_available_without_account_and_retry_checks_state(self):
        runner = self.app.state.runner
        normal = self.client.post(self.url + '/run', headers=self.headers, json={'consent': True})
        self.assertEqual(normal.status_code, 422)
        with patch.object(runner, 'schedule') as scheduled:
            result = self.client.post(self.url + '/run', headers=self.headers, json={'consent': True, 'mode': 'manual'})
            result.raise_for_status()
            scheduled.assert_called_once_with(self.key, PROFILE, False, manual=True, confirmed_url='')
        self.assertTrue(self.store.queue(self.key, PROFILE))
        self.store.update(self.key, status='blocked')
        row = next(row for row in self.client.get('/api/state').json()['jobs'] if row['id'] == self.key)
        self.assertTrue(row['can_retry'])
        self.assertTrue(self.store.queue(self.key, PROFILE))
        self.store.claim(self.key)
        self.store.begin_emission(self.key, 'prueba')
        self.store.update(self.key, status='blocked')
        self.assertFalse(self.store.queue(self.key, PROFILE))

    def test_unlisted_merchant_can_be_reviewed_then_reconciled(self):
        values = RECEIPT.model_copy(update={'provider': 'solicitud_directa'}).model_dump()
        reply = self.client.put(self.url, headers=self.headers, json=values)
        self.assertEqual(reply.json()['status'], 'external')
        self.assertFalse(self.store.queue(self.key, PROFILE))
        other = self.new_ticket('otra-foto')
        reply = self.client.put('/api/jobs/' + other, headers=self.headers, json={**values, 'merchant': 'Otra tienda'})
        self.assertEqual(reply.json()['status'], 'external')
        save_candidate(self.store, self.key, 'f.xml', xml_bytes())
        save_candidate(self.store, self.key, 'f.pdf', pdf_bytes())
        self.assertEqual(reconcile(self.store, self.key, PROFILE)['uuid'], FISCAL_UUID)

    def test_diagnostics_report_independent_failures_and_rollback_probe(self):
        with patch.object(self.app.state.runner, 'start_browser', new=AsyncMock(side_effect=ValueError('Fuera de servicio'))), \
             patch('facturador.diagnostics.ocr_text', side_effect=ValueError('Sin OCR')):
            response = self.client.post('/api/tools/check', headers=self.headers)
        checks = {item['name']: item for item in response.json()}
        self.assertEqual(checks['Navegador y Playwright']['status'], 'error')
        self.assertEqual(checks['OCR local']['status'], 'error')
        for name in ('Expedientes y almacenamiento', 'QR y códigos de barras', 'PDF y ZIP', 'Datos del CFDI 4.0'):
            self.assertEqual(checks[name]['status'], 'ok', name)
        self.assertEqual(checks['Gmail']['status'], 'pending')
        with self.store.connect() as db:
            self.assertEqual(db.execute("SELECT COUNT(*) FROM events WHERE kind='diagnostico'").fetchone()[0], 0)

    def test_failed_browser_start_keeps_panel_and_health_response_available(self):
        with patch.dict(os.environ, {'FACTURADOR_CONTAINER': '1'}), tempfile.TemporaryDirectory() as folder:
            app = create_app(Path(folder))
            with patch.object(app.state.runner, 'start_browser', new=AsyncMock(side_effect=ValueError('Sin navegador'))) as start:
                with TestClient(app) as client:
                    self.assertEqual(client.get('/healthz').json(), {'status': 'degraded', 'browser': 'unavailable'})
                    self.assertEqual(client.get('/').status_code, 200)
                    self.assertEqual(client.get('/api/state').json()['jobs'], [])
                    start.assert_awaited_once()
