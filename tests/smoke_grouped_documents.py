"""Show both ticket allocations while downloading one original invoice from the real panel."""
import asyncio
import socket
import tempfile
from pathlib import Path

import uvicorn
from playwright.async_api import async_playwright, expect

from facturador.app import create_app
from facturador.documents import reconcile, save_candidate
from test_core import FISCAL_UUID, PROFILE, pdf_bytes
from test_grouped_documents import CODES, add_toll, toll_xml


async def smoke():
    with tempfile.TemporaryDirectory() as tmp, socket.socket() as listener:
        listener.bind(('127.0.0.1', 0))
        port = listener.getsockname()[1]
        app = create_app(Path(tmp), port=port)
        store = app.state.store
        first = add_toll(store, 'primer-peaje', CODES[0])
        save_candidate(store, first, 'factura.pdf', pdf_bytes())
        save_candidate(store, first, 'factura.xml', toll_xml())
        reconcile(store, first, PROFILE)
        second = add_toll(store, 'segundo-peaje', CODES[1])
        reconcile(store, second, PROFILE)
        server = uvicorn.Server(uvicorn.Config(app, log_level='error', access_log=False))
        serving = asyncio.create_task(server.serve(sockets=[listener]))
        try:
            while not server.started:
                if serving.done():
                    await serving
                await asyncio.sleep(.03)
            async with async_playwright() as pw:
                chrome = await pw.chromium.launch(executable_path='/usr/bin/google-chrome-stable', headless=False, chromium_sandbox=True)
                try:
                    page = await chrome.new_page()
                    await page.goto(f'http://127.0.0.1:{port}/#comprobantes')
                    await expect(page.locator('#count-verified')).to_have_text('1')
                    cells = page.locator('#documents .ticket-amount')
                    await expect(cells).to_have_count(2)
                    for i, key in enumerate((first, second)):
                        await expect(cells.nth(i)).to_contain_text('$50.00')
                        await expect(cells.nth(i)).to_contain_text('Total del CFDI $100.00')
                        for suffix, content in [('xml', toll_xml()), ('pdf', pdf_bytes())]:
                            anchor = page.locator(f'#documents a[href="/api/jobs/{key}/files/{suffix}"]')
                            async with page.expect_download() as item:
                                await anchor.click()
                            download = await item.value
                            assert download.suggested_filename == FISCAL_UUID + '.' + suffix
                            assert Path(await download.path()).read_bytes() == content
                finally:
                    await chrome.close()
        finally:
            server.should_exit = True
            await serving
    print('Chrome mostró dos tickets de $50, un CFDI de $100 y las mismas descargas originales para ambos.')


if __name__ == '__main__':
    asyncio.run(smoke())
