"""A multi-photo upload keeps separate purchases separate and reads fiscal backsides."""
import io
import asyncio
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock, patch

from fastapi.testclient import TestClient
from PIL import Image

from facturador.ai import AI
from facturador.app import create_app
from facturador.automation import TicketAutomation
from facturador.db import Store
from facturador.ingest import save_input
from facturador.models import Receipt
from facturador.runner import Runner


def photo(color):
    output = io.BytesIO()
    Image.new('RGB', (24, 20), color).save(output, 'PNG')
    return output.getvalue()


class BatchCompanionTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.store = Store(self.root)

    def add(self, name, color, batch):
        return save_input(self.store, name, photo(color), batch_id=batch)['id']

    def test_http_upload_marks_only_new_files_in_the_same_batch(self):
        first = self.add('old.png', 'black', 'prior-request')
        app = create_app(self.root)
        with TestClient(app) as client:
            token = client.get('/api/state').json()['token']
            reply = client.post('/api/upload', headers={'x-app-token': token}, files=[
                ('files', ('old-again.png', photo('black'), 'image/png')),
                ('files', ('front.png', photo('red'), 'image/png')),
                ('files', ('back.png', photo('blue'), 'image/png')),
            ])
            reply.raise_for_status()
            old, front, back = reply.json()
        self.assertEqual(old, {'id': first, 'created': False, 'status': 'new'})
        self.assertEqual(self.store.get(first)['batch_id'], 'prior-request')
        self.assertTrue(front['created'])
        self.assertTrue(back['created'])
        self.assertEqual(self.store.get(front['id'])['batch_id'], self.store.get(back['id'])['batch_id'])
        self.assertNotEqual(self.store.get(front['id'])['batch_id'], 'prior-request')

    def test_fiscal_back_joins_only_its_unique_purchase_in_mixed_batch(self):
        batch = 'one-upload'
        front = self.add('front.png', 'red', batch)
        back = self.add('back.png', 'blue', batch)
        fuel = self.add('fuel.png', 'green', batch)
        cafe = self.add('cafe.png', 'yellow', batch)
        self.store.save_receipt(front, Receipt(provider='vianney', merchant='Vianney Textil Hogar, sucursal Centro',
                                               amount='123.45', date='2026-01-02', ticket_number='TEST-101'))
        self.store.save_receipt(back, Receipt(provider='vianney', merchant='Vianney',
                                              details='El reverso muestra un QR de facturación.'))
        self.store.save_receipt(fuel, Receipt(provider='bp', merchant='BP', amount='600.00',
                                              date='2026-01-02', ticket_number='TEST-202'))
        self.assertEqual(self.store.pair_batch_if_unique(back), [])  # Another image is not read yet.
        self.store.save_receipt(cafe, Receipt(provider='unknown', merchant='Cafetería de prueba',
                                              amount='90.00', date='2026-01-02', ticket_number='TEST-303'))
        self.assertEqual(self.store.pair_batch_if_unique(cafe), [(front, [back])])
        self.assertEqual(self.store.get(back)['status'], 'attached')
        self.assertEqual(self.store.get(back)['companion_of'], front)
        self.assertEqual(self.store.get(front)['auto_stage'], 'retry_read')
        self.assertEqual(self.store.get(fuel)['companion_of'], '')
        self.assertEqual(self.store.get(cafe)['companion_of'], '')
        self.assertEqual(self.store.companion_paths(front), [Path(self.store.get(back)['source_path'])])
        self.assertTrue(Path(self.store.get(back)['source_path']).is_file())

    def test_two_complete_purchases_of_same_brand_leave_back_unpaired(self):
        batch = 'ambiguous'
        one = self.add('one.png', 'red', batch)
        two = self.add('two.png', 'blue', batch)
        back = self.add('back.png', 'green', batch)
        for key, folio in [(one, 'TEST-101'), (two, 'TEST-102')]:
            self.store.save_receipt(key, Receipt(provider='vianney', merchant='Vianney Textil Hogar',
                                                 amount='100.00', date='2026-01-02', ticket_number=folio))
        self.store.save_receipt(back, Receipt(provider='vianney', merchant='Vianney',
                                              details='Código para facturar en el reverso.'))
        self.assertEqual(self.store.pair_batch_if_unique(back), [])
        self.assertEqual(self.store.get(back)['status'], 'review')
        self.assertFalse(self.store.companion_paths(one))
        self.assertFalse(self.store.companion_paths(two))

    def test_reader_sends_primary_and_companion_pixels_to_same_model_call(self):
        plan = Mock()
        plan.infer.return_value = object()
        with patch('facturador.ai.read_receipt', side_effect=lambda path: [
                {'type': 'input_image', 'image_url': str(path), 'detail': 'high'}]) as read:
            AI(plan).extract(Path('front.png'), {}, [Path('back.png')])
        self.assertEqual(read.call_count, 2)
        content = plan.infer.call_args.args[1]
        self.assertEqual([part['image_url'] for part in content if part['type'] == 'input_image'],
                         ['front.png', 'back.png'])
        self.assertIn('complementaria', content[2]['text'])

    def test_automatic_reader_retries_primary_with_attached_back(self):
        front = self.add('front.png', 'red', 'batch-to-read')
        back = self.add('back.png', 'blue', 'batch-to-read')
        front_path = Path(self.store.get(front)['source_path'])
        back_path = Path(self.store.get(back)['source_path'])
        plan = Mock()
        plan.status.return_value = {'status': 'ready'}
        runner = Runner(self.store, plan)
        worker = TicketAutomation(self.store, runner, plan, lambda: None)
        seen = []

        def extraction(path, _catalog, companions):
            seen.append((path, companions))
            fields = dict(provider='vianney', merchant='Vianney Textil Hogar', is_receipt=True,
                          amount='123.45', date='2026-01-02', ticket_number='TEST-101',
                          billing_codes=[], billing_url=None, payment_code='01',
                          payment_evidence='Pago en efectivo', details='', uncertainties=[])
            if path == back_path:
                fields.update(merchant='Vianney', is_receipt=False, amount=None, date=None,
                              ticket_number=None, payment_code=None, payment_evidence=None,
                              details='Reverso con QR de facturación.')
            return SimpleNamespace(**fields)

        async def exercise():
            with patch('facturador.automation.AI.extract', side_effect=extraction), \
                 patch('facturador.automation.read_local_page', return_value={'text':'Vianney facturación'}), \
                 patch('facturador.automation.portal_evidence', return_value=([], [])):
                await worker.analyze(front, automatic=True)
                await worker.analyze(back, automatic=True)
                self.assertEqual(self.store.get(back)['status'], 'attached')
                self.assertEqual(self.store.get(front)['auto_stage'], 'retry_read')
                await worker.analyze(front, automatic=True)
            await runner.close()

        asyncio.run(exercise())
        self.assertEqual(seen, [(front_path, []), (back_path, []), (front_path, [back_path])])
        self.assertEqual(self.store.get(front)['receipt']['ticket_number'], 'TEST-101')


if __name__ == '__main__':
    unittest.main()
