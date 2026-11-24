"""A read-only automatic reobservation must not impersonate operator approval."""
import asyncio
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from facturador.db import Store
from facturador.models import Action, Target
from facturador.runner import Runner, action_risk, press_without_target


class Page:
    url = 'https://facturacion-clientes.walmart.com/address'
    frames = []

    def is_closed(self):
        return False

    def locator(self, selector):
        return self

    async def aria_snapshot(self, **_kwargs):
        return '- textbox "Razón social"\n- combobox "Uso Factura"'

    async def count(self):
        return 0


class AutomaticReobservationTests(unittest.IsolatedAsyncioTestCase):
    def test_unbound_press_is_rejected_before_execution(self):
        unbound = Action(kind='press', target=None, value='Enter', reason='Avanzar', risk='normal')
        ambiguous = Action(kind='press', target=Target(by='role', role='textbox', name='Código postal', exact=True),
                           value='Enter', reason='Avanzar', risk='normal')
        self.assertTrue(press_without_target(unbound))
        self.assertFalse(press_without_target(ambiguous))
        self.assertEqual(action_risk(ambiguous), 'unknown')

    async def test_untouched_form_reobservation_does_not_claim_operator_approval(self):
        with tempfile.TemporaryDirectory() as directory:
            runner = Runner(Store(Path(directory)))
            self.addCleanup(runner.plan.close)
            page = Page()
            runner.viewer.page = page
            runner.viewer.manual = True
            key = 'synthetic-walmart-ticket'
            pending = {'page': page, 'future': asyncio.get_running_loop().create_future(),
                       'control_epoch': runner.viewer.control_epoch, 'url': page.url}
            runner.pending[key] = pending
            row = {'status': 'needs_user', 'issue_attempted': False, 'issuance_seen': False, 'uuid': ''}
            with (patch('facturador.runner.REOBSERVE_DELAY', 0),
                  patch.object(runner.store, 'get', return_value=row),
                  patch.object(runner.store, 'event') as event):
                await runner.reobserve_untouched_form(key, pending, '- heading "Cargando"', set())
            self.assertEqual(pending['future'].result(), 'reobserve')
            self.assertNotIn(key, runner.explicit_approval)
            self.assertFalse(runner.viewer.manual)
            event.assert_called_once()

    async def test_operator_control_change_prevents_automatic_reobservation(self):
        with tempfile.TemporaryDirectory() as directory:
            runner = Runner(Store(Path(directory)))
            self.addCleanup(runner.plan.close)
            page = Page()
            runner.viewer.page = page
            runner.viewer.manual = True
            key = 'synthetic-walmart-ticket'
            pending = {'page': page, 'future': asyncio.get_running_loop().create_future(),
                       'control_epoch': runner.viewer.control_epoch, 'url': page.url}
            runner.pending[key] = pending
            runner.viewer.control_epoch += 1
            with (patch('facturador.runner.REOBSERVE_DELAY', 0),
                  patch.object(runner.store, 'event') as event):
                await runner.reobserve_untouched_form(key, pending, '- heading "Cargando"', set())
            self.assertFalse(pending['future'].done())
            self.assertTrue(runner.viewer.manual)
            event.assert_not_called()

    async def test_resume_without_operator_control_does_not_claim_approval(self):
        with tempfile.TemporaryDirectory() as directory:
            runner = Runner(Store(Path(directory)))
            self.addCleanup(runner.plan.close)
            page = Page()
            runner.viewer.page = page
            runner.viewer.manual = True
            key = 'synthetic-walmart-ticket'
            pending = {'kind': 'blocked', 'page': page, 'future': asyncio.get_running_loop().create_future(),
                       'control_epoch': runner.viewer.control_epoch, 'url': page.url}
            runner.pending[key] = pending
            row = {'status': 'needs_user', 'message': 'Pausa de prueba', 'historical': False}
            with (patch.object(runner.store, 'get', return_value=row),
                  patch.object(runner.store, 'pause_automatic_recovery'),
                  patch.object(runner, 'retry_action', return_value='resume')):
                result = await runner.retry_job(key)
            self.assertEqual(result['action'], 'resumed')
            self.assertEqual(pending['future'].result(), 'reobserve')
            self.assertNotIn(key, runner.explicit_approval)
