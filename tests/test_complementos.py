"""The engine names no merchant: what one portal needs lives in its complement, behind fixed hooks.

Nothing here reaches the network. The engine runs against fake pages, or against Chrome with
pages served from memory, and the «model» is a function.
"""
import ast
import asyncio
import io
import json
import os
import re
import tempfile
import tokenize
import unicodedata
import unittest
from contextlib import ExitStack, contextmanager
from pathlib import Path
from unittest.mock import AsyncMock, patch
from urllib.parse import urlsplit

import facturador.runner as engine
from facturador import complementos
from facturador.catalog import CATALOG, CUSTOM_PORTAL
from facturador.complementos import DONE, GO, Complement, Signal, View, complement_class, observe
from facturador.db import Store
from facturador.models import Plan, Receipt
from facturador.runner import Runner
from facturador.ticket_retry import recover_ticket
from simulated_portal import WAIT, Brain, Portal, step
from test_core import PROFILE, RECEIPT, FakeLocator
from test_screen_plans import PortalCase, Tab, kinds

SOURCES = Path(complementos.__file__).resolve().parent
# The general engine. None of these files may name a merchant.
ENGINE = ('runner.py', 'automation.py', 'portal_memory.py', 'ticket_retry.py', 'complementos.py')
RULE = ('Regla del producto: lo nuevo va en el motor general; lo de un comercio, solo en su complemento '
        '(facturador/complementos.py). El motor no nombra a ningún comercio: ni su clave del catálogo ni su dominio.')
# The hooks that answer a signal, and every hook in the order the runner calls them on one pass.
SIGNAL_HOOKS = ('after_reading', 'before_fiscal_choices', 'after_fiscal_choices', 'after_ticket_lookup', 'check_action',
                'before_acting', 'prove_preparation', 'before_issue', 'before_recording')
ORDER = ('after_reading', 'before_fiscal_choices', 'after_fiscal_choices', 'after_ticket_lookup', 'adjust_risk',
         'check_action', 'before_acting', 'prepares_issue', 'known_preparation', 'prove_preparation', 'before_issue',
         'issue_proof', 'before_recording', 'after_acting', 'after_logging')
BEFORE_EMISSION = ('before_fiscal_choices', 'after_fiscal_choices', 'after_ticket_lookup')
DONE_ACTION = WAIT.model_copy(update={'kind': 'done', 'reason': 'Fin'})


def merchant_words() -> re.Pattern:
    """Every catalog key as a whole word and the registrable domain of every catalog host."""
    domains = set()
    for item in CATALOG.values():
        for host in [*item['hosts'], urlsplit(item['url']).netloc]:
            labels = host.split(':')[0].lower().split('.')
            wide = len(labels) > 2 and len(labels[-1]) == 2 and labels[-2] in {'com', 'org', 'gob', 'net', 'edu'}
            domains.add('.'.join(labels[-3 if wide else -2:]))
    keys = '|'.join(map(re.escape, sorted(CATALOG)))
    return re.compile(f'(?<![a-z0-9])(?:{keys})(?![a-z0-9])|' + '|'.join(map(re.escape, sorted(domains))))


def named_merchants(source: str) -> list[tuple[int, str]]:
    """(line, word) of every merchant named in the code: in its identifiers and its texts, never in its comments."""
    words = merchant_words()
    texts = {tokenize.STRING, getattr(tokenize, 'FSTRING_MIDDLE', tokenize.STRING)}
    found = []
    for token in tokenize.generate_tokens(io.StringIO(source).readline):
        if token.type == tokenize.NAME:
            # BPForm and bp_form are the same name for this purpose.
            text = re.sub(r'(?<=[a-z0-9])(?=[A-Z])|(?<=[A-Z])(?=[A-Z][a-z])', '_', token.string)
        elif token.type in texts:
            text = token.string
        else:
            continue
        text = ''.join(char for char in unicodedata.normalize('NFKD', text.lower()) if not unicodedata.combining(char))
        found.extend((token.start[0] + text.count('\n', 0, match.start()), match.group())
                     for match in words.finditer(text))
    return found


def without_registry(source: str) -> str:
    """The interface module with its registry blanked: the one table that lists the merchants."""
    table = next(node for node in ast.parse(source).body if isinstance(node, ast.Assign)
                 and any(isinstance(target, ast.Name) and target.id == 'REGISTRY' for target in node.targets))
    lines = source.splitlines(keepends=True)
    for number in range(table.lineno - 1, table.end_lineno):
        lines[number] = '\n'
    return ''.join(lines)


def listed_class(key: str) -> type[Complement]:
    """The complement of a listed merchant, whatever the switch says."""
    with patch.dict(os.environ, {'FACTURADOR_COMPLEMENTOS': '1'}):
        return complement_class(key)


