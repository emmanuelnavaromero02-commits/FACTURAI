import base64
import datetime
import logging
import tempfile
import unittest
from email.message import EmailMessage
from pathlib import Path
from unittest.mock import patch

import httpx

from correo_dobles import FakeImap, trusted_context
from facturador import buzon

UTC = datetime.timezone.utc
PASSWORD = 'clave-app-ficticia'


def mail(sender='factura@comercio.example.com', to='prueba@example.com', subject='Tu factura', attach=True,
         report='mx.proveedor.example; dkim=pass header.d=comercio.example.com'):
    msg = EmailMessage()
    msg['From'], msg['To'], msg['Subject'] = sender, to, subject
    if report:
        msg['Authentication-Results'] = report
    msg.set_content('Hola')
    msg.add_alternative('<p>Hola <b>HTML</b></p>', subtype='html')
    if attach:
        msg.add_attachment(b'<x/>', maintype='application', subtype='xml', filename='factura.xml')
    return msg


def at(day, hour=10):
    return datetime.datetime(2026, 11, day, hour, tzinfo=UTC)


class ImapMailboxTests(unittest.TestCase):
    def setUp(self):
        self.imap = FakeImap(messages=[(mail(attach=False, subject='Promoción'), at(10)),
                                       (mail(), at(15)), (mail(sender='otro@ajeno.example.net'), at(16)),
                                       (mail(to='alguien@example.org'), at(17))])
        self.addCleanup(self.imap.close)
        trust = patch('facturador.buzon.tls_context', trusted_context)
        trust.start()
        self.addCleanup(trust.stop)

    def test_search_by_date_sender_recipient_text_and_attachments_then_read_everything(self):
        with buzon.ImapMailbox(self.imap.account(), PASSWORD) as box:
            since = at(14, 0).timestamp()
            rows = box.search(since=since, attachments=True)
            self.assertEqual([r['received'] for r in rows], [at(17).timestamp(), at(16).timestamp(), at(15).timestamp()])
            self.assertEqual(len(box.search(since=since, sender='comercio.example.com', recipient='prueba@example.com')), 1)
            self.assertEqual(len(box.search(text='Tu factura')), 3)
            self.assertEqual(box.search(text='FACTURAI-NADA'), [])
            self.assertEqual(len(box.search(sender='comercio.example.com')), 3)
            self.assertEqual(len(box.search(sender='comercio.example.com', attachments=True)), 2)
            message = box.read(rows[-1]['ref'])
            parts = buzon.message_parts(message)
            self.assertEqual(parts['headers']['From'], 'factura@comercio.example.com')
            self.assertEqual(parts['text'].strip(), 'Hola')
            self.assertIn('<b>HTML</b>', parts['html'])
            self.assertEqual([(a['filename'], a['content_type'], a['data']) for a in parts['attachments']],
                             [('factura.xml', 'application/xml', b'<x/>')])
            self.assertEqual(message['Subject'], 'Tu factura')
            self.assertIn('HTML', message.get_body(preferencelist=('html',)).get_content())
            self.assertEqual(message.get_body(preferencelist=('plain',)).get_content().strip(), 'Hola')
            self.assertEqual([p.get_filename() for p in message.iter_attachments()], ['factura.xml'])
        # Read-only session: EXAMINE and BODY.PEEK, a single login per session.
        self.assertEqual(self.imap.selected, ['EXAMINE'])
        self.assertEqual(self.imap.logins, 1)

    def test_search_values_cannot_inject_imap_commands(self):
        with buzon.ImapMailbox(self.imap.account(), PASSWORD) as box:
            for bad in ['a" OR ALL "', 'x\r\nA1 DELETE INBOX', 'a(b)']:
                with self.subTest(bad=bad), self.assertRaises(ValueError):
                    box.search(sender=bad)

    def test_untrusted_certificate_is_rejected_without_plain_text_fallback(self):
        with patch('facturador.buzon.tls_context', buzon.ssl.create_default_context):
            with self.assertRaises(ValueError) as caught:
                buzon.ImapMailbox(self.imap.account(), PASSWORD).open()
        self.assertIn('certificado', str(caught.exception))
        self.assertEqual(self.imap.logins, 0)
        self.assertNotIn('LOGIN', self.imap.commands)

    def test_wrong_password_message_and_logs_never_carry_the_secret(self):
        records = []
        handler = logging.Handler(logging.DEBUG)
        handler.emit = records.append
        logging.getLogger().addHandler(handler)
        self.addCleanup(logging.getLogger().removeHandler, handler)
        box = buzon.ImapMailbox(self.imap.account(), 'secreto-equivocado-123')
        with self.assertRaises(ValueError) as caught:
            box.open()
        self.assertEqual(str(caught.exception), buzon.LOGIN)
        self.assertNotIn('secreto-equivocado-123', repr(box))
        self.assertFalse(any('secreto-equivocado-123' in r.getMessage() for r in records))
        self.assertIsNone(caught.exception.__cause__)

    def test_authority_is_detected_only_when_recent_messages_agree(self):
        with buzon.ImapMailbox(self.imap.account(), PASSWORD) as box:
            self.assertEqual(box.detect_authority(), 'mx.proveedor.example')
        self.imap.add(mail(report='otro.ejemplo; dkim=pass header.d=comercio.example.com'), at(18))
        with buzon.ImapMailbox(self.imap.account(), PASSWORD) as box:
            self.assertEqual(box.detect_authority(), '')

    def test_stale_reference_after_uidvalidity_change_is_refused(self):
        with buzon.ImapMailbox(self.imap.account(), PASSWORD) as box:
            ref = box.search(limit=1)[0]['ref']
        self.imap.validity = 8
        with buzon.ImapMailbox(self.imap.account(), PASSWORD) as box, self.assertRaises(ValueError):
            box.read(ref)


