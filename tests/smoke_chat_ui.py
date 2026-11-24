"""Check the chat, grouped attachments, real progress and viewer lifecycle in Chrome."""
import asyncio
import io
import os
from pathlib import Path
from urllib.parse import urlsplit

from PIL import Image
from playwright.async_api import async_playwright, expect


WEB = Path(__file__).resolve().parents[1] / 'web'
ORIGIN = 'http://127.0.0.1:8765'
CHROME = os.environ.get('CHROME_EXECUTABLE') or (
    'C:/Program Files/Google/Chrome/Application/chrome.exe' if os.name == 'nt' else '/usr/bin/google-chrome-stable')


def receipt(status='queued'):
    return {
        'id': 'chat-ticket', 'status': status, 'message': 'Preparando el portal', 'historical': False,
        'batch_id': 'chat-example', 'can_retry': False, 'retry_action': None,
        'issue_attempted': False, 'issuance_seen': False, 'human_exposed': False,
        'has_pdf': False, 'has_xml': False,
        'receipt': {'provider': 'portal_ticket', 'portal_url': 'https://tienda.example/facturas',
                    'merchant': 'Comercio de prueba', 'amount': '10.00', 'date': '2026-11-23',
                    'payment_code': '01', 'ticket_number': '1234', 'source': 'archivo: ticket.jpg',
                    'billing_codes': [], 'details': ''},
    }


class ChatFixture:
    def __init__(self):
        self.messages = []
        self.posts = []
        self.state = {
            'token': 'test-ui', 'jobs': [], 'catalog': {}, 'active_job': None,
            'plan': {'status': 'ready', 'model': 'gpt-6.1-sol'}, 'profile': {}, 'gmail': 'disconnected',
            'automation': {'enabled': True, 'phase': 'idle', 'current_id': None, 'message': ''},
            'supports_ticket_retry': True,
            'browser': {'open': False, 'mode': 'idle', 'revision': 0, 'manual': False,
                        'engine': 'Google Chrome', 'url': '', 'job_id': None},
        }
        frame = io.BytesIO()
        Image.new('RGB', (80, 60), 'white').save(frame, 'JPEG')
        self.picture = frame.getvalue()

    async def serve(self, route):
        request = route.request
        url = urlsplit(request.url)
        if url.netloc != '127.0.0.1:8765':
            return await route.abort()
        if url.path == '/api/state':
            return await route.fulfill(json=self.state)
        if url.path == '/api/chat':
            if request.method == 'GET':
                return await route.fulfill(json={'messages': self.messages})
            body = request.post_data_buffer or b''
            self.posts.append(body)
            assert b'partido' in body and b'parte-1.jpg' in body and b'parte-2.jpg' in body
            self.messages = [
                {'id': 'm1', 'role': 'user', 'text': 'Mi ticket está partido en dos fotos.',
                 'at': '2026-11-23T20:00:00Z', 'files': [
                     {'name': 'parte-1.jpg', 'url': '/api/chat/files/1'},
                     {'name': 'parte-2.jpg', 'url': '/api/chat/files/2'}], 'job_ids': ['chat-ticket']},
                {'id': 'm2', 'role': 'assistant', 'text': 'Leeré las dos partes del mismo ticket.',
                 'at': '2026-11-23T20:00:01Z', 'files': [], 'job_ids': ['chat-ticket']},
            ]
            self.state['jobs'] = [receipt()]
            self.state['automation'].update(phase='reading', current_id='chat-ticket')
            return await route.fulfill(json={'messages': self.messages, 'job_ids': ['chat-ticket'], 'intent': 'invoice'})
        if url.path.startswith('/api/chat/files/'):
            return await route.fulfill(body=self.picture, content_type='image/jpeg')
        if url.path == '/api/jobs/chat-ticket/events':
            return await route.fulfill(json=[
                {'kind': 'browser_opened', 'detail': 'Se abrió el portal del comercio.', 'at': '2026-11-23T20:00:02Z'},
                {'kind': 'campo_verificado', 'detail': 'Campo cotejado: código postal.', 'at': '2026-11-23T20:00:03Z'},
            ])
        if url.path == '/api/browser/frame':
            return await route.fulfill(body=self.picture, content_type='image/jpeg')
        asset = WEB / (url.path.removeprefix('/') or 'index.html')
        if asset.parent == WEB and asset.is_file():
            kinds = {'.js': 'text/javascript', '.css': 'text/css', '.html': 'text/html'}
            return await route.fulfill(path=asset, content_type=kinds[asset.suffix])
        return await route.fulfill(status=404, body='Recurso fuera de la prueba')


