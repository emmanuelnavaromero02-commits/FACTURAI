"""The Gmail card of the panel in a real browser, at phone (390 px) and desktop (1280 px) widths, in both modes.

The real application runs with fictitious data. Google's sign-in page is an intercepted route that
answers like the stand-in of the other Gmail tests: nothing leaves the machine.
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

from acceso_dobles import PASSWORD, clean_env, make_accounts, make_entrance, quiet_app, use_master_key
from facturador import gmail, secretos
from google_dobles import DESKTOP_CLIENT, MARKS, SCOPE, SEND_SCOPE, WEB_CLIENT, FakeGoogle, visit
from test_acceso_paginas import EXECUTABLE, free_port

WIDTHS = (390, 1280)
# A whole trip (Google, the panel again, the token and the mailbox) on a machine that runs other tests too.
TRIP = 20_000
GOOGLE = 'https://accounts.google.com'
RETURN = '/auth/gmail/callback'
IMPORT_HELP = ('Importa el JSON de un cliente OAuth de escritorio de Google. Al conectar, Google te pedirá en otra pestaña '
               'que autorices a FACTURAI a leer el buzón. Mantén abierto el acceso a FACTURAI hasta regresar.')
SHARED_HELP = ('Pulsa Conectar Gmail. Google te pedirá en otra pestaña que autorices a FACTURAI a leer el buzón. '
               'Mantén abierto FACTURAI hasta regresar.')
LINKED_HERE = 'Gmail vinculado. Puedes cerrar esta pestaña y volver a la pestaña original de FACTURAI.'
LINKED_THERE = 'Gmail vinculado. FACTURAI puede leer los correos de verificación y las facturas.'
CANCELLED = 'La autorización se canceló. Puedes volver a conectar Gmail cuando estés listo.'
EXPIRED = 'El enlace de Google caducó. Pulsa Conectar Gmail para obtener uno nuevo.'


class GmailBrowserCase(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        folder = tempfile.TemporaryDirectory(ignore_cleanup_errors=True)
        self.addCleanup(folder.cleanup)
        self.folder = Path(folder.name)
        self.apart = self.folder / 'fuera-de-los-datos'
        self.apart.mkdir()
        # No setting of this machine takes part: each test says which ones it runs with.
        self.enterContext(clean_env())
        gmail._READ.clear()
        gmail._WARNED.clear()
        self.google = FakeGoogle()
        self.addCleanup(self.google.close)
        # The address of Google's page stays the real one: the browser never reaches it, the route answers.
        for name in ('token', 'profile'):
            self.enterContext(patch.object(gmail, name.upper(), getattr(self.google, name)))
        self.playwright = await async_playwright().start()
        self.addAsyncCleanup(self.playwright.stop)
        self.browser = await self.playwright.chromium.launch(executable_path=EXECUTABLE, headless=True)
        self.addAsyncCleanup(self.browser.close)
        self.asked, self.errors = [], []
        self.declines = False

    def use_client(self, kind, *redirects):
        """The installation gets its own Google client: the JSON is kept away from the data folder."""
        path = self.apart / 'cliente-google.json'
        path.write_bytes(self.google.register(WEB_CLIENT if kind == 'web' else DESKTOP_CLIENT, kind, *redirects))
        self.enterContext(clean_env(FACTURADOR_GMAIL_CLIENTE=str(path)))

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

    async def google_page(self, route):
        """Google's sign-in: a page of another site that sends the browser back to the registered address."""
        asked = urlsplit(route.request.url)
        self.asked.append({key: values[0] for key, values in parse_qs(asked.query).items()} | {'path': asked.path})
        self.google.declines = self.declines
        status, location = await asyncio.to_thread(visit, self.google.url + asked.path + '?' + asked.query)
        if status != 302:
            return await route.fulfill(status=400, content_type='text/html; charset=utf-8',
                                       body='<p>Google no reconoce esa dirección de regreso.</p>')
        await route.fulfill(content_type='text/html; charset=utf-8', body=(
            '<!doctype html><title>Acceder con Google</title><p>Volviendo a FACTURAI…</p>'
            f'<script>location.replace({json.dumps(location)})</script>'))

    async def context_at(self, width):
        context = await self.browser.new_context(viewport={'width': width, 'height': 900}, service_workers='block')
        self.addAsyncCleanup(context.close)
        await context.route(GOOGLE + '/**', self.google_page)
        context.on('page', lambda page: page.on('pageerror', lambda error: self.errors.append(str(error))))
        return context

    async def assert_fits(self, page):
        overflow = await page.evaluate('document.documentElement.scrollWidth - window.innerWidth')
        self.assertLessEqual(overflow, 0)

    async def assert_clean(self, *pages):
        """Nothing secret is left in the address, the page or its fields."""
        for page in pages:
            shown = page.url + await page.content() + json.dumps(await page.evaluate(
                '[...document.querySelectorAll("input")].map(input => input.value)'))
            for mark in MARKS:
                self.assertNotIn(mark, shown)

    async def to_google_and_back(self, context, button):
        """Press a button that opens Google's page in another tab; that tab comes back to the panel."""
        async with context.expect_page() as opened:
            await button.click()
        return await opened.value


