"""Load a synthetic two-page CSF through the real settings screen in Google Chrome."""
import asyncio
import os
import socket
import tempfile
from pathlib import Path

import uvicorn
from playwright.async_api import async_playwright, expect

from facturador.app import create_app
from facturador.auth import write_private
from test_core import PROFILE
from test_fiscal_capture import ID, REGIMES, synthetic_pdf


async def smoke():
    os.environ.pop('FACTURADOR_CONTAINER', None)
    with tempfile.TemporaryDirectory() as directory, socket.socket() as listener:
        root = Path(directory)
        write_private(root/'profile.json', PROFILE.model_dump())
        listener.bind(('127.0.0.1', 0))
        port = listener.getsockname()[1]
        server = uvicorn.Server(uvicorn.Config(create_app(root, port=port), log_level='error', access_log=False))
        task = asyncio.create_task(server.serve(sockets=[listener]))
        try:
            while not server.started:
                if task.done():
                    await task
                await asyncio.sleep(.03)
            async with async_playwright() as pw:
                chrome = await pw.chromium.launch(executable_path='/usr/bin/google-chrome-stable',
                                                  headless=False, chromium_sandbox=True)
                try:
                    page = await chrome.new_page()
                    await page.goto(f'http://127.0.0.1:{port}/#ajustes')
                    fields = page.locator('#profile')
                    await expect(fields.get_by_label('RFC', exact=True)).to_have_value(PROFILE.rfc)
                    previous = fields.get_by_label('Correo de entrega')
                    async with page.expect_file_chooser() as chosen:
                        await page.get_by_role('button', name='Subir constancia', exact=True).click()
                    table = REGIMES.replace('31/12/2020','')
                    await (await chosen.value).set_files({'name':'prueba.pdf', 'mimeType':'application/pdf',
                                                          'buffer':synthetic_pdf(ID,table)})
                    await expect(fields.get_by_label('Código postal')).to_have_value('01234')
                    await expect(fields.get_by_label('Nombre o razón social')).to_have_value('MARIA PENA LOPEZ')
                    await expect(previous).to_have_value(PROFILE.email)
                    chooser = page.get_by_label('Regímenes de la constancia')
                    await expect(chooser).to_be_visible()
                    await chooser.select_option('605')
                    await fields.get_by_label('Tu WhatsApp', exact=False).fill('+525555550100')
                    async with page.expect_response(lambda r: r.url.endswith('/api/profile') and r.request.method=='PUT') as saved:
                        await page.get_by_role('button', name='Guardar datos fiscales').click()
                    assert (await saved.value).ok
                    await page.reload()
                    await expect(fields.get_by_label('Régimen fiscal', exact=True)).to_have_value('605')
                    await expect(fields.get_by_label('Tu WhatsApp', exact=False)).to_have_value('+525555550100')
                    print('OK: Chrome capturó dos páginas, conservó correo y guardó el régimen elegido y teléfono propio.')
                finally:
                    await chrome.close()
        finally:
            server.should_exit = True
            await task


if __name__ == '__main__':
    asyncio.run(smoke())