@contextmanager
def watched_complements():
    """Note every listed complement that gets built and every search one of them is asked for."""
    calls = []
    with ExitStack() as stack:
        for key in complementos.REGISTRY:
            listed = listed_class(key)

            def built(self, *args, _start=listed.__init__, _key=key):
                calls.append((_key, 'built'))
                _start(self, *args)

            async def searched(*args, _key=key):
                calls.append((_key, 'recover_documents'))
                return False

            stack.enter_context(patch.object(listed, '__init__', built))
            stack.enter_context(patch.object(listed, 'recover_documents', searched))
        yield calls


class EngineNamesNoMerchantTests(unittest.TestCase):
    def test_the_engine_files_name_no_merchant(self):
        for name in ENGINE:
            source = (SOURCES / name).read_text(encoding='utf-8')
            if name == 'complementos.py':
                source = without_registry(source)
            found = named_merchants(source)
            places = ', '.join(f'línea {line}: «{word}»' for line, word in found[:8])
            self.assertEqual(found, [], f'facturador/{name} vuelve a nombrar un comercio ({places}). {RULE}')

    def test_the_check_reads_names_and_texts_and_skips_comments(self):
        key = sorted(complementos.REGISTRY)[-1]
        host = CATALOG[key]['hosts'][0]
        for code in (f"if receipt.provider == '{key}':\n    pass\n",
                     f'{key}_lookup = None\n',
                     f'state = {key.title()}Progress()\n',
                     f'from .{key}_issue import proof\n',
                     f"ADDRESS = 'https://{host}/address'\n",
                     f"message = f'Se cerró el aviso de {key.title()} en {{page.url}}.'\n"):
            self.assertTrue(named_merchants(code), code)
        # Every address of the catalog is found, and so is any other host under the same domain.
        for item in CATALOG.values():
            for address in item['hosts']:
                self.assertTrue(named_merchants(f"ADDRESS = 'https://{address}/'\n"), address)
                self.assertTrue(named_merchants(f"ADDRESS = 'https://otro.{address.split(':')[0]}/'\n"), address)
        self.assertEqual(named_merchants(f'paso = 1  # como hace {key.title()} en su portal\n'), [])
        self.assertEqual(named_merchants(f"primera = 'otra{key}sinseparar'\nprovider = receipt.provider\n"), [])
        self.assertEqual(named_merchants("a = 1\ntexto = '''uno\ndos " + key + "'''\n"), [(3, key)])

    def test_only_the_registry_table_lists_the_merchants(self):
        source = (SOURCES / 'complementos.py').read_text(encoding='utf-8')
        self.assertEqual(sorted({word for _, word in named_merchants(source)}), sorted(complementos.REGISTRY))


class RegistryTests(unittest.TestCase):
    def test_every_listed_complement_belongs_to_a_catalog_merchant(self):
        for key in complementos.REGISTRY:
            listed = complement_class(key)
            self.assertIn(key, CATALOG)
            self.assertTrue(issubclass(listed, Complement) and listed is not Complement, key)

    def test_a_portal_without_complement_gets_the_plain_one(self):
        unlisted = next(key for key in sorted(CATALOG) if key not in complementos.REGISTRY)
        for provider in (CUSTOM_PORTAL, unlisted, 'portal-que-no-existe', ''):
            self.assertIs(complement_class(provider), Complement, provider)

    def test_the_switch_turns_every_complement_off(self):
        with patch.dict(os.environ):
            os.environ.pop('FACTURADOR_COMPLEMENTOS', None)
            self.assertTrue(complementos.enabled())                      # on unless it is turned off
            for value, plain in (('1', False), ('0', True)):
                os.environ['FACTURADOR_COMPLEMENTOS'] = value
                for key in complementos.REGISTRY:
                    self.assertEqual(complement_class(key) is Complement, plain, (value, key))

    def test_each_attempt_gets_a_complement_with_its_own_state(self):
        for key in complementos.REGISTRY:
            listed = complement_class(key)
            shared = [name for name, value in vars(listed).items() if isinstance(value, (list, dict, set))]
            self.assertEqual(shared, [], f'{key}: el estado va en cada intento, no en la clase')
            first, second = (listed(None, ticket, PROFILE, RECEIPT, False) for ticket in ('uno', 'dos'))
            self.assertEqual((first.key, second.key), ('uno', 'dos'))
            for name, value in vars(first).items():
                if not isinstance(value, (str, int, bool, tuple, frozenset, type(None))) and value not in (PROFILE, RECEIPT):
                    self.assertIsNot(value, vars(second)[name], f'{key}.{name}')

    def test_names_that_moved_are_still_found_and_patched_in_the_engine_module(self):
        moved = [(module, name) for entry in complementos.REGISTRY.values()
                 for module in [__import__('facturador.' + entry.partition(':')[0], fromlist=['*'])]
                 for name in getattr(module, 'ENGINE_NAMES', ())]
        self.assertTrue(moved)
        for module, name in moved:
            own = getattr(module, name)
            self.assertIs(getattr(engine, name), own)
            self.assertIs(complementos.patched(name, own), own)
            with patch('facturador.runner.' + name, 'doble') as double:
                self.assertIs(complementos.patched(name, own), double)
            self.assertIs(complementos.patched(name, own), own)
            self.assertNotIn(name, vars(engine))
        self.assertFalse(hasattr(engine, 'nombre_que_nunca_existio'))
        self.assertFalse(hasattr(engine, '__nombre_reservado__'))


