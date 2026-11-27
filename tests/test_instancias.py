"""Life of each customer's instance and the shared limit of open browsers. No real browser is launched."""
import asyncio
import os
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

from fastapi.testclient import TestClient

from acceso_dobles import ORIGIN, FakeClock, use_master_key
from facturador import capufe_recovery
from facturador.app import create_app
from facturador.capufe_recovery import _lookup_browser
from facturador.catalog import CUSTOM_PORTAL
from facturador.instancias import BUSY, CLOSING, TURN, WAITING, YIELDED, BrowserPool, Instances, Unavailable
from facturador.models import Profile, Receipt
from facturador.runner import Runner

ANA, BETO, CARLA = 'a' * 16, 'b' * 16, 'c' * 16
PROFILE = Profile(rfc='XAXX010101000', name='PRUEBA AUTOMATIZACION', postal_code='01000', regime='612',
                  cfdi_use='G03', email='prueba@example.com')


def receipt(number):
    return Receipt(provider=CUSTOM_PORTAL, portal_url='https://facturacion.comercio.example.com/', merchant='COMERCIO DE PRUEBA',
                   amount='100.00', date='2026-11-13', ticket_number=f'T-{number}', payment_code='01')


class InstancesCase(unittest.IsolatedAsyncioTestCase):
    limit = 3

    async def asyncSetUp(self):
        folder = tempfile.TemporaryDirectory()
        self.addCleanup(folder.cleanup)
        # A watcher hands its look at the database to a thread, and that thread may outlive the task
        # that asked for it. The folder is removed only once every such thread is done: Windows
        # refuses to delete a database that is still open. (Cleanups run last one first.)
        self.addAsyncCleanup(asyncio.get_running_loop().shutdown_default_executor)
        self.root = Path(folder.name)
        use_master_key(self)
        self.clock = FakeClock(1000.0)
        self.made = []
        self.pool = BrowserPool(self.limit, clock=self.clock, poll=.01)
        self.instances = Instances(self.root, self.factory, port=8765, public_origin=ORIGIN, idle_seconds=1800,
                                   clock=self.clock, pool=self.pool)
        self.addAsyncCleanup(self.instances.close)
        self.numbers = iter(range(1, 1000))

    def factory(self, root, **options):
        app = create_app(root, **options)
        self.made.append((root, options, app))
        return app

    async def use(self, owner, path='/api/state'):
        """One request going in and out."""
        instance = await self.instances.enter(owner, path)
        self.instances.leave(instance)
        return instance

    def ticket(self, instance, status='ready', **columns):
        number = next(self.numbers)
        store = instance.app.state.store
        row, _ = store.add(f'ticket-{number}.png', f'hash-{instance.owner}-{number}')
        store.save_receipt(row['id'], receipt(number), ready=True)
        columns['status'] = status
        with store.connect() as db:
            db.execute('UPDATE jobs SET ' + ','.join(f'{name}=?' for name in columns) + ' WHERE id=?', (*columns.values(), row['id']))
        return row['id']

    @staticmethod
    def running(instance) -> bool:
        state = instance.app.state
        return bool(state.automation.task and not state.automation.task.done()
                    and state.whatsapp.watcher.task and not state.whatsapp.watcher.task.done())


