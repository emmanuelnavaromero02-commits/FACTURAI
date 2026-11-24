"""Dismiss only Walmart's informational CFDI 4.0 notice, never a form consent.

Chrome runs offline. Every page is fulfilled by this test; no merchant is contacted.
"""

import os
import unittest

from playwright.async_api import async_playwright

from facturador.runner import dismiss_walmart_address_notice, dismiss_walmart_cfdi_notice


WALMART_CLIENTS = 'https://facturacion-clientes.walmart.com/'
WALMART_ADDRESS = WALMART_CLIENTS + 'address'
OTHER_HOST = 'https://unrelated.example/'
NOTICE = '''<!doctype html><html lang="es"><body>
<main><h1>Bienvenido al sistema de Facturación Electrónica</h1>
<div role="dialog" aria-label="CFDI VERSION 4.0">
  <h2>CFDI VERSION 4.0</h2>
  <p>Los datos fiscales obligatorios serán validados por el SAT.</p>
  <button onclick="window.noticeAccepted = true; this.closest('[role=dialog]').remove()">Aceptar</button>
</div></main></body></html>'''
FORM = '''<!doctype html><html lang="es"><body>
<form><label>RFC<input name="rfc"></label>
<button type="button" onclick="window.formAccepted = true">Aceptar</button></form>
</body></html>'''
NOTICE_WITH_FORM_BUTTON = '''<!doctype html><html lang="es"><body>
<div role="dialog" aria-label="CFDI VERSION 4.0">
  <h2>CFDI VERSION 4.0</h2>
  <p>Los datos fiscales obligatorios serán validados por el SAT.</p>
</div>
<form><label>RFC<input name="rfc"></label>
<button type="button" onclick="window.formAccepted = true">Aceptar</button></form>
</body></html>'''
ADDRESS_NOTICE = '''<!doctype html><html lang="es"><body>
<form><label>Razón social<input name="name"></label>
<button type="button" onclick="window.formAccepted = true">Aceptar</button></form>
<div role="dialog" aria-label="Aviso de información fiscal">
  <p><strong>Importante:</strong> Para proteger sus datos personales y garantizar la correcta emisión de
  sus facturas, a partir de ahora será necesario capturar nuevamente su información fiscal en cada
  solicitud de facturación.</p>
  <button onclick="window.noticeAccepted = true; this.closest('[role=dialog]').remove()">Aceptar</button>
</div></body></html>'''


