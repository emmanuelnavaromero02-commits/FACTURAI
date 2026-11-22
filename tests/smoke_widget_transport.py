"""Check Chrome's native two-origin transport using HTTPS fixtures on loopback, without a challenge service."""
import asyncio
import os
import ssl
import subprocess
import tempfile
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from unittest.mock import AsyncMock, patch
from urllib.parse import urlsplit

from playwright.async_api import async_playwright

from facturador.catalog import CUSTOM_PORTAL
from facturador.db import Store
from facturador.runner import Runner


async def smoke():
    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp)
        cert, key = root/'cert.pem', root/'key.pem'
        subprocess.run(['openssl','req','-x509','-newkey','rsa:2048','-nodes','-days','1','-subj','/CN=local-fixture',
                        '-keyout',str(key),'-out',str(cert)], check=True, capture_output=True)
        observed = []
        class Pages(BaseHTTPRequestHandler):
            def log_message(self, *args):
                pass

            def do_GET(self):
                if self.path == '/ticket':
                    content = f'<iframe title="Herramienta sintetica" src="{widget}/widget"></iframe>'
                else:
                    content = '''<button id="ordinary">Probar entrega</button><button id="redirect">Probar redireccion</button>
                      <script>for (const b of document.querySelectorAll('button')) b.onclick = async () => {
                      try { const r = await fetch('/' + b.id, {method:'POST',body:'fixture'}); b.dataset.outcome = r.status; }
                      catch { b.dataset.outcome = 'blocked'; } };</script>'''
                body = content.encode()
                self.send_response(200)
                self.send_header('Content-Type','text/html; charset=utf-8')
                self.send_header('Content-Length',str(len(body)))
                self.end_headers()
                self.wfile.write(body)

            def do_POST(self):
                self.rfile.read(int(self.headers['Content-Length']))
                observed.append((self.headers['Host'],self.path))
                self.send_response(307 if self.path == '/redirect' else 200)
                if self.path == '/redirect':
                    self.send_header('Location', outsider+'/other')
                self.send_header('Content-Length','0')
                self.end_headers()

        with ThreadingHTTPServer(('127.0.0.1',0), Pages) as server:
            tls = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
            tls.load_cert_chain(cert, key)
            server.socket = tls.wrap_socket(server.socket, server_side=True)
            port = server.server_port
            merchant = f'https://shop.example.com:{port}'
            widget = f'https://widget.example.org:{port}'
            outsider = f'https://other.example.net:{port}'
            threading.Thread(target=server.serve_forever,daemon=True).start()
            runner = Runner(Store(root/'store'))
            try:
                async with async_playwright() as pw:
                    browser = await pw.chromium.launch(executable_path=os.environ['FACTURADOR_CHROMIUM_PATH'],headless=False,
                        chromium_sandbox=True,args=['--no-proxy-server',
                        '--host-resolver-rules=MAP shop.example.com 127.0.0.1, MAP widget.example.org 127.0.0.1, MAP other.example.net 127.0.0.1'])
                    context = await browser.new_context(ignore_https_errors=True,service_workers='block')
                    page = await context.new_page()
                    await page.goto(merchant+'/ticket')
                    frame = next(f for f in page.frames if f.url.startswith(widget+'/'))
                    row = runner.store.add('prueba','local')[0]
                    runner.active_id = row['id']
                    runner.store.update(row['id'],receipt={'provider':CUSTOM_PORTAL,'portal_url':page.url})
                    runner.page_job[page] = row['id']
                    runner.page_portal_origins[page] = merchant
                    await runner.viewer.show(page,'Comercio local',manual=True)
                    runner.pending[row['id']] = {'kind':'captcha','page':page,'url':page.url,'future':asyncio.get_running_loop().create_future(),
                                                 'control_epoch':runner.viewer.control_epoch,'challenge_seen':True}
                    def synthetic(url):
                        return widget if urlsplit(url).netloc == urlsplit(widget).netloc else None
                    with (patch('facturador.captcha_transport.widget_origin',side_effect=synthetic),
                          patch('facturador.runner.resolves_publicly',new=AsyncMock(return_value=True))):
                        await runner.widgets.arm(frame)
                        assert frame in runner.widgets.sessions
                        await context.route('**/*',runner.guard_navigation)
                        async def send(name):
                            button = frame.locator('#'+name)
                            await button.evaluate("b => delete b.dataset.outcome")
                            await button.click()
                            return await frame.wait_for_function("id => document.getElementById(id).dataset.outcome || false",arg=name,timeout=6500)
                        assert await (await send('ordinary')).json_value() == 'blocked'
                        lease = await runner.viewer.take_control(runner.viewer.revision)
                        assert await (await send('ordinary')).json_value() == '200'
                        assert await (await send('redirect')).json_value() == 'blocked'
                        await runner.viewer.release(lease)
                        assert await (await send('ordinary')).json_value() == 'blocked'
                    assert observed == [(urlsplit(widget).netloc,'/ordinary'),(urlsplit(widget).netloc,'/redirect')], observed
                    await browser.close()
                print('OK Chrome: mensaje de iframe local únicamente durante cesión; redirección 307 bloqueada sin entrega al tercero.')
            finally:
                runner.plan.close()
                server.shutdown()


if __name__ == '__main__':
    asyncio.run(smoke())
