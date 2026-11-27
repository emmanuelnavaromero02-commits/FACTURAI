"""Several engines against the invented portals of the bank, each with its own real browser.

One run for the whole class: three engines, three tickets of different portals at once, one of
them stopped for the person's approval, and a fourth ticket of a portal that is already busy.
What the tests read was noted at fixed moments of that run, reached by waiting for the state
itself and never for a number of seconds. A second class runs the default (one engine).
Nothing leaves this machine: the portals are served from memory and the AI is the scripted one.
"""
import asyncio
import hashlib
import json
import os
import sys
import tempfile
import threading
import time
import unittest
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parent))

from facturador import banco
from facturador import runner as motor
from facturador.db import Store
from facturador.models import Plan
from facturador.motores import SAME_PORTAL, SLOTS, Engines
from facturador.uso_ia import tarea_actual
from facturador.viewer import ViewerInput
from simulated_portal import CHROME

PERFIL = banco.PERFIL
# The profile folder of the main engine here: the container works with a windowed Chrome, which keeps its own.
PROFILE_FOLDER = 'browser_profile' + ('_chrome' if os.getenv('FACTURADOR_CHROME_HEADED') == '1' else '')


class Watched:
    """The scripted AI, noting for which ticket each consultation was counted.

    The first consultation of ``together`` different tickets waits until all of them are
    here at the same moment: tickets worked one after another would never meet.
    """

    def __init__(self, plan, together=0):
        self.plan, self.together = plan, together
        self.seen: list[tuple[str, str]] = []    # (ticket of the call's context, origin of the screen shown)
        self.meeting = threading.Condition()
        self.arrived: set[str] = set()
        self.met = False

    def status(self):
        return self.plan.status()

    def close(self):
        self.plan.close()

    def infer(self, instructions, content, output_type, **options):
        if output_type is Plan:
            ticket = tarea_actual(output_type)[1]
            origin = json.loads(content[0]['text']).get('url')
            with self.meeting:
                self.seen.append((ticket, origin))
                if self.together and not self.met:
                    self.arrived.add(ticket)
                    if len(self.arrived) >= self.together:
                        self.met = True
                        self.meeting.notify_all()
                    else:
                        self.meeting.wait_for(lambda: self.met, timeout=90)
        return self.plan.infer(instructions, content, output_type, **options)


