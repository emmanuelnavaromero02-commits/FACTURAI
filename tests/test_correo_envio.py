import base64
import datetime
import email
import hashlib
import json
import logging
import tempfile
import unittest
from email import policy
from email.message import EmailMessage
from pathlib import Path
from unittest.mock import patch

import httpx
from fastapi import FastAPI
from fastapi.responses import PlainTextResponse
from fastapi.testclient import TestClient
from PIL import Image

from correo_dobles import FakeImap, FakeSmtp, parsed, trusted_context
from facturador import buzon, gmail, permisos
from facturador.correo_ajustes import correo_router
from facturador.correo_envio import REFUSED, MailOutbox, compose_mail
from facturador.db import Store
from test_core import PROFILE, RECEIPT

PASSWORD = 'clave-app-ficticia'
MERCHANT = 'facturacion@comercio.example.com'


class Case(unittest.TestCase):
    smtp_mode = 'ssl'

    def setUp(self):
        folder = tempfile.TemporaryDirectory()
        self.addCleanup(folder.cleanup)
        self.root = Path(folder.name)
        self.store = Store(self.root)
        self.key = self.ticket('ticket-1', RECEIPT)
        self.smtp = FakeSmtp(mode=self.smtp_mode)
        self.addCleanup(self.smtp.close)
        trust = patch('facturador.buzon.tls_context', trusted_context)
        trust.start()
        self.addCleanup(trust.stop)

    def ticket(self, name, receipt):
        source = self.root / 'originales' / (name + '.png')
        source.parent.mkdir(parents=True, exist_ok=True)
        Image.new('RGB', (12, 12), tuple(hashlib.sha256(name.encode()).digest()[:3])).save(source)
        row, _ = self.store.add(str(source), hashlib.sha256(source.read_bytes()).hexdigest())
        self.store.save_receipt(row['id'], receipt, ready=True)
        return row['id']

    def connect_smtp(self, **changes):
        buzon.save_account(self.root, {'imap_host': '127.0.0.1', 'imap_port': 1, 'user': self.smtp.user,
                                       'address': self.smtp.user, 'smtp_host': '127.0.0.1', 'smtp_port': self.smtp.port,
                                       'smtp_tls': self.smtp_mode if self.smtp_mode != 'plain' else 'starttls',
                                       'authority': '', **changes}, PASSWORD)

    def request_row(self, key=None):
        with self.store.connect() as db:
            row = db.execute('SELECT * FROM mail_requests WHERE job_id=?', (key or self.key,)).fetchone()
        return dict(row) if row else None


