import tempfile
import unittest
from pathlib import Path
from unittest.mock import AsyncMock, Mock, patch

from facturador.db import Store
from facturador.personal_browser import PersonalBrowser
from facturador.runner import Runner
from facturador.viewer import ViewerInput


def page(url):
    return Mock(url=url, is_closed=Mock(return_value=False), goto=AsyncMock(),
                bring_to_front=AsyncMock(), reload=AsyncMock(), keyboard=Mock(insert_text=AsyncMock()))


class PersonalBrowserTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        folder = tempfile.TemporaryDirectory()
        self.addCleanup(folder.cleanup)
        self.runner = Runner(Store(Path(folder.name)))
        self.addCleanup(self.runner.plan.close)
        self.blank = page('about:blank')
        self.context = Mock(pages=[self.blank], route=AsyncMock(), route_web_socket=AsyncMock())
        self.launch = AsyncMock(return_value=self.context)
        self.runner.engine = 'Google Chrome'
        self.runner.playwright = Mock(chromium=Mock(launch_persistent_context=self.launch))
        self.runner.start_browser = AsyncMock()

    def tearDown(self):
        for event, callback in (registration.args for registration in self.context.on.call_args_list):
            if event == 'close':
                callback(self.context)

    async def test_blank_home_persists_its_own_profile_and_leaves_worker_unlocked(self):
        state = await self.runner.open_personal()
        self.assertEqual(state['mode'], 'personal')
        self.assertEqual(state['tabs'][0]['title'], 'Nueva pestaña')
        self.assertEqual(self.launch.await_args.args, (self.runner.store.root / 'browser_personal_chrome',))
        self.assertFalse(self.launch.await_args.kwargs['headless'])
        self.blank.goto.assert_not_awaited()
        lease = await self.runner.screen.take_control(state['revision'])
        self.assertTrue(lease)
        self.assertFalse(self.runner.viewer.channel)
        self.assertIsNone(self.runner.viewer.page)
        with self.assertRaisesRegex(ValueError, 'otra ventana'):
            await self.runner.open_personal(channel='other')

    async def test_return_to_same_fiscal_page_and_lease_isolation(self):
        billing = page('https://tienda.example.com/')
        self.runner.active_id = 'ticket'
        self.runner.page_job[billing] = 'ticket'
        await self.runner.viewer.show(billing, 'Comercio de prueba', manual=True)
        state = await self.runner.open_personal()
        lease = await self.runner.screen.take_control(state['revision'])
        self.assertEqual(state['return_job_id'], 'ticket')
        self.assertIs(self.runner.viewer.page, billing)
        state = await self.runner.return_to_job(lease)
        self.assertEqual((state['job_id'], state['mode']), ('ticket', 'job'))
        with self.assertRaises(ValueError):
            await self.runner.screen.send(ViewerInput(channel=lease, kind='text', value='ejemplo'))
        billing.keyboard.insert_text.assert_not_awaited()
        await self.runner.open_personal()
        self.assertIs(self.runner.screen.page, self.blank)

    async def test_manual_navigation_does_not_export_query_or_send_to_work_context(self):
        await self.runner.open_personal()
        dest = 'https://consulta.example/buscar?test=dummy'
        with patch('facturador.personal_browser.resolves_publicly', new=AsyncMock(return_value=True)):
            await self.runner.open_personal(dest)
        self.assertEqual(self.blank.goto.await_args.args[0], dest)
        self.blank.url = dest
        self.assertNotIn('dummy', str(self.runner.browser_state()))
        with self.assertRaises(ValueError):
            await self.runner.open_personal('http://localhost/')
        self.assertEqual(self.blank.goto.await_count, 1)
        await self.runner.screen.history('', 'reload', personal=True)
        self.blank.reload.assert_awaited_once()

    async def test_personal_input_survives_a_new_job_until_lease_is_released(self):
        state = await self.runner.open_personal()
        channel = await self.runner.screen.take_control(state['revision'])
        billing = page('https://tienda.example.com/')
        with patch.object(self.runner.store, 'queue', return_value=True), patch.object(self.runner, 'run', new=AsyncMock()):
            self.runner.schedule('ticket', Mock())
            await self.runner.job_tasks['ticket']
        self.runner.active_id = 'ticket'
        self.runner.page_job[billing] = 'ticket'
        await self.runner.viewer.show(billing, 'Comercio')
        self.assertEqual(self.runner.browser_state()['return_job_id'], 'ticket')
        await self.runner.screen.send(ViewerInput(channel=channel, kind='text', value='prueba'))
        self.blank.keyboard.insert_text.assert_awaited_once_with('prueba')
        billing.keyboard.insert_text.assert_not_awaited()
        await self.runner.personal.view.release(channel)
        self.assertIs(self.runner.screen.page, billing)

    async def test_guards_public_https_resources_and_secure_websockets(self):
        route = Mock(request=Mock(url='https://consulta.example/x?' + 'z'*3000),
                     continue_=AsyncMock(), abort=AsyncMock())
        with patch('facturador.personal_browser.resolves_publicly', new=AsyncMock(return_value=True)) as resolve:
            await PersonalBrowser.guard_request(route)
        resolve.assert_awaited_once_with('https://consulta.example')
        route.continue_.assert_awaited_once()
        with patch('facturador.personal_browser.resolves_publicly', new=AsyncMock(return_value=False)):
            await PersonalBrowser.guard_request(route)
            socket = Mock(url='wss://blocked.example.com/data', close=AsyncMock())
            await PersonalBrowser.guard_websocket(socket)
        route.abort.assert_awaited_once()
        socket.close.assert_awaited_once()
        socket.connect_to_server.assert_not_called()

    async def test_google_sign_in_document_stays_outside_the_remote_browser(self):
        route = Mock(request=Mock(resource_type='document', url='https://accounts.google.com/o/oauth2/auth?state=prueba'),
                     continue_=AsyncMock(), fulfill=AsyncMock())
        await PersonalBrowser.guard_request(route)
        route.continue_.assert_not_awaited()
        route.fulfill.assert_awaited_once()
        self.assertEqual(route.fulfill.await_args.kwargs['status'], 403)
        self.assertNotIn('state=',route.fulfill.await_args.kwargs['body'])
