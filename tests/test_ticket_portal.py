import socket
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock, patch

from facturador.catalog import CUSTOM_PORTAL, allowed_url, portal_suggestions, public_portal_origin, resolves_publicly
from facturador.db import Store
from facturador.models import Receipt
from facturador.runner import Runner, visible_challenge


URL = 'https://Facturas.Example.COM:443/inicio?folio=A-123'
ORIGIN = 'https://facturas.example.com'


class TicketPortalTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        directory = self.enterContext(tempfile.TemporaryDirectory())
        self.store = Store(Path(directory))
        self.runner = Runner(self.store)
        self.addCleanup(self.runner.plan.close)

    def test_printed_links_are_suggestions_and_authorization_is_exact_origin(self):
        other = 'https://otras.example.com/'
        self.assertEqual(public_portal_origin(URL), ORIGIN)
        self.assertEqual(portal_suggestions([URL], f'Factura en {URL} o en {other}.'), [URL, other])
        self.assertTrue(allowed_url(ORIGIN + '/registro', CUSTOM_PORTAL, URL))
        for url in (other, 'https://facturas.example.com.otro.mx', ORIGIN + ':8443', ORIGIN.replace('https:', 'http:')):
            self.assertFalse(allowed_url(url, CUSTOM_PORTAL, URL))
        for url in ('http://comercio.mx/', 'https://usuario@comercio.mx/', 'https://localhost/',
                    'https://127.0.0.1/', 'https://10.1/', 'https://[::1]/', 'https://srv.internal/',
                    'https://example.org\\@comercio.mx', 'https://co\nmercio.mx/', 'https://comercio.mx:99999'):
            with self.subTest(url=url):
                self.assertIsNone(public_portal_origin(url))

    async def test_dns_rejects_private_and_mixed_results(self):
        def record(ip):
            return (socket.AF_INET, socket.SOCK_STREAM, socket.IPPROTO_TCP, '', (ip, 443))
        for addresses in (['127.0.0.1'], ['1.1.1.1', '169.254.169.254'], ['168.63.129.16'], []):
            with self.subTest(addresses=addresses), patch('asyncio.BaseEventLoop.getaddrinfo', new_callable=AsyncMock) as lookup:
                lookup.return_value = [record(ip) for ip in addresses]
                self.assertFalse(await resolves_publicly(URL))

    def test_changed_url_requires_new_approval_and_distinct_merchants_can_share_codes(self):
        first = self.store.add('first.png', 'first')[0]['id']
        receipt = Receipt(provider=CUSTOM_PORTAL, portal_url=URL, merchant='Tienda Uno', amount='100',
                          date='2026-11-01', payment_code='01', billing_codes=['A-123'])
        self.store.save_receipt(first, receipt, ready=True)
        second = self.store.add('second.png', 'second')[0]['id']
        self.assertEqual(self.store.save_receipt(second, receipt.model_copy(update={'merchant':'Tienda Dos'}), ready=True)['status'], 'ready')
        self.assertFalse(self.store.queue(first))
        self.assertFalse(self.store.queue(first, confirmed_url=URL + 'X'))
        self.assertTrue(self.store.queue(first, confirmed_url=URL))

    async def test_manual_address_requires_confirmation_before_navigation(self):
        page = Mock(url=URL, goto=AsyncMock(), is_closed=Mock(return_value=False))
        self.runner.context = Mock(new_page=AsyncMock(return_value=page))
        self.runner.start_browser = AsyncMock()
        for origin in ('', 'https://otras.example.com'):
            with self.assertRaises(ValueError):
                await self.runner.open_portal(url=URL, confirmed_origin=origin)
        page.goto.assert_not_awaited()
        with patch('facturador.runner.resolves_publicly', new_callable=AsyncMock, return_value=True):
            await self.runner.open_portal(url=URL, confirmed_origin=ORIGIN)
        page.goto.assert_awaited_once_with(URL, wait_until='domcontentloaded', timeout=30_000)
        self.assertTrue(self.runner.viewer.manual)
        frame = Mock(page=page)
        page.main_frame = frame
        route = Mock(request=Mock(frame=frame, method='POST', url='https://otro.example.com/registro',
                                  is_navigation_request=Mock(return_value=False)),
                     abort=AsyncMock(), continue_=AsyncMock())
        with patch('facturador.runner.resolves_publicly', new_callable=AsyncMock, return_value=True):
            await self.runner.guard_navigation(route)
        route.abort.assert_awaited_once()
        route.continue_.assert_not_awaited()

    async def test_otp_screen_requires_operator_before_model_observation(self):
        self.assertEqual(await visible_challenge(SimpleNamespace(frames=[]), 'Ingresa tu código de verificación'), 'login')
