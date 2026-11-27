import datetime
import tempfile
import time
import unittest
from email.message import EmailMessage
from pathlib import Path
from unittest.mock import patch

from correo_dobles import FakeImap, trusted_context
from facturador import buzon
from facturador.portal_mail import authenticated_by, gmail_authenticated, registration_email

ORIGIN = 'https://factura.comercio.example.com'
HOST = 'factura.comercio.example.com'
USER = 'prueba@example.com'
PASSWORD = 'clave-app-ficticia'
LINK = ORIGIN + '/cuenta/activar?k=demo'
PROVIDER = 'mx.proveedor.example'
GOOD = (f'{PROVIDER}; spf=pass smtp.mailfrom={HOST}; dkim=pass (firma verificada) header.d={HOST}; '
        f'dmarc=pass action=none header.from={HOST}')


def enrollment(sender='registro@' + HOST, reports=(GOOD,), link=LINK):
    msg = EmailMessage()
    for report in reports:
        msg['Authentication-Results'] = report
    msg['From'], msg['To'], msg['Subject'] = sender, USER, 'Activa tu cuenta'
    msg.set_content('Activa tu cuenta con la liga.')
    msg.add_alternative(f'<p>Bienvenido</p><a href="{link}">Activar cuenta</a>', subtype='html')
    return msg


class ImapRegistrationTests(unittest.TestCase):
    def setUp(self):
        folder = tempfile.TemporaryDirectory()
        self.addCleanup(folder.cleanup)
        self.root = Path(folder.name)
        self.imap = FakeImap(user='buzon@example.com')
        self.addCleanup(self.imap.close)
        trust = patch('facturador.buzon.tls_context', trusted_context)
        trust.start()
        self.addCleanup(trust.stop)
        self.started = time.time() - 60

    def deliver(self, msg):
        self.imap.add(msg, datetime.datetime.now(datetime.timezone.utc))

    def lookup(self, authority=PROVIDER):
        buzon.save_account(self.root, self.imap.account(authority=authority), PASSWORD)
        return registration_email(self.root, ORIGIN, USER, self.started, 'activate')

    def test_authenticated_message_from_the_portal_domain_is_used(self):
        self.deliver(enrollment())
        self.assertEqual(self.lookup(), LINK)
        self.assertEqual(self.imap.selected, ['EXAMINE'])

    def test_dkim_alone_aligned_is_enough(self):
        self.deliver(enrollment(reports=(f'{PROVIDER}; dkim=pass header.d={HOST}; dmarc=none',)))
        self.assertEqual(self.lookup(), LINK)

    def test_rejected_messages(self):
        cases = {
            'remitente ajeno': enrollment(sender='registro@ajeno.example.net',
                                          reports=(f'{PROVIDER}; dkim=pass header.d=ajeno.example.net; '
                                                   'dmarc=pass header.from=ajeno.example.net',)),
            'sin autenticación': enrollment(reports=()),
            'fallida': enrollment(reports=(f'{PROVIDER}; dkim=fail header.d={HOST}; dmarc=fail header.from={HOST}',)),
            'firma de otro dominio': enrollment(reports=(f'{PROVIDER}; dkim=pass header.d=ajeno.example.net; dmarc=none',)),
            # The forged report sits below a header written by somebody else, never on top.
            'encabezado falsificado': enrollment(reports=(f'mx.atacante.example; dkim=pass header.d={HOST}', GOOD)),
        }
        for name, msg in cases.items():
            with self.subTest(name):
                self.imap.messages.clear()
                self.deliver(msg)
                self.assertIsNone(self.lookup())

    def test_without_a_pinned_provider_the_message_is_left_for_the_person(self):
        self.deliver(enrollment())
        self.assertIsNone(self.lookup(authority=''))
        self.assertNotIn('UID SEARCH', self.imap.commands)

    def test_messages_from_before_the_enrollment_are_ignored(self):
        self.imap.add(enrollment(), datetime.datetime.now(datetime.timezone.utc) - datetime.timedelta(minutes=10))
        self.assertIsNone(self.lookup())

    def test_two_matching_messages_are_not_guessed(self):
        self.deliver(enrollment())
        self.deliver(enrollment(link=ORIGIN + '/cuenta/activar?k=otra'))
        with self.assertRaises(ValueError):
            self.lookup()


class GmailRegistrationTests(unittest.TestCase):
    def test_gmail_keeps_its_query_and_its_dmarc_rule(self):
        gmail_ok = enrollment(reports=(f'mx.google.com; dkim=pass header.i=@{HOST}; dmarc=pass (p=NONE) header.from={HOST}',))
        dkim_only = enrollment(reports=(f'mx.google.com; dkim=pass header.d={HOST}',))
        started = time.time() - 60
        for msg, expected in [(gmail_ok, LINK), (dkim_only, None)]:
            with self.subTest(expected=expected), tempfile.TemporaryDirectory() as folder:
                root = Path(folder)
                (root / 'gmail_token.json').write_text('{}')
                queries = []

                def search(_, query):
                    queries.append(query)
                    return [{'id': '1', 'sender': msg['From'], 'received_ms': int(time.time() * 1000)}]

                value = registration_email(root, ORIGIN, USER, started, 'activate', search=search,
                                           read=lambda _, key: msg)
                self.assertEqual(value, expected)
                self.assertEqual(queries, [f'to:{USER} from:({HOST}) after:{int(started)} -in:spam -in:trash'])

    def test_no_mailbox_keeps_the_former_error(self):
        with tempfile.TemporaryDirectory() as folder, self.assertRaises(ValueError) as caught:
            registration_email(Path(folder), ORIGIN, USER, time.time() - 5, 'activate')
        self.assertIn('Gmail', str(caught.exception))

    def test_gmail_rule_matches_the_account_flow_rule(self):
        from facturador.account_flow import authenticated_registration
        samples = [enrollment(reports=(f'mx.google.com; dmarc=pass header.from={HOST}',)),
                   enrollment(reports=(f'mx.google.com; dmarc=pass header.from=otro.{HOST}',)),
                   enrollment(reports=(f'{PROVIDER}; dmarc=pass header.from={HOST}',)),
                   enrollment(reports=()), enrollment(sender='x@ajeno.example.net')]
        for msg in samples:
            self.assertEqual(gmail_authenticated(msg, HOST), authenticated_registration(msg, HOST))

    def test_imap_rule_needs_the_pinned_provider(self):
        self.assertTrue(authenticated_by(enrollment(), HOST, PROVIDER))
        self.assertFalse(authenticated_by(enrollment(), HOST, ''))
        self.assertFalse(authenticated_by(enrollment(), HOST, 'mx.google.com'))


if __name__ == '__main__':
    unittest.main()
