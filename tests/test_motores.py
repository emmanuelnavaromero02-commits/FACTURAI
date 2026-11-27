"""Several tickets in their portals at once: who takes each ticket, who has it and what the panel shows.

No browser here. The engines are real ``Runner`` objects whose work inside a portal is a stand-in
that stays «in the portal» until the test lets it go, so every test decides the order of things
with events and never with the clock. The real portals of the bank are in test_motores_navegador.
"""
import asyncio
import logging
import os
import sys
import tempfile
import threading
import time
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock, patch

import httpx
from dotenv import dotenv_values
from fastapi import FastAPI

sys.path.insert(0, str(Path(__file__).resolve().parent))

from facturador import motores
from facturador.app import create_app
from facturador.auth import write_private
from facturador.automation import TicketAutomation
from facturador.catalog import CATALOG, CUSTOM_PORTAL
from facturador.chat import SECRET_KEPT, SECRET_THANKS, ChatDecision, chat_router
from facturador.db import Store
from facturador.instancias import Instances
from facturador.invoice_mail import InvoiceMailRecovery
from facturador.models import Receipt
from facturador.motores import SAME_PORTAL, SETTING, SLOTS, Engines, portal_keys, tickets_at_once
from facturador.runner import Meter, Runner
from acceso_dobles import ORIGIN, FakeClock, quiet_app, use_master_key
from test_core import PROFILE

CAFE, PAN, FLOR, MIEL = (f'https://facturas.{name}-de-prueba.example' for name in ('cafe', 'pan', 'flor', 'miel'))


def receipt_at(origin: str, folio: str, **changes) -> Receipt:
    fields = dict(provider=CUSTOM_PORTAL, portal_url=origin + '/', merchant='Comercio ' + folio, amount='100.00',
                  date='2026-11-13', ticket_number=folio, payment_code='04')
    return Receipt(**{**fields, **changes})


def open_page():
    """A page of a portal as a viewer looks at it."""
    page = Mock()
    page.is_closed.return_value = False
    page.url = CAFE + '/'
    return page


class FakePage:
    """A page a ticket left open, as far as the coordinator looks at it."""
    url = 'about:blank'

    def is_closed(self):
        return False


class EnginesCase(unittest.IsolatedAsyncioTestCase):
    """``limit`` engines whose time inside a portal is held by the test."""
    limit = 3

    async def asyncSetUp(self):
        folder = tempfile.TemporaryDirectory()
        self.addCleanup(folder.cleanup)
        self.root = Path(folder.name)
        self.plan = Mock()
        self.plan.status.return_value = {'status': 'ready'}
        self.open_engines()
        self.started: dict[str, asyncio.Event] = {}
        self.leave: dict[str, asyncio.Event] = {}
        self.asks_person: set[str] = set()   # tickets that stop inside the portal until the person decides
        self.log: list[tuple] = []           # ('in' | 'out', number of the engine, ticket)
        case = self

        async def in_portal(runner, key, profile, auto_issue=False, manual=False):
            """What Runner._run keeps of its ticket: the engine's lock, the single claim and the active ticket."""
            async with runner.lock:
                if not runner.store.claim(key):
                    return
                number = case.number(runner)
                runner.active_id = key
                case.log.append(('in', number, key))
                case.event(case.started, key).set()
                try:
                    if key in case.asks_person:
                        await case.person(runner, key)
                    await case.event(case.leave, key).wait()
                    runner.store.update(key, status='blocked', message='Fin de la prueba.')
                finally:
                    runner.active_id = None
                    case.log.append(('out', number, key))

        patcher = patch.object(Runner, '_run', in_portal)
        patcher.start()
        self.addCleanup(patcher.stop)

    def open_engines(self):
        self.store = Store(self.root)
        self.engines = self.build(self.store, self.limit)

    def build(self, store, limit):
        engines = Engines(Runner(store, self.plan), limit)
        self.addAsyncCleanup(engines.close)
        return engines

    def number(self, runner) -> int:
        return next(index for index, engine in enumerate(self.engines.all, 1) if engine is runner)

    @staticmethod
    def event(events, key) -> asyncio.Event:
        return events.setdefault(key, asyncio.Event())

    @staticmethod
    async def person(runner, key, kind='issue', page=None):
        """Wait for the person as Runner.human does: the ticket is pending until somebody decides."""
        future = asyncio.get_running_loop().create_future()
        runner.pending[key] = {'kind': kind, 'future': future, 'page': page, 'url': getattr(page, 'url', ''),
                               'control_epoch': runner.viewer.control_epoch, 'challenge_seen': False}
        runner.store.enter_intervention(key, kind, 'Revisa este paso.', False)
        try:
            return await future
        finally:
            runner.pending.pop(key, None)
            runner.store.resume_intervention(key)

    def ticket(self, origin, folio, store=None, **changes):
        store = store or self.store
        key = store.add(folio + '.png', 'foto-' + folio)[0]['id']
        receipt = receipt_at(origin, folio, **changes)
        self.assertEqual(store.save_receipt(key, receipt, ready=True)['status'], 'ready')
        return key, receipt

    def start(self, origin, folio, **options):
        key, receipt = self.ticket(origin, folio)
        self.engines.schedule(key, PROFILE, auto_issue=True, confirmed_url=receipt.portal_url, **options)
        return key

    async def until(self, condition, seconds=10):
        deadline = time.monotonic() + seconds
        while not condition():
            if time.monotonic() > deadline:
                self.fail('La condición no se cumplió a tiempo.')
            await asyncio.sleep(0.005)

    async def inside(self, *keys):
        async with asyncio.timeout(10):
            await asyncio.gather(*(self.event(self.started, key).wait() for key in keys))

    async def finish(self, *keys):
        for key in keys:
            self.event(self.leave, key).set()
        await self.until(lambda: all(self.engines.owner(key) is None for key in keys))

    def engine_of(self, key) -> int:
        return next(number for moment, number, ticket in self.log if moment == 'in' and ticket == key)

    def status(self, key):
        row = self.store.get(key)
        return row['status'], row['message']


class SettingTests(unittest.TestCase):
    def test_one_ticket_at_a_time_unless_asked_and_never_more_than_four(self):
        self.assertEqual(tickets_at_once({}), 1)
        self.assertEqual(tickets_at_once({SETTING: ' '}), 1)
        for text, expected in (('1', 1), ('2', 2), (' 3 ', 3), ('4', 4)):
            self.assertEqual(tickets_at_once({SETTING: text}), expected)
        # The example configuration names the setting and leaves it as always.
        example = dotenv_values(Path(__file__).resolve().parents[1] / '.env.example')
        self.assertIn(SETTING, example)
        with self.assertNoLogs(motores.log, logging.WARNING):
            self.assertEqual(tickets_at_once(example), 1)

    def test_an_invalid_value_is_one_with_a_notice_in_the_log(self):
        for text in ('0', '5', '-1', 'dos', '2.5', '²', '12'):
            with self.subTest(text), self.assertLogs(motores.log, logging.WARNING) as noted:
                self.assertEqual(tickets_at_once({SETTING: text}), 1)
            self.assertIn(SETTING, noted.output[0])

    def test_with_several_accounts_it_stays_at_one(self):
        motores._told_accounts.clear()
        with self.assertLogs(motores.log, logging.WARNING) as noted:
            self.assertEqual(tickets_at_once({SETTING: '3'}, accounts=True), 1)
        self.assertIn('varias cuentas', noted.output[0])
        with self.assertNoLogs(motores.log, logging.WARNING):   # Said once, however many accounts open.
            self.assertEqual(tickets_at_once({SETTING: '3'}, accounts=True), 1)
            self.assertEqual(tickets_at_once({SETTING: '1'}, accounts=True), 1)
            self.assertEqual(tickets_at_once({}, accounts=True), 1)

    def test_the_application_reads_it_and_the_accounts_mode_forces_one(self):
        motores._told_accounts.clear()
        with tempfile.TemporaryDirectory() as folder, patch.dict(os.environ, {SETTING: '3', 'FACTURADOR_MODO': 'local'}):
            local = create_app(Path(folder) / 'local')
            self.assertEqual((local.state.engines.limit, len(local.state.engines.helpers)), (3, 2))
            self.assertIs(local.state.engines.main, local.state.runner)
            self.assertEqual([engine.profile_suffix for engine in local.state.engines.all], ['', '_2', '_3'])
            # Behind the entrance for several customers each account keeps a single engine, whatever is asked.
            with self.assertLogs(motores.log, logging.WARNING):
                shared = create_app(Path(folder) / 'cuenta', public_origin='https://facturas.example', lazy_browser=True)
            self.assertEqual((shared.state.engines.limit, shared.state.engines.helpers), (1, []))
            self.assertIsNone(shared.state.automation.engines)
            with patch.dict(os.environ, {'FACTURADOR_MODO': 'usuarios'}):
                other = create_app(Path(folder) / 'otra')
            self.assertEqual((other.state.engines.limit, other.state.engines.helpers), (1, []))
            for app in (local, shared, other):
                app.state.plan.close()

    def test_by_default_there_is_one_engine_untouched_and_no_helper(self):
        with tempfile.TemporaryDirectory() as folder, patch.dict(os.environ, {}, clear=False):
            os.environ.pop(SETTING, None)
            app = create_app(Path(folder))
            engines, runner = app.state.engines, app.state.runner
            self.assertEqual((engines.limit, engines.helpers, engines.several), (1, [], False))
            self.assertEqual(engines.all, [runner])
            # Alone, the engine starts its own tickets, waits for nobody and asks without taking turns.
            self.assertIsNone(runner.dispatch)
            self.assertIsNone(runner.chat_turn)
            self.assertEqual(runner.profile_suffix, '')
            self.assertIs(engines.viewer, runner.viewer)
            self.assertIs(engines.screen, runner.screen)
            self.assertEqual(engines.view_at(7), (runner.screen, 7))
            self.assertEqual(engines.browser_state(), runner.browser_state())
            self.assertIsNone(app.state.automation.engines)
            self.assertEqual([path.name for path in Path(folder).iterdir() if path.name.startswith('browser_')], [])
            app.state.plan.close()


