"""The limit of open browsers with real browsers: a place is handed over by closing a real one, and it opens again."""
import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from acceso_dobles import ORIGIN, FakeClock, quiet_app, use_master_key
from facturador.instancias import BUSY, BrowserPool, Instances
from test_acceso_paginas import EXECUTABLE

ANA, BETO = 'a' * 16, 'b' * 16


@unittest.skipUnless(EXECUTABLE, 'Requiere Google Chrome o Chromium')
class RealBrowserLimitTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        folder = tempfile.TemporaryDirectory(ignore_cleanup_errors=True)
        self.addCleanup(folder.cleanup)
        use_master_key(self)
        self.enterContext(patch.dict(os.environ, {'FACTURADOR_CHROMIUM_PATH': EXECUTABLE}))
        os.environ.pop('FACTURADOR_CHROME_HEADED', None)
        self.clock = FakeClock(1000.0)
        self.pool = BrowserPool(1, clock=self.clock, poll=.01)
        self.instances = Instances(Path(folder.name), quiet_app, port=8765, public_origin=ORIGIN, clock=self.clock, pool=self.pool)
        self.addAsyncCleanup(self.instances.close)

    async def use(self, owner, path='/api/state'):
        instance = await self.instances.enter(owner, path)
        self.instances.leave(instance)
        return instance

    async def test_idle_real_browser_is_closed_for_the_next_one_and_can_open_again(self):
        ana, beto = await self.use(ANA, '/api/browser/personal'), await self.use(BETO)
        self.assertFalse(ana.browser_open)  # Nothing was launched when the instances started.
        state = await ana.runner.open_personal()  # The person opens «Navegación libre»: two real profiles.
        self.assertEqual((state['mode'], state['open']), ('personal', True))
        context, personal, driver = ana.runner.context, ana.runner.personal.context, ana.runner.playwright
        self.assertIsNotNone(context)
        self.assertIsNotNone(personal)
        self.assertTrue((ana.root / 'browser_profile').is_dir())
        with self.assertRaises(ValueError) as caught:
            await beto.runner.start_browser()
        self.assertEqual(str(caught.exception), BUSY)
        self.assertIsNone(beto.runner.context)
        self.assertFalse((beto.root / 'browser_profile').exists())

        self.clock.advance(61)
        await beto.runner.start_browser()  # Ana's browser was idle: it is closed and Beto's opens.
        self.assertIsNone(ana.runner.context)
        self.assertIsNone(ana.runner.personal.context)
        self.assertIsNone(ana.runner.playwright)
        self.assertFalse(ana.browser_open)
        self.assertFalse(ana.runner.browser_state()['open'])
        self.assertEqual(list(self.pool.holders), [beto])
        self.assertEqual(beto.runner.engine, 'Chromium')
        page = await beto.runner.context.new_page()
        await page.set_content('<p>Beto</p>')
        self.assertEqual(await page.inner_text('p'), 'Beto')
        with self.assertRaises(Exception):
            await context.new_page()  # Ana's real browser is gone, not just forgotten.
        self.assertIsNot(beto.runner.playwright, driver)

        await page.close()
        await ana.runner.start_browser()  # Later Ana opens hers again on the same saved profile.
        self.assertIsNotNone(ana.runner.context)
        self.assertIsNot(ana.runner.context, context)
        self.assertIsNone(beto.runner.context)
        self.assertEqual(list(self.pool.holders), [ana])
        again = await ana.runner.context.new_page()
        await again.set_content('<p>Ana</p>')
        self.assertEqual(await again.inner_text('p'), 'Ana')


if __name__ == '__main__':
    unittest.main()