class Untouchable:
    """A page nobody may use."""

    def __getattr__(self, name):
        raise AssertionError('El complemento vacío tocó la página: ' + name)


class PlainComplementTests(unittest.IsolatedAsyncioTestCase):
    async def test_every_hook_lets_the_engine_go_on_without_touching_anything(self):
        plain = Complement(None, 'ticket', PROFILE, RECEIPT, True)
        view = View([])
        view.read(Untouchable(), 'pantalla', True)
        view.act(step('click', 'button', 'Facturar'), 'Facturar', True)
        self.assertIsNone(await plain.start({'source_path': 'ticket.png'}))
        for name in SIGNAL_HOOKS:
            self.assertIs(await getattr(plain, name)(view), GO, name)
        self.assertEqual([plain.adjust_risk(view, risk) for risk in ('normal', 'issue', 'confirm')],
                         ['normal', 'issue', 'confirm'])
        self.assertFalse(plain.prepares_issue(view) or plain.known_preparation(view))
        self.assertIsNone(await plain.issue_proof(view))                 # the general proof decides
        self.assertIsNone(await plain.after_acting(view))
        self.assertIsNone(plain.after_logging(view))
        self.assertFalse(await Complement.recover_documents(None, 'ticket', PROFILE, RECEIPT, []))
        self.assertFalse(Complement.prepares_after_negative_check)
        self.assertEqual(view.recent, [])
        self.assertEqual((GO.then, GO.error, GO.before, GO.note, GO.active, GO.pending, GO.action),
                         (complementos.PROCEED, None, None, None, False, (), None))
        self.assertEqual((observe(error='x').then, observe(error='x').error, DONE.then),
                         (complementos.OBSERVE, 'x', complementos.FINISH))


def recording(**script):
    """A new complement class that notes every call and answers what ``script`` says for a hook."""
    def answer(complement, name, view, default):
        complement.calls.append(name)
        return script[name](complement, view) if name in script else default

    class Recorder(Complement):
        made = []        # one per attempt, in order; this class lives for one test only

        def __init__(self, *args):
            super().__init__(*args)
            self.calls = []
            self.made.append(self)

        async def start(self, job):
            self.calls.append('start')

        async def after_reading(self, view): return answer(self, 'after_reading', view, GO)
        async def before_fiscal_choices(self, view): return answer(self, 'before_fiscal_choices', view, GO)
        async def after_fiscal_choices(self, view): return answer(self, 'after_fiscal_choices', view, GO)
        async def after_ticket_lookup(self, view): return answer(self, 'after_ticket_lookup', view, GO)
        def adjust_risk(self, view, risk): return answer(self, 'adjust_risk', view, risk)
        async def check_action(self, view): return answer(self, 'check_action', view, GO)
        async def before_acting(self, view): return answer(self, 'before_acting', view, GO)
        def prepares_issue(self, view): return answer(self, 'prepares_issue', view, False)
        def known_preparation(self, view): return answer(self, 'known_preparation', view, False)
        async def prove_preparation(self, view): return answer(self, 'prove_preparation', view, GO)
        async def before_issue(self, view): return answer(self, 'before_issue', view, GO)
        async def issue_proof(self, view): return answer(self, 'issue_proof', view, None)
        async def before_recording(self, view): return answer(self, 'before_recording', view, GO)
        async def after_acting(self, view): answer(self, 'after_acting', view, None)
        def after_logging(self, view): answer(self, 'after_logging', view, None)

    return Recorder


def passes(calls: list[str]) -> list[list[str]]:
    """The calls of one attempt, cut at every new reading of the page."""
    cut = []
    for name in calls:
        if name == 'after_reading':
            cut.append([])
        if cut:
            cut[-1].append(name)
    return cut


def once_then(first, later=GO):
    """A hook answer that is ``first`` the first time it is asked and ``later`` from then on."""
    asked = []

    def reply(complement, view):
        asked.append(1)
        chosen = first if len(asked) == 1 else later
        return chosen(complement, view) if callable(chosen) else chosen
    return reply


class Control(FakeLocator):
    async def get_attribute(self, _name):
        return None      # not one of the numbered fields


class Screen(Tab):
    """The fake page, with controls a plan of several steps can point at."""

    def locator(self, selector):
        return Control(self)

    def get_by_role(self, *args, **kwargs):
        return Control(self)


