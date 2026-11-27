"""Lightweight panel: cached state, history pages, HEIC, previews, viewer frames and static files."""
import asyncio
import hashlib
import io
import re
import tempfile
import threading
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock, patch

import httpx
import pillow_heif
from fastapi.testclient import TestClient
from PIL import Image

from facturador import chat as chat_module
from facturador.app import IMMUTABLE, create_app
from facturador.ingest import heif_to_jpeg
from facturador.viewer import BrowserView, ViewerBatch, ViewerEvent


def picture(size=(64, 48), color='white', kind='PNG'):
    output = io.BytesIO()
    Image.new('RGB', size, color).save(output, kind)
    return output.getvalue()


def heic(size=(120, 90), color='orange'):
    pillow_heif.register_heif_opener()
    output = io.BytesIO()
    Image.new('RGB', size, color).save(output, 'HEIF', quality=80)
    return output.getvalue()


class PanelCase(unittest.TestCase):
    def setUp(self):
        folder = tempfile.TemporaryDirectory()
        self.addCleanup(folder.cleanup)
        self.root = Path(folder.name)
        self.app = create_app(self.root)
        self.store = self.app.state.store
        quiet = patch.object(self.app.state.automation, 'start', lambda: None)
        quiet.start()
        self.addCleanup(quiet.stop)
        self.client = TestClient(self.app)
        self.client.__enter__()
        self.addCleanup(self.client.__exit__, None, None, None)
        self.token = self.client.get('/api/state').json()['token']
        self.headers = {'x-app-token': self.token}

    def add_jobs(self, count, status='verified'):
        keys = []
        for number in range(count):
            row, _ = self.store.add(f'/nada/{number}.png', hashlib.sha256(str(number).encode()).hexdigest(), f'archivo: {number}')
            if status != 'new':
                self.store.update(row['id'], status=status)
            keys.append(row['id'])
        return keys


class StateTests(PanelCase):
    def test_state_etag_answers_304_until_something_changes(self):
        first = self.client.get('/api/state')
        tag = first.headers['etag']
        self.assertRegex(tag, r'^"[0-9a-f]{32}"$')
        self.assertNotIn('catalog', first.json())
        again = self.client.get('/api/state', headers={'if-none-match': tag})
        self.assertEqual(again.status_code, 304)
        self.assertEqual(again.content, b'')
        self.assertEqual(again.headers['etag'], tag)
        self.add_jobs(1, 'new')
        changed = self.client.get('/api/state', headers={'if-none-match': tag})
        self.assertEqual(changed.status_code, 200)
        self.assertNotEqual(changed.headers['etag'], tag)
        self.assertEqual(len(changed.json()['jobs']), 1)

    def test_catalog_is_served_once_with_its_own_etag(self):
        response = self.client.get('/api/catalog')
        self.assertEqual(response.status_code, 200)
        self.assertIn('capufe', response.json())
        self.assertEqual(self.client.get('/api/catalog', headers={'if-none-match': response.headers['etag']}).status_code, 304)
        self.assertEqual(self.client.get('/api/state').json()['catalog_url'], '/api/catalog')

    def test_state_keeps_recent_and_open_tickets_and_pages_the_history(self):
        keys = self.add_jobs(75)
        self.store.update(keys[0], status='review')  # Oldest ticket still waits for the person.
        state = self.client.get('/api/state').json()
        listed = [row['id'] for row in state['jobs']]
        self.assertEqual(len(listed), 61)
        self.assertIn(keys[0], listed)
        self.assertEqual(set(listed), set(keys[-60:]) | {keys[0]})
        self.assertEqual(listed[-1], keys[0])
        self.assertEqual(state['jobs_summary'], {'total': 75, 'pending': 1, 'verified': 74})
        self.assertTrue(state['history_cursor'])
        self.assertNotIn('source_path', state['jobs'][0])
        seen, cursor, pages = set(listed), state['history_cursor'], 0
        while cursor:
            page = self.client.get('/api/jobs', params={'before': cursor, 'limit': 6})
            self.assertEqual(page.status_code, 200)
            body = page.json()
            self.assertLessEqual(len(body['jobs']), 6)
            seen.update(row['id'] for row in body['jobs'])
            cursor, pages = body['next'], pages + 1
        self.assertEqual(pages, 3)
        self.assertEqual(seen, set(keys))
        self.assertEqual(self.client.get('/api/jobs', params={'before': 'no-es-un-cursor'}).status_code, 422)

    def test_small_inbox_has_no_history_cursor(self):
        self.add_jobs(3)
        state = self.client.get('/api/state').json()
        self.assertEqual(state['history_cursor'], '')
        self.assertEqual(len(state['jobs']), 3)

    def test_events_and_chat_return_only_what_is_new(self):
        key = self.add_jobs(1, 'new')[0]
        for number in range(3):
            self.store.event(key, 'action', f'Paso {number}')
        events = self.client.get(f'/api/jobs/{key}/events', params={'after': 0}).json()
        self.assertEqual([event['detail'] for event in events], ['Paso 0', 'Paso 1', 'Paso 2'])
        newer = self.client.get(f'/api/jobs/{key}/events', params={'after': events[1]['id']}).json()
        self.assertEqual([event['detail'] for event in newer], ['Paso 2'])
        self.assertNotIn('id', self.client.get(f'/api/jobs/{key}/events').json()[0])

        self.store.add_assistant_message('Hola', [])
        history = self.client.get('/api/chat')
        body = history.json()
        self.assertEqual(len(body['messages']), 1)
        last = body['last']
        self.assertEqual(body['messages'][0]['seq'], last)
        unchanged = self.client.get('/api/chat', params={'after': last}, headers={'if-none-match': history.headers['etag']})
        self.assertEqual(unchanged.status_code, 304)
        self.assertEqual(self.client.get('/api/chat', params={'after': last}).json()['messages'], [])
        self.store.add_assistant_message('¿Pagaste con tarjeta?', [])
        fresh = self.client.get('/api/chat', params={'after': last}, headers={'if-none-match': history.headers['etag']})
        self.assertEqual(fresh.status_code, 200)
        self.assertEqual([m['text'] for m in fresh.json()['messages']], ['¿Pagaste con tarjeta?'])
        self.assertTrue(fresh.json()['partial'])


