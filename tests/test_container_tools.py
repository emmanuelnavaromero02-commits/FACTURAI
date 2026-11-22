import asyncio
import base64
import io
import tempfile
import unittest
from email.message import EmailMessage
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import httpx
from fastapi.testclient import TestClient
from PIL import Image
from pypdf import PdfWriter

from facturador.ai import read_receipt
from facturador.app import create_app
from facturador.ingest import save_input
from facturador.local_reader import ocr_languages
from facturador.sample import sample_ticket
from facturador.viewer import BrowserView
from test_core import FISCAL_UUID, PROFILE, RECEIPT, pdf_bytes, xml_bytes


class ToolsTests(unittest.TestCase):
    def setUp(self):
        folder = tempfile.TemporaryDirectory()
        self.addCleanup(folder.cleanup)
        self.app = create_app(Path(folder.name))
        self.client = self.enterContext(TestClient(self.app))
        self.headers = {'x-app-token': self.client.get('/api/state').json()['token']}
        self.store = self.app.state.store

    def post(self, url, body):
        return self.client.post(url, json=body, headers=self.headers)

    def test_read_digital_pdf_by_page_without_a_connected_plan(self):
        pdf = PdfWriter()
        for code in (FISCAL_UUID, '99999999-1234-4234-8234-1234567890AB'):
            pdf.append(io.BytesIO(pdf_bytes(uuid_value=code)))
        buffer = io.BytesIO()
        pdf.write(buffer)
        job = save_input(self.store, 'boleto.pdf', buffer.getvalue())
        url = '/api/jobs/' + job['id'] + '/read-local'
        result = self.post(url, {'page': 2})
        self.assertEqual(result.status_code, 200, result.text)
        self.assertEqual(result.json()['pages'], 2)
        self.assertEqual(result.json()['method'], 'pdf')
        self.assertIn('99999999-', result.json()['text'])
        self.assertNotIn(FISCAL_UUID, result.json()['text'])
        self.assertEqual(self.post(url, {'page': 3}).status_code, 422)
        self.assertEqual(self.client.post(url, json={}).status_code, 403)

    @unittest.skipUnless(ocr_languages(), 'Tesseract local no está disponible')
    def test_ocr_on_rotated_image_and_raster_pdf(self):
        photo = Image.open(io.BytesIO(sample_ticket()))
        for suffix, format, image, rotation in [('png', 'PNG', photo.rotate(90, expand=True), 90),
                                                ('pdf', 'PDF', photo, 0)]:
            with self.subTest(suffix=suffix):
                src = io.BytesIO()
                image.save(src, format)
                job = save_input(self.store, 'foto.' + suffix, src.getvalue())
                reply = self.post('/api/jobs/' + job['id'] + '/read-local', {'rotation': rotation}).json()
                self.assertEqual(reply['method'], 'ocr')
                self.assertIn('CAPUFE', reply['text'])
                self.assertIn('50.00', reply['text'])

    def test_downloads_persist_and_can_be_matched_to_a_specific_ticket(self):
        job = save_input(self.store, 'ticket.png', sample_ticket())
        key = job['id']
        self.store.save_receipt(key, RECEIPT, ready=True)
        self.client.put('/api/profile', json=PROFILE.model_dump(), headers=self.headers).raise_for_status()
        runner = self.app.state.runner
        for name, data in [('Comprobante.pdf', pdf_bytes()), ('Comprobante.xml', xml_bytes())]:
            async def save(path, data=data):
                path.write_bytes(data)
            download = SimpleNamespace(suggested_filename=name, save_as=save)
            asyncio.run(runner.save_download(download, None))
        files = self.client.get('/api/browser/downloads').json()['files']
        self.assertCountEqual([item['name'] for item in files], ['Comprobante.pdf', 'Comprobante.xml'])
        recover = '/api/jobs/' + key + '/recover-browser'
        self.assertEqual(self.post(recover, {'files': ['../profile.json']}).status_code, 422)
        self.assertEqual(self.post(recover, {'files': [files[0]['id']]}).status_code, 422)
        result = self.post(recover, {'files': [item['id'] for item in files]})
        self.assertEqual(result.json()['uuid'], FISCAL_UUID)
        self.assertEqual(self.store.get(key)['status'], 'verified')
        for item in files:
            response = self.client.get('/api/browser/downloads/' + item['id'])
            self.assertIn('attachment', response.headers['content-disposition'])
            self.assertEqual(response.content, pdf_bytes() if item['name'].endswith('.pdf') else xml_bytes())

    def test_search_and_import_selected_mail_content_are_readonly_and_idempotent(self):
        sample = EmailMessage()
        sample.set_content('<p>Reserva MX123</p><script>ignore-this</script>', subtype='html')
        sample.add_attachment(sample_ticket(), maintype='image', subtype='png', filename='compra.png')
        sample.add_attachment(pdf_bytes(), maintype='application', subtype='pdf', filename='otro.pdf')
        raw = base64.urlsafe_b64encode(sample.as_bytes()).decode()
        methods = []

        def gmail(request):
            methods.append(request.method)
            self.assertEqual(request.headers['Authorization'], 'Bearer credential-for-offline-test')
            if request.url.path.endswith('/messages'):
                self.assertEqual(request.url.params['q'], 'MX123')
                return httpx.Response(200, json={'messages': [{'id': 'correo1'}]})
            self.assertTrue(request.url.path.endswith('/messages/correo1'))
            if request.url.params['format'] == 'metadata':
                return httpx.Response(200, json={'payload': {'headers': [{'name': 'Subject', 'value': 'Reserva MX123'}]}})
            return httpx.Response(200, json={'raw': raw})

        client_class = httpx.Client
        transport = httpx.MockTransport(gmail)
        with patch('facturador.gmail.access_token', return_value='credential-for-offline-test'), \
             patch('facturador.gmail.httpx.Client', side_effect=lambda **kwargs: client_class(transport=transport, **kwargs)):
            base = '/api/gmail/receipts'
            self.assertEqual(self.post(base + '/search', {'query': 'MX123'}).json()[0]['subject'], 'Reserva MX123')
            parts = self.post(base + '/correo1/parts', {}).json()
            by_name = {part['name']: part['id'] for part in parts}
            self.assertCountEqual(by_name, ['Texto del correo', 'compra.png', 'otro.pdf'])
            chosen = {'selected': ['body', by_name['compra.png']]}
            imported = self.post(base + '/correo1/import', chosen).json()
            self.assertEqual(len(imported), 2)
            self.assertTrue(all(item['created'] for item in imported))
            repeated = self.post(base + '/correo1/import', chosen).json()
            self.assertFalse(any(item['created'] for item in repeated))
            text_id = imported[0]['id']
            self.assertEqual(self.post('/api/jobs/' + text_id + '/read-local', {}).json()['text'], 'Reserva MX123')
            self.assertEqual(read_receipt(Path(self.store.get(text_id)['source_path']))[0]['text'], 'Reserva MX123')
            self.assertEqual(self.post(base + '/correo1/import', {'selected': ['invalid']}).status_code, 422)
        self.assertEqual(len(self.store.list()), 2)
        self.assertEqual(set(methods), {'GET'})


