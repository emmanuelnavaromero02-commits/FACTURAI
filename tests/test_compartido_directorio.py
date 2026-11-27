"""What the accounts of one installation know about the portals of the merchants, and how far each trusts the others.

Data folders stand for the accounts. Nothing here reaches the network: no portal, no model, no
search. Every person, ticket and merchant is made up.
"""
import json
import shutil
import sqlite3
import tempfile
import unittest
from pathlib import Path
from unittest.mock import AsyncMock, Mock, patch

from compartido_dobles import (ANA, BETO, CARLA, DANTE, DIRECTORY, FIELDS, FIRST, FOLIO, ISSUER, MEMORY, PORTAL, SCREEN,
                               SEARCH, SECOND, THIRD, Installation, SharedMemoryCase, act, button, rows, tables)
from facturador import compartido, portal_directory as directory
from facturador.automation import TicketAutomation
from facturador.db import Store
from facturador.documents import reconcile, save_candidate
from facturador.models import Receipt
from facturador.portal_memory import Attempt, Known, PortalMemory, describe
from facturador.registro_datos import guardar_datos
from facturador.runner import Runner
from test_core import pdf_bytes, xml_bytes
from test_ticket_automation import extracted


class SharedDirectoryCase(Installation):
    def setUp(self):
        super().setUp()
        self.ana, self.beto, self.carla = self.account(ANA), self.account(BETO), self.account(CARLA)

    def entries(self) -> list[dict]:
        return rows(self.shared / DIRECTORY, 'portals')

    def entry(self, kind='rfc') -> dict:
        return next(row for row in self.entries() if row['key_kind'] == kind)