async def smoke():
    async with async_playwright() as playwright:
        browser = await playwright.chromium.launch(executable_path=CHROME, headless=True)
        try:
            fixture = ChatFixture()
            page = await browser.new_page(service_workers='block')
            errors = []
            page.on('pageerror', lambda error: errors.append(str(error)))
            await page.route('**/*', fixture.serve)
            await page.goto(ORIGIN + '/')
            await expect(page.get_by_role('heading', name='FACTURAI')).to_be_visible()
            await expect(page.locator('.sidebar')).to_be_hidden()
            await expect(page.locator('#chat-browser-slot')).to_be_hidden()
            await page.locator('#chat-text').fill('Mi ticket está partido en dos fotos.')
            await page.locator('#chat-files').set_input_files([
                {'name': 'parte-1.jpg', 'mimeType': 'image/jpeg', 'buffer': fixture.picture},
                {'name': 'parte-2.jpg', 'mimeType': 'image/jpeg', 'buffer': fixture.picture},
            ])
            await expect(page.locator('.chat-attachment')).to_have_count(2)
            await page.locator('#chat-send').click()
            await expect(page.locator('.chat-message')).to_have_count(2)
            await expect(page.locator('.chat-message-files img')).to_have_count(2)
            await expect(page.locator('#chat-progress-state')).to_have_text('Leyendo imágenes')
            assert len(fixture.posts) == 1, fixture.posts

            fixture.state['jobs'][0].update(status='running', message='Abriendo el formulario del comercio')
            fixture.state['active_job'] = 'chat-ticket'
            fixture.state['automation']['phase'] = 'running'
            fixture.state['browser'].update(open=True, mode='job', revision=1,
                                             url='https://tienda.example/facturas', job_id='chat-ticket')
            await expect(page.locator('#chat-browser-slot')).to_be_visible()
            await expect(page.locator('#browser-image')).to_be_visible()
            await expect(page.locator('#chat-events')).to_contain_text('Cotejó un campo')
            await expect(page.locator('#chat-events')).to_contain_text('código postal')
            if os.environ.get('CHAT_SMOKE_SCREENSHOT'):
                await page.screenshot(path=os.environ['CHAT_SMOKE_SCREENSHOT'], full_page=True)

            fixture.state['jobs'][0].update(status='verified', message='PDF y XML cotejados', has_pdf=True, has_xml=True)
            fixture.state['active_job'] = None
            fixture.state['browser'].update(open=False, mode='idle', revision=2, url='', job_id=None)
            await expect(page.locator('#chat-browser-slot')).to_be_hidden()
            await expect(page.locator('#chat-progress-state')).to_have_text('Factura comprobada')
            await expect(page.locator('#chat-progress-actions')).to_contain_text('Descargar PDF')
            fixture.state['jobs'][0].update(status='ready', message='Esperando perfil fiscal', has_pdf=False, has_xml=False)
            fixture.state['automation'].update(phase='waiting_profile', current_id='chat-ticket')
            await expect(page.locator('#chat-progress-state')).to_have_text('Faltan datos fiscales')
            await expect(page.locator('#chat-progress-actions')).to_contain_text('Completar datos fiscales')
            assert not errors, errors
        finally:
            await browser.close()


if __name__ == '__main__':
    asyncio.run(smoke())