class Pages:
    """A browser context of fake pages: every attempt opens its own tab, as the real one does."""

    def __init__(self, success=True):
        self.success = success
        self.opened = []

    async def new_page(self):
        self.opened.append(Screen(self.success))
        return self.opened[-1]

    async def close(self):
        pass


class Model:
    """Stands in for the model: answers its scripted actions in order, the last one from then on."""

    def __init__(self, *answers):
        self.answers = list(answers)
        self.seen = []

    def next_action(self, _instructions, observation, *_rest):
        self.seen.append(json.loads(observation))
        return self.answers.pop(0) if len(self.answers) > 1 else self.answers[0]


class EngineCase(unittest.IsolatedAsyncioTestCase):
    """The real runner on fake pages, with a recording complement listed for the ticket's merchant."""
    success = True

    async def asyncSetUp(self):
        folder = tempfile.TemporaryDirectory()
        self.addCleanup(folder.cleanup)
        self.store = Store(Path(folder.name))
        self.runner = Runner(self.store)
        self.addAsyncCleanup(self.runner.close)
        self.pages = self.runner.context = Pages(self.success)
        self.count = 0
        self.attempted_at_click = []
        execute = self.runner.execute

        async def watched(page, action, receipt):
            if action.kind == 'click':
                self.attempted_at_click.append(bool(self.store.get(self.runner.active_id)['issue_attempted']))
            return await execute(page, action, receipt)

        self.runner.execute = watched

    def begin(self, complement, model, *, auto_issue=False, before=None):
        self.count += 1
        key = self.store.add(f'ticket-{self.count}.png', f'sintetico-{self.count}')[0]['id']
        self.store.save_receipt(key, RECEIPT.model_copy(update={'billing_codes': [f'COD-{self.count}']}), ready=True)
        if complement is not None:
            self.enterContext(patch.dict(complementos.REGISTRY, {RECEIPT.provider: complement}))
        self.enterContext(patch('facturador.runner.AI', return_value=model))
        if before:
            before(key)
        self.runner.schedule(key, PROFILE, auto_issue=auto_issue)
        return key, self.runner.job_tasks[key]

    async def until(self, ready, key):
        task = self.runner.job_tasks.get(key)
        async with asyncio.timeout(15):
            while not ready():
                if task is not None and task.done():
                    self.fail((self.store.get(key)['message'], self.store.events(key)[-6:]))
                await asyncio.sleep(.01)

    async def paused(self, key, kind):
        await self.until(lambda: self.runner.pending.get(key, {}).get('kind') == kind, key)

    @property
    def clicks(self):
        return sum(page.clicks for page in self.pages.opened)


class HookOrderTests(EngineCase):
    async def test_the_hooks_run_in_a_fixed_order_on_every_pass(self):
        recorder = recording()
        key, task = self.begin(recorder, Model(step('click', 'button', 'Facturar'), DONE_ACTION))
        await self.paused(key, 'issue')
        self.assertEqual(self.clicks, 0)
        self.runner.decide(key, 'approve')
        await asyncio.wait_for(task, 10)
        self.assertEqual((self.clicks, self.attempted_at_click), (1, [True]))
        self.assertEqual(len(recorder.made), 1)
        calls = recorder.made[0].calls
        self.assertEqual((calls[0], calls.count('start')), ('start', 1))
        rounds = passes(calls)
        for round_ in rounds:
            places = [ORDER.index(name) for name in round_]
            self.assertEqual(places, sorted(set(places)), round_)
        emission = next(index for index, round_ in enumerate(rounds) if 'before_recording' in round_)
        # A pass that emits goes through every hook except those of a preparation step.
        self.assertEqual(rounds[emission], [name for name in ORDER if name not in ('known_preparation', 'prove_preparation')])
        # Once something was sent, the hooks that prepare the form are not called again.
        self.assertTrue(rounds[emission + 1:])
        for round_ in rounds[emission + 1:]:
            self.assertFalse(set(round_) & set(BEFORE_EMISSION), round_)

    async def test_a_ticket_without_complement_runs_on_the_plain_one(self):
        given = []

        def asked(provider):
            given.append((provider, complement_class(provider)))
            return given[-1][1]

        with patch('facturador.runner.complement_class', side_effect=asked):
            key, task = self.begin(None, Model(step('click', 'button', 'Facturar'), DONE_ACTION))
            await self.paused(key, 'issue')
            self.runner.decide(key, 'approve')
            await asyncio.wait_for(task, 10)
        self.assertEqual(given, [(RECEIPT.provider, Complement)])
        self.assertEqual(self.clicks, 1)
        self.assertEqual(kinds(self.store, key).count('issue_attempt'), 1)

    async def test_each_attempt_builds_its_own_complement(self):
        def first_attempt_fails(complement, _view):
            complement.readings = getattr(complement, 'readings', 0) + 1
            if complement is complement.made[0]:
                raise ValueError('El portal no avanzó.')      # nothing was sent: the run tries another route
            return GO

        recorder = recording(after_reading=first_attempt_fails)
        model = Model(step('click', 'button', 'Facturar'), DONE_ACTION)
        key, task = self.begin(recorder, model)
        await self.paused(key, 'issue')
        self.runner.decide(key, 'approve')
        await asyncio.wait_for(task, 10)
        self.assertEqual(len(recorder.made), 2)
        first, second = recorder.made
        self.assertEqual((first.calls, first.readings), (['start', 'after_reading'], 1))
        self.assertEqual(second.calls[:2], ['start', 'after_reading'])
        self.assertEqual(second.readings, second.calls.count('after_reading'))
        self.assertIn('El portal no avanzó.', model.seen[0]['ultimo_error'])
        self.assertEqual(len(self.pages.opened), 2)
        # Another ticket is another attempt: it never sees the state of the one before.
        other, task = self.begin(recorder, Model(step('click', 'button', 'Facturar'), DONE_ACTION))
        await self.paused(other, 'issue')
        self.assertEqual(len(recorder.made), 3)
        self.assertEqual((recorder.made[2].key, recorder.made[2].readings), (other, 1))
        await self.runner.stop(other)