class HeicTests(PanelCase):
    def test_heic_upload_is_kept_as_a_jpeg_original(self):
        reply = self.client.post('/api/upload', headers=self.headers,
                                 files=[('files', ('IMG_0001.HEIC', heic(), 'image/heic'))])
        self.assertEqual(reply.status_code, 200, reply.text)
        row = self.store.get(reply.json()[0]['id'])
        self.assertTrue(row['source_path'].endswith('.jpg'))
        self.assertIn('IMG_0001.jpg', row['receipt']['source'])
        with Image.open(row['source_path']) as stored:
            self.assertEqual((stored.format, stored.size), ('JPEG', (120, 90)))

    def test_heic_in_chat_becomes_the_same_jpeg_for_the_ticket_and_the_preview(self):
        reply = self.client.post('/api/chat', headers=self.headers, data={'text': '', 'client_id': 'heic_1234567890123456'},
                                 files=[('files', ('foto.heic', heic((90, 160)), 'image/heic'))])
        self.assertEqual(reply.status_code, 200, reply.text)
        body = reply.json()
        self.assertEqual(body['intent'], 'invoice')
        self.assertEqual(body['messages'][0]['files'][0]['name'], 'foto.jpg')
        row = self.store.get(body['job_ids'][0])
        asset = self.store.chat_asset(body['messages'][0]['files'][0]['url'].rsplit('/', 1)[1])
        self.assertEqual(asset['path'], row['source_path'])
        with Image.open(row['source_path']) as stored:
            self.assertEqual((stored.format, stored.size), ('JPEG', (90, 160)))

    def test_heic_limits_match_other_images(self):
        with patch('facturador.ingest.MAX_PIXELS', 100):
            with self.assertRaisesRegex(ValueError, 'demasiado grande'):
                heif_to_jpeg('a.heic', heic())
        with self.assertRaisesRegex(ValueError, '25 MB'):
            heif_to_jpeg('a.heic', b'')
        bad = self.client.post('/api/upload', headers=self.headers, files=[('files', ('a.heic', picture(), 'image/heic'))])
        self.assertEqual(bad.status_code, 422)
        disguised = self.client.post('/api/upload', headers=self.headers, files=[('files', ('a.jpg', heic(), 'image/jpeg'))])
        self.assertEqual(disguised.status_code, 422)