class LifeTests(InstancesCase):
    async def test_instance_is_created_on_first_use_with_its_own_folder_and_lifespan(self):
        self.assertIsNone(self.instances.live(ANA))
        self.assertFalse((self.root / 'usuarios').exists())
        ana = await self.use(ANA)
        self.assertEqual(ana.root, self.root / 'usuarios' / ANA)
        self.assertTrue((ana.root / 'facturador.sqlite').is_file())
        self.assertEqual(self.made[0][1], {'port': 8765, 'public_origin': ORIGIN, 'lazy_browser': True})
        self.assertEqual(ana.app.state.store.root, ana.root.resolve())
        self.assertTrue(self.running(ana))  # Its start-up ran: the queue and the watchers are alive.
        self.assertIs(await self.use(ANA), ana)
        self.assertEqual(len(self.made), 1)
        beto = await self.use(BETO)
        self.assertIsNot(beto.app, ana.app)
        self.assertNotEqual(beto.app.state.csrf_token, ana.app.state.csrf_token)
        self.assertEqual({path.name for path in (self.root / 'usuarios').iterdir()}, {ANA, BETO})

    async def test_owner_is_never_a_path(self):
        for owner in ('', 'ana', '../' + ANA, ANA + '/..', ANA.upper(), 'a' * 15, 'a' * 17, 'g' * 16, None, 7, ANA + '\n'):
            with self.subTest(owner=owner), self.assertRaises(Unavailable):
                await self.instances.enter(owner)
        self.assertFalse((self.root / 'usuarios').exists())
        self.assertEqual(self.made, [])

    async def test_browser_is_opened_only_when_it_is_needed(self):
        with patch.dict(os.environ, {'FACTURADOR_CONTAINER': '1'}), \
                patch.object(Runner, 'open_personal', new=AsyncMock(return_value={})) as opened:
            ana = await self.use(ANA)
            self.assertTrue(self.running(ana))
            opened.assert_not_awaited()
            self.assertFalse(ana.browser_open)
            self.assertEqual(self.pool.holders, {})
            # The local container keeps opening its browser at start, as before.
            with TestClient(create_app(self.root / 'local')) as client:
                self.assertEqual(client.get('/api/state').status_code, 200)
            opened.assert_awaited_once()

    async def test_rested_instance_closes_in_order_and_wakes_up_with_its_data(self):
        ana = await self.use(ANA)
        key = self.ticket(ana, 'verified')
        token = ana.app.state.csrf_token
        ana.runner.close = AsyncMock(wraps=ana.runner.close)
        self.clock.advance(1799)
        self.assertEqual(await self.instances.sweep(), [])
        self.clock.advance(2)
        self.assertEqual(await self.instances.sweep(), [ANA])
        self.assertIsNone(self.instances.live(ANA))
        ana.runner.close.assert_awaited_once()  # The application's own shutdown ran.
        self.assertFalse(self.running(ana))
        self.assertTrue(ana.app.state.automation.task.done())
        again = await self.use(ANA)
        self.assertIsNot(again, ana)
        self.assertEqual(again.app.state.store.get(key)['status'], 'verified')
        self.assertNotEqual(again.app.state.csrf_token, token)
        self.assertTrue(self.running(again))

    async def test_use_keeps_an_instance_awake(self):
        ana = await self.use(ANA)
        for _ in range(5):
            self.clock.advance(1000)
            await self.use(ANA)
            self.assertEqual(await self.instances.sweep(), [])
        held = await self.instances.enter(ANA)  # A request that has not finished yet.
        self.clock.advance(5000)
        self.assertEqual(await self.instances.sweep(), [])
        self.instances.leave(held)
        self.clock.advance(1800)
        self.assertEqual(await self.instances.sweep(), [ANA])
        self.assertFalse(self.running(ana))

    async def rests(self, prepare):
        """Create an instance, prepare it, let a long time pass and tell why it stays, and whether it was closed."""
        owner = f'{next(self.numbers):016x}'
        instance = await self.use(owner)
        cleanup = prepare(instance)
        self.clock.advance(100_000)
        reason = instance.work()
        closed = owner in await self.instances.sweep()
        if callable(cleanup):
            cleanup()
        return reason, closed

    async def test_instance_with_a_ticket_in_progress_or_pending_work_is_not_closed(self):
        recent = datetime.now(timezone.utc).isoformat()
        sleeping = []

        def job(instance):
            task = asyncio.ensure_future(asyncio.sleep(3600))
            sleeping.append(task)
            instance.runner.job_tasks['k'] = task

        def reading(instance):
            task = asyncio.ensure_future(asyncio.sleep(3600))
            sleeping.append(task)
            instance.app.state.automation.reads['k'] = task
            return lambda: instance.app.state.automation.reads.clear()

        def followed(instance):
            key = self.ticket(instance, 'external', updated='2026-01-01T00:00:00+00:00', issue_attempted=1)
            with instance.app.state.store.connect() as db:
                db.execute("INSERT INTO whatsapp_followups(job_id,enabled,next_check) VALUES(?,1,?)", (key, recent))

        def automatic(status, **columns):
            def prepare(instance):
                instance.app.state.automation.enabled = True
                self.ticket(instance, status, **columns)
            return prepare

        cases = {
            'ticket running in the browser': (job, 'tramite'),
            'step waiting for the person': (lambda i: i.runner.pending.update(k={'kind': 'captcha'}), 'tramite'),
            'active ticket': (lambda i: setattr(i.runner, 'active_id', 'k'), 'tramite'),
            'download in flight': (lambda i: i.runner.downloads.add('d') or i.runner.downloads.clear, 'tramite'),
            'person controlling the browser': (lambda i: setattr(i.runner.viewer, 'channel', 'canal'), 'navegador'),
            'dialog open in the browser': (lambda i: setattr(i.runner.viewer, 'dialog', object())
                                           or (lambda: setattr(i.runner.viewer, 'dialog', None)), 'navegador'),
            'ticket being read': (reading, 'lectura'),
            'chat upload in the queue': (lambda i: i.app.state.store.queue_chat_job(self.ticket(i, 'new')), 'cola'),
            'followed on WhatsApp': (followed, 'seguimiento'),
            'automatic queue with a new ticket': (automatic('new'), 'tickets'),
            'automatic queue with a ready ticket': (automatic('ready'), 'tickets'),
            'automatic retry of a read': (automatic('review', auto_stage='retry_read'), 'tickets'),
            'external request already sent': (lambda i: self.ticket(i, 'external', issue_attempted=1, updated=recent), 'tickets'),
            'state nobody knows yet': (lambda i: self.ticket(i, 'estado_nuevo'), 'tickets'),
        }
        for status in ('queued', 'running', 'issuing', 'needs_user', 'whatsapp_preparing'):
            cases['ticket ' + status] = (lambda i, status=status: self.ticket(i, status), 'tickets')
        for status in ('issued_waiting_files', 'uncertain'):
            cases['recent ' + status] = (lambda i, status=status: self.ticket(i, status, updated=recent), 'tickets')
        for name, (prepare, expected) in cases.items():
            with self.subTest(case=name):
                self.assertEqual(await self.rests(prepare), (expected, False))
        for task in sleeping:
            task.cancel()
        await asyncio.gather(*sleeping, return_exceptions=True)

    async def test_instance_whose_browser_lock_is_taken_is_not_closed(self):
        ana = await self.use(ANA)
        async with ana.runner.lock:
            self.clock.advance(100_000)
            self.assertEqual(ana.work(), 'tramite')
            self.assertEqual(await self.instances.sweep(), [])
        self.assertEqual(await self.instances.sweep(), [ANA])

    async def test_instance_with_only_resting_tickets_is_closed(self):
        old = (datetime.now(timezone.utc) - timedelta(days=5)).isoformat()
        cases = {'no tickets': lambda i: None}
        for status in ('verified', 'duplicate', 'attached', 'historical', 'blocked', 'review', 'new', 'ready', 'external'):
            cases[status] = lambda i, status=status: self.ticket(i, status)
        for status in ('issued_waiting_files', 'uncertain'):
            cases['old ' + status] = lambda i, status=status: self.ticket(i, status, updated=old)
        cases['blocked after the queue'] = lambda i: self.ticket(i, 'blocked', auto_stage='scheduled')

        def reviewed(instance):
            instance.app.state.automation.enabled = True
            self.ticket(instance, 'review', auto_stage='review')
            self.ticket(instance, 'blocked')
        cases['automatic queue with nothing to do'] = reviewed
        for name, prepare in cases.items():
            with self.subTest(case=name):
                self.assertEqual(await self.rests(prepare), ('', True))

    async def test_unreadable_store_keeps_the_instance_awake(self):
        ana = await self.use(ANA)
        with patch.object(ana.app.state.store, 'connect', side_effect=__import__('sqlite3').OperationalError('dañada')):
            self.clock.advance(100_000)
            self.assertEqual(ana.work(), 'desconocido')
            self.assertEqual(await self.instances.sweep(), [])

    async def test_closing_everything_shuts_each_instance_down(self):
        ana, beto = await self.use(ANA), await self.use(BETO)
        ana.runner.close = AsyncMock(wraps=ana.runner.close)
        beto.runner.close = AsyncMock(wraps=beto.runner.close)
        await self.instances.close()
        ana.runner.close.assert_awaited_once()
        beto.runner.close.assert_awaited_once()
        self.assertFalse(self.running(ana) or self.running(beto))
        self.assertIsNone(self.instances.live(ANA))
        with self.assertRaises(Unavailable) as caught:
            await self.instances.enter(ANA)
        self.assertEqual(str(caught.exception), CLOSING)
        self.assertEqual(len(self.made), 2)

    async def test_instance_that_cannot_start_is_refused_and_tried_again(self):
        with patch('facturador.db.Store.recover_after_restart', side_effect=RuntimeError('fallo')):
            with self.assertRaises(Unavailable):
                await self.instances.enter(ANA)
        self.assertIsNone(self.instances.live(ANA))
        with patch.object(self, 'factory', side_effect=OSError('disco')):
            broken = Instances(self.root, self.factory, port=8765, public_origin=ORIGIN, clock=self.clock)
            with self.assertLogs('facturador.instancias', 'ERROR') as logged, self.assertRaises(Unavailable) as caught:
                await broken.enter(BETO)
        self.assertEqual(str(caught.exception), 'No se pudo abrir tu espacio de trabajo. Intenta de nuevo en un momento.')
        self.assertNotIn('disco', ' '.join(logged.output))  # The log names the kind of failure, not its details.
        self.assertTrue(self.running(await self.use(ANA)))

    async def test_turned_off_account_starts_nothing_new_and_leaves_when_its_ticket_ends(self):
        ana = await self.use(ANA)
        done = asyncio.Event()
        task = asyncio.ensure_future(done.wait())
        ana.runner.job_tasks['k'] = task
        task.add_done_callback(lambda _: ana.runner.job_tasks.pop('k', None))
        await self.instances.retire(ANA)
        self.assertIs(self.instances.live(ANA), ana)  # Its ticket is still running.
        self.assertFalse(self.running(ana))           # But the queue and the watchers stopped.
        self.assertTrue(ana.app.state.automation.task.done())
        self.assertEqual(await self.instances.sweep(), [])
        done.set()
        await task
        self.assertEqual(await self.instances.sweep(), [ANA])
        # A step that waits for the person can no longer be answered: it does not hold the instance.
        beto = await self.use(BETO)
        waiting = asyncio.ensure_future(asyncio.sleep(3600))
        beto.runner.job_tasks['k'] = waiting
        beto.runner.pending['k'] = {'kind': 'captcha'}
        beto.runner.close = AsyncMock(wraps=beto.runner.close)
        await self.instances.retire(BETO)
        self.assertIsNone(self.instances.live(BETO))
        beto.runner.close.assert_awaited_once()
        self.assertTrue(waiting.cancelled())
        await self.instances.retire(CARLA)  # No instance: nothing to do.

    async def test_account_turned_on_again_gets_its_workers_back(self):
        ana = await self.use(ANA)
        task = asyncio.ensure_future(asyncio.sleep(3600))
        ana.runner.job_tasks['k'] = task
        await self.instances.retire(ANA)
        self.assertFalse(self.running(ana))
        self.assertIs(await self.use(ANA), ana)
        self.assertFalse(ana.retiring)
        self.assertTrue(self.running(ana))
        task.cancel()
        await asyncio.gather(task, return_exceptions=True)
        ana.runner.job_tasks.clear()