class PortalKeyTests(unittest.TestCase):
    def test_same_origin_or_same_catalog_entry_is_the_same_portal(self):
        first, second = (receipt_at(CAFE, folio).model_dump() for folio in ('A1', 'A2'))
        self.assertTrue(portal_keys(first) & portal_keys(second))
        self.assertFalse(portal_keys(first) & portal_keys(receipt_at(PAN, 'B1').model_dump()))
        # Another page or a ticket link of the same site is still that site.
        self.assertTrue(portal_keys(first) & portal_keys(receipt_at(CAFE, 'A3', portal_url=CAFE + '/t?folio=A3').model_dump()))
        provider = next(iter(CATALOG))
        listed = {'provider': provider, 'portal_url': ''}
        self.assertTrue(portal_keys(listed) & portal_keys({'provider': provider, 'portal_url': CATALOG[provider]['url']}))
        # A ticket that names the site of a catalog portal by its address is that portal too.
        self.assertTrue(portal_keys(listed) & portal_keys({'provider': CUSTOM_PORTAL, 'portal_url': CATALOG[provider]['url']}))
        other = next(key for key in CATALOG if key != provider)
        self.assertFalse(portal_keys(listed) & portal_keys({'provider': other, 'portal_url': ''}))
        self.assertEqual(portal_keys({'provider': 'unknown', 'portal_url': 'http://sin-https.example/'}), frozenset())


class SeveralAtOnceTests(EnginesCase):
    async def test_three_tickets_of_different_portals_are_in_three_engines_at_the_same_time(self):
        keys = [self.start(origin, folio) for origin, folio in ((CAFE, 'A1'), (PAN, 'B1'), (FLOR, 'C1'))]
        await self.inside(*keys)
        self.assertEqual(sorted(self.engine_of(key) for key in keys), [1, 2, 3])
        self.assertEqual([self.status(key)[0] for key in keys], ['running'] * 3)
        self.assertEqual(sorted(self.engines.active_tickets()), sorted(keys))
        self.assertTrue(self.engines.has_active_jobs())
        self.assertIsNone(self.engines.free_engine())
        await self.finish(*keys)
        self.assertFalse(self.engines.has_active_jobs())
        self.assertEqual([moment for moment, _, _ in self.log], ['in'] * 3 + ['out'] * 3)

    async def test_the_main_engine_takes_the_first_ticket_and_a_helper_only_the_one_that_does_not_fit(self):
        first = self.start(CAFE, 'A1')
        await self.inside(first)
        self.assertEqual(self.engine_of(first), 1)
        self.assertTrue(all(engine.context is None for engine in self.engines.helpers))
        second = self.start(PAN, 'B1')
        await self.inside(second)
        self.assertEqual(self.engine_of(second), 2)
        await self.finish(first, second)

    async def test_a_ticket_that_waits_for_the_person_does_not_stop_the_others(self):
        waiting = self.start(CAFE, 'A1')
        self.asks_person.add(waiting)
        await self.inside(waiting)
        await self.until(lambda: self.engines.waiting_kind(waiting) == 'issue')
        self.assertEqual(self.status(waiting)[0], 'needs_user')
        others = [self.start(origin, folio) for origin, folio in ((PAN, 'B1'), (FLOR, 'C1'))]
        await self.inside(*others)
        await self.finish(*others)
        # A third and a fourth one go through as well while the first still waits.
        more = [self.start(origin, folio) for origin, folio in ((MIEL, 'D1'), (PAN, 'B2'))]
        await self.inside(*more)
        await self.finish(*more)
        self.assertEqual(self.engines.waiting_kind(waiting), 'issue')
        self.assertNotIn(self.engine_of(waiting), {self.engine_of(key) for key in (*others, *more)})
        # Attended, it goes on in its own engine and ends too.
        await self.engines.continue_job(waiting, 'approve')
        await self.finish(waiting)
        self.assertEqual(self.status(waiting), ('blocked', 'Fin de la prueba.'))

    async def test_a_fourth_ticket_waits_in_line_and_takes_the_first_engine_that_is_free(self):
        keys = [self.start(origin, folio) for origin, folio in ((CAFE, 'A1'), (PAN, 'B1'), (FLOR, 'C1'))]
        await self.inside(*keys)
        fourth, fifth = self.start(MIEL, 'D1'), self.start(MIEL + '.mx', 'E1')
        self.assertEqual(self.status(fourth), ('queued', 'En cola'))
        self.assertTrue(self.engines.busy_with(fourth))
        self.assertIsNone(self.engines.owner(fourth))
        self.assertIn(fourth, self.engines.open_tickets())
        await self.finish(keys[1])
        await self.inside(fourth)
        self.assertEqual(self.engine_of(fourth), self.engine_of(keys[1]))
        self.assertEqual(self.status(fifth)[0], 'queued')   # Still in line, behind the fourth.
        await self.finish(keys[0], keys[2], fourth)
        await self.inside(fifth)
        await self.finish(fifth)

    async def test_a_ticket_in_line_can_be_taken_out_and_never_starts(self):
        keys = [self.start(origin, folio) for origin, folio in ((CAFE, 'A1'), (PAN, 'B1'), (FLOR, 'C1'))]
        await self.inside(*keys)
        fourth = self.start(MIEL, 'D1')
        await self.engines.stop(fourth)
        self.assertEqual(self.status(fourth), ('ready', 'Retirado de la cola.'))
        self.assertFalse(self.engines.busy_with(fourth))
        await self.finish(*keys)
        self.assertNotIn(fourth, self.started)
        with self.assertRaisesRegex(ValueError, 'ya no está en ejecución'):
            await self.engines.stop(fourth)

    async def test_stopping_goes_to_the_engine_that_has_the_ticket(self):
        keys = [self.start(origin, folio) for origin, folio in ((CAFE, 'A1'), (PAN, 'B1'))]
        await self.inside(*keys)
        await self.engines.stop(keys[1])
        self.assertIsNone(self.engines.owner(keys[1]))
        self.assertEqual(self.log[-1], ('out', 2, keys[1]))
        self.assertIs(self.engines.owner(keys[0]), self.engines.main)
        await self.finish(keys[0])

    async def test_no_ticket_is_scheduled_twice(self):
        key, receipt = self.ticket(CAFE, 'A1')
        self.engines.schedule(key, PROFILE, auto_issue=True, confirmed_url=receipt.portal_url)
        for _ in range(3):
            with self.assertRaisesRegex(ValueError, 'Solo se puede encolar'):
                self.engines.schedule(key, PROFILE, auto_issue=True, confirmed_url=receipt.portal_url)
        await self.inside(key)
        with self.assertRaisesRegex(ValueError, 'Solo se puede encolar'):
            self.engines.schedule(key, PROFILE, auto_issue=True, confirmed_url=receipt.portal_url)
        self.assertEqual(sum(key in engine.job_tasks for engine in self.engines.all), 1)
        await self.finish(key)
        self.assertEqual([entry for entry in self.log if entry[2] == key], [('in', 1, key), ('out', 1, key)])

    async def test_closing_cancels_every_engine_and_starts_nothing_from_the_line(self):
        keys = [self.start(origin, folio) for origin, folio in ((CAFE, 'A1'), (PAN, 'B1'), (FLOR, 'C1'))]
        await self.inside(*keys)
        fourth = self.start(MIEL, 'D1')
        await self.engines.close()
        self.assertEqual(sorted(entry[2] for entry in self.log if entry[0] == 'out'), sorted(keys))
        self.assertFalse(any(engine.job_tasks for engine in self.engines.all))
        self.assertFalse(self.engines.has_active_jobs())
        self.assertNotIn(fourth, self.started)
        # Like a ticket queued behind a single engine, it stays queued until the next start puts it back.
        self.assertEqual(self.status(fourth)[0], 'queued')
        self.store.recover_after_restart()
        self.assertEqual(self.status(fourth)[0], 'ready')


