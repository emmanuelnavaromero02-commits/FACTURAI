"""Panel in a real browser: reduced photo uploads with progress and stages, and taps on a phone-sized viewer."""
import asyncio
import glob
import io
import json
import os
import re
import socket
import tempfile
import threading
import time
import unittest
from pathlib import Path
from unittest.mock import patch
from urllib.parse import parse_qs, urlsplit

import uvicorn
from PIL import Image
from playwright.async_api import async_playwright, expect

from facturador.app import create_app

WEB = Path(__file__).resolve().parents[1] / 'web'


def browser_executable() -> str | None:
    candidates = [os.environ.get('CHROME_EXECUTABLE', ''), 'C:/Program Files/Google/Chrome/Application/chrome.exe',
                  '/usr/bin/google-chrome-stable', '/usr/bin/chromium',
                  *sorted(glob.glob(os.path.expanduser('~/AppData/Local/ms-playwright/chromium-*/chrome-win*/chrome.exe'))),
                  *sorted(glob.glob(os.path.expanduser('~/.cache/ms-playwright/chromium-*/chrome-linux*/chrome')))]
    return next((path for path in candidates if path and os.path.isfile(path)), None)


EXECUTABLE = browser_executable()


def free_port() -> int:
    with socket.socket() as sock:
        sock.bind(('127.0.0.1', 0))
        return sock.getsockname()[1]


async def until(read, wanted, timeout=10):
    deadline = time.monotonic() + timeout
    while (value := read()) != wanted and time.monotonic() < deadline:
        await asyncio.sleep(.05)
    return value


# Records every value a progress bar takes, because the bar hides as soon as the upload ends.
RECORD_PROGRESS = '''
window.progressSeen = {};
new MutationObserver(changes => {
  for (const change of changes) {
    const bar = change.target;
    if (bar.tagName === 'PROGRESS') (window.progressSeen[bar.id] ||= []).push(Number(bar.getAttribute('value')));
  }
}).observe(document, {subtree: true, attributes: true, attributeFilter: ['value']});
'''


def large_photo() -> bytes:
    gradient = Image.linear_gradient('L').resize((4000, 3000))
    output = io.BytesIO()
    Image.merge('RGB', (gradient, gradient.transpose(Image.Transpose.FLIP_LEFT_RIGHT), gradient)).save(output, 'JPEG', quality=92)
    return output.getvalue()