class SendTests(Case):
    def test_without_consent_or_permission_nothing_is_contacted(self):
        self.connect_smtp()
        with self.assertRaises(ValueError):
            MailOutbox(self.store).request(self.key, MERCHANT, False, PROFILE)
        self.assertEqual(self.smtp.commands, [])
        self.assertIsNone(self.request_row())
        self.assertFalse(self.store.get(self.key)['issue_attempted'])

    def test_confirmed_request_is_recorded_before_the_data_and_sent_once(self):
        self.connect_smtp()
        outbox = MailOutbox(self.store)
        seen = {}

        def at_data():
            seen['row'] = self.request_row()
            seen['events'] = [e['kind'] for e in self.store.events(self.key)]
            seen['job'] = self.store.get(self.key)

        self.smtp.on_data = at_data
        status = outbox.request(self.key, MERCHANT, True, PROFILE)
        self.assertEqual(status['status'], 'sent')
        self.assertEqual(seen['row']['state'], 'sending')
        self.assertIn('mail_attempt', seen['events'])
        self.assertEqual((seen['job']['status'], seen['job']['issue_attempted']), ('external', 1))
        self.assertEqual(len(self.smtp.received), 1)
        sent = parsed(self.smtp.received[0])
        self.assertEqual(self.smtp.received[0]['to'], [MERCHANT])
        self.assertEqual(sent['To'], MERCHANT)
        self.assertIn(status['reference'], sent['Subject'])
        body = sent.get_body(preferencelist=('plain',)).get_content()
        subject, expected = compose_mail(RECEIPT, PROFILE, status['reference'])
        self.assertEqual(body.replace('\r\n', '\n').strip(), expected.strip())
        self.assertIn(PROFILE.rfc, body)
        self.assertEqual([p.get_filename() for p in sent.iter_attachments()], [f'ticket-{status["reference"]}.png'])
        self.assertNotIn(PASSWORD.encode(), self.smtp.received[0]['data'])
        with self.assertRaises(ValueError):
            outbox.request(self.key, MERCHANT, True, PROFILE)
        self.assertEqual(len(self.smtp.received), 1)
        self.assertEqual(MailOutbox(self.store).status(self.key)['status'], 'sent')

    def test_standing_permission_replaces_the_per_ticket_confirmation(self):
        self.connect_smtp()
        permisos.guardar(self.root, 'pedir_por_correo', True)
        self.assertEqual(MailOutbox(self.store).request(self.key, MERCHANT, False, PROFILE)['status'], 'sent')

    def test_refused_recipient_sends_nothing_and_can_be_corrected(self):
        self.connect_smtp()
        self.smtp.refuse.add(MERCHANT)
        outbox = MailOutbox(self.store)
        with self.assertRaises(ValueError) as caught:
            outbox.request(self.key, MERCHANT, True, PROFILE)
        self.assertEqual(str(caught.exception), REFUSED)
        self.assertNotIn('DATA', self.smtp.commands)
        self.assertIsNone(self.request_row())
        job = self.store.get(self.key)
        self.assertEqual((job['status'], job['issue_attempted']), ('ready', 0))
        self.assertEqual(outbox.request(self.key, 'ventas@comercio.example.com', True, PROFILE)['status'], 'sent')

    def test_interrupted_delivery_is_uncertain_and_never_resent(self):
        self.connect_smtp()
        self.smtp.drop_after_data = True
        outbox = MailOutbox(self.store)
        self.assertEqual(outbox.request(self.key, MERCHANT, True, PROFILE)['status'], 'uncertain')
        with self.assertRaises(ValueError):
            MailOutbox(self.store).request(self.key, MERCHANT, True, PROFILE)
        self.assertEqual(len(self.smtp.received), 1)
        self.assertTrue(self.store.get(self.key)['issue_attempted'])

    def test_restart_turns_an_unfinished_send_into_uncertain_and_drops_unsent_preparations(self):
        other = self.ticket('ticket-2', RECEIPT.model_copy(update={'billing_codes': ['COD-O0-I2']}))
        MailOutbox(self.store)
        with self.store.connect() as db:
            db.execute("INSERT INTO mail_requests(job_id,address,reference,channel,state,detail,created,updated) "
                       "VALUES(?,?,?,?,?,?,?,?)", (self.key, MERCHANT, 'FACTURAI-AAA', 'smtp', 'sending', '', 'x', 'x'))
            db.execute("INSERT INTO mail_requests(job_id,address,reference,channel,state,detail,created,updated) "
                       "VALUES(?,?,?,?,?,?,?,?)", (other, MERCHANT, 'FACTURAI-BBB', '', 'preparing', '', 'x', 'x'))
        outbox = MailOutbox(self.store)
        self.assertEqual(outbox.status(self.key)['status'], 'uncertain')
        self.assertEqual(outbox.status(other)['status'], 'new')

    def test_not_a_reviewed_ticket(self):
        self.connect_smtp()
        self.store.update(self.key, status='review')
        with self.assertRaises(ValueError):
            MailOutbox(self.store).request(self.key, MERCHANT, True, PROFILE)
        self.assertEqual(self.smtp.commands, [])

    def test_gmail_api_send_when_its_optional_permission_was_granted(self):
        (self.root / 'gmail_token.json').write_text(json.dumps({
            'token': 'token-de-prueba', 'refresh_token': 'r', 'expires_at': 9e12,
            'scope': gmail.SCOPE + ' ' + gmail.SEND_SCOPE}))
        posts = []

        def post(url, **kwargs):
            posts.append((url, kwargs['json']))
            return httpx.Response(200, json={'id': 'enviado-1'}, request=httpx.Request('POST', url))

        with patch('facturador.gmail.httpx.post', side_effect=post):
            status = MailOutbox(self.store).request(self.key, MERCHANT, True, PROFILE)
        self.assertEqual(status['status'], 'sent')
        self.assertEqual(posts[0][0], gmail.GMAIL + '/send')
        sent = email.message_from_bytes(base64.urlsafe_b64decode(posts[0][1]['raw']), policy=policy.default)
        self.assertEqual(sent['To'], MERCHANT)
        self.assertEqual(self.request_row()['channel'], 'gmail')

    def test_gmail_without_send_permission_cannot_send(self):
        (self.root / 'gmail_token.json').write_text(json.dumps({'token': 't', 'scope': gmail.SCOPE, 'expires_at': 9e12}))
        with self.assertRaises(ValueError):
            MailOutbox(self.store).request(self.key, MERCHANT, True, PROFILE)
        self.assertIsNone(self.request_row())

    def test_gmail_authorization_asks_for_sending_only_when_requested(self):
        connection = gmail.GmailConnection(self.root)
        (self.root / 'google_client.json').write_text(json.dumps(
            {'installed': {'client_id': 'demo.apps.googleusercontent.com', 'client_secret': 'x'}}))
        self.assertNotIn('gmail.send', connection.start())
        self.assertIn('gmail.send', connection.start(send=True))


class StartTlsTests(Case):
    smtp_mode = 'starttls'

    def test_starttls_is_used(self):
        self.connect_smtp()
        self.assertEqual(MailOutbox(self.store).request(self.key, MERCHANT, True, PROFILE)['status'], 'sent')
        self.assertLess(self.smtp.commands.index('STARTTLS'), self.smtp.commands.index('AUTH'))