class SamePortalTests(EnginesCase):
    limit = 1  # Each test builds the engines it needs.

    async def test_two_tickets_of_the_same_portal_never_overlap_with_any_number_of_engines(self):
        for limit in (1, 2, 3, 4):
            with self.subTest(engines=limit), tempfile.TemporaryDirectory() as folder:
                store = Store(Path(folder))
                self.engines = engines = Engines(Runner(store, self.plan), limit)
                self.log.clear()
                try:
                    tickets = [self.ticket(origin, f'{folio}-{limit}', store) for origin, folio in
                               ((CAFE, 'A1'), (CAFE, 'A2'), (PAN, 'B1'), (CAFE, 'A3'))]
                    for key, receipt in tickets:
                        engines.schedule(key, PROFILE, auto_issue=True, confirmed_url=receipt.portal_url)
                    first, second, other, third = (key for key, _ in tickets)
                    await self.inside(first)
                    if limit > 1:
                        # The other portal does not wait for anybody; the same portal does, with its reason.
                        await self.inside(other)
                        self.assertEqual(store.get(second)['message'], SAME_PORTAL)
                        self.assertEqual(store.get(third)['message'], SAME_PORTAL)
                    if limit > 2:
                        self.assertIsNotNone(engines.free_engine())   # It waits although an engine is free.
                    for _ in range(20):
                        await asyncio.sleep(0)
                    self.assertFalse(self.event(self.started, second).is_set())
                    self.assertFalse(self.event(self.started, third).is_set())
                    self.assertEqual(store.get(second)['status'], 'queued')
                    self.event(self.leave, other).set()   # With one engine it goes through when its turn comes.
                    self.event(self.leave, first).set()
                    await self.inside(second)
                    self.assertFalse(self.event(self.started, third).is_set())
                    self.event(self.leave, second).set()
                    await self.inside(third)
                    self.event(self.leave, third).set()
                    await self.until(lambda: not engines.has_active_jobs())
                    order = [(moment, key) for moment, _, key in self.log if key in (first, second, third)]
                    self.assertEqual(order, [('in', first), ('out', first), ('in', second), ('out', second),
                                             ('in', third), ('out', third)])
                    if limit > 1:
                        # The engine that worked on the portal keeps its session: the next ticket goes to it.
                        self.assertEqual({self.engine_of(key) for key in (first, second, third)}, {self.engine_of(first)})
                finally:
                    await engines.close()

    async def test_a_portal_the_person_has_open_by_hand_gets_its_ticket_in_the_main_browser_or_waits(self):
        self.engines = engines = self.build(self.store, 3)
        main = engines.main
        main.portal_page = open_page()
        await main.viewer.show(main.portal_page, 'Portal de prueba', manual=True)
        lease = await main.viewer.take_control(main.viewer.revision)
        same = self.start(CAFE, 'A1')    # The person is acting on that portal: its ticket waits.
        other = self.start(PAN, 'B1')    # Another portal does not: it goes to a helper.
        await self.inside(other)
        self.assertEqual(self.engine_of(other), 2)
        self.assertEqual(self.status(same), ('queued', SAME_PORTAL))
        self.assertIsNotNone(engines.free_engine())
        await main.viewer.release(lease)
        await self.inside(same)          # Left alone, the main browser takes it: never a second visitor beside it.
        self.assertEqual(self.engine_of(same), 1)
        await self.finish(same, other)

    async def test_a_recovery_sent_straight_to_another_engine_waits_for_the_portal_too(self):
        self.engines = engines = self.build(self.store, 3)
        first = self.start(CAFE, 'A1')
        await self.inside(first)
        key, _ = self.ticket(CAFE, 'A2')
        self.store.update(key, status='uncertain', message='Resultado pendiente.')
        recovered = asyncio.Event()

        async def recover_only(runner, ticket, profile):
            recovered.set()

        with patch.object(Runner, 'recover_only', recover_only):
            helper = engines.helpers[1]
            recovery = asyncio.create_task(helper.recover_queued(key, PROFILE))
            await self.until(lambda: any(event['kind'] == 'portal_wait' for event in self.store.events(key)))
            self.assertFalse(recovered.is_set())
            self.assertFalse(helper.lock.locked())   # It waits outside its engine, which stays usable.
            self.event(self.leave, first).set()
            async with asyncio.timeout(10):
                await recovered.wait()
                await recovery
        self.assertEqual([event['detail'] for event in self.store.events(key) if event['kind'] == 'portal_wait'],
                         [SAME_PORTAL])

    async def test_a_ticket_may_come_back_into_its_own_portal_while_it_is_still_inside(self):
        self.engines = engines = self.build(self.store, 2)
        key, _ = self.ticket(CAFE, 'A1')
        other, _ = self.ticket(CAFE, 'A2')
        entered = []
        async with engines.main.portal_turn(key):
            async with engines.main.portal_turn(key):   # A check that goes on to prepare the same ticket.
                entered.append('twice')
            waiting = asyncio.create_task(self.enter(engines.helpers[0], other, entered))
            for _ in range(20):
                await asyncio.sleep(0)
            self.assertEqual(entered, ['twice'])
        async with asyncio.timeout(10):
            await waiting
        self.assertEqual(entered, ['twice', other])

    @staticmethod
    async def enter(engine, key, entered):
        async with engine.portal_turn(key):
            entered.append(key)


