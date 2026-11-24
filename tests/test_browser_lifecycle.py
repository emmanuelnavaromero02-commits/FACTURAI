"""The live browser belongs to a running ticket, not to the idle inbox."""

import tempfile
import unittest
from pathlib import Path
from unittest.mock import AsyncMock, Mock

from facturador.db import Store
from facturador.runner import Runner


class FakePage:
    url = 'https://merchant.example/ticket?secret=hidden'

    def __init__(self):
        self.closed = False
        self.screenshot = AsyncMock(return_value=b'frame')

    def is_closed(self):
        return self.closed

    async def close(self):
        self.closed = True


class BrowserLifecycleTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.store = Store(Path(temporary.name))
        self.runner = Runner(self.store, Mock())
        self.key = self.store.add('synthetic.png', 'browser-lifecycle')[0]['id']
        self.page = FakePage()
        self.runner.page_job[self.page] = self.key
        await self.runner.viewer.show(self.page, 'Comercio de prueba')

    async def test_blocked_ticket_closes_page_and_live_frame(self):
        self.store.update(self.key, status='blocked', message='Se detuvo el trámite.')
        revision = self.runner.viewer.revision
        assert self.runner.browser_state()['mode'] == 'job'
        await self.runner.finish_job_browser(self.key)
        state = self.runner.browser_state()
        self.assertEqual(state['mode'], 'idle')
        self.assertFalse(state['open'])
        self.assertEqual(state['url'], '')
        self.assertIsNone(state['job_id'])
        self.assertTrue(self.page.closed)
        self.assertIsNone(await self.runner.viewer.frame(revision))
        self.assertIn('browser_closed', [event['kind'] for event in self.store.events(self.key)])

    async def test_uncertain_ticket_hides_frame_but_keeps_page_for_read_only_recovery(self):
        self.store.update(self.key, status='uncertain', message='Consulta pendiente.')
        revision = self.runner.viewer.revision
        await self.runner.finish_job_browser(self.key)
        self.assertFalse(self.runner.browser_state()['open'])
        self.assertEqual(self.runner.browser_state()['mode'], 'idle')
        self.assertIsNone(await self.runner.viewer.frame(revision))
        self.assertFalse(self.page.closed)
        self.assertEqual(self.runner.page_job[self.page], self.key)

    async def test_running_ticket_stays_visible_until_worker_finishes(self):
        self.store.update(self.key, status='needs_user', message='Verificación visible.')
        self.assertEqual(self.runner.browser_state()['mode'], 'job')
        self.assertTrue(self.runner.browser_state()['open'])
        self.assertEqual(await self.runner.viewer.frame(self.runner.viewer.revision), b'frame')

    async def test_next_ticket_closes_parked_recovery_page(self):
        self.store.update(self.key, status='uncertain', message='Consulta pendiente.')
        await self.runner.finish_job_browser(self.key)
        next_key = self.store.add('next.png', 'next-browser-lifecycle')[0]['id']
        await self.runner.close_parked_job_pages(next_key)
        self.assertTrue(self.page.closed)
        self.assertNotIn(self.page, self.runner.page_job)


if __name__ == '__main__':
    unittest.main()
