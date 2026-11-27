"""The engine decides a whole screen per consultation, repeats what it learned and stops in time.

Chrome runs against pages served from memory and the «model» is a function: no
merchant portal and no AI service is contacted.
"""
import asyncio
import json
import os
import tempfile
import time
import unittest
from pathlib import Path
from unittest.mock import AsyncMock, Mock, patch

from facturador.ai import AI
from facturador.auth import strict_schema
from facturador.db import Store
from facturador.documents import reconcile, save_candidate
from facturador.models import Action, Plan, Target
from facturador.portal_memory import describe
from facturador.runner import LIMIT_MESSAGE, AttemptLimit, Course, Planned, Runner, question_key
from simulated_portal import CHROME, ORIGIN, PAGE, Brain, Portal, WAIT, numbered, step, ticket
from test_core import FakeContext, FakePage, PROFILE, RECEIPT


class Tab(FakePage):
    """The fake portal page, with the close a stopped ticket asks of its tab."""
    closed = False

    async def close(self):
        self.closed = True

    def is_closed(self):
        return self.closed


def kinds(store, key):
    return [event['kind'] for event in store.events(key)]


def metrics(store, key):
    return [json.loads(event['detail']) for event in store.events(key) if event['kind'] == 'session_metrics']


class PlanContractTests(unittest.TestCase):
    def test_plan_holds_one_to_ten_actions_and_its_schema_has_no_bounds(self):
        with self.assertRaises(ValueError):
            Plan(steps=[])
        self.assertEqual(len(Plan(steps=[WAIT] * 14).steps), 10)
        schema = strict_schema(Plan)
        self.assertEqual(schema['required'], ['steps'])
        self.assertNotIn('minItems', json.dumps(schema))
        self.assertNotIn('maxItems', json.dumps(schema))
        self.assertEqual(schema['$defs']['Action']['required'], list(schema['$defs']['Action']['properties']))

    def test_one_consultation_asks_the_model_for_a_plan(self):
        service = Mock()
        service.infer.return_value = Plan(steps=[WAIT, WAIT])
        plan = AI(service).next_actions('instrucciones', 'pantalla', None, None, 'high')
        self.assertEqual(len(plan.steps), 2)
        self.assertIs(service.infer.call_args.args[2], Plan)
        self.assertEqual(service.infer.call_args.kwargs, {'effort': 'high'})
        # A planner that answers a single action is taken as a one-step plan.
        service.infer.return_value = WAIT
        self.assertEqual(AI(service).next_actions('instrucciones', 'pantalla', None).steps, [WAIT])

    def test_question_is_the_same_with_other_case_accents_or_filler(self):
        self.assertEqual(question_key('¿Cuál es la forma de pago del ticket 4471?'),
                         question_key('cual es LA forma de pago del ticket 9?'))
        self.assertNotEqual(question_key('¿Cuál es el número de tienda?'), question_key('¿Cuál es el número de transacción?'))


class CourseTests(unittest.TestCase):
    """The plan in course goes on only while the screen it was decided for is still there."""
    ITEMS = [{'n': 1, 'kind': 'text', 'label': 'RFC', 'key': 'rfc|', 'filled': True},
             {'n': 2, 'kind': 'text', 'label': 'Correo', 'key': 'correo|', 'filled': False}]

    def planned(self, number, source='ia', kind='fill'):
        action = Action(kind=kind, target=Target(by='field', role=None, name=str(number), exact=True),
                        value='x' if kind == 'fill' else None, reason='paso', risk='normal')
        key = next(item['key'] for item in self.ITEMS if item['n'] == number)
        return Planned(action, source, 1, describe(self.ITEMS, number), key if source == 'ia' else '', ('o', '/', 'firma'))

    def course(self, *steps):
        course = Course()
        course.start(list(steps), ('url', 0, 0, False, False))
        return course

    def test_plan_goes_on_while_nothing_changed(self):
        course = self.course(self.planned(2))
        course.written.append(self.planned(1))
        self.assertEqual(course.broken('', course.context, self.ITEMS), '')

    def test_plan_is_dropped_on_error_change_of_page_or_person(self):
        course = self.course(self.planned(2))
        self.assertEqual(course.broken('No se completó el paso.', course.context, self.ITEMS), 'un paso falló')
        for changed in (('otra', 0, 0, False, False), ('url', 1, 0, False, False), ('url', 0, 1, False, False),
                        ('url', 0, 0, True, False)):
            self.assertEqual(course.broken('', changed, self.ITEMS), 'la página cambió o intervino una persona')

    def test_plan_is_dropped_when_a_field_it_needs_or_wrote_is_no_longer_the_same(self):
        course = self.course(self.planned(2))
        self.assertEqual(course.broken('', course.context, self.ITEMS[:1]), 'un campo del plan ya no está')
        swapped = [self.ITEMS[0], dict(self.ITEMS[1], key='sucursal|')]
        self.assertEqual(course.broken('', course.context, swapped), 'un campo del plan ya no está')
        course.written.append(self.planned(1))
        emptied = [dict(self.ITEMS[0], filled=False), self.ITEMS[1]]
        self.assertEqual(course.broken('', course.context, emptied), 'cambió un campo ya capturado')

    def test_repeated_recipe_keeps_only_field_values_that_still_fit_when_the_screen_changes(self):
        press = Planned(Action(kind='click', target=Target(by='role', role='button', name='Buscar', exact=True),
                               value=None, reason='paso', risk='normal'), 'memoria', 2, None, '', ('o', '/', 'firma'), '', True)
        course = self.course(self.planned(1, 'memoria'), self.planned(2, 'memoria'),
                             self.planned(2, 'memoria', 'click'), press)
        self.assertEqual(course.broken('', course.context, []), '')        # its fields are checked by the signature
        course.carry(self.ITEMS[:1], ('o', '/', 'otra'))
        self.assertEqual([(step.field['label'], step.screen[2], step.last) for step in course.steps],
                         [('rfc', 'otra', False)])