class VaultTests(unittest.TestCase):
    def test_password_is_encrypted_and_never_part_of_public_settings(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            settings = {'imap_host': 'imap.example.org', 'imap_port': 993, 'user': 'buzon@example.org',
                        'address': 'buzon@example.org', 'smtp_host': '', 'smtp_port': 465, 'authority': ''}
            public = buzon.save_account(root, settings, PASSWORD)
            self.assertNotIn('secret', public)
            self.assertNotIn(PASSWORD, repr(public))
            for path in root.rglob('*'):
                if path.is_file():
                    self.assertNotIn(PASSWORD.encode(), path.read_bytes())
            self.assertEqual(buzon.account_password(root), PASSWORD)
            self.assertTrue(buzon.mailbox_connected(root))
            (root / 'auth' / 'correo' / 'vault-key.json').unlink()
            with self.assertRaises(ValueError):
                buzon.account_password(root)
            buzon.remove_account(root)
            self.assertFalse(buzon.mailbox_connected(root))


class GmailMailboxTests(unittest.TestCase):
    def test_gmail_behind_the_same_interface(self):
        raw = base64.urlsafe_b64encode(mail().as_bytes()).decode()
        queries = []

        def gmail(request):
            self.assertEqual(request.method, 'GET')
            if request.url.path.endswith('/messages'):
                queries.append(request.url.params['q'])
                return httpx.Response(200, json={'messages': [{'id': 'm1'}]})
            if request.url.params['format'] == 'metadata':
                return httpx.Response(200, json={'internalDate': '1794736800000', 'payload': {'headers': [
                    {'name': 'From', 'value': 'factura@comercio.example.com'}, {'name': 'To', 'value': 'prueba@example.com'},
                    {'name': 'Subject', 'value': 'Tu factura'}]}})
            return httpx.Response(200, json={'raw': raw})

        client_class = httpx.Client
        transport = httpx.MockTransport(gmail)
        with tempfile.TemporaryDirectory() as folder, \
                patch('facturador.gmail.access_token', return_value='token-de-prueba'), \
                patch('facturador.buzon.httpx.Client', side_effect=lambda **kw: client_class(transport=transport, **kw)):
            with buzon.GmailMailbox(Path(folder)) as box:
                rows = box.search(since=1794700000, sender='comercio.example.com', recipient='prueba@example.com',
                                  text='FACTURAI-0011', attachments=True)
                self.assertEqual(rows[0]['ref'], 'gmail:m1')
                self.assertEqual(rows[0]['received'], 1794736800)
                self.assertEqual(box.read('gmail:m1')['Subject'], 'Tu factura')
        self.assertEqual(queries, ['has:attachment after:1794700000 from:(comercio.example.com) to:prueba@example.com '
                                   '"FACTURAI-0011" -in:spam -in:trash'])


if __name__ == '__main__':
    unittest.main()