class FakeContext:
    def __init__(self, runner):
        self.runner, self.closed = runner, False

    def is_closed(self):
        return self.closed

    async def close(self):
        self.closed = True
        if self.runner.context is self:
            self.runner.context, self.runner.engine = None, ''


class FakeDriver:
    stopped = False

    async def stop(self):
        self.stopped = True


class FakePage:
    def is_closed(self):
        return False


class BrowserCase(InstancesCase):
    """Instances whose browser is a double. Tickets go through the real Runner.run with a short session."""

    async def asyncSetUp(self):
        await super().asyncSetUp()
        test = self
        self.launches, self.most_open, self.runners = [], 0, []
        self.inside, self.leave, self.uncertain = {}, {}, set()
        self.fail_start = False

        async def start_browser(runner):
            async with runner.browser_start:
                if runner.context:
                    return
                if test.fail_start:
                    raise ValueError('No se pudo iniciar el navegador interno.')
                runner.playwright = runner.playwright or FakeDriver()
                runner.context, runner.engine = FakeContext(runner), 'Chromium'
                test.launches.append(runner)
                test.most_open = max(test.most_open, sum(1 for item in test.runners if item.context))

        async def session(runner, key, profile, auto_issue, manual):
            await runner.start_browser()
            if key in test.uncertain:
                runner.page_job[FakePage()] = key  # The page that may still show the result.
            test.inside[key].set()
            await test.leave[key].wait()
            runner.store.update(key, status='uncertain' if key in test.uncertain else 'verified', message='Fin de la prueba.')

        self.enterContext(patch.object(Runner, 'start_browser', start_browser))
        self.enterContext(patch.object(Runner, '_session', session))

    async def use(self, owner, path='/api/state'):
        instance = await super().use(owner, path)
        if instance.runner not in self.runners:
            self.runners.append(instance.runner)
        return instance

    def start_ticket(self, instance):
        """A ticket through the real Runner.run: its lock, its active ticket and its tracked task."""
        key = self.ticket(instance)
        self.inside[key], self.leave[key] = asyncio.Event(), asyncio.Event()
        instance.runner.track_job(key, instance.runner.run(key, PROFILE))
        return key

    async def until(self, condition, timeout=5):
        deadline = asyncio.get_running_loop().time() + timeout
        while not condition():
            self.assertLess(asyncio.get_running_loop().time(), deadline, 'la condición no se cumplió a tiempo')
            await asyncio.sleep(.005)

    @staticmethod
    def events(instance, key):
        return [(event['kind'], event['detail']) for event in instance.app.state.store.events(key)]


