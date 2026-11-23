"""Real Chrome checks for unlabeled series and ticket fields used by many POS portals."""
import os
import unittest

from playwright.async_api import async_playwright

from facturador.ticket_lookup import prefill_ticket_lookup, prepare_tesoro_lookup, split_series_ticket
from test_core import RECEIPT


HTML = '''<table>
<tr><td>Empresa</td><td><select><option>Vianney</option></select></td></tr>
<tr><td><span>Serie</span></td><td><input type=text maxlength=10></td></tr>
<tr><td><span>Tiquet</span></td><td><input type=text maxlength=10></td></tr>
<tr><td colspan=2><input type=button value=Siguiente></td></tr>
</table>'''

TESORO = '''<form action="/GenerarCFDI/DatosTicket" method="post">
<div class="form-group"><label>Código facturación</label>
<input id="CodigoFacturacion" name="CodigoFacturacion" type="text"></div>
<input id="CodigoVerificado" type="hidden" value="False">
<button id="btnVerificar" name="btn" value="VerificarCodigo" type="submit">Verificar</button>
<button name="btn" value="GenerarFactura" type="submit" disabled>Generar factura</button>
</form><script>
window.lookups = 0;
document.querySelector('form').addEventListener('submit', event => {
  event.preventDefault();
  if (event.submitter?.value === 'VerificarCodigo') {
    window.lookups++;
    document.querySelector('#CodigoVerificado').value = 'True';
  }
});
</script>'''


class TicketLookupTests(unittest.IsolatedAsyncioTestCase):
    def test_split_only_explicit_series_and_numeric_ticket(self):
        self.assertEqual(split_series_ticket('E51T / 148,390'), ('E51T', '148390'))
        self.assertEqual(split_series_ticket('A-12 / 204'), ('A-12', '204'))
        self.assertIsNone(split_series_ticket('148390'))
        self.assertIsNone(split_series_ticket('E51T / 14O390'))

    @unittest.skipUnless(os.path.isfile('/usr/bin/google-chrome-stable'), 'Requiere Google Chrome')
    async def test_unlabeled_table_fields_are_mapped_without_guessing_position(self):
        async with async_playwright() as playwright:
            browser = await playwright.chromium.launch(executable_path='/usr/bin/google-chrome-stable')
            try:
                page = await browser.new_page()
                await page.set_content(HTML)
                self.assertEqual(await prefill_ticket_lookup(page, 'E51T / 148,390'), 'series')
                self.assertEqual(await prefill_ticket_lookup(page, 'E51T / 148,390'), 'ticket')
                self.assertIsNone(await prefill_ticket_lookup(page, 'E51T / 148,390'))
                self.assertEqual(await page.locator('tr').nth(1).locator('input').input_value(), 'E51T')
                self.assertEqual(await page.locator('tr').nth(2).locator('input').input_value(), '148390')
                await page.set_content(HTML.replace('<tr><td><span>Serie</span>',
                                                    '<tr><td><span>Serie</span><input type=text value=otro>'))
                self.assertIsNone(await prefill_ticket_lookup(page, 'E51T / 148,390'))
                await page.set_content(HTML.replace('</table>', '<tr><td>Serie</td><td><input></td></tr></table>'))
                self.assertIsNone(await prefill_ticket_lookup(page, 'E51T / 148,390'))
                await page.set_content('<table><tr><td>Serie</td><td><input></td></tr></table>'
                                       '<table><tr><td>Tiquet</td><td><input></td></tr></table>')
                self.assertIsNone(await prefill_ticket_lookup(page, 'E51T / 148,390'))
            finally:
                await browser.close()

    @unittest.skipUnless(os.path.isfile('/usr/bin/google-chrome-stable'), 'Requiere Google Chrome')
    async def test_tesoro_code_lookup_is_one_preissue_validation(self):
        async with async_playwright() as playwright:
            browser = await playwright.chromium.launch(executable_path='/usr/bin/google-chrome-stable')
            try:
                page = await browser.new_page()
                url = 'https://buzonfacturas.com/GenerarCFDI/DatosTicket'
                async def serve(route):
                    await route.fulfill(content_type='text/html; charset=utf-8', body=TESORO)
                await page.route('https://buzonfacturas.com/**', serve)
                await page.goto(url)
                receipt = RECEIPT.model_copy(update={'provider': 'tesoro', 'billing_codes': ['DEMO12A34B56']})
                self.assertEqual(await prepare_tesoro_lookup(page, receipt), 'Código facturación')
                self.assertEqual(await page.locator('#CodigoFacturacion').input_value(), 'DEMO12A34B56')
                self.assertEqual(await page.evaluate('window.lookups'), 0)
                self.assertEqual(await prepare_tesoro_lookup(page, receipt), 'Verificación del código de facturación')
                self.assertEqual(await page.evaluate('window.lookups'), 1)
                self.assertIsNone(await prepare_tesoro_lookup(page, receipt, verify_attempted=True))
                self.assertEqual(await page.evaluate('window.lookups'), 1)
                self.assertFalse(await page.locator('button[value="GenerarFactura"]').evaluate('e => e.dataset.clicked === "yes"'))
            finally:
                await browser.close()

    @unittest.skipUnless(os.path.isfile('/usr/bin/google-chrome-stable'), 'Requiere Google Chrome')
    async def test_tesoro_code_lookup_rejects_ambiguous_or_changed_forms(self):
        async with async_playwright() as playwright:
            browser = await playwright.chromium.launch(executable_path='/usr/bin/google-chrome-stable')
            try:
                page = await browser.new_page()
                url = 'https://buzonfacturas.com/GenerarCFDI/DatosTicket'
                async def serve(route):
                    await route.fulfill(content_type='text/html; charset=utf-8', body=TESORO)
                await page.route('https://buzonfacturas.com/**', serve)
                receipt = RECEIPT.model_copy(update={'provider': 'tesoro', 'billing_codes': ['DEMO12A34B56']})
                await page.goto(url)
                self.assertIsNone(await prepare_tesoro_lookup(page, receipt.model_copy(update={'billing_codes': ['A12345678', 'B12345678']})))
                self.assertIsNone(await prepare_tesoro_lookup(page, receipt.model_copy(update={'billing_codes': ['CODIGO CON ESPACIOS']})))
                self.assertIsNone(await prepare_tesoro_lookup(page, receipt.model_copy(update={'provider': 'vianney'})))
                await page.locator('#CodigoFacturacion').fill('OTROCODIGO')
                self.assertIsNone(await prepare_tesoro_lookup(page, receipt))
                self.assertEqual(await page.locator('#CodigoFacturacion').input_value(), 'OTROCODIGO')
                for changed in (TESORO.replace('</form>', '<input id="CodigoFacturacion" name="CodigoFacturacion"></form>'),
                                TESORO.replace('CÓdigo facturación', 'Otro dato').replace('Código facturación', 'Otro dato'),
                                TESORO.replace('action="/GenerarCFDI/DatosTicket"', 'action="/GenerarCFDI/Emitir"'),
                                TESORO.replace('value="VerificarCodigo"', 'value="GenerarFactura"')):
                    await page.goto(url)
                    await page.set_content(changed)
                    self.assertIsNone(await prepare_tesoro_lookup(page, receipt))
                await page.goto('https://buzonfacturas.com/OtraRuta')
                self.assertIsNone(await prepare_tesoro_lookup(page, receipt))
            finally:
                await browser.close()
