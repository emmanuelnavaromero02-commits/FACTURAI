"""Exercise the public CAPUFE recovery dialog with a synthetic toll and merchant."""
import asyncio
from playwright.async_api import async_playwright

from facturador.capufe_recovery import HOST, LOOKUP, _lookup
from facturador.read_http import ServiceWait


async def main():
    code = 'T' * 18
    # The existing page also contains an invoice form with an identically named
    # input. The test must see only the recovery operation.
    url = 'https://' + HOST + LOOKUP
    html = '''<meta charset=utf-8><input id=codigo><button>Facturar</button>
    <a href="#" onclick="document.querySelector('dialog').showModal()">Recuperar una factura, por código alfanúmerico</a>
    <dialog><h2>Recuperar Factura por Código Alfanumérico</h2><input id=codigo>
    <button onclick="fetch('URL?codigo='+this.previousElementSibling.value,{method:'POST'})">check recuperar</button></dialog>'''.replace('URL',url)
    async with async_playwright() as pw:
        browser = await pw.chromium.launch(executable_path='/usr/bin/google-chrome-stable', headless=False, chromium_sandbox=True)
        try:
            context = await browser.new_context()
            page = await context.new_page()
            await page.context.route('**/*', lambda r: r.fulfill(content_type='text/html',body=html))
            # Intercept Route.fetch: exercise actual browser controls while keeping the network offline.
            from unittest.mock import patch
            from playwright.async_api import Route
            seen = []
            status = b'200 OK'
            retry = b''
            async def reply(reader, writer):
                await reader.readuntil(b'\r\n\r\n')
                payload = b'{"mensaje":"El codigo aun no ha sido facturado."}'
                writer.write(b'HTTP/1.1 ' + status + b'\r\n' + retry + b'Content-Type: application/json\r\nConnection: close\r\nContent-Length: ' +
                             str(len(payload)).encode()+b'\r\n\r\n'+payload)
                await writer.drain()
                writer.close()
            server = await asyncio.start_server(reply, '127.0.0.1', 0)
            async def fake_fetch(route, **kwargs):
                seen.append(route.request.url)
                return await page.context.request.get(f'http://127.0.0.1:{server.sockets[0].getsockname()[1]}/')
            async with server:
                with patch.object(Route,'fetch',fake_fetch):
                    files, negative, _ = await _lookup(page,code)
                    assert negative and not files
                    status = b'404 Not Found'
                    missing = await page.context.new_page()
                    assert (await _lookup(missing, code))[:2] == ({}, False)
                    status, retry = b'429 Too Many Requests', b'Retry-After: 900\r\n'
                    limited = await page.context.new_page()
                    try:
                        await _lookup(limited, code)
                    except ServiceWait as exc:
                        assert exc.seconds == 900
                    else:
                        raise AssertionError('Faltó respetar la espera indicada por el servicio.')
            assert seen == [url+'?codigo='+code]*3
            print('Chrome comprobó recuperación, error HTTP y espera del comercio. Ninguna consulta emitió una factura.')
        finally:
            await browser.close()


if __name__ == '__main__':
    asyncio.run(main())