@unittest.skipUnless(os.path.isfile(CHROME), 'Requiere Google Chrome oficial')
class PortalCase(unittest.IsolatedAsyncioTestCase):
    """A runner with its own browser and data folder; portals are attached by each test."""

    async def asyncSetUp(self):
        # The test loop's debug mode records where every future was created, which makes
        # a browser session several times slower. These tests drive whole tickets.
        asyncio.get_running_loop().set_debug(False)
        folder = tempfile.TemporaryDirectory()
        self.addCleanup(folder.cleanup)
        self.root = Path(folder.name)
        self.store = Store(self.root)
        self.runner = Runner(self.store)
        for target, value in (('facturador.runner.resolves_publicly', AsyncMock(return_value=True)),
                              ('facturador.runner.REPLAY_PAUSE', 0), ('facturador.runner.SETTLE_PAUSE', 0.05)):
            patcher = patch(target, value)
            patcher.start()
            self.addCleanup(patcher.stop)
        environment = patch.dict(os.environ, {'FACTURADOR_CHROMIUM_PATH': CHROME})
        environment.start()
        self.addCleanup(environment.stop)
        await self.runner.start_browser()
        self.addAsyncCleanup(self.runner.close)
        self.count = 0

    async def serve(self, portal):
        await self.runner.context.route(portal.origin + '/**', portal.handle)
        return portal

    def add(self, receipt):
        self.count += 1
        key = self.store.add(f'ticket-{self.count}.png', f'sintetico-{self.count}')[0]['id']
        self.store.save_receipt(key, receipt, ready=True)
        return key

    async def bill(self, receipt, brain, *, auto_issue=True, until=None, timeout=60, before=None):
        """Run one ticket. With ``until`` it returns when the ticket pauses for that reason."""
        key = self.add(receipt)
        if before:
            before(key)
        with patch('facturador.runner.AI.next_action', side_effect=brain):
            self.runner.schedule(key, PROFILE, auto_issue=auto_issue, confirmed_url=receipt.portal_url)
            task = self.runner.job_tasks[key]
            async with asyncio.timeout(timeout):
                if until:
                    while self.runner.pending.get(key, {}).get('kind') != until:
                        if task.done():
                            raise AssertionError((self.store.get(key)['message'], self.store.events(key)[-6:]))
                        await asyncio.sleep(.03)
                else:
                    await task
        return key

    async def finish(self, key, brain, decision='approve'):
        with patch('facturador.runner.AI.next_action', side_effect=brain):
            self.runner.decide(key, decision)
            await asyncio.wait_for(self.runner.job_tasks[key], 60)

    def stored(self) -> bytes:
        return b''.join(path.read_bytes() for path in self.root.glob('memoria_portales.sqlite*'))


