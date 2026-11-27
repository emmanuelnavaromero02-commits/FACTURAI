import datetime
import tempfile
import unittest
from email.message import EmailMessage
from pathlib import Path
from unittest.mock import patch

from correo_dobles import FakeImap, trusted_context
from facturador import buzon
from facturador.db import Store, now as stamp
from facturador.documents import save_candidate
from facturador.invoice_mail import AMBIGUOUS, FINISHED, InvoiceMailRecovery, search_job_now
from test_core import PROFILE, RECEIPT, FISCAL_UUID, pdf_bytes, xml_bytes

OTHER_UUID = '87654321-4321-4321-8321-BA0987654321'
PASSWORD = 'clave-app-ficticia'
UTC = datetime.timezone.utc


def invoice(*attachments, subject='Tu factura electrónica', sender='facturas@comercio.example.com'):
    msg = EmailMessage()
    msg['From'], msg['To'], msg['Subject'] = sender, PROFILE.email, subject
    msg.set_content('Adjuntamos su comprobante fiscal.')
    for name, data in attachments:
        kind = 'pdf' if name.endswith('.pdf') else 'xml' if name.endswith('.xml') else 'zip'
        msg.add_attachment(data, maintype='application', subtype=kind, filename=name)
    return msg


def day(number, hour=12):
    return datetime.datetime(2026, 11, number, hour, tzinfo=UTC)


class MemoryMailbox(buzon.Mailbox):
    """Provider-neutral double that records how the recovery queries the mailbox."""
    provider = 'memoria'

    def __init__(self, messages):
        self.messages = messages
        self.searches = []
        self.sessions = 0

    def open(self):
        self.sessions += 1

    def search(self, **criteria):
        self.searches.append(criteria)
        found = []
        for index, msg in enumerate(self.messages):
            if criteria.get('text') and criteria['text'] not in str(msg['Subject']):
                continue
            found.append({'ref': f'memoria:{index}', 'sender': msg['From'], 'to': msg['To'], 'subject': msg['Subject'],
                          'date': '', 'received': day(16).timestamp()})
        return found

    def read(self, ref):
        return self.messages[int(ref.split(':')[1])]