class OneBrowserTests(BrowserCase):
    limit = 1

    async def test_ticket_waits_for_its_turn_with_a_clear_message_and_never_fails(self):
        ana, beto = await self.use(ANA), await self.use(BETO)
        first = self.start_ticket(ana)
        await asyncio.wait_for(self.inside[first].wait(), 5)
        self.assertEqual(list(self.pool.holders), [ana])
        second = self.start_ticket(beto)
        await self.until(lambda: self.pool.queue == [beto])
        row = beto.app.state.store.get(second)
        self.assertEqual((row['status'], row['message']), ('running', WAITING))
        self.assertIn(('browser_wait', WAITING), self.events(beto, second))
        self.assertIsNone(beto.runner.context)
        self.assertEqual(beto.work(), 'tramite')  # Waiting in line is work: the instance is not put to sleep.
        self.clock.advance(100_000)
        await self.use(ANA, '/api/browser/frame')  # Ana watches her ticket in the panel.
        self.assertEqual(await self.instances.sweep(), [])
        await asyncio.sleep(.05)
        self.assertEqual(self.pool.queue, [beto])  # However long Ana's ticket takes, Beto's keeps waiting.
        self.assertFalse(self.inside[second].is_set())
        # Ana's ticket ends. Her browser stays open while she may still be looking at it...
        driver = ana.runner.playwright
        self.leave[first].set()
        await self.until(lambda: not ana.runner.has_active_jobs())
        await asyncio.sleep(.05)
        self.assertFalse(self.inside[second].is_set())
        self.assertIsNotNone(ana.runner.context)
        # ...and a minute after her last look it gives its place to the ticket in line.
        self.clock.advance(61)
        await asyncio.wait_for(self.inside[second].wait(), 5)
        self.assertIsNone(ana.runner.context)
        self.assertTrue(driver.stopped)
        self.assertIsNone(ana.runner.playwright)
        self.assertEqual(list(self.pool.holders), [beto])
        self.assertEqual(self.pool.queue, [])
        self.assertIn(('browser_turn', TURN), self.events(beto, second))
        self.assertEqual(beto.app.state.store.get(second)['message'], TURN)
        self.assertEqual(ana.app.state.store.get(first)['status'], 'verified')
        self.leave[second].set()
        await self.until(lambda: not beto.runner.has_active_jobs())
        self.assertEqual(beto.app.state.store.get(second)['status'], 'verified')
        self.assertEqual(self.most_open, 1)
        self.assertEqual(len(self.launches), 2)

    async def test_tickets_take_turns_in_order_and_a_stopped_one_leaves_the_line(self):
        ana, beto, carla = await self.use(ANA), await self.use(BETO), await self.use(CARLA)
        first = self.start_ticket(ana)
        await asyncio.wait_for(self.inside[first].wait(), 5)
        second = self.start_ticket(beto)
        await self.until(lambda: self.pool.queue == [beto])
        third = self.start_ticket(carla)
        await self.until(lambda: self.pool.queue == [beto, carla])
        dora = await self.use('d' * 16)
        fourth = self.start_ticket(dora)
        await self.until(lambda: self.pool.queue == [beto, carla, dora])
        await carla.runner.stop(third)  # The person stops the ticket that was waiting.
        self.assertEqual(self.pool.queue, [beto, dora])
        self.assertEqual(carla.app.state.store.get(third)['status'], 'blocked')
        self.assertIsNone(carla.runner.context)
        self.leave[first].set()
        await asyncio.wait_for(self.inside[second].wait(), 5)
        self.assertFalse(self.inside[fourth].is_set())
        self.assertEqual(self.pool.queue, [dora])
        self.leave[second].set()
        await asyncio.wait_for(self.inside[fourth].wait(), 5)
        self.leave[fourth].set()
        await self.until(lambda: not dora.runner.has_active_jobs())
        self.assertEqual(self.most_open, 1)
        self.assertEqual([runner for runner in self.launches], [ana.runner, beto.runner, dora.runner])

    async def test_person_gets_a_clear_answer_when_every_browser_is_busy(self):
        ana, beto = await self.use(ANA), await self.use(BETO)
        first = self.start_ticket(ana)
        await asyncio.wait_for(self.inside[first].wait(), 5)
        self.clock.advance(100_000)
        for attempt in (beto.runner.start_browser, beto.runner.open_portal, beto.runner.open_whatsapp):
            with self.subTest(attempt=attempt.__name__), self.assertRaises(ValueError) as caught:
                await asyncio.wait_for(attempt(), 2)  # It answers at once; it does not wait in line.
            self.assertEqual(str(caught.exception), BUSY)
        self.assertEqual(self.pool.queue, [])
        self.assertEqual(list(self.pool.holders), [ana])
        self.assertIsNone(beto.runner.context)
        self.leave[first].set()
        await self.until(lambda: not ana.runner.has_active_jobs())

    async def test_toll_lookup_takes_its_turn_instead_of_opening_a_browser_of_its_own(self):
        ana, beto = await self.use(ANA), await self.use(BETO)
        first = self.start_ticket(ana)
        await asyncio.wait_for(self.inside[first].wait(), 5)  # Ana's ticket holds the only place.
        private = SimpleNamespace(new_context=AsyncMock(return_value=SimpleNamespace(close=AsyncMock())))
        hosted, release = [], asyncio.Event()

        async def recovery():
            # What the recovery of a toll ticket does, from the ticket's own task: ask for a browser to look in.
            async with _lookup_browser(beto.runner) as browser:
                hosted.append(browser)
                await release.wait()

        def another_browser():
            raise AssertionError('La consulta no debe lanzar un navegador fuera del tope.')

        with patch.object(FakeContext, 'browser', private, create=True), \
                patch.object(capufe_recovery, 'async_playwright', another_browser):
            beto.runner.track_job('consulta', recovery())
            await self.until(lambda: self.pool.queue == [beto])
            await asyncio.sleep(.05)
            # It waits in line like any ticket: no browser of Beto's is open, in the limit or outside it.
            self.assertEqual((hosted, self.launches, beto.runner.context), ([], [ana.runner], None))
            self.leave[first].set()
            await self.until(lambda: not ana.runner.has_active_jobs())
            self.clock.advance(61)
            await self.until(lambda: hosted == [private])
            # Its turn came: it looks inside its own engine's browser, the one the limit counts.
            self.assertEqual((list(self.pool.holders), self.launches), ([beto], [ana.runner, beto.runner]))
            self.assertIsNone(ana.runner.context)
            release.set()
            await self.until(lambda: not beto.runner.has_active_jobs())
        self.assertEqual(self.most_open, 1)

    async def test_idle_browser_gives_its_place_only_when_nobody_is_using_it(self):
        ana, beto = await self.use(ANA, '/api/browser/personal'), await self.use(BETO)
        await ana.runner.start_browser()  # Opened by the person from the panel, without a ticket.
        context, driver = ana.runner.context, ana.runner.playwright
        with self.assertRaises(ValueError):
            await beto.runner.start_browser()  # Ana opened it a moment ago.
        self.clock.advance(61)
        busy = {
            'person in control': lambda on: setattr(ana.runner.viewer, 'channel', 'canal' if on else ''),
            'dialog on screen': lambda on: setattr(ana.runner.viewer, 'dialog', object() if on else None),
            'file request': lambda on: setattr(ana.runner.viewer, 'file_request', object() if on else None),
            'personal tab in use': lambda on: setattr(ana.runner.personal.view, 'channel', 'canal' if on else ''),
            'step waiting for the person': lambda on: ana.runner.pending.update(k={}) if on else ana.runner.pending.clear(),
            'download in flight': lambda on: ana.runner.downloads.add('d') if on else ana.runner.downloads.clear(),
            'active ticket': lambda on: setattr(ana.runner, 'active_id', 'k' if on else None),
        }
        for name, switch in busy.items():
            switch(True)
            with self.subTest(case=name), self.assertRaises(ValueError) as caught:
                await beto.runner.start_browser()
            self.assertEqual(str(caught.exception), BUSY)
            self.assertFalse(context.closed)
            switch(False)
        async with ana.runner.lock:
            with self.assertRaises(ValueError):
                await beto.runner.start_browser()
        await self.use(ANA, '/api/browser/frame')  # Her panel is showing the browser right now.
        with self.assertRaises(ValueError):
            await beto.runner.start_browser()
        self.assertFalse(context.closed)
        self.clock.advance(61)
        await self.use(ANA, '/api/state')  # Having the panel open elsewhere does not hold the browser.
        await beto.runner.start_browser()
        self.assertTrue(context.closed)
        self.assertTrue(driver.stopped)
        self.assertFalse(ana.browser_open)
        self.assertEqual(list(self.pool.holders), [beto])
        self.assertEqual(self.most_open, 1)
        # Ana can open hers again later, when there is a place.
        self.clock.advance(61)
        await ana.runner.start_browser()
        self.assertTrue(ana.browser_open)
        self.assertIsNone(beto.runner.context)

    async def test_page_kept_for_an_uncertain_ticket_is_protected_for_longer(self):
        ana, beto = await self.use(ANA), await self.use(BETO)
        key = self.ticket(ana)
        self.uncertain.add(key)
        self.inside[key], self.leave[key] = asyncio.Event(), asyncio.Event()
        ana.runner.track_job(key, ana.runner.run(key, PROFILE))
        await asyncio.wait_for(self.inside[key].wait(), 5)
        context = ana.runner.context
        self.leave[key].set()
        await self.until(lambda: not ana.runner.has_active_jobs())
        # The real runner kept the page of the uncertain ticket to read its result later.
        self.assertEqual(list(ana.runner.page_job.values()), [key])
        self.assertEqual(ana.app.state.store.get(key)['status'], 'uncertain')
        self.clock.advance(61)
        with self.assertRaises(ValueError) as caught:
            await beto.runner.start_browser()
        self.assertEqual(str(caught.exception), BUSY)
        self.assertFalse(context.closed)
        self.clock.advance(840)
        await beto.runner.start_browser()
        self.assertTrue(context.closed)
        self.assertIn(('browser_closed', YIELDED), self.events(ana, key))
        self.assertEqual(self.most_open, 1)

    async def test_browser_that_closed_or_failed_does_not_keep_a_place(self):
        ana, beto = await self.use(ANA), await self.use(BETO)
        await ana.runner.start_browser()
        await ana.runner.context.close()  # It closed on its own: restart, crash or the person.
        await beto.runner.start_browser()
        self.assertEqual(list(self.pool.holders), [beto])
        await beto.runner.context.close()
        self.fail_start = True
        with self.assertRaisesRegex(ValueError, 'No se pudo iniciar el navegador interno'):
            await ana.runner.start_browser()
        self.assertEqual(self.pool.holders, {})
        self.fail_start = False
        await ana.runner.start_browser()
        await ana.runner.start_browser()  # Asking again with the browser open changes nothing.
        self.assertEqual(list(self.pool.holders), [ana])
        self.assertEqual(len(self.launches), 3)

    async def test_closing_an_instance_frees_its_browser(self):
        ana, beto = await self.use(ANA), await self.use(BETO)
        await ana.runner.start_browser()
        context = ana.runner.context
        self.clock.advance(1801)
        await self.use(BETO)
        self.assertEqual(await self.instances.sweep(), [ANA])
        self.assertTrue(context.closed)
        self.assertEqual(self.pool.holders, {})
        await beto.runner.start_browser()
        self.assertEqual(list(self.pool.holders), [beto])