class ScreenPlanTests(PortalCase):
    async def test_a_plan_of_several_fields_runs_with_one_consultation(self):
        portal = await self.serve(Portal())
        receipt = portal.add(ticket('TKT-88231-Q', '2026-11-13', '1137.45'))
        brain = Brain()
        key = await self.bill(receipt, brain)
        row = self.store.get(key)
        self.assertEqual(row['status'], 'verified', (row['message'], self.store.events(key)[-8:]))
        # Four screens, four consultations: the lookup (3 fields and its button), the fiscal
        # data (4 fields and its button), the emission alone and the download.
        self.assertEqual(len(brain.calls), 4)
        self.assertEqual(brain.seen('Folio del ticket'), 1)
        self.assertEqual(portal.lookups, [{'folio': 'TKT-88231-Q', 'fecha': '13/11/2026', 'total': '1137.45'}])
        self.assertEqual(portal.fiscal[0]['rfc'], PROFILE.rfc)
        self.assertEqual((portal.fiscal[0]['regimen'], portal.fiscal[0]['uso'], portal.fiscal[0]['pago']), ('612', 'G03', '28'))
        self.assertEqual(len(portal.emitted), 1)
        self.assertEqual(kinds(self.store, key).count('issue_attempt'), 1)
        self.assertEqual(metrics(self.store, key), [{'ai_calls': 4, 'actions': 11, 'replayed_actions': 0,
                                                     'seconds': metrics(self.store, key)[0]['seconds'],
                                                     'outcome': 'verified'}])
        self.assertIn('memory_learned', kinds(self.store, key))

    async def test_risky_action_that_is_not_first_is_observed_and_planned_again(self):
        form = PAGE % ('<h1>Factura tu compra</h1><form action="/emitir" method="post">'
                       '<label>RFC<input name="rfc"></label><label>Folio del ticket<input name="folio"></label>'
                       '<button>Emitir factura</button></form>')
        emitted = []

        async def single_page(route):
            if route.request.method == 'POST':
                emitted.append(route.request.post_data)
                await route.fulfill(content_type='text/html; charset=utf-8', body=PAGE % '<h1>Solicitud recibida</h1>')
            else:
                await route.fulfill(content_type='text/html; charset=utf-8', body=form)

        await self.runner.context.route(ORIGIN + '/**', single_page)
        receipt = ticket('TKT-40021-B', '2026-11-14', '250.00')
        calls = []

        def brain(_instructions, observation, *_rest):
            view = json.loads(observation)
            calls.append(view)
            emit = step('click', 'button', 'Emitir factura')   # mislabelled as normal on purpose
            if 'Solicitud recibida' in view['estado_visible']:
                return Plan(steps=[WAIT])
            if len(calls) == 1:
                return Plan(steps=[step('fill', 'textbox', 'RFC', PROFILE.rfc),
                                   step('fill', 'textbox', 'Folio del ticket', receipt.ticket_number), emit])
            return Plan(steps=[emit])

        key = await self.bill(receipt, brain, until='issue')
        # The fields were filled from the first plan; the emission was thrown away from it
        # and asked for again with a fresh observation, where it came alone.
        self.assertEqual(len(calls), 2)
        self.assertIn(PROFILE.rfc, json.dumps(calls[1]['campos']))
        self.assertIn('una acción de riesgo va sola', json.dumps(calls[1]['acciones_recientes'], ensure_ascii=False))
        # The page does not show the amount, so it is never emitted without the person.
        self.assertEqual(emitted, [])
        self.assertFalse(self.store.get(key)['issue_attempted'])
        with patch('facturador.runner.MAX_AI_CALLS', 3):
            await self.finish(key, brain)
        self.assertEqual(len(emitted), 1)
        self.assertEqual(kinds(self.store, key).count('issue_attempt'), 1)

    async def unlabeled(self, change):
        page = PAGE % ('<h1>Consulta de compra</h1><form action="/fin" method="get"><div id="nuevo"></div>'
                       '<div><span>Referencia de compra</span><input name="ref" oninput="' + change + '"></div>'
                       '<div id="monto"><span>Importe</span><input name="importe"></div>'
                       '<button>Consultar</button></form>')
        sent = []

        async def portal(route):
            address = route.request.url
            if '/fin' in address:
                sent.append(address.split('?', 1)[1])
                await route.fulfill(content_type='text/html; charset=utf-8', body=PAGE % '<h1>Sin resultados</h1>')
            else:
                await route.fulfill(content_type='text/html; charset=utf-8', body=page)

        await self.runner.context.route(ORIGIN + '/**', portal)
        calls = []

        def brain(_instructions, observation, *_rest):
            view = json.loads(observation)
            calls.append(view)
            fields = {line.split('«')[1].split('»')[0]: line.split()[0].lstrip('#') for line in view['campos']}
            if len(calls) == 1:
                return Plan(steps=[numbered('fill', fields['Referencia de compra'], 'TKT-70310-K'),
                                   numbered('fill', fields['Importe'], '480.00'),
                                   step('click', 'button', 'Consultar')])
            return Plan(steps=[WAIT])

        with patch('facturador.runner.MAX_AI_CALLS', 2):
            key = await self.bill(ticket('TKT-70310-K', '2026-11-15', '480.00'), brain)
        return calls, sent, key

    async def test_field_number_is_resolved_again_by_what_the_field_is(self):
        # Typing the reference makes the portal add a field before the others: every number moves.
        calls, sent, _ = await self.unlabeled(
            "if(!this.dataset.hecho){this.dataset.hecho=1;document.getElementById('nuevo').innerHTML="
            "'<span>Cupón</span><input name=cupon>'}")
        self.assertEqual(len(calls) - 1, 1)           # one consultation filled both fields and pressed the button
        self.assertEqual(sent, ['cupon=&ref=TKT-70310-K&importe=480.00'])

    async def test_changed_page_never_gets_a_value_in_the_wrong_field(self):
        # Typing the reference swaps the amount field for another one in the same place and number.
        calls, sent, key = await self.unlabeled(
            "if(!this.dataset.hecho){this.dataset.hecho=1;document.getElementById('monto').innerHTML="
            "'<span>Sucursal</span><input name=sucursal>'}")
        self.assertEqual(sent, [])                    # the rest of the plan was dropped, button included
        self.assertGreaterEqual(len(calls), 2)        # and the screen was observed and planned again
        branch = next(line for line in calls[1]['campos'] if 'Sucursal' in line)
        self.assertTrue(branch.endswith('»: '), branch)
        self.assertIn('un campo del plan ya no está', json.dumps(calls[1]['acciones_recientes'], ensure_ascii=False))
        self.assertEqual(self.store.get(key)['message'], LIMIT_MESSAGE)


