"""Reading speed: one OCR per page, several tickets at once, visible stages and timings."""
import asyncio
import io
import json
import subprocess
import tempfile
import threading
import time
import unittest
from pathlib import Path
from unittest.mock import Mock, patch

from PIL import Image

from facturador import local_reader
from facturador.ai import read_receipt
from facturador.auth import PlanError
from facturador.automation import MAX_PARALLEL_READS, READ_PHASES, TicketAutomation, read_ticket
from facturador.db import Store
from facturador.ingest import save_input
from facturador.models import Extraction, PortalSearch, Receipt
from facturador.runner import Runner


def unread_locally(row):
    return Receipt.model_validate(row['receipt']), '', 'lector_local'


def photo(color, size=(40, 30)):
    output = io.BytesIO()
    Image.new('RGB', size, color).save(output, 'PNG')
    return output.getvalue()


def extraction(**changes):
    fields = dict(is_receipt=True, provider='capufe', merchant='CAPUFE', amount='40.50', date='2026-06-15',
                  ticket_number='PRUEBA-A', billing_codes=[], billing_url=None, payment_code='28',
                  payment_evidence='Pago con tarjeta de débito', details='', uncertainties=[])
    return Extraction.model_validate({**fields, **changes})


class FakeTesseract:
    """Counts OCR runs; the language probe is answered separately."""
    def __init__(self, text):
        self.text, self.runs = text, 0
        self.lock = threading.Lock()

    def __call__(self, command, **options):
        if command[:2] == ['tesseract', '--list-langs']:
            return subprocess.CompletedProcess(command, 0, stdout='spa\neng\n', stderr='')
        with self.lock:
            self.runs += 1
        text = self.text(options['input']) if callable(self.text) else self.text
        return subprocess.CompletedProcess(command, 0, stdout=text.encode(), stderr=b'')


class SingleOcrTests(unittest.TestCase):
    def setUp(self):
        local_reader.clear_page_cache()
        self.addCleanup(local_reader.clear_page_cache)
        folder = tempfile.TemporaryDirectory()
        self.addCleanup(folder.cleanup)
        self.root = Path(folder.name)

    def image(self, name, color='white'):
        path = self.root / name
        path.write_bytes(photo(color))
        return path

    def row(self, path):
        return {'id': 'prueba', 'source_path': str(path), 'receipt': {'source': 'archivo: ticket.png'},
                'batch_id': '', 'auto_stage': '', 'confirmed_payment': ''}

    def read(self, tesseract, path, companions=()):
        plan = Mock()
        plan.infer.return_value = extraction()
        with patch('facturador.local_reader.subprocess.run', side_effect=tesseract), \
             patch('facturador.local_reader.ocr_languages', return_value='spa+eng'):
            result = read_ticket(plan, self.row(path), True, self.root, list(companions))
        return plan, result

    def test_full_read_of_one_photo_runs_tesseract_once_and_sends_its_text(self):
        tesseract = FakeTesseract('CAPUFE\nFOLIO PRUEBA-A\nTOTAL 40.50')
        plan, (receipt, ready, note, provenance) = self.read(tesseract, self.image('ticket.png'))
        self.assertEqual(tesseract.runs, 1)
        self.assertTrue(ready, note)
        self.assertEqual(plan.infer.call_count, 1)  # A clear ticket costs one model call.
        content = plan.infer.call_args.args[1]
        self.assertEqual(content[0]['type'], 'input_image')
        self.assertIn('CAPUFE', content[-1]['text'])
        self.assertIn('ocr', provenance['tools'])

    def test_rotation_fallback_is_the_only_second_run(self):
        tesseract = FakeTesseract(lambda data: 'texto ilegible')
        _, (receipt, ready, note, provenance) = self.read(tesseract, self.image('ticket.png'))
        self.assertEqual(tesseract.runs, 2)
        self.assertNotIn('ocr_giro_180', provenance['tools'])

    def test_front_and_back_run_tesseract_once_per_image(self):
        tesseract = FakeTesseract('CAPUFE\nFOLIO PRUEBA-A\nFacturación en el reverso')
        front, back = self.image('frente.png', 'white'), self.image('reverso.png', 'gray')
        plan, (receipt, ready, note, _) = self.read(tesseract, front, [back])
        self.assertEqual(tesseract.runs, 2)
        images = [part for part in plan.infer.call_args.args[1] if part['type'] == 'input_image']
        self.assertEqual(len(images), 2)

    def test_photo_reaches_the_model_when_ocr_fails_or_is_missing(self):
        path = self.image('sin-texto.png')
        broken = Mock(side_effect=subprocess.TimeoutExpired('tesseract', 25))
        with patch('facturador.local_reader.subprocess.run', broken), \
             patch('facturador.local_reader.ocr_languages', return_value='spa'):
            content = read_receipt(path)
            self.assertEqual([part['type'] for part in content], ['input_image'])
            self.assertEqual(read_receipt(path), content)
        self.assertEqual(broken.call_count, 1)  # A failed OCR is not repeated at once either.
        local_reader.clear_page_cache()
        with patch('facturador.local_reader.ocr_languages', return_value=''):
            self.assertEqual([part['type'] for part in read_receipt(path)], ['input_image'])
            with self.assertRaises(ValueError):
                local_reader.read_local_page(path)

    def test_cache_returns_copies_and_follows_file_changes(self):
        path = self.image('ticket.png')
        tesseract = FakeTesseract('PRIMERA')
        with patch('facturador.local_reader.subprocess.run', side_effect=tesseract), \
             patch('facturador.local_reader.ocr_languages', return_value='spa'):
            first = local_reader.read_local_page(path)
            first['codes'].append('alterado')
            first['text'] = 'alterado'
            again = local_reader.read_local_page(path)
            self.assertEqual((again['text'], again['codes']), ('PRIMERA', []))
            self.assertEqual(tesseract.runs, 1)
            local_reader.read_local_page(path, rotation=180, force_ocr=True)
            self.assertEqual(tesseract.runs, 2)
            tesseract.text = 'SEGUNDA'
            path.write_bytes(photo('black', (41, 30)))
            self.assertEqual(local_reader.read_local_page(path)['text'], 'SEGUNDA')
            for number in range(local_reader.PAGE_CACHE_SIZE + 5):
                local_reader.read_local_page(self.image(f'otra-{number}.png'))
        self.assertLessEqual(len(local_reader._page_cache), local_reader.PAGE_CACHE_SIZE)


class ConcurrentReadTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        local_reader.clear_page_cache()
        folder = tempfile.TemporaryDirectory()
        self.addCleanup(folder.cleanup)
        self.root = Path(folder.name)
        self.store = Store(self.root)
        self.plan = Mock()
        self.plan.status.return_value = {'status': 'ready'}
        self.worker = TicketAutomation(self.store, Mock(), self.plan, Mock())
        # These photos have no printed text here; nothing depends on a local Tesseract.
        languages = patch('facturador.local_reader.ocr_languages', return_value='')
        languages.start()
        self.addCleanup(languages.stop)

    async def asyncTearDown(self):
        await self.worker.close()

    def chat_ticket(self, color):
        key = save_input(self.store, color + '.png', photo(color))['id']
        self.store.queue_chat_job(key)
        return key, Path(self.store.get(key)['source_path'])

    async def until(self, condition, seconds=5):
        deadline = time.monotonic() + seconds
        while not condition():
            if time.monotonic() > deadline:
                self.fail('La condición no se cumplió a tiempo.')
            await asyncio.sleep(0.01)

    def finished(self, keys):
        return all(self.store.get(key)['status'] not in {'new'} and
                   not self.store.get(key)['auto_stage'].startswith('reading:') for key in keys)

    async def test_three_slow_reads_overlap_and_one_failure_does_not_stop_the_others(self):
        tickets = [self.chat_ticket(color) for color in ('red', 'green', 'blue')]
        failing = tickets[1][1]
        # Each read stays inside the model until the three of them are there at the same time. That
        # they overlap is proved by all of them getting out, not by how long the whole thing took:
        # read one after another, the first would wait for company that never comes.
        together = threading.Barrier(3)
        overlapped = []

        def slow(path, _catalog, _companions, **_):
            try:
                together.wait(20)
                overlapped.append(path)
            except threading.BrokenBarrierError:
                pass
            if path == failing:
                raise RuntimeError('lectura rota')
            return extraction(ticket_number='FOLIO-' + path.stem[:8])

        with patch('facturador.automation.AI.extract', side_effect=slow) as extract, \
             patch('facturador.automation.local_ticket', side_effect=unread_locally):
            self.worker.start()
            await self.until(lambda: self.finished([key for key, _ in tickets]), 60)
        self.assertEqual(extract.call_count, 3)
        self.assertEqual(sorted(overlapped), sorted(path for _, path in tickets))
        statuses = {key: self.store.get(key)['status'] for key, _ in tickets}
        self.assertEqual(statuses[tickets[1][0]], 'review')
        self.assertIn('No se pudo completar la lectura', self.store.get(tickets[1][0])['message'])
        for key, _ in (tickets[0], tickets[2]):
            self.assertEqual(self.store.get(key)['receipt']['amount'], '40.50')

    async def test_queue_never_reads_more_than_the_limit_at_once(self):
        tickets = [self.chat_ticket(color) for color in ('red', 'green', 'blue', 'yellow', 'black')]
        active, peak, lock = [0], [0], threading.Lock()
        # The readings stay inside the model until the test lets them out, so how many overlap
        # does not depend on how fast this machine starts each one.
        full, leave = threading.Event(), threading.Event()
        self.addCleanup(leave.set)

        def slow(path, _catalog, _companions, **_):
            with lock:
                active[0] += 1
                peak[0] = max(peak[0], active[0])
                if active[0] >= MAX_PARALLEL_READS:
                    full.set()
            leave.wait(30)
            with lock:
                active[0] -= 1
            return extraction(ticket_number='FOLIO-' + path.stem[:8])

        with patch('facturador.automation.AI.extract', side_effect=slow) as extract:
            self.worker.start()
            # The queue takes every place it has...
            self.assertTrue(await asyncio.to_thread(full.wait, 30))
            # ...and with all of them taken it starts nothing else, however long those readings last.
            await asyncio.sleep(0.5)
            self.assertEqual((active[0], extract.call_count), (MAX_PARALLEL_READS, MAX_PARALLEL_READS))
            leave.set()
            await self.until(lambda: self.finished([key for key, _ in tickets]), 60)
        self.assertEqual(extract.call_count, 5)
        self.assertEqual(peak[0], MAX_PARALLEL_READS)

    async def test_unexpected_error_in_one_background_read_is_contained(self):
        (bad, _), (good, _) = self.chat_ticket('red'), self.chat_ticket('blue')
        original = self.worker.analyze

        async def analyze(key, **options):
            if key == bad:
                raise RuntimeError('fallo inesperado')
            return await original(key, **options)

        self.worker.analyze = analyze
        with patch('facturador.automation.AI.extract', return_value=extraction()), \
             self.assertLogs('facturador.automation', 'ERROR'):
            self.assertTrue(await self.worker.chat_step())
        self.assertEqual(self.store.get(good)['receipt']['amount'], '40.50')
        self.assertEqual(self.worker.reads, {})

    async def test_status_shows_each_reading_stage_and_drops_it_when_done(self):
        key, _ = self.chat_ticket('red')
        reading, searching, release = threading.Event(), threading.Event(), threading.Event()

        def infer(_instructions, _content, output_type, *, effort='medium', web_search=False):
            if web_search:
                searching.set()
                release.wait(5)
                return PortalSearch(billing_url=None, evidence_url=None), []
            reading.set()
            release.wait(5)
            return extraction(provider='unknown', merchant='Cafetería de prueba')

        self.plan.infer.side_effect = infer
        before = time.time()
        state = self.worker.status()
        self.assertEqual(state['jobs'], {})
        self.assertTrue({'enabled', 'current_id', 'phase', 'message'} <= set(state))
        self.worker.start()
        await asyncio.to_thread(reading.wait, 5)
        await self.until(lambda: self.worker.status()['jobs'].get(key, {}).get('phase') == 'leyendo')
        job = self.worker.status()['jobs'][key]
        self.assertEqual(job['message'], READ_PHASES['leyendo'])
        self.assertGreaterEqual(job['since'], before)
        release.set()
        await asyncio.to_thread(searching.wait, 5)
        await self.until(lambda: key not in self.worker.status()['jobs'])
        self.assertEqual(self.worker.status()['jobs'], {})

        timing = [json.loads(event['detail']) for event in self.store.events(key) if event['kind'] == 'read_timing']
        self.assertEqual(len(timing), 1)
        self.assertEqual(set(timing[0]), {'total_ms', 'ocr_ms', 'model_ms', 'web_search_ms', 'effort', 'escalated'})
        self.assertEqual((timing[0]['effort'], timing[0]['escalated']), ('low', False))
        for field in ('total_ms', 'ocr_ms', 'model_ms', 'web_search_ms'):
            self.assertIsInstance(timing[0][field], int)
        self.assertGreaterEqual(timing[0]['total_ms'], timing[0]['model_ms'])

    async def test_search_stage_is_reported(self):
        key, _ = self.chat_ticket('red')
        searching, release = threading.Event(), threading.Event()

        def infer(_instructions, _content, output_type, *, effort='medium', web_search=False):
            if web_search:
                searching.set()
                release.wait(5)
                time.sleep(.02)  # A search this fast would round down to 0 ms on a quick machine.
                return PortalSearch(billing_url=None, evidence_url=None), []
            return extraction(provider='unknown', merchant='Cafetería de prueba')

        self.plan.infer.side_effect = infer
        self.worker.start()
        await asyncio.to_thread(searching.wait, 5)
        await self.until(lambda: self.worker.status()['jobs'].get(key, {}).get('phase') == 'buscando_portal')
        release.set()
        await self.until(lambda: self.finished([key]))
        timing = next(json.loads(e['detail']) for e in self.store.events(key) if e['kind'] == 'read_timing')
        self.assertGreater(timing['web_search_ms'], 0)

    async def test_escalated_read_is_recorded(self):
        key, _ = self.chat_ticket('red')
        self.plan.infer.side_effect = [extraction(amount=None), extraction()]
        self.assertTrue(await self.worker.chat_step())
        self.assertEqual([call.kwargs['effort'] for call in self.plan.infer.call_args_list], ['low', 'high'])
        timing = next(json.loads(e['detail']) for e in self.store.events(key) if e['kind'] == 'read_timing')
        self.assertEqual((timing['effort'], timing['escalated']), ('high', True))

    async def test_model_wait_keeps_new_reads_local(self):
        tickets = [self.chat_ticket(c) for c in ('red', 'green', 'blue', 'yellow')]
        calls, during_wait, lock = [], [], threading.Lock()

        def read(path, _catalog, _companions, **_):
            with lock:
                if self.worker.plan_read_after > time.time():
                    during_wait.append(path)
                calls.append((path, time.monotonic()))
                first = len(calls) == 1
            if first:
                raise PlanError('Cupo en pausa.', 'limit', 60)
            time.sleep(0.3)
            return extraction(ticket_number='FOLIO-' + path.stem[:8])

        with patch('facturador.automation.AI.extract', side_effect=read), \
             patch('facturador.automation.local_ticket', side_effect=unread_locally), \
             self.assertNoLogs('facturador.automation', 'ERROR'):
            self.worker.start()
            await self.until(lambda: self.finished([key for key, _ in tickets]))
        self.assertGreater(self.worker.plan_read_after, time.time())
        # Only reads already started with the first one reached the model; the fourth
        # ticket could only start after a slot freed, so it was read locally.
        self.assertLessEqual(len(calls), MAX_PARALLEL_READS)
        self.assertEqual(during_wait, [])
        read_by_model = {path for path, _ in calls}
        local = [key for key, path in tickets if path not in read_by_model]
        self.assertGreaterEqual(len(local), 1)
        for key in local:
            self.assertEqual(self.store.get(key)['auto_stage'], 'offline')

    async def test_close_cancels_reads_and_releases_their_claims(self):
        key, _ = self.chat_ticket('red')
        entered, release, left = threading.Event(), threading.Event(), threading.Event()
        self.addCleanup(release.set)

        def stuck(*_args, **_options):
            entered.set()
            release.wait(10)
            return extraction()

        def tracked(*args, **options):
            # The reader thread outlives the cancelled read: the test waits for it to leave
            # before the folder is removed, so its open original never blocks the clean-up.
            try:
                return read_ticket(*args, **options)
            finally:
                left.set()

        with patch('facturador.automation.AI.extract', side_effect=stuck), \
                patch('facturador.automation.read_ticket', side_effect=tracked):
            self.worker.start()
            await asyncio.to_thread(entered.wait, 5)
            self.assertIn(key, self.worker.status()['jobs'])
            await asyncio.wait_for(self.worker.close(), 3)
            row = self.store.get(key)
            self.assertEqual((row['status'], row['auto_stage']), ('review', 'retry_read'))
            self.assertIn('Se interrumpió la lectura', row['message'])
            self.assertEqual(self.worker.reads, {})
            self.assertEqual(self.worker.status()['jobs'], {})
            release.set()
            self.assertTrue(await asyncio.to_thread(left.wait, 15))
        self.assertFalse([e for e in self.store.events(key) if e['kind'] == 'read_timing'])


