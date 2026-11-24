"""Walmart issue proof uses only synthetic pages in offline official Chrome."""

import os
import unittest

from playwright.async_api import async_playwright

from facturador.models import Profile, Receipt
from facturador.walmart_issue import walmart_issue_button, walmart_lookup_proof


ROOT = 'https://facturacion-clientes.walmart.com/'
LOOKUP = ROOT + 'ticket'
ADDRESS = ROOT + 'address'
TC = '1234567890123456789012'
TR = '01234'
PROFILE = Profile(rfc='XAXX010101000', name='PRUEBA AUTOMATIZACION', postal_code='76146',
                  regime='612', cfdi_use='G03', email='prueba@example.com')
RECEIPT = Receipt(provider='walmart', merchant='Walmart', amount='100.00', date='2026-11-20',
                  ticket_number=TC, billing_codes=['TR#' + TR], payment_code='08')
LOOKUP_HTML = f'''<!doctype html><html><head><meta charset="utf-8"></head><body><form>
  <label>RFC<input value="{PROFILE.rfc}"></label>
  <label>Código Postal<input value="{PROFILE.postal_code}"></label>
  <label>Número de Ticket<input value="{TC}"></label>
  <label># Transacción<input value="{TR}"></label>
  <button type="button">Continuar</button>
</form></body></html>'''
ADDRESS_HTML = f'''<!doctype html><html><head><meta charset="utf-8"></head><body><form>
  <label>RFC<input value="{PROFILE.rfc}"></label>
  <label>Razón Social<input value="{PROFILE.name}"></label>
  <label>Código Postal<input value="{PROFILE.postal_code}"></label>
  <label>Correo Electrónico<input value="{PROFILE.email}"></label>
  <label>Régimen Fiscal<select><option value="">Seleccione</option>
    <option value="612" selected>Personas Físicas con Actividades Empresariales y Profesionales</option></select></label>
  <label>Uso Factura<select><option value="">Seleccione</option>
    <option value="G03" selected>Gastos en general</option></select></label>
  <label>Forma de Pago<select><option value="08" selected>Vales de despensa</option></select></label>
  <button type="button" onclick="window.submitted = true">Aceptar</button>
</form></body></html>'''