class PortalAttachmentTests(unittest.IsolatedAsyncioTestCase):
    async def test_attachment_belongs_to_one_page_one_control_lease_and_one_selector(self):
        view = BrowserView()
        page = SimpleNamespace(url='https://comercio.example.test/facturar', is_closed=lambda: False)
        frame = SimpleNamespace(url=page.url, is_detached=lambda: False)
        page.main_frame = frame
        chooser = SimpleNamespace(element=SimpleNamespace(owner_frame=AsyncMock(return_value=frame)), set_files=AsyncMock())
        await view.show(page, 'Comercio', manual=True)
        channel = await view.take_control(view.revision)
        await view.receive_file_request(page, chooser)
        request_id = view.state()['upload']['id']
        with self.assertRaises(ValueError):
            await view.attach('invalid', request_id, None)
        payload = {'name': 'constancia.pdf', 'mimeType': 'application/pdf', 'buffer': pdf_bytes()}
        await view.attach(channel, request_id, payload)
        chooser.set_files.assert_awaited_once_with(payload, timeout=6000)
        with self.assertRaises(ValueError):
            await view.attach(channel, request_id, payload)
        await view.receive_file_request(page, chooser)
        view.invalidate_file_request(page, frame)
        self.assertIsNone(view.state()['upload'])
        frame.url = 'https://ajeno.example.test/'
        await view.receive_file_request(page, chooser)
        self.assertIsNone(view.state()['upload'])
        await view.release(channel)
        with self.assertRaises(ValueError):
            await view.visible_text(channel)
