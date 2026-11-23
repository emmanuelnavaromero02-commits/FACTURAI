"""A rejected Vianney lookup is reported and its document search never emits."""
import asyncio
import os
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock, patch
from urllib.parse import urlsplit

from playwright.async_api import async_playwright

from facturador.catalog import allowed_url
from facturador.models import Receipt
from facturador.vianney_consulta import (lookup_rejected, rejection_for_current_ticket,
                                         recover_vianney_consulta)


class VianneyMessageTests(unittest.TestCase):
    def test_only_the_official_pdf_endpoint_is_allowed(self):
        self.assertTrue(allowed_url(
            'https://facturas2.vianney.mx/cgi-bin/cfds/cfdsORACLE33?test=1', 'vianney'))
        self.assertFalse(allowed_url('https://facturas2.vianney.mx/another-path', 'vianney'))

    def test_explicit_ambiguous_portal_rejection(self):
        page = '- text: "Parametros incorrectos: El tiquet no existe o ya ha sido facturado."'
        self.assertEqual(lookup_rejected(page),
                         'Parametros incorrectos: El tiquet no existe o ya ha sido facturado.')
        self.assertIsNone(lookup_rejected('El tiquet se encuentra disponible para factura.'))
        self.assertIsNone(lookup_rejected('There is no row at position 0.'))


CONSULTA = '''<form><select id="MainContent_DropDownList2">
<option value="pdf">PDF</option><option value="xml">XML</option></select>
<input id="MainContent_serie"><input id="MainContent_tiquet">
<input id="MainContent_Button1" type="button" value="Siguiente"
 onclick="location.href='/download/'+document.querySelector('#MainContent_DropDownList2').value"></form>'''


class FakeRunner:
    def __init__(self):
        self.store = SimpleNamespace(event=Mock())
        self.downloads = set()
        self.formats = []

    def on_download(self, download):
        async def save():
            await download.path()
            self.formats.append(download.suggested_filename)
        task = asyncio.create_task(save())
        self.downloads.add(task)
        task.add_done_callback(self.downloads.discard)

    async def recover(self, key, _):
        if hasattr(self.store, 'root') and list((self.store.root / 'downloads' / key / 'recibidos').glob('*.pdf')):
            self.formats.append('factura.pdf')
        return set(self.formats) == {'factura.pdf', 'factura.xml'}


