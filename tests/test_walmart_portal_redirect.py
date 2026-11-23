"""Walmart portal redirects must not be mistaken for a closed invoice page.

All browser responses in this module are synthetic. No Walmart endpoint is called.
"""

import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import AsyncMock, Mock, patch

from playwright.async_api import async_playwright

from facturador.catalog import allowed_url, provider_for_url
from facturador.db import Store
from facturador.runner import Runner


WALMART_START = 'https://facturacion.walmartmexico.com.mx/'
WALMART_CLIENTS = 'https://facturacion-clientes.walmart.com/'
CFDI_NOTICE = '''<!doctype html><html lang="es"><body>
<main><h1>Bienvenido al sistema de Facturación Electrónica</h1>
<div role="dialog" aria-label="CFDI VERSION 4.0">
  <h2>CFDI VERSION 4.0</h2>
  <p>Los datos fiscales obligatorios serán validados por el SAT.</p>
  <button>Aceptar</button>
</div></main></body></html>'''


class WalmartPortalRedirectTests(unittest.IsolatedAsyncioTestCase):
    def test_client_portal_is_walmart_and_lookalike_hosts_are_rejected(self):
        self.assertTrue(allowed_url(WALMART_START, 'walmart'))
        self.assertTrue(allowed_url(WALMART_CLIENTS, 'walmart'))
        self.assertEqual(provider_for_url(WALMART_CLIENTS), 'walmart')
        for url in (
            'http://facturacion-clientes.walmart.com/',
            'https://facturacion-clientes.walmart.com.evil.example/',
            'https://facturacion-clientes.walmart.com:8443/',
            'https://other.walmart.com/',
        ):
            with self.subTest(url=url):
                self.assertFalse(allowed_url(url, 'walmart'))

    async def test_registered_ticket_main_frame_can_follow_client_redirect(self):
        with tempfile.TemporaryDirectory() as directory:
            runner = Runner(Store(Path(directory)))
            self.addCleanup(runner.plan.close)
            runner.active_id = 'synthetic-walmart-ticket'
            page = Mock()
            frame = Mock(page=page)
            page.main_frame = frame
            route = Mock(
                request=Mock(
                    url=WALMART_CLIENTS,
                    method='GET',
                    frame=frame,
                    is_navigation_request=Mock(return_value=True),
                ),
                abort=AsyncMock(),
                continue_=AsyncMock(),
            )
            with (patch.object(runner.store, 'get', return_value={
                    'receipt': {'provider': 'walmart', 'portal_url': ''}}),
                  patch.object(runner.store, 'event') as event):
                await runner.guard_navigation(route)
            route.continue_.assert_awaited_once()
            route.abort.assert_not_awaited()
            event.assert_not_called()


@unittest.skipUnless(os.path.isfile('/usr/bin/google-chrome-stable'), 'Requiere Google Chrome oficial')
class WalmartPortalRedirectChromeTests(unittest.IsolatedAsyncioTestCase):
    async def test_navigation_to_client_portal_with_cfdi_notice_stays_authorized(self):
        async with async_playwright() as playwright:
            browser = await playwright.chromium.launch(executable_path='/usr/bin/google-chrome-stable')
            try:
                context = await browser.new_context(offline=True)
                page = await context.new_page()
                seen = []

                async def synthetic_portal(route):
                    seen.append(route.request.url)
                    if route.request.url == WALMART_START:
                        await route.fulfill(content_type='text/html', body=(
                            '<a href="' + WALMART_CLIENTS + '">Portal de clientes</a>'))
                    elif route.request.url == WALMART_CLIENTS:
                        await route.fulfill(content_type='text/html', body=CFDI_NOTICE)
                    else:
                        await route.abort()

                await context.route('**/*', synthetic_portal)
                await page.goto(WALMART_START)
                await page.get_by_role('link', name='Portal de clientes').click()
                self.assertEqual(seen, [WALMART_START, WALMART_CLIENTS])
                self.assertEqual(page.url, WALMART_CLIENTS)
                self.assertTrue(await page.get_by_role('dialog', name='CFDI VERSION 4.0').is_visible(),
                                await page.locator('body').inner_text())
                self.assertTrue(allowed_url(page.url, 'walmart'))
            finally:
                await browser.close()