class SharedDirectoryTests(SharedDirectoryCase):
    def test_one_account_cannot_make_another_open_a_portal_unasked_and_two_can(self):
        self.assertTrue(directory.record(self.ana, 'factura', PORTAL, rfc=ISSUER, merchant='Café Estrella'))
        # Ana got her invoice there: for her it is as for a single person.
        own = directory.lookup(self.ana, rfc=ISSUER)
        self.assertEqual((own['url'], own['level'], own['matched_by']), (PORTAL, 'factura', 'rfc'))
        self.assertTrue(directory.invoice_confirmed(self.ana, ISSUER, PORTAL))
        # For Beto it is what a search would bring: a proposal whose domain he confirms.
        for _ in range(2):
            proposal = directory.lookup(self.beto, rfc=ISSUER, merchant='Café Estrella')
            self.assertEqual((proposal['url'], proposal['level'], proposal['matched_by'], proposal['evidence_url']),
                             (PORTAL, 'busqueda', 'rfc', ''))
            self.assertFalse(directory.invoice_confirmed(self.beto, ISSUER, PORTAL))
            # The same account again, however many invoices: it is still one account.
            directory.record(self.ana, 'factura', PORTAL, rfc=ISSUER, merchant='Café Estrella')
        self.assertEqual((self.entry()['hits'], self.entry()['accounts']), (3, 1))
        # A second account gets its own verified invoice from that portal.
        directory.record(self.carla, 'factura', PORTAL, rfc=ISSUER, merchant='Café Estrella')
        self.assertEqual((self.entry()['hits'], self.entry()['accounts']), (4, 2))
        known = directory.lookup(self.beto, rfc=ISSUER)
        self.assertEqual((known['url'], known['level'], known['matched_by']), (PORTAL, 'factura', 'rfc'))
        self.assertTrue(directory.invoice_confirmed(self.beto, ISSUER, PORTAL))
        # Only that exact portal, and only for that issuer.
        self.assertFalse(directory.invoice_confirmed(self.beto, ISSUER, 'https://facturas.cafe-estrella.com.mx/otra'))
        self.assertFalse(directory.invoice_confirmed(self.beto, 'XYZ010101AB1', PORTAL))
        self.assertEqual(directory.lookup(self.beto, merchant='cafe estrella')['matched_by'], 'nombre')

    def test_weaker_evidence_of_many_accounts_never_opens_a_portal_unasked(self):
        for root in (self.ana, self.carla):
            directory.record(root, 'impreso', PORTAL, rfc=ISSUER)
            directory.record(root, 'busqueda', PORTAL, rfc=ISSUER, evidence_url='https://cafe-estrella.com.mx/facturacion')
        self.assertEqual((self.entry()['level'], self.entry()['accounts']), (2, 2))
        self.assertEqual(directory.lookup(self.beto, rfc=ISSUER)['level'], 'busqueda')
        self.assertFalse(directory.invoice_confirmed(self.beto, ISSUER, PORTAL))
        # Stronger evidence starts its own count: the accounts that only saw it printed are not behind the invoice.
        directory.record(self.ana, 'factura', PORTAL, rfc=ISSUER)
        self.assertEqual((self.entry()['level'], self.entry()['hits'], self.entry()['accounts']), (3, 1, 1))
        self.assertFalse(directory.invoice_confirmed(self.beto, ISSUER, PORTAL))
        directory.record(self.carla, 'impreso', PORTAL, rfc=ISSUER)       # Weaker: it adds nobody.
        self.assertEqual((self.entry()['accounts'], self.entry()['seen']), (1, 6))
        directory.record(self.carla, 'factura', PORTAL, rfc=ISSUER)
        self.assertTrue(directory.invoice_confirmed(self.beto, ISSUER, PORTAL))

    def test_a_search_another_account_made_is_never_checked_again_on_its_word(self):
        evidence = 'https://cafeestrella.mx/facturacion'
        directory.record(self.ana, 'busqueda', PORTAL, merchant='Café Estrella', evidence_url=evidence)
        self.assertEqual(directory.lookup(self.ana, merchant='Café Estrella')['evidence_url'], evidence)
        proposal = directory.lookup(self.beto, merchant='Café Estrella')
        self.assertEqual((proposal['url'], proposal['level'], proposal['evidence_url']), (PORTAL, 'busqueda', ''))
        self.assert_absent(evidence, 'cafeestrella.mx')

    def test_an_account_answers_first_with_what_it_knows_itself(self):
        other = 'https://facturacion.otro-portal.mx/'
        for root in (self.ana, self.carla):
            directory.record(root, 'factura', other, rfc=ISSUER)
        directory.record(self.beto, 'impreso', PORTAL, rfc=ISSUER)
        entry = directory.lookup(self.beto, rfc=ISSUER)
        self.assertEqual((entry['url'], entry['level'], entry['alternates']), (PORTAL, 'impreso', []))
        # Among what only the others know, a corroborated invoice goes before anything else.
        directory.record(self.ana, 'factura', PORTAL, rfc=ISSUER)
        dante = self.account(DANTE)
        entry = directory.lookup(dante, rfc=ISSUER)
        self.assertEqual((entry['url'], entry['level']), (other, 'factura'))
        self.assertEqual([(alt['url'], alt['level']) for alt in entry['alternates']], [(PORTAL, 'busqueda')])

    def test_shared_directory_holds_nothing_of_the_person_nor_of_the_ticket(self):
        guardar_datos(self.ana, {'nombres': 'Ana', 'apellido_paterno': 'Ficticia', 'calle': 'Avenida Imaginaria'})
        folio, token = 'PRUEBA-A7731', '9f8a7b6c5d4e'
        private = [ANA.rfc, folio, '1137.45', 'WEB-55/7001']
        url = PORTAL + '?rfc=' + ANA.rfc + '&correo=' + ANA.email + '&folio=' + folio
        directory.record(self.ana, 'factura', url, rfc=ISSUER, merchant='Café Estrella', private=private)
        # A link that names one ticket, by a code the reading knows or by one it does not.
        directory.record(self.ana, 'impreso', 'https://tickets.example.mx/r/' + folio, merchant='Otra Tienda', private=private)
        directory.record(self.ana, 'impreso', 'https://codigos.example.mx/t/' + token, merchant='Tienda de Codigos',
                         private=private)
        # A reading that took the buyer for the merchant, in whole or in part.
        directory.record(self.ana, 'impreso', 'https://uno.example.mx/factura', rfc=ANA.rfc, merchant=ANA.name)
        directory.record(self.ana, 'impreso', 'https://dos.example.mx/factura', merchant='Cliente Ficticia')
        directory.record(self.ana, 'impreso', 'https://tres.example.mx/factura', merchant='Abarrotes Avenida')
        directory.record(self.ana, 'impreso', 'https://cuatro.example.mx/factura', merchant='Tienda ' + folio)
        directory.record(self.ana, 'impreso', 'https://cinco.example.mx/factura', merchant='Tienda 5500000001')
        shared = {(row['key_kind'], row['key']): row['url'] for row in self.entries()}
        self.assertEqual(shared, {('rfc', ISSUER): PORTAL, ('nombre', 'cafe estrella'): PORTAL,
                                  ('nombre', 'otra tienda'): 'https://tickets.example.mx/',
                                  ('nombre', 'tienda de codigos'): 'https://codigos.example.mx/'})
        # Her own directory keeps what a single person's would: the link with the code it could not tell apart.
        self.assertEqual(directory.lookup(self.ana, merchant='Tienda de Codigos')['url'], 'https://codigos.example.mx/t/' + token)
        self.assertEqual(directory.lookup(self.beto, merchant='Tienda de Codigos')['url'], 'https://codigos.example.mx/')
        self.assert_absent(ANA.rfc, ANA.email, ANA.name, 'FICTICIA', 'PRIMERA', ANA.postal_code, ANA.whatsapp_phone,
                           '5500000001', folio, 'A7731', token, '1137.45', 'WEB-55/7001', 'Imaginaria', 'Avenida',
                           self.ana.name)
        # The shared file has no page of evidence and no note of who was counted: those stay with each account.
        self.assertEqual(tables(self.shared / DIRECTORY), {'portals', 'store'})
        self.assertEqual(set(self.entries()[0]),
                         {'key_kind', 'key', 'url', 'level', 'hits', 'seen', 'first_seen', 'last_seen', 'accounts'})
        self.assertEqual(tables(self.ana / DIRECTORY), {'portals', 'shared_marks'})

    def test_nothing_is_shared_while_what_is_personal_cannot_be_told(self):
        (self.ana / 'profile.json').write_text('{esto no es json', encoding='utf-8')
        self.assertTrue(directory.record(self.ana, 'factura', PORTAL, rfc=ISSUER, merchant='Café Estrella'))
        self.assertEqual(directory.lookup(self.ana, rfc=ISSUER)['level'], 'factura')
        self.assertIsNone(directory.lookup(self.beto, rfc=ISSUER, merchant='Café Estrella'))

    def test_a_shared_directory_that_fails_never_costs_the_account_what_it_learned(self):
        with patch('facturador.portal_directory._publish', side_effect=sqlite3.OperationalError('ocupada')), \
                self.assertLogs('facturador.portal_directory', 'WARNING'):
            self.assertTrue(directory.record(self.ana, 'factura', PORTAL, rfc=ISSUER))
        self.assertTrue(directory.invoice_confirmed(self.ana, ISSUER, PORTAL))
        (self.shared / DIRECTORY).write_bytes(b'esto no es una base de datos')
        with self.assertLogs('facturador.portal_directory', 'WARNING'):
            self.assertTrue(directory.record(self.ana, 'factura', PORTAL, rfc=ISSUER))
        self.assertEqual(directory.lookup(self.ana, rfc=ISSUER)['hits'], 2)
        with self.assertLogs('facturador.portal_directory', 'WARNING'):
            self.assertIsNone(directory.lookup_safely(self.beto, rfc=ISSUER))
            self.assertFalse(directory.invoice_confirmed(self.beto, ISSUER, PORTAL))

    def test_a_shared_directory_that_starts_again_counts_every_account_again(self):
        for root in (self.ana, self.carla):
            directory.record(root, 'factura', PORTAL, rfc=ISSUER)
        self.assertTrue(directory.invoice_confirmed(self.beto, ISSUER, PORTAL))
        shutil.rmtree(self.shared)      # Emptied by hand, folder and all.
        self.assertFalse(directory.invoice_confirmed(self.beto, ISSUER, PORTAL))
        self.assertIsNone(directory.lookup(self.beto, rfc=ISSUER))
        directory.record(self.ana, 'factura', PORTAL, rfc=ISSUER)
        self.assertEqual(self.entry()['accounts'], 1)
        directory.record(self.carla, 'factura', PORTAL, rfc=ISSUER)
        self.assertEqual(self.entry()['accounts'], 2)
        self.assertTrue(directory.invoice_confirmed(self.beto, ISSUER, PORTAL))

    def test_an_account_whose_run_dies_between_its_note_and_the_count_is_never_counted_twice(self):
        directory.record(self.ana, 'factura', PORTAL, rfc=ISSUER)
        noted = directory._note

        def dies(*values):
            noted(*values)
            raise sqlite3.OperationalError('el proceso murió aquí')

        with patch('facturador.portal_directory._note', dies), self.assertLogs('facturador.portal_directory', 'WARNING'):
            self.assertTrue(directory.record(self.carla, 'factura', PORTAL, rfc=ISSUER))
        self.assertEqual((self.entry()['hits'], self.entry()['accounts']), (1, 1))      # Nothing was counted.
        self.assertTrue(directory.invoice_confirmed(self.carla, ISSUER, PORTAL))        # Her own directory did learn.
        # Her note says she was already counted: she is counted one time less, never one time more.
        directory.record(self.carla, 'factura', PORTAL, rfc=ISSUER)
        self.assertEqual((self.entry()['hits'], self.entry()['accounts']), (2, 1))
        self.assertFalse(directory.invoice_confirmed(self.beto, ISSUER, PORTAL))
        directory.record(self.account(DANTE), 'factura', PORTAL, rfc=ISSUER)
        self.assertEqual((self.entry()['hits'], self.entry()['accounts']), (3, 2))

    def test_a_verified_invoice_of_each_account_is_what_counts_it(self):
        """Through the real match of a CFDI with its ticket, as the engine does when a ticket ends."""
        receipt = Receipt(provider='portal_ticket', portal_url=PORTAL, merchant='Café Estrella', amount='100.00',
                          date='2026-11-13', ticket_number='PRUEBA-A7731', payment_code='04')
        for root, profile in ((self.ana, ANA), (self.carla, CARLA)):
            self.assertFalse(directory.invoice_confirmed(self.beto, ISSUER, PORTAL))
            store = Store(root)
            key = store.add('original', 'uno')[0]['id']
            store.save_receipt(key, receipt, ready=True)
            receiver = {'Rfc': profile.rfc, 'Nombre': profile.name, 'DomicilioFiscalReceptor': profile.postal_code}
            save_candidate(store, key, 'f.xml', xml_bytes(issuer=ISSUER, **receiver))
            save_candidate(store, key, 'f.pdf', pdf_bytes(rfc=profile.rfc))
            reconcile(store, key, profile)
            self.assertEqual(store.get(key)['status'], 'verified')
        self.assertEqual((self.entry()['level'], self.entry()['accounts']), (3, 2))
        self.assertTrue(directory.invoice_confirmed(self.beto, ISSUER, PORTAL))
        self.assert_absent(ANA.rfc, CARLA.rfc, ANA.name, CARLA.name, 'PRUEBA-A7731', self.ana.name, self.carla.name)