class InvoiceMailTests(unittest.TestCase):
    def setUp(self):
        folder = tempfile.TemporaryDirectory()
        self.addCleanup(folder.cleanup)
        self.store = Store(Path(folder.name))
        self.key = self.issued('invoice-mail')
        self.imap = FakeImap()
        self.addCleanup(self.imap.close)
        trust = patch('facturador.buzon.tls_context', trusted_context)
        trust.start()
        self.addCleanup(trust.stop)
        self.worker = self.recovery()

    def issued(self, source, receipt=RECEIPT):
        row, _ = self.store.add('fixture', source)
        self.store.save_receipt(row['id'], receipt, ready=True)
        self.store.queue(row['id'], PROFILE)
        self.store.confirm_issued(row['id'])
        return row['id']

    def recovery(self, active=None, boxes=None):
        boxes = boxes or (lambda: [buzon.ImapMailbox(self.imap.account(), PASSWORD)])
        return InvoiceMailRecovery(self.store, lambda: True, lambda: active, boxes)

    def check(self, key):
        with self.store.connect() as db:
            return dict(db.execute('SELECT * FROM invoice_mail_checks WHERE job_id=?', (key,)).fetchone())

    def kept_files(self, key):
        folder = self.store.root / 'downloads' / key
        return [p for p in folder.rglob('*') if p.is_file()] if folder.exists() else []

    def test_exact_uuid_is_searched_first_and_pairs_previously_saved_xml_with_email_pdf(self):
        self.store.update(self.key, uuid=FISCAL_UUID)
        save_candidate(self.store, self.key, 'previo.xml', xml_bytes())
        box = MemoryMailbox([invoice(('recibido.pdf', pdf_bytes()), subject='CFDI ' + FISCAL_UUID)])
        worker = self.recovery(boxes=lambda: [box])
        worker.round(1000)
        self.assertEqual(self.store.get(self.key)['status'], 'verified')
        self.assertEqual(box.searches[0], {'text': FISCAL_UUID, 'attachments': True, 'limit': 20})
        self.assertEqual(box.sessions, 1)
        self.assertEqual(worker.due(100000), [])

    def test_invoice_without_uuid_in_the_mail_is_found_and_reconciled_over_imap(self):
        self.imap.add(invoice(('anterior.xml', xml_bytes(uuid=OTHER_UUID)), ('anterior.pdf', pdf_bytes(OTHER_UUID))), day(10))
        self.imap.add(invoice(('ajena.xml', xml_bytes(Rfc='AAA010101AAA', uuid=OTHER_UUID))), day(15))
        self.imap.add(invoice(('Factura_123.xml', xml_bytes()), ('Factura_123.pdf', pdf_bytes())), day(16))
        self.assertIsNone(self.store.get(self.key)['uuid'])
        outcome = self.worker.round(1000)
        self.assertEqual(outcome, {self.key: 'verified'})
        job = self.store.get(self.key)
        self.assertEqual((job['status'], job['uuid']), ('verified', FISCAL_UUID))
        self.assertEqual(self.imap.logins, 1)
        self.assertEqual(self.imap.selected, ['EXAMINE'])
        self.assertEqual(self.check(self.key)['state'], 'done')
        # The message from before the ticket date was never downloaded.
        self.assertNotIn(101, self.imap.downloads)

    def test_two_matching_invoices_are_left_for_the_person(self):
        self.imap.add(invoice(('a.xml', xml_bytes()), ('a.pdf', pdf_bytes())), day(16))
        self.imap.add(invoice(('b.xml', xml_bytes(uuid=OTHER_UUID)), ('b.pdf', pdf_bytes(OTHER_UUID))), day(17))
        self.assertEqual(self.worker.round(1000), {self.key: 'ambiguous'})
        job = self.store.get(self.key)
        self.assertEqual((job['status'], job['uuid']), ('issued_waiting_files', None))
        self.assertEqual(self.kept_files(self.key), [])
        self.assertEqual(self.check(self.key)['state'], 'ambiguous')
        self.assertIn(AMBIGUOUS, [e['detail'] for e in self.store.events(self.key)])
        self.assertEqual(self.worker.due(10_000), [])

    def test_one_invoice_matching_two_waiting_tickets_is_not_assigned_and_the_mailbox_opens_once(self):
        second = self.issued('invoice-mail-2', RECEIPT.model_copy(update={'billing_codes': ['COD-O0-I2']}))
        self.imap.add(invoice(('a.xml', xml_bytes()), ('a.pdf', pdf_bytes())), day(16))
        self.assertEqual(self.worker.round(1000), {self.key: 'ambiguous', second: 'ambiguous'})
        self.assertEqual(self.imap.logins, 1)
        for key in (self.key, second):
            self.assertEqual(self.store.get(key)['status'], 'issued_waiting_files')
            self.assertEqual(self.kept_files(key), [])

    def test_known_uuid_is_matched_even_when_imap_cannot_search_inside_attachments(self):
        self.store.update(self.key, uuid=FISCAL_UUID)
        self.imap.add(invoice(('b.xml', xml_bytes(uuid=OTHER_UUID)), ('b.pdf', pdf_bytes(OTHER_UUID))), day(16))
        self.imap.add(invoice(('a.xml', xml_bytes()), ('a.pdf', pdf_bytes())), day(17))
        self.assertEqual(self.worker.round(1000), {self.key: 'verified'})
        self.assertEqual(self.store.get(self.key)['uuid'], FISCAL_UUID)

    def test_reply_to_an_email_request_is_collected(self):
        row, _ = self.store.add('fixture', 'pedido-por-correo')
        key = row['id']
        self.store.save_receipt(key, RECEIPT.model_copy(update={'billing_codes': ['COD-O0-I9']}), ready=True)
        with self.store.connect() as db:
            db.execute("UPDATE jobs SET status='external',issue_attempted=1,billing_profile=? WHERE id=?",
                       (PROFILE.model_dump_json(), key))
            db.execute("INSERT INTO mail_requests(job_id,address,reference,channel,state,detail,created,updated) "
                       "VALUES(?,?,?,?,?,?,?,?)", (key, 'ventas@comercio.example.com', 'FACTURAI-00AA', 'smtp', 'sent',
                                                    'Solicitud enviada.', stamp(), stamp()))
            db.execute("UPDATE jobs SET status='verified' WHERE id=?", (self.key,))
        self.imap.add(invoice(('respuesta.zip', zipped(xml_bytes(), pdf_bytes()))), day(16))
        self.assertEqual(self.worker.round(1000), {key: 'verified'})

    def test_messages_that_cannot_match_are_not_downloaded_again(self):
        self.imap.add(invoice(('ajena.xml', xml_bytes(Rfc='AAA010101AAA', uuid=OTHER_UUID))), day(16))
        self.worker.round(1000)
        self.assertEqual(self.imap.downloads, [101])
        self.worker.round(1000 + 120)
        self.assertEqual(self.imap.downloads, [101])

    def test_calendar_excludes_active_job_and_survives_restarts(self):
        self.assertEqual(self.recovery(active=self.key).due(5000), [])
        start, current, claims, turn = 5000, 5000, [], 0
        worker = self.recovery()
        while current < start + 80 * 3600:
            turn += 1
            if turn % 23 == 0:
                worker = self.recovery()  # A fresh instance stands for a restart between two searches.
            rows = worker.due(current)
            if rows:
                self.assertEqual([r['id'] for r in rows], [self.key])
                claims.append(current - start)
            current += 120  # Every delay of the calendar is a multiple of two minutes.
        gaps = [b - a for a, b in zip(claims, claims[1:])]
        self.assertEqual(claims[0], 0)
        self.assertTrue(all(g == 120 for a, g in zip(claims, gaps) if a < 1800))
        self.assertTrue(all(g == 600 for a, g in zip(claims, gaps) if 1800 <= a < 6 * 3600))
        self.assertTrue(all(g == 3600 for a, g in zip(claims, gaps) if 6 * 3600 <= a < 72 * 3600))
        self.assertEqual(len(claims), 15 + 33 + 66)
        self.assertLess(claims[-1], 72 * 3600)
        self.assertEqual(self.check(self.key)['state'], 'done')
        self.assertEqual([e['detail'] for e in self.store.events(self.key) if e['kind'] == 'gmail_recovery'], [FINISHED])
        self.assertFalse(self.store.get(self.key)['issue_attempted'])

    def test_rows_finished_under_the_former_calendar_stay_finished(self):
        with self.store.connect() as db:
            db.execute('DROP TABLE invoice_mail_checks')
            db.execute('CREATE TABLE invoice_mail_checks (job_id TEXT PRIMARY KEY, uuid TEXT NOT NULL, '
                       'attempts INTEGER NOT NULL, next_check INTEGER NOT NULL)')
            db.execute('INSERT INTO invoice_mail_checks VALUES(?,?,5,0)', (self.key, FISCAL_UUID))
        self.assertEqual(self.recovery().due(10**9), [])

    def test_search_now_for_one_ticket(self):
        self.imap.add(invoice(('a.xml', xml_bytes()), ('a.pdf', pdf_bytes())), day(16))
        result = search_job_now(self.store, self.key, lambda: [buzon.ImapMailbox(self.imap.account(), PASSWORD)])
        self.assertEqual(result['status'], 'verified')


def zipped(xml, pdf):
    import io
    import zipfile
    stream = io.BytesIO()
    with zipfile.ZipFile(stream, 'w') as archive:
        archive.writestr('factura.xml', xml)
        archive.writestr('factura.pdf', pdf)
    return stream.getvalue()


if __name__ == '__main__':
    unittest.main()
