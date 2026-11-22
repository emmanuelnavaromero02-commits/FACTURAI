"""Ejercita el selector de archivos y las descargas desde el panel, sin cuentas reales."""

import asyncio
import os
import socket
import tempfile
from pathlib import Path
from unittest.mock import patch
from urllib.parse import urlsplit

import uvicorn
from playwright.async_api import async_playwright, expect

from facturador.app import create_app
from facturador.catalog import CATALOG
from facturador.documents import browser_files
from test_core import pdf_bytes


async def smoke():
    origin = 'https://portal.example.test'
    catalog = {'name': 'Portal de prueba', 'hosts': ['portal.example.test'], 'url': origin + '/', 'guide': ''}
    pdf = pdf_bytes()

    async def merchant(route):
        path = urlsplit(route.request.url).path
        if path == '/':
            await route.fulfill(content_type='text/html', body='<a href="/documentos">Documentos</a>')
        elif path == '/documentos':
            await route.fulfill(content_type='text/html', body=(
                '<h1>Documentos de prueba</h1><label>Constancia<input type="file"></label>'
                '<p><a href="/archivo">Descargar PDF</a></p>'))
        elif path == '/archivo':
            await route.fulfill(content_type='application/pdf', body=pdf,
                                headers={'Content-Disposition': 'attachment; filename="comprobante.pdf"'})
        else:
            await route.fulfill(status=404, body='')

    with tempfile.TemporaryDirectory() as folder, socket.socket() as sock, patch.dict(CATALOG, {'prueba': catalog}):
        sock.bind(('127.0.0.1', 0))
        port = sock.getsockname()[1]
        root = Path(folder)
        app = create_app(root, port=port)
        runner = app.state.runner
        server = uvicorn.Server(uvicorn.Config(app, log_level='warning', access_log=False))
        serving = asyncio.create_task(server.serve(sockets=[sock]))
        try:
            async with asyncio.timeout(40):
                while not server.started:
                    if serving.done():
                        await serving
                        raise AssertionError('No arrancó FastAPI.')
                    await asyncio.sleep(.05)
            await runner.start_browser()
            await runner.context.route(origin + '/**', merchant)
            async with async_playwright() as pw:
                operator = await pw.chromium.launch(executable_path=os.getenv('FACTURADOR_CHROMIUM_PATH'), chromium_sandbox=True)
                try:
                    ui = await operator.new_page(viewport={'width': 1500, 'height': 1000})
                    await ui.goto(f'http://127.0.0.1:{port}/#portales')
                    await ui.locator('#ticket-url').fill(origin + '/')
                    await ui.get_by_role('button', name='Abrir enlace en el navegador').click()
                    screen = ui.locator('#browser-image')
                    await expect(screen).to_be_visible(timeout=20_000)
                    await ui.get_by_role('button', name='Tomar control').click()
                    remote = runner.viewer.page

                    async def click_in_portal(locator):
                        await screen.scroll_into_view_if_needed()
                        bounds = await locator.bounding_box()
                        view = await screen.bounding_box()
                        viewport = remote.viewport_size
                        await screen.click(position={
                            'x': (bounds['x'] + bounds['width'] / 2) * view['width'] / viewport['width'],
                            'y': (bounds['y'] + bounds['height'] / 2) * view['height'] / viewport['height']})

                    await click_in_portal(remote.get_by_text('Documentos', exact=True))
                    await expect(remote.get_by_role('heading')).to_have_text('Documentos de prueba')
                    await expect(ui.locator('#browser-address')).to_have_text(origin + '/documentos')
                    await ui.get_by_role('button', name='Atrás', exact=True).click()
                    await expect(remote.get_by_text('Documentos', exact=True)).to_be_visible()
                    await expect(ui.locator('#browser-address')).to_have_text(origin + '/')
                    await ui.get_by_role('button', name='Adelante', exact=True).click()
                    await expect(remote.get_by_role('heading')).to_be_visible()
                    await expect(ui.locator('#browser-address')).to_have_text(origin + '/documentos')
                    await click_in_portal(remote.get_by_label('Constancia'))
                    await expect(ui.locator('#upload-origin')).to_have_text(origin, timeout=12_000)
                    await ui.locator('#portal-file').set_input_files({'name': 'constancia.pdf', 'mimeType': 'application/pdf', 'buffer': pdf})
                    await ui.get_by_role('button', name='Adjuntar al portal').click()
                    await remote.wait_for_function("document.querySelector('input').files.length === 1")
                    actual = await remote.get_by_label('Constancia').evaluate(
                        'async e => Array.from(new Uint8Array(await e.files[0].arrayBuffer()))')
                    assert bytes(actual) == pdf
                    await click_in_portal(remote.get_by_text('Descargar PDF'))
                    async with asyncio.timeout(10):
                        while not browser_files(root):
                            await asyncio.sleep(.05)
                    await ui.get_by_role('button', name='Descargas manuales').click()
                    async with ui.expect_download() as downloaded:
                        await ui.get_by_role('dialog').get_by_role('link', name='Descargar').click()
                    file = await downloaded.value
                    assert file.suggested_filename == 'comprobante.pdf' and (await file.path()).read_bytes() == pdf
                    print('OK: historial, selector del portal, entrega exacta del PDF y descarga manual desde el panel.')
                finally:
                    await operator.close()
        finally:
            server.should_exit = True
            await serving


if __name__ == '__main__':
    asyncio.run(smoke())