class ChatServerTests(PanelCase):
    def test_each_photo_is_validated_and_written_once(self):
        photos = [('files', ('arriba.png', picture(color='red'), 'image/png')),
                  ('files', ('abajo.png', picture(color='blue'), 'image/png'))]
        with patch('facturador.chat.write_original', wraps=chat_module.write_original) as write, \
                patch('facturador.chat.validate_document', wraps=chat_module.validate_document) as validate:
            reply = self.client.post('/api/chat', headers=self.headers,
                                     data={'text': '', 'client_id': 'once_12345678901234567'}, files=photos)
        self.assertEqual(reply.status_code, 200, reply.text)
        self.assertEqual(write.call_count, 2)
        self.assertEqual(validate.call_count, 2)
        written = sorted(str(call.args[0]) for call in write.call_args_list)
        jobs = self.store.list()
        self.assertEqual(sorted(row['source_path'] for row in jobs), written)
        self.assertFalse(any((self.root / 'chat_uploads').iterdir()))

    def test_previews_are_small_and_cacheable(self):
        reply = self.client.post('/api/chat', headers=self.headers,
                                 data={'text': 'Esta foto es solo un ejemplo; no la factures.', 'client_id': 'thumb_1234567890123456'},
                                 files=[('files', ('ticket.png', picture((1200, 1600), 'green'), 'image/png'))])
        url = reply.json()['messages'][0]['files'][0]['url']
        preview = self.client.get(url, params={'w': 320})
        self.assertEqual(preview.status_code, 200)
        self.assertEqual(preview.headers['content-type'], 'image/jpeg')
        self.assertEqual(preview.headers['cache-control'], 'private, max-age=31536000, immutable')
        self.assertEqual(preview.headers['x-content-type-options'], 'nosniff')
        with Image.open(io.BytesIO(preview.content)) as small:
            self.assertEqual(small.size, (320, 427))
        original = self.client.get(url)
        self.assertEqual(original.headers['cache-control'], 'no-store')
        self.assertEqual(len(original.content), len(picture((1200, 1600), 'green')))
        self.assertEqual(self.client.get(url, params={'w': 10}).status_code, 422)

    def test_classification_does_not_hold_the_chat_lock(self):
        gate, entered = threading.Event(), threading.Event()

        def slow(*_):
            entered.set()
            gate.wait(10)
            return None

        async def scenario():
            transport = httpx.ASGITransport(app=self.app)
            async with httpx.AsyncClient(transport=transport, base_url='http://testserver') as client:
                first = asyncio.create_task(client.post('/api/chat', headers=self.headers,
                                                        data={'text': '¿Cómo va mi ticket?', 'client_id': 'slow_123456789012345678'}))
                self.assertTrue(await asyncio.to_thread(entered.wait, 10))
                second = await asyncio.wait_for(client.post('/api/chat', headers=self.headers, data={
                    'text': 'Esta foto es solo un ejemplo; no la factures.', 'client_id': 'fast_123456789012345678'}), 10)
                self.assertEqual(second.status_code, 200, second.text)
                self.assertFalse(first.done())
                gate.set()
                self.assertEqual((await first).status_code, 200)

        with patch('facturador.chat._model_decision', slow):
            asyncio.run(scenario())

    def test_chat_questions_use_low_effort(self):
        plan = Mock()
        plan.status.return_value = {'status': 'ready'}
        plan.infer.return_value = chat_module.ChatDecision(intent='conversation', reply='Hola')
        chat_module._model_decision(plan, self.store, 'Hola', False)
        self.assertEqual(plan.infer.call_args.kwargs, {'effort': 'low'})
        plan.infer.return_value = chat_module.ChatAnswer(reply='Se ve un ticket.')
        source = self.root / 'ejemplo.png'
        source.write_bytes(picture())
        with patch('facturador.chat.read_receipt', return_value=[]):
            self.assertEqual(chat_module._example_answer(plan, '¿Qué ves?', [source]), 'Se ve un ticket.')
        self.assertEqual(plan.infer.call_args.kwargs, {'effort': 'low'})