@unittest.skipUnless(os.path.isfile('/usr/bin/google-chrome-stable'), 'Requiere Google Chrome oficial')
class VianneyConsultaChromeTests(unittest.IsolatedAsyncioTestCase):
    async def test_persisted_rejection_requires_same_visible_ticket_and_prior_submission(self):
        async with async_playwright() as playwright:
            browser = await playwright.chromium.launch(executable_path='/usr/bin/google-chrome-stable')
            try:
                page = await browser.new_page()
                html = ('<input id="MainContent_serie" value="E51T">'
                        '<input id="MainContent_tiquet" value="148390">'
                        '<p>Parametros incorrectos: El tiquet no existe o ya ha sido facturado.</p>')
                await page.route('https://poscfdi.vianney.com.mx/**',
                                 lambda route: route.fulfill(content_type='text/html', body=html))
                await page.goto('https://poscfdi.vianney.com.mx/')
                snapshot = await page.locator('body').aria_snapshot()
                receipt = Receipt(provider='vianney', ticket_number='E51T / 148,390')
                expected = 'Parametros incorrectos: El tiquet no existe o ya ha sido facturado.'
                self.assertEqual(await rejection_for_current_ticket(
                    page, snapshot, receipt, submitted_here=False, prior_submission=True), expected)
                self.assertIsNone(await rejection_for_current_ticket(
                    page, snapshot, receipt, submitted_here=False, prior_submission=False))
                other = receipt.model_copy(update={'ticket_number': 'E51T / 148,391'})
                self.assertIsNone(await rejection_for_current_ticket(
                    page, snapshot, other, submitted_here=False, prior_submission=True))
                await page.locator('#MainContent_serie').fill('OTRA')
                self.assertIsNone(await rejection_for_current_ticket(
                    page, snapshot, receipt, submitted_here=False, prior_submission=True))
                self.assertEqual(await rejection_for_current_ticket(
                    page, snapshot, receipt, submitted_here=True, prior_submission=False), expected)
            finally:
                await browser.close()

    async def test_pdf_and_xml_are_queried_once_without_emission(self):
        requests = []
        async with async_playwright() as playwright:
            browser = await playwright.chromium.launch(executable_path='/usr/bin/google-chrome-stable')
            try:
                page = await browser.new_page(accept_downloads=True)
                runner = FakeRunner()
                page.on('download', runner.on_download)

                async def portal(route):
                    path = urlsplit(route.request.url).path
                    requests.append((route.request.method, path))
                    if path == '/Default.aspx':
                        await route.fulfill(content_type='text/html', body='<p>El tiquet no existe o ya ha sido facturado.</p>')
                    elif path == '/Consulta.aspx':
                        await route.fulfill(content_type='text/html', body=CONSULTA)
                    elif path in {'/download/pdf', '/download/xml'}:
                        kind = path.rsplit('/', 1)[1]
                        await route.fulfill(status=200, headers={
                            'Content-Type': 'application/pdf' if kind == 'pdf' else 'application/xml',
                            'Content-Disposition': f'attachment; filename=factura.{kind}'},
                            body=b'%PDF-1.4\n' if kind == 'pdf' else b'<xml/>')
                    else:
                        await route.fulfill(status=404, body='Missing')

                await page.route('https://poscfdi.vianney.com.mx/**', portal)
                await page.goto('https://poscfdi.vianney.com.mx/Default.aspx')
                receipt = Receipt(provider='vianney', ticket_number='E51T / 148,390')
                result = await recover_vianney_consulta(runner, page, 'synthetic', None, receipt)
                self.assertTrue(result)
                self.assertEqual(set(runner.formats), {'factura.pdf', 'factura.xml'})
                self.assertEqual(requests, [('GET', '/Default.aspx'), ('GET', '/Consulta.aspx'),
                                            ('GET', '/download/pdf'), ('GET', '/Consulta.aspx'),
                                            ('GET', '/download/xml')])
                self.assertEqual(runner.store.event.call_count, 2)
            finally:
                await browser.close()

    async def test_unrecognized_consulta_does_not_submit(self):
        async with async_playwright() as playwright:
            browser = await playwright.chromium.launch(executable_path='/usr/bin/google-chrome-stable')
            try:
                page = await browser.new_page()
                runner = FakeRunner()

                async def portal(route):
                    path = urlsplit(route.request.url).path
                    body = '<p>Error previo</p>' if path == '/Default.aspx' else '<p>Formulario desconocido</p>'
                    await route.fulfill(content_type='text/html', body=body)

                await page.route('https://poscfdi.vianney.com.mx/**', portal)
                await page.goto('https://poscfdi.vianney.com.mx/Default.aspx')
                receipt = Receipt(provider='vianney', ticket_number='E51T / 148,390')
                self.assertFalse(await recover_vianney_consulta(runner, page, 'synthetic', None, receipt))
                runner.store.event.assert_not_called()
            finally:
                await browser.close()

    async def test_chrome_inline_pdf_is_fetched_and_saved_with_same_ticket_xml(self):
        pdf = b'%PDF-1.4\nsynthetic\n%%EOF'
        inline = '''<form><select id="MainContent_DropDownList2">
        <option value="pdf">PDF</option><option value="xml">XML</option></select>
        <input id="MainContent_serie"><input id="MainContent_tiquet">
        <input id="MainContent_Button1" type="button" value="Siguiente"
        onclick="location.href=document.querySelector('#MainContent_DropDownList2').value==='pdf'
        ?'https://facturas2.vianney.mx/cgi-bin/cfds/cfdsORACLE33?test=1':'/download/xml'"></form>'''
        with TemporaryDirectory() as directory:
            async with async_playwright() as playwright:
                browser = await playwright.chromium.launch(executable_path='/usr/bin/google-chrome-stable')
                try:
                    page = await browser.new_page(accept_downloads=True)
                    runner = FakeRunner()
                    runner.store.root = Path(directory)
                    page.on('download', runner.on_download)

                    async def portal(route):
                        address = urlsplit(route.request.url)
                        if address.path == '/Consulta.aspx':
                            await route.fulfill(content_type='text/html', body=inline)
                        elif address.path == '/Default.aspx':
                            await route.fulfill(content_type='text/html', body='<p>Consulta</p>')
                        elif address.path == '/download/xml':
                            await route.fulfill(status=200, headers={
                                'Content-Type': 'application/xml',
                                'Content-Disposition': 'attachment; filename=factura.xml'}, body=b'<xml/>')
                        elif address.path == '/cgi-bin/cfds/cfdsORACLE33':
                            await route.fulfill(status=200, headers={
                                'Content-Type': 'application/pdf',
                                'Content-Disposition': 'inline',
                                'Content-Length': str(len(pdf))}, body=pdf)
                        else:
                            await route.fulfill(status=404, body='Missing')

                    await page.route('https://poscfdi.vianney.com.mx/**', portal)
                    await page.route('https://facturas2.vianney.mx/**', portal)
                    await page.goto('https://poscfdi.vianney.com.mx/Default.aspx')
                    receipt = Receipt(provider='vianney', ticket_number='E51T / 148,390')
                    response = SimpleNamespace(status=200, headers={'content-type': 'application/pdf',
                                                                      'content-length': str(len(pdf))},
                                               body=AsyncMock(return_value=pdf))
                    with patch.object(page.context.request, 'get', new=AsyncMock(return_value=response)) as get:
                        result = await recover_vianney_consulta(runner, page, 'synthetic', None, receipt)
                    self.assertTrue(result)
                    get.assert_awaited_once()
                    self.assertEqual(urlsplit(get.await_args.args[0]).hostname, 'facturas2.vianney.mx')
                    candidates = list((Path(directory) / 'downloads' / 'synthetic' / 'recibidos').glob('*.pdf'))
                    self.assertEqual(len(candidates), 1)
                    self.assertEqual(candidates[0].read_bytes(), pdf)
                    self.assertEqual(runner.store.event.call_count, 3)
                finally:
                    await browser.close()