class SharedDirectoryReadTests(unittest.IsolatedAsyncioTestCase):
    """Beto's own queue reads his tickets; what it finds in the directory came from the other accounts."""

    def setUp(self):
        folder = tempfile.TemporaryDirectory()
        self.addCleanup(folder.cleanup)
        base = Path(folder.name)
        compartido.enable(base / 'usuarios', base / 'compartido')
        self.addCleanup(compartido.reset)
        self.ana, self.carla, self.root = (base / 'usuarios' / (letter * 16) for letter in 'acb')
        for root in (self.ana, self.carla, self.root):
            root.mkdir(parents=True)
        self.store = Store(self.root)
        self.plan = Mock()
        self.plan.status.return_value = {'status': 'ready'}
        self.runner = Runner(self.store, self.plan)
        self.worker = TicketAutomation(self.store, self.runner, self.plan, lambda: BETO)
        self.worker.configure(True)

    def add(self, key, text):
        path = self.root / (key + '.txt')
        path.write_text(text, encoding='utf-8')
        return self.store.add(str(path), key, 'archivo: ticket.txt')[0]['id']

    async def read(self, key, **patches):
        with patch('facturador.automation.AI.extract', return_value=extracted(
                provider='unknown', merchant='Café Estrella', issuer_rfc=ISSUER, billing_url=None, ticket_number=key.upper())), \
             patch('facturador.automation.AI.discover_portal', side_effect=AssertionError('sin búsqueda')), \
             patch('facturador.automation.public_page_links_to', new_callable=AsyncMock, return_value=True) as linked:
            await self.worker.step()
        return self.store.get(key), linked

    async def test_portal_only_one_other_account_invoiced_in_waits_for_the_person_to_confirm_the_domain(self):
        directory.record(self.ana, 'factura', PORTAL, rfc=ISSUER, merchant='Café Estrella')
        row, _ = await self.read(self.add('primero', 'CAFE ESTRELLA\nRFC CES101010AB1\nTOTAL 40.50'))
        self.assertEqual((row['status'], row['receipt']['portal_url']), ('review', PORTAL), row['message'])
        self.assertIn('Confirma el dominio del portal antes de compartir tus datos fiscales: '
                      'https://facturas.cafe-estrella.com.mx', row['message'])
        self.assertFalse(self.runner.has_active_jobs())
        # It is a proposal, like a web search nobody checked: for the direct channels the ticket has no portal yet.
        events = [json.loads(event['detail']) for event in self.store.events(row['id']) if event['kind'] == 'fuentes_de_campos']
        self.assertEqual(events[0]['fields']['portal_url'], 'directorio_compartido')
        self.assertTrue(self.worker._without_portal(row))
        # A second account got its invoice there: Beto's next ticket needs no question, as with his own invoices.
        directory.record(self.carla, 'factura', PORTAL, rfc=ISSUER, merchant='Café Estrella')
        row, _ = await self.read(self.add('segundo', 'CAFE ESTRELLA\nRFC CES101010AB1\nTOTAL 40.50'))
        self.assertEqual((row['status'], row['receipt']['portal_url']), ('ready', PORTAL), row['message'])
        self.assertNotIn('Confirma el dominio', row['message'])
        events = [json.loads(event['detail']) for event in self.store.events(row['id']) if event['kind'] == 'fuentes_de_campos']
        self.assertEqual(events[0]['fields']['portal_url'], 'directorio_portales')
        self.assertFalse(self.worker._without_portal(row))

    async def test_search_of_another_account_is_a_proposal_even_when_its_page_would_check_out(self):
        directory.record(self.ana, 'busqueda', PORTAL, rfc=ISSUER, merchant='Café Estrella',
                         evidence_url='https://cafeestrella.mx/facturacion')
        row, linked = await self.read(self.add('primero', 'CAFE ESTRELLA\nTOTAL 40.50'))
        self.assertEqual((row['status'], row['receipt']['portal_url']), ('review', PORTAL), row['message'])
        self.assertIn('Confirma el dominio', row['message'])
        linked.assert_not_awaited()


