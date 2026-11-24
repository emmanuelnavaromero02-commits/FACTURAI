import asyncio
import tempfile
import unittest
from pathlib import Path
from unittest.mock import AsyncMock, Mock, patch

from fastapi.testclient import TestClient
from playwright.async_api import Error as BrowserError

from facturador.app import create_app
from facturador.db import Store
from facturador.documents import save_candidate
from facturador.models import Action, Target
from facturador.runner import Runner
from facturador.ticket_retry import open_ticket_portal, ticket_in_result
from test_core import FakeContext, FakePage, PROFILE, RECEIPT, FISCAL_UUID, pdf_bytes, xml_bytes


class RetryTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.root = Path(self.enterContext(tempfile.TemporaryDirectory()))
        self.store = Store(self.root)
        self.runner = Runner(self.store)
        self.addAsyncCleanup(self.runner.close)
        row, _ = self.store.add('synthetic.jpg', 'fixture-retry')
        self.key = row['id']
        self.store.save_receipt(self.key, RECEIPT, ready=True)

    async def pending(self, kind):
        async with asyncio.timeout(4):
            while self.runner.pending.get(self.key, {}).get('kind') != kind:
                await asyncio.sleep(.01)
        return self.runner.pending[self.key]

    def model(self):
        return Mock(next_action=Mock(side_effect=[
            Action(kind='human', risk='unknown', reason='Revisión', target=None, value=None),
            Action(kind='click', risk='issue', reason='Emisión', value=None, target=Target(by='role', role='button', name='Facturar', exact=True)),
            Action(kind='done', risk='normal', reason='Fin', target=None, value=None),
        ]))

    async def test_same_tab_reobserved_only_once_and_approval_remains_separate(self):
        page = FakePage()
        self.runner.context = FakeContext(page)
        with patch('facturador.runner.AI', return_value=self.model()):
            self.runner.schedule(self.key, PROFILE, auto_issue=True)
            task = self.runner.job_tasks[self.key]
            await self.pending('unknown')
            self.assertEqual(self.store.get(self.key)['human_exposed'], 0)
            result = await asyncio.gather(self.runner.retry_job(self.key), self.runner.retry_job(self.key))
            self.assertEqual({r['action'] for r in result}, {'active', 'resumed'})
            await self.pending('issue')
            self.assertIs(self.runner.viewer.page, page)
            self.assertEqual(page.clicks, 0)
            self.assertEqual((await self.runner.retry_job(self.key))['action'], 'needs_user')
            await self.runner.continue_job(self.key, 'approve')
            await task
            self.assertEqual(page.clicks, 1)
            self.assertEqual(self.store.get(self.key)['human_exposed'], 0)

    async def test_actual_control_then_retry_recovers_only_even_if_control_was_returned(self):
        page = FakePage()
        self.runner.context = FakeContext(page)
        ai = self.model()
        with patch('facturador.runner.AI', return_value=ai):
            self.runner.schedule(self.key, PROFILE)
            task = self.runner.job_tasks[self.key]
            await self.pending('unknown')
            channel = await self.runner.viewer.take_control(self.runner.viewer.revision)
            self.store.mark_human_exposed(self.key)
            await self.runner.viewer.release(channel)
            self.assertEqual(self.runner.retry_action(self.store.get(self.key)), 'recover')
            self.assertEqual((await self.runner.retry_job(self.key))['action'], 'recovering')
            await task
        row = self.store.get(self.key)
        self.assertEqual((row['status'], row['issue_attempted'], row['human_exposed']), ('uncertain', 0, 1))
        self.assertEqual(ai.next_action.call_count, 1)
        self.assertEqual(page.clicks, 0)

    async def test_legacy_exposed_after_restart_reconciles_existing_files_without_new_tab(self):
        self.store.queue(self.key, PROFILE)
        self.store.enter_intervention(self.key, 'unknown', 'Revisión', True)
        self.store.mark_human_exposed(self.key)
        self.store.recover_after_restart()
        self.assertEqual(self.store.get(self.key)['issue_attempted'], 0)
        save_candidate(self.store, self.key, 'cfdi.xml', xml_bytes())
        save_candidate(self.store, self.key, 'cfdi.pdf', pdf_bytes())
        with patch.object(self.runner, 'start_browser', side_effect=AssertionError('No se abre otra página.')):
            first = await self.runner.retry_job(self.key)
            second = await self.runner.retry_job(self.key)
            self.assertEqual([first['action'], second['action']], ['recovering', 'active'])
            await self.runner.job_tasks[self.key]
        row = self.store.get(self.key)
        self.assertEqual((row['status'], row['uuid'], row['issue_attempted']), ('verified', FISCAL_UUID, 0))
        self.assertEqual((await self.runner.retry_job(self.key))['action'], 'complete')

    async def test_untouched_captcha_can_retry_after_restart(self):
        self.store.queue(self.key, PROFILE)
        self.store.enter_intervention(self.key, 'captcha', 'Verificación pendiente', False)
        self.assertEqual(self.store.get(self.key)['human_exposed'], 0)
        self.store.recover_after_restart()
        row = self.store.get(self.key)
        self.assertEqual((row['status'], row['issue_attempted'], row['human_exposed']), ('ready', 0, 0))
        self.assertEqual(self.runner.retry_action(row), 'queue')

    async def test_untouched_manual_pause_can_retry_after_restart(self):
        for kind in ('unknown', 'blocked', 'operator', 'login'):
            with self.subTest(kind=kind):
                row, _ = self.store.add('synthetic.jpg', 'untouched-'+kind)
                key = row['id']
                self.store.save_receipt(key, RECEIPT.model_copy(update={'billing_codes': ['COD-'+kind]}), ready=True)
                self.assertTrue(self.store.queue(key, PROFILE))
                self.assertTrue(self.store.claim(key))
                self.store.enter_intervention(key, kind, 'Pausa sin controlar Chrome', True)
                self.assertEqual(self.store.get(key)['human_exposed'], 0)
                self.store.recover_after_restart()
                restored = self.store.get(key)
                self.assertEqual((restored['status'], restored['issue_attempted'], restored['human_exposed']),
                                 ('ready', 0, 0))
                self.assertEqual(self.runner.retry_action(restored), 'queue')

    async def test_manual_inspection_does_not_claim_invoice_submission(self):
        self.assertTrue(self.store.queue(self.key, PROFILE))
        self.assertTrue(self.store.claim(self.key))
        page = FakePage(success=False)
        self.runner.viewer.page = page
        async def inspect(key, _page, kind, message):
            self.assertFalse(self.store.get(key)['issue_attempted'])
            self.store.enter_intervention(key, kind, message, True)
            self.store.mark_human_exposed(key)
            self.store.resume_intervention(key)
        with patch.object(self.runner, 'human', side_effect=inspect), \
             patch.object(self.runner, 'recover', new_callable=AsyncMock, return_value=False):
            await self.runner.manual_job(self.key, page, PROFILE)
        row = self.store.get(self.key)
        self.assertEqual((row['status'], row['issue_attempted'], row['human_exposed']),
                         ('uncertain', 0, 1))
        self.assertFalse(self.store.queue(self.key, PROFILE))

    async def test_captcha_control_lease_marks_exposure_once(self):
        app = create_app(self.root)
        runner = app.state.runner
        self.addAsyncCleanup(runner.close)
        self.addCleanup(app.state.plan.close)
        self.store.queue(self.key, PROFILE)
        self.store.enter_intervention(self.key, 'captcha', 'Verificación pendiente', False)
        page = Mock()
        page.is_closed.return_value = False
        await runner.viewer.show(page, 'Portal de prueba', manual=True)
        runner.active_id = self.key
        runner.page_job[page] = self.key
        runner.pending[self.key] = {'kind': 'captcha', 'page': page,
                                    'future': Mock(done=Mock(return_value=False))}
        endpoint = next(route.endpoint for route in app.routes if getattr(route, 'path', '') == '/api/browser/control')
        with self.assertRaises(ValueError):
            await endpoint(revision=runner.viewer.revision, job_id='otro-ticket')
        self.assertEqual(self.store.get(self.key)['human_exposed'], 0)
        result = await endpoint(revision=runner.viewer.revision, job_id=self.key)
        self.assertTrue(result['channel'])
        self.assertEqual(self.store.get(self.key)['human_exposed'], 1)
        self.assertEqual(sum(event['kind'] == 'human_control_taken' for event in self.store.events(self.key)), 1)
        await runner.viewer.release(result['channel'])
        app.state.store.mark_human_exposed(self.key)
        self.assertEqual(sum(event['kind'] == 'human_control_taken' for event in self.store.events(self.key)), 1)
        self.store.recover_after_restart()
        self.assertEqual(self.store.get(self.key)['status'], 'uncertain')

    async def test_generic_job_control_marks_only_current_ticket(self):
        app = create_app(self.root)
        runner = app.state.runner
        self.addAsyncCleanup(runner.close)
        self.addCleanup(app.state.plan.close)
        other, _ = self.store.add('synthetic.jpg', 'other-control-ticket')
        self.store.save_receipt(other['id'], RECEIPT, ready=True)
        self.store.queue(self.key, PROFILE)
        self.store.enter_intervention(self.key, 'unknown', 'Revisa el portal', True)
        page = Mock()
        page.is_closed.return_value = False
        await runner.viewer.show(page, 'Portal de prueba', manual=True)
        runner.active_id = self.key
        runner.page_job[page] = self.key
        runner.pending[self.key] = {'kind': 'unknown', 'page': page,
                                    'future': Mock(done=Mock(return_value=False))}
        endpoint = next(route.endpoint for route in app.routes if getattr(route, 'path', '') == '/api/browser/control')
        with self.assertRaises(ValueError):
            await endpoint(revision=runner.viewer.revision, job_id=other['id'])
        self.assertEqual(self.store.get(self.key)['human_exposed'], 0)
        self.assertEqual(self.store.get(other['id'])['human_exposed'], 0)
        result = await endpoint(revision=runner.viewer.revision, job_id=None)
        self.assertTrue(result['channel'])
        self.assertEqual(self.store.get(self.key)['human_exposed'], 1)
        self.assertEqual(self.store.get(other['id'])['human_exposed'], 0)
        self.assertEqual(sum(event['kind'] == 'human_control_taken' for event in self.store.events(self.key)), 1)
        await runner.viewer.release(result['channel'])
        runner.active_id = other['id']
        with self.assertRaises(ValueError):
            await endpoint(revision=runner.viewer.revision, job_id=None)
        self.assertEqual(self.store.get(other['id'])['human_exposed'], 0)

    async def test_capufe_proof_requires_same_ticket_and_unexpired_negative_without_prior_submission(self):
        receipt = RECEIPT.model_copy(update={'provider':'capufe', 'billing_codes':['ABCDEFGHIJKLMNOPQR']})
        self.store.save_receipt(self.key, receipt, ready=True)
        self.store.queue(self.key, PROFILE)
        self.store.claim(self.key)
        self.store.enter_intervention(self.key, 'unknown', 'Control', True)
        self.store.mark_human_exposed(self.key)
        self.store.recover_after_restart()
        self.assertFalse(self.store.record_not_billed(self.key, 'capufe', receipt.model_copy(update={'amount':'9'}), PROFILE))
        self.assertTrue(self.store.record_not_billed(self.key, 'capufe', receipt, PROFILE))
        self.assertTrue(self.store.queue_after_negative_check(self.key, receipt, PROFILE))
        self.assertFalse(self.store.queue_after_negative_check(self.key, receipt, PROFILE))
        self.assertTrue(self.store.claim(self.key))
        with self.store.connect() as db:
            db.execute('UPDATE negative_billing_checks SET checked_at=0')
        with self.assertRaisesRegex(ValueError, 'Venció'):
            self.store.begin_emission(self.key, 'Emitir')
        self.assertFalse(self.store.get(self.key)['issue_attempted'])
        with self.store.connect() as db:
            db.execute('UPDATE negative_billing_checks SET checked_at=unixepoch()')
        self.store.begin_emission(self.key, 'Emitir')
        self.store.update(self.key, status='uncertain')
        self.assertFalse(self.store.record_not_billed(self.key, 'capufe', receipt, PROFILE))
        self.assertFalse(self.store.queue_after_negative_check(self.key, receipt, PROFILE))

    async def test_retry_dispatches_capufe_then_prepares_only_after_explicit_negative(self):
        receipt = RECEIPT.model_copy(update={'provider':'capufe','billing_codes':['ABCDEFGHIJKLMNOPQR']})
        self.store.save_receipt(self.key, receipt, ready=True)
        self.store.queue(self.key, PROFILE)
        self.store.enter_intervention(self.key, 'unknown', 'Control', True)
        self.store.mark_human_exposed(self.key)
        self.store.recover_after_restart()
        checked = []
        async def lookup(store, key, ticket, profile):
            checked.append(key)
            if len(checked) == 1:
                return 'CAPUFE no confirmó que el código siga sin facturar.'
            self.assertTrue(store.record_not_billed(key, 'capufe', ticket, profile))
            return 'CAPUFE confirmó que el peaje no ha sido facturado.'
        started, finish = asyncio.Event(), asyncio.Event()
        async def prepare(key, profile, auto_issue, manual):
            self.assertFalse(auto_issue or manual)
            self.assertTrue(self.store.checked_preparation(key))
            started.set()
            await finish.wait()
        with (patch('facturador.capufe_recovery.recover_capufe', side_effect=lookup),
              patch.object(self.runner, '_session', side_effect=prepare) as preparing,
              patch.object(self.runner.plan, 'status', return_value={'status':'ready'})):
            await self.runner.retry_job(self.key)
            await self.runner.job_tasks[self.key]
            self.assertEqual(self.store.get(self.key)['status'], 'uncertain')
            await self.runner.retry_job(self.key)
            self.assertEqual(len(checked), 1)
            self.assertEqual(preparing.call_count, 0)
            with self.store.connect() as db:
                db.execute('UPDATE recovery_requests SET requested_at=0')
            await self.runner.retry_job(self.key)
            recovery = self.runner.job_tasks[self.key]
            await recovery
            await asyncio.wait_for(started.wait(), 2)
            task = self.runner.job_tasks[self.key]
            self.assertIsNot(task, recovery)
            self.assertTrue(self.store.get(self.key)['human_exposed'])
            self.assertFalse(self.store.get(self.key)['issue_attempted'])
            finish.set()
            await task

    async def test_only_transient_initial_get_is_retried(self):
        page = FakePage()
        bad = Mock(status=503, headers={})
        good = Mock(status=200)
        page.goto = AsyncMock(side_effect=[BrowserError('net::ERR_CONNECTION_RESET'), bad, good])
        challenge = AsyncMock(return_value=None)
        page.locator = Mock(return_value=Mock(aria_snapshot=AsyncMock(return_value='')))
        with patch('facturador.ticket_retry.asyncio.sleep', new_callable=AsyncMock) as sleep:
            await open_ticket_portal(page, page.url, RECEIPT, challenge, self.store, self.key)
            self.assertEqual(page.goto.await_count, 3)
            self.assertEqual([c.args[0] for c in sleep.await_args_list], [1, 3])
            page.goto = AsyncMock(return_value=bad)
            challenge.return_value = 'captcha'
            await open_ticket_portal(page, page.url, RECEIPT, challenge, self.store, self.key)
            self.assertEqual(page.goto.await_count, 1)
            page.goto = AsyncMock(return_value=Mock(status=403, headers={}))
            challenge.return_value = None
            with self.assertRaisesRegex(ValueError, '403'):
                await open_ticket_portal(page, page.url, RECEIPT, challenge, self.store, self.key)
            self.assertEqual(page.goto.await_count, 1)

    async def test_unknown_page_result_requires_ticket_identity(self):
        row = self.store.get(self.key)
        self.assertFalse(ticket_in_result('Factura emitida '+FISCAL_UUID, row, RECEIPT))
        self.assertTrue(ticket_in_result('Ticket COD-O0-I1 ya facturado', row, RECEIPT))


class RetryEndpointTests(unittest.TestCase):
    def test_authenticated_route_and_capability_are_available_before_first_job(self):
        with tempfile.TemporaryDirectory() as tmp:
            app = create_app(Path(tmp))
            with TestClient(app) as client:
                state = client.get('/api/state').json()
                self.assertTrue(state['supports_ticket_retry'])
                row, _ = app.state.store.add('fixture.png','fresh')
                url = f"/api/jobs/{row['id']}/retry"
                self.assertEqual(client.post(url).status_code, 403)
                result = client.post(url, headers={'x-app-token':state['token']})
                self.assertEqual(result.json()['action'], 'needs_user')
                self.assertEqual(client.post('/api/jobs/missing/retry', headers={'x-app-token':state['token']}).status_code, 404)