class StaticTests(PanelCase):
    def test_page_references_versioned_files_and_only_those_are_immutable(self):
        page = self.client.get('/')
        self.assertEqual(page.headers['cache-control'], 'no-cache')
        self.assertIn("default-src 'self'", page.headers['content-security-policy'])
        script = re.search(r'src="(/app\.js\?v=[0-9a-f]{16})"', page.text).group(1)
        style = re.search(r'href="(/style\.css\?v=[0-9a-f]{16})"', page.text).group(1)
        self.assertIn('rel="modulepreload" href="/visor.js?v=', page.text)
        self.assertEqual(self.client.get('/', headers={'if-none-match': page.headers['etag']}).status_code, 304)
        for url in (script, style):
            versioned = self.client.get(url)
            self.assertEqual(versioned.status_code, 200)
            self.assertEqual(versioned.headers['cache-control'], IMMUTABLE)
            self.assertEqual(versioned.headers['x-content-type-options'], 'nosniff')
        code = self.client.get(script).text
        self.assertRegex(code, r"from '\./visor\.js\?v=[0-9a-f]{16}'")
        child = re.search(r"'(\./visor\.js\?v=[0-9a-f]{16})'", code).group(1)
        self.assertEqual(self.client.get(child[1:]).headers['cache-control'], IMMUTABLE)
        plain = self.client.get('/app.js')
        self.assertEqual(plain.headers['cache-control'], 'no-cache')
        self.assertEqual(self.client.get('/app.js', headers={'if-none-match': plain.headers['etag']}).status_code, 304)
        self.assertEqual(self.client.get('/app.js?v=antigua').headers['cache-control'], 'no-cache')

    def test_text_responses_are_compressed_and_the_api_stays_private(self):
        page = self.client.get('/')
        script = re.search(r'src="(/app\.js\?v=[0-9a-f]{16})"', page.text).group(1)
        style = re.search(r'href="(/style\.css\?v=[0-9a-f]{16})"', page.text).group(1)
        for url in ('/', script, style, '/api/catalog'):
            response = self.client.get(url, headers={'accept-encoding': 'gzip'})
            self.assertEqual(response.headers.get('content-encoding'), 'gzip', url)
        self.add_jobs(5)
        api = self.client.get('/api/state', headers={'accept-encoding': 'gzip'})
        self.assertEqual(api.headers.get('content-encoding'), 'gzip')
        for response in (api, self.client.get('/api/catalog'), self.client.get('/api/jobs')):
            self.assertEqual(response.headers['cache-control'], 'no-store')
            self.assertEqual(response.headers['x-content-type-options'], 'nosniff')
            self.assertEqual(response.headers['referrer-policy'], 'no-referrer')
            self.assertEqual(response.headers['cross-origin-resource-policy'], 'same-origin')
            self.assertIn("frame-ancestors 'none'", response.headers['content-security-policy'])
        self.assertEqual(self.client.post('/api/automation', json={'enabled': True}).status_code, 403)
        self.assertEqual(self.client.put('/api/automation', json={'enabled': True},
                                         headers={**self.headers, 'origin': 'https://otro.example'}).status_code, 403)
        self.assertEqual(self.client.get('/api/browser/frame', params={'revision': 0}).status_code, 403)


class FakePage:
    def __init__(self, frame):
        self.frame = frame
        self.url = 'https://portal.example/facturar'
        self.viewport_size = {'width': 1001, 'height': 501}
        self.log = []
        self.mouse = SimpleNamespace(move=self.record('move'), down=self.record('down'), up=self.record('up'),
                                     wheel=self.record('wheel'))
        self.keyboard = SimpleNamespace(insert_text=self.record('text'), press=self.record('key'))
        self.screenshot = AsyncMock(side_effect=lambda **_: self.frame)

    def record(self, name):
        async def action(*args):
            self.log.append((name, *args))
        return action

    def is_closed(self):
        return False


