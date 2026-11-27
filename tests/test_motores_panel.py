"""The panel in a real browser with several tickets at once: the picker of what the viewer shows.

The application is the real one, served on a local port with three engines. What a ticket does
inside its portal is a stand-in that stays there until the test lets it go, so no portal, no
model and no second browser is involved: only the panel's own page is opened.
"""
import asyncio
import os
import sys
import tempfile
import threading
import time
import unittest
from pathlib import Path
from unittest.mock import patch

import httpx
import uvicorn
from playwright.async_api import async_playwright, expect

sys.path.insert(0, str(Path(__file__).resolve().parent))

from facturador.app import create_app
from facturador.auth import write_private
from facturador.catalog import CUSTOM_PORTAL
from facturador.models import Receipt
from facturador.motores import SETTING
from facturador.runner import Runner
from test_core import PROFILE
from test_panel_browser import EXECUTABLE, free_port

# The panel asks for its state every few seconds while nothing moves: what it shows may take that long to change.
POLL = 15_000


class PanelCase(unittest.IsolatedAsyncioTestCase):
    engines_wanted = '3'

    async def asyncSetUp(self):
        folder = tempfile.TemporaryDirectory()
        self.addCleanup(folder.cleanup)
        self.root = Path(folder.name)
        self.port = free_port()
        with patch.dict(os.environ, {SETTING: self.engines_wanted, 'FACTURADOR_MODO': 'local'}):
            self.app = create_app(self.root, port=self.port)
        self.store, self.engines = self.app.state.store, self.app.state.engines
        write_private(self.root / 'profile.json', PROFILE.model_dump())
        self.asks_person: set[str] = set()
        self.done: set[str] = set()
        case = self

        async def in_portal(runner, key, profile, auto_issue=False, manual=False):
            """What Runner._run keeps of its ticket: the engine's lock, the single claim and the active ticket."""
            async with runner.lock:
                if not runner.store.claim(key):
                    return
                runner.active_id = key
                try:
                    if key in case.asks_person:
                        future = asyncio.get_running_loop().create_future()
                        runner.pending[key] = {'kind': 'issue', 'future': future, 'page': None, 'url': '',
                                               'control_epoch': runner.viewer.control_epoch, 'challenge_seen': False}
                        runner.store.enter_intervention(key, 'issue', 'Revisa y aprueba la emisión.', False)
                        try:
                            await future
                        finally:
                            runner.pending.pop(key, None)
                            runner.store.resume_intervention(key)
                    while key not in case.done:
                        await asyncio.sleep(0.02)
                    runner.store.update(key, status='blocked', message='Fin de la prueba.')
                finally:
                    runner.active_id = None

        for patcher in (patch.object(Runner, '_run', in_portal),
                        patch.object(self.app.state.automation, 'start', lambda: None),
                        patch.object(self.app.state.plan, 'status', return_value={'status': 'ready', 'message': ''})):
            patcher.start()
            self.addCleanup(patcher.stop)
        self.server = uvicorn.Server(uvicorn.Config(self.app, host='127.0.0.1', port=self.port, log_level='warning'))
        thread = threading.Thread(target=self.server.run, daemon=True)
        thread.start()
        deadline = time.monotonic() + 15
        while not self.server.started and time.monotonic() < deadline:
            await asyncio.sleep(.05)
        self.assertTrue(self.server.started)

        async def stop():
            self.done.update(row['id'] for row in self.store.list())
            self.server.should_exit = True
            await asyncio.to_thread(thread.join, 15)
        self.addAsyncCleanup(stop)
        origin = f'http://127.0.0.1:{self.port}'
        self.api = httpx.AsyncClient(base_url=origin, headers={'X-App-Token': self.app.state.csrf_token, 'Origin': origin})
        self.addAsyncCleanup(self.api.aclose)
        self.playwright = await async_playwright().start()
        self.addAsyncCleanup(self.playwright.stop)
        self.browser = await self.playwright.chromium.launch(executable_path=EXECUTABLE, headless=True)
        self.addAsyncCleanup(self.browser.close)
        self.page = await self.browser.new_page()
        self.errors = []
        self.page.on('pageerror', lambda error: self.errors.append(str(error)))

    def ticket(self, merchant: str, folio: str) -> str:
        key = self.store.add(folio + '.png', 'foto-' + folio)[0]['id']
        origin = f'https://facturas.{folio.lower()}-de-prueba.example/'
        receipt = Receipt(provider=CUSTOM_PORTAL, portal_url=origin, merchant=merchant, amount='100.00',
                          date='2026-11-13', ticket_number=folio, payment_code='04')
        self.store.save_receipt(key, receipt, ready=True)
        return key

    async def start(self, key: str) -> None:
        url = self.store.get(key)['receipt']['portal_url']
        reply = await self.api.post(f'/api/jobs/{key}/run', json={'consent': True, 'confirmed_url': url, 'auto_issue': True})
        self.assertEqual(reply.status_code, 200, reply.text)

    async def until(self, condition, seconds=15):
        deadline = time.monotonic() + seconds
        while not condition():
            if time.monotonic() > deadline:
                self.fail('La condición no se cumplió a tiempo.')
            await asyncio.sleep(0.05)

    async def show(self, screen: str):
        """Another screen of the panel already loaded. Loading it on the browser screen would open the personal browser."""
        await self.page.evaluate('screen => { location.hash = screen; }', screen)

    async def open(self, screen: str):
        await self.page.goto(f'http://127.0.0.1:{self.port}/#{screen}')
        await self.page.wait_for_function('document.querySelector("#automation-enabled") && '
                                          '!document.querySelector("#automation-enabled").disabled')


