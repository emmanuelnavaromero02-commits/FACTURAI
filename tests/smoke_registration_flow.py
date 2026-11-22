"""One synthetic merchant and mailbox, an offline official Chrome, and a real persistent vault."""
import asyncio
import tempfile
import time
from pathlib import Path
from types import SimpleNamespace
from urllib.parse import parse_qs, urlsplit
from unittest.mock import patch

from playwright.async_api import async_playwright

from facturador.account_flow import PortalAccountFlow
from facturador.portal_accounts import credential_file, site_password
from test_registration_flow import EMAIL, ORIGIN, enrollment_message


async def main():
    posted = []
    login = ('<form action="/session" method="post"><label>Correo<input name="email" type="email" required></label>'
             '<label>Contraseña<input name="pass" type="password" required></label><button>Iniciar sesión</button></form>'
             '<a href="/registro">Crear cuenta</a>')
    signup = ('<form method="post" action="/alta"><label>Correo<input name="email" type="email" required></label>'
              '<label>Contraseña<input type="password" name="pass" required></label>'
              '<label>WhatsApp (+52), 10 dígitos<input name="phone" type="tel" maxlength="10" required></label>'
              '<label>Teléfono opcional<input name="otro" type="tel"></label>'
              '<button>Crear cuenta</button></form>')
    activate = ('<h1>Activa tu cuenta</h1><form action="/confirmado" method="post"><label>Código de activación'
                '<input name="codigo" required></label><button>Verificar correo</button></form>')
    pages = {'/factura': login, '/registro': signup, '/alta': activate, '/confirmado': '<h1>Datos del receptor</h1>',
             '/session': '<h1>Datos del receptor</h1>'}
    pages['/cambio'] = ('<form action="/cambiada" method="post"><label>Contraseña temporal<input type="password" '
                        'autocomplete="current-password" name="anterior" required></label><label>Contraseña nueva'
                        '<input type="password" name="nueva" autocomplete="new-password" required></label>'
                        '<label>Confirmar contraseña<input type="password" autocomplete="new-password" required></label>'
                        '<button>Cambiar contraseña</button></form>')
    pages['/cambiada'] = '<h1>Contraseña cambiada correctamente</h1>'

    async def portal(route):
        path = urlsplit(route.request.url).path
        if route.request.method == 'POST':
            posted.append((path, parse_qs(route.request.post_data or '')))
        await route.fulfill(body=pages.get(path, ''), content_type='text/html; charset=utf-8')

    with tempfile.TemporaryDirectory() as folder:
        root = Path(folder)
        person = SimpleNamespace(email=EMAIL, whatsapp_phone='+526641234567')
        flow = PortalAccountFlow(root, ORIGIN+'/factura', person, 'solo-este-ticket')
        async with async_playwright() as pw:
            chrome = await pw.chromium.launch(executable_path='/usr/bin/google-chrome-stable', headless=False, chromium_sandbox=True)
            page = await chrome.new_page()
            await page.route('**/*', portal)
            await page.goto(ORIGIN+'/factura')
            async def step():
                return await flow.step(page, await page.locator('body').aria_snapshot(), None)

            assert (await step())[0] == 'advanced'
            assert (await step())[0] == 'advanced'
            assert posted[0][0] == '/alta' and posted[0][1]['phone'] == ['6641234567']
            assert 'otro' not in posted[0][1]
            key = credential_file(root, ORIGIN, EMAIL)
            password = posted[0][1]['pass'][0]
            assert password == site_password(key, False) and password not in key.read_text()
            paused = await step()
            assert paused[0] == 'unknown' and 'Gmail' in paused[1]
            assert page.url == ORIGIN+'/alta'
            (root/'gmail_token.json').write_text('{}')
            msg = enrollment_message()
            result = dict(sender=str(msg['From']), id='mensaje', received_ms=int(time.time()*1000))
            with patch('facturador.account_flow.search_tickets', return_value=[result]), \
                    patch('facturador.account_flow.read_selected_message', return_value=msg):
                advanced = await step()
            assert advanced[0] == 'advanced' and '815270' not in advanced[1]
            assert posted[-1] == ('/confirmado', {'codigo':['815270']})
            assert await step() is None
            await page.goto(ORIGIN+'/registro')
            assert (await step())[0] == 'unknown' and len(posted) == 2
            await page.goto(ORIGIN+'/cambio')
            temporary = enrollment_message(body='Contraseña temporal: DEMO!TEMP7')
            with patch('facturador.account_flow.search_tickets', return_value=[result]), \
                    patch('facturador.account_flow.read_selected_message', return_value=temporary):
                assert (await step())[0] == 'advanced'
            assert posted[-1][0] == '/cambiada' and posted[-1][1]['anterior'] == ['DEMO!TEMP7']
            new_password = posted[-1][1]['nueva'][0]
            assert new_password != password and site_password(key, False) == password
            assert (await step())[0] == 'advanced' and site_password(key, False) == new_password
            flow = PortalAccountFlow(root, ORIGIN+'/factura', person, 'otro-ticket')
            await page.goto(ORIGIN+'/factura')
            assert (await step())[0] == 'advanced'
            assert posted[-1][0] == '/session' and posted[-1][1]['pass'] == [new_password]
            pages['/alta'] = activate.replace('Activa tu cuenta', 'Activa tu cuenta. Segundo factor de inicio de sesión')
            await page.goto(ORIGIN+'/alta')
            assert (await step())[0] == 'login' and len(posted) == 4
            await page.set_content('<h1>Verificación en dos pasos</h1><p>Código de acceso: 123456</p>')
            protected = await step()
            assert protected[0] == 'login' and '123456' not in protected[1]
            pages['/registro'] = ('<form action="/alta" method="post"><label>Correo<input name="email" type="email" required>'
                                  '</label><button>Crear cuenta</button></form>')
            pages['/alta'] = login
            second = SimpleNamespace(email='segunda@example.org', whatsapp_phone='')
            flow = PortalAccountFlow(root, ORIGIN, second, 'registro-sin-password')
            await page.goto(ORIGIN+'/registro')
            assert (await step())[0] == 'advanced'
            assert posted[-1] == ('/alta', {'email': [second.email]})
            temporary.replace_header('To', second.email)
            result['received_ms'] = int(time.time()*1000)
            with patch('facturador.account_flow.search_tickets', return_value=[result]), \
                    patch('facturador.account_flow.read_selected_message', return_value=temporary):
                assert (await step())[0] == 'advanced'
            assert posted[-1] == ('/session', {'email': [second.email], 'pass':['DEMO!TEMP7']})
            await chrome.close()
            print('OK: alta única, WhatsApp del titular, pausa sin Gmail, activación de registro verificada y acceso posterior con contraseña privada.')


if __name__ == '__main__':
    asyncio.run(main())
