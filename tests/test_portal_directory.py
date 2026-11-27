import asyncio
import json
import sqlite3
import tempfile
import unittest
from pathlib import Path
from unittest.mock import AsyncMock, Mock, patch

from facturador.auth import strict_schema
from facturador.automation import TicketAutomation, read_ticket
from facturador.catalog import CATALOG
from facturador.db import Store
from facturador.documents import reconcile, save_candidate
from facturador.models import Extraction, Receipt
from facturador import portal_directory as directory
from facturador.runner import Runner
from test_core import FISCAL_UUID, PROFILE, RECEIPT, pdf_bytes, xml_bytes
from test_ticket_automation import extracted

ISSUER = 'CES101010AB1'  # Fictitious merchant RFC.
PORTAL = 'https://facturas.cafe-estrella.com.mx/emitir'


class ReceiptContactFieldsTests(unittest.TestCase):
    def test_valid_values_are_normalized(self):
        receipt = Receipt(issuer_rfc=' ces101010ab1 ', merchant_whatsapp='55 1234 5678',
                          merchant_email=' facturas@cafe.example.mx ')
        self.assertEqual((receipt.issuer_rfc, receipt.merchant_whatsapp, receipt.merchant_email),
                         ('CES101010AB1', '+525512345678', 'facturas@cafe.example.mx'))
        self.assertEqual(Receipt(merchant_whatsapp='+1 (212) 555-0100').merchant_whatsapp, '+12125550100')
        self.assertEqual(Receipt(merchant_whatsapp='52 55 1234 5678').merchant_whatsapp, '+525512345678')

    def test_invalid_values_are_kept_empty_without_breaking_the_read(self):
        receipt = Receipt(issuer_rfc='RFC-123', merchant_whatsapp='llama a la tienda',
                          merchant_email='no es correo')
        self.assertEqual((receipt.issuer_rfc, receipt.merchant_whatsapp, receipt.merchant_email), ('', '', ''))
        receipt = Receipt(issuer_rfc=None, merchant_whatsapp=12345, merchant_email='a@' + 'b' * 300 + '.mx')
        self.assertEqual((receipt.issuer_rfc, receipt.merchant_whatsapp, receipt.merchant_email), ('', '', ''))

    def test_old_saved_receipt_loads_and_keeps_its_identity(self):
        old = RECEIPT.model_dump(exclude={'issuer_rfc', 'merchant_whatsapp', 'merchant_email'})
        loaded = Receipt.model_validate(old)
        self.assertEqual((loaded.issuer_rfc, loaded.merchant_whatsapp, loaded.merchant_email), ('', '', ''))
        enriched = loaded.model_copy(update={'issuer_rfc': ISSUER, 'merchant_email': 'f@cafe.example.mx'})
        self.assertEqual(enriched.identity(), loaded.identity())
        self.assertEqual(enriched.merchant_key(), loaded.merchant_key())
        self.assertEqual(Receipt.model_validate_json(loaded.model_dump_json()), loaded)

    def test_extraction_schema_requires_new_nullable_fields(self):
        schema = strict_schema(Extraction)
        for field in ('issuer_rfc', 'merchant_whatsapp', 'merchant_email'):
            self.assertIn(field, schema['required'])
            self.assertIn({'type': 'null'}, schema['properties'][field]['anyOf'])
        reply = {'is_receipt': True, 'provider': 'unknown', 'merchant': 'Café', 'amount': None, 'date': None,
                 'ticket_number': None, 'billing_codes': [], 'billing_url': None, 'payment_code': None,
                 'payment_evidence': None, 'details': '', 'uncertainties': []}
        self.assertIsNone(Extraction.model_validate(reply).issuer_rfc)

    def test_read_passes_printed_contacts_with_their_source(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            path = root / 'ticket.txt'
            path.write_text('CAFE ESTRELLA\nTOTAL 40.50', encoding='utf-8')
            row = {'id': 'uno', 'source_path': str(path), 'receipt': {'source': 'archivo: ticket.txt'}}
            with patch('facturador.automation.AI.extract', return_value=extracted(
                    provider='unknown', merchant='Café Estrella', issuer_rfc='ces101010ab1',
                    merchant_whatsapp='5512345678', merchant_email='no-es-correo')), \
                 patch('facturador.automation.AI.discover_portal', return_value=('', '')):
                receipt, _, _, provenance = read_ticket(None, row, True, root)
        self.assertEqual((receipt.issuer_rfc, receipt.merchant_whatsapp, receipt.merchant_email),
                         (ISSUER, '+525512345678', ''))
        self.assertEqual(provenance['fields']['issuer_rfc'], 'vision_modelo')
        self.assertEqual(provenance['fields']['merchant_whatsapp'], 'vision_modelo')
        self.assertNotIn('merchant_email', provenance['fields'])


class DirectoryStoreTests(unittest.TestCase):
    def setUp(self):
        self.root = Path(self.enterContext(tempfile.TemporaryDirectory()))

    def test_lookup_by_rfc_then_by_name(self):
        self.assertIsNone(directory.lookup(self.root, rfc=ISSUER, merchant='Café Estrella'))
        directory.record(self.root, 'impreso', PORTAL, rfc=ISSUER, merchant='CAFÉ  Estrella')
        by_rfc = directory.lookup(self.root, rfc=ISSUER.lower())
        self.assertEqual((by_rfc['url'], by_rfc['level'], by_rfc['matched_by']), (PORTAL, 'impreso', 'rfc'))
        by_name = directory.lookup(self.root, rfc='XYZ010101AB1', merchant='cafe estrella')
        self.assertEqual((by_name['url'], by_name['matched_by']), (PORTAL, 'nombre'))

    def test_only_public_https_links_are_accepted(self):
        for url in ('http://facturas.example.mx/', 'https://localhost/factura', 'https://192.168.0.1/',
                    'https://usuario:clave@facturas.example.mx/', 'javascript:alert(1)'):
            self.assertFalse(directory.record(self.root, 'factura', url, rfc=ISSUER), url)
        self.assertIsNone(directory.lookup(self.root, rfc=ISSUER))

    def test_stronger_level_is_never_lowered(self):
        directory.record(self.root, 'factura', PORTAL, rfc=ISSUER, at=100)
        directory.record(self.root, 'busqueda', PORTAL, rfc=ISSUER, evidence_url='https://cafe.mx/f', at=200)
        directory.record(self.root, 'impreso', 'https://otro.example.mx/factura', rfc=ISSUER, at=300)
        best = directory.lookup(self.root, rfc=ISSUER)
        self.assertEqual((best['url'], best['level'], best['hits']), (PORTAL, 'factura', 1))
        self.assertEqual([alt['level'] for alt in best['alternates']], ['impreso'])
        directory.record(self.root, 'factura', 'https://otro.example.mx/factura', rfc=ISSUER, at=400)
        self.assertEqual(directory.lookup(self.root, rfc=ISSUER)['alternates'][0]['level'], 'factura')

    def test_equal_levels_keep_the_other_as_alternate(self):
        other = 'https://facturacion.cafe-estrella.mx/'
        directory.record(self.root, 'factura', PORTAL, rfc=ISSUER, at=100)
        directory.record(self.root, 'factura', other, rfc=ISSUER, at=200)
        best = directory.lookup(self.root, rfc=ISSUER)
        self.assertEqual(best['url'], other)  # Same hits: the most recent wins.
        self.assertEqual([alt['url'] for alt in best['alternates']], [PORTAL])
        directory.record(self.root, 'factura', PORTAL, rfc=ISSUER, at=300)
        best = directory.lookup(self.root, rfc=ISSUER)
        self.assertEqual((best['url'], best['hits']), (PORTAL, 2))
        self.assertEqual([alt['url'] for alt in best['alternates']], [other])

    def test_ticket_specific_parts_of_a_link_are_not_stored(self):
        directory.record(self.root, 'impreso', PORTAL + '?folio=PRUEBA-A&total=40.50#x', rfc=ISSUER)
        self.assertEqual(directory.lookup(self.root, rfc=ISSUER)['url'], PORTAL)
        directory.record(self.root, 'impreso', 'https://tickets.example.mx/t/PRUEBA-A', merchant='Tienda Ejemplo',
                         private=['PRUEBA-A'])
        self.assertEqual(directory.lookup(self.root, merchant='Tienda Ejemplo')['url'], 'https://tickets.example.mx/')

    def test_never_keyed_by_the_person(self):
        (self.root / 'profile.json').write_text(PROFILE.model_dump_json(), encoding='utf-8')
        self.assertFalse(directory.record(self.root, 'factura', PORTAL, rfc=PROFILE.rfc, merchant=PROFILE.name))


class LearningTests(unittest.TestCase):
    def setUp(self):
        self.root = Path(self.enterContext(tempfile.TemporaryDirectory()))
        self.store = Store(self.root)
        (self.root / 'profile.json').write_text(PROFILE.model_dump_json(), encoding='utf-8')

    def ticket(self, receipt, label='uno'):
        key = self.store.add('original-' + label, label)[0]['id']
        self.store.save_receipt(key, receipt, ready=True)
        for name, data in (('f.xml', xml_bytes(issuer=ISSUER if receipt.provider != 'samara' else 'DBM11011043A')),
                           ('f.pdf', pdf_bytes())):
            save_candidate(self.store, key, name, data)
        return key

    def custom(self, url):
        return RECEIPT.model_copy(update={'provider': 'portal_ticket', 'portal_url': url,
                                          'merchant': 'Café Estrella', 'billing_codes': [],
                                          'ticket_number': 'PRUEBA-A'})

    def test_verified_invoice_teaches_its_own_portal_by_issuer_rfc_and_name(self):
        key = self.ticket(self.custom(PORTAL))
        self.assertEqual(reconcile(self.store, key, PROFILE)['uuid'], FISCAL_UUID)
        entry = directory.lookup(self.root, rfc=ISSUER)
        self.assertEqual((entry['url'], entry['level']), (PORTAL, 'factura'))
        self.assertEqual(directory.lookup(self.root, merchant='Cafe Estrella')['url'], PORTAL)

    def test_catalog_invoice_teaches_the_catalog_link(self):
        key = self.ticket(RECEIPT)
        reconcile(self.store, key, PROFILE)
        entry = directory.lookup(self.root, rfc='DBM11011043A')
        self.assertEqual((entry['url'], entry['level']), (CATALOG['samara']['url'], 'factura'))

    def test_direct_request_without_portal_teaches_nothing(self):
        key = self.ticket(self.custom(PORTAL).model_copy(update={'provider': 'solicitud_directa', 'portal_url': ''}))
        reconcile(self.store, key, PROFILE)
        self.assertIsNone(directory.lookup(self.root, rfc=ISSUER, merchant='Café Estrella'))

    def test_directory_error_does_not_break_the_match(self):
        key = self.ticket(self.custom(PORTAL))
        with patch('facturador.portal_directory.record', side_effect=sqlite3.OperationalError('bloqueada')), \
             self.assertLogs('facturador.portal_directory', 'WARNING'):
            self.assertEqual(reconcile(self.store, key, PROFILE)['uuid'], FISCAL_UUID)
        self.assertEqual(self.store.get(key)['status'], 'verified')

    def test_directory_file_has_no_personal_data(self):
        url = PORTAL + '?rfc=' + PROFILE.rfc + '&correo=' + PROFILE.email + '&folio=PRUEBA-A'
        key = self.ticket(self.custom(url))
        reconcile(self.store, key, PROFILE)
        directory.record(self.root, 'impreso', 'https://tickets.example.mx/r/' + PROFILE.rfc, merchant='Otra Tienda')
        data = (self.root / directory.FILE).read_bytes()
        for private in (PROFILE.rfc, PROFILE.email, 'PRUEBA-A', FISCAL_UUID):
            self.assertNotIn(private.encode(), data)
            self.assertNotIn(private.lower().encode(), data)
        self.assertEqual(directory.lookup(self.root, rfc=ISSUER)['url'], PORTAL)


class DirectoryReadTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        folder = tempfile.TemporaryDirectory()
        self.addCleanup(folder.cleanup)
        self.root = Path(folder.name)
        self.store = Store(self.root)
        self.plan = Mock()
        self.plan.status.return_value = {'status': 'ready'}
        self.runner = Runner(self.store, self.plan)
        self.worker = TicketAutomation(self.store, self.runner, self.plan, lambda: PROFILE)
        self.worker.configure(True)

    def add(self, key, text):
        path = self.root / (key + '.txt')
        path.write_text(text, encoding='utf-8')
        return self.store.add(str(path), key, 'archivo: ticket.txt')[0]['id']

    def sources(self, key):
        event = next(e for e in self.store.events(key) if e['kind'] == 'fuentes_de_campos')
        return json.loads(event['detail'])

    async def test_second_ticket_of_an_invoiced_merchant_needs_no_search_nor_domain_question(self):
        directory.record(self.root, 'factura', PORTAL, rfc=ISSUER, merchant='Café Estrella')
        key = self.add('segundo', 'CAFE ESTRELLA\nRFC CES101010AB1\nTOTAL 40.50')
        with patch('facturador.automation.AI.extract', return_value=extracted(
                provider='unknown', merchant='Café Estrella', issuer_rfc=ISSUER, billing_url=None,
                uncertainties=['No aparece una liga de facturación.'])), \
             patch('facturador.automation.AI.discover_portal', side_effect=AssertionError('sin búsqueda')):
            await self.worker.step()
        row = self.store.get(key)
        self.assertEqual((row['status'], row['receipt']['portal_url']), ('ready', PORTAL), row['message'])
        self.assertNotIn('Confirma el dominio', row['message'])
        provenance = self.sources(key)
        self.assertEqual(provenance['fields']['portal_url'], 'directorio_portales')
        self.assertIn('directorio_portales', provenance['tools'])
        self.assertNotIn('busqueda_web', provenance['tools'])
        with patch.object(self.runner, 'run', new_callable=AsyncMock) as run:
            await self.worker.step()
            await asyncio.gather(*self.runner.job_tasks.values())
            run.assert_awaited_once()
        self.assertEqual(self.store.get(key)['status'], 'queued')

    async def test_match_by_name_only_keeps_todays_domain_question(self):
        directory.record(self.root, 'factura', PORTAL, rfc=ISSUER, merchant='Café Estrella')
        key = self.add('nombre', 'CAFE ESTRELLA\nTOTAL 40.50')
        with patch('facturador.automation.AI.extract', return_value=extracted(
                provider='unknown', merchant='Café Estrella', billing_url=None)), \
             patch('facturador.automation.AI.discover_portal', side_effect=AssertionError('sin búsqueda')):
            await self.worker.step()
        row = self.store.get(key)
        self.assertEqual(row['receipt']['portal_url'], PORTAL)
        self.assertEqual(row['status'], 'review')
        self.assertIn('Confirma el dominio', row['message'])
        self.assertFalse(self.runner.has_active_jobs())

    async def test_printed_or_searched_evidence_is_only_a_proposal(self):
        directory.record(self.root, 'impreso', PORTAL, rfc=ISSUER, merchant='Café Estrella')
        key = self.add('impreso', 'CAFE ESTRELLA\nTOTAL 40.50')
        with patch('facturador.automation.AI.extract', return_value=extracted(
                provider='unknown', merchant='Café Estrella', issuer_rfc=ISSUER, billing_url=None)), \
             patch('facturador.automation.AI.discover_portal', side_effect=AssertionError('sin búsqueda')):
            await self.worker.step()
        row = self.store.get(key)
        self.assertEqual((row['status'], row['receipt']['portal_url']), ('review', PORTAL))
        self.assertIn('Confirma el dominio', row['message'])

    async def test_printed_link_wins_over_the_directory_and_notes_the_difference(self):
        directory.record(self.root, 'factura', 'https://facturacion.otro-portal.mx/', rfc=ISSUER)
        key = self.add('liga', 'CAFE ESTRELLA\nFactura en ' + PORTAL)
        with patch('facturador.automation.AI.extract', return_value=extracted(
                provider='unknown', merchant='Café Estrella', issuer_rfc=ISSUER, billing_url=PORTAL)), \
             patch('facturador.automation.AI.discover_portal', side_effect=AssertionError('sin búsqueda')):
            await self.worker.step()
        row = self.store.get(key)
        self.assertEqual((row['status'], row['receipt']['portal_url']), ('ready', PORTAL), row['message'])
        self.assertIn('portal distinto', row['message'])
        self.assertIn('https://facturacion.otro-portal.mx', row['message'])
        # The single printed fiscal link is learned as printed evidence.
        self.assertEqual([alt['url'] for alt in directory.lookup(self.root, rfc=ISSUER)['alternates']], [PORTAL])

    async def test_verified_search_is_saved_and_reused_without_a_new_search(self):
        evidence = 'https://cafeestrella.mx/facturacion'
        first = self.add('primero', 'CAFE ESTRELLA\nTOTAL 40.50')
        with patch('facturador.automation.AI.extract', return_value=extracted(
                provider='unknown', merchant='Café Estrella', billing_url=None)), \
             patch('facturador.automation.AI.discover_portal', return_value=(PORTAL, evidence)) as search, \
             patch('facturador.automation.public_page_links_to', new_callable=AsyncMock, return_value=True):
            await self.worker.step()
        search.assert_called_once()
        self.assertEqual(self.store.get(first)['status'], 'ready')
        entry = directory.lookup(self.root, merchant='Café Estrella')
        self.assertEqual((entry['url'], entry['level'], entry['evidence_url']), (PORTAL, 'busqueda', evidence))
        second = self.add('segundo', 'CAFE ESTRELLA\nTOTAL 40.50')
        with patch('facturador.automation.AI.extract', return_value=extracted(
                provider='unknown', merchant='Café Estrella', ticket_number='PRUEBA-B', billing_url=None)), \
             patch('facturador.automation.AI.discover_portal', side_effect=AssertionError('sin búsqueda')), \
             patch('facturador.automation.public_page_links_to', new_callable=AsyncMock, return_value=True) as linked, \
             patch.object(self.runner, 'run', new_callable=AsyncMock):
            await self.worker.step()  # Prepares the first ticket (the portal run is a double).
            await self.worker.step()  # Reads the second one.
            await asyncio.gather(*self.runner.job_tasks.values())
        linked.assert_awaited_once_with(evidence, PORTAL)
        self.assertEqual(self.store.get(second)['status'], 'ready')
        self.assertEqual(self.store.get(second)['receipt']['portal_url'], PORTAL)
        self.assertNotIn('Confirma el dominio', self.store.get(second)['message'])

    async def test_empty_directory_still_searches_the_web(self):
        key = self.add('vacio', 'CAFE ESTRELLA\nTOTAL 40.50')
        with patch('facturador.automation.AI.extract', return_value=extracted(
                provider='unknown', merchant='Café Estrella', billing_url=None)), \
             patch('facturador.automation.AI.discover_portal', return_value=('', '')) as search:
            await self.worker.step()
        search.assert_called_once_with('Café Estrella')
        self.assertEqual(self.store.get(key)['status'], 'review')
        self.assertIsNone(directory.lookup(self.root, merchant='Café Estrella'))

    async def test_broken_directory_does_not_lose_the_read(self):
        key = self.add('roto', 'CAFE ESTRELLA\nTOTAL 40.50')
        with patch('facturador.portal_directory.lookup', side_effect=sqlite3.DatabaseError('dañada')), \
             patch('facturador.automation.AI.extract', return_value=extracted(
                 provider='unknown', merchant='Café Estrella', issuer_rfc=ISSUER, billing_url=None)), \
             patch('facturador.automation.AI.discover_portal', return_value=('', '')) as search:
            await self.worker.step()
        search.assert_called_once()
        self.assertEqual(self.store.get(key)['receipt']['issuer_rfc'], ISSUER)


if __name__ == '__main__':
    unittest.main()
