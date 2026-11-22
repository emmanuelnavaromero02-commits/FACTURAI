import asyncio
import tempfile
import unittest
from pathlib import Path
from unittest.mock import AsyncMock, Mock, patch

from facturador.account_flow import PortalAccountFlow
from facturador.db import Store
from facturador.runner import Runner
from test_core import FakeAI, FakeContext, FakeLocator, FakePage, PROFILE, RECEIPT


class EchoPage(FakePage):
    secret = 'prueba-privada-876543'

    def __init__(self, after_click='', change_during_capture=False):
        super().__init__()
        self.after_click = after_click
        self.change_during_capture = change_during_capture
        self.captures = 0

    async def screenshot(self, **kwargs):
        self.captures += 1
        if self.change_during_capture:
            self.body = 'Código de acceso: ' + self.secret
        return self.body.encode()

    def get_by_role(self, *args, **kwargs):
        class Button(FakeLocator):
            async def click(self, *, trial=False, **_):
                if not trial:
                    self.page.clicks += 1
                    self.page.body = self.page.after_click + ' ' + self.page.secret
        return Button(self)


class PrivateObservationTests(unittest.IsolatedAsyncioTestCase):
    async def examine(self, page, private_value_known):
        with tempfile.TemporaryDirectory() as folder:
            store = Store(Path(folder))
            row, _ = store.add('ejemplo.png', 'observacion')
            key = row['id']
            store.save_receipt(key, RECEIPT, ready=True)
            runner = Runner(store)
            runner.context = FakeContext(page)
            account = PortalAccountFlow(store.root, page.url, PROFILE, key)
            if private_value_known:
                account.credential_values.add(page.secret)
            model = Mock(wraps=FakeAI())
            try:
                with patch('facturador.runner.AI', return_value=model), \
                        patch('facturador.runner.PortalAccountFlow', return_value=account), \
                        patch.object(account, 'step', new=AsyncMock(wraps=account.step)) as steps:
                    runner.schedule(key, PROFILE)
                    task = runner.job_tasks[key]
                    async with asyncio.timeout(5):
                        while key not in runner.pending:
                            await asyncio.sleep(.01)
                        if runner.pending[key]['kind'] == 'issue':
                            runner.decide(key, 'approve')
                            while runner.pending.get(key, {}).get('kind') != 'login':
                                await asyncio.sleep(.01)
                    self.assertEqual(runner.pending[key]['kind'], 'login')
                    self.assertNotIn(page.secret, store.get(key)['message'])
                    self.assertNotIn(page.secret, str(model.next_action.call_args_list))
                    self.assertEqual(steps.await_count, 1 + int(page.change_during_capture))
                    await runner.stop(key)
                    await asyncio.gather(task, return_exceptions=True)
                return model.next_action.call_args_list
            finally:
                await runner.close()

    async def test_commit_and_recovery_do_not_share_echoed_credentials(self):
        for status in ['Esperando respuesta', 'Factura emitida']:
            with self.subTest(status=status):
                page = EchoPage(after_click=status)
                decisions = await self.examine(page, True)
                self.assertEqual(len(decisions), 1)
                self.assertIn('Formulario listo', decisions[0].args[1])
                self.assertIsNone(decisions[0].args[2])
                self.assertEqual(page.clicks, 1)
                self.assertEqual(page.captures, 0)

    async def test_discard_image_and_text_when_challenge_appears_during_capture(self):
        page = EchoPage(change_during_capture=True)
        self.assertEqual(await self.examine(page, False), [])
        self.assertEqual(page.captures, 1)
        self.assertEqual(page.clicks, 0)
