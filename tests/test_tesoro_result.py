"""Post-lookup rejection must not be mistaken for a ready invoice."""
import os
import unittest

from playwright.async_api import async_playwright

from facturador.tesoro_result import tesoro_verification_failure
from test_core import PROFILE, RECEIPT


CODE = 'DEMO12A34B56'
URL = 'https://buzonfacturas.com/GenerarCFDI/DatosTicket'


def html(*, verified='False', station='', sale='', total='0.00', alert='', payment='04', use='G03'):
    return f'''<form action="/GenerarCFDI/DatosTicket" method="post">
      <input id="CodigoVerificado" type="hidden" value="{verified}">
      <input id="CodigoFacturacion" value="{CODE}">
      <input id="RFC" value="{PROFILE.rfc}" readonly>
      <input id="RazonSocial" value="{PROFILE.name}" readonly>
      <input id="EstacionId" value="{station}" readonly>
      <input id="NumeroDeVenta" value="{sale}" readonly>
      <input id="TotalVenta" value="{total}" readonly>
      <select id="FormaDePago"><option value="{payment}">{payment}</option></select>
      <select id="UsoCFDI"><option value="{use}">{use}</option></select>
      <button name="btn" value="GenerarFactura" {'disabled' if verified == 'False' else ''}>Generar factura</button>
    </form><div class="alert" {'hidden' if not alert else ''}>{alert}</div>'''


@unittest.skipUnless(os.path.isfile('/usr/bin/google-chrome-stable'), 'Requiere Google Chrome')
class TesoroResultTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.playwright = await async_playwright().start()
        self.browser = await self.playwright.chromium.launch(executable_path='/usr/bin/google-chrome-stable')
        self.page = await self.browser.new_page()
        await self.page.route('https://buzonfacturas.com/**', lambda route: route.fulfill(
            content_type='text/html; charset=utf-8', body=html()))
        await self.page.goto(URL)
        self.receipt = RECEIPT.model_copy(update={'provider': 'tesoro', 'billing_codes': [CODE],
                                                 'ticket_number': '2404899'})

    async def asyncTearDown(self):
        await self.browser.close()
        await self.playwright.stop()

    async def test_preserves_visible_rejection_then_still_blocks_after_alert_fades(self):
        await self.page.set_content(html(alert='Error al procesar la venta: El código de facturación no pertenece a ninguna sucursal Znak.'))
        reason = await tesoro_verification_failure(self.page, self.receipt, PROFILE)
        self.assertIn('no pertenece a ninguna sucursal Znak', reason)
        await self.page.locator('.alert').evaluate('element => element.remove()')
        reason = await tesoro_verification_failure(self.page, self.receipt, PROFILE)
        self.assertIn('no verificó', reason)
        self.assertIn('importe', reason)

    async def test_only_complete_verified_sale_continues(self):
        await self.page.set_content(html(verified='True', station='11380', sale='2404899', total='100.00'))
        self.assertIsNone(await tesoro_verification_failure(self.page, self.receipt, PROFILE))
        self.assertIsNone(await tesoro_verification_failure(self.page, self.receipt, PROFILE, issue_gate=True))
        await self.page.locator('#TotalVenta').evaluate('element => element.value = "0.00"')
        self.assertIn('no dejó datos', await tesoro_verification_failure(self.page, self.receipt, PROFILE))
        await self.page.goto('https://buzonfacturas.com/GenerarCFDI/Index?avanzada=0')
        self.assertIsNone(await tesoro_verification_failure(self.page, self.receipt, PROFILE))
        self.assertIn('sólo se aprueba', await tesoro_verification_failure(
            self.page, self.receipt, PROFILE, issue_gate=True))

    async def test_amount_sale_and_payment_mismatches_block_issuance(self):
        await self.page.set_content(html(verified='True', station='11380', sale='2404899', total='99.00'))
        self.assertIn('importe', await tesoro_verification_failure(self.page, self.receipt, PROFILE))
        await self.page.set_content(html(verified='True', station='11380', sale='2404898', total='100.00'))
        self.assertIn('número de venta', await tesoro_verification_failure(self.page, self.receipt, PROFILE))
        await self.page.set_content(html(verified='True', station='11380', sale='2404899', total='100.00', payment='01'))
        self.assertIn('forma de pago', await tesoro_verification_failure(self.page, self.receipt, PROFILE, issue_gate=True))
        await self.page.set_content(html(verified='True', station='11380', sale='2404899', total='100.00', use='G01'))
        self.assertIn('uso CFDI', await tesoro_verification_failure(self.page, self.receipt, PROFILE, issue_gate=True))


if __name__ == '__main__':
    unittest.main()