class Bench:
    """Bank portals, a temporary data folder and ``limit`` engines whose browsers only reach the bank."""

    def __init__(self, folder: Path, slugs: list[str], limit: int, *, together=0, clock=time.monotonic):
        self.portals = banco.portales(slugs)
        self.by_slug = {portal.slug: portal for portal in self.portals}
        self.root = folder / 'datos'
        self.store = Store(self.root)
        self.ai = Watched(banco.IAGuionizada(self.portals), together)
        self.engines = Engines(motor.Runner(self.store, self.ai), limit, clock=clock)
        self.doors = []
        for engine in self.engines.all:
            door = banco.Aislamiento(self.portals)
            door.vigilar(engine)
            self.doors.append(door)
        self.photos = folder / 'fotos'
        self.photos.mkdir(parents=True, exist_ok=True)
        # Which tickets were inside a portal at the same time, as each engine's own attempt saw it.
        self.inside: dict[str, str] = {}
        self.peak = 0
        self.same_portal_together: list[tuple] = []
        self.order: list[tuple] = []     # ('in' | 'out', ticket)
        self.where: dict[str, int] = {}  # ticket -> number of the engine that worked on it
        self.portal_of: dict[str, str] = {}

    def ticket(self, slug: str, number: int) -> str:
        portal = self.by_slug[slug]
        receipt = banco.ticket_para(portal, number)
        portal.registrar(receipt, PERFIL)
        photo = self.photos / f'{slug}-{number}.png'
        banco.foto_png(photo, f'{slug}:{number}:{receipt.ticket_number}')
        key = self.store.add(str(photo), hashlib.sha256(photo.read_bytes()).hexdigest())[0]['id']
        self.store.save_receipt(key, receipt, ready=True)
        self.portal_of[key] = portal.origen
        return key

    def start(self, key: str, *, auto_issue=True) -> None:
        receipt = self.store.get(key)['receipt']
        self.engines.schedule(key, PERFIL, auto_issue=auto_issue, confirmed_url=receipt['portal_url'])

    def watch_sessions(self):
        """Note every attempt of every engine from the inside; the engine's own code runs untouched."""
        original = motor.Runner._session
        bench = self

        async def session(runner, key, profile, auto_issue, manual):
            portal = bench.portal_of.get(key, '')
            for other, its_portal in bench.inside.items():
                if its_portal == portal:
                    bench.same_portal_together.append((other, key))
            bench.inside[key] = portal
            bench.peak = max(bench.peak, len(bench.inside))
            bench.order.append(('in', key))
            bench.where[key] = bench.engines.all.index(runner) + 1
            try:
                return await original(runner, key, profile, auto_issue, manual)
            finally:
                del bench.inside[key]
                bench.order.append(('out', key))

        return patch.object(motor.Runner, '_session', session)

    def status(self, key: str) -> str:
        return self.store.get(key)['status']

    def kinds(self, key: str) -> list[str]:
        return [event['kind'] for event in self.store.events(key)]

    def metrics(self, key: str) -> list[dict]:
        return [json.loads(event['detail']) for event in self.store.events(key) if event['kind'] == 'session_metrics']

    def profiles(self) -> list[str]:
        return sorted(path.name for path in self.root.iterdir() if path.name.startswith('browser_profile'))

    async def until(self, condition, what: str, seconds: float = 180) -> None:
        deadline = time.monotonic() + seconds
        while not condition():
            if time.monotonic() > deadline:
                state = {key[:6]: (self.status(key), self.store.get(key)['message'], self.kinds(key)[-6:])
                         for key in self.portal_of}
                raise AssertionError(f'No se llegó a: {what}. Estado: {state}')
            await asyncio.sleep(0.05)


def environment():
    return patch.dict(os.environ, {'FACTURADOR_CHROMIUM_PATH': CHROME})