class SignalTests(EngineCase):
    async def test_observe_ends_the_pass_and_its_error_reaches_the_model(self):
        recorder = recording(after_reading=once_then(observe(error='Cierra el aviso del portal.')))
        model = Model(step('click', 'button', 'Facturar'))
        key, _ = self.begin(recorder, model)
        await self.paused(key, 'issue')
        rounds = passes(recorder.made[0].calls)
        self.assertEqual(rounds[0], ['after_reading'])                    # nothing else ran on that pass
        self.assertEqual(model.seen[0]['ultimo_error'], 'Cierra el aviso del portal.')
        await self.runner.stop(key)

    async def test_what_the_complement_did_and_what_is_pending_reach_the_model(self):
        def entered(_complement, view):
            view.recent.append({'tipo': 'click', 'control': 'Entrada propia'})
            return observe(before=(view.page.url, 'la pantalla anterior'))

        recorder = recording(before_fiscal_choices=once_then(entered, Signal(pending=('ficha del receptor',))))
        model = Model(step('click', 'button', 'Facturar'))
        key, _ = self.begin(recorder, model)
        await self.paused(key, 'issue')
        self.assertEqual(model.seen[0]['acciones_recientes'][0],
                         {'tipo': 'click', 'control': 'Entrada propia', 'cambio_visible': True})
        self.assertEqual(model.seen[0]['campos_fiscales_pendientes'], ['ficha del receptor'])
        await self.runner.stop(key)

    async def test_a_question_answered_at_the_gate_holds_the_emission(self):
        asked = observe(active=True, note='Paga con vales.', error='Elige la forma de pago y vuelve a pulsar.')
        recorder = recording(before_issue=once_then(asked))
        model = Model(step('click', 'button', 'Facturar'))
        key, _ = self.begin(recorder, model)
        await self.paused(key, 'issue')
        self.assertEqual((model.seen[1]['respuesta_del_titular'], model.seen[1]['ultimo_error']),
                         ('Paga con vales.', 'Elige la forma de pago y vuelve a pulsar.'))
        self.assertEqual(recorder.made[0].calls.count('before_issue'), 2)
        self.assertEqual((self.clicks, self.store.get(key)['issue_attempted']), (0, 0))
        await self.runner.stop(key)

    async def test_done_ends_the_attempt_and_an_error_stops_the_ticket(self):
        model = Model(step('click', 'button', 'Facturar'))
        key, task = self.begin(recording(after_reading=lambda *_: DONE), model)
        await asyncio.wait_for(task, 10)
        self.assertEqual((model.seen, self.clicks), ([], 0))

        def rejected(*_):
            raise ValueError('El portal respondió: el ticket venció.')

        key, task = self.begin(recording(check_action=rejected), model)
        await asyncio.wait_for(task, 10)
        row = self.store.get(key)
        self.assertEqual((row['status'], row['message'], self.clicks), ('blocked', 'El portal respondió: el ticket venció.', 0))
        self.assertFalse(row['issue_attempted'])
        # A hook that answers anything but a signal is a fault of its complement: the ticket stops closed.
        key, task = self.begin(recording(before_issue=lambda *_: None), model)
        await asyncio.wait_for(task, 10)
        row = self.store.get(key)
        self.assertEqual((row['status'], row['message'], self.clicks), ('blocked', 'El trámite se interrumpió.', 0))
        self.assertIn('internal_error', kinds(self.store, key))
        self.assertFalse(row['issue_attempted'])