class ParallelPairingTests(unittest.IsolatedAsyncioTestCase):
    """Front and back of one upload, read in parallel, pair exactly as when read one by one."""

    async def scenario(self, order):
        """Read the three images of one upload at once and let their readings end in ``order``.

        Each group of ``order`` ends together and before the next one starts ending. Events decide
        the order, not sleeps, so a busy machine cannot change which reading ends first.
        """
        folder = tempfile.TemporaryDirectory()
        self.addCleanup(folder.cleanup)
        store = Store(Path(folder.name))
        plan = Mock()
        plan.status.return_value = {'status': 'ready'}
        runner = Runner(store, plan)
        worker = TicketAutomation(store, runner, plan, Mock(side_effect=ValueError('sin perfil')))
        worker.configure(True)
        keys = {name: save_input(store, name + '.png', photo(color), batch_id='una-carga')['id']
                for name, color in (('frente', 'red'), ('reverso', 'blue'), ('otro', 'green'))}
        paths = {Path(store.get(key)['source_path']): name for name, key in keys.items()}
        seen, active, peak, lock = [], [0], [0], threading.Lock()
        entered = {name: threading.Event() for name in keys}
        leave = {name: threading.Event() for name in keys}
        self.addCleanup(lambda: [event.set() for event in leave.values()])

        def read(path, _catalog, companions, **_):
            name = paths[path]
            with lock:
                seen.append((name, [paths[extra] for extra in companions]))
                first = not entered[name].is_set()  # The front is read once more, later, with its back.
                active[0] += 1
                peak[0] = max(peak[0], active[0])
            if first:
                entered[name].set()
                leave[name].wait(30)
            with lock:
                active[0] -= 1
            fields = dict(provider='vianney', merchant='Vianney Textil Hogar', ticket_number='TEST-101',
                          payment_code='01', payment_evidence='Pago en efectivo')
            if name == 'reverso':
                fields.update(merchant='Vianney', is_receipt=False, amount=None, date=None, ticket_number=None,
                              payment_code=None, payment_evidence=None, details='Reverso con QR de facturación.')
            if name == 'otro':
                fields.update(provider='bp', merchant='BP', ticket_number='TEST-202')
            return extraction(**fields)

        with patch('facturador.automation.AI.extract', side_effect=read), \
             patch('facturador.automation.read_local_page', return_value={'text': 'Vianney facturación'}), \
             patch('facturador.automation.portal_evidence', return_value=([], [])):
            worker.start()
            # The three readings are inside the model at the same time before any of them may end.
            for name in keys:
                self.assertTrue(await asyncio.to_thread(entered[name].wait, 30), (name, seen))
            first_reads = {name: worker.reads[key] for name, key in keys.items()}
            for group in order:
                for name in group:
                    leave[name].set()
                # Its result is saved and paired before the next reading is allowed to end.
                await asyncio.wait_for(asyncio.gather(*(first_reads[name] for name in group)), 30)
            deadline = time.monotonic() + 30
            while not (('frente', ['reverso']) in seen and not worker.reads):
                self.assertLess(time.monotonic(), deadline, seen)
                await asyncio.sleep(0.01)
            await worker.close()
        await runner.close()
        return store, keys, seen, peak[0]

    async def test_back_joins_front_whatever_finishes_first(self):
        for order in ([['reverso'], ['otro'], ['frente']],
                      [['frente'], ['otro'], ['reverso']],
                      [['frente', 'reverso', 'otro']]):
            with self.subTest(order=order):
                store, keys, seen, peak = await self.scenario(order)
                self.assertGreaterEqual(peak, 2)  # They really were read at the same time.
                self.assertEqual(store.get(keys['reverso'])['status'], 'attached')
                self.assertEqual(store.get(keys['reverso'])['companion_of'], keys['frente'])
                self.assertEqual(store.get(keys['otro'])['companion_of'], '')
                self.assertNotEqual(store.get(keys['otro'])['status'], 'attached')
                self.assertEqual(sorted(seen[:3]), [('frente', []), ('otro', []), ('reverso', [])])
                self.assertEqual(seen.count(('frente', ['reverso'])), 1)
                pairs = [e for e in store.events(keys['frente']) if e['kind'] == 'companion_attached']
                self.assertEqual([e['detail'] for e in pairs], [keys['reverso']])
                self.assertEqual(store.get(keys['frente'])['receipt']['ticket_number'], 'TEST-101')


if __name__ == '__main__':
    unittest.main()
