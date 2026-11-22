import os
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock, patch

import httpx
from playwright.async_api import async_playwright

from facturador.catalog import CUSTOM_PORTAL
from facturador.models import Receipt
from facturador.portal_discovery import fetch_home, review_links, review_missing_portal, visible_links
from facturador.ticket_retry import open_ticket_portal


ORIGIN = 'https://cafeteria.example.org'
FAILED = ORIGIN + '/antiguo?boleto=solo-en-ticket'
HTML = """<style>.oculto{display:none}</style><h1>Cafetería y bar</h1>
<a href="/facturas">Facturación electrónica</a><a href="/promo">Promociones</a>
<a href="https://otro.example.org/privado">Facturar ahí</a><a class="oculto" href="/oculto">Facturación</a>
<a href="/folio?sesion=dato">Facturar</a><a aria-hidden="true" href="/interno">Descargar CFDI</a>
<a href="/#">Facturas</a><script>document.cookie='cookie=privada'</script>"""


class MerchantDiscoveryTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        folder = self.enterContext(tempfile.TemporaryDirectory())
        self.store = SimpleNamespace(root=Path(folder))
        self.receipt = Receipt(provider=CUSTOM_PORTAL, portal_url=FAILED, merchant='Cafetería y bar')
        self.page = Mock(url=FAILED, is_closed=Mock(return_value=False))
        self.page.locator.return_value.aria_snapshot = AsyncMock(return_value='Página no encontrada')
        self.response = Mock(url=FAILED, status=404, headers={})
        self.page.goto = AsyncMock(return_value=self.response)
        self.challenge = AsyncMock(return_value=None)

    async def test_root_request_pins_public_ip_tls_host_and_excludes_ticket_or_auth(self):
        seen = []
        def receive(request):
            seen.append(request)
            return httpx.Response(200, headers={'Content-Type':'text/html; charset=UTF-8'}, content=HTML.encode())
        client_class = httpx.AsyncClient
        with patch('facturador.portal_discovery.public_addresses', AsyncMock(return_value=['1.1.1.1'])) as dns, \
             patch('facturador.portal_discovery.httpx.AsyncClient', side_effect=lambda **kw: client_class(
                 **kw, transport=httpx.MockTransport(receive))):
            html, wait = await fetch_home(ORIGIN)
        dns.assert_awaited_once_with(ORIGIN)
        self.assertEqual((html,wait), (HTML,0))
        request, = seen
        self.assertEqual(str(request.url), 'https://1.1.1.1/')
        self.assertEqual(request.headers['host'], 'cafeteria.example.org')
        self.assertEqual(request.extensions['sni_hostname'], 'cafeteria.example.org')
        self.assertEqual(request.content, b'')
        self.assertFalse({'authorization','cookie','referer'} & set(request.headers))
        with patch('facturador.portal_discovery.public_addresses', AsyncMock(return_value=[])):
            self.assertEqual(await fetch_home(ORIGIN), ('',0))

    async def test_returns_proposals_only_for_original_approved_url_and_respects_cooldown(self):
        links = [ORIGIN+'/facturas']
        with patch('facturador.portal_discovery.fetch_home', AsyncMock(return_value=(HTML,0))) as fetch, \
             patch('facturador.portal_discovery.visible_links', AsyncMock(return_value=links)):
            for status in (404,410):
                self.response.status = status
                with self.assertRaisesRegex(ValueError,'Revísalo'):
                    await open_ticket_portal(self.page, FAILED, self.receipt, self.challenge, self.store, 'demo')
            fetch.assert_awaited_once_with(ORIGIN)
            self.assertEqual(self.receipt.portal_url, FAILED)
            self.assertEqual(review_links(self.store.root, 'demo', self.receipt.model_dump()),links)
            self.assertEqual(review_links(self.store.root, 'otro', self.receipt.model_dump()),[])
            changed = self.receipt.model_copy(update={'portal_url':ORIGIN+'/otra'})
            self.assertEqual(review_links(self.store.root, 'demo', changed.model_dump()),[])
            self.response.headers = {'retry-after':'600'}
            with self.assertRaisesRegex(ValueError,'600'):
                await open_ticket_portal(self.page, FAILED, self.receipt, self.challenge, self.store, 'demo')
            self.assertEqual(review_links(self.store.root,'demo',self.receipt.model_dump()), [])

    async def test_denial_rate_limit_login_and_other_origins_never_start_discovery(self):
        with patch('facturador.portal_discovery.fetch_home', AsyncMock()) as fetch:
            for status in (403,429,503):
                self.response.status = status
                await review_missing_portal(self.page,self.response,FAILED,self.receipt,self.challenge,self.store,'demo')
            self.response.status = 404
            self.challenge.return_value='captcha'
            await open_ticket_portal(self.page,FAILED,self.receipt,self.challenge,self.store,'demo')
            self.response.url='https://distinto.example.org/error'
            with self.assertRaisesRegex(ValueError, '404'):
                await open_ticket_portal(self.page,FAILED,self.receipt,self.challenge,self.store,'demo')
            fetch.assert_not_awaited()

    @unittest.skipUnless(os.path.isfile('/usr/bin/google-chrome-stable'), 'Requiere Google Chrome del contenedor')
    async def test_real_chrome_only_visible_links_and_no_original_cookies(self):
        async with async_playwright() as p:
            browser = await p.chromium.launch(executable_path='/usr/bin/google-chrome-stable')
            try:
                fiscal = await browser.new_context()
                await fiscal.add_cookies([{'name':'prueba','value':'privada','url':ORIGIN}])
                page = await fiscal.new_page()
                await page.route('**/*', lambda route: route.fulfill(status=404,content_type='text/html',body='Sin página'))
                with patch('facturador.portal_discovery.fetch_home',AsyncMock(return_value=(HTML,0))):
                    with self.assertRaisesRegex(ValueError,'Revísalo'):
                        await open_ticket_portal(page,FAILED,self.receipt,self.challenge,self.store,'bar')
                self.assertEqual(review_links(self.store.root,'bar',self.receipt.model_dump()), [ORIGIN+'/facturas'])
                self.assertEqual(page.url, FAILED)
                self.assertEqual(len(browser.contexts), 1)
                self.assertEqual(len(await fiscal.cookies()), 1)
                self.assertEqual(await visible_links(browser, '<base href="https://otro.example.org/">'+HTML, ORIGIN), [])
            finally:
                await browser.close()