@unittest.skipUnless(os.path.isfile(CHROME), 'Requiere Google Chrome oficial')
class ThreeBrowsersTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.folder = tempfile.TemporaryDirectory()
        cls.addClassCleanup(cls.folder.cleanup)
        with environment():
            asyncio.run(cls.run_once(Path(cls.folder.name)))

    @classmethod
    async def run_once(cls, folder: Path):
        clock = {'now': 5000.0}
        cls.bench = bench = Bench(folder, ['una_pagina', 'asistente_3_pasos', 'boton_neutro'], 3, together=3,
                                  clock=lambda: clock['now'])
        engines = bench.engines
        cls.noted = noted = {}
        with patch.object(motor, 'resolves_publicly', bench.doors[0].resuelve), \
                patch.object(motor, 'SETTLE_PAUSE', 0.05), bench.watch_sessions():
            try:
                approval = bench.ticket('una_pagina', 1)        # stops until the person approves the emission
                second = bench.ticket('asistente_3_pasos', 1)
                third = bench.ticket('boton_neutro', 1)
                same = bench.ticket('una_pagina', 2)             # the portal of the first one
                cls.keys = {'approval': approval, 'second': second, 'third': third, 'same': same}
                noted['before'] = bench.profiles()
                bench.start(approval, auto_issue=False)
                for key in (second, third, same):
                    bench.start(key)
                noted['queued'] = (bench.status(same), bench.store.get(same)['message'])

                # -- the other two end (and their engines let go of them) while the first one waits for the person
                await bench.until(lambda: bench.status(second) == bench.status(third) == 'verified'
                                  and engines.owner(second) is None and engines.owner(third) is None
                                  and engines.waiting_kind(approval) == 'issue', 'dos verificados y uno esperando')
                noted['waiting'] = {
                    'approval': bench.status(approval), 'same': (bench.status(same), bench.store.get(same)['message']),
                    'same_started': same in bench.where, 'same_in_line': engines.busy_with(same),
                    'free_engine': engines.free_engine() is not None,
                    'shown': engines.all.index(engines.focused()) + 1, 'active': engines.active_id,
                    'panel': engines.panel(), 'state': engines.browser_state(),
                    'in_progress': engines.in_progress(),
                }
                state = noted['waiting']['state']
                view, revision = engines.view_at(state['revision'])
                frame, _ = await view.frame_update(revision)
                noted['frame'] = (view is engines.owner(approval).viewer, bytes(frame or b'')[:3])
                noted['stale'] = engines.view_at(state['revision'] + 1)

                # -- the helpers that rest close their browsers; the main one keeps its own
                await engines.rest_helpers()
                noted['open_at_rest'] = [engine.context is not None for engine in engines.all]
                clock['now'] += engines.idle_grace + 1
                await engines.rest_helpers()
                await bench.until(lambda: all(engine.context is None for engine in engines.helpers),
                                  'navegadores auxiliares cerrados', 30)
                noted['open_after_rest'] = [engine.context is not None for engine in engines.all]

                # -- the person approves through the coordinator: the request reaches the engine of that ticket
                await engines.continue_job(approval, 'approve')
                await bench.until(lambda: bench.status(approval) == bench.status(same) == 'verified',
                                  'el ticket aprobado y el de su mismo portal verificados')
                noted['after'] = bench.profiles()
            finally:
                await engines.close()
        noted['closed'] = [engine.context is None and not engine.job_tasks for engine in engines.all]

    def test_three_tickets_of_different_portals_were_in_their_portals_at_the_same_time(self):
        bench, keys = self.bench, self.keys
        # Their first consultations met: each one waited inside the model until the three were there.
        self.assertTrue(bench.ai.met)
        self.assertEqual(bench.ai.arrived, {keys['approval'], keys['second'], keys['third']})
        self.assertGreaterEqual(bench.peak, 3)
        self.assertEqual(sorted(bench.where[keys[name]] for name in ('approval', 'second', 'third')), [1, 2, 3])
        for key in keys.values():
            self.assertEqual(bench.status(key), 'verified', bench.kinds(key)[-8:])
            self.assertIn('PDF/XML coinciden', bench.store.get(key)['message'])

    def test_a_ticket_that_waits_for_the_person_did_not_stop_the_others_and_ended_once_attended(self):
        waiting = self.noted['waiting']
        self.assertEqual(waiting['approval'], 'needs_user')
        order = self.bench.order
        approved_out = order.index(('out', self.keys['approval']))
        for name in ('second', 'third'):
            self.assertLess(order.index(('out', self.keys[name])), approved_out)
        self.assertEqual(self.bench.kinds(self.keys['approval']).count('issue_attempt'), 1)
        for name in ('second', 'third', 'same'):
            self.assertEqual(self.bench.kinds(self.keys[name]).count('issue_attempt'), 1)

    def test_two_tickets_of_the_same_portal_never_ran_at_the_same_time(self):
        bench, keys, waiting = self.bench, self.keys, self.noted['waiting']
        self.assertEqual(self.noted['queued'], ('queued', SAME_PORTAL))
        # Two engines were free and it still waited: its portal was in use.
        self.assertTrue(waiting['free_engine'])
        self.assertEqual(waiting['same'], ('queued', SAME_PORTAL))
        self.assertFalse(waiting['same_started'])
        self.assertTrue(waiting['same_in_line'])
        self.assertEqual(bench.same_portal_together, [])
        self.assertLess(bench.order.index(('out', keys['approval'])), bench.order.index(('in', keys['same'])))
        # It went to the engine that had just worked on that portal, where its session is.
        self.assertEqual(bench.where[keys['same']], bench.where[keys['approval']])

    def test_the_viewer_showed_the_ticket_that_waited_and_its_request_reached_its_engine(self):
        waiting, keys = self.noted['waiting'], self.keys
        number = self.bench.where[keys['approval']]
        self.assertEqual((waiting['shown'], waiting['active']), (number, keys['approval']))
        self.assertEqual(waiting['panel'], [{'n': number, 'job_id': keys['approval'], 'waiting': True, 'shown': True}])
        self.assertEqual(waiting['in_progress'], [keys['approval'], keys['same']])
        state = waiting['state']
        self.assertEqual((state['mode'], state['job_id'], state['open']), ('job', keys['approval'], True))
        self.assertEqual(state['revision'] % SLOTS, number - 1)
        self.assertEqual(self.noted['frame'], (True, b'\xff\xd8\xff'))   # A live JPEG frame of that engine's page.
        self.assertEqual(self.noted['stale'], (None, 0))
        # The approval sent through the coordinator was the one emission of that ticket.
        events = self.bench.store.events(keys['approval'])
        self.assertEqual([event['detail'] for event in events if event['kind'] == 'needs_user'], ['issue'])

    def test_each_consultation_and_each_measure_belongs_to_its_own_ticket(self):
        bench = self.bench
        self.assertTrue(bench.ai.seen)
        for ticket, origin in bench.ai.seen:
            self.assertEqual(origin, bench.portal_of.get(ticket), 'Consulta contada para otro ticket.')
        for key in self.keys.values():
            # What each attempt says it spent is what the model was asked for that ticket, no more and no less.
            sessions = bench.metrics(key)
            self.assertGreaterEqual(len(sessions), 1)
            self.assertEqual(sum(session['ai_calls'] for session in sessions),
                             sum(ticket == key for ticket, _ in bench.ai.seen))
            self.assertEqual(bench.kinds(key).count('browser_opened'), len(sessions))
        self.assertEqual([door.fuera for door in bench.doors], [[], [], []])
        self.assertEqual([door.bloqueadas_por_motor for door in bench.doors], [[], [], []])

    def test_each_engine_has_its_own_profile_and_a_helper_at_rest_closes_its_browser(self):
        self.assertEqual(self.noted['before'], [])
        self.assertEqual(self.noted['after'], [PROFILE_FOLDER, PROFILE_FOLDER + '_2', PROFILE_FOLDER + '_3'])
        # Just after their tickets the three browsers are open; a minute later only the main one is.
        self.assertEqual(self.noted['open_at_rest'], [True, True, True])
        self.assertEqual(self.noted['open_after_rest'], [True, False, False])
        self.assertEqual(self.noted['closed'], [True, True, True])
        # WhatsApp, the personal browser and the pages opened by hand belong to the main engine alone.
        for helper in self.bench.engines.helpers:
            self.assertEqual((helper.whatsapp, helper.personal.context, helper.portal_page), (None, None, None))
        self.assertEqual([path.name for path in self.bench.root.iterdir() if path.name.startswith('browser_personal')], [])