class ClaimTests(unittest.IsolatedAsyncioTestCase):
    """The real Runner.run of several engines racing for the same ticket."""

    async def asyncSetUp(self):
        folder = tempfile.TemporaryDirectory()
        self.addCleanup(folder.cleanup)
        self.store = Store(Path(folder.name))
        plan = Mock()
        plan.status.return_value = {'status': 'ready'}
        self.engines = Engines(Runner(self.store, plan), 4)
        self.addAsyncCleanup(self.engines.close)

    def queued(self, folio='A1'):
        key = self.store.add(folio + '.png', 'foto-' + folio)[0]['id']
        receipt = receipt_at(CAFE, folio)
        self.store.save_receipt(key, receipt, ready=True)
        self.assertTrue(self.store.queue(key, PROFILE, receipt.portal_url))
        return key

    async def test_four_engines_racing_for_one_ticket_work_on_it_once(self):
        key = self.queued()
        worked = []
        together = asyncio.Event()

        async def session(runner, ticket, profile, auto_issue, manual):
            worked.append(runner)
            await together.wait()

        with patch.object(Runner, '_session', session):
            races = [asyncio.create_task(engine.run(key, PROFILE, True)) for engine in self.engines.all]
            async with asyncio.timeout(10):
                # The three that lost the claim end at once; the winner is still inside its session.
                while sum(task.done() for task in races) < 3:
                    await asyncio.sleep(0.005)
            self.assertEqual(len(worked), 1)
            self.assertEqual(sum(engine.active_id == key for engine in self.engines.all), 1)
            self.assertEqual(self.store.get(key)['status'], 'running')
            together.set()
            await asyncio.gather(*races)
        self.assertEqual(len(worked), 1)

    def test_only_one_of_many_threads_claims_a_ticket(self):
        key = self.queued('A2')
        start = threading.Barrier(8)
        claims = []

        def claim():
            start.wait(10)
            claims.append(self.store.claim(key))

        threads = [threading.Thread(target=claim) for _ in range(8)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join(20)
        self.assertEqual((len(claims), sum(claims)), (8, 1))


class RetryTests(EnginesCase):
    async def asyncSetUp(self):
        await super().asyncSetUp()
        self.calls = []
        case = self

        async def retry_job(runner, key, current_profile=None, *, automatic=False):
            case.calls.append((case.number(runner), key, automatic))
            return {'id': key, 'status': 'uncertain', 'action': 'recovering', 'message': 'Consulta solicitada.'}

        patcher = patch.object(Runner, 'retry_job', retry_job)
        patcher.start()
        self.addCleanup(patcher.stop)

    def uncertain(self, origin, folio):
        key, _ = self.ticket(origin, folio)
        self.store.update(key, status='uncertain', message='Resultado pendiente.')
        return key

    async def test_a_ticket_an_engine_has_is_retried_there(self):
        keys = [self.start(origin, folio) for origin, folio in ((CAFE, 'A1'), (PAN, 'B1'), (FLOR, 'C1'))]
        self.asks_person.update(keys)
        await self.inside(*keys)
        for key in reversed(keys):
            await self.engines.retry_job(key, PROFILE)
        self.assertEqual(self.calls, [(self.engine_of(key), key, False) for key in reversed(keys)])
        for key in keys:
            await self.engines.continue_job(key, 'approve')
        await self.finish(*keys)

    async def test_a_ticket_goes_back_to_the_engine_that_kept_its_page(self):
        key = self.uncertain(CAFE, 'A1')
        self.engines.helpers[1].page_job[FakePage()] = key
        await self.engines.retry_job(key, PROFILE)
        await self.engines.retry_job(key, PROFILE, automatic=True)
        self.assertEqual(self.calls, [(3, key, False), (3, key, True)])

    async def test_a_ticket_nobody_has_goes_to_a_free_engine_and_never_to_two(self):
        busy = self.start(CAFE, 'A1')
        await self.inside(busy)
        key = self.uncertain(PAN, 'B1')
        await asyncio.gather(*(self.engines.retry_job(key, PROFILE) for _ in range(4)))
        self.assertEqual({number for number, _, _ in self.calls}, {2})
        await self.finish(busy)

    async def test_with_its_portal_busy_a_retry_queues_behind_that_ticket_and_an_automatic_one_does_nothing(self):
        busy = self.start(CAFE, 'A1')
        other = self.start(PAN, 'B1')
        await self.inside(busy, other)
        key = self.uncertain(PAN, 'B2')
        result = await self.engines.retry_job(key, PROFILE, automatic=True)
        self.assertEqual((result['action'], self.calls), ('active', []))
        await self.engines.retry_job(key, PROFILE)
        self.assertEqual(self.calls, [(self.engine_of(other), key, False)])
        await self.finish(busy, other)

    async def test_an_automatic_recovery_needs_a_free_engine(self):
        keys = [self.start(origin, folio) for origin, folio in ((CAFE, 'A1'), (PAN, 'B1'), (FLOR, 'C1'))]
        await self.inside(*keys)
        key = self.uncertain(MIEL, 'D1')
        result = await self.engines.retry_job(key, lambda: PROFILE, automatic=True)
        self.assertEqual((result['action'], result['id'], self.calls), ('active', key, []))
        waiting = self.start(MIEL + '.mx', 'E1')   # A ticket in the line is already on its way.
        self.assertEqual((await self.engines.retry_job(waiting, PROFILE))['action'], 'active')
        self.assertEqual(self.calls, [])
        await self.finish(keys[0])
        await self.inside(waiting)
        await self.engines.retry_job(key, lambda: PROFILE, automatic=True)
        self.assertEqual(self.calls, [])            # The engine that was free took the ticket of the line.
        await self.finish(keys[1])
        await self.engines.retry_job(key, lambda: PROFILE, automatic=True)
        self.assertEqual(self.calls, [(self.engine_of(keys[1]), key, True)])
        await self.finish(keys[2], waiting)


class FocusTests(EnginesCase):
    def shown(self):
        return self.number(self.engines.focused())

    async def test_the_panel_shows_the_ticket_that_waits_for_the_person_and_its_requests_reach_its_engine(self):
        running = self.start(CAFE, 'A1')
        waiting = self.start(PAN, 'B1')
        self.asks_person.add(waiting)
        await self.inside(running, waiting)
        await self.until(lambda: self.engines.waiting_kind(waiting))
        self.assertEqual((self.engine_of(running), self.engine_of(waiting)), (1, 2))
        self.assertEqual(self.shown(), 2)
        self.assertEqual(self.engines.active_id, waiting)
        self.assertIs(self.engines.screen, self.engines.helpers[0].viewer)
        self.assertEqual(self.engines.waiting_kind(running), None)
        self.assertEqual(self.engines.retry_action(self.store.get(waiting)), None)   # An approval is not retried.
        panel = self.engines.panel()
        self.assertEqual(panel, [{'n': 1, 'job_id': running, 'waiting': False, 'shown': False},
                                 {'n': 2, 'job_id': waiting, 'waiting': True, 'shown': True}])
        self.assertEqual(self.engines.waiting_for_person(), 1)
        self.assertEqual(self.engines.in_progress(), [waiting, running])
        # The decision of that ticket goes to the engine that has it, whatever the panel shows.
        with self.assertRaisesRegex(ValueError, 'ya no está activa'):
            await self.engines.continue_job(running, 'approve')
        await self.engines.continue_job(waiting, 'approve')
        await self.until(lambda: not self.engines.waiting_kind(waiting))
        self.assertEqual(self.shown(), 1)   # Nobody waits: the main engine, which has a ticket.
        await self.finish(running, waiting)

    async def test_the_oldest_wait_goes_first_and_the_next_one_follows_when_it_is_attended(self):
        keys = [self.start(origin, folio) for origin, folio in ((CAFE, 'A1'), (PAN, 'B1'), (FLOR, 'C1'))]
        await self.inside(*keys)
        first, second, third = keys
        for key in (third, first):   # The third one starts to wait before the first.
            await self.person_starts_to_wait(key)
        self.assertEqual(self.engines.active_id, third)
        await self.engines.continue_job(third, 'approve')
        await self.until(lambda: not self.engines.waiting_kind(third))
        self.assertEqual(self.engines.active_id, first)
        await self.engines.continue_job(first, 'approve')
        await self.until(lambda: not self.engines.waiting_kind(first))
        self.assertEqual(self.shown(), 1)
        await self.finish(*keys)

    async def person_starts_to_wait(self, key):
        engine = self.engines.owner(key)
        asyncio.create_task(self.person(engine, key))
        await self.until(lambda: self.engines.waiting_kind(key))
        self.engines.focused()   # The panel asks, as it does every second.

    async def test_what_the_person_chose_stays_until_another_ticket_starts_to_wait(self):
        keys = [self.start(origin, folio) for origin, folio in ((CAFE, 'A1'), (PAN, 'B1'), (FLOR, 'C1'))]
        await self.inside(*keys)
        first, second, third = keys
        await self.person_starts_to_wait(second)
        self.assertEqual(self.shown(), 2)
        await self.engines.show(job_id=third)
        self.assertEqual((self.shown(), self.engines.active_id), (3, third))
        self.assertEqual(self.shown(), 3)   # The ticket that already waited does not take the screen back.
        await self.person_starts_to_wait(first)
        self.assertEqual(self.shown(), 2)   # A new wait does: the one that has waited longest is shown.
        await self.engines.show(engine=1)
        self.assertEqual(self.shown(), 1)
        for key in (first, second):
            await self.engines.continue_job(key, 'approve')
        await self.finish(*keys)
        self.assertEqual(self.shown(), 1)

    async def test_a_choice_ends_with_its_ticket_and_a_helper_at_work_is_shown_when_the_main_one_rests(self):
        first = self.start(CAFE, 'A1')
        second = self.start(PAN, 'B1')
        await self.inside(first, second)
        await self.engines.show(job_id=second)
        self.assertEqual(self.shown(), 2)
        await self.finish(second)
        self.assertEqual(self.shown(), 1)
        third = self.start(PAN, 'B2')
        await self.inside(third)
        await self.finish(first)
        self.assertEqual((self.shown(), self.engines.active_id), (self.engine_of(third), third))
        await self.finish(third)
        self.assertEqual((self.shown(), self.engines.active_id), (1, None))
        with self.assertRaisesRegex(ValueError, 'ya no está en un navegador'):
            await self.engines.show(job_id=third)
        with self.assertRaisesRegex(ValueError, 'ya no está en un navegador'):
            await self.engines.show(engine=9)

    async def test_the_ticket_the_person_just_started_is_shown_once_an_engine_has_it(self):
        keys = [self.start(origin, folio) for origin, folio in ((CAFE, 'A1'), (PAN, 'B1'), (FLOR, 'C1'))]
        await self.inside(*keys)
        fourth = self.start(MIEL, 'D1')
        self.engines.choose(fourth)
        self.assertEqual(self.shown(), 1)       # Still in the line: nothing of it to show.
        await self.finish(keys[2])
        await self.inside(fourth)
        self.assertEqual((self.shown(), self.engines.active_id), (3, fourth))
        self.engines.choose('no-existe')
        self.assertEqual(self.shown(), 3)
        await self.finish(keys[0], keys[1], fourth)

    async def test_a_screen_the_person_controls_is_not_taken_away_and_is_left_only_with_its_own_lease(self):
        first = self.start(CAFE, 'A1')
        second = self.start(PAN, 'B1')
        await self.inside(first, second)
        main, helper = self.engines.all[:2]
        await main.viewer.show(open_page(), 'Portal de prueba', manual=True)
        lease = await main.viewer.take_control(main.viewer.revision)
        self.assertEqual(self.shown(), 1)
        await self.person_starts_to_wait(second)
        self.assertEqual(self.shown(), 1)       # The person is acting on the first screen.
        with self.assertRaisesRegex(ValueError, 'Devuelve el control'):
            await self.engines.show(job_id=second)
        with self.assertRaisesRegex(ValueError, 'Devuelve el control'):
            await self.engines.show(job_id=second, channel='otro-permiso')
        self.assertEqual((self.shown(), main.viewer.channel), (1, lease))
        await self.engines.show(job_id=second, channel=lease)
        self.assertEqual((self.shown(), main.viewer.channel), (2, ''))
        # A lease nobody used for a long time holds nothing.
        lease = await main.viewer.take_control(main.viewer.revision)
        await self.engines.show(job_id=first, channel='')
        self.assertEqual(self.shown(), 1)
        main.viewer.channel_activity -= 10_000
        await self.engines.show(job_id=second)
        self.assertEqual(self.shown(), 2)
        await main.viewer.release(lease)
        await self.engines.continue_job(second, 'approve')
        await self.finish(first, second)

    async def test_a_revision_of_one_screen_never_fits_another(self):
        first = self.start(CAFE, 'A1')
        second = self.start(PAN, 'B1')
        await self.inside(first, second)
        main, helper = self.engines.all[:2]
        for viewer in (main.viewer, helper.viewer):
            await viewer.show(open_page(), 'Portal de prueba', manual=True)
        self.assertEqual(main.viewer.revision, helper.viewer.revision)   # The same number in both viewers.
        state = self.engines.browser_state()
        self.assertEqual(state['revision'], main.viewer.revision * SLOTS)
        self.assertEqual(self.engines.view_at(state['revision']), (main.viewer, main.viewer.revision))
        await self.engines.show(job_id=second)
        self.assertEqual(self.engines.view_at(state['revision']), (None, 0))   # The old number is of the other screen.
        other = self.engines.browser_state()
        self.assertEqual(other['revision'], helper.viewer.revision * SLOTS + 1)
        self.assertNotEqual(other['revision'], state['revision'])
        self.assertEqual(self.engines.view_at(other['revision']), (helper.viewer, helper.viewer.revision))
        self.assertEqual(self.engines.view_at(-1), (None, 0))
        # The tools of the page in front follow the engine shown, with the number the panel has.
        self.assertIs(self.engines.viewer.page, helper.viewer.page)
        self.assertEqual(self.engines.viewer.revision, other['revision'])
        await self.finish(first, second)

    async def test_the_lease_of_any_screen_is_given_back_by_its_own_engine(self):
        first = self.start(CAFE, 'A1')
        second = self.start(PAN, 'B1')
        await self.inside(first, second)
        helper = self.engines.helpers[0]
        await helper.viewer.show(open_page(), 'Portal de prueba', manual=True)
        lease = await helper.viewer.take_control(helper.viewer.revision)
        await self.engines.release_control('permiso-de-nadie')
        self.assertEqual(helper.viewer.channel, lease)
        await self.engines.release_control(lease)
        self.assertEqual(helper.viewer.channel, '')
        lease = await helper.viewer.take_control(helper.viewer.revision)
        await self.engines.abandon(lease)
        self.assertEqual(helper.viewer.channel, '')
        await self.finish(first, second)


class RestTests(EnginesCase):
    """A helper's browser closes after a while without work; the test moves the clock itself."""

    def open_engines(self):
        self.now = 1000.0
        self.store = Store(self.root)
        self.engines = Engines(Runner(self.store, self.plan), 3, clock=lambda: self.now)
        self.addAsyncCleanup(self.engines.close)

    @staticmethod
    def with_browser(engine):
        engine.context = Mock(close=AsyncMock(), pages=[])
        engine.playwright = Mock(stop=AsyncMock())
        return engine.context, engine.playwright

    async def test_a_helper_at_rest_closes_its_browser_after_a_minute_and_the_main_one_never(self):
        main, helper, other = self.engines.all
        opened = {engine: self.with_browser(engine) for engine in self.engines.all}
        self.assertEqual(await self.engines.rest_helpers(), 0)
        self.now += 59
        helper.active_id = 'un-ticket'            # A ticket arrives at one of them: its rest starts over.
        self.assertEqual(await self.engines.rest_helpers(), 0)
        self.now += 2
        self.assertEqual(await self.engines.rest_helpers(), 1)
        self.assertEqual((other.context, other.playwright, other.engine), (None, None, ''))
        opened[other][0].close.assert_awaited_once()
        opened[other][1].stop.assert_awaited_once()
        self.assertIs(helper.context, opened[helper][0])
        helper.active_id = None
        self.assertEqual(await self.engines.rest_helpers(), 0)
        self.now += 59
        self.assertEqual(await self.engines.rest_helpers(), 0)
        self.now += 1
        self.assertEqual(await self.engines.rest_helpers(), 1)
        self.assertIsNone(helper.context)
        # The main engine keeps its browser as always, however long it rests.
        self.now += 100_000
        self.assertEqual(await self.engines.rest_helpers(force=True), 0)
        self.assertIs(main.context, opened[main][0])
        opened[main][0].close.assert_not_awaited()

    async def test_a_page_kept_to_read_a_result_is_protected_longer_and_its_ticket_is_told(self):
        helper = self.engines.helpers[0]
        context, driver = self.with_browser(helper)
        key, _ = self.ticket(CAFE, 'A1')
        helper.page_job[FakePage()] = key
        self.assertEqual(await self.engines.rest_helpers(), 0)
        self.now += self.engines.idle_grace + 1
        self.assertEqual(await self.engines.rest_helpers(), 0)
        self.assertIs(helper.context, context)
        self.now += self.engines.parked_grace
        self.assertEqual(await self.engines.rest_helpers(), 1)
        self.assertIsNone(helper.context)
        last = self.store.events(key)[-1]
        self.assertEqual((last['kind'], last['detail']), ('browser_closed', motores.RESTED))

    async def test_a_browser_in_the_person_s_hands_or_a_driver_left_alone(self):
        helper, other = self.engines.helpers
        self.with_browser(helper)
        await helper.viewer.show(open_page(), 'Portal de prueba', manual=True)
        lease = await helper.viewer.take_control(helper.viewer.revision)
        self.assertEqual(await self.engines.rest_helpers(force=True), 0)   # The person controls that page.
        await helper.viewer.release(lease)
        self.assertEqual(await self.engines.rest_helpers(force=True), 1)
        # A browser that closed by itself leaves its driver running: it is stopped too.
        other.playwright = driver = Mock(stop=AsyncMock())
        self.assertEqual(await self.engines.rest_helpers(force=True), 1)
        driver.stop.assert_awaited_once()
        self.assertIsNone(other.playwright)


class ChatTests(EnginesCase):
    limit = 3

    async def asyncSetUp(self):
        await super().asyncSetUp()
        self.clock = SimpleNamespace(now=1000.0)
        patcher = patch('facturador.runner.time', SimpleNamespace(monotonic=lambda: self.clock.now))
        patcher.start()
        self.addCleanup(patcher.stop)
        self.keys = [self.ticket(origin, folio)[0] for origin, folio in ((CAFE, 'A1'), (PAN, 'B1'), (FLOR, 'C1'))]
        for engine, key in zip(self.engines.all, self.keys):
            engine.meters[key] = Meter()

    def questions(self):
        return [message['text'] for message in self.store.chat_messages() if message['intent'] == 'question']

    async def ask(self, number, question, **options):
        engine, key = self.engines.all[number], self.keys[number]
        return asyncio.create_task(engine.ask_chat(key, question, **options))

    async def settle(self):
        for _ in range(30):
            await asyncio.sleep(0)

    async def test_two_questions_of_two_tickets_do_not_cross_the_second_waits_for_the_first(self):
        first, second = self.keys[:2]
        asking_first = await self.ask(0, '¿Qué sucursal uso?')
        await self.until(lambda: self.engines.open_question() == first)
        asking_second = await self.ask(1, '¿Cuál es el número de caja?')
        await self.settle()
        # Only the first one is in the conversation: the second has not been asked yet.
        self.assertEqual(len(self.questions()), 1)
        self.assertIn('¿Qué sucursal uso?', self.questions()[0])
        self.assertEqual(self.engines.open_question(), first)
        self.assertNotIn(second, self.engines.helpers[0].questions)
        self.assertNotIn('question', [event['kind'] for event in self.store.events(second)])
        self.clock.now += 50
        self.engines.answer_question(second, 'no es para ti')   # Nothing is open for that ticket.
        self.engines.answer_question(first, 'La del centro')
        self.assertEqual(await asking_first, 'La del centro')
        await self.until(lambda: self.engines.open_question() == second)
        self.assertEqual(len(self.questions()), 2)
        self.assertIn('¿Cuál es el número de caja?', self.questions()[1])
        self.clock.now += 30
        self.engines.answer_question(second, 'La 4')
        self.assertEqual(await asking_second, 'La 4')
        self.assertIsNone(self.engines.open_question())
        # Neither the turn nor the person's time is charged to the attempts.
        self.assertEqual(self.engines.main.meters[first].waited, 50)
        self.assertEqual(self.engines.helpers[0].meters[second].waited, 80)
        self.assertEqual(self.engines.helpers[0].meters[second].active(), 0)

    async def test_a_secret_question_takes_its_turn_too_and_its_code_reaches_only_its_ticket(self):
        first, second = self.keys[:2]
        asking_first = await self.ask(0, '¿Qué sucursal uso?')
        await self.until(lambda: self.engines.open_question() == first)
        asking_code = await self.ask(1, 'El portal mandó un código a tu teléfono.', secreto=True)
        await self.settle()
        self.assertFalse(self.engines.secret_open(second))
        self.assertIsNone(self.engines.secret_recent())
        self.engines.answer_question(first, 'La del centro')
        await asking_first
        await self.until(lambda: self.engines.open_question() == second)
        self.assertTrue(self.engines.secret_open(second))
        self.assertFalse(self.engines.secret_open(first))
        self.assertEqual(self.engines.secret_recent(), second)
        self.engines.answer_question(second, '482913')
        self.assertEqual(await asking_code, '482913')
        self.assertFalse(self.engines.secret_open(second))
        self.assertEqual(self.engines.secret_recent(), second)   # Still a secret for a while.

    async def test_a_ticket_that_stopped_for_its_answer_is_answered_when_no_other_question_is_open(self):
        first, second, third = self.keys
        stopped = self.engines.all[2]
        answer = asyncio.get_running_loop().create_future()
        stopped.questions[third] = answer
        stopped.pending[third] = {'kind': 'unknown', 'future': asyncio.get_running_loop().create_future(), 'page': None}
        self.assertEqual(self.engines.open_question(), third)
        asking = await self.ask(0, '¿Qué sucursal uso?')
        await self.until(lambda: self.engines.open_question() == first)   # The question just asked goes first.
        self.engines.answer_question(first, 'La del centro')
        await asking
        self.assertEqual(self.engines.open_question(), third)
        self.engines.answer_question(third, 'Sí, ese es')
        self.assertEqual(answer.result(), 'Sí, ese es')
        self.assertIsNone(self.engines.open_question())
        stopped.pending.clear()
        stopped.questions.clear()

    async def test_the_chat_of_the_panel_answers_the_open_question_and_tells_the_ticket_shown(self):
        first, second = self.keys[:2]
        plan = Mock()
        plan.status.return_value = {'status': 'ready'}
        plan.infer.return_value = ChatDecision(intent='conversation', reply='Dime en qué te ayudo.')
        app = FastAPI()
        app.include_router(chat_router(self.store, plan, Mock(), self.engines, lambda: PROFILE))
        client = httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url='http://panel.prueba')
        self.addAsyncCleanup(client.aclose)
        sent = iter(range(1, 100))

        async def say(text):
            reply = await client.post('/api/chat', data={'text': text, 'client_id': f'mensaje_{next(sent):016d}'})
            self.assertEqual(reply.status_code, 200, reply.text)
            return reply.json()

        asking_first = await self.ask(0, '¿Qué sucursal uso?')
        await self.until(lambda: self.engines.open_question() == first)
        asking_code = await self.ask(1, 'El portal mandó un código a tu teléfono.', secreto=True)
        body = await say('La del centro')
        self.assertEqual((body['intent'], body['job_ids']), ('answer', [first]))
        self.assertEqual(await asking_first, 'La del centro')
        await self.until(lambda: self.engines.open_question() == second)
        body = await say('482913')
        self.assertEqual((body['intent'], body['job_ids']), ('answer', [second]))
        self.assertEqual([message['text'] for message in body['messages']], [SECRET_KEPT, SECRET_THANKS])
        self.assertEqual(await asking_code, '482913')
        # A loose message goes to the ticket at work that the panel shows.
        for engine, key in zip(self.engines.all, self.keys):
            task = asyncio.ensure_future(asyncio.sleep(3600))
            engine.active_id, engine.job_tasks[key] = key, task
            self.addCleanup(task.cancel)
        self.assertEqual(self.engines.running_job(), first)
        await self.engines.show(job_id=second)
        self.assertEqual(self.engines.running_job(), second)
        body = await say('Usa la tarjeta que termina en 1234')
        self.assertEqual((body['intent'], body['job_ids']), ('note', [second]))
        self.assertEqual(self.engines.helpers[0].live_notes[second], ['Usa la tarjeta que termina en 1234'])
        self.assertEqual(self.engines.main.live_notes, {})
        for engine in self.engines.all:
            engine.active_id = None
            engine.job_tasks.clear()