@unittest.skipUnless(EXECUTABLE, 'Requiere Google Chrome o Chromium')
class GmailCardInBrowser(GmailBrowserCase):
    """One person on their own computer."""

    async def start_app(self):
        from facturador import app as panel
        folder = tempfile.TemporaryDirectory(ignore_cleanup_errors=True, dir=self.folder)
        self.addCleanup(folder.cleanup)
        root, port = Path(folder.name), free_port()
        app = panel.create_app(root, port=port)
        for worker in (app.state.automation, app.state.whatsapp.watcher):
            quiet = patch.object(worker, 'start', lambda: None)
            quiet.start()
            self.addCleanup(quiet.stop)
        await self.serve(app, port)
        return root, f'http://127.0.0.1:{port}'

    async def test_with_the_client_of_the_installation_the_person_only_presses_connect(self):
        self.use_client('installed')
        for width in WIDTHS:
            with self.subTest(width=width):
                root, origin = await self.start_app()
                context = await self.context_at(width)
                page = await context.new_page()
                await page.goto(origin + '/#ajustes')
                connect = page.locator('#gmail-connect')
                await expect(page.locator('#gmail-status')).to_have_text('Listo para vincular')
                await expect(connect).to_be_enabled()
                await expect(connect).to_have_text('Conectar Gmail')
                # There is nothing to import: that step is gone from the card.
                await expect(page.locator('#gmail-configure')).to_be_hidden()
                await expect(page.locator('#gmail-help')).to_have_text(SHARED_HELP)
                card = page.locator('section.settings-card', has=page.locator('#gmail-status'))
                await expect(card.get_by_role('link', name='Abrir Gmail en mi Chrome')).to_be_visible()
                await self.assert_fits(page)

                # The person says no at Google: the tab that comes back says so and nothing is connected.
                self.declines = True
                back = await self.to_google_and_back(context, connect)
                await expect(back.locator('#notice')).to_have_text(CANCELLED, timeout=TRIP)
                await expect(back).to_have_url(origin + '/#ajustes')
                await expect(page.locator('#notice')).to_contain_text('La autorización de Gmail no se completó.', timeout=TRIP)
                await back.close()
                self.declines = False
                self.assertFalse((root / 'gmail_token.json').exists())

                back = await self.to_google_and_back(context, connect)
                await expect(back.locator('#notice')).to_have_text(LINKED_HERE, timeout=TRIP)
                await expect(back).to_have_url(origin + '/#ajustes')
                await expect(back.locator('#gmail-status')).to_have_text('Conectado')
                asked = self.asked[-1]
                self.assertEqual((asked['path'], asked['client_id'], asked['redirect_uri'], asked['scope']),
                                 ('/o/oauth2/v2/auth', DESKTOP_CLIENT, origin + RETURN, SCOPE))
                await self.assert_fits(back)
                await self.assert_clean(page, back)
                await back.close()

                # The first tab learns it by itself.
                await expect(page.locator('#notice')).to_have_text(LINKED_THERE, timeout=TRIP)
                await expect(page.locator('#gmail-status')).to_have_text('Conectado')
                await expect(connect).to_have_text('Vincular otra cuenta')
                await expect(page.locator('#gmail-configure')).to_be_hidden()
                await expect(page.locator('#gmail-disconnect')).to_be_visible()
                self.assertEqual(gmail.GmailConnection(root).status(), 'connected')
                self.assertFalse((root / 'google_client.json').exists())

                # Sending is asked apart.
                await page.reload()
                send = page.locator('#gmail-send')
                await expect(send).to_be_visible()
                await expect(page.locator('#gmail-send-status')).to_contain_text('Gmail solo lee tu buzón.')
                back = await self.to_google_and_back(context, send)
                await expect(back.locator('#notice')).to_have_text(LINKED_HERE, timeout=TRIP)
                self.assertEqual(self.asked[-1]['scope'].split(), [SCOPE, SEND_SCOPE])
                await expect(back.locator('#gmail-send-status')).to_have_text('Gmail también puede enviar tus solicitudes de factura.')
                await expect(back.locator('#gmail-send')).to_be_hidden()
                await back.close()
                self.assertTrue(gmail.can_send(root))

                await page.locator('#gmail-disconnect').click()
                await expect(page.locator('#notice')).to_have_text('Gmail desvinculado de este equipo.')
                await expect(page.locator('#gmail-status')).to_have_text('Listo para vincular')
                await expect(connect).to_have_text('Conectar Gmail')
                await expect(connect).to_be_enabled()
                await self.assert_clean(page)
                await self.assert_fits(page)
                await context.close()
        self.assertEqual(self.errors, [])

    async def test_without_the_setting_the_card_asks_to_import_a_client_as_before(self):
        root, origin = await self.start_app()
        for width in WIDTHS:
            with self.subTest(width=width):
                context = await self.context_at(width)
                page = await context.new_page()
                await page.goto(origin + '/#ajustes')
                await expect(page.locator('#gmail-status')).to_have_text('Por configurar')
                await expect(page.locator('#gmail-configure')).to_be_visible()
                await expect(page.locator('#gmail-configure')).to_have_text('Importar cliente OAuth')
                await expect(page.locator('#gmail-help')).to_have_text(IMPORT_HELP)
                await expect(page.locator('#gmail-connect')).to_be_disabled()
                await self.assert_fits(page)
                await context.close()
        self.assertEqual((self.asked, self.errors), ([], []))
        self.assertFalse((root / 'gmail_token.json').exists())

    async def test_client_of_the_installation_that_cannot_be_read_is_said_in_the_card(self):
        self.enterContext(clean_env(FACTURADOR_GMAIL_CLIENTE=str(self.apart / 'falta.json')))
        _, origin = await self.start_app()
        context = await self.context_at(390)
        page = await context.new_page()
        await page.goto(origin + '/#ajustes')
        await expect(page.locator('#gmail-status')).to_have_text('No disponible')
        await expect(page.locator('#gmail-help')).to_have_text(gmail.SHARED_BROKEN + gmail.UNREADABLE)
        await expect(page.locator('#gmail-configure')).to_be_hidden()
        await expect(page.locator('#gmail-connect')).to_be_disabled()
        await self.assert_fits(page)
        # The shortcut of the browser screen says the same instead of asking for a client to import.
        await page.goto(origin + '/#navegador')
        await page.locator('#browser-google').click()
        await expect(page).to_have_url(origin + '/#ajustes')
        await expect(page.locator('#notice')).to_have_text(gmail.SHARED_BROKEN + gmail.UNREADABLE)
        self.assertEqual((self.asked, self.errors), ([], []))


