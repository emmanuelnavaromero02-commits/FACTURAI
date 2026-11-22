"""Ejecutar contra una instancia de prueba recién creada, sin credenciales."""

import asyncio
import io
import os

from PIL import Image, ImageChops
from playwright.async_api import async_playwright, expect

from facturador.local_reader import ocr_languages


async def smoke():
    assert set(ocr_languages().split('+')) == {'spa', 'eng'}, 'Faltan los idiomas de OCR de la imagen Docker'
    async with async_playwright() as playwright:
        # Separate browser for the operator. The server launches its own persistent Chromium.
        operator = await playwright.chromium.launch(
            executable_path=os.getenv('FACTURADOR_CHROMIUM_PATH'),
            chromium_sandbox=True,
        )
        try:
            page = await operator.new_page()
            page.set_default_timeout(30_000)
            url = 'http://127.0.0.1:8765'
            await page.goto(url)
            await page.get_by_role('button', name='Cargar ticket de ejemplo').click()
            await page.get_by_role('button', name='Revisar', exact=True).click()
            await expect(page.get_by_role('dialog').get_by_role('link', name='Ver original ↗')).to_be_visible()
            await page.get_by_role('button', name='Leer texto, QR y barras en el contenedor').click()
            await expect(page.locator('#reader-text')).to_contain_text('CAPUFE', timeout=25_000)
            await expect(page.locator('#reader-text')).to_contain_text('50.00')
            await page.get_by_role('button', name='Cerrar lectura').click()
            await page.keyboard.press('Escape')
            await page.get_by_role('link', name='Navegador', exact=True).click()
            await page.get_by_role('button', name='Abrir navegador', exact=True).click()
            await expect(page.get_by_alt_text('Página en vivo del navegador')).to_be_visible()
            await expect(page.locator('#browser-address')).to_have_text('Portales de facturación')
            await page.get_by_role('button', name='Tomar control').click()
            await expect(page.get_by_role('button', name='Devolver control')).to_be_visible()

            state = await (await page.request.get(url + '/api/state')).json()
            revision = state['browser']['revision']
            headers = {'x-app-token': state['token']}

            async def frame():
                result = await page.request.get(url + f'/api/browser/frame?revision={revision}', headers=headers)
                if result.status == 204:
                    return None
                assert result.status == 200
                image = Image.open(io.BytesIO(await result.body()))
                assert image.format == 'JPEG' and image.size == (1280, 850)
                return image.convert('RGB')

            async def read_frame():
                for _ in range(60):
                    image = await frame()
                    if image is not None:
                        return image
                    await asyncio.sleep(0.15)
                raise AssertionError('El navegador interno no produjo un fotograma.')

            before = await read_frame()
            # The search field in Chromium has autofocus. Filtering removes the portal tiles below it.
            async with page.expect_response(lambda r: '/api/browser/input' in r.url and r.status == 200):
                await page.locator('#remote-text').fill('CAPUFE')
                await page.locator('#paste-form button').click()
            region = (140, 265, 1130, 725)
            for _ in range(25):
                after = await read_frame()
                delta = ImageChops.difference(before.crop(region), after.crop(region)).convert('L')
                if sum(delta.histogram()[15:]) > 1800:
                    break
                await asyncio.sleep(0.1)
            else:
                raise AssertionError('El teclado del panel no cambió el contenido de Chromium.')
            await page.get_by_role('button', name='Leer página', exact=True).click()
            await page.get_by_role('searchbox', name='Buscar en la página').fill('CAPUFE')
            await page.get_by_role('button', name='Buscar siguiente').click()
            assert await page.locator('#page-content').evaluate('(e) => e.value.slice(e.selectionStart, e.selectionEnd).toUpperCase()') == 'CAPUFE'
            await page.get_by_role('button', name='Cerrar texto').click()
            async with page.expect_download() as saved_png:
                await page.get_by_role('button', name='Guardar captura PNG').click()
            capture = await saved_png.value
            with Image.open(await capture.path()) as png:
                assert png.format == 'PNG' and png.size == (1280, 850)
            await page.get_by_role('button', name='Devolver control').click()
            await expect(page.get_by_role('button', name='Tomar control')).to_be_visible()
            await page.get_by_role('link', name='Ajustes', exact=True).click()
            await page.get_by_role('button', name='Comprobar herramientas').click()
            await expect(page.locator('#tools-status')).to_have_text('Comprobación terminada.', timeout=30_000)
            for name in ('OCR local', 'QR y códigos de barras', 'Chromium y Playwright', 'Expedientes y almacenamiento', 'PDF y ZIP', 'Datos del CFDI 4.0'):
                await expect(page.locator('#tools-report')).to_contain_text(name + ' · Comprobado')
            print('OK: OCR, teclado, página, captura PNG y diagnóstico del contenedor desde Ajustes.')
        finally:
            await operator.close()


if __name__ == '__main__':
    asyncio.run(smoke())
