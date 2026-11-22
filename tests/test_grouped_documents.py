import tempfile
import unittest
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from xml.etree import ElementTree as ET

from facturador.db import Store
from facturador.documents import reconcile, save_candidate
from test_core import FISCAL_UUID, PROFILE, pdf_bytes
from test_capufe_grouped_cfdi import FIRST, SECOND, grouped_xml, toll


CODES = (FIRST, SECOND)


def toll_xml():
    return ET.tostring(grouped_xml(), encoding='utf-8')


def add_toll(store, name, code):
    key = store.add(name + '.jpg', name)[0]['id']
    ticket = toll([code]).model_copy(update={'merchant':'CAPUFE', 'ticket_number':name})
    store.save_receipt(key, ticket, ready=True)
    return key


class GroupedDocumentsTests(unittest.TestCase):
    def setUp(self):
        self.root = Path(self.enterContext(tempfile.TemporaryDirectory()))
        self.store = Store(self.root)
        self.first = add_toll(self.store, 'primer-ticket', CODES[0])
        self.xml, self.pdf = toll_xml(), pdf_bytes()
        save_candidate(self.store, self.first, 'uno.xml', self.xml)
        save_candidate(self.store, self.first, 'uno.pdf', self.pdf)

    def test_two_tickets_share_originals_and_one_invoice_even_after_restart(self):
        second = add_toll(self.store, 'otro-ticket', CODES[1])
        first_match = reconcile(self.store, self.first, PROFILE)
        self.assertNotIn('invoice_codes', first_match)
        self.assertNotIn(CODES[1], str(self.store.events(self.first)))
        self.assertEqual(self.store.get(second)['status'], 'issued_waiting_files')
        self.assertFalse(self.store.queue(second, PROFILE))
        with ThreadPoolExecutor(2) as pool:
            results = list(pool.map(lambda _: reconcile(Store(self.root), second, PROFILE), range(2)))
        self.assertTrue(all(r['matched_codes'] == [CODES[1]] for r in results))
        rows = Store(self.root).list()
        self.assertEqual({r['uuid'] for r in rows}, {FISCAL_UUID})
        self.assertEqual({(r['status'],r['ticket_total'],r['invoice_total'],r['match_type']) for r in rows},
                         {('verified','50.00','100.00','agrupado')})
        self.assertEqual({r['pdf'] for r in rows}, {str(self.root/'cfdi'/FISCAL_UUID/'factura.pdf')})
        self.assertEqual(Path(rows[0]['xml']).read_bytes(), self.xml)
        self.assertEqual(Path(rows[0]['pdf']).read_bytes(), self.pdf)
        with self.store.connect() as db:
            self.assertEqual(db.execute('SELECT count(uuid) FROM jobs').fetchone()[0], 1)
            self.assertEqual(db.execute('SELECT count(DISTINCT job_id) FROM invoice_items WHERE job_id IS NOT NULL').fetchone()[0], 2)
        self.assertEqual(sum(e['kind']=='verified' for e in self.store.events(second)), 1)

    def test_new_ticket_reuses_archive_only_after_profile_and_amount_checks(self):
        reconcile(self.store, self.first, PROFILE)
        second = add_toll(self.store, 'llega-despues', CODES[1])
        self.assertFalse(self.store.queue(second, PROFILE))
        self.assertFalse(self.store.claim(second))
        with self.assertRaises(ValueError):
            reconcile(self.store, second, PROFILE.model_copy(update={'rfc':'XEXX010101000'}))
        self.assertFalse(self.store.get(second)['pdf'])
        self.assertEqual(reconcile(self.store, second, PROFILE)['matched_concepts'], [1])
        duplicate = add_toll(self.store, 'repetido', CODES[0][:8] + '-' + CODES[0][8:])
        with self.assertRaisesRegex(ValueError, 'partida|duplicado'):
            reconcile(self.store, duplicate, PROFILE)
        self.assertEqual(self.store.get(duplicate)['status'], 'duplicate')
        self.assertFalse(self.store.get(duplicate)['pdf'])

    def test_same_uuid_different_xml_does_not_replace_original(self):
        reconcile(self.store, self.first, PROFILE)
        second = add_toll(self.store, 'mismo-uuid', CODES[1])
        alternate = save_candidate(self.store, second, 'otro.xml', self.xml + b'\n')
        pdf = save_candidate(self.store, second, 'otro.pdf', self.pdf)
        with self.assertRaisesRegex(ValueError, 'otro XML distinto'):
            reconcile(self.store, second, PROFILE, [alternate, pdf])
        self.assertEqual(Path(self.store.get(self.first)['xml']).read_bytes(), self.xml)
        self.assertFalse(self.store.get(second)['pdf'])

    def test_portal_observations_do_not_preempt_the_first_verified_pair(self):
        second = add_toll(self.store, 'otra-observacion', CODES[1])
        self.store.update(second, uuid=FISCAL_UUID)
        self.store.update(self.first, uuid=FISCAL_UUID)
        self.assertEqual(self.store.get(self.first)['uuid'], FISCAL_UUID)
        self.assertFalse(self.store.get(second)['pdf'])
        reconcile(self.store, self.first, PROFILE)
        self.assertEqual(self.store.get(second)['uuid'], FISCAL_UUID)
        reconcile(self.store, second, PROFILE)
        self.assertEqual(self.store.get(second)['status'], 'verified')
        with self.store.connect() as db:
            self.assertEqual(db.execute('SELECT id FROM jobs WHERE uuid=?', (FISCAL_UUID,)).fetchone()['id'], self.first)
