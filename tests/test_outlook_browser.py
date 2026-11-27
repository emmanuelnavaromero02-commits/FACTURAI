"""The «Outlook / Hotmail» option of the mail card in a real browser, at phone (390 px) and desktop (1280 px) widths.

The real application runs with fictitious data. Microsoft's sign-in page is an intercepted route
that answers like the stand-in of the other Outlook tests, and the mailbox is the IMAP/SMTP double
that only takes its tokens: nothing leaves the machine.
"""
import asyncio
import json
import tempfile
import threading
import time
import unittest
from pathlib import Path
from unittest.mock import patch
from urllib.parse import parse_qs, urlsplit

import uvicorn
from playwright.async_api import async_playwright, expect

from acceso_dobles import PASSWORD, make_accounts, make_entrance, quiet_app, use_master_key
from correo_dobles import FakeImap, FakeSmtp, trusted_context
from facturador import outlook
from microsoft_dobles import CLIENT, IMAP_SCOPE, MARKS, SEND_SCOPE, FakeMicrosoft, visit
from test_acceso_paginas import EXECUTABLE, free_port
from test_outlook import ADDRESS, without_outlook_settings

WIDTHS = (390, 1280)
# A whole trip (Microsoft, the panel again, the token and the mailbox) on a machine that runs other tests too.
TRIP = 20_000
MICROSOFT = 'https://login.microsoftonline.com'


def installation(**settings):
    """Outlook settings of the installation for one test (none by default)."""
    def mark(test):
        test.installation = settings
        return test
    return mark