class OwnRulesTests(EngineCase):
    async def test_its_own_action_skips_the_model_but_not_the_gate(self):
        own = Signal(action=step('click', 'link', 'Facturar', risk='issue'))
        recorder = recording(before_fiscal_choices=lambda *_: own)
        model = Model(DONE_ACTION)
        key, task = self.begin(recorder, model)
        await self.paused(key, 'issue')
        self.assertEqual((model.seen, self.clicks), ([], 0))
        self.assertEqual(passes(recorder.made[0].calls)[-1][-2:], ['before_issue', 'issue_proof'])
        self.runner.decide(key, 'approve')
        await asyncio.wait_for(task, 10)
        self.assertEqual((self.clicks, self.attempted_at_click), (1, [True]))
        self.assertEqual(kinds(self.store, key).count('issue_attempt'), 1)
        self.assertTrue(model.seen[0]['emision_intentada'])               # the model comes back after the emission

    async def test_its_own_proof_decides_the_automatic_emission(self):
        general = self.enterContext(patch.object(self.runner, 'generic_issue_proof', AsyncMock(return_value=True)))
        model = Model(step('click', 'button', 'Facturar'), DONE_ACTION)
        # Its proof passes: the ticket is emitted without the person, after the attempt is recorded.
        key, task = self.begin(recording(issue_proof=lambda *_: True), model, auto_issue=True)
        await asyncio.wait_for(task, 10)
        self.assertEqual((self.clicks, self.attempted_at_click), (1, [True]))
        self.assertNotIn('needs_user', kinds(self.store, key))
        general.assert_not_awaited()
        # Its proof fails: the general one is not asked instead and the person decides.
        model = Model(step('click', 'button', 'Facturar'), DONE_ACTION)
        key, task = self.begin(recording(issue_proof=lambda *_: False), model, auto_issue=True)
        await self.paused(key, 'issue')
        self.assertEqual(self.clicks, 1)
        general.assert_not_awaited()
        await self.runner.stop(key)
        # No proof of its own: the general one decides.
        model = Model(step('click', 'button', 'Facturar'), DONE_ACTION)
        key, task = self.begin(recording(), model, auto_issue=True)
        await asyncio.wait_for(task, 10)
        self.assertEqual(self.clicks, 2)
        general.assert_awaited_once()

    async def test_a_last_look_can_still_hold_an_approved_emission(self):
        recorder = recording(before_recording=once_then(observe(error='El formulario cambió antes de enviar.')))
        model = Model(step('click', 'button', 'Facturar'))
        key, _ = self.begin(recorder, model)
        await self.paused(key, 'issue')
        self.runner.decide(key, 'approve')
        await self.until(lambda: len(model.seen) == 2, key)
        self.assertEqual(model.seen[1]['ultimo_error'], 'El formulario cambió antes de enviar.')
        self.assertEqual((self.clicks, self.store.get(key)['issue_attempted']), (0, 0))
        await self.paused(key, 'issue')
        await self.runner.stop(key)


class PreparationTests(EngineCase):
    success = False      # these buttons do not issue anything: the page stays as it is

    async def test_a_preparation_step_is_proven_by_the_complement_and_never_recorded_as_an_emission(self):
        recorder = recording(prepares_issue=lambda _complement, view: view.name == 'Facturar',
                             prove_preparation=once_then(observe(error='El formulario aún no refleja el expediente.')))
        model = Model(step('click', 'button', 'Facturar'), step('click', 'button', 'Facturar'), WAIT)
        key, _ = self.begin(recorder, model)
        await self.until(lambda: self.clicks == 1, key)
        await self.runner.stop(key)
        calls = recorder.made[0].calls
        self.assertEqual(calls.count('prove_preparation'), 2)             # held once, then let through
        self.assertEqual(model.seen[1]['ultimo_error'], 'El formulario aún no refleja el expediente.')
        self.assertFalse({'before_issue', 'issue_proof', 'before_recording'} & set(calls))
        self.assertEqual(self.attempted_at_click, [False])
        self.assertNotIn('issue_attempt', kinds(self.store, key))
        self.assertNotIn('needs_user', kinds(self.store, key))

    async def test_a_known_preparation_is_not_held_while_the_person_watches_the_ticket(self):
        model = Model(step('click', 'button', 'Agregar'), WAIT)
        key, _ = self.begin(recording(), model, before=self.runner.explicit_approval.add)
        await self.paused(key, 'blocked')
        self.assertEqual(self.clicks, 0)
        await self.runner.stop(key)
        recorder = recording(known_preparation=lambda _complement, view: view.name == 'Agregar')
        model = Model(step('click', 'button', 'Agregar'), WAIT)
        key, _ = self.begin(recorder, model, before=self.runner.explicit_approval.add)
        await self.until(lambda: self.clicks == 1, key)
        await self.runner.stop(key)
        self.assertNotIn('needs_user', kinds(self.store, key))

    async def test_a_risk_raised_by_the_complement_goes_alone_and_through_the_gate(self):
        raised = recording(adjust_risk=lambda _complement, view: 'issue' if view.name == 'Continuar' else 'normal')
        both = Plan(steps=[step('click', 'button', 'Buscar'), step('click', 'button', 'Continuar')])
        model = Model(both, step('click', 'button', 'Continuar'))
        key, _ = self.begin(raised, model)
        await self.paused(key, 'issue')
        # The first button ran; the second one was dropped from that plan and asked for again, alone.
        self.assertEqual((self.clicks, len(model.seen)), (1, 2))
        self.assertIn('una acción de riesgo va sola', json.dumps(model.seen[1]['acciones_recientes'], ensure_ascii=False))
        self.assertFalse(self.store.get(key)['issue_attempted'])
        await self.runner.stop(key)
        # And a risk the complement lowers runs as an ordinary step.
        lowered = recording(adjust_risk=lambda *_: 'normal')
        key, _ = self.begin(lowered, Model(step('click', 'button', 'Facturar'), WAIT))
        await self.until(lambda: self.clicks == 2, key)
        await self.runner.stop(key)
        self.assertNotIn('issue_attempt', kinds(self.store, key))
        self.assertFalse({'before_issue', 'issue_proof'} & set(lowered.made[0].calls))