@unittest.skipUnless(EXECUTABLE, 'Requiere Google Chrome o Chromium')
class GmailBehindTheEntrance(GmailBrowserCase):
    """Several customers: the session cookie (SameSite=Strict) does not travel when the browser comes back from Google."""

    async def start_entrance(self, client=True):
        use_master_key(self)
        port = free_port()
        self.origin = f'http://localhost:{port}'
        if client:
            self.use_client('web', self.origin + RETURN)
        data = self.folder / 'datos'
        accounts = make_accounts(data)
        self.account = accounts.create_user('ana', PASSWORD)
        self.root = data / 'usuarios' / self.account['id']
        # The panel includes the add-on that the entrance serves, as the product does behind it.
        gate = make_entrance(data, accounts=accounts, origin=self.origin, clock=time.monotonic, factory=quiet_app)
        await self.serve(gate, port)

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

    async def test_person_only_presses_connect_and_the_answer_reaches_their_own_panel(self):
        await self.start_entrance()
        for width in WIDTHS:
            with self.subTest(width=width):
                context = await self.context_at(width)
                page = await self.sign_in(context)
                connect = page.locator('#gmail-connect')
                # The card is offered here only because the installation has its own Google client.
                await expect(connect).to_be_visible()
                await expect(connect).to_be_enabled()
                await expect(page.locator('#gmail-status')).to_have_text('Listo para vincular')
                await expect(page.locator('#gmail-configure')).to_be_hidden()
                await expect(page.locator('#gmail-help')).to_have_text(SHARED_HELP)
                await expect(page.locator('#pod-connect')).to_be_hidden()  # The ChatGPT plan is still not offered.
                await self.assert_fits(page)

                self.declines = True
                back = await self.to_google_and_back(context, connect)
                await expect(back.locator('#notice')).to_have_text(CANCELLED, timeout=TRIP)
                await expect(back).to_have_url(self.origin + '/#ajustes')
                await expect(page.locator('#notice')).to_contain_text('La autorización de Gmail no se completó.', timeout=TRIP)
                await back.close()
                self.declines = False
                self.assertFalse((self.root / 'gmail_token.json').exists())

                arrivals = []
                context.on('request', lambda request: arrivals.append(request)
                           if request.is_navigation_request() and request.url.startswith(self.origin) else None)
                back = await self.to_google_and_back(context, connect)
                await expect(back.locator('#notice')).to_have_text(LINKED_HERE, timeout=TRIP)
                await expect(back).to_have_url(self.origin + '/#ajustes')
                await expect(back.locator('#gmail-status')).to_have_text('Conectado')
                # Coming from Google's site the cookie did not travel. The page of the entrance moved the
                # answer after «#» and went on to the panel inside this site, where the session travels again.
                self.assertEqual([urlsplit(request.url).path for request in arrivals], [RETURN, '/'])
                sent = [await request.all_headers() for request in arrivals]
                self.assertNotIn('cookie', sent[0])
                self.assertIn('facturai_sesion=', sent[1]['cookie'])
                # Google documents its answer in the address of that first visit; after it, nothing of the
                # answer is sent in an address again.
                self.assertIn('CODIGO-FICTICIO', arrivals[0].url)
                for mark in MARKS:
                    self.assertNotIn(mark, arrivals[1].url)
                asked = self.asked[-1]
                self.assertEqual((asked['client_id'], asked['redirect_uri'], asked['scope']),
                                 (WEB_CLIENT, self.origin + RETURN, SCOPE))
                self.assertNotIn('response_mode', asked)
                record = secretos.read_json(self.root / 'gmail_token.json', purpose=secretos.GMAIL_TOKEN)
                self.assertEqual(sorted(record), ['client_id', 'expires_at', 'refresh_token', 'scope', 'token'])
                self.assertEqual(record['client_id'], WEB_CLIENT)
                await self.assert_fits(back)
                await self.assert_clean(page, back)
                await back.close()

                await expect(page.locator('#notice')).to_have_text(LINKED_THERE, timeout=TRIP)
                await expect(page.locator('#gmail-status')).to_have_text('Conectado')
                await expect(connect).to_have_text('Vincular otra cuenta')

                # An answer nobody asked for, in the address or after «#», connects nothing and leaves no trace.
                for mark in ('?', '#'):
                    stray = await context.new_page()
                    await stray.goto(f'{self.origin}{RETURN}{mark}state=gmail-{"x" * 43}&code=4/0inventado&scope=algo')
                    await expect(stray.locator('#notice')).to_have_text(EXPIRED, timeout=TRIP)
                    await expect(stray).to_have_url(self.origin + '/#ajustes')
                    await stray.close()
                # What is not an answer of Google at all only opens the settings.
                stray = await context.new_page()
                await stray.goto(f'{self.origin}{RETURN}?state=otra-cosa&code=4/0inventado&next=https://ajeno.example.com')
                await expect(stray).to_have_url(self.origin + '/#ajustes')
                await expect(stray.get_by_role('heading', name='Ajustes', exact=True)).to_be_visible()
                await expect(stray.locator('#notice')).to_be_hidden()
                await stray.close()
                await expect(page.locator('#gmail-status')).to_have_text('Conectado')

                await page.locator('#gmail-disconnect').click()
                await expect(page.locator('#gmail-status')).to_have_text('Listo para vincular')
                self.assertFalse((self.root / 'gmail_token.json').exists())
                await context.close()
        self.assertFalse(any((self.folder / 'datos').rglob('google_client.json')))
        self.assertEqual(self.errors, [])

    async def test_answer_waits_for_the_person_to_sign_in_again(self):
        await self.start_entrance()
        context = await self.context_at(390)
        page = await self.sign_in(context)
        await expect(page.locator('#gmail-status')).to_have_text('Listo para vincular')
        # The person asks to connect and, while they are at Google, this browser loses the session.
        address = await page.evaluate("""async () => {
          const {token} = await (await fetch('/api/state')).json();
          const reply = await fetch('/api/gmail/connect', {method: 'POST', headers: {'x-app-token': token}});
          return (await reply.json()).url;
        }""")
        location = (await asyncio.to_thread(visit, self.google.url + '/o/oauth2/v2/auth?' + urlsplit(address).query))[1]
        self.assertTrue(location.startswith(self.origin + RETURN + '?'))
        await context.clear_cookies()
        back = await context.new_page()
        await back.goto(location)
        # The answer stays after «#» while the sign-in page is shown; nothing was handed over yet.
        form = back.locator('#form-entrar')
        await expect(form).to_be_visible()
        self.assertEqual(urlsplit(back.url).path, '/entrar')
        self.assertEqual(self.google.token_requests, [])
        self.assertFalse((self.root / 'gmail_token.json').exists())
        await form.get_by_label('Usuario').fill('ana')
        await form.get_by_label('Contraseña', exact=True).fill(PASSWORD)
        await form.get_by_role('button', name='Entrar').click()
        await expect(back.locator('#notice')).to_have_text(LINKED_HERE, timeout=TRIP)
        await expect(back).to_have_url(self.origin + '/#ajustes')
        await expect(back.locator('#gmail-status')).to_have_text('Conectado')
        await self.assert_clean(back)
        self.assertTrue((self.root / 'gmail_token.json').exists())
        self.assertEqual(self.errors, [])

    async def test_without_a_client_of_the_installation_the_card_stays_out_of_sight(self):
        await self.start_entrance(client=False)
        context = await self.context_at(390)
        page = await self.sign_in(context)
        await expect(page.locator('#mail-card')).to_be_visible()
        for control in ('#gmail-connect', '#gmail-configure', '#gmail-status', '#gmail-help', '#pod-connect'):
            await expect(page.locator(control)).to_be_hidden()
        # The address Google would return to answers as it always did here.
        reply = await page.request.get(self.origin + RETURN + '?state=gmail-x&code=y', max_redirects=0)
        self.assertEqual((reply.status, reply.headers['location']), (303, '/?conexion=gmail-error&motivo=no-disponible#ajustes'))
        await self.assert_fits(page)
        self.assertEqual((self.asked, self.errors), ([], []))


if __name__ == '__main__':
    unittest.main()