@unittest.skipUnless(EXECUTABLE, 'Requiere Google Chrome o Chromium')
class UploadInBrowserTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        folder = tempfile.TemporaryDirectory()
        self.addCleanup(folder.cleanup)
        self.port = free_port()
        self.app = create_app(Path(folder.name), port=self.port)
        quiet = patch.object(self.app.state.automation, 'start', lambda: None)
        quiet.start()
        self.addCleanup(quiet.stop)
        self.server = uvicorn.Server(uvicorn.Config(self.app, host='127.0.0.1', port=self.port, log_level='warning'))
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

    async def test_large_photo_arrives_reduced_with_progress_and_stages(self):
        page = await self.browser.new_page()
        await page.add_init_script(RECORD_PROGRESS)
        errors = []
        page.on('pageerror', lambda error: errors.append(str(error)))
        await page.goto(f'http://127.0.0.1:{self.port}/')
        await expect(page.get_by_role('heading', name='FACTURAI')).to_be_visible()
        await page.wait_for_function('document.querySelector("#chat-send") && !document.querySelector("#chat-camera").disabled')
        self.assertEqual(await page.get_attribute('#chat-camera-file', 'capture'), 'environment')
        held = asyncio.Event()
        release = asyncio.Event()
        bodies = []

        async def hold(route):
            bodies.append(route.request.post_data_buffer or b'')
            held.set()
            await release.wait()
            await route.continue_()
        await page.route('**/api/chat', lambda route: hold(route) if route.request.method == 'POST' else route.continue_())
        original = large_photo()
        await page.locator('#chat-files').set_input_files({'name': 'ticket-grande.jpg', 'mimeType': 'image/jpeg', 'buffer': original})
        await expect(page.locator('.chat-attachment')).to_have_count(1)
        await page.locator('#chat-send').click()
        await asyncio.wait_for(held.wait(), 20)
        bar = page.locator('#chat-upload')
        await expect(bar).to_be_visible()
        await expect(page.locator('#chat-stages')).to_contain_text('Subiendo')
        await expect(page.locator('#chat-stages li.current')).to_have_text('Subiendo')
        release.set()
        await expect(page.locator('.chat-message')).to_have_count(2, timeout=15000)
        await expect(bar).to_be_hidden()
        await expect(page.locator('#chat-stages li.current')).to_have_text('Leyendo')
        await expect(page.locator('#chat-stages .stage-time')).to_have_text(re.compile(r'^\d+:\d\d$'))
        self.assertEqual(len(bodies), 1)
        seen = await page.evaluate('window.progressSeen["chat-upload"]')
        self.assertEqual((seen[0], max(seen)), (0, 100), seen)
        rows = self.app.state.store.list()
        self.assertEqual(len(rows), 1)
        with Image.open(rows[0]['source_path']) as stored:
            self.assertEqual(stored.format, 'JPEG')
            self.assertEqual(stored.size, (3200, 2400))
        # The thread shows a reduced preview of the photo, never the original.
        preview = page.locator('.chat-message-files img')
        await expect(preview).to_have_attribute('src', re.compile(r'/api/chat/files/[0-9a-f]{32}\?w=320$'))
        self.assertEqual(errors, [])

    async def test_action_before_the_first_state_waits_for_the_panel_token(self):
        page = await self.browser.new_page()
        held, release = asyncio.Event(), asyncio.Event()

        async def hold(route):
            held.set()
            await release.wait()
            await route.continue_()
        # The first state is held back: the panel is on screen but has no token yet.
        await page.route('**/api/state', hold)
        tokens = []
        page.on('request', lambda request: tokens.append(request.headers.get('x-app-token'))
                if request.method == 'POST' and request.url.endswith('/api/demo') else None)
        await page.goto(f'http://127.0.0.1:{self.port}/#bandeja')
        await asyncio.wait_for(held.wait(), 20)
        await page.get_by_role('button', name='Cargar ticket de ejemplo').click()
        await asyncio.sleep(.4)
        self.assertEqual(tokens, [])  # Nothing leaves without the token, and nothing is refused.
        await expect(page.locator('#notice')).to_be_hidden()
        release.set()
        await expect(page.locator('#notice')).to_contain_text('Ticket de ejemplo disponible')
        self.assertEqual(tokens, [self.app.state.csrf_token])

    async def test_inbox_upload_shows_progress_and_reading_stage(self):
        page = await self.browser.new_page()
        await page.add_init_script(RECORD_PROGRESS)
        errors = []
        page.on('pageerror', lambda error: errors.append(str(error)))
        await page.goto(f'http://127.0.0.1:{self.port}/#bandeja')
        await expect(page.get_by_role('heading', name='Tickets y facturas')).to_be_visible()
        await page.wait_for_function('document.querySelector("#automation-enabled") && !document.querySelector("#automation-enabled").disabled')
        held, release = asyncio.Event(), asyncio.Event()

        async def hold(route):
            held.set()
            await release.wait()
            await route.continue_()
        await page.route('**/api/upload', hold)
        await page.locator('#photo-upload').set_input_files({'name': 'foto.jpg', 'mimeType': 'image/jpeg', 'buffer': large_photo()})
        await asyncio.wait_for(held.wait(), 20)
        await expect(page.locator('#upload-progress')).to_be_visible()
        await expect(page.locator('#upload-stage li.current')).to_have_count(1)
        release.set()
        await expect(page.locator('#upload-progress')).to_be_hidden(timeout=15000)
        seen = await page.evaluate('window.progressSeen["upload-bar"]')
        self.assertEqual((seen[0], max(seen)), (0, 100), seen)
        await expect(page.locator('#tickets .stages .stage-now')).to_contain_text('Te necesito')
        row = self.app.state.store.list()[0]
        with Image.open(row['source_path']) as stored:
            self.assertLessEqual(max(stored.size), 3200)
        self.assertEqual(errors, [])