class ViewerTests(unittest.IsolatedAsyncioTestCase):
    async def test_unchanged_frame_is_not_sent_again_and_can_be_narrower(self):
        page = FakePage(picture((1280, 850), 'white', 'JPEG'))
        view = BrowserView()
        await view.show(page, 'Portal')
        frame, digest = await view.frame_update(view.revision)
        self.assertEqual(frame, page.frame)
        same, again = await view.frame_update(view.revision, digest)
        self.assertEqual((same, again), (b'', digest))
        small, _ = await view.frame_update(view.revision, '', 480)
        with Image.open(io.BytesIO(small)) as image:
            self.assertEqual(image.size, (480, 319))
        page.frame = picture((1280, 850), 'black', 'JPEG')
        changed, other = await view.frame_update(view.revision, digest)
        self.assertEqual(changed, page.frame)
        self.assertNotEqual(other, digest)
        self.assertEqual(await view.frame_update(view.revision + 1, digest), (None, ''))

    async def test_batches_keep_order_and_control_checks(self):
        page = FakePage(b'')
        view = BrowserView()
        await view.show(page, 'Portal', manual=True)
        lease = await view.take_control(view.revision)
        events = [ViewerEvent(kind='down', x=.5, y=.5), ViewerEvent(kind='move', x=1, y=1), ViewerEvent(kind='up', x=1, y=1),
                  ViewerEvent(kind='text', value='ab'), ViewerEvent(kind='key', value='Enter'),
                  ViewerEvent(kind='key', value='Alt+F4')]
        await view.send_batch(ViewerBatch(channel=lease, events=events))
        self.assertEqual(page.log, [('move', 500.0, 250.0), ('down',), ('move', 1000.0, 500.0), ('move', 1000.0, 500.0), ('up',),
                                    ('text', 'ab'), ('key', 'Enter')])
        with self.assertRaises(ValueError):
            await view.send_batch(ViewerBatch(channel='otro', events=events[:1]))
        # A native dialog that appears in the middle of a batch stops the remaining events.
        page.log.clear()
        original = page.mouse.down

        async def opens_dialog():
            await original()
            view.dialog = SimpleNamespace(type='alert', message='Aviso')
        page.mouse.down = opens_dialog
        with self.assertRaises(ValueError):
            await view.send_batch(ViewerBatch(channel=lease, events=events))
        self.assertEqual(page.log, [('move', 500.0, 250.0), ('down',)])
        view.dialog = None
        await view.set_manual(False)
        with self.assertRaises(ValueError):
            await view.send_batch(ViewerBatch(channel=lease, events=events[3:4]))


class ViewerRouteTests(PanelCase):
    def test_frame_route_answers_204_for_the_same_frame_and_scales_on_request(self):
        view = self.app.state.runner.viewer
        page = FakePage(picture((1280, 850), 'white', 'JPEG'))
        view.page, view.visible, view.revision = page, True, 7
        first = self.client.get('/api/browser/frame', params={'revision': 7}, headers=self.headers)
        self.assertEqual(first.status_code, 200)
        self.assertEqual(first.content, page.frame)
        self.assertNotIn('content-encoding', first.headers)
        digest = first.headers['x-frame-hash']
        same = self.client.get('/api/browser/frame', params={'revision': 7, 'have': digest}, headers=self.headers)
        self.assertEqual((same.status_code, same.content, same.headers['x-frame-hash']), (204, b'', digest))
        small = self.client.get('/api/browser/frame', params={'revision': 7, 'w': 640}, headers=self.headers)
        with Image.open(io.BytesIO(small.content)) as image:
            self.assertEqual(image.width, 640)
        self.assertEqual(self.client.get('/api/browser/frame', params={'revision': 7, 'w': 20}, headers=self.headers).status_code, 422)

    def test_input_route_accepts_single_events_and_batches(self):
        view = self.app.state.runner.viewer
        page = FakePage(b'')
        view.page, view.visible, view.manual, view.revision, view.channel = page, True, True, 3, 'canal-de-prueba'
        single = self.client.post('/api/browser/input', headers=self.headers,
                                  json={'channel': 'canal-de-prueba', 'kind': 'text', 'value': 'x'})
        self.assertEqual(single.json(), {'sent': True})
        batch = self.client.post('/api/browser/input', headers=self.headers, json={
            'channel': 'canal-de-prueba', 'events': [{'kind': 'text', 'value': 'y'}, {'kind': 'key', 'value': 'Tab'}]})
        self.assertEqual(batch.json(), {'sent': True, 'count': 2})
        self.assertEqual(page.log, [('text', 'x'), ('text', 'y'), ('key', 'Tab')])
        too_many = self.client.post('/api/browser/input', headers=self.headers, json={
            'channel': 'canal-de-prueba', 'events': [{'kind': 'text', 'value': 'z'}] * 41})
        self.assertEqual(too_many.status_code, 422)
        wrong = self.client.post('/api/browser/input', headers=self.headers, json={
            'channel': 'otro', 'events': [{'kind': 'text', 'value': 'z'}]})
        self.assertEqual(wrong.status_code, 422)
        self.assertEqual(len(page.log), 3)


if __name__ == '__main__':
    unittest.main()
