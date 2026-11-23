"""Official Chrome checks for BP's pre-issuance RFC lookup."""
import os
import unittest

from playwright.async_api import async_playwright

from facturador.bp_recipient import prepare_bp_rfc
from facturador.models import Receipt
from test_core import PROFILE


BP = Receipt(provider='bp', merchant='BP', amount='10.00', date='2026-11-22',
             ticket_number='12345', payment_code='04')
FORM = 'https://facturacion.grupocentra.mx/Karmi_FacturacionWeb/PAGE_Inicio/example'
SEARCH = "javascript:if(clWDUtil.pfGetTraitement('PAGE_FACTURAR_GASOLINA_CFDI33',18,undefined)()){_JSL(_PAGE_,'A1','_self','','');}"


def form(*, row='RFC:', count=1, href=SEARCH, value='', extra=''):
    fields = ''.join(f'<input id="A4" value="{value}" type="text">' for _ in range(count))
    return f'''<script>const _PAGE_='form';
      const clWDUtil={{pfGetTraitement:() => () => true}};
      function _JSL(page,id) {{
        sessionStorage.setItem('chosen',id);
        location.href='/Karmi_FacturacionWeb/PAGE_Facturar_Gasolina_CFDI33/example';
    }}</script><table><tr><td>{row}</td><td>{fields}</td></tr>
    <tr><td><a id="A1" href="{href}">Buscar</a></td></tr></table>{extra}'''


@unittest.skipUnless(os.path.isfile('/usr/bin/google-chrome-stable'), 'Requiere Google Chrome oficial')
class BpRecipientTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.playwright = await async_playwright().start()
        self.browser = await self.playwright.chromium.launch(executable_path='/usr/bin/google-chrome-stable')
        self.page = await self.browser.new_page()
        async def route(request):
            if '/PAGE_Facturar_Gasolina_CFDI33/' in request.request.url:
                await request.fulfill(content_type='text/html', body='<p>Consulta del RFC</p>')
            else:
                await request.fulfill(content_type='text/html', body=form())
        await self.page.route('https://facturacion.grupocentra.mx/**', route)
        self.addAsyncCleanup(self.playwright.stop)
        self.addAsyncCleanup(self.browser.close)

    async def set_form(self, **changes):
        await self.page.goto(FORM)
        await self.page.set_content(form(**changes))

    async def test_lookup_fills_only_verified_rfc_then_opens_search_page_once(self):
        await self.set_form()
        self.assertEqual(await prepare_bp_rfc(self.page, BP, PROFILE), 'RFC')
        self.assertEqual(await self.page.locator('input#A4').input_value(), PROFILE.rfc)
        self.assertIsNone(await self.page.evaluate("sessionStorage.getItem('chosen')"))
        self.assertEqual(await prepare_bp_rfc(self.page, BP, PROFILE), 'Buscar RFC')
        self.assertIn('/PAGE_Facturar_Gasolina_CFDI33/', self.page.url)
        self.assertEqual(await self.page.evaluate("sessionStorage.getItem('chosen')"), 'A1')
        self.assertIsNone(await prepare_bp_rfc(self.page, BP, PROFILE, lookup_attempted=True))

    async def test_mismatch_or_ambiguous_form_does_not_fill_or_search(self):
        for changes in ({'row':'Folio:'}, {'count':2}, {'value':'OTRORFC000000'}, {'href':'javascript:void(0)'}):
            with self.subTest(changes=changes):
                await self.set_form(**changes)
                result = await prepare_bp_rfc(self.page, BP, PROFILE)
                if changes.get('href'):
                    self.assertEqual(result, 'RFC')
                    result = await prepare_bp_rfc(self.page, BP, PROFILE)
                self.assertIsNone(result)
                self.assertIsNone(await self.page.evaluate("sessionStorage.getItem('chosen')"))
        await self.set_form()
        self.assertIsNone(await prepare_bp_rfc(self.page, BP.model_copy(update={'provider':'tesoro'}), PROFILE))
        self.assertEqual(await self.page.locator('input#A4').input_value(), '')