class PlanShapeTests(PortalCase):
    async def test_plan_is_cut_before_what_must_travel_alone_or_is_not_on_screen(self):
        portal = await self.serve(Portal())
        receipt = portal.add(ticket('TKT-90001-T', '2026-11-13', '70.00'))
        page = await self.runner.context.new_page()
        await page.goto(ORIGIN + '/datos?folio=TKT-90001-T&fecha=13/11/2026&total=70.00')
        items = (await self.runner.page_state(page, receipt))['items']
        postal = next(item['n'] for item in items if item['label'] == 'Código postal')
        rfc, name = step('fill', 'textbox', 'RFC', 'x'), step('fill', 'textbox', 'Nombre o razón social', 'y')
        read = Action(kind='read_text', target=None, value=None, reason='leer', risk='normal')

        async def planned(*actions):
            steps, cut = await self.runner.planned_steps(page, receipt, Plan(steps=list(actions)), items)
            return [(item.action.kind, item.field and item.field['label']) for item in steps], cut

        self.assertEqual(await planned(rfc, numbered('fill', postal, '76146'), step('click', 'button', 'Continuar')),
                         ([('fill', 'rfc'), ('fill', 'codigo postal'), ('click', None)], ''))
        self.assertEqual(await planned(rfc, read, name), ([('fill', 'rfc')], 'una herramienta o pregunta va sola'))
        self.assertEqual(await planned(read, rfc), ([('read_text', None)], 'una herramienta o pregunta va sola'))
        self.assertEqual(await planned(rfc, step('click', 'button', 'Pagar después'), name),
                         ([('fill', 'rfc')], 'un control que todavía no se ve'))
        self.assertEqual(await planned(rfc, numbered('fill', 99, 'z')),
                         ([('fill', 'rfc')], 'un número de campo que no está en la lista'))
        steps, message = await planned(numbered('fill', 99, 'z'), rfc)
        self.assertEqual(steps, [])
        self.assertIn('no está en la lista campos', message)

    async def single_screen(self, body, brain, **limits):
        async def portal(route):
            await route.fulfill(content_type='text/html; charset=utf-8', body=PAGE % body)

        await self.runner.context.route(ORIGIN + '/**', portal)
        patches = [patch('facturador.runner.' + name, value) for name, value in limits.items()]
        for patcher in patches:
            patcher.start()
            self.addCleanup(patcher.stop)
        return ticket('TKT-90002-V', '2026-11-13', '70.00')

    async def test_button_that_changes_the_screen_ends_the_plan(self):
        receipt = await self.single_screen(
            '<h1>Solicitud</h1><label>Referencia<input name="ref"></label>'
            '<button type="button" onclick="document.getElementById(\'mas\').hidden=false">Agregar comentario</button>'
            '<div id="mas" hidden><label>Comentario<input name="comentario"></label></div>'
            '<button type="button">Consultar</button>', None, MAX_AI_CALLS=2)
        calls = []

        def brain(_instructions, observation, *_rest):
            calls.append(json.loads(observation))
            if len(calls) == 1:
                return Plan(steps=[step('click', 'button', 'Agregar comentario'),
                                   step('fill', 'textbox', 'Referencia', 'TKT-90002-V'),
                                   step('click', 'button', 'Consultar')])
            return Plan(steps=[WAIT])

        await self.bill(receipt, brain)
        # The button showed a new field: what followed was decided for the screen before.
        reference = next(line for line in calls[1]['campos'] if 'Referencia' in line)
        self.assertTrue(reference.endswith('»: '), reference)
        self.assertTrue(any('Comentario' in line for line in calls[1]['campos']))
        self.assertIn('la pantalla cambió', json.dumps(calls[1]['acciones_recientes'], ensure_ascii=False))

    async def test_same_plan_without_any_change_is_stopped(self):
        receipt = await self.single_screen(
            '<h1>Validación</h1><label>Referencia<input name="ref"></label><button type="button">Validar</button>', None)
        calls = []

        def brain(_instructions, observation, *_rest):
            calls.append(json.loads(observation))
            return Plan(steps=[step('fill', 'textbox', 'Referencia', 'TKT-90002-V'), step('click', 'button', 'Validar')])

        key = await self.bill(receipt, brain, until='blocked')
        # Three runs of the plan (the last two changed nothing), two warnings, then the person.
        self.assertEqual(len(calls), 6)
        self.assertEqual(calls[3]['ultimo_error'], '')
        self.assertIn('mismo plan sin cambio visible', calls[4]['ultimo_error'])
        self.assertEqual(metrics(self.store, key), [])
        await self.runner.stop(key)
        self.assertEqual(metrics(self.store, key)[0]['actions'], 6)