class RecoveryTests(unittest.IsolatedAsyncioTestCase):
    """What a retry asks of a complement outside any attempt."""

    async def asyncSetUp(self):
        self.root = Path(self.enterContext(tempfile.TemporaryDirectory()))
        self.store = Store(self.root)
        self.runner = Runner(self.store)
        self.addAsyncCleanup(self.runner.close)
        self.key = self.store.add('ticket.png', 'recuperacion')[0]['id']
        self.store.save_receipt(self.key, RECEIPT, ready=True)

    async def test_the_complement_searches_before_the_mailbox_and_can_end_the_recovery(self):
        asked = []

        class Finder(Complement):
            @staticmethod
            async def recover_documents(runner, key, profile, receipt, notes):
                asked.append((key, receipt.provider))
                notes.append('El portal entregó los archivos.')
                return True

        self.runner.recover = AsyncMock(return_value=False)
        with (patch.dict(complementos.REGISTRY, {RECEIPT.provider: Finder}),
              patch('facturador.ticket_retry.GmailConnection', side_effect=AssertionError('No toca el correo'))):
            await recover_ticket(self.runner, self.key, PROFILE)
        self.assertEqual(asked, [(self.key, RECEIPT.provider)])
        self.assertNotIn('recovery_checked', kinds(self.store, self.key))

    async def test_preparing_again_after_a_negative_check_is_a_capability_of_the_complement(self):
        class Rechecked(Complement):
            prepares_after_negative_check = True

        self.store.queue(self.key, PROFILE)
        self.store.enter_intervention(self.key, 'unknown', 'Control', True)
        self.store.mark_human_exposed(self.key)
        self.store.update(self.key, status='uncertain')
        with (patch.object(self.runner.plan, 'status', return_value={'status': 'ready'}),
              patch.object(self.store, 'queue_after_negative_check', return_value=True) as queued,
              patch.object(self.runner, 'track_job') as tracked):
            self.assertFalse(self.runner.prepare_checked_ticket(self.key, PROFILE))      # the plain one never does
            queued.assert_not_called()
            with patch.dict(complementos.REGISTRY, {RECEIPT.provider: Rechecked}):
                self.assertTrue(self.runner.prepare_checked_ticket(self.key, PROFILE))
            queued.assert_called_once()
            tracked.call_args.args[1].close()        # the run it would start; never awaited here

    async def test_with_the_switch_off_a_retry_asks_no_complement(self):
        key = next(key for key in sorted(complementos.REGISTRY) if listed_class(key).prepares_after_negative_check)
        self.store.save_receipt(self.key, RECEIPT.model_copy(update={'provider': key}), ready=True)
        self.store.queue(self.key, PROFILE)
        self.store.enter_intervention(self.key, 'unknown', 'Control', True)
        self.store.mark_human_exposed(self.key)
        self.store.update(self.key, status='uncertain')
        for value, expected in (('0', []), ('1', [(key, 'recover_documents')])):
            with (watched_complements() as calls, patch.dict(os.environ, {'FACTURADOR_COMPLEMENTOS': value}),
                  patch.object(self.runner.plan, 'status', return_value={'status': 'ready'})):
                with self.store.connect() as db:
                    db.execute('DELETE FROM recovery_requests')
                answer = await self.runner.retry_job(self.key)
                self.assertEqual(answer['action'], 'recovering', answer)
                await self.runner.job_tasks[self.key]
            self.assertEqual(calls, expected, value)
            self.assertEqual(self.store.get(self.key)['status'], 'uncertain')