@unittest.skipUnless(os.path.isfile('/usr/bin/google-chrome-stable'), 'Requiere Google Chrome oficial')
class WalmartCfdiNoticeChromeTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.playwright = await async_playwright().start()
        self.addAsyncCleanup(self.playwright.stop)
        self.browser = await self.playwright.chromium.launch(executable_path='/usr/bin/google-chrome-stable')
        self.addAsyncCleanup(self.browser.close)
        self.context = await self.browser.new_context(offline=True)
        self.addAsyncCleanup(self.context.close)
        self.page = await self.context.new_page()

    async def load_synthetic(self, url, html):
        seen = []

        async def route_handler(route):
            seen.append(route.request.url)
            if route.request.url == url:
                await route.fulfill(content_type='text/html; charset=utf-8', body=html)
            else:
                await route.abort()

        await self.context.route('**/*', route_handler)
        await self.page.goto(url)
        self.assertEqual(seen, [url])
        return await self.page.locator('body').aria_snapshot()

    async def test_dismisses_visible_walmart_cfdi_notice(self):
        snapshot = await self.load_synthetic(WALMART_CLIENTS, NOTICE)
        self.assertTrue(await dismiss_walmart_cfdi_notice(self.page, snapshot))
        self.assertTrue(await self.page.evaluate('window.noticeAccepted === true'))
        self.assertEqual(await self.page.get_by_role('dialog').count(), 0)

    async def test_dismisses_bootstrap_style_informational_modal(self):
        html = NOTICE.replace('role="dialog" aria-label="CFDI VERSION 4.0"', 'class="modal"').replace(
            "this.closest('[role=dialog]')", "this.closest('.modal')")
        snapshot = await self.load_synthetic(WALMART_CLIENTS, html)
        self.assertTrue(await dismiss_walmart_cfdi_notice(self.page, snapshot))
        self.assertTrue(await self.page.evaluate('window.noticeAccepted === true'))
        self.assertEqual(await self.page.locator('.modal').count(), 0)

    async def test_does_not_accept_plain_walmart_form(self):
        snapshot = await self.load_synthetic(WALMART_CLIENTS, FORM)
        self.assertFalse(await dismiss_walmart_cfdi_notice(self.page, snapshot))
        self.assertFalse(await self.page.evaluate('window.formAccepted === true'))

    async def test_does_not_accept_form_button_beside_notice(self):
        snapshot = await self.load_synthetic(WALMART_CLIENTS, NOTICE_WITH_FORM_BUTTON)
        self.assertFalse(await dismiss_walmart_cfdi_notice(self.page, snapshot))
        self.assertFalse(await self.page.evaluate('window.formAccepted === true'))

    async def test_does_not_accept_notice_containing_form_controls(self):
        html = NOTICE.replace('<button onclick=', '<label>RFC<input name="rfc"></label><button onclick=')
        snapshot = await self.load_synthetic(WALMART_CLIENTS, html)
        self.assertFalse(await dismiss_walmart_cfdi_notice(self.page, snapshot))
        self.assertFalse(await self.page.evaluate('window.noticeAccepted === true'))

    async def test_does_not_accept_notice_on_unrelated_host(self):
        snapshot = await self.load_synthetic(OTHER_HOST, NOTICE)
        self.assertFalse(await dismiss_walmart_cfdi_notice(self.page, snapshot))
        self.assertFalse(await self.page.evaluate('window.noticeAccepted === true'))
        self.assertTrue(await self.page.get_by_role('dialog').is_visible())

    async def test_does_not_accept_hidden_notice(self):
        snapshot = await self.load_synthetic(WALMART_CLIENTS, NOTICE.replace(
            '<div role="dialog"', '<div hidden role="dialog"'))
        self.assertFalse(await dismiss_walmart_cfdi_notice(self.page, snapshot))
        self.assertFalse(await self.page.evaluate('window.noticeAccepted === true'))

    async def test_dismisses_only_address_notice_not_fiscal_form(self):
        snapshot = await self.load_synthetic(WALMART_ADDRESS, ADDRESS_NOTICE)
        self.assertTrue(await dismiss_walmart_address_notice(self.page, snapshot))
        self.assertTrue(await self.page.evaluate('window.noticeAccepted === true'))
        self.assertFalse(await self.page.evaluate('window.formAccepted === true'))

    async def test_dismisses_address_notice_in_modal_container(self):
        html = ADDRESS_NOTICE.replace('role="dialog" aria-label="Aviso de información fiscal"', 'class="modal"').replace(
            "this.closest('[role=dialog]')", "this.closest('.modal')")
        snapshot = await self.load_synthetic(WALMART_ADDRESS, html)
        self.assertTrue(await dismiss_walmart_address_notice(self.page, snapshot))
        self.assertFalse(await self.page.evaluate('window.formAccepted === true'))

    async def test_address_notice_requires_exact_portal_path(self):
        snapshot = await self.load_synthetic(WALMART_CLIENTS, ADDRESS_NOTICE)
        self.assertFalse(await dismiss_walmart_address_notice(self.page, snapshot))
        self.assertFalse(await self.page.evaluate('window.noticeAccepted === true'))

    async def test_address_notice_requires_full_message(self):
        html = ADDRESS_NOTICE.replace('capturar nuevamente su información fiscal', 'enviar su factura')
        snapshot = await self.load_synthetic(WALMART_ADDRESS, html)
        self.assertFalse(await dismiss_walmart_address_notice(self.page, snapshot))
        self.assertFalse(await self.page.evaluate('window.noticeAccepted === true'))

    async def test_address_notice_cannot_confirm_a_form_within_dialog(self):
        html = ADDRESS_NOTICE.replace('  <button onclick="window.noticeAccepted',
                                      '  <input name="rfc"><button onclick="window.noticeAccepted')
        snapshot = await self.load_synthetic(WALMART_ADDRESS, html)
        self.assertFalse(await dismiss_walmart_address_notice(self.page, snapshot))
        self.assertFalse(await self.page.evaluate('window.noticeAccepted === true'))

    async def test_address_notice_cannot_click_form_button_outside_dialog(self):
        html = ADDRESS_NOTICE.replace('  <button onclick="window.noticeAccepted = true; this.closest(\'[role=dialog]\').remove()">Aceptar</button>', '')
        snapshot = await self.load_synthetic(WALMART_ADDRESS, html)
        self.assertFalse(await dismiss_walmart_address_notice(self.page, snapshot))
        self.assertFalse(await self.page.evaluate('window.formAccepted === true'))
