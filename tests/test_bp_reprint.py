"""Bounded, read-only BP invoice recovery from synthetic official mail."""

import io
import tempfile
import unittest
import zipfile
from decimal import Decimal
from email.message import EmailMessage
from pathlib import Path
from unittest.mock import patch

from facturador.bp_reprint import recover_bp_reprint
from facturador.bp_ticket import BPEvidence
from facturador.models import Receipt
from test_bp_cfdi import bp_xml
from test_core import FISCAL_UUID, PROFILE, pdf_bytes


RECEIPT = Receipt(provider='bp', merchant='BP PLAYAS', amount='100.00',
                  date='2026-11-21', ticket_number='1234567', payment_code='04',
                  details='Estación 11739, consumo de gasolina.')
EVIDENCE = BPEvidence('11739', '1234567', '21/11/2026', '12:30', Decimal('100.00'), '04')


def invoice_mail(ticket='1234567', uuid=FISCAL_UUID):
    xml = bp_xml('PL/20876/EXP/ES/2018-' + ticket).replace(FISCAL_UUID.encode(), uuid.encode())
    pdf = pdf_bytes(uuid_value=uuid)
    with io.BytesIO() as stream:
        with zipfile.ZipFile(stream, 'w') as archive:
            archive.writestr('factura.xml', xml)
            archive.writestr('factura.pdf', pdf)
        zipped = stream.getvalue()
    msg = EmailMessage()
    msg['From'] = 'Facturación BP <facturacion@grupocentra.mx>'
    msg['To'] = PROFILE.email
    msg['Authentication-Results'] = ('mx.google.com; dkim=pass header.i=@grupocentra.mx; '
                                     'spf=pass smtp.mailfrom=facturacion@grupocentra.mx')
    msg['Subject'] = 'Factura BP'
    msg.set_content('Factura solicitada')
    msg.add_attachment(zipped, maintype='application', subtype='zip', filename='factura.zip')
    return msg


class FakeStore:
    def __init__(self, root):
        self.root = root
        self.events = []

    def get(self, key):
        return {'uuid': ''}

    def event(self, key, kind, message):
        self.events.append((kind, message))


class FakeRunner:
    def __init__(self, root):
        self.store = FakeStore(root)

    async def recover(self, key, profile):
        directory = self.store.root / 'downloads' / key / 'recibidos'
        return directory.is_dir() and {p.suffix for p in directory.iterdir()} == {'.xml', '.pdf'}


class BpReprintTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.runner = FakeRunner(Path(self.temp.name))
        self.page = type('Page', (), {'url': 'https://facturacion.grupocentra.mx/Karmi_FacturacionWeb/PAGE_Facturar_Gasolina_CFDI33/abc'})()

    async def test_unique_exact_ticket_from_official_mail_is_saved(self):
        with (patch('facturador.bp_reprint.GmailConnection.status', return_value='connected'),
              patch('facturador.bp_reprint.search_tickets', return_value=[{'id': 'a'}, {'id': 'b'}]),
              patch('facturador.bp_reprint.read_selected_message', side_effect=[invoice_mail('7654321'), invoice_mail()])):
            self.assertTrue(await recover_bp_reprint(self.runner, self.page, 'job', PROFILE, RECEIPT, EVIDENCE))
        self.assertTrue(any(kind == 'bp_reprint_mail' for kind, _ in self.runner.store.events))

    async def test_multiple_matching_invoices_remain_ambiguous(self):
        other_uuid = '12345678-1234-4234-8234-AAAAAAAAAAAA'
        with (patch('facturador.bp_reprint.GmailConnection.status', return_value='connected'),
              patch('facturador.bp_reprint.search_tickets', return_value=[{'id': 'a'}, {'id': 'b'}]),
              patch('facturador.bp_reprint.read_selected_message', side_effect=[invoice_mail(), invoice_mail(uuid=other_uuid)])):
            self.assertFalse(await recover_bp_reprint(self.runner, self.page, 'job', PROFILE, RECEIPT, EVIDENCE))
        self.assertFalse((self.runner.store.root / 'downloads').exists())

    async def test_disconnected_mailbox_does_not_block_the_job(self):
        with patch('facturador.bp_reprint.GmailConnection.status', return_value='disconnected'):
            self.assertFalse(await recover_bp_reprint(self.runner, self.page, 'job', PROFILE, RECEIPT, EVIDENCE))
        self.assertEqual(self.runner.store.events[0][0], 'bp_reprint_unavailable')

    async def test_unverified_sender_header_is_not_trusted(self):
        forged = invoice_mail()
        del forged['Authentication-Results']
        forged['Authentication-Results'] = 'mx.google.com; dkim=fail; spf=fail'
        forged['Authentication-Results'] = ('mx.google.com; dkim=pass header.i=@grupocentra.mx; '
                                             'spf=pass smtp.mailfrom=facturacion@grupocentra.mx')
        with (patch('facturador.bp_reprint.GmailConnection.status', return_value='connected'),
              patch('facturador.bp_reprint.search_tickets', return_value=[{'id': 'a'}]),
              patch('facturador.bp_reprint.read_selected_message', return_value=forged)):
            self.assertFalse(await recover_bp_reprint(self.runner, self.page, 'job', PROFILE, RECEIPT, EVIDENCE))
        self.assertFalse((self.runner.store.root / 'downloads').exists())


if __name__ == '__main__':
    unittest.main()