class StartTests(SharedMemoryCase):
    def test_shared_store_starts_empty_and_what_an_account_already_knew_is_not_mixed_in(self):
        # Carla used the program as a single person before the installation took accounts in.
        carla = self.users / ('c' * 16)
        carla.mkdir(parents=True)
        compartido.reset()
        self.assertIsNone(PortalMemory(carla).commons)
        self.solved(carla, CARLA, THIRD).commit()
        directory.record(carla, 'factura', PORTAL, rfc=ISSUER, merchant='Café Estrella')
        before = {path.name: path.read_bytes() for path in carla.iterdir() if path.suffix == '.sqlite'}
        compartido.enable(self.users, self.shared)
        # Nothing of hers moved by itself: the others start from zero and she keeps what she knew.
        self.assertEqual(self.recalled(self.beto, BETO, SECOND, family=True), (None, True))
        self.assertIsNone(directory.lookup(self.beto, rfc=ISSUER, merchant='Café Estrella'))
        self.assertEqual(self.recalled(carla, CARLA, THIRD)[0], [FOLIO, SEARCH])
        self.assertEqual(directory.lookup(carla, rfc=ISSUER)['level'], 'factura')
        self.assertTrue(directory.invoice_confirmed(carla, ISSUER, PORTAL))
        self.assertIsNone(self.row())
        self.assertFalse((self.shared / DIRECTORY).exists())
        self.assertEqual(before[DIRECTORY], (carla / DIRECTORY).read_bytes())
        # Her next verified invoice there is something she learns now: that one does serve the others.
        self.repeated(carla, CARLA, THIRD).commit()
        directory.record(carla, 'factura', PORTAL, rfc=ISSUER, merchant='Café Estrella')
        self.assertEqual(self.recalled(self.beto, BETO, SECOND)[0], [FOLIO, SEARCH])
        self.assertEqual(directory.lookup(self.beto, rfc=ISSUER)['url'], PORTAL)