@unittest.skipUnless(os.path.isfile(CHROME), 'Requiere Google Chrome oficial')
class ControlOfAHelperTests(unittest.IsolatedAsyncioTestCase):
    """The person takes the page of a ticket that a helper has: what they do reaches that page and no other."""

    async def test_the_clicks_of_the_person_reach_the_page_of_the_helper_shown_and_only_with_its_lease(self):
        folder = tempfile.TemporaryDirectory()
        self.addCleanup(folder.cleanup)
        with environment():
            bench = Bench(Path(folder.name), ['una_pagina', 'no_soy_robot'], 2)
            engines = bench.engines
            with patch.object(motor, 'resolves_publicly', bench.doors[0].resuelve), patch.object(motor, 'SETTLE_PAUSE', 0.05):
                try:
                    approval = bench.ticket('una_pagina', 1)
                    challenge = bench.ticket('no_soy_robot', 1)
                    bench.start(approval, auto_issue=False)
                    bench.start(challenge)
                    await bench.until(lambda: engines.waiting_kind(approval) == 'issue'
                                      and engines.waiting_kind(challenge) == 'captcha', 'los dos esperando a la persona')
                    main, helper = engines.all
                    self.assertEqual((engines.owner(approval), engines.owner(challenge)), (main, helper))
                    self.assertEqual(engines.waiting_for_person(), 2)
                    await engines.show(job_id=challenge)
                    state = engines.browser_state()
                    self.assertEqual((state['job_id'], state['mode'], state['manual']), (challenge, 'job', True))
                    # A number of the main engine's screen takes nothing; the one of the screen shown does.
                    self.assertEqual(engines.view_at(main.viewer.revision * SLOTS), (None, 0))
                    view, revision = engines.view_at(state['revision'])
                    self.assertIs(view, helper.viewer)
                    lease = await view.take_control(revision)
                    page = helper.viewer.page
                    box, size = await page.locator('#robot').bounding_box(), page.viewport_size
                    spot = {'x': (box['x'] + box['width'] / 2) / (size['width'] - 1),
                            'y': (box['y'] + box['height'] / 2) / (size['height'] - 1)}
                    self.assertFalse(await page.locator('#robot').is_checked())
                    for kind in ('down', 'up'):
                        await engines.screen.send(ViewerInput(channel=lease, kind=kind, **spot))
                    self.assertTrue(await page.locator('#robot').is_checked())
                    # That lease is of the helper's viewer alone: on the main one it is nobody's.
                    with self.assertRaisesRegex(ValueError, 'ya terminó'):
                        main.viewer.require_control(lease)
                    self.assertEqual((main.viewer.channel, helper.viewer.channel), ('', lease))
                    # While the person holds that page the panel stays on it; it leaves with the lease given back.
                    self.assertIs(engines.focused(), helper)
                    with self.assertRaisesRegex(ValueError, 'Devuelve el control'):
                        await engines.show(job_id=approval)
                    await engines.show(job_id=approval, channel=lease)
                    self.assertIs(engines.focused(), main)
                    self.assertEqual(helper.viewer.channel, '')
                    with self.assertRaisesRegex(ValueError, 'ya terminó'):
                        await engines.screen.send(ViewerInput(channel=lease, kind='down', **spot))
                    self.assertEqual(bench.kinds(approval).count('issue_attempt'), 0)
                    # Nothing was sent to the portal of the challenge: the person only ticked the box.
                    self.assertEqual(bench.by_slug['no_soy_robot'].consultas, [])
                    for key in (challenge, approval):
                        await engines.stop(key)
                    self.assertFalse(engines.has_active_jobs())
                finally:
                    await engines.close()


