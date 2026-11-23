"""Already-issued Tesoro invoices are recovered without another issuance request."""
import asyncio
import os
import tempfile
import unittest
from pathlib import Path
from urllib.parse import parse_qs, urlencode, urlsplit

from playwright.async_api import async_playwright

from facturador.tesoro_consulta import existing_invoice_folio, recover_tesoro_consulta
from test_core import PROFILE, RECEIPT


FOLIO = 'BIV0000123'
REJECTION = f'Esta venta ya fue facturada con la serie y folio : {FOLIO}. Valide la información de la nota de venta e intente de nuevo'
CHROME = next((path for path in ('/usr/bin/google-chrome-stable',
                                  r'C:\Program Files\Google\Chrome\Application\chrome.exe') if os.path.isfile(path)), None)


def result_html(*, rows=1, amount='100.00', status='ACTIVA', rfc=PROFILE.rfc, pdf=True):
    headers = ('RFC receptor', 'Factura', 'Fecha Factura', 'Estatus', 'Importe', 'IVA', 'Total',
               'Estación', 'Zona', 'Marca', 'Opciones')
    contents = ['<table class="table">', '<tr>' + ''.join(f'<th>{item}</th>' for item in headers) + '</tr>']
    for _ in range(rows):
        links = []
        for kind in ('xml', 'pdf'):
            if kind == 'pdf' and not pdf:
                continue
            query = urlencode({'btn': 'btn' + kind.upper(), 'rfc': rfc, 'folioInterno': FOLIO})
            links.append(f'<a title="Descargar archivo {kind}" href="/CFDI/DescargarFactura?{query}">{kind}</a>')
        cells = [rfc, FOLIO, '2026-11-23', status, '86.21', '13.79', amount, '11380',
                 'Zona', 'Tesoro', ' '.join(links)]
        contents.append('<tr>' + ''.join(f'<td>{cell}</td>' for cell in cells) + '</tr>')
    return ''.join(contents) + '</table>'


class _Store:
    def __init__(self):
        self.events = []

    def event(self, key, kind, detail):
        self.events.append((key, kind, detail))


class _Viewer:
    def __init__(self):
        self.page = None

    async def show(self, page, title):
        self.page = page


class _Runner:
    def __init__(self, context, folder):
        self.context = context
        self.folder = folder
        self.page_job = {}
        self.viewer = _Viewer()
        self.store = _Store()
        self.downloads = set()
        self.recover_calls = 0
        context.on('page', self._register)

    def _register(self, page):
        def capture(download):
            async def save():
                await download.save_as(self.folder / download.suggested_filename)
            task = asyncio.create_task(save())
            self.downloads.add(task)
            task.add_done_callback(self.downloads.discard)
        page.on('download', capture)

    async def recover(self, key, profile):
        self.recover_calls += 1
        files = list(self.folder.iterdir())
        return ({path.suffix for path in files} == {'.xml', '.pdf'}
                and all(path.stat().st_size > 0 for path in files))


@unittest.skipUnless(CHROME, 'Requiere Google Chrome oficial')
class TesoroConsultaTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.folder = Path(self.temp.name)
        self.playwright = await async_playwright().start()
        self.browser = await self.playwright.chromium.launch(executable_path=CHROME)
        self.context = await self.browser.new_context(accept_downloads=True)
        self.runner = _Runner(self.context, self.folder)
        self.row_count = 1
        self.amount = '100.00'
        self.status = 'ACTIVA'
        self.rfc = PROFILE.rfc
        self.pdf = True
        self.download_requests = []

        async def route(request):
            path = urlsplit(request.request.url).path
            query = parse_qs(urlsplit(request.request.url).query)
            if path == '/GenerarCFDI/DatosTicket':
                await request.fulfill(content_type='text/html', body='<h1>Formulario original</h1>')
            elif path == '/CFDI/Index':
                await request.fulfill(content_type='text/html', body='''
                <form action="/CFDI/DescargarFactura" method="post">
                  <input id="RFC" name="RFC"><input id="FolioInterno" name="FolioInterno">
                  <button type="submit">Buscar</button></form>''')
            elif path == '/CFDI/DescargarFactura' and request.request.method == 'POST':
                await request.fulfill(content_type='text/html', body=result_html(
                    rows=self.row_count, amount=self.amount, status=self.status, rfc=self.rfc, pdf=self.pdf))
            elif path == '/CFDI/DescargarFactura' and query.get('btn') in (['btnXML'], ['btnPDF']):
                kind = query['btn'][0][-3:].lower()
                self.download_requests.append(kind)
                await request.fulfill(body=b'%PDF synthetic' if kind == 'pdf' else b'<xml/>',
                                      headers={'Content-Disposition': 'attachment; filename="invoice.' + kind + '"',
                                               'Content-Type': 'application/pdf' if kind == 'pdf' else 'application/xml'})
            else:
                await request.abort()

        await self.context.route('https://buzonfacturas.com/**', route)
        self.original = await self.context.new_page()
        await self.original.goto('https://buzonfacturas.com/GenerarCFDI/DatosTicket')
        await self.runner.viewer.show(self.original, 'Tesoro')
        self.receipt = RECEIPT.model_copy(update={'provider': 'tesoro', 'amount': '100.00'})

    async def asyncTearDown(self):
        await self.browser.close()
        await self.playwright.stop()
        self.temp.cleanup()

    async def test_recovers_one_exact_active_invoice_and_preserves_original_tab(self):
        recovered = await recover_tesoro_consulta(
            self.runner, self.original, 'synthetic', PROFILE, self.receipt, REJECTION)
        self.assertTrue(recovered)
        self.assertEqual(self.download_requests, ['xml', 'pdf'])
        self.assertFalse(self.original.is_closed())
        self.assertTrue(self.original.url.endswith('/GenerarCFDI/DatosTicket'))
        self.assertEqual(self.runner.recover_calls, 2)
        self.assertEqual([event[1] for event in self.runner.store.events], ['tesoro_consulta'])

    async def test_does_not_download_ambiguous_or_mismatched_invoice(self):
        for changes in ({'row_count': 2}, {'rfc': 'OTR010101AAA'}, {'status': 'CANCELADA'},
                        {'amount': '99.00'}, {'pdf': False}):
            for key, value in changes.items():
                setattr(self, key, value)
            recovered = await recover_tesoro_consulta(
                self.runner, self.original, 'synthetic', PROFILE, self.receipt, REJECTION)
            self.assertFalse(recovered)
            self.assertEqual(self.download_requests, [])
            for key in changes:
                setattr(self, key, {'row_count': 1, 'rfc': PROFILE.rfc, 'status': 'ACTIVA',
                                    'amount': '100.00', 'pdf': True}[key])


class FolioParserTests(unittest.TestCase):
    def test_only_portal_already_invoiced_message_yields_folio(self):
        self.assertEqual(existing_invoice_folio(REJECTION), FOLIO)
        self.assertIsNone(existing_invoice_folio('El folio ' + FOLIO + ' está pendiente.'))
        self.assertIsNone(existing_invoice_folio(REJECTION + ' ' + REJECTION))


if __name__ == '__main__':
    unittest.main()