class LocalModeTests(unittest.TestCase):
    """A single person: both files stay in the data folder, as they were, and nothing else appears."""

    def setUp(self):
        self.base = Path(self.enterContext(tempfile.TemporaryDirectory()))
        self.addCleanup(compartido.reset)
        # An installation with accounts somewhere else in the same process changes nothing here.
        compartido.enable(self.base / 'otra' / 'usuarios', self.base / 'otra' / 'compartido')
        self.root = self.base / 'datos'
        self.root.mkdir()

    def test_memory_and_directory_stay_where_they_were(self):
        attempt = Attempt(PortalMemory(self.root), Known(ANA, FIRST))
        attempt.step(SCREEN, act('fill', 'Folio del ticket', FIRST.ticket_number), describe(FIELDS, 1), 'normal', 'ia')
        attempt.step(SCREEN, button('Buscar ticket'), None, 'normal', 'ia')
        self.assertEqual(attempt.commit(), 1)
        self.assertTrue(directory.record(self.root, 'busqueda', PORTAL, rfc=ISSUER,
                                         evidence_url='https://cafeestrella.mx/facturacion'))
        self.assertTrue(directory.record(self.root, 'factura', PORTAL, rfc=ISSUER, merchant='Café Estrella'))
        entry = directory.lookup(self.root, rfc=ISSUER)
        self.assertEqual(entry, {'url': PORTAL, 'level': 'factura', 'matched_by': 'rfc', 'hits': 1,
                                 'last_seen': entry['last_seen'], 'evidence_url': 'https://cafeestrella.mx/facturacion',
                                 'alternates': []})
        self.assertTrue(directory.invoice_confirmed(self.root, ISSUER, PORTAL))
        self.assertIsNone(directory.lookup(self.root, rfc='XYZ010101AB1', merchant='Otra Tienda'))
        self.assertEqual({path.name.split('-')[0] for path in self.root.iterdir()}, {MEMORY, DIRECTORY})
        self.assertEqual({path.name for path in self.base.iterdir()}, {'datos', 'otra'})
        self.assertEqual(list((self.base / 'otra' / 'compartido').iterdir()), [])
        # The files are the ones of always: no new table and no new column.
        self.assertEqual((tables(self.root / MEMORY), tables(self.root / DIRECTORY)), ({'screens', 'pending'}, {'portals'}))
        self.assertEqual(set(rows(self.root / DIRECTORY, 'portals')[0]),
                         {'key_kind', 'key', 'url', 'level', 'hits', 'seen', 'first_seen', 'last_seen', 'evidence_url'})


