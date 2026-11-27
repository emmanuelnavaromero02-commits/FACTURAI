"""The entrance pages in a real browser the size of a phone: sign in, invitation, account and administration."""
import asyncio
import glob
import os
import re
import socket
import tempfile
import threading
import time
import unittest
from pathlib import Path
from urllib.parse import urlsplit

import uvicorn
from playwright.async_api import async_playwright, expect

from acceso_dobles import PASSWORD, make_accounts, make_entrance, use_master_key


def browser_executable() -> str | None:
    candidates = [os.environ.get('CHROME_EXECUTABLE', ''), 'C:/Program Files/Google/Chrome/Application/chrome.exe',
                  '/usr/bin/google-chrome-stable', '/usr/bin/chromium',
                  *sorted(glob.glob(os.path.expanduser('~/AppData/Local/ms-playwright/chromium-*/chrome-win*/chrome.exe'))),
                  *sorted(glob.glob(os.path.expanduser('~/.cache/ms-playwright/chromium-*/chrome-linux*/chrome')))]
    return next((path for path in candidates if path and os.path.isfile(path)), None)


EXECUTABLE = browser_executable()
PHONE = {'viewport': {'width': 390, 'height': 844}, 'is_mobile': True, 'has_touch': True}


def free_port() -> int:
    with socket.socket() as sock:
        sock.bind(('127.0.0.1', 0))
        return sock.getsockname()[1]