class SeveralBrowsersTests(BrowserCase):
    limit = 3

    async def test_default_limit_lets_three_work_and_makes_the_fourth_wait(self):
        self.assertEqual(Instances(self.root, self.factory, port=8765, public_origin=ORIGIN).browsers.limit, 3)
        people = [await self.use(f'{number:016x}') for number in range(1, 5)]
        keys = [self.start_ticket(person) for person in people[:3]]
        for key in keys:
            await asyncio.wait_for(self.inside[key].wait(), 5)
        self.assertEqual(len(self.pool.holders), 3)
        fourth = self.start_ticket(people[3])
        await self.until(lambda: self.pool.queue == [people[3]])
        self.assertEqual(people[3].app.state.store.get(fourth)['message'], WAITING)
        self.leave[keys[1]].set()
        await asyncio.wait_for(self.inside[fourth].wait(), 5)
        self.assertIsNone(people[1].runner.context)
        self.assertEqual(self.most_open, 3)
        for key in (keys[0], keys[2], fourth):
            self.leave[key].set()
        await self.until(lambda: not any(person.runner.has_active_jobs() for person in people))


class TurnedOffWhileWaitingTests(BrowserCase):
    limit = 1

    async def test_ticket_of_a_turned_off_account_does_not_keep_waiting_for_a_browser(self):
        ana, beto = await self.use(ANA), await self.use(BETO)
        first = self.start_ticket(ana)
        await asyncio.wait_for(self.inside[first].wait(), 5)
        second = self.start_ticket(beto)
        await self.until(lambda: self.pool.queue == [beto])
        await self.instances.retire(BETO)
        self.assertIsNone(self.instances.live(BETO))
        self.assertEqual(self.pool.queue, [])
        # It never reached the portal, and it is not left as if it were still running.
        self.assertFalse(self.inside[second].is_set())
        self.assertEqual(beto.app.state.store.get(second)['status'], 'blocked')
        self.assertEqual(self.launches, [ana.runner])
        # Ana's own ticket goes on untouched while her account is active.
        self.assertTrue(ana.runner.has_active_jobs())
        self.leave[first].set()
        await self.until(lambda: not ana.runner.has_active_jobs())
        self.assertEqual(ana.app.state.store.get(first)['status'], 'verified')


if __name__ == '__main__':
    unittest.main()