class QueueTests(EnginesCase):
    """The inbox queue with several engines: a ready ticket starts as soon as one of them can take it."""

    async def asyncSetUp(self):
        await super().asyncSetUp()
        self.worker = TicketAutomation(self.store, self.engines.main, self.plan, lambda: PROFILE, engines=self.engines)
        self.worker.enabled = True
        self.addAsyncCleanup(self.worker.close)
        confirmed = patch('facturador.automation.portal_confirmed', return_value=True)
        self.confirmed = confirmed.start()
        self.addCleanup(confirmed.stop)

    async def rounds(self, limit=12):
        for _ in range(limit):
            if not await self.worker.step():
                break

    def prepared(self):
        """The tickets the queue looked at in full (their originals are read to confirm the portal)."""
        looked = [call.args[1]['id'] for call in self.confirmed.call_args_list]
        self.confirmed.reset_mock()
        return looked

    async def test_behind_the_oldest_ticket_only_those_that_can_start_now_are_prepared(self):
        busy = [self.start(origin, folio) for origin, folio in ((CAFE, 'A1'), (PAN, 'B1'), (FLOR, 'C1'))]
        await self.inside(*busy)
        same, other, another = (self.ticket(origin, folio)[0] for origin, folio in ((CAFE, 'A2'), (MIEL, 'D1'), (MIEL + '.mx', 'E1')))
        self.prepared()
        await self.rounds()
        # Every engine is busy: the oldest says why it waits and the ones behind it are left alone.
        self.assertEqual(self.prepared(), [same])
        self.assertEqual(self.worker.status()['message'], SAME_PORTAL)
        await self.finish(busy[1])
        await self.rounds()
        # One engine is free: the first ticket whose portal is not busy takes it, and only that one.
        self.assertEqual(self.prepared(), [same, other, same])
        await self.inside(other)
        self.assertEqual([self.store.get(key)['status'] for key in (same, another)], ['ready', 'ready'])
        await self.finish(busy[0], busy[2], other)
        await self.rounds()
        await self.inside(same, another)
        await self.finish(same, another)

    async def test_ready_tickets_start_without_waiting_for_the_one_before_and_the_same_portal_waits_its_turn(self):
        tickets = [self.ticket(origin, folio)[0] for origin, folio in
                   ((CAFE, 'A1'), (CAFE, 'A2'), (PAN, 'B1'), (FLOR, 'C1'), (MIEL, 'D1'))]
        first, same, second, third, fourth = tickets
        await self.rounds()
        await self.inside(first, second, third)
        self.assertEqual([self.store.get(key)['status'] for key in tickets],
                         ['running', 'ready', 'running', 'running', 'ready'])
        self.assertEqual(sorted(self.engine_of(key) for key in (first, second, third)), [1, 2, 3])
        # Nothing of the two that wait was touched: they are ready, not queued twice nor lost.
        self.assertEqual(self.engines.in_progress(), [first, second, third])
        await self.rounds()
        self.assertEqual(self.worker.status()['message'], 'Esperando a que termine otro ticket del mismo portal.')
        self.assertEqual(self.worker.status()['current_id'], same)
        await self.finish(second)
        await self.rounds()
        await self.inside(fourth)             # The free engine goes to the next portal that is not busy.
        self.assertEqual(self.store.get(same)['status'], 'ready')
        self.assertEqual(self.engine_of(fourth), self.engine_of(second))
        await self.finish(first)
        await self.rounds()
        await self.inside(same)
        self.assertEqual(self.engine_of(same), self.engine_of(first))
        await self.finish(third, fourth, same)
        await self.rounds()
        self.assertEqual([moment for moment, _, key in self.log if key == same], ['in', 'out'])
        self.assertEqual(len([entry for entry in self.log if entry[0] == 'in']), 5)

    async def test_with_every_engine_busy_the_queue_says_so_and_schedules_nothing(self):
        busy = [self.start(origin, folio) for origin, folio in ((CAFE, 'A1'), (PAN, 'B1'), (FLOR, 'C1'))]
        await self.inside(*busy)
        waiting = self.ticket(MIEL, 'D1')[0]
        await self.rounds()
        self.assertEqual(self.store.get(waiting)['status'], 'ready')
        status = self.worker.status()
        self.assertEqual((status['phase'], status['current_id'], status['message']),
                         ('waiting_browser', waiting, 'Esperando a que se libere el navegador.'))
        await self.finish(*busy)
        await self.rounds()
        await self.inside(waiting)
        await self.finish(waiting)

    async def test_the_status_makes_sense_with_several_in_progress(self):
        keys = [self.start(origin, folio) for origin, folio in ((CAFE, 'A1'), (PAN, 'B1'))]
        self.asks_person.add(keys[1])
        await self.inside(*keys)
        await self.until(lambda: self.engines.waiting_kind(keys[1]))
        await self.rounds()
        status = self.worker.status()
        self.assertEqual((status['phase'], status['current_id'], status['message']),
                         ('queued', None, '2 tickets en trámite a la vez. Uno espera tu ayuda.'))
        await self.engines.continue_job(keys[1], 'approve')
        await self.finish(keys[1])
        await self.rounds()
        status = self.worker.status()
        self.assertEqual((status['phase'], status['current_id']), ('queued', keys[0]))
        await self.finish(keys[0])
        await self.rounds()
        self.assertEqual(self.worker.status()['phase'], 'idle')

    async def test_a_recovery_that_is_due_runs_in_a_free_engine_while_another_ticket_waits_for_the_person(self):
        waiting = self.start(CAFE, 'A1')
        self.asks_person.add(waiting)
        await self.inside(waiting)
        due, _ = self.ticket(PAN, 'B1')
        self.store.queue(due, PROFILE, PAN + '/')
        self.store.update(due, status='uncertain', message='Resultado pendiente.')
        calls = []

        async def retry_job(runner, key, current_profile=None, *, automatic=False):
            calls.append((self.number(runner), key, automatic))
            return {'id': key, 'status': 'uncertain', 'action': 'recovering', 'message': 'Consulta solicitada.'}

        with patch.object(Runner, 'retry_job', retry_job):
            await self.rounds(3)
        self.assertEqual(calls, [(2, due, True)])
        await self.engines.continue_job(waiting, 'approve')
        await self.finish(waiting)