class PortalMemoryRunTests(PortalCase):
    async def learn(self, portal, folio='TKT-88231-Q', day='2026-11-13', amount='1137.45'):
        brain = Brain()
        key = await self.bill(portal.add(ticket(folio, day, amount, portal.origin)), brain)
        self.assertEqual(self.store.get(key)['status'], 'verified', self.store.events(key)[-8:])
        return brain

    async def test_second_ticket_repeats_the_portal_without_the_model(self):
        portal = await self.serve(Portal())
        self.assertEqual(len((await self.learn(portal)).calls), 4)
        second = portal.add(ticket('TKT-10457-Z', '2026-11-21', '89.50', payment='01'))
        brain = Brain()
        key = await self.bill(second, brain)
        row = self.store.get(key)
        self.assertEqual(row['status'], 'verified', (row['message'], self.store.events(key)[-8:]))
        self.assertEqual(brain.calls, [])                                     # not one consultation
        self.assertEqual(portal.lookups[1], {'folio': 'TKT-10457-Z', 'fecha': '21/11/2026', 'total': '89.50'})
        self.assertEqual(portal.fiscal[1]['pago'], '01')
        self.assertEqual(portal.emitted[1]['folio'], 'TKT-10457-Z')
        self.assertEqual(portal.emitted[1]['rfc'], PROFILE.rfc)
        events = kinds(self.store, key)
        self.assertEqual(events.count('memory_replay'), 4)
        self.assertEqual(events.count('issue_attempt'), 1)
        self.assertEqual(metrics(self.store, key)[0] | {'seconds': 0},
                         {'ai_calls': 0, 'actions': 11, 'replayed_actions': 11, 'seconds': 0, 'outcome': 'verified'})
        stored = self.stored()
        for private in (PROFILE.rfc, PROFILE.email, PROFILE.name, PROFILE.postal_code, 'TKT-88231-Q', 'TKT-10457-Z',
                        '88231', '1137.45', '89.50', '13/11/2026'):
            self.assertNotIn(private.encode(), stored)
        self.assertIn(b'$ticket.ticket_number', stored)

    async def test_repeated_emission_still_needs_the_proof_on_screen_or_the_person(self):
        portal = await self.serve(Portal())
        await self.learn(portal)
        # Same portal, no automatic emission allowed: the repeated steps stop at the gate.
        brain = Brain()
        key = await self.bill(portal.add(ticket('TKT-20001-A', '2026-11-22', '310.00')), brain, auto_issue=False,
                              until='issue')
        self.assertEqual(brain.calls, [])
        self.assertEqual(len(portal.emitted), 1)
        self.assertFalse(self.store.get(key)['issue_attempted'])
        self.assertEqual(portal.fiscal[-1]['folio'], 'TKT-20001-A')           # reached the gate with this ticket's data
        await self.finish(key, brain)
        self.assertEqual(self.store.get(key)['status'], 'verified')
        self.assertEqual(len(portal.emitted), 2)
        # The confirmation stops showing the amount. Its shape is the same, so it is repeated,
        # but with automatic emission allowed the gate still asks the person.
        portal.show_amount = False
        key = await self.bill(portal.add(ticket('TKT-20002-B', '2026-11-23', '512.75')), brain, until='issue')
        self.assertEqual(brain.calls, [])
        self.assertEqual(len(portal.emitted), 2)
        self.assertFalse(self.store.get(key)['issue_attempted'])
        await self.runner.stop(key)

    async def test_changed_page_goes_back_to_the_model_and_is_learned_again(self):
        portal = await self.serve(Portal())
        await self.learn(portal)
        portal.phone = True                                                   # the fiscal page gains a field
        brain = Brain()
        key = await self.bill(portal.add(ticket('TKT-30001-C', '2026-11-24', '77.10')), brain)
        self.assertEqual(self.store.get(key)['status'], 'verified', self.store.events(key)[-8:])
        self.assertEqual(len(brain.calls), 1)
        self.assertEqual(brain.seen('Teléfono de contacto'), 1)
        events = kinds(self.store, key)
        self.assertEqual((events.count('memory_miss'), events.count('memory_replay')), (1, 3))
        self.assertIn('memory_learned', events)
        brain = Brain()
        key = await self.bill(portal.add(ticket('TKT-30002-D', '2026-11-25', '63.00')), brain)
        self.assertEqual(self.store.get(key)['status'], 'verified')
        self.assertEqual(brain.calls, [])

    async def test_three_failures_switch_the_recipe_off_until_the_model_learns_it_again(self):
        portal = await self.serve(Portal())
        await self.learn(portal)
        lookup = next(key for key in self.screens() if key[1] == '/')
        portal.stuck = True                                                   # same page, it no longer advances
        with patch('facturador.runner.SETTLE_TRIES', 1), patch('facturador.runner.MAX_AI_CALLS', 1):
            for number in range(3):
                key = await self.bill(portal.add(ticket(f'TKT-4000{number}-E', '2026-11-26', '45.00')), Brain())
                events = kinds(self.store, key)
                self.assertEqual(events.count('memory_replay'), 1)
                self.assertEqual(self.store.get(key)['message'], LIMIT_MESSAGE)
                self.assertEqual(self.runner.memory.screen(lookup, 'F')['streak'], number + 1)
            self.assertEqual(self.runner.memory.screen(lookup, 'F')['active'], 0)
            key = await self.bill(portal.add(ticket('TKT-40009-E', '2026-11-26', '45.00')), Brain())
            self.assertNotIn('memory_replay', kinds(self.store, key))
        portal.stuck = False
        brain = await self.learn(portal, 'TKT-40010-F', '2026-11-27', '52.00')
        self.assertEqual(brain.seen('Folio del ticket'), 1)                   # the model solved that screen again
        row = self.runner.memory.screen(lookup, 'F')
        self.assertEqual((row['active'], row['streak']), (1, 0))

    async def test_family_of_portals_is_trusted_after_two_successes(self):
        first = await self.serve(Portal('https://facturas.comercio-uno.mx'))
        second = await self.serve(Portal('https://facturas.comercio-dos.mx'))
        third = await self.serve(Portal('https://facturas.comercio-tres.mx'))
        self.assertEqual(len((await self.learn(first, 'TKT-50001-G', '2026-11-13', '120.00')).calls), 4)
        # One success elsewhere is not enough: the second portal is solved by the model.
        self.assertEqual(len((await self.learn(second, 'TKT-50002-H', '2026-11-14', '130.00')).calls), 4)
        brain = await self.learn(third, 'TKT-50003-J', '2026-11-15', '140.00')
        self.assertEqual(brain.calls, [])
        self.assertEqual(third.emitted[0]['folio'], 'TKT-50003-J')
        replays = [event['detail'] for event in self.store.events(self.last_key()) if event['kind'] == 'memory_replay']
        self.assertEqual(len(replays), 4)
        self.assertTrue(all('familia' in detail for detail in replays))

    async def test_memory_steps_aside_when_the_person_wrote_or_another_route_is_due(self):
        portal = await self.serve(Portal())
        await self.learn(portal)
        # A message from the person must be read by the model: nothing is repeated around it.
        brain = Brain()
        key = await self.bill(portal.add(ticket('TKT-11001-A', '2026-11-14', '31.00')), brain,
                              before=lambda key: self.runner.tell(key, 'Usa el correo de la oficina'))
        self.assertEqual(self.store.get(key)['status'], 'verified')
        self.assertEqual(len(brain.calls), 4)
        self.assertNotIn('memory_replay', kinds(self.store, key))
        self.assertIn('Usa el correo de la oficina', brain.calls[0]['mensajes_del_titular'])
        # An attempt that starts over by another route does not take the remembered one again.
        brain = Brain()
        hint = 'Intento anterior falló: el portal no avanzó. Toma otra ruta para llegar a la factura.'
        key = await self.bill(portal.add(ticket('TKT-11002-B', '2026-11-15', '32.00')), brain,
                              before=lambda key: self.runner.session_hints.__setitem__(key, hint))
        self.assertEqual(self.store.get(key)['status'], 'verified')
        self.assertEqual(len(brain.calls), 4)
        self.assertNotIn('memory_replay', kinds(self.store, key))
        # And the repetition can be switched off as a whole.
        brain = Brain()
        with patch.dict(os.environ, {'FACTURADOR_MEMORIA': '0'}):
            key = await self.bill(portal.add(ticket('TKT-11003-C', '2026-11-16', '33.00')), brain)
        self.assertEqual((self.store.get(key)['status'], len(brain.calls)), ('verified', 4))

    async def test_choices_and_a_field_that_shows_up_earlier_are_repeated_for_the_new_ticket(self):
        # The lookup has a payment list the fiscal rules do not recognize, a plain list and an
        # optional field that appears while typing. First it appears after the date.
        portal = await self.serve(Portal(choices=True, reveal='fecha'))
        brain = await self.learn(portal)
        self.assertEqual(len(brain.calls), 4)
        self.assertEqual((portal.lookups[0]['como'], portal.lookups[0]['tipo']), ('2', 'T'))
        stored = self.stored()
        self.assertIn(b'$sat.payment', stored)               # the payment is a link, chosen again for each ticket
        self.assertIn('=Ticket de caja'.encode(), stored)    # a plain literal is kept as it is
        self.assertNotIn('Tarjeta de débito'.encode(), stored)
        # Now it appears earlier, after the folio, and the ticket was paid in cash.
        portal.reveal = 'folio'
        brain = Brain()
        key = await self.bill(portal.add(ticket('TKT-12001-C', '2026-11-18', '64.20', payment='01')), brain)
        self.assertEqual(self.store.get(key)['status'], 'verified', self.store.events(key)[-10:])
        self.assertEqual(brain.calls, [])
        self.assertEqual(portal.lookups[1], {'como': '1', 'tipo': 'T', 'folio': 'TKT-12001-C', 'fecha': '18/11/2026',
                                             'total': '64.20'})
        self.assertEqual(portal.fiscal[1]['pago'], '01')

    async def test_emission_behind_a_plain_button_keeps_its_gate_when_repeated(self):
        # The emission button of this portal only says «Continuar». The model marked it as an
        # emission; the memory keeps that mark, so repeating it still goes through the gate.
        portal = await self.serve(Portal(emit='Continuar'))
        key = await self.bill(portal.add(ticket('TKT-60001-K', '2026-11-13', '410.00')), Brain(emit='Continuar'))
        self.assertEqual(self.store.get(key)['status'], 'verified', self.store.events(key)[-8:])
        brain = Brain(emit='Continuar')
        key = await self.bill(portal.add(ticket('TKT-60002-L', '2026-11-14', '420.00')), brain, auto_issue=False,
                              until='issue')
        self.assertEqual(brain.calls, [])
        self.assertEqual(len(portal.emitted), 1)
        self.assertFalse(self.store.get(key)['issue_attempted'])
        await self.runner.stop(key)

    async def test_invoice_sent_by_mail_is_learned_when_its_files_are_verified(self):
        portal = await self.serve(Portal(download=False))
        key = await self.bill(portal.add(ticket('TKT-70001-M', '2026-11-13', '95.00')), Brain())
        self.assertEqual(self.store.get(key)['status'], 'issued_waiting_files')
        self.assertEqual(self.screens(), [])                                  # nothing is learned yet
        self.assertNotIn(b'TKT-70001-M', self.stored())
        # Its files arrive later and match: what that attempt did is learned then.
        save_candidate(self.store, key, 'comprobantes.zip', portal.invoice('TKT-70001-M')[1])
        reconcile(self.store, key, PROFILE)
        self.assertEqual(self.store.get(key)['status'], 'verified')
        brain = Brain()
        second = await self.bill(portal.add(ticket('TKT-70002-N', '2026-11-14', '96.00')), brain)
        self.assertEqual(self.store.get(second)['status'], 'issued_waiting_files')
        self.assertEqual(len(portal.emitted), 2)
        self.assertEqual(brain.seen('Folio del ticket') + brain.seen('Datos fiscales') + brain.seen('Confirma tu factura'), 0)

    async def test_screen_a_person_worked_on_is_left_to_the_model(self):
        portal = await self.serve(Portal())
        brain = Brain()
        asked = []

        def needs_help(instructions, observation, *rest):
            if 'Datos fiscales' in observation and not asked:
                asked.append(1)
                return Action(kind='human', target=None, value=None, reason='Revisa los datos', risk='unknown')
            return brain(instructions, observation, *rest)

        with patch('facturador.runner.SELF_HELP_LIMIT', 0), patch('facturador.runner.MAX_QUESTIONS', 0):
            key = await self.bill(portal.add(ticket('TKT-80001-R', '2026-11-13', '55.00')), needs_help, until='unknown')
            with patch('facturador.runner.AI.next_action', side_effect=needs_help):
                await self.runner.retry_job(key)
                await asyncio.wait_for(self.runner.job_tasks[key], 60)
        self.assertEqual(self.store.get(key)['status'], 'verified', self.store.events(key)[-8:])
        fiscal = next(screen for screen in self.screens(whole=True) if screen['path'] == '/datos')
        self.assertEqual((fiscal['needs_ai'], fiscal['steps']), (1, '[]'))
        brain = Brain()
        key = await self.bill(portal.add(ticket('TKT-80002-S', '2026-11-14', '56.00')), brain)
        self.assertEqual(self.store.get(key)['status'], 'verified')
        self.assertEqual((len(brain.calls), brain.seen('Datos fiscales')), (1, 1))

    def screens(self, whole=False):
        with self.runner.memory.connect() as db:
            rows = [dict(row) for row in db.execute('SELECT * FROM screens')]
        return rows if whole else [(row['origin'], row['path'], row['signature']) for row in rows]

    def last_key(self):
        return self.store.list()[0]['id']


