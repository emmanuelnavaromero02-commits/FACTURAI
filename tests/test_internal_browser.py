import json
import os
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import AsyncMock, Mock, patch

from PIL import Image
from playwright.async_api import Error as BrowserError
import zxingcpp

from facturador.ai import receipt_image
from facturador.catalog import CATALOG
from facturador.db import Store
from facturador.runner import Runner


class BrowserStartupTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        folder = tempfile.TemporaryDirectory()
        self.addCleanup(folder.cleanup)
        self.runner = Runner(Store(Path(folder.name)))
        self.addCleanup(self.runner.plan.close)
        self.context = Mock(pages=[], route=AsyncMock())
        self.chromium = AsyncMock(return_value=self.context)
        self.firefox = AsyncMock(return_value=self.context)
        self.runner.playwright = Mock(stop=AsyncMock(), chromium=Mock(launch_persistent_context=self.chromium),
                                      firefox=Mock(launch_persistent_context=self.firefox))

    async def test_headed_chrome_uses_its_native_identity_and_own_persistent_profile(self):
        profile = self.runner.store.root / 'browser_profile_chrome'
        with patch.dict(os.environ, FACTURADOR_CHROME_HEADED='1'):
            expected_env = {'env': {**os.environ, 'TMPDIR': str(profile.resolve() / '.runtime')}} if sys.platform.startswith('linux') else {}
            await self.runner.start_browser()
            await self.runner.start_browser()
        for registration in self.context.on.call_args_list:
            if registration.args[0] == 'close':
                self.addCleanup(registration.args[1], self.context)
        self.assertEqual(self.runner.engine, 'Google Chrome')
        self.chromium.assert_awaited_once_with(
            profile, executable_path='/usr/bin/google-chrome-stable',
            headless=False, chromium_sandbox=True, accept_downloads=True, service_workers='block',
            viewport={'width': 1280, 'height': 850}, timeout=15_000, **expected_env)
        self.context.route.assert_awaited_once_with('**/*', self.runner.guard_navigation)

    async def test_explicit_chrome_reports_its_failure_without_substituting_another_browser(self):
        self.chromium.side_effect = BrowserError('Chrome unavailable')
        with patch.dict(os.environ, FACTURADOR_CHROME_HEADED='1'):
            with self.assertRaisesRegex(ValueError, 'No se pudo iniciar Google Chrome'):
                await self.runner.start_browser()
        self.chromium.assert_awaited_once()
        self.firefox.assert_not_awaited()
        self.assertIsNone(self.runner.context)
        self.assertIsNone(self.runner.playwright)

    async def test_legacy_mode_skips_chrome_and_keeps_firefox_recovery(self):
        self.chromium.side_effect = BrowserError('Chromium unavailable')
        with patch.dict(os.environ, FACTURADOR_CHROME_HEADED='0'):
            await self.runner.start_browser()
        self.chromium.assert_awaited_once()
        self.assertEqual(self.runner.engine, 'Firefox')
        self.assertEqual(self.firefox.await_args.args, (self.runner.store.root / 'browser_profile_firefox',))


class ReceiptImageTests(unittest.TestCase):
    def test_both_codes_survive_black_margins_and_are_read_without_inference(self):
        canvas = Image.new('L', (1100, 600), 0)
        canvas.paste(255, (35, 60, 1065, 540))
        for number, (text, kind) in enumerate([
            ('FACTURAI-00001', zxingcpp.BarcodeFormat.Code128),
            ('https://example.test/ticket', zxingcpp.BarcodeFormat.QRCode),
        ]):
            code = zxingcpp.write_barcode_to_image(zxingcpp.create_barcode(text, kind), scale=3)
            image = Image.frombytes('L', (code.shape[1], code.shape[0]), bytes(code))
            canvas.paste(image, (80, 95 + number * 220))
        parts = receipt_image(canvas)
        found = json.loads(parts[1]['text'].split(': ', 1)[1])
        self.assertCountEqual(found, ['FACTURAI-00001', 'https://example.test/ticket'])
        self.assertEqual(canvas.getpixel((0, 0)), 0)
        self.assertEqual(canvas.size, (1100, 600))


class PortalBrowserTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.folder = tempfile.TemporaryDirectory()
        self.addCleanup(self.folder.cleanup)
        self.runner = Runner(Store(Path(self.folder.name)))
        self.addCleanup(self.runner.plan.close)
        self.page = Mock()
        self.page.is_closed.return_value = False
        self.page.goto = AsyncMock()
        self.page.set_content = AsyncMock()
        self.runner.context = Mock(new_page=AsyncMock(return_value=self.page))
        self.runner.start_browser = AsyncMock()

    async def test_home_and_catalog_reuse_the_manual_page(self):
        await self.runner.open_portal()
        self.page.goto.assert_awaited_once_with('about:blank')
        self.assertIn(CATALOG['capufe']['url'], self.page.set_content.await_args.args[0])
        await self.runner.open_portal('capufe')
        self.runner.context.new_page.assert_awaited_once()
        self.assertEqual(self.page.goto.await_args.args[0], CATALOG['capufe']['url'])
        self.assertTrue(self.runner.viewer.manual)

    async def test_manual_portal_does_not_launch_for_an_arbitrary_url_or_interrupt_a_job(self):
        with self.assertRaises(ValueError):
            await self.runner.open_portal('https://example.test/')
        self.runner.job_tasks['ticket'] = object()
        with self.assertRaises(ValueError):
            await self.runner.open_portal('capufe')
        self.runner.start_browser.assert_not_awaited()

    async def test_printed_qr_preserves_the_original_query_and_rejects_other_origins(self):
        url = CATALOG['capufe']['url'] + '?folio=O0-I1'
        await self.runner.open_portal(url=url)
        self.assertEqual(self.page.goto.await_args.args[0], url)
        with self.assertRaises(ValueError):
            await self.runner.open_portal(url='https://ejemplo.test/?folio=O0-I1')
        self.assertEqual(self.page.goto.await_count, 1)