class PanelTests(EnginesCase):
    """The routes of the panel with three engines: each request reaches the engine that has its ticket."""

    def open_engines(self):
        with patch.dict(os.environ, {SETTING: '3', 'FACTURADOR_MODO': 'local'}):
            self.app = create_app(self.root)
        self.store, self.engines = self.app.state.store, self.app.state.engines
        self.addAsyncCleanup(self.engines.close)
        self.addCleanup(self.app.state.plan.close)
        ready = patch.object(self.app.state.plan, 'status', return_value={'status': 'ready', 'message': ''})
        ready.start()
        self.addCleanup(ready.stop)
        write_private(self.root / 'profile.json', PROFILE.model_dump())
        origin = 'http://127.0.0.1:8765'
        self.client = httpx.AsyncClient(transport=httpx.ASGITransport(app=self.app), base_url=origin,
                                        headers={'X-App-Token': self.app.state.csrf_token, 'Origin': origin})
        self.addAsyncCleanup(self.client.aclose)

    async def state(self):
        reply = await self.client.get('/api/state')
        self.assertEqual(reply.status_code, 200, reply.text)
        return reply.json()

    async def post(self, path, body=None, status=200):
        reply = await self.client.post(path, json=body or {})
        self.assertEqual(reply.status_code, status, reply.text)
        return reply

    async def test_the_state_lists_the_engines_and_each_request_reaches_the_engine_of_its_ticket(self):
        self.assertEqual((self.engines.limit, len(self.engines.all)), (3, 3))
        running = self.start(CAFE, 'A1')
        waiting = self.start(PAN, 'B1')
        self.asks_person.add(waiting)
        await self.inside(running, waiting)
        await self.until(lambda: self.engines.waiting_kind(waiting))
        state = await self.state()
        self.assertEqual(state['active_job'], waiting)   # The one that waits for the person is the one in front.
        self.assertEqual(state['engines'], [{'n': 1, 'job_id': running, 'waiting': False, 'shown': False},
                                            {'n': 2, 'job_id': waiting, 'waiting': True, 'shown': True}])
        rows = {row['id']: row for row in state['jobs']}
        self.assertEqual((rows[waiting]['human_kind'], rows[running]['human_kind']), ('issue', None))
        self.assertEqual(state['browser']['revision'] % SLOTS, 1)
        # The person picks what the viewer shows, by ticket or by engine.
        shown = (await self.post('/api/browser/focus', {'job_id': running})).json()
        self.assertEqual(shown['revision'] % SLOTS, 0)
        self.assertEqual((await self.state())['active_job'], running)
        await self.post('/api/browser/focus', {'engine': 2})
        self.assertEqual((await self.state())['active_job'], waiting)
        for body in ({'job_id': 'no-existe'}, {'engine': 4}, {}):
            reply = await self.post('/api/browser/focus', body, 422)
            self.assertIn('ya no está en un navegador', reply.text)
        # A decision goes to the engine that has the ticket; the other ticket is not touched by it.
        reply = await self.post(f'/api/jobs/{running}/decision', {'decision': 'approve'}, 422)
        self.assertIn('ya no está activa', reply.text)
        await self.post(f'/api/jobs/{waiting}/decision', {'decision': 'approve'})
        await self.until(lambda: not self.engines.waiting_kind(waiting))
        self.assertEqual((await self.state())['active_job'], waiting)   # The ticket just approved stays in front.
        # So does stopping it.
        await self.post(f'/api/jobs/{waiting}/stop')
        self.assertIsNone(self.engines.owner(waiting))
        self.assertEqual(self.log[-1], ('out', 2, waiting))
        reply = await self.post(f'/api/jobs/{waiting}/stop', status=422)
        self.assertIn('ya no está en ejecución', reply.text)
        self.assertIs(self.engines.owner(running), self.engines.main)
        await self.finish(running)
        state = await self.state()
        self.assertEqual((state['active_job'], state['engines']),
                         (None, [{'n': 1, 'job_id': None, 'waiting': False, 'shown': True}]))

    async def test_a_ticket_started_from_the_panel_goes_to_a_free_engine_and_is_the_one_shown(self):
        busy = self.start(CAFE, 'A1')
        await self.inside(busy)
        key, receipt = self.ticket(PAN, 'B1')
        consent = {'consent': True, 'auto_issue': True}
        await self.post(f'/api/jobs/{key}/run', {**consent, 'confirmed_url': receipt.portal_url})
        await self.inside(key)
        self.assertEqual(self.engine_of(key), 2)
        self.assertEqual((await self.state())['active_job'], key)
        third = self.start(FLOR, 'C1')
        await self.inside(third)
        waiting, receipt = self.ticket(MIEL, 'D1')
        await self.post(f'/api/jobs/{waiting}/run', {**consent, 'confirmed_url': receipt.portal_url})
        self.assertEqual(self.status(waiting), ('queued', 'En cola'))
        state = await self.state()
        self.assertIn(waiting, [row['id'] for row in state['jobs']])
        # In the line its files are not touched, and it can be taken out before it starts.
        reply = await self.post(f'/api/jobs/{waiting}/recover-local', status=422)
        self.assertIn('Espera a que termine el trámite abierto', reply.text)
        reply = await self.post(f'/api/jobs/{waiting}/run', {**consent, 'confirmed_url': receipt.portal_url}, 422)
        self.assertIn('Solo se puede encolar', reply.text)
        await self.post(f'/api/jobs/{waiting}/stop')
        self.assertEqual(self.status(waiting), ('ready', 'Retirado de la cola.'))
        await self.finish(busy, key, third)
        self.assertNotIn(waiting, self.started)

    async def test_control_is_taken_on_the_screen_shown_and_a_number_of_another_screen_takes_nothing(self):
        first = self.start(CAFE, 'A1')
        second = self.start(PAN, 'B1')
        await self.inside(first, second)
        main, helper = self.engines.all[:2]
        for engine in (main, helper):
            page = open_page()
            engine.page_job[page] = engine.active_id
            await engine.viewer.show(page, 'Portal de prueba', manual=True)
        stopped = asyncio.create_task(self.person(helper, second, 'blocked', helper.viewer.page))
        await self.until(lambda: self.engines.waiting_kind(second) == 'blocked')
        state = await self.state()
        browser = state['browser']
        self.assertEqual((state['active_job'], browser['job_id'], browser['mode'], browser['manual']),
                         (second, second, 'job', True))
        self.assertEqual(main.viewer.revision, helper.viewer.revision)
        for stale in (main.viewer.revision * SLOTS, helper.viewer.revision, browser['revision'] + SLOTS):
            await self.post('/api/browser/control', {'revision': stale}, 422)
        self.assertEqual((main.viewer.channel, helper.viewer.channel), ('', ''))
        self.assertFalse(self.store.get(second)['human_exposed'])
        lease = (await self.post('/api/browser/control', {'revision': browser['revision']})).json()['channel']
        self.assertEqual((main.viewer.channel, helper.viewer.channel), ('', lease))
        self.assertTrue(self.store.get(second)['human_exposed'])
        self.assertFalse(self.store.get(first)['human_exposed'])
        # The frames of another screen's number are not served either.
        frame = await self.client.get('/api/browser/frame', params={'revision': main.viewer.revision * SLOTS})
        self.assertEqual((frame.status_code, frame.content), (204, b''))
        # While the person holds that screen nothing takes the panel elsewhere without the lease.
        reply = await self.post('/api/browser/focus', {'job_id': first}, 422)
        self.assertIn('Devuelve el control', reply.text)
        reply = await self.post('/api/browser/open', {'provider': next(iter(CATALOG))}, 422)
        self.assertIn('Devuelve el control', reply.text)
        self.assertEqual((await self.state())['active_job'], second)
        await self.post('/api/browser/release', {'channel': lease})
        self.assertEqual(helper.viewer.channel, '')
        await self.post('/api/browser/focus', {'job_id': first})
        self.assertEqual((await self.state())['active_job'], first)
        helper.pending[second]['future'].set_result('resume')
        await stopped
        await self.finish(first, second)

    async def test_a_portal_with_a_ticket_in_another_engine_is_not_opened_by_hand_in_the_main_browser(self):
        first = self.start(CAFE, 'A1')
        second = self.start(PAN, 'B1')
        await self.inside(first, second)
        await self.finish(first)   # The main browser is free; a helper still works on the second portal.
        opened = AsyncMock()
        with patch.object(self.engines.main, 'open_portal', opened):
            reply = await self.post('/api/browser/open', {'url': PAN + '/facturar', 'confirmed_origin': PAN}, 422)
            self.assertIn('Hay un trámite en curso en ese portal', reply.text)
            opened.assert_not_awaited()
            await self.post('/api/browser/open', {'url': FLOR + '/facturar', 'confirmed_origin': FLOR})
            opened.assert_awaited_once_with(None, FLOR + '/facturar', FLOR)
            await self.post('/api/browser/open')   # The list of portals is nobody's portal.
            self.assertEqual(opened.await_count, 2)
        await self.finish(second)

    async def test_a_retry_from_the_panel_goes_to_the_engine_that_kept_the_page_of_the_ticket(self):
        key, _ = self.ticket(CAFE, 'A1')
        self.store.update(key, status='uncertain', message='Resultado pendiente.')
        self.engines.helpers[1].page_job[FakePage()] = key
        calls = []

        async def retry_job(runner, ticket, current_profile=None, *, automatic=False):
            calls.append((self.number(runner), ticket, current_profile().rfc, automatic))
            return {'id': ticket, 'status': 'uncertain', 'action': 'recovering', 'message': 'Consulta solicitada.'}

        with patch.object(Runner, 'retry_job', retry_job):
            reply = await self.post(f'/api/jobs/{key}/retry')
        self.assertEqual(reply.json()['action'], 'recovering')
        self.assertEqual(calls, [(3, key, PROFILE.rfc, False)])