class PlainServerTests(Case):
    smtp_mode = 'plain'

    def test_server_without_encryption_is_refused_before_the_password(self):
        self.connect_smtp()
        with self.assertRaises(ValueError):
            MailOutbox(self.store).request(self.key, MERCHANT, True, PROFILE)
        self.assertNotIn('AUTH', self.smtp.commands)
        self.assertIsNone(self.request_row())


class RouteTests(Case):
    def setUp(self):
        super().setUp()
        report = 'mx.proveedor.example; dkim=pass header.d=example.org'
        messages = []
        for index in range(3):
            msg = EmailMessage()
            msg['Authentication-Results'] = report
            msg['From'], msg['To'], msg['Subject'] = 'aviso@example.org', self.smtp.user, f'Aviso {index}'
            msg.set_content('Hola')
            messages.append((msg, datetime.datetime(2026, 11, 20 + index, tzinfo=datetime.timezone.utc)))
        self.imap = FakeImap(user=self.smtp.user, messages=messages)
        self.addCleanup(self.imap.close)
        self.records = []
        handler = logging.Handler(logging.DEBUG)
        handler.emit = self.records.append
        logging.getLogger().addHandler(handler)
        self.addCleanup(logging.getLogger().removeHandler, handler)
        app = FastAPI()

        @app.exception_handler(ValueError)
        async def invalid(request, exc):
            return PlainTextResponse(str(exc), status_code=422)

        app.include_router(correo_router(self.root, self.store, lambda: PROFILE))
        self.client = TestClient(app)

    def account(self, **changes):
        return {'imap_host': '127.0.0.1', 'imap_port': self.imap.port, 'smtp_host': '127.0.0.1',
                'smtp_port': self.smtp.port, 'smtp_tls': 'ssl', 'user': self.smtp.user, 'password': PASSWORD, **changes}

    def assert_clean(self, text):
        self.assertNotIn(PASSWORD, text)
        self.assertNotIn('secreto-largo', text)

    def test_connect_status_request_and_disconnect_without_exposing_the_secret(self):
        self.assertFalse(self.client.get('/api/correo').json()['conectado'])
        wrong = self.client.post('/api/correo/cuenta', json=self.account(password='otra-clave-equivocada'))
        self.assertEqual(wrong.status_code, 422)
        self.assertNotIn('otra-clave-equivocada', wrong.text)
        for bad in [self.account(password='secreto-largo' * 60), {**self.account(), 'extra': 1},
                    self.account(imap_port='x'), self.account(imap_host='bad host')]:
            response = self.client.post('/api/correo/cuenta', json=bad)
            self.assertEqual(response.status_code, 422)
            self.assert_clean(response.text)
        connected = self.client.post('/api/correo/cuenta', json=self.account())
        self.assertEqual(connected.status_code, 200, connected.text)
        body = connected.json()
        self.assertTrue(body['conectado'] and body['envio'])
        self.assertEqual(body['imap']['authority'], 'mx.proveedor.example')
        self.assertTrue(body['imap']['verifica_altas'])
        self.assert_clean(connected.text)
        self.assert_clean(self.client.get('/api/correo').text)
        self.assertEqual(self.client.post('/api/correo/probar').json()['prueba'], 'ok')
        self.assertEqual(self.client.get(f'/api/jobs/{self.key}/correo').json()['status'], 'new')
        refused = self.client.post(f'/api/jobs/{self.key}/correo/pedir', json={'to': MERCHANT})
        self.assertEqual(refused.status_code, 422)
        sent = self.client.post(f'/api/jobs/{self.key}/correo/pedir', json={'to': MERCHANT, 'consent': True})
        self.assertEqual(sent.json()['status'], 'sent')
        self.assert_clean(sent.text)
        self.assert_clean(self.client.get(f'/api/jobs/{self.key}/correo').text)
        self.assertEqual(self.client.post(f'/api/jobs/{self.key}/correo/pedir',
                                          json={'to': MERCHANT, 'consent': True}).status_code, 422)
        self.assertEqual(len(self.smtp.received), 1)
        # Nothing stored, logged or attached to events carries the password in clear.
        self.assert_clean(json.dumps(self.store.events(self.key)))
        with self.store.connect() as db:
            self.assert_clean(json.dumps([dict(r) for r in db.execute('SELECT * FROM mail_requests')]))
        self.assertFalse(any(PASSWORD in r.getMessage() for r in self.records))
        for path in self.root.rglob('*'):
            if path.is_file():
                self.assertNotIn(PASSWORD.encode(), path.read_bytes(), path)
        status = self.client.delete('/api/correo/cuenta').json()
        self.assertFalse(status['conectado'])
        self.assertIsNone(status['imap'])

    def test_search_now_requires_a_waiting_ticket(self):
        self.assertEqual(self.client.post(f'/api/jobs/{self.key}/correo/buscar').status_code, 422)


if __name__ == '__main__':
    unittest.main()
