import asyncio
import tempfile
import unittest
from pathlib import Path
from unittest.mock import AsyncMock, Mock, patch

from facturador.captcha_transport import widget_origin
from facturador.catalog import CUSTOM_PORTAL
from facturador.db import Store
from facturador.runner import Runner


class WidgetRoutes(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.runner = Runner(Store(Path(self.enterContext(tempfile.TemporaryDirectory()))))
        self.addCleanup(self.runner.plan.close)
        self.merchant = 'https://comercio.example.com'
        self.origin = 'https://www.google.com'
        self.page = Mock(url=self.merchant+'/ticket', is_closed=Mock(return_value=False))
        self.frame = Mock(url=self.origin+'/recaptcha/widget', parent_frame=self.page.main_frame, page=self.page,
                          frame_element=AsyncMock(return_value=Mock(is_visible=AsyncMock(return_value=True))))
        self.session = Mock(send=AsyncMock())
        self.page.context.new_cdp_session = AsyncMock(return_value=self.session)
        self.key = self.runner.store.add('prueba', 'sintetico')[0]['id']
        self.runner.store.update(self.key, receipt={'provider':CUSTOM_PORTAL,'portal_url':self.page.url})
        self.runner.active_id = self.key
        self.runner.page_job[self.page] = self.key
        self.runner.page_portal_origins[self.page] = self.merchant
        await self.runner.viewer.show(self.page,'Prueba',manual=True)
        self.pause = {'kind':'captcha','page':self.page,'url':self.page.url,'future':asyncio.get_running_loop().create_future(),
                      'control_epoch':self.runner.viewer.control_epoch,'challenge_seen':True}
        self.runner.pending[self.key] = self.pause
        await self.runner.widgets.arm(self.frame)

    async def run_request(self, expected, url=None, frame=None, **properties):
        fields = {'url':url or self.origin+'/recaptcha/widget-response','method':'POST','frame':frame or self.frame,
                  'resource_type':'xhr','is_navigation_request':Mock(return_value=False),'redirected_from':None,
                  'header_value':AsyncMock(return_value=self.origin)}
        fields.update(properties)
        route = Mock(request=Mock(**fields), abort=AsyncMock(), continue_=AsyncMock())
        with patch('facturador.runner.resolves_publicly', new=AsyncMock(return_value=True)):
            await self.runner.guard_navigation(route)
        if expected:
            route.continue_.assert_awaited_once_with()
            route.abort.assert_not_awaited()
        else:
            route.abort.assert_awaited_once()
            route.continue_.assert_not_awaited()

    async def test_only_inside_verified_widget_during_real_lease(self):
        await self.run_request(False)
        lease = await self.runner.viewer.take_control(self.runner.viewer.revision)
        await self.run_request(True)
        await self.run_request(False, url='https://recaptcha.google.com/recaptcha/widget-response')
        await self.run_request(False, url=self.origin+'/otro-servicio')
        await self.run_request(False, method='PUT')
        await self.run_request(False, redirected_from=Mock())
        await self.run_request(False, header_value=AsyncMock(return_value=self.merchant))
        await self.run_request(False, is_navigation_request=Mock(return_value=True))
        self.page.main_frame.page = self.page
        await self.run_request(False, frame=self.page.main_frame)
        self.frame.parent_frame = Mock(url='https://publicidad.example.org/')
        await self.run_request(False)
        self.frame.parent_frame = self.page.main_frame
        self.pause['kind'] = 'login'
        await self.run_request(False)
        self.pause['kind'] = 'captcha'
        self.pause['challenge_seen'] = False
        await self.run_request(False)
        self.pause['challenge_seen'] = True
        await self.runner.viewer.release(lease)
        await self.run_request(False)

    async def test_network_target_checked_before_lease_and_after_visibility_probe(self):
        lease = await self.runner.viewer.take_control(self.runner.viewer.revision)
        async def finish_while_checking():
            await self.runner.viewer.release(lease)
            return True
        self.frame.frame_element.return_value.is_visible.side_effect = finish_while_checking
        await self.run_request(False)
        await self.runner.viewer.take_control(self.runner.viewer.revision)
        self.frame.frame_element.return_value.is_visible.side_effect = None
        self.frame.frame_element.return_value.is_visible.return_value = False
        await self.run_request(False)
        self.frame.frame_element.return_value.is_visible.return_value = True
        self.pause['future'].set_result('resume')
        await self.run_request(False)

    def test_provider_paths_do_not_include_neighbors_or_custom_ports(self):
        for url in ('http://www.google.com/recaptcha/widget','https://recaptcha.ejemplo.org/recaptcha/widget',
                    'https://www.google.com:8443/recaptcha/widget','https://www.google.com/signin',
                    'https://newassets.hcaptcha.com.otro.org/captcha/widget'):
            self.assertIsNone(widget_origin(url))
        for url in ('https://recaptcha.google.com/recaptcha/widget','https://challenges.cloudflare.com/cdn-cgi/challenge-platform/test',
                    'https://newassets.hcaptcha.com/captcha/test'):
            self.assertIsNotNone(widget_origin(url))
