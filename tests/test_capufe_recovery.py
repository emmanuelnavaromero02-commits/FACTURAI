import base64
import sys
import unittest
from contextlib import asynccontextmanager
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

from playwright.async_api import Error as BrowserError

sys.path.insert(0, str(Path(__file__).resolve().parent))

from facturador import capufe_recovery
from facturador.capufe_recovery import (CONTEXT, HOST, LOOKUP, LookupProblem, _is_lookup, _lookup_browser, _payload,
                                        _reply, recover_capufe)
from facturador.models import Receipt
from test_core import PROFILE
from test_screen_plans import PortalCase


class TollLookupTests(unittest.TestCase):
    def test_network_gate_only_accepts_the_official_read_operation_for_this_code(self):
        key = 'X' * 18
        req = SimpleNamespace(method='POST', post_data=None, url=f'https://{HOST}{LOOKUP}?codigo={key}')
        self.assertTrue(_is_lookup(req, key))
        self.assertFalse(_is_lookup(req, 'Y' * 18))
        for url in (req.url.replace(HOST, 'otro.example'), req.url.replace(LOOKUP, '/timbrar'), req.url + '&emitir=1'):
            self.assertFalse(_is_lookup(SimpleNamespace(method='POST', post_data=None,url=url), key))

    def test_an_ambiguous_negative_is_not_evidence_of_nonissuance(self):
        for msg in ('El código aún no ha sido facturado.', 'El código no se encuentra facturado'):
            self.assertEqual(_reply({'mensaje':msg})[:2], ({},True))
        for msg in ('No existe información con los valores ingresados', 'El código ya se encuentra facturado.',
                    'Límite de consultas', 'Código inválido', 'Factura cancelada'):
            self.assertEqual(_reply({'mensaje':msg})[:2], ({},False))

    def test_recover_actual_bytes_and_preserve_the_cotejo_as_a_separate_step(self):
        pdf = b'%PDF-1.7\nficticio'
        xml = b'<?xml version="1.0"?><cfdi:Comprobante />'
        files, negative, _ = _reply({'documentos':{'pdf':base64.b64encode(pdf).decode(),'xml':xml.decode()},
                                  'mensaje':'El código aún no ha sido facturado.'})
        self.assertEqual(files, {'pdf':pdf, 'xml':xml})
        self.assertFalse(negative)
        self.assertEqual(_reply({'pdf':'archivos no disponibles'})[:2], ({},False))

    def test_direct_files_are_candidates_and_html_errors_cannot_authorize_another_issue(self):
        for kind, raw in [('zip',b'PK\x03\x04doc'),('pdf',b'%PDF-1.6'),('xml',b'<cfdi:Comprobante/>')]:
            self.assertEqual(_payload(raw), ({kind:raw}, False, ''))
        with self.assertRaises(LookupProblem):
            _payload(b'<html>Consulta no disponible</html>')