class AccountsModeTests(unittest.IsolatedAsyncioTestCase):
    async def test_the_space_of_each_account_keeps_one_engine_whatever_the_setting_asks(self):
        folder = tempfile.TemporaryDirectory()
        self.addCleanup(folder.cleanup)
        # The folder goes away only after the worker threads of the instance are done (Windows).
        self.addAsyncCleanup(asyncio.get_running_loop().shutdown_default_executor)
        use_master_key(self)
        root = Path(folder.name)
        with patch.dict(os.environ, {SETTING: '3', 'FACTURADOR_MODO': 'usuarios'}):
            instances = Instances(root, quiet_app, port=8765, public_origin=ORIGIN, clock=FakeClock())
            self.addAsyncCleanup(instances.close)
            spaces = []
            for owner in ('00000000000000a1', '00000000000000b2'):
                instance = await instances.enter(owner, '/api/state')
                instances.leave(instance)
                spaces.append(instance)
        for instance in spaces:
            engines = instance.app.state.engines
            self.assertEqual((engines.limit, engines.helpers, engines.several), (1, [], False))
            self.assertIs(engines.main, instance.runner)
            self.assertIsNone(instance.runner.dispatch)
            self.assertIsNone(instance.runner.chat_turn)
            self.assertIsNone(instance.app.state.automation.engines)
        # The limit of browsers there is the one shared among the accounts.
        self.assertEqual(instances.browsers.limit, 3)