class AtStart(Portal):
    """The made-up portal, answering at the address where a catalog merchant's own portal starts."""

    def __init__(self, start: str, **options):
        address = urlsplit(start)
        super().__init__(f'{address.scheme}://{address.netloc}', **options)
        self.start = address.path or '/'
        self.attempted_when_emitting = []

    async def handle(self, route):
        if urlsplit(route.request.url).path == self.start != '/':
            await route.fulfill(content_type='text/html; charset=utf-8', body=self.lookup())
        else:
            await super().handle(route)


class SwitchedOffTests(PortalCase):
    """FACTURADOR_COMPLEMENTOS=0: a listed merchant's ticket runs on the general engine alone."""

    async def asyncSetUp(self):
        await super().asyncSetUp()
        # These addresses belong to real merchants: whatever is not served from memory must fail.
        await self.runner.context.set_offline(True)
        self.enterContext(patch.dict(os.environ, {'FACTURADOR_COMPLEMENTOS': '0'}))
        self.calls = self.enterContext(watched_complements())
        self.given = []

        def asked(provider):
            self.given.append(complement_class(provider))
            return self.given[-1]

        self.enterContext(patch('facturador.runner.complement_class', side_effect=asked))
        proof = self.runner.generic_issue_proof
        self.proofs = []

        async def general_proof(page, profile, receipt):
            self.proofs.append((receipt.provider, await proof(page, profile, receipt)))
            return self.proofs[-1][1]

        self.runner.generic_issue_proof = general_proof

    async def merchant(self, key, number):
        portal = await self.serve(AtStart(CATALOG[key]['url']))
        receipt = portal.add(Receipt(provider=key, merchant=CATALOG[key]['name'], amount=f'{210 + number}.50',
                                     date='2026-11-13', ticket_number=f'TKT-7700{number}-S', payment_code='28'))
        return portal, receipt

    async def test_every_listed_merchant_reaches_the_emission_gate_by_the_general_way(self):
        for number, key in enumerate(sorted(complementos.REGISTRY)):
            with self.subTest(merchant=key):
                portal, receipt = await self.merchant(key, number)
                brain = Brain()
                ticket = await self.bill(receipt, brain, auto_issue=False, until='issue', timeout=120)
                # The made-up portal was walked like any unknown one: lookup, fiscal data, confirmation.
                self.assertEqual(portal.lookups, [{'folio': receipt.ticket_number, 'fecha': '13/11/2026',
                                                   'total': receipt.amount}])
                self.assertEqual((portal.fiscal[0]['rfc'], portal.fiscal[0]['regimen'], portal.fiscal[0]['pago']),
                                 (PROFILE.rfc, '612', '28'))
                self.assertEqual(urlsplit(self.runner.viewer.page.url).path, '/confirmar')
                # The gate is the general one and nothing was sent: the person decides.
                self.assertEqual(self.proofs[-1], (key, True))
                self.assertEqual(portal.emitted, [])
                self.assertFalse(self.store.get(ticket)['issue_attempted'])
                self.assertNotIn('issue_attempt', kinds(self.store, ticket))
                self.assertEqual(self.given[-1], Complement)
                await self.runner.stop(ticket)
        self.assertEqual(self.calls, [])                                  # no complement was built or asked
        self.assertEqual(set(self.given), {Complement})
        self.assertEqual(len(self.given), len(complementos.REGISTRY))

    async def test_the_general_proof_alone_lets_a_listed_merchant_emit(self):
        key = sorted(complementos.REGISTRY)[0]
        portal, receipt = await self.merchant(key, 9)
        handle = portal.handle

        async def watched(route):
            if route.request.method == 'POST':
                portal.attempted_when_emitting.append(bool(self.store.get(self.ticket)['issue_attempted']))
            await handle(route)

        await self.runner.context.route(portal.origin + '/emitir', watched)
        self.ticket = self.add(receipt)
        with patch('facturador.runner.AI.next_action', side_effect=Brain()):
            self.runner.schedule(self.ticket, PROFILE, auto_issue=True)
            task = self.runner.job_tasks[self.ticket]
            async with asyncio.timeout(120):
                while not portal.emitted and not task.done():
                    await asyncio.sleep(.03)
            if not task.done():
                await self.runner.stop(self.ticket)
        # One emission, recorded before the portal received it, proven by what the page showed.
        self.assertEqual(len(portal.emitted), 1, self.store.events(self.ticket)[-8:])
        self.assertEqual(portal.attempted_when_emitting, [True])
        self.assertEqual(kinds(self.store, self.ticket).count('issue_attempt'), 1)
        self.assertEqual(self.proofs, [(key, True)])
        self.assertFalse(self.store.get(self.ticket)['human_exposed'])
        self.assertEqual((self.calls, self.given), ([], [Complement]))


if __name__ == '__main__':
    unittest.main()
