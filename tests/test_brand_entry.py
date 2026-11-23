"""Official Chrome regression for the image-only BP brand chooser."""
import os
import unittest

from playwright.async_api import async_playwright

from facturador.brand_entry import enter_bp_franchise
from facturador.models import Receipt


BP = Receipt(provider='bp', merchant='BP', amount='10.00', date='2026-11-22',
             ticket_number='12345', payment_code='04')
HOME = 'https://facturacion.grupocentra.mx/Karmi_FacturacionWeb'
BP_LINK = "javascript:{_JSL(_PAGE_,'A10','_self','','');}"


def home(*, image='BP-min__8AE4400E_.png', href=BP_LINK, extra='', count=1):
    bp = ''.join(f'''<a id="A10" href="{href}"><span id="z_A10_IMG"
      style="display:block;width:130px;height:120px;background-image:url(/KARMI_FACTURACIONWEB_WEB/res/{image})"></span></a>'''
                 for _ in range(count))
    return f'''<script>const _PAGE_='home'; function _JSL(page,id) {{
        sessionStorage.setItem('chosen',id);
        location.href='/Karmi_FacturacionWeb/PAGE_Inicio/example';
    }}</script><p>Tiene 3 dias para realizar su factura a partir de la fecha de su compra.</p>
    <a id="A13" href="javascript:void(0)"><span style="background-image:url(/res/VIP-min.png)">VIP</span></a>
    {bp}<a id="A3" href="javascript:void(0)"><span style="background-image:url(/res/PEMEX-min.png)">PEMEX</span></a>
    {extra}'''


@unittest.skipUnless(os.path.isfile('/usr/bin/google-chrome-stable'), 'Requiere Google Chrome oficial')
class BrandEntryTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.playwright = await async_playwright().start()
        self.browser = await self.playwright.chromium.launch(executable_path='/usr/bin/google-chrome-stable')
        self.page = await self.browser.new_page()
        async def route(request):
            if '/PAGE_Inicio/' in request.request.url:
                await request.fulfill(content_type='text/html', body='<label>RFC<input></label>')
            else:
                await request.fulfill(content_type='text/html', body=home())
        await self.page.route('https://facturacion.grupocentra.mx/**', route)
        self.addAsyncCleanup(self.playwright.stop)
        self.addAsyncCleanup(self.browser.close)

    async def set_home(self, **changes):
        await self.page.goto(HOME)
        await self.page.set_content(home(**changes))
        return await self.page.locator('body').aria_snapshot()

    async def test_bp_logo_enters_only_bp_form(self):
        snapshot = await self.set_home()
        self.assertTrue(await enter_bp_franchise(self.page, BP, snapshot))
        self.assertIn('/PAGE_Inicio/', self.page.url)
        self.assertEqual(await self.page.evaluate("sessionStorage.getItem('chosen')"), 'A10')
        self.assertTrue(await self.page.get_by_label('RFC').is_visible())
        self.assertFalse(await enter_bp_franchise(self.page, BP, await self.page.locator('body').aria_snapshot()))

    async def test_wrong_logo_or_ambiguous_control_never_selects_a_brand(self):
        for changes in ({'image':'PEMEX-min__B6BFDA56_.png'},
                        {'count':2},
                        {'href':'javascript:void(0)'},
                        {'extra':'<label>RFC<input></label>'}):
            with self.subTest(changes=changes):
                snapshot = await self.set_home(**changes)
                self.assertFalse(await enter_bp_franchise(self.page, BP, snapshot))
                self.assertEqual(await self.page.evaluate("sessionStorage.getItem('chosen')"), None)
        snapshot = await self.set_home()
        self.assertFalse(await enter_bp_franchise(self.page, BP.model_copy(update={'provider':'tesoro'}), snapshot))
        self.assertEqual(await self.page.evaluate("sessionStorage.getItem('chosen')"), None)