@unittest.skipUnless(EXECUTABLE, 'Requiere Google Chrome o Chromium')
class PickerTests(PanelCase):
    async def test_the_picker_appears_with_two_tickets_and_what_the_person_picks_is_what_is_shown(self):
        page, picker, task = self.page, self.page.locator('#engine-picker'), self.page.locator('#browser-task')
        running = self.ticket('Cafetería de Prueba', 'CAF100')
        waiting = self.ticket('Papelería de Prueba', 'PAP200')
        await self.open('bandeja')
        await expect(picker).to_be_hidden()
        await self.start(running)
        await self.until(lambda: self.engines.active_id == running)
        await self.show('navegador')
        await expect(task).to_contain_text('Cafetería de Prueba', timeout=POLL)
        await expect(picker).to_be_hidden()          # One ticket in a portal: nothing to pick.
        self.asks_person.add(waiting)
        await self.start(waiting)
        await self.until(lambda: self.engines.waiting_kind(waiting) == 'issue')
        # Two in progress: the picker shows both, and the one that waits for the person is in front.
        await expect(picker).to_be_visible(timeout=POLL)
        options = page.locator('#engine-select option')
        await expect(options).to_have_text(['Cafetería de Prueba · CAF100 — en trámite',
                                            'Papelería de Prueba · PAP200 — espera tu ayuda'], timeout=POLL)
        await expect(page.locator('#engine-select')).to_have_value('2', timeout=POLL)
        await expect(task).to_contain_text('Papelería de Prueba', timeout=POLL)
        await expect(task.get_by_role('button', name='Aprobar emisión')).to_be_visible(timeout=POLL)
        # The person picks the other one: its ticket is the current one and the approval is no longer offered.
        await page.locator('#engine-select').select_option('1')
        await expect(task).to_contain_text('Cafetería de Prueba', timeout=POLL)
        await expect(task.get_by_role('button', name='Aprobar emisión')).to_have_count(0)
        self.assertEqual(self.engines.active_id, running)
        await expect(page.locator('#engine-select')).to_have_value('1', timeout=POLL)
        # From the list of tickets, «Ver en vivo» of the one that waits brings it back in front.
        await self.show('bandeja')
        row = page.locator(f'#tickets tr[data-job-id="{waiting}"]')
        await row.get_by_role('button', name='Ver en vivo').click()
        await self.until(lambda: self.engines.active_id == waiting)
        await self.until(lambda: page.url.endswith('#chat'))   # «Ver en vivo» ends in the conversation.
        await self.show('navegador')
        await expect(task).to_contain_text('Papelería de Prueba', timeout=POLL)
        await expect(page.locator('#engine-select')).to_have_value('2', timeout=POLL)
        # Its approval reaches the engine that has it; the other ticket goes on untouched.
        await task.get_by_role('button', name='Aprobar emisión').click()
        await self.until(lambda: self.engines.waiting_kind(waiting) is None)
        self.assertIs(self.engines.owner(running), self.engines.main)
        self.assertEqual(self.store.get(running)['status'], 'running')
        await expect(options).to_have_text(['Cafetería de Prueba · CAF100 — en trámite',
                                            'Papelería de Prueba · PAP200 — en trámite'], timeout=POLL)
        # When only one is left there is nothing to pick again.
        self.done.add(waiting)
        await self.until(lambda: self.engines.owner(waiting) is None)
        await expect(picker).to_be_hidden(timeout=POLL)
        await expect(task).to_contain_text('Cafetería de Prueba', timeout=POLL)
        self.done.add(running)
        await self.until(lambda: not self.engines.has_active_jobs())
        self.assertEqual(self.errors, [])


@unittest.skipUnless(EXECUTABLE, 'Requiere Google Chrome o Chromium')
class NoPickerTests(PanelCase):
    engines_wanted = '1'

    async def test_with_one_engine_the_panel_is_the_same_and_the_picker_never_shows(self):
        self.assertEqual(self.engines.helpers, [])
        first = self.ticket('Cafetería de Prueba', 'CAF100')
        second = self.ticket('Papelería de Prueba', 'PAP200')
        await self.open('bandeja')
        await self.start(first)
        await self.start(second)
        await self.until(lambda: self.engines.active_id == first)
        await self.show('navegador')
        await expect(self.page.locator('#browser-task')).to_contain_text('Cafetería de Prueba', timeout=POLL)
        state = (await self.api.get('/api/state')).json()
        self.assertNotIn('engines', state)
        self.assertEqual(state['active_job'], first)
        self.assertEqual(self.store.get(second)['status'], 'queued')
        await expect(self.page.locator('#engine-picker')).to_be_hidden()
        self.done.update((first, second))
        await self.until(lambda: not self.engines.has_active_jobs())
        self.assertEqual(self.errors, [])


if __name__ == '__main__':
    unittest.main()