class OutlookBrowserCase(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        # Set before anything else starts, and the same for the whole test.
        settings = getattr(getattr(type(self), self._testMethodName), 'installation', {})
        self.enterContext(without_outlook_settings(**settings))
        self.microsoft = FakeMicrosoft()
        self.addCleanup(self.microsoft.close)
        self.imap = FakeImap(user=ADDRESS, password='nadie-usa-esta-clave', tokens=self.microsoft.access)
        self.smtp = FakeSmtp('starttls', user=ADDRESS, password='nadie-usa-esta-clave', tokens=self.microsoft.access)
        self.addCleanup(self.imap.close)
        self.addCleanup(self.smtp.close)
        # The address of Microsoft's page stays the real one: the browser never reaches it, the route answers.
        for target in (patch.object(outlook, 'TOKEN', self.microsoft.token),
                       patch.object(outlook, 'IMAP_SERVER', ('127.0.0.1', self.imap.port)),
                       patch.object(outlook, 'SMTP_SERVERS', {'personal': ('127.0.0.1', self.smtp.port),
                                                              'trabajo': ('127.0.0.1', self.smtp.port)}),
                       patch('facturador.buzon.tls_context', trusted_context)):
            target.start()
            self.addCleanup(target.stop)
        outlook._FORCED.clear()
        self.playwright = await async_playwright().start()
        self.addAsyncCleanup(self.playwright.stop)
        self.browser = await self.playwright.chromium.launch(executable_path=EXECUTABLE, headless=True)
        self.addAsyncCleanup(self.browser.close)
        self.asked, self.errors = [], []
        self.declines = False

    async def serve(self, app, port):
        server = uvicorn.Server(uvicorn.Config(app, host='127.0.0.1', port=port, log_level='warning'))
        thread = threading.Thread(target=server.run, daemon=True)
        thread.start()
        deadline = time.monotonic() + 15
        while not server.started and time.monotonic() < deadline:
            await asyncio.sleep(.05)
        self.assertTrue(server.started)

        async def stop():
            server.should_exit = True
            await asyncio.to_thread(thread.join, 15)
        self.addAsyncCleanup(stop)

    async def microsoft_page(self, route):
        """Microsoft's sign-in: a page of another site that sends the browser back to the registered address."""
        asked = urlsplit(route.request.url)
        self.asked.append({key: values[0] for key, values in parse_qs(asked.query).items()} | {'path': asked.path})
        self.microsoft.declines = self.declines
        status, location = await asyncio.to_thread(visit, self.microsoft.url + asked.path + '?' + asked.query)
        if status != 302:
            return await route.fulfill(status=400, content_type='text/html; charset=utf-8',
                                       body='<p>Microsoft no reconoce esa dirección de regreso.</p>')
        await route.fulfill(content_type='text/html; charset=utf-8', body=(
            '<!doctype html><title>Iniciar sesión</title><p>Volviendo a FACTURAI…</p>'
            f'<script>location.replace({json.dumps(location)})</script>'))

    async def context_at(self, width):
        context = await self.browser.new_context(viewport={'width': width, 'height': 900}, service_workers='block')
        self.addAsyncCleanup(context.close)
        await context.route(MICROSOFT + '/**', self.microsoft_page)
        context.on('page', lambda page: page.on('pageerror', lambda error: self.errors.append(str(error))))
        return context

    async def assert_fits(self, page):
        overflow = await page.evaluate('document.documentElement.scrollWidth - window.innerWidth')
        self.assertLessEqual(overflow, 0)

    async def assert_clean(self, *pages):
        for page in pages:
            shown = page.url + await page.content() + json.dumps(await page.evaluate(
                '[...document.querySelectorAll("input")].map(input => input.value)'))
            for mark in MARKS:
                self.assertNotIn(mark, shown)

    async def to_microsoft_and_back(self, context, button):
        """Press a button that opens Microsoft's page in another tab; that tab comes back to the panel."""
        async with context.expect_page() as opened:
            await button.click()
        return await opened.value


@unittest.skipUnless(EXECUTABLE, 'Requiere Google Chrome o Chromium')
class OutlookCardInBrowser(OutlookBrowserCase):
    async def start_app(self):
        from facturador import app as panel
        folder = tempfile.TemporaryDirectory(ignore_cleanup_errors=True)
        self.addCleanup(folder.cleanup)
        root, port = Path(folder.name), free_port()
        app = panel.create_app(root, port=port)
        for worker in (app.state.automation, app.state.whatsapp.watcher):
            quiet = patch.object(worker, 'start', lambda: None)
            quiet.start()
            self.addCleanup(quiet.stop)
        await self.serve(app, port)
        self.microsoft.redirects.clear()
        self.microsoft.redirects.add('http://localhost/auth/outlook/callback')
        return root, port

    async def test_register_connect_allow_sending_test_and_disconnect(self):
        for width in WIDTHS:
            with self.subTest(width=width):
                root, port = await self.start_app()
                context = await self.context_at(width)
                page = await context.new_page()
                await page.goto(f'http://127.0.0.1:{port}/#ajustes')
                card, box = page.locator('#mail-card'), page.locator('#outlook-box')
                await expect(box.get_by_role('heading', name='Outlook / Hotmail')).to_be_visible()
                await expect(page.locator('#outlook-pill')).to_have_text('Sin configurar')
                await expect(page.locator('#outlook-summary')).to_contain_text('Microsoft ya no acepta contraseña de aplicación')
                await expect(box.get_by_role('button', name='Conectar Outlook', exact=True)).to_be_hidden()
                # The form for other providers is still there, below, and the card says nothing is connected.
                await expect(card.get_by_role('heading', name='Otro proveedor de correo')).to_be_visible()
                await expect(card.get_by_label('Servidor de entrada (IMAP)')).to_be_visible()
                await expect(page.locator('#mail-pill')).to_have_text('Por conectar')
                self.assertTrue(await page.evaluate(
                    "document.querySelector('#outlook-box').compareDocumentPosition(document.querySelector('#mail-form')) & 4"))

                # The help: five short steps with the exact address to register.
                await box.get_by_text('¿Cómo obtengo el identificador?').click()
                steps = box.locator('.mail-steps li')
                await expect(steps).to_have_count(5)
                await expect(steps.nth(2)).to_contain_text('Cualquier inquilino de Entra ID + cuentas personales de Microsoft')
                await expect(steps.nth(3)).to_contain_text('Aplicaciones móviles y de escritorio')
                await expect(page.locator('#outlook-redirect')).to_have_text(f'http://localhost:{port}/auth/outlook/callback')
                await expect(page.locator('#outlook-imap')).to_contain_text('Reenvío e IMAP')
                await self.assert_fits(page)

                field = box.get_by_label('Identificador de tu aplicación de Microsoft')
                await field.fill('esto no es un identificador')
                await box.get_by_role('button', name='Guardar identificador').click()
                await expect(page.locator('#outlook-feedback')).to_have_text(outlook.BAD_CLIENT)
                await expect(page.locator('#outlook-feedback')).to_have_class('error')
                await field.fill(f' {CLIENT.upper()} ')
                await box.get_by_role('button', name='Guardar identificador').click()
                await expect(page.locator('#outlook-feedback')).to_have_text('Identificador guardado. Ahora pulsa Conectar Outlook.')
                await expect(page.locator('#outlook-pill')).to_have_text('Lista para conectar')
                await expect(field).to_have_value(CLIENT)

                # The person cancels at Microsoft: the tab that comes back says so and nothing is connected.
                self.declines = True
                back = await self.to_microsoft_and_back(context, box.get_by_role('button', name='Conectar Outlook', exact=True))
                await expect(back.locator('#outlook-feedback')).to_have_text(outlook.REASONS['consentimiento'], timeout=TRIP)
                await expect(back.locator('#outlook-feedback')).to_have_class('error')
                await expect(back).to_have_url(f'http://localhost:{port}/#ajustes')
                await expect(page.locator('#outlook-feedback')).to_have_text('Autoriza en la pestaña de Microsoft y vuelve aquí.')
                await back.close()
                self.declines = False

                back = await self.to_microsoft_and_back(context, box.get_by_role('button', name='Conectar Outlook', exact=True))
                await expect(back.locator('#outlook-feedback')).to_contain_text(f'Outlook conectado como {ADDRESS}.', timeout=TRIP)
                await expect(back.locator('#outlook-pill')).to_have_text('Conectado')
                await expect(back.get_by_role('heading', name='Ajustes', exact=True)).to_be_visible()
                # The code left the address bar at once; the panel handed it over itself.
                await expect(back).to_have_url(f'http://localhost:{port}/#ajustes')
                asked = self.asked[-1]
                self.assertEqual((asked['path'], asked['client_id'], asked['response_mode'], asked['redirect_uri']),
                                 ('/common/oauth2/v2.0/authorize', CLIENT, 'fragment', f'http://localhost:{port}/auth/outlook/callback'))
                self.assertEqual(asked['scope'].split()[-1], IMAP_SCOPE)
                await self.assert_fits(back)
                await self.assert_clean(page, back)
                await back.close()

                # Back in the first tab the state is read again by itself.
                await page.evaluate("window.dispatchEvent(new Event('focus'))")
                await expect(page.locator('#outlook-feedback')).to_have_text(f'Outlook conectado como {ADDRESS}.', timeout=TRIP)
                await expect(page.locator('#outlook-pill')).to_have_text('Conectado')
                await expect(page.locator('#outlook-summary')).to_have_text(
                    f'Conectado como {ADDRESS}. Lee tu buzón; no envía correos. El permiso se renueva solo.')
                await expect(page.locator('#mail-pill')).to_have_text('Solo lectura')
                await expect(page.locator('#outlook-app-form')).to_be_hidden()
                await expect(box.get_by_role('button', name='Conectar Outlook', exact=True)).to_be_hidden()
                await box.get_by_role('button', name='Probar Outlook').click()
                await expect(page.locator('#outlook-feedback')).to_have_text('La conexión con tu correo de Outlook funciona.', timeout=TRIP)
                await self.assert_fits(page)

                # Sending is asked apart.
                back = await self.to_microsoft_and_back(context, box.get_by_role('button', name='Permitir que también envíe'))
                await expect(back.locator('#outlook-summary')).to_contain_text('puede pedir facturas por correo', timeout=TRIP)
                self.assertEqual(self.asked[-1]['scope'].split()[-2:], [IMAP_SCOPE, SEND_SCOPE])
                await back.close()
                await page.evaluate("window.dispatchEvent(new Event('focus'))")
                await expect(page.locator('#mail-pill')).to_have_text('Conectado')
                await expect(box.get_by_role('button', name='Permitir que también envíe')).to_be_hidden()
                self.assertTrue(outlook.can_send(root))

                # Microsoft stops renewing the permission: the option says so and offers to connect again.
                self.microsoft.revoke(ADDRESS)
                await box.get_by_role('button', name='Probar Outlook').click()
                await expect(page.locator('#outlook-feedback')).to_have_text(outlook.RECONNECT, timeout=TRIP)
                await expect(page.locator('#outlook-pill')).to_have_text('Requiere volver a conectar')
                await expect(page.locator('#outlook-summary')).to_have_text(
                    f'Microsoft dejó de renovar el permiso de {ADDRESS}: se retiró o caducó. '
                    'Pulsa «Volver a conectar Outlook» para darlo otra vez.')
                await expect(page.locator('#outlook-help')).to_be_hidden()
                await expect(page.locator('#mail-pill')).to_have_text('Por conectar')
                await expect(box.get_by_role('button', name='Probar Outlook')).to_be_hidden()
                await self.assert_fits(page)
                self.microsoft.revoked.clear()
                back = await self.to_microsoft_and_back(context, box.get_by_role('button', name='Volver a conectar Outlook'))
                await expect(back.locator('#outlook-pill')).to_have_text('Conectado', timeout=TRIP)
                await back.close()
                await page.evaluate("window.dispatchEvent(new Event('focus'))")
                await expect(page.locator('#outlook-pill')).to_have_text('Conectado')

                page.once('dialog', lambda prompt: asyncio.ensure_future(prompt.accept()))
                await box.get_by_role('button', name='Desconectar Outlook').click()
                await expect(page.locator('#outlook-feedback')).to_have_text('Outlook desconectado.')
                await expect(page.locator('#outlook-pill')).to_have_text('Lista para conectar')
                await expect(page.locator('#mail-pill')).to_have_text('Por conectar')
                self.assertFalse(outlook.connected(root))
                await self.assert_clean(page)
                await self.assert_fits(page)
                await context.close()
        self.assertEqual(self.errors, [])

    async def test_form_for_other_providers_points_microsoft_mailboxes_to_the_option(self):
        _, port = await self.start_app()
        context = await self.context_at(390)
        page = await context.new_page()
        await page.goto(f'http://127.0.0.1:{port}/#ajustes')
        card = page.locator('#mail-card')
        await card.get_by_label('Usuario de la cuenta').fill(ADDRESS)
        await card.get_by_label('Contraseña de aplicación').fill('clave-de-aplicacion-ficticia')
        await card.get_by_label('Servidor de entrada (IMAP)').fill('outlook.office365.com')
        await card.get_by_role('button', name='Conectar correo').click()
        await expect(page.locator('#mail-feedback')).to_contain_text('Conecta ese correo en «Outlook / Hotmail»', timeout=TRIP)
        await self.assert_fits(page)
        self.assertEqual((self.imap.commands, self.errors), ([], []))


@unittest.skipUnless(EXECUTABLE, 'Requiere Google Chrome o Chromium')
class OutlookBehindTheEntrance(OutlookBrowserCase):
    """Several customers: the session cookie (SameSite=Strict) does not travel when the browser comes back from Microsoft."""

    async def start_entrance(self):
        folder = tempfile.TemporaryDirectory(ignore_cleanup_errors=True)
        self.addCleanup(folder.cleanup)
        use_master_key(self)
        port = free_port()
        self.origin = f'http://localhost:{port}'
        accounts = make_accounts(Path(folder.name))
        self.account = accounts.create_user('ana', PASSWORD)
        self.root = Path(folder.name) / 'usuarios' / self.account['id']
        # The panel includes the add-on that the entrance serves, as the product does behind it.
        gate = make_entrance(Path(folder.name), accounts=accounts, origin=self.origin, clock=time.monotonic, factory=quiet_app)
        await self.serve(gate, port)
        self.microsoft.redirects.clear()
        self.microsoft.redirects.add(self.origin + '/auth/outlook/callback')

    async def sign_in(self, context):
        page = await context.new_page()
        await page.goto(self.origin + '/#ajustes')
        form = page.locator('#form-entrar')
        await form.get_by_label('Usuario').fill('ana')
        await form.get_by_label('Contraseña', exact=True).fill(PASSWORD)
        await form.get_by_role('button', name='Entrar').click()
        await page.wait_for_url(self.origin + '/#ajustes')
        await expect(page.get_by_role('heading', name='Ajustes', exact=True)).to_be_visible()
        return page

    @installation(FACTURADOR_OUTLOOK_APLICACION=CLIENT)
    async def test_person_only_presses_connect_and_the_answer_reaches_their_own_panel(self):
        await self.start_entrance()
        for width in WIDTHS:
            with self.subTest(width=width):
                context = await self.context_at(width)
                page = await self.sign_in(context)
                box = page.locator('#outlook-box')
                await expect(box).to_be_visible()
                # Gmail's own client and the ChatGPT plan are not offered here; the mail card is.
                await expect(page.locator('#gmail-configure')).to_be_hidden()
                await expect(page.locator('#outlook-pill')).to_have_text('Lista para conectar')
                await expect(page.locator('#outlook-app-form')).to_be_hidden()
                await expect(page.locator('#outlook-help')).to_be_hidden()
                await expect(page.locator('#outlook-imap')).to_be_visible()
                await self.assert_fits(page)

                arrivals = []
                context.on('request', lambda request: arrivals.append(request)
                           if request.is_navigation_request() and request.url.startswith(self.origin) else None)
                back = await self.to_microsoft_and_back(context, box.get_by_role('button', name='Conectar Outlook', exact=True))
                await expect(back.locator('#outlook-feedback')).to_contain_text(f'Outlook conectado como {ADDRESS}.', timeout=TRIP)
                await expect(back).to_have_url(self.origin + '/#ajustes')
                await expect(back.locator('#outlook-pill')).to_have_text('Conectado')
                # Coming from Microsoft's site the cookie did not travel: the entrance sent the browser to the
                # sign-in page, which kept the answer after «#» and went on to the panel with the session.
                self.assertEqual([urlsplit(request.url).path for request in arrivals],
                                 ['/auth/outlook/callback', '/entrar', '/'])
                sent = [await request.all_headers() for request in arrivals]
                self.assertNotIn('cookie', sent[0])
                self.assertNotIn('cookie', sent[1])
                self.assertIn('facturai_sesion=', sent[2]['cookie'])
                for request in arrivals:
                    for mark in MARKS:
                        self.assertNotIn(mark, request.url)  # What follows «#» is never sent to a server.
                self.assertEqual(self.asked[-1]['redirect_uri'], self.origin + '/auth/outlook/callback')
                self.assertTrue(outlook.connected(self.root))
                await self.assert_fits(back)
                await self.assert_clean(page, back)
                await back.close()
                await page.evaluate("window.dispatchEvent(new Event('focus'))")
                await expect(page.locator('#outlook-pill')).to_have_text('Conectado')
                page.once('dialog', lambda prompt: asyncio.ensure_future(prompt.accept()))
                await box.get_by_role('button', name='Desconectar Outlook').click()
                await expect(page.locator('#outlook-pill')).to_have_text('Lista para conectar')
                await context.close()
        self.assertEqual(self.errors, [])

    async def test_without_an_application_of_the_installation_the_option_only_says_so(self):
        await self.start_entrance()
        context = await self.context_at(390)
        page = await self.sign_in(context)
        box = page.locator('#outlook-box')
        await expect(page.locator('#outlook-pill')).to_have_text('No disponible')
        await expect(page.locator('#outlook-summary')).to_have_text(
            'Este servicio todavía no ofrece conectar correos de Outlook y Hotmail.')
        for name in ('Conectar Outlook', 'Guardar identificador', 'Probar Outlook', 'Desconectar Outlook'):
            await expect(box.get_by_role('button', name=name, exact=True)).to_be_hidden()
        await expect(page.locator('#outlook-help')).to_be_hidden()
        await expect(page.locator('#outlook-imap')).to_be_hidden()
        await expect(page.locator('#mail-form')).to_be_visible()
        await self.assert_fits(page)
        self.assertEqual(self.errors, [])


if __name__ == '__main__':
    unittest.main()