class TouchFixture:
    """Simulated panel API with a tall frame that the viewer letterboxes."""

    def __init__(self):
        self.inputs = []
        self.frames = []
        output = io.BytesIO()
        Image.new('RGB', (1280, 2000), 'white').save(output, 'JPEG')
        self.frame = output.getvalue()
        row = {'id': 'ticket-1', 'status': 'needs_user', 'human_kind': 'operator', 'message': 'Completa el formulario',
               'historical': False, 'issue_attempted': False, 'issuance_seen': False, 'human_exposed': True,
               'has_pdf': False, 'has_xml': False, 'can_retry': False, 'retry_action': None, 'batch_id': '',
               'receipt': {'provider': 'portal_ticket', 'portal_url': 'https://portal.example/facturar', 'merchant': 'Tienda',
                           'amount': '10.00', 'date': '2026-11-20', 'payment_code': '01', 'ticket_number': '1',
                           'source': 'archivo: ticket.jpg', 'billing_codes': [], 'details': ''}}
        self.state = {
            'token': 'prueba', 'jobs': [row], 'catalog': {}, 'active_job': 'ticket-1', 'profile': {},
            'plan': {'status': 'ready'}, 'gmail': 'disconnected', 'supports_ticket_retry': True,
            'automation': {'enabled': True, 'phase': 'running', 'current_id': 'ticket-1', 'message': ''},
            'browser': {'open': True, 'mode': 'job', 'manual': True, 'revision': 1, 'job_id': 'ticket-1',
                        'url': 'https://portal.example/facturar', 'label': 'Tienda', 'engine': 'Google Chrome'},
        }

    async def serve(self, route):
        request = route.request
        url = urlsplit(request.url)
        if url.netloc != '127.0.0.1:8765':
            return await route.abort()
        if url.path == '/api/state':
            return await route.fulfill(json=self.state)
        if url.path == '/api/chat':
            return await route.fulfill(json={'messages': []})
        if url.path.endswith('/events'):
            return await route.fulfill(json=[])
        if url.path == '/api/browser/frame':
            self.frames.append(parse_qs(url.query))
            return await route.fulfill(body=self.frame, content_type='image/jpeg', headers={'X-Frame-Hash': 'abc'})
        if url.path == '/api/browser/control':
            return await route.fulfill(json={'channel': 'canal-movil'})
        if url.path == '/api/browser/input':
            self.inputs.append(json.loads(request.post_data))
            return await route.fulfill(json={'sent': True})
        if url.path == '/api/browser/release':
            return await route.fulfill(json={'released': True})
        asset = WEB / (url.path.removeprefix('/') or 'index.html')
        if asset.parent == WEB and asset.is_file():
            kinds = {'.js': 'text/javascript', '.css': 'text/css', '.html': 'text/html'}
            return await route.fulfill(path=asset, content_type=kinds[asset.suffix])
        return await route.fulfill(status=404, body='Fuera de la prueba')

    def events(self):
        result = []
        for body in self.inputs:
            assert body['channel'] == 'canal-movil', body
            result += body.get('events') or [{key: value for key, value in body.items() if key != 'channel'}]
        return result


@unittest.skipUnless(EXECUTABLE, 'Requiere Google Chrome o Chromium')
class PhoneViewerTests(unittest.IsolatedAsyncioTestCase):
    async def test_tap_lands_on_the_page_point_and_phone_keyboard_types(self):
        fixture = TouchFixture()
        async with async_playwright() as playwright:
            browser = await playwright.chromium.launch(executable_path=EXECUTABLE, headless=True)
            try:
                context = await browser.new_context(viewport={'width': 375, 'height': 812}, has_touch=True, is_mobile=True,
                                                    device_scale_factor=1, service_workers='block')
                page = await context.new_page()
                errors = []
                page.on('pageerror', lambda error: errors.append(str(error)))
                await context.route('**/*', fixture.serve)
                await page.goto('http://127.0.0.1:8765/#navegador')
                image = page.locator('#browser-image')
                await expect(image).to_be_visible()
                await page.wait_for_function('document.querySelector("#browser-image").naturalWidth === 1280')
                await page.locator('#take-control').click()
                await expect(page.locator('#take-control')).to_have_text('Devolver control')
                box = await image.evaluate('''node => {
                    const r = node.getBoundingClientRect();
                    const scale = Math.min(r.width / node.naturalWidth, r.height / node.naturalHeight);
                    const w = node.naturalWidth * scale, h = node.naturalHeight * scale;
                    return {left: r.left + (r.width - w) / 2, top: r.top + (r.height - h) / 2, w, h, boxWidth: r.width};
                }''')
                self.assertLess(box['w'], box['boxWidth'] - 40)  # The frame really is letterboxed.
                await page.touchscreen.tap(box['left'] + box['w'] * .25, box['top'] + box['h'] * .75)
                self.assertEqual(await until(lambda: [e['kind'] for e in fixture.events()], ['down', 'up']), ['down', 'up'])
                down, up = fixture.events()
                for event in (down, up):
                    self.assertAlmostEqual(event['x'], .25, delta=.02)
                    self.assertAlmostEqual(event['y'], .75, delta=.02)
                self.assertEqual(len(fixture.inputs), 1)  # Both events travelled in one batch.
                typing = page.get_by_role('textbox', name='Escribir con el teclado del teléfono')
                await expect(typing).to_be_visible()
                await typing.fill('hola')
                await typing.press('Enter')
                typed = [('text', 'hola'), ('key', 'Enter')]
                self.assertEqual(await until(lambda: [(e['kind'], e.get('value')) for e in fixture.events()[2:]], typed), typed)
                self.assertTrue(any('w' in query for query in fixture.frames))
                widths = {int(query['w'][0]) for query in fixture.frames if 'w' in query}
                self.assertTrue(all(width < 1280 for width in widths), widths)
                self.assertEqual(errors, [])
            finally:
                await browser.close()


if __name__ == '__main__':
    unittest.main()