class StartupTests(unittest.IsolatedAsyncioTestCase):
    async def test_starting_with_three_engines_relaunches_no_ticket_that_already_existed(self):
        folder = tempfile.TemporaryDirectory()
        self.addCleanup(folder.cleanup)
        self.addAsyncCleanup(asyncio.get_running_loop().shutdown_default_executor)
        root = Path(folder.name)
        store = Store(root)
        keys = []
        for state, origin, folio in (('queued', CAFE, 'A1'), ('running', PAN, 'B1'), ('needs_user', FLOR, 'C1')):
            key = store.add(folio + '.png', 'foto-' + folio)[0]['id']
            receipt = receipt_at(origin, folio)
            store.save_receipt(key, receipt, ready=True)
            self.assertTrue(store.queue(key, PROFILE, receipt.portal_url))
            if state != 'queued':
                self.assertTrue(store.claim(key))
            if state == 'needs_user':
                store.enter_intervention(key, 'issue', 'Aprueba la emisión.', False)
            self.assertEqual(store.get(key)['status'], state)
            keys.append(key)
        write_private(root / 'profile.json', PROFILE.model_dump())
        write_private(root / 'automation.json', {'enabled': True, 'plan_read_after': 0})
        # Outside the container the program opens no browser by itself when it starts; inside it the
        # main engine opens the personal browser, which says nothing about tickets.
        with patch.dict(os.environ, {SETTING: '3', 'FACTURADOR_MODO': 'local', 'FACTURADOR_CONTAINER': ''}):
            app = create_app(root)
        self.addCleanup(app.state.plan.close)
        engines, automation = app.state.engines, app.state.automation
        started = []

        async def in_portal(runner, key, profile, auto_issue=False, manual=False):
            started.append(key)

        with patch.object(Runner, '_run', in_portal), \
                patch.object(app.state.plan, 'status', return_value={'status': 'ready', 'message': ''}), \
                patch('facturador.automation.portal_confirmed', return_value=True):
            async with app.router.lifespan_context(app):
                self.assertTrue(automation.enabled)
                for _ in range(6):
                    await automation.step()
                    await asyncio.sleep(0)
                self.assertEqual(started, [])
                self.assertFalse(engines.has_active_jobs())
                self.assertEqual([len(engine.job_tasks) for engine in engines.all], [0, 0, 0])
                self.assertTrue(all(engine.context is None for engine in engines.all))
                for key in keys:
                    row = store.get(key)
                    self.assertEqual((row['status'], row['auto_stage']), ('ready', 'review'))
                    self.assertIn('Trámite interrumpido antes de enviar', row['message'])
            self.assertTrue(engines.closing)
        self.assertEqual(started, [])


class WithoutHelpersTests(EnginesCase):
    limit = 1

    async def test_with_one_engine_everything_is_one_after_another_in_the_main_one(self):
        self.assertEqual((self.engines.helpers, self.engines.all), ([], [self.engines.main]))
        keys = [self.start(origin, folio) for origin, folio in ((CAFE, 'A1'), (PAN, 'B1'), (CAFE, 'A2'))]
        await self.inside(keys[0])
        for _ in range(20):
            await asyncio.sleep(0)
        # Queued behind the lock of the only engine, exactly as before: nothing waits in the coordinator.
        self.assertEqual(set(self.engines.main.job_tasks), set(keys))
        self.assertEqual([self.status(key) for key in keys[1:]], [('queued', 'En cola')] * 2)
        self.assertFalse(self.event(self.started, keys[1]).is_set())
        self.assertEqual((self.engines.focused(), self.engines.active_id), (self.engines.main, keys[0]))
        for key in keys:
            await self.inside(key)
            self.event(self.leave, key).set()
        await self.until(lambda: not self.engines.has_active_jobs())
        self.assertEqual(self.log, [(moment, 1, key) for key in keys for moment in ('in', 'out')])
        await self.engines.show(job_id='lo-que-sea')   # Nothing to choose with a single engine.
        self.assertEqual(self.engines.waits_for(receipt_at(CAFE, 'A9').model_dump()), '')


class MailTests(unittest.TestCase):
    def test_the_mail_search_leaves_alone_every_ticket_a_browser_is_working_on(self):
        with tempfile.TemporaryDirectory() as folder:
            store = Store(Path(folder))
            keys = []
            for folio in ('A1', 'B1', 'C1'):
                key = store.add(folio + '.png', 'foto-' + folio)[0]['id']
                receipt = receipt_at(CAFE, folio)
                store.save_receipt(key, receipt, ready=True)
                store.queue(key, PROFILE, receipt.portal_url)
                store.confirm_issued(key)   # The portal confirmed the invoice: its files are searched in the mail.
                keys.append(key)
            for active, expected in ((lambda: None, keys), (lambda: keys[0], keys[1:]), (lambda: [], keys),
                                     (lambda: [keys[0], keys[2]], [keys[1]]), (lambda: keys, [])):
                with self.subTest(active=active()):
                    worker = InvoiceMailRecovery(store, lambda: True, active, mailboxes=lambda: [])
                    with store.connect() as db:
                        db.execute('DELETE FROM invoice_mail_checks')
                    self.assertEqual(sorted(row['id'] for row in worker.due(int(time.time()))), sorted(expected))


if __name__ == '__main__':
    unittest.main()