@unittest.skipUnless(EXECUTABLE, 'Requiere Google Chrome o Chromium')
class EntrancePagesInBrowser(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        folder = tempfile.TemporaryDirectory()
        self.addCleanup(folder.cleanup)
        use_master_key(self)
        self.port = free_port()
        # http is admitted only for localhost, to try the entrance on the same machine.
        self.origin = f'http://localhost:{self.port}'
        self.accounts = make_accounts(Path(folder.name))
        self.accounts.create_user('jefa', PASSWORD, role='admin')
        self.gate = make_entrance(Path(folder.name), accounts=self.accounts, origin=self.origin, clock=time.monotonic)
        self.server = uvicorn.Server(uvicorn.Config(self.gate, host='127.0.0.1', port=self.port, log_level='warning'))
        thread = threading.Thread(target=self.server.run, daemon=True)
        thread.start()
        deadline = time.monotonic() + 15
        while not self.server.started and time.monotonic() < deadline:
            await asyncio.sleep(.05)
        self.assertTrue(self.server.started)

        async def stop():
            self.server.should_exit = True
            await asyncio.to_thread(thread.join, 15)
        self.addAsyncCleanup(stop)
        self.playwright = await async_playwright().start()
        self.addAsyncCleanup(self.playwright.stop)
        self.browser = await self.playwright.chromium.launch(executable_path=EXECUTABLE, headless=True)
        self.addAsyncCleanup(self.browser.close)
        self.problems = []

    async def phone(self):
        context = await self.browser.new_context(**PHONE)
        page = await context.new_page()
        page.on('pageerror', lambda error: self.problems.append(str(error)))
        page.on('console', lambda message: self.problems.append(message.text)
                if re.search(r'Content Security Policy|Refused to', message.text) else None)
        return context, page

    async def fits(self, page):
        self.assertTrue(await page.evaluate('document.documentElement.scrollWidth <= window.innerWidth'),
                        'la página se desborda a lo ancho en un teléfono')

    async def panel_ready(self, page):
        await page.wait_for_url(self.origin + '/')
        await expect(page.get_by_role('heading', name='FACTURAI')).to_be_visible()
        await page.wait_for_function('document.querySelector("#chat-send") && !document.querySelector("#chat-camera").disabled')

    async def test_sign_in_invitation_account_and_administration_from_a_phone(self):
        context, page = await self.phone()
        await page.goto(self.origin + '/')
        await page.wait_for_url(self.origin + '/entrar')  # Nothing of the panel without signing in.
        await expect(page.get_by_role('heading', name='Entrar')).to_be_visible()
        await self.fits(page)
        self.assertEqual(await page.evaluate('document.activeElement.name'), 'usuario')

        form = page.locator('#form-entrar')
        await form.get_by_label('Usuario').fill('jefa')
        await form.get_by_label('Contraseña', exact=True).fill('no es la contraseña 1')
        await form.get_by_role('button', name='Entrar').click()
        await expect(page.get_by_role('alert')).to_have_text('Usuario o contraseña incorrectos.')
        self.assertEqual(await context.cookies(), [])
        await form.get_by_label('Mostrar contraseña').check()
        self.assertEqual(await form.get_by_label('Contraseña', exact=True).get_attribute('type'), 'text')
        await form.get_by_label('Contraseña', exact=True).fill(PASSWORD)
        await form.get_by_role('button', name='Entrar').click()
        await self.panel_ready(page)

        cookie, = await context.cookies()
        self.assertEqual((cookie['name'], cookie['httpOnly'], cookie['secure'], cookie['sameSite'], cookie['path']),
                         ('facturai_sesion', True, True, 'Strict', '/'))
        self.assertRegex(cookie['value'], r'^[A-Za-z0-9_-]{43}$')
        self.assertEqual(await page.evaluate('document.cookie'), '')  # Scripts cannot read it.
        self.assertGreater(len(await page.evaluate("fetch('/api/state').then(reply => reply.json()).then(state => state.token)")), 20)

        await page.goto(self.origin + '/cuenta')
        await expect(page.locator('#quien')).to_have_text('Entraste como jefa (administrador).')
        await expect(page.get_by_role('heading', name='Personas con acceso')).to_be_visible()
        await self.fits(page)
        await page.get_by_label('Usuario para esa persona (opcional)').fill('carla')
        await page.get_by_role('button', name='Crear invitación').click()
        await expect(page.get_by_role('alert')).to_have_text('Invitación creada.')
        link = await page.locator('#enlace').input_value()
        self.assertRegex(link, '^' + re.escape(self.origin) + r'/invitacion#[A-Za-z0-9_-]{43}$')
        await expect(page.locator('#invitaciones li')).to_contain_text('Invitación para carla')
        await self.fits(page)

        guest_context, guest = await self.phone()
        await guest.goto(link)
        await expect(guest.get_by_role('heading', name='Crear tu cuenta')).to_be_visible()
        await expect(guest.locator('#texto-invitacion')).to_have_text('Elige tu contraseña para entrar a FACTURAI.')
        self.assertEqual(guest.url, self.origin + '/invitacion')  # The code left the address bar.
        form = guest.locator('#form-invitacion')
        name = form.get_by_label('Usuario')
        self.assertEqual((await name.input_value(), await name.get_attribute('readonly')), ('carla', ''))
        await self.fits(guest)
        await form.get_by_label('Contraseña nueva').fill('frase de carla 123')
        await form.get_by_label('Repite la contraseña').fill('frase distinta 12345')
        await form.get_by_role('button', name='Crear cuenta y entrar').click()
        await expect(guest.get_by_role('alert')).to_have_text('Las dos contraseñas no coinciden.')
        await form.get_by_label('Repite la contraseña').fill('frase de carla 123')
        await form.get_by_role('button', name='Crear cuenta y entrar').click()
        await self.panel_ready(guest)
        await guest.goto(self.origin + '/cuenta')
        await expect(guest.locator('#quien')).to_have_text('Entraste como carla.')
        await expect(guest.locator('#admin')).to_be_hidden()

        # The used link is worth nothing in another phone.
        late_context, late = await self.phone()
        await late.goto(link)
        await expect(late.locator('#texto-invitacion')).to_contain_text('Esta invitación ya no es válida')
        await expect(late.locator('#form-invitacion')).to_be_hidden()
        await late_context.close()

        await page.reload()
        row = page.locator('#usuarios li', has_text='carla')
        await expect(row).to_be_visible()
        page.once('dialog', lambda dialog: asyncio.ensure_future(dialog.accept()))
        await row.get_by_role('button', name='Quitar acceso').click()
        await expect(page.get_by_role('alert')).to_have_text('Acceso retirado.')
        await expect(row).to_contain_text('sin acceso')
        await guest.goto(self.origin + '/')
        await guest.wait_for_url(self.origin + '/entrar')  # Her session ended on the server.
        await guest_context.close()

        new = 'otra frase larga nueva 2'
        await page.get_by_label('Contraseña actual').fill('no es la actual 12345')
        await page.get_by_label('Contraseña nueva', exact=True).fill(new)
        await page.get_by_label('Repite la contraseña nueva').fill(new)
        await page.get_by_role('button', name='Cambiar contraseña').click()
        await expect(page.get_by_role('alert')).to_have_text('La contraseña actual no es correcta.')
        await page.get_by_label('Contraseña actual').fill(PASSWORD)
        await page.get_by_label('Contraseña nueva', exact=True).fill(new)
        await page.get_by_label('Repite la contraseña nueva').fill(new)
        await page.get_by_role('button', name='Cambiar contraseña').click()
        await expect(page.get_by_role('alert')).to_have_text('Contraseña cambiada. Tus otras sesiones se cerraron.')
        renewed, = await context.cookies()
        self.assertNotEqual(renewed['value'], cookie['value'])

        await page.get_by_role('button', name='Cerrar sesión').click()
        await page.wait_for_url(self.origin + '/entrar')
        self.assertEqual(await context.cookies(), [])
        await page.goto(self.origin + '/')
        await page.wait_for_url(self.origin + '/entrar')
        await context.close()
        self.assertEqual(self.problems, [])

    async def test_panel_add_on_leads_to_the_account_and_back_to_sign_in_when_the_session_ends(self):
        context, page = await self.phone()
        await page.goto(self.origin + '/entrar')
        form = page.locator('#form-entrar')
        await form.get_by_label('Usuario').fill('jefa')
        await form.get_by_label('Contraseña', exact=True).fill(PASSWORD)
        await form.get_by_role('button', name='Entrar').click()
        await self.panel_ready(page)
        # What one line in index.html will do: include the add-on that the entrance serves.
        await page.add_script_tag(type='module', url=self.origin + '/acceso/panel.js')
        await page.evaluate("location.hash = 'ajustes'")
        link = page.get_by_role('link', name='Mi cuenta: contraseña y cerrar sesión')
        await expect(link).to_be_visible()
        await self.fits(page)
        await link.click()
        await page.wait_for_url(self.origin + '/cuenta')
        await page.get_by_role('link', name='Volver a mis tickets').click()
        await self.panel_ready(page)
        await page.add_script_tag(type='module', url=self.origin + '/acceso/panel.js')
        cookie, = await context.cookies()
        self.accounts.close_session(cookie['value'])  # The session ends on the server while the panel is open.
        await page.wait_for_url(self.origin + '/entrar')  # The panel's next request brings her back here.
        await expect(page.get_by_role('heading', name='Entrar')).to_be_visible()
        await context.close()
        self.assertEqual(self.problems, [])

    async def test_link_opened_from_another_app_keeps_the_session_and_the_screen(self):
        context, page = await self.phone()
        await page.goto(self.origin + '/#comprobantes')
        await page.wait_for_url(self.origin + '/entrar#comprobantes')
        form = page.locator('#form-entrar')
        await form.get_by_label('Usuario').fill('jefa')
        await form.get_by_label('Contraseña', exact=True).fill(PASSWORD)
        await form.get_by_role('button', name='Entrar').click()
        await page.wait_for_url(self.origin + '/#comprobantes')  # She lands on the screen she asked for.
        await expect(page.get_by_role('heading', name='Comprobantes', exact=True)).to_be_visible()

        # A link to the panel inside a page of another site, as in a notice opened from a chat or the mail.
        await page.route('https://avisos.example.com/**', lambda route: route.fulfill(
            content_type='text/html', body=f'<a id="liga" href="{self.origin}/#ajustes">Abrir FACTURAI</a>'))
        await page.goto('https://avisos.example.com/aviso')
        arrivals = []
        page.on('request', lambda request: arrivals.append(request)
                if request.is_navigation_request() and request.url.startswith(self.origin) else None)
        await page.click('#liga')
        await page.wait_for_url(self.origin + '/#ajustes')
        await expect(page.get_by_role('heading', name='Ajustes', exact=True)).to_be_visible()
        # The cookie did not travel with the click from the other site, and nobody had to type again.
        self.assertEqual([urlsplit(request.url).path for request in arrivals], ['/', '/entrar', '/'])
        sent = [await request.all_headers() for request in arrivals]
        self.assertNotIn('cookie', sent[0])
        self.assertNotIn('cookie', sent[1])
        self.assertIn('facturai_sesion=', sent[2]['cookie'])
        await context.close()
        self.assertEqual(self.problems, [])


if __name__ == '__main__':
    unittest.main()