class ConcurrencyTests(Installation):
    """Several accounts write from their own threads at the same time: nothing is lost or broken."""
    accounts, rounds = 6, 12

    def test_accounts_recording_portals_while_another_one_looks_them_up(self):
        roots = [self.account() for _ in range(self.accounts)]
        reader = self.account()

        def work(number):
            if number == self.accounts:      # One more account only reads, all the while.
                for _ in range(self.accounts * self.rounds):
                    entry = directory.lookup(reader, rfc=ISSUER)
                    if entry is not None and entry['url'] != PORTAL:
                        raise AssertionError('lectura incompleta')
                    directory.invoice_confirmed(reader, ISSUER, PORTAL)
                return
            for turn in range(self.rounds):
                if not directory.record(roots[number], 'factura', PORTAL, rfc=ISSUER, merchant='Café Estrella'):
                    raise AssertionError('no se guardó')
                directory.record(roots[number], 'impreso', f'https://portal-{chr(ord("a") + number)}.example.mx/factura',
                                 rfc=ISSUER)

        with self.assertNoLogs('facturador.portal_directory', 'WARNING'):
            self.assertEqual(self.run_together(work, self.accounts + 1), [])
        shared = {(row['key_kind'], row['url']): row for row in rows(self.shared / DIRECTORY, 'portals')}
        self.assertEqual(len(shared), self.accounts + 2)
        for kind in ('rfc', 'nombre'):
            row = shared[kind, PORTAL]
            self.assertEqual((row['level'], row['hits'], row['seen'], row['accounts']),
                             (3, self.accounts * self.rounds, self.accounts * self.rounds, self.accounts))
        for number, root in enumerate(roots):
            row = shared['rfc', f'https://portal-{chr(ord("a") + number)}.example.mx/factura']
            self.assertEqual((row['level'], row['hits'], row['accounts']), (2, self.rounds, 1))
            self.assertEqual(directory.lookup(root, rfc=ISSUER)['hits'], self.rounds)
        self.assertTrue(directory.invoice_confirmed(reader, ISSUER, PORTAL))
        self.healthy(DIRECTORY)


if __name__ == '__main__':
    unittest.main()