@unittest.skipUnless(os.path.isfile(CHROME), 'Requiere Google Chrome oficial')
class OneBrowserTests(unittest.TestCase):
    """The default: one ticket at a time in the main engine, nothing else created."""

    @classmethod
    def setUpClass(cls):
        cls.folder = tempfile.TemporaryDirectory()
        cls.addClassCleanup(cls.folder.cleanup)
        with environment():
            asyncio.run(cls.run_once(Path(cls.folder.name)))

    @classmethod
    async def run_once(cls, folder: Path):
        cls.bench = bench = Bench(folder, ['una_pagina', 'boton_neutro'], 1)
        with patch.object(motor, 'resolves_publicly', bench.doors[0].resuelve), \
                patch.object(motor, 'SETTLE_PAUSE', 0.05), bench.watch_sessions():
            try:
                cls.keys = [bench.ticket('una_pagina', 1), bench.ticket('boton_neutro', 1)]
                for key in cls.keys:
                    bench.start(key)
                cls.queued = [bench.status(key) for key in cls.keys]
                cls.tasks = sorted(bench.engines.main.job_tasks)
                await bench.until(lambda: all(bench.status(key) == 'verified' for key in cls.keys), 'los dos verificados')
            finally:
                await bench.engines.close()

    def test_two_tickets_go_one_after_the_other_and_no_helper_nor_profile_is_created(self):
        bench, (first, second) = self.bench, self.keys
        self.assertEqual((bench.engines.limit, bench.engines.helpers), (1, []))
        self.assertEqual(self.queued, ['queued', 'queued'])
        self.assertEqual(self.tasks, sorted(self.keys))   # Both in the line of the only engine, as always.
        self.assertEqual(bench.peak, 1)
        self.assertEqual(bench.order, [('in', first), ('out', first), ('in', second), ('out', second)])
        self.assertEqual(bench.where, {first: 1, second: 1})
        self.assertEqual(bench.profiles(), [PROFILE_FOLDER])
        self.assertIsNone(bench.engines.main.dispatch)
        self.assertIsNone(bench.engines.main.chat_turn)
        for key in self.keys:
            self.assertEqual(len(bench.metrics(key)), 1)
            self.assertIn('PDF/XML coinciden', bench.store.get(key)['message'])


if __name__ == '__main__':
    unittest.main()