class PersonClock:
    """Stands in for ``time`` inside the runner: the real clock, plus what the test adds for a person's wait."""

    def __init__(self):
        self.ahead = 0.0

    def monotonic(self):
        return time.monotonic() + self.ahead

    def __getattr__(self, name):
        return getattr(time, name)


class LimitTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        folder = tempfile.TemporaryDirectory()
        self.addCleanup(folder.cleanup)
        self.store = Store(Path(folder.name))
        self.runner = Runner(self.store)
        self.addAsyncCleanup(self.runner.close)
        self.key = self.store.add('ticket.png', 'limites')[0]['id']
        self.store.save_receipt(self.key, RECEIPT, ready=True)
        self.page = Tab()
        self.runner.context = FakeContext(self.page)

        async def forget_context():
            self.runner.context = None
        self.addAsyncCleanup(forget_context)

    def click(self, name='Facturar', risk='normal'):
        return Action(kind='click', target=Target(by='role', role='button', name=name, exact=True),
                      value=None, reason='paso', risk=risk)

    async def pending(self, kind):
        async with asyncio.timeout(30):  # Ample: on a busy machine a ticket may take seconds to get here.
            while self.runner.pending.get(self.key, {}).get('kind') != kind:
                await asyncio.sleep(.01)

    async def run_with(self, model, **limits):
        patches = [patch('facturador.runner.AI', return_value=model),
                   *(patch('facturador.runner.' + name, value) for name, value in limits.items())]
        for patcher in patches:
            patcher.start()
            self.addCleanup(patcher.stop)
        self.runner.schedule(self.key, PROFILE)
        return self.runner.job_tasks[self.key]

    async def test_consultation_limit_stops_the_ticket_without_another_attempt(self):
        scroll = Action(kind='scroll', target=None, value='300', reason='ver más', risk='normal')
        model = Mock(next_action=Mock(return_value=scroll))
        await asyncio.wait_for(await self.run_with(model, MAX_AI_CALLS=3), 10)
        row = self.store.get(self.key)
        self.assertEqual((row['status'], row['message'], row['issue_attempted']), ('blocked', LIMIT_MESSAGE, 0))
        self.assertEqual(model.next_action.call_count, 3)
        events = self.store.events(self.key)
        self.assertEqual([e['detail'] for e in events if e['kind'] == 'limit_reached'], ['consultas'])
        self.assertEqual([e['kind'] for e in events].count('browser_opened'), 1)
        self.assertNotIn('session_retry', [e['kind'] for e in events])
        self.assertEqual(metrics(self.store, self.key)[0] | {'seconds': 0},
                         {'ai_calls': 3, 'actions': 0, 'replayed_actions': 0, 'seconds': 0, 'outcome': 'limit_consultas'})

    async def test_time_limit_stops_the_ticket(self):
        model = Mock(next_action=Mock(return_value=WAIT))
        await asyncio.wait_for(await self.run_with(model, MAX_ACTIVE_SECONDS=0.4), 10)
        row = self.store.get(self.key)
        self.assertEqual((row['status'], row['message']), ('blocked', LIMIT_MESSAGE))
        self.assertEqual(model.next_action.call_count, 1)
        self.assertEqual(metrics(self.store, self.key)[0]['outcome'], 'limit_tiempo')

    async def test_time_waiting_for_the_person_is_not_charged(self):
        help_me = Action(kind='human', target=None, value=None, reason='Revisión', risk='unknown')
        finished = Action(kind='done', target=None, value=None, reason='Fin', risk='normal')
        model = Mock(next_action=Mock(side_effect=[help_me, self.click(risk='issue'), finished]))
        # The runner's clock is moved by hand while the person «thinks»: the two pauses take far
        # longer than the whole attempt may work, whatever the machine is busy with meanwhile.
        clock = PersonClock()
        budget = 30
        task = await self.run_with(model, time=clock, MAX_ACTIVE_SECONDS=budget, SELF_HELP_LIMIT=0, MAX_QUESTIONS=0)
        await self.pending('unknown')
        clock.ahead += 3 * budget
        await self.runner.retry_job(self.key)
        await self.pending('issue')
        clock.ahead += 2 * budget
        self.runner.decide(self.key, 'approve')
        await asyncio.wait_for(task, 30)
        self.assertEqual(self.store.get(self.key)['status'], 'issued_waiting_files')
        self.assertNotIn('limit_reached', kinds(self.store, self.key))
        spent = metrics(self.store, self.key)[0]
        # Five times the budget went by on that clock; only the work itself was charged.
        self.assertLess(spent['seconds'], budget)
        self.assertEqual((spent['ai_calls'], spent['actions'], spent['outcome']), (3, 1, 'issued'))

    async def test_limit_after_an_emission_attempt_keeps_the_recovery_path(self):
        self.page.success = False                       # the portal never confirms the emission
        model = Mock(next_action=Mock(side_effect=[self.click(risk='issue')] + [WAIT] * 5))
        task = await self.run_with(model, MAX_AI_CALLS=2)
        await self.pending('issue')
        self.runner.decide(self.key, 'approve')
        await asyncio.wait_for(task, 10)
        row = self.store.get(self.key)
        self.assertEqual((row['status'], row['issue_attempted']), ('uncertain', 1))
        self.assertIn('Busca los comprobantes', row['message'])
        self.assertEqual(self.page.clicks, 1)

    async def test_same_question_is_asked_once_and_then_the_ticket_waits_for_the_person(self):
        def ask(text):
            return Action(kind='ask', target=None, value=text, reason='Falta un dato', risk='normal')
        finished = Action(kind='done', target=None, value=None, reason='Fin', risk='normal')
        model = Mock(next_action=Mock(side_effect=[ask('¿Cuál es la forma de pago?'), ask('¿CUAL ES LA FORMA DE PAGO?'),
                                                   ask('¿Cuál es la forma de pago?'), self.click(risk='issue'), finished]))
        task = await self.run_with(model, ASK_TIMEOUT=0.05)

        def chat():
            return [m['text'] for m in self.store.chat_messages() if 'forma de pago' in m['text'].casefold()]

        # Asked once in the chat, unanswered. Needed again: the ticket waits with the question as its message.
        await self.pending('unknown')
        self.assertEqual(model.next_action.call_count, 2)
        self.assertEqual(len(chat()), 1)
        row = self.store.get(self.key)
        self.assertEqual(row['status'], 'needs_user')
        self.assertIn('FORMA DE PAGO', row['message'])
        self.assertEqual(self.runner.open_question(), self.key)
        self.runner.answer_question(self.key, 'Fue con tarjeta de débito')
        # The answer resumes the ticket and is given back, without asking, the third time.
        await self.pending('issue')
        self.assertEqual(model.next_action.call_count, 4)
        self.assertEqual(len(chat()), 1)
        for call in model.next_action.call_args_list[2:]:
            self.assertIn('tarjeta de débito', call.args[1])
        self.assertEqual(kinds(self.store, self.key).count('question'), 1)
        self.runner.decide(self.key, 'approve')
        await asyncio.wait_for(task, 5)

    async def test_unanswered_question_is_not_repeated_after_the_single_wait(self):
        question = '¿Qué número de sucursal uso?'
        self.assertTrue(self.runner.can_ask(self.key, question))
        with patch('facturador.runner.ASK_TIMEOUT', 0.05):
            self.assertEqual(await self.runner.ask_once(self.key, self.page, question), ('', ''))
        self.assertTrue(self.runner.can_ask(self.key, question))             # one wait for the person is left
        await self.runner.viewer.show(self.page, 'Portal de prueba')
        waiting = asyncio.create_task(self.runner.ask_once(self.key, self.page, 'que numero de SUCURSAL uso'))
        await self.pending('unknown')
        self.runner.decide(self.key, 'resume')                                # resumed from the panel, no answer
        self.assertEqual(await asyncio.wait_for(waiting, 2), ('', 'resume'))
        self.assertFalse(self.runner.can_ask(self.key, question))             # never again in this run
        self.assertEqual(len([m for m in self.store.chat_messages() if 'sucursal' in m['text']]), 1)

    async def test_run_retries_another_route_only_once(self):
        self.runner.viewer.page = self.page
        with patch.object(self.runner, '_session', side_effect=ValueError('El portal no avanzó.')) as session:
            await self.runner.run(self.key, PROFILE)
        self.assertEqual(session.await_count, 2)
        self.assertEqual(kinds(self.store, self.key).count('session_retry'), 1)
        self.assertEqual(self.store.get(self.key)['status'], 'blocked')

    async def test_spent_attempt_is_not_started_again(self):
        self.runner.viewer.page = self.page
        with patch.object(self.runner, '_session', side_effect=AttemptLimit(LIMIT_MESSAGE)) as session:
            await self.runner.run(self.key, PROFILE)
        session.assert_awaited_once()
        row = self.store.get(self.key)
        self.assertEqual((row['status'], row['message']), ('blocked', LIMIT_MESSAGE))


if __name__ == '__main__':
    unittest.main()
