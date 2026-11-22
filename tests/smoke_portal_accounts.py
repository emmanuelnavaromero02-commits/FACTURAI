"""Ejecutar dentro de la imagen con xvfb-run; un portal sintético verifica el flujo sin red."""
import asyncio
from pathlib import Path
import tempfile

from playwright.async_api import async_playwright
from facturador.portal_accounts import prepare_form, credential_file, site_password


async def main():
    origin = 'https://mercado.example.com'
    profile = {'email': 'prueba@example.com', 'rfc': 'XAXX010101000', 'postal_code': '22000', 'name': 'PRUEBA',
               'regime': '616', 'cfdi_use': 'S01'}
    form = '''<form method="post" action="/registro" onsubmit="event.preventDefault();window.sent++">
            <label>Correo<input type="email" required></label><label>Contraseña<input type="password" required></label>
            {extra}<button type="submit">{button}</button></form><script>window.sent=0</script>'''
    with tempfile.TemporaryDirectory() as directory:
        path = credential_file(Path(directory), origin, profile['email'])
        password = site_password(path, True)
        async with async_playwright() as pw:
            browser = await pw.chromium.launch(executable_path='/usr/bin/google-chrome-stable',
                                               headless=False, chromium_sandbox=True)
            page = await browser.new_page()
            html = form.format(extra='', button='Crear cuenta')
            await page.route('**/*', lambda route: route.fulfill(body=html, content_type='text/html; charset=utf-8'))
            await page.goto(origin)
            for _ in range(2):
                result = await prepare_form(page, origin, 'create', profile, password, path)
                assert password not in str(result)
            assert await page.evaluate('window.sent') == 1
            assert await page.locator('input[type=password]').input_value() == site_password(path, False)
            for number, (extra, button) in enumerate([('', 'Comprar'), ('<label><input type="checkbox">Acepto los términos</label>', 'Registrarme'),
                                  ('<div data-sitekey="prueba">Verificación</div>', 'Registrarme')]):
                html = form.format(extra=extra, button=button)
                await page.reload()
                other_path = credential_file(Path(directory), origin, f'test{number}@example.com')
                result = await prepare_form(page, origin, 'create', profile, site_password(other_path, True), other_path)
                assert not result['submitted']
                assert await page.evaluate('window.sent') == 0
            html = form.format(extra='', button='Crear cuenta').replace('action="/registro"', 'action="https://otro.example.com/registro"')
            await page.reload()
            try:
                await prepare_form(page, origin, 'register', profile, password, path)
            except ValueError:
                pass
            assert not await page.locator('input[type=password]').input_value()
            html = '''<label>Contraseña temporal<input type="password" autocomplete="current-password"></label>
            <label>Contraseña nueva<input type="password" autocomplete="new-password"></label>
            <label>Confirmar contraseña<input type="password" autocomplete="new-password"></label>'''
            await page.reload()
            await prepare_form(page, origin, 'temporary', {}, 'SINTETICA', None)
            await prepare_form(page, origin, 'change', profile, password, path)
            assert await page.locator('[autocomplete=current-password]').input_value() == 'SINTETICA'
            assert await page.locator('[autocomplete=new-password]').first.input_value() == password
            html = '''<label>RFC receptor<input></label><label>RFC emisor<input></label><label>Código postal<input></label>
                      <label>Uso CFDI<select><option value="">Elige</option><option value="22">S01 - Sin efectos</option></select></label>'''
            await page.reload()
            result = await prepare_form(page, origin, 'fiscal', profile, '', None)
            assert set(result['filled']) == {'RFC', 'código postal', 'uso del CFDI'}, result
            assert await page.get_by_label('RFC receptor').input_value() == profile['rfc']
            assert not await page.get_by_label('RFC emisor').input_value()
            await browser.close()
            print('OK: alta única, campos fiscales, cambio de temporal y controles del destino')


if __name__ == '__main__':
    asyncio.run(main())