@unittest.skipUnless(os.path.isfile('/usr/bin/google-chrome-stable'), 'Requiere Google Chrome oficial')
class WalmartIssueChromeTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.playwright = await async_playwright().start()
        self.addAsyncCleanup(self.playwright.stop)
        self.browser = await self.playwright.chromium.launch(executable_path='/usr/bin/google-chrome-stable')
        self.addAsyncCleanup(self.browser.close)
        self.context = await self.browser.new_context(offline=True)
        self.addAsyncCleanup(self.context.close)
        self.page = await self.context.new_page()

    async def load(self, url, html):
        async def handler(route):
            if route.request.url == url:
                await route.fulfill(content_type='text/html', body=html)
            else:
                await route.abort()
        await self.context.unroute('**/*')
        await self.context.route('**/*', handler)
        await self.page.goto(url)

    async def lookup(self):
        await self.load(LOOKUP, LOOKUP_HTML)
        proof = await walmart_lookup_proof(self.page, PROFILE, RECEIPT)
        self.assertIsNotNone(proof)
        return proof

    async def test_exact_lookup_and_fiscal_form_allow_only_its_accept_button(self):
        proof = await self.lookup()
        await self.load(ADDRESS, ADDRESS_HTML)
        button = await walmart_issue_button(self.page, PROFILE, RECEIPT, proof)
        self.assertIsNotNone(button)
        await button.click()
        self.assertTrue(await self.page.evaluate('window.submitted === true'))

    async def test_lookup_requires_labeled_tr_and_exact_tc(self):
        await self.load(LOOKUP, LOOKUP_HTML)
        self.assertIsNone(await walmart_lookup_proof(
            self.page, PROFILE, RECEIPT.model_copy(update={'billing_codes': [TR]})))
        self.assertIsNone(await walmart_lookup_proof(
            self.page, PROFILE, RECEIPT.model_copy(update={'billing_codes': ['TR#' + TR, 'TR#98765']})))
        self.assertIsNone(await walmart_lookup_proof(
            self.page, PROFILE, RECEIPT.model_copy(update={'ticket_number': TC[:-1] + '3'})))

    async def test_lookup_can_cotejar_unique_fields_without_html_form(self):
        await self.load(LOOKUP, LOOKUP_HTML.replace('<form>', '<main>').replace('</form>', '</main>'))
        self.assertIsNotNone(await walmart_lookup_proof(self.page, PROFILE, RECEIPT))

    async def test_lookup_refuses_mismatched_visible_values(self):
        for wrong in (LOOKUP_HTML.replace(f'value="{TC}"', 'value="0000000000000000000000"'),
                      LOOKUP_HTML.replace(f'value="{TR}"', 'value="99999"'),
                      LOOKUP_HTML.replace(f'value="{PROFILE.rfc}"', 'value="OTRO010101000"'),
                      LOOKUP_HTML.replace(f'value="{PROFILE.postal_code}"', 'value="00000"')):
            await self.load(LOOKUP, wrong)
            self.assertIsNone(await walmart_lookup_proof(self.page, PROFILE, RECEIPT))

    async def test_issue_refuses_mismatched_fiscal_or_payment_fields(self):
        proof = await self.lookup()
        changes = [
            (PROFILE.rfc, 'OTRO010101000'), (PROFILE.name, 'OTRO RECEPTOR'),
            (PROFILE.postal_code, '00000'), (PROFILE.email, 'otro@example.com'),
            ('value="612" selected', 'value="605" selected'),
            ('value="G03" selected', 'value="G01" selected'),
            ('value="08" selected', 'value="01" selected'),
        ]
        for old, new in changes:
            await self.load(ADDRESS, ADDRESS_HTML.replace(old, new))
            self.assertIsNone(await walmart_issue_button(self.page, PROFILE, RECEIPT, proof), old)

    async def test_saved_payment_is_enough_when_address_has_no_payment_control(self):
        proof = await self.lookup()
        without_payment = ADDRESS_HTML.replace(
            '  <label>Forma de Pago<select><option value="08" selected>Vales de despensa</option></select></label>\n', '')
        await self.load(ADDRESS, without_payment)
        self.assertIsNotNone(await walmart_issue_button(self.page, PROFILE, RECEIPT, proof))

    async def test_issue_refuses_modal_duplicate_button_other_host_and_missing_lookup(self):
        proof = await self.lookup()
        await self.load(ADDRESS, ADDRESS_HTML + '<div role="dialog">Aviso <button>Aceptar</button></div>')
        self.assertIsNone(await walmart_issue_button(self.page, PROFILE, RECEIPT, proof))
        await self.load(ADDRESS, ADDRESS_HTML.replace('</form>', '<button>Aceptar</button></form>'))
        self.assertIsNone(await walmart_issue_button(self.page, PROFILE, RECEIPT, proof))
        await self.load(ADDRESS, ADDRESS_HTML)
        self.assertIsNone(await walmart_issue_button(self.page, PROFILE, RECEIPT, None))
        await self.load('https://other.example/address', ADDRESS_HTML)
        self.assertIsNone(await walmart_issue_button(self.page, PROFILE, RECEIPT, proof))

    async def test_issue_requires_form_ownership_and_selected_use(self):
        proof = await self.lookup()
        await self.load(ADDRESS, ADDRESS_HTML.replace('<button type="button" onclick=',
                                                      '</form><button type="button" onclick=').replace('</form></body>', '</body>'))
        self.assertIsNone(await walmart_issue_button(self.page, PROFILE, RECEIPT, proof))
        await self.load(ADDRESS, ADDRESS_HTML.replace('value="G03" selected', 'value="G03"'))
        self.assertIsNone(await walmart_issue_button(self.page, PROFILE, RECEIPT, proof))