class LookupBrowserTests(unittest.IsolatedAsyncioTestCase):
    """Which browser hosts the lookup. Doubles only: nothing is launched here."""

    def engine(self, *, name='Chromium', browser='own', start=None, refuses=False):
        own = SimpleNamespace(close=AsyncMock(), new_context=AsyncMock(
            side_effect=BrowserError('sin contextos') if refuses else None,
            return_value=SimpleNamespace(close=AsyncMock())))
        runner = SimpleNamespace(start_browser=AsyncMock(side_effect=start), engine=name,
                                 context=SimpleNamespace(browser=own if browser == 'own' else browser))
        return runner, own

    def launcher(self):
        """Stands in for Playwright: records the launch of a browser of its own and its closing."""
        launched = SimpleNamespace(close=AsyncMock())
        launch = AsyncMock(return_value=launched)

        @asynccontextmanager
        async def playwright():
            yield SimpleNamespace(chromium=SimpleNamespace(launch=launch))
        return patch.object(capufe_recovery, 'async_playwright', playwright), launch, launched

    async def test_engine_browser_hosts_the_lookup_and_no_other_is_launched(self):
        runner, own = self.engine()
        patcher, launch, _ = self.launcher()
        with patcher:
            async with _lookup_browser(runner) as browser:
                self.assertIs(browser, own)
        # It asked the engine for its browser the usual way (where its place is counted)...
        runner.start_browser.assert_awaited_once()
        # ...checked that it gives private contexts, launched nothing and left the engine's browser open.
        own.new_context.assert_awaited_once_with(**CONTEXT)
        own.new_context.return_value.close.assert_awaited_once()
        launch.assert_not_awaited()
        own.close.assert_not_awaited()
        self.assertEqual(CONTEXT, {'service_workers': 'block', 'accept_downloads': False})

    async def test_without_a_usable_engine_browser_one_is_launched_and_closed_as_before(self):
        cases = {'sin motor': None,
                 'motor Firefox': self.engine(name='Firefox')[0],
                 'contexto sin navegador': self.engine(browser=None)[0],
                 'navegador que no da contextos': self.engine(refuses=True)[0],
                 'motor que no arranca': self.engine(start=ValueError('No se pudo iniciar el navegador interno.'))[0]}
        for name, runner in cases.items():
            with self.subTest(name):
                patcher, launch, launched = self.launcher()
                with patcher, patch.dict('os.environ', {'FACTURADOR_CHROMIUM_PATH': '/ruta/chromium',
                                                        'FACTURADOR_CHROME_HEADED': '0'}):
                    async with _lookup_browser(runner) as browser:
                        self.assertIs(browser, launched)
                        launched.close.assert_not_awaited()
                launch.assert_awaited_once_with(executable_path='/ruta/chromium', headless=True, chromium_sandbox=True)
                launched.close.assert_awaited_once()
                own = runner.context.browser if runner is not None else None
                if own is not None:
                    own.close.assert_not_awaited()  # Whatever the engine has open is never closed from here.


class EngineLookupTests(PortalCase):
    """The real engine with Chrome: the lookup of each code runs in a private context of that same browser."""

    async def test_each_code_gets_a_private_context_of_the_engine_browser(self):
        site = 'https://consulta.peajes.example'
        receipt = Receipt(provider='capufe', merchant='CAPUFE', amount='100.00', date='2026-11-13',
                          billing_codes=['A' * 18, 'B' * 18], payment_code='01')
        key = self.add(receipt)
        engine = self.runner.context
        await engine.add_cookies([{'name': 'sesion', 'value': 'del-perfil-del-motor', 'url': site}])
        seen = []

        async def lookup(page, code):
            sent = []

            async def answer(route):
                sent.append(await route.request.all_headers())
                await route.fulfill(content_type='text/html; charset=utf-8', body='<h1>Consulta lista</h1>')
            await page.route('**/*', answer)
            await page.goto(site + '/recuperar')
            seen.append({'code': code, 'context': page.context, 'browser': page.context.browser,
                         'heading': await page.inner_text('h1'), 'cookie': sent[0].get('cookie'),
                         'cookies': await page.context.cookies(), 'open': len(page.context.browser.contexts)})
            return {}, True, ''

        def no_other_browser():
            raise AssertionError('La consulta no debe lanzar otro navegador.')

        with patch.object(capufe_recovery, '_lookup', lookup), \
                patch.object(capufe_recovery, 'async_playwright', no_other_browser):
            report = await recover_capufe(self.store, key, receipt, PROFILE, runner=self.runner)
        self.assertEqual(report, 'CAPUFE informó que el código de peaje aún no ha sido facturado.')
        self.assertEqual([item['code'] for item in seen], ['A' * 18, 'B' * 18])
        for item in seen:
            # Same browser as the engine, another context, and nothing of the engine's profile in it.
            self.assertIs(item['browser'], engine.browser)
            self.assertIsNot(item['context'], engine)
            self.assertEqual((item['heading'], item['cookie'], item['cookies']), ('Consulta lista', None, []))
            self.assertEqual(item['open'], 2)  # the engine's profile and this code's context, one at a time
        self.assertIsNot(seen[0]['context'], seen[1]['context'])
        # Every context of the lookup is closed; the engine's browser and its profile go on as they were.
        self.assertEqual(engine.browser.contexts, [engine])
        self.assertIs(self.runner.context, engine)
        self.assertEqual([cookie['value'] for cookie in await engine.cookies(site)], ['del-perfil-del-motor'])


if __name__ == '__main__':
    unittest.main()
