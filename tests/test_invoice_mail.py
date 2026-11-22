import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from facturador.db import Store
from facturador.documents import save_candidate
from facturador.invoice_mail import InvoiceMailRecovery
from test_core import PROFILE, RECEIPT, FISCAL_UUID, pdf_bytes, xml_bytes


class InvoiceMailTests(unittest.TestCase):
    def setUp(self):
        folder = tempfile.TemporaryDirectory()
        self.addCleanup(folder.cleanup)
        self.store = Store(Path(folder.name))
        row, _ = self.store.add('fixture', 'invoice-mail')
        self.key = row['id']
        self.store.save_receipt(self.key, RECEIPT, ready=True)
        self.store.queue(self.key, PROFILE)
        self.store.confirm_issued(self.key)
        self.worker = InvoiceMailRecovery(self.store, lambda: True, lambda: None)

    def test_exact_uuid_pairs_previously_saved_xml_with_email_pdf(self):
        self.assertIsNone(self.worker.claim(1000))
        self.store.update(self.key, uuid=FISCAL_UUID)
        save_candidate(self.store, self.key, 'previo.xml', xml_bytes())
        def received(root, key, query):
            self.assertEqual(query, f'has:attachment "{FISCAL_UUID}"')
            save_candidate(self.store, key, 'recibido.pdf', pdf_bytes())
        claimed = self.worker.claim(1000)
        with patch('facturador.invoice_mail.recover_attachments', side_effect=received):
            self.worker.recover(claimed)
        self.assertEqual(self.store.get(self.key)['status'], 'verified')
        self.assertIsNone(self.worker.claim(100000))

    def test_active_job_excluded_backoff_and_limit_survive_restart(self):
        self.store.update(self.key, uuid=FISCAL_UUID)
        busy = InvoiceMailRecovery(self.store, lambda: True, lambda: self.key)
        self.assertIsNone(busy.claim(5000))
        now = 5000
        for attempt in range(1 + len(self.worker.delays)):
            self.assertEqual(self.worker.claim(now)['attempts'], attempt)
            self.assertIsNone(InvoiceMailRecovery(self.store, lambda: True, lambda: None).claim(now + 1))
            now += 86400
        self.assertIsNone(self.worker.claim(now))
        self.assertFalse(self.store.get(self.key)['issue_attempted'])
