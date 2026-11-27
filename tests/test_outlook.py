"""Outlook.com, Hotmail and Microsoft 365 mailboxes: Microsoft's sign-in, then IMAP and SMTP with XOAUTH2.

Everything runs against doubles on this machine: a stand-in for Microsoft's authorize and token
endpoints, and IMAP and SMTP servers over TLS that only take the access tokens it issued. Nothing
here talks to Microsoft or to a real mailbox.
"""
import asyncio
import datetime
import hashlib
import json
import logging
import os
import tempfile
import time
import unittest
from email.message import EmailMessage
from pathlib import Path
from unittest.mock import patch
from urllib.parse import parse_qs, parse_qsl, urlsplit

from fastapi.testclient import TestClient
from PIL import Image

from acceso_dobles import ORIGIN, PASSWORD, Visitor, make_accounts, make_entrance, use_master_key
from correo_dobles import FakeImap, FakeSmtp, parsed, trusted_context
from facturador import buzon, correo_envio, outlook, secretos
from facturador.app import create_app
from facturador.avisos import Avisos
from facturador.avisos_vigilante import BUZON_RECONECTAR, VigilanteAvisos
from facturador.correo_ajustes import SEND_NOT_ACTIVE, USE_OUTLOOK
from facturador.correo_envio import NO_SENDER, MailOutbox
from facturador.db import Store
from facturador.invoice_mail import InvoiceMailRecovery
from facturador.portal_mail import authenticated_by, registration_email
from microsoft_dobles import (CLIENT, IMAP_SCOPE, MARKS, OTHER_CLIENT, PERSONAL_TENANT, SEND_SCOPE, WORK_TENANT,
                              FakeMicrosoft, visit)
from test_core import FISCAL_UUID, PROFILE, RECEIPT, pdf_bytes, xml_bytes

UTC = datetime.timezone.utc
ADDRESS = 'persona@outlook.example'
OTHER = 'otra@outlook.example'
UNUSED = 'nadie-usa-esta-clave'
LOCAL_REDIRECT = 'http://localhost/auth/outlook/callback'
PORTAL = 'https://factura.comercio.example.com'
HOST = 'factura.comercio.example.com'
LINK = PORTAL + '/cuenta/activar?k=demo'
MERCHANT = 'facturacion@comercio.example.com'
# The header Microsoft's servers write on inbound mail: no identifier in front, and their own «compauth».
MICROSOFT_PASS = (f'spf=pass (sender IP is 203.0.113.9) smtp.mailfrom={HOST}; dkim=pass (signature was verified) '
                  f'header.d={HOST};dmarc=pass action=none header.from={HOST};compauth=pass reason=100')
MICROSOFT_FAIL = (f'spf=fail (sender IP is 198.51.100.7) smtp.mailfrom={HOST}; dkim=fail (body hash did not verify) '
                  f'header.d={HOST};dmarc=fail action=oreject header.from={HOST};compauth=fail reason=000')


def without_outlook_settings(**values):
    """os.environ without any Outlook setting of the machine, plus the given ones."""
    kept = {name: value for name, value in os.environ.items() if not name.startswith('FACTURADOR_OUTLOOK_')}
    return patch.dict(os.environ, {**kept, **values}, clear=True)


def enrollment(*reports, sender='registro@' + HOST, link=LINK):
    msg = EmailMessage()
    for report in reports:
        msg['Authentication-Results'] = report
    msg['From'], msg['To'], msg['Subject'] = sender, PROFILE.email, 'Activa tu cuenta'
    msg.set_content('Activa tu cuenta con la liga.')
    msg.add_alternative(f'<p>Bienvenido</p><a href="{link}">Activar cuenta</a>', subtype='html')
    return msg


def invoice(*attachments, report=MICROSOFT_PASS):
    msg = EmailMessage()
    msg['Authentication-Results'] = report
    msg['From'], msg['To'], msg['Subject'] = 'facturas@comercio.example.com', PROFILE.email, 'Tu factura electrónica'
    msg.set_content('Adjuntamos su comprobante fiscal.')
    for name, data in attachments:
        msg.add_attachment(data, maintype='application', subtype=name.rpartition('.')[2], filename=name)
    return msg


def day(number, hour=12):
    return datetime.datetime(2026, 11, number, hour, tzinfo=UTC)


class Case(unittest.TestCase):
    """A data folder, Microsoft's stand-in and the two mail servers that only take its tokens."""
    smtp_mode = 'starttls'

    def setUp(self):
        folder = tempfile.TemporaryDirectory(ignore_cleanup_errors=True)
        self.addCleanup(folder.cleanup)
        self.root = Path(folder.name)
        self.microsoft = FakeMicrosoft()
        self.addCleanup(self.microsoft.close)
        self.microsoft.redirects.add(LOCAL_REDIRECT)
        self.imap = FakeImap(user=ADDRESS, password=UNUSED, tokens=self.microsoft.access)
        self.smtp = FakeSmtp(self.smtp_mode, user=ADDRESS, password=UNUSED, tokens=self.microsoft.access)
        self.addCleanup(self.imap.close)
        self.addCleanup(self.smtp.close)
        for target in (patch.object(outlook, 'AUTHORIZE', self.microsoft.authorize),
                       patch.object(outlook, 'TOKEN', self.microsoft.token),
                       patch.object(outlook, 'IMAP_SERVER', ('127.0.0.1', self.imap.port)),
                       patch.object(outlook, 'SMTP_SERVERS', {'personal': ('127.0.0.1', self.smtp.port),
                                                              'trabajo': ('127.0.0.1', self.smtp.port)}),
                       patch('facturador.buzon.tls_context', trusted_context), without_outlook_settings()):
            self.enterContext(target)
        outlook._FORCED.clear()
        outlook._WARNED.clear()
        self.replies, self.records = [], []
        handler = logging.Handler(logging.DEBUG)
        handler.emit = self.records.append
        log = logging.getLogger()
        self.addCleanup(log.setLevel, log.level)
        self.addCleanup(log.removeHandler, handler)
        log.addHandler(handler)
        log.setLevel(logging.DEBUG)

    # ------------------------------------------------------------ the person's browser

    visit = staticmethod(visit)

    def sign_in(self, connection, send=False):
        """Go to Microsoft with the address the program gave and come back with its answer after «#»."""
        status, location = self.visit(connection.start(send))
        self.assertEqual(status, 302)
        target, _, fragment = location.partition('#')
        return target, dict(parse_qsl(fragment))

    def connection(self, **options):
        return outlook.OutlookConnection(self.root, 8765, **options)

    def connect(self, send=False, connection=None):
        connection = connection or self.connection()
        if connection.editable and not connection.client():
            connection.configure(CLIENT)
        _, answer = self.sign_in(connection, send)
        return connection.finish(answer['state'], answer['code'])

    def expire(self, seconds=-1):
        """The saved access token is good for ``seconds`` more (already over by default)."""
        with outlook._lock(self.root):
            record, tokens = outlook._load(self.root)
            outlook._save(self.root, {**record, 'expires_at': time.time() + seconds}, tokens)

    def renewals(self):
        return [form for form in self.microsoft.token_requests if form.get('grant_type') == 'refresh_token']

    # ------------------------------------------------------------ nothing secret is left anywhere

    def logged(self):
        lines = []
        for record in self.records:
            try:
                lines.append(record.getMessage())
            except Exception:
                lines.append(str(record.msg) + repr(record.args))
        return '\n'.join(lines)

    @staticmethod
    def content(path):
        """A file of the data folder; one that is being replaced right now is read a moment later."""
        for _ in range(40):
            try:
                return path.read_bytes()
            except FileNotFoundError:
                return b''
            except OSError:
                time.sleep(.05)
        return path.read_bytes()

    def assert_no_secrets(self, *texts):
        """No token, code or identity of the stand-in in the data folder, the logs or what the API answered."""
        for path in self.root.rglob('*'):
            if path.is_file():
                for mark in MARKS:
                    self.assertNotIn(mark.encode(), self.content(path), path.name)
        everything = '\n'.join([*self.replies, self.logged(), *texts])
        for mark in MARKS:
            self.assertNotIn(mark, everything)


class AuthorityTests(unittest.TestCase):
    def test_microsoft_reports_without_an_identifier_get_their_own_name(self):
        documented = [MICROSOFT_PASS, MICROSOFT_FAIL,
                      'spf=pass (sender IP is 10.2.3.4)\r\n  smtp.mailfrom=fabrikam.com; contoso.com; dkim=none\r\n'
                      '  (message not signed) header.d=none; contoso.com; dmarc=bestguesspass\r\n'
                      '  action=none header.from=fabrikam.com; compauth=pass reason=109',
                      'SPF=None (sender IP is 192.168.1.8) smtp.mailfrom=x.example; DKIM=pass header.d=x.example; '
                      'COMPAUTH=fail reason=001']
        for report in documented:
            self.assertEqual(buzon.authserv_id(report), buzon.MICROSOFT_AUTHORITY, report)
        # The name is not a valid identifier: no header can claim it and the settings form refuses it.
        self.assertIsNone(buzon.AUTHORITY.fullmatch(buzon.MICROSOFT_AUTHORITY))
        self.assertEqual(buzon.authserv_id(buzon.MICROSOFT_AUTHORITY + f'; dkim=pass header.d={HOST}; compauth=pass'), '')

    def test_every_other_report_keeps_its_meaning(self):
        cases = {'mx.proveedor.example; dkim=pass header.d=a.example': 'mx.proveedor.example',
                 'MX.Google.com;\r\n dkim=pass': 'mx.google.com',
                 'mx.proveedor.example (comentario) 1; spf=pass; compauth=pass': 'mx.proveedor.example',
                 '': '', ';': '', 'dkim=pass header.d=a.example': '',
                 f'spf=pass smtp.mailfrom={HOST}; dkim=pass header.d={HOST}': '',  # Without Microsoft's own verdict.
                 'compauth=pass reason=100': '', 'algo=raro; compauth=pass': ''}
        for report, expected in cases.items():
            self.assertEqual(buzon.authserv_id(report), expected, report)

    def test_rule_for_registration_mail_reads_microsofts_report_only_when_that_name_is_pinned(self):
        good, forged_below = enrollment(MICROSOFT_PASS), enrollment(MICROSOFT_FAIL, MICROSOFT_PASS)
        self.assertTrue(authenticated_by(good, HOST, buzon.MICROSOFT_AUTHORITY))
        self.assertFalse(authenticated_by(forged_below, HOST, buzon.MICROSOFT_AUTHORITY))
        for authority in ('', 'mx.proveedor.example', 'mx.google.com'):
            self.assertFalse(authenticated_by(good, HOST, authority), authority)
        self.assertFalse(authenticated_by(enrollment('mx.proveedor.example; dkim=pass header.d=' + HOST), HOST,
                                          buzon.MICROSOFT_AUTHORITY))


class SignInTests(Case):
    """The whole connection through the application's routes, as the panel uses them."""

    def panel(self, port=8765, **options):
        self.origin = f'http://127.0.0.1:{port}'
        app = create_app(self.root, port=port, **options)
        self.client = self.enterContext(TestClient(app, base_url=self.origin))
        self.headers = {'Origin': self.origin, 'X-App-Token': self.client.get('/api/state').json()['token']}
        return app

    def api(self, method, path, **options):
        response = self.client.request(method, path, headers=self.headers, **options)
        self.replies.append(response.text)
        return response

    def status(self):
        """What the mail card reads: every mailbox together, and the Outlook option apart."""
        return {**self.api('GET', '/api/correo').json(), 'outlook': self.api('GET', '/api/correo/outlook').json()}

    def authorize(self, send=False):
        started = self.api('POST', '/api/correo/outlook/conectar', json={'enviar': send})
        self.assertEqual(started.status_code, 200, started.text)
        address = started.json()['url']
        status, location = self.visit(address)
        self.assertEqual(status, 302)
        target, _, fragment = location.partition('#')
        return address, target, dict(parse_qsl(fragment))

    def finish(self, answer):
        return self.api('POST', '/api/correo/outlook/terminar', json={
            'estado': answer.get('state', ''), 'codigo': answer.get('code', ''), 'error': answer.get('error', '')})

    def test_connection_from_the_panel_with_the_mailbox_checked_before_it_is_saved(self):
        for port in (8765, 18765):
            with self.subTest(port=port):
                self.root = Path(self.enterContext(tempfile.TemporaryDirectory(ignore_cleanup_errors=True)))
                self.panel(port)
                first = self.status()
                # The common answer keeps its shape; the option has its own.
                self.assertEqual(sorted(self.api('GET', '/api/correo').json()), ['conectado', 'envio', 'gmail', 'imap', 'permiso_pedir'])
                self.assertFalse(first['conectado'])
                self.assertEqual(first['outlook'], {
                    'estado': 'sin_aplicacion', 'aplicacion': {'configurada': False, 'editable': True, 'identificador': ''},
                    'redireccion': f'http://localhost:{port}/auth/outlook/callback', 'cuenta': '', 'envio': False,
                    'verifica_altas': False, 'conectado_desde': None, 'vigente_hasta': None, 'mensaje': ''})
                # Like every other change, it needs the panel's own token and origin.
                self.assertEqual(self.client.post('/api/correo/outlook/conectar', json={}).status_code, 403)
                self.assertEqual(self.client.post('/api/correo/outlook/conectar', json={}, headers={
                    **self.headers, 'Origin': 'https://ajeno.example'}).status_code, 403)
                self.assertEqual(self.api('POST', '/api/correo/outlook/conectar', json={}).status_code, 422)
                saved = self.api('POST', '/api/correo/outlook/aplicacion', json={'identificador': f'  {CLIENT.upper()} '})
                self.assertEqual(saved.json()['estado'], 'lista')
                self.assertEqual(saved.json()['aplicacion'], {'configurada': True, 'editable': True, 'identificador': CLIENT})

                address, target, answer = self.authorize()
                asked = urlsplit(address)
                self.assertEqual(asked.path, '/common/oauth2/v2.0/authorize')
                query = {key: values[0] for key, values in parse_qs(asked.query).items()}
                self.assertEqual(query.pop('scope').split(), ['openid', 'email', 'profile', 'offline_access', IMAP_SCOPE])
                self.assertRegex(query.pop('state'), r'^outlook-[A-Za-z0-9_-]{43}$')
                self.assertRegex(query.pop('code_challenge'), r'^[A-Za-z0-9_-]{43}$')
                # A public client: the proof is PKCE, and no secret or verifier travels in the address.
                self.assertEqual(query, {'client_id': CLIENT, 'response_type': 'code', 'response_mode': 'fragment',
                                         'redirect_uri': f'http://localhost:{port}/auth/outlook/callback',
                                         'code_challenge_method': 'S256', 'prompt': 'select_account'})
                self.assertEqual(target, f'http://localhost:{port}/auth/outlook/callback')
                # The browser asks for that address without what follows «#»; the panel page reads it there.
                back = self.client.get('/auth/outlook/callback', follow_redirects=False)
                self.assertEqual((back.status_code, back.headers['location']), (303, '/'))
                self.assertFalse(self.status()['conectado'])

                done = self.finish(answer)
                self.assertEqual(done.status_code, 200, done.text)
                link = done.json()
                self.assertEqual((link.pop('resultado'), link.pop('aviso')), ('ok', ''))
                self.assertEqual((link['estado'], link['cuenta'], link['envio'], link['verifica_altas'], link['mensaje']),
                                 ('conectado', ADDRESS, False, True, ''))
                common = self.api('GET', '/api/correo').json()
                self.assertEqual((common['conectado'], common['envio']), (True, False))
                self.assertAlmostEqual(link['vigente_hasta'], time.time() + 3600, delta=30)
                self.assertAlmostEqual(link['conectado_desde'], time.time(), delta=30)
                self.assertEqual(self.status()['outlook'], link)
                redeemed = self.microsoft.token_requests[-1]
                self.assertEqual((redeemed['grant_type'], redeemed['tenant'], redeemed['redirect_uri']),
                                 ('authorization_code', 'common', f'http://localhost:{port}/auth/outlook/callback'))
                self.assertNotIn('client_secret', redeemed)
                self.assertRegex(redeemed['code_verifier'], r'^[A-Za-z0-9_-]{86}$')
                # The mailbox was opened with the token, for reading only, before anything was saved.
                self.assertEqual(self.imap.presented[-1], (ADDRESS, list(self.microsoft.access)[-1]))
                self.assertIn('AUTHENTICATE', self.imap.commands)
                self.assertNotIn('LOGIN', self.imap.commands)
                self.assertEqual(set(self.imap.selected), {'EXAMINE'})
                self.assertEqual(self.smtp.commands, [])
                self.assertEqual(self.api('POST', '/api/correo/outlook/probar').json()['prueba'], 'ok')

                # The answer of Microsoft works once.
                again = self.finish(answer)
                self.assertEqual((again.status_code, again.text), (422, outlook.REASONS['caducado']))
                self.assertEqual(self.status()['outlook'], link)
                self.assert_no_secrets()
                record = json.loads((self.root / 'auth' / 'correo' / 'outlook.json').read_text(encoding='utf-8'))
                self.assertEqual(sorted(record), sorted([*outlook.FIELDS, 'secret']))

                gone = self.api('DELETE', '/api/correo/outlook').json()
                self.assertEqual((gone['estado'], gone['cuenta']), ('lista', ''))
                self.assertFalse(self.status()['conectado'])
                self.assertEqual(sorted(p.name for p in (self.root / 'auth' / 'correo').iterdir()), ['outlook-aplicacion.json'])
                self.assertEqual(self.api('POST', '/api/correo/outlook/probar').status_code, 422)
                self.imap.presented.clear()
                self.imap.commands.clear()
                self.imap.selected.clear()

    def test_answer_must_belong_to_a_request_of_this_panel_that_is_still_open(self):
        self.panel()
        self.api('POST', '/api/correo/outlook/aplicacion', json={'identificador': CLIENT})
        _, _, answer = self.authorize()
        for state in ('', 'inventado', 'outlook-' + 'x' * 43, answer['state'] + 'x', answer['state'][:-1]):
            refused = self.finish({'state': state, 'code': answer['code']})
            self.assertEqual((refused.status_code, refused.text), (422, outlook.REASONS['caducado']), state)
        self.assertFalse(self.status()['conectado'])
        for body in ('[]', '{"estado": 5}', '{"otro": "x"}', json.dumps({'estado': 'x' * 17000}), 'x' * 33000):
            refused = self.client.post('/api/correo/outlook/terminar', content=body,
                                       headers={**self.headers, 'content-type': 'application/json'})
            self.assertEqual(refused.status_code, 422)
            self.assertNotIn('xxxx', refused.text)
        # Ten minutes later the request is closed, even with the right answer.
        with patch.object(outlook.time, 'monotonic', return_value=time.monotonic() + 601):
            self.assertEqual(self.finish(answer).text, outlook.REASONS['caducado'])
        self.assertEqual(self.microsoft.token_requests, [])

        # The person says no at Microsoft: nothing is connected and that request is over.
        self.microsoft.declines = True
        _, _, refusal = self.authorize()
        self.assertEqual(refusal['error'], 'access_denied')
        self.assertEqual(self.finish(refusal).text, outlook.REASONS['consentimiento'])
        self.microsoft.declines = False
        self.assertEqual(self.finish({'state': refusal['state'], 'code': 'CODIGO-FICTICIO-99'}).text,
                         outlook.REASONS['caducado'])
        self.assertEqual(self.microsoft.token_requests, [])

        # Several tries may be open at once; each answer closes only its own, and a good one closes them all.
        _, _, one = self.authorize()
        _, _, two = self.authorize()
        self.assertEqual(self.finish(two).status_code, 200)
        self.assertEqual(self.finish(one).text, outlook.REASONS['caducado'])
        self.assertEqual(self.status()['outlook']['estado'], 'conectado')
        # An answer that arrives in the address instead of after «#» is passed on the same way.
        back = self.client.get('/auth/outlook/callback?state=s1&code=c1&session_state=z', follow_redirects=False)
        self.assertEqual((back.status_code, back.headers['location']), (303, '/#state=s1&code=c1'))
        self.assert_no_secrets()

    def test_code_is_tied_to_its_own_request_and_to_the_registered_address(self):
        self.panel()
        self.api('POST', '/api/correo/outlook/aplicacion', json={'identificador': CLIENT})
        _, _, one = self.authorize()
        _, _, two = self.authorize()
        # The code of one request with the state of another: the proof (PKCE) does not match.
        crossed = self.finish({'state': two['state'], 'code': one['code']})
        self.assertEqual((crossed.status_code, crossed.text), (422, outlook.REASONS['intercambio']))
        self.assertFalse(self.status()['conectado'])
        self.assertFalse((self.root / 'auth' / 'correo' / 'outlook.json').exists())
        # That code was spent by the refused try; a new sign-in is needed.
        self.assertEqual(self.finish(one).text, outlook.REASONS['intercambio'])
        # Microsoft does not return to an address that is not the registered one.
        self.microsoft.redirects.clear()
        self.microsoft.redirects.add('http://localhost/otra/direccion')
        started = self.api('POST', '/api/correo/outlook/conectar', json={}).json()['url']
        self.assertEqual(self.visit(started)[0], 400)
        self.assert_no_secrets()

    def test_token_of_another_account_never_opens_or_replaces_a_mailbox(self):
        self.panel()
        self.api('POST', '/api/correo/outlook/aplicacion', json={'identificador': CLIENT})
        _, _, answer = self.authorize()
        self.assertEqual(self.finish(answer).status_code, 200)
        saved = {path.name: path.read_bytes() for path in (self.root / 'auth' / 'correo').iterdir()}
        # Microsoft names one account and hands the token of another: the mail server refuses it.
        self.microsoft.accounts[OTHER] = {'tid': PERSONAL_TENANT}
        self.microsoft.names = {'email': OTHER, 'preferred_username': OTHER, 'tid': PERSONAL_TENANT}
        _, _, answer = self.authorize()
        refused = self.finish(answer)
        self.assertEqual((refused.status_code, refused.text), (422, outlook.REASONS['buzon']))
        self.assertEqual(self.imap.presented[-1][0], OTHER)
        self.assertEqual({path.name: path.read_bytes() for path in (self.root / 'auth' / 'correo').iterdir()}, saved)
        self.assertEqual(self.status()['outlook']['cuenta'], ADDRESS)
        # Signing in with another account of one's own replaces the connection with that mailbox.
        self.microsoft.names = None
        self.microsoft.signed_in = OTHER
        _, _, answer = self.authorize()
        self.assertEqual(self.finish(answer).json()['cuenta'], OTHER)
        self.assertEqual(buzon.outlook_mailboxes(self.root)[0].address, OTHER)
        # At the mail server a token opens the mailbox it was issued for and no other.
        theirs = next(token for token, owner in self.microsoft.access.items() if owner == OTHER)
        box = buzon.OutlookMailbox(outlook.settings_for(ADDRESS, True), lambda force=False: theirs)
        with self.assertRaises(buzon.LoginRefused) as caught:
            box.open()
        self.assertEqual(str(caught.exception), buzon.OUTLOOK_LOGIN)
        self.assertIsNone(caught.exception.__cause__)
        self.assert_no_secrets(str(caught.exception))

    def test_incomplete_answers_of_microsoft_keep_the_connection_that_worked(self):
        self.panel()
        self.api('POST', '/api/correo/outlook/aplicacion', json={'identificador': CLIENT})
        _, _, answer = self.authorize()
        self.assertEqual(self.finish(answer).status_code, 200)
        saved = (self.root / 'auth' / 'correo' / 'outlook.json').read_bytes()
        cases = [('grants_renewal', False, 'sin-renovacion'), ('granted', [SEND_SCOPE], 'permiso'),
                 ('names', {'name': 'Persona sin correo', 'tid': PERSONAL_TENANT}, 'cuenta'),
                 ('names', {'email': 'no es un correo', 'preferred_username': '+5215500000000'}, 'cuenta'),
                 ('lifetime', 0, 'intercambio'), ('unavailable', True, 'sin-respuesta')]
        for name, value, reason in cases:
            with self.subTest(reason=reason), patch.object(self.microsoft, name, value):
                _, _, answer = self.authorize()
                refused = self.finish(answer)
                self.assertEqual((refused.status_code, refused.text), (422, outlook.REASONS[reason]))
                self.assertEqual((self.root / 'auth' / 'correo' / 'outlook.json').read_bytes(), saved)
        # An identity token meant for another application is not believed.
        with patch.object(self.microsoft, 'client', OTHER_CLIENT):
            forged = self.microsoft.issue(ADDRESS, [IMAP_SCOPE], offline=True, identity=True)
        with patch.object(outlook, '_token_request', return_value=forged):
            _, _, answer = self.authorize()
            self.assertEqual(self.finish(answer).text, outlook.REASONS['intercambio'])
        self.assertEqual((self.root / 'auth' / 'correo' / 'outlook.json').read_bytes(), saved)
        self.assertEqual(self.status()['outlook']['estado'], 'conectado')
        self.assert_no_secrets()

    def test_application_identifier_is_checked_and_not_changed_under_a_connection(self):
        self.panel()
        for bad in ('', 'no-es-un-identificador', CLIENT[:-1], CLIENT + 'a', 'https://entra.microsoft.com/' + CLIENT):
            refused = self.api('POST', '/api/correo/outlook/aplicacion', json={'identificador': bad})
            self.assertEqual((refused.status_code, refused.text), (422, outlook.BAD_CLIENT), bad)
        self.assertEqual(self.api('POST', '/api/correo/outlook/aplicacion', json={'identificador': 5}).status_code, 422)
        self.assertEqual(self.api('POST', '/api/correo/outlook/aplicacion', json={'identificador': CLIENT}).status_code, 200)
        _, _, answer = self.authorize()
        self.assertEqual(self.finish(answer).status_code, 200)
        self.assertEqual(self.api('POST', '/api/correo/outlook/aplicacion', json={'identificador': CLIENT}).status_code, 200)
        changed = self.api('POST', '/api/correo/outlook/aplicacion', json={'identificador': OTHER_CLIENT})
        self.assertEqual((changed.status_code, changed.text), (422, outlook.CHANGE_CLIENT))
        self.assertEqual(self.status()['outlook']['aplicacion']['identificador'], CLIENT)
        self.assertEqual(self.api('DELETE', '/api/correo/outlook').status_code, 200)
        self.assertEqual(self.api('POST', '/api/correo/outlook/aplicacion', json={'identificador': OTHER_CLIENT}).status_code, 200)

    def test_installation_application_from_the_environment_is_fixed_for_the_panel(self):
        with without_outlook_settings(FACTURADOR_OUTLOOK_APLICACION=CLIENT.upper(), FACTURADOR_OUTLOOK_INQUILINO='Consumers'):
            self.microsoft.tenant = 'consumers'
            self.panel()
            link = self.status()['outlook']
            self.assertEqual((link['estado'], link['aplicacion'], link['redireccion']),
                             ('lista', {'configurada': True, 'editable': False, 'identificador': ''}, ''))
            fixed = self.api('POST', '/api/correo/outlook/aplicacion', json={'identificador': OTHER_CLIENT})
            self.assertEqual((fixed.status_code, fixed.text), (422, outlook.FIXED))
            address, _, answer = self.authorize()
            self.assertEqual(urlsplit(address).path, '/consumers/oauth2/v2.0/authorize')
            self.assertEqual(self.finish(answer).json()['estado'], 'conectado')
            self.assertEqual(self.microsoft.token_requests[-1]['tenant'], 'consumers')
            self.expire()
            self.assertEqual(outlook.access_token(self.root), 'ACCESO-FICTICIO-3')
            self.assertEqual(self.renewals()[-1]['tenant'], 'consumers')
        for name, value in (('FACTURADOR_OUTLOOK_APLICACION', 'mi aplicacion'), ('FACTURADOR_OUTLOOK_INQUILINO', 'un/inquilino')):
            with without_outlook_settings(**{'FACTURADOR_OUTLOOK_APLICACION': OTHER_CLIENT, name: value}):
                connection = outlook.OutlookConnection(self.root / name, 8765)
                # A setting that is not what Microsoft gives is ignored, and said once in the log.
                self.assertEqual(connection.client(), None if 'APLICACION' in name else {'client_id': OTHER_CLIENT, 'tenant': 'common'})
                connection.status()
                self.assertEqual(sum(name in record.getMessage() for record in self.records), 1)

    def test_form_for_other_providers_sends_microsoft_mailboxes_to_their_own_option(self):
        self.panel()
        account = {'imap_port': 993, 'user': ADDRESS, 'password': 'clave-de-aplicacion-ficticia'}
        for host in ('outlook.office365.com', 'imap-mail.outlook.com', 'Outlook.Office365.com.', 'imap.hotmail.com'):
            refused = self.api('POST', '/api/correo/cuenta', json={**account, 'imap_host': host})
            self.assertEqual((refused.status_code, refused.text), (422, USE_OUTLOOK), host)
        self.assertEqual(self.imap.commands, [])
        # Microsoft's unnamed report cannot be chosen by hand for another provider's mailbox.
        named = self.api('POST', '/api/correo/cuenta', json={**account, 'imap_host': 'imap.proveedor.example',
                                                             'authority': buzon.MICROSOFT_AUTHORITY})
        self.assertEqual(named.status_code, 422)
        self.assertNotIn('clave-de-aplicacion-ficticia', named.text)


class MailboxTests(Case):
    """Once connected, the mailbox is read and used with the rules of any IMAP one."""

    def setUp(self):
        super().setUp()
        self.store = Store(self.root)

    def ticket(self, name='ticket-1', receipt=RECEIPT):
        source = self.root / 'originales' / (name + '.png')
        source.parent.mkdir(parents=True, exist_ok=True)
        Image.new('RGB', (12, 12), tuple(hashlib.sha256(name.encode()).digest()[:3])).save(source)
        row, _ = self.store.add(str(source), hashlib.sha256(source.read_bytes()).hexdigest())
        self.store.save_receipt(row['id'], receipt, ready=True)
        return row['id']

    def test_mail_servers_of_these_tests_refuse_a_password_as_microsofts_do(self):
        account = outlook.settings_for(ADDRESS, True)
        with self.assertRaises(buzon.LoginRefused) as caught:
            buzon.ImapMailbox(account, UNUSED).open()
        self.assertEqual(str(caught.exception), buzon.LOGIN)
        sender = correo_envio.SmtpSender(account, UNUSED)
        with self.assertRaises(buzon.LoginRefused):
            sender.connect()
        self.assertEqual((self.imap.logins, self.smtp.logins), (0, 0))
        self.assertEqual(self.smtp.commands[:4], ['EHLO', 'STARTTLS', 'EHLO', 'AUTH'])

    def test_search_and_read_through_the_common_interface(self):
        self.imap.add(invoice(('viejo.xml', b'<x/>')), day(10))
        self.imap.add(invoice(('factura.xml', b'<x/>'), ('factura.pdf', b'%PDF-1.4')), day(16))
        self.imap.add(enrollment(MICROSOFT_PASS), day(17))
        self.connect()
        self.assertTrue(buzon.mailbox_connected(self.root))
        self.imap.presented.clear()
        self.imap.commands.clear()
        self.imap.selected.clear()
        rows = buzon.search_all(self.root, since=day(15, 0).timestamp())
        self.assertEqual([(row['ref'], row['received']) for row in rows],
                         [('outlook:7.103', day(17).timestamp()), ('outlook:7.102', day(16).timestamp())])
        self.assertEqual([row['ref'] for row in buzon.search_all(self.root, since=day(15, 0).timestamp(), attachments=True)],
                         ['outlook:7.102'])
        self.assertEqual(len(buzon.search_all(self.root, sender='comercio.example.com', recipient=PROFILE.email)), 3)
        self.assertEqual(buzon.search_all(self.root, text='FACTURAI-NADA'), [])
        message = buzon.read_ref(self.root, 'outlook:7.102')
        parts = buzon.message_parts(message)
        self.assertEqual(parts['headers']['Subject'], 'Tu factura electrónica')
        self.assertEqual([(a['filename'], a['data']) for a in parts['attachments']],
                         [('factura.xml', b'<x/>'), ('factura.pdf', b'%PDF-1.4')])
        with self.assertRaises(ValueError):
            buzon.search_all(self.root, sender='a" OR ALL "')
        # A reference of another kind of mailbox, or of another state of this one, is not read here.
        for ref in ('imap:7.102', 'outlook:8.102', 'gmail:abc'):
            with self.assertRaises(ValueError):
                buzon.read_ref(self.root, ref)
        # Read-only sessions, each opened with the token for this mailbox and never with a password.
        self.assertEqual(set(self.imap.selected), {'EXAMINE'})
        self.assertEqual(set(self.imap.presented), {(ADDRESS, 'ACCESO-FICTICIO-2')})
        self.assertNotIn('LOGIN', self.imap.commands)
        self.assertEqual(self.renewals(), [])
        box = buzon.outlook_mailboxes(self.root)[0]
        with box:
            self.assertEqual((box.provider, box.address, box.authority), ('outlook', ADDRESS, buzon.MICROSOFT_AUTHORITY))
            for mark in MARKS:
                self.assertNotIn(mark, repr(box) + repr(vars(box)))
        self.assert_no_secrets()

    def test_password_mailbox_and_microsoft_mailbox_live_together(self):
        other = FakeImap(user='buzon@proveedor.example', messages=[(invoice(('otro.xml', b'<y/>'), report=''), day(18))])
        self.addCleanup(other.close)
        buzon.save_account(self.root, other.account(), 'clave-app-ficticia')
        self.imap.add(invoice(('factura.xml', b'<x/>')), day(16))
        self.connect()
        self.assertEqual([box.provider for box in buzon.open_mailboxes(self.root)], ['imap', 'outlook'])
        rows = buzon.search_all(self.root, attachments=True)
        self.assertEqual([row['ref'] for row in rows], ['imap:7.101', 'outlook:7.101'])
        names = {row['ref']: buzon.message_parts(buzon.read_ref(self.root, row['ref']))['attachments'][0]['filename']
                 for row in rows}
        self.assertEqual(names, {'imap:7.101': 'otro.xml', 'outlook:7.101': 'factura.xml'})
        self.assertIn('LOGIN', other.commands)
        self.assertNotIn('AUTHENTICATE', other.commands)
        # Removing one leaves the other as it was.
        buzon.remove_account(self.root)
        self.assertEqual([row['ref'] for row in buzon.search_all(self.root, attachments=True)], ['outlook:7.101'])
        buzon.save_account(self.root, other.account(), 'clave-app-ficticia')
        outlook.remove(self.root)
        self.assertEqual([row['ref'] for row in buzon.search_all(self.root, attachments=True)], ['imap:7.101'])
        # A password that can no longer be opened does not hide the Microsoft mailbox.
        self.connect()
        (self.root / 'auth' / 'correo' / 'vault-key.json').unlink()
        self.assertEqual([box.provider for box in buzon.open_mailboxes(self.root)], ['outlook'])

    def test_registration_mail_is_used_only_with_microsofts_own_passing_report(self):
        self.connect()
        self.assertEqual(outlook.account(self.root)['authority'], buzon.MICROSOFT_AUTHORITY)
        started = time.time() - 60
        now = datetime.datetime.now(UTC)

        def lookup(*messages):
            self.imap.messages.clear()
            for message in messages:
                self.imap.add(message, now)
            return registration_email(self.root, PORTAL, PROFILE.email, started, 'activate')

        self.assertEqual(lookup(enrollment(MICROSOFT_PASS)), LINK)
        self.assertEqual(lookup(enrollment(
            f'spf=none (sender IP is 203.0.113.9) smtp.mailfrom=envios.example.net; dkim=pass (signature was verified) '
            f'header.d={HOST};dmarc=bestguesspass action=none header.from={HOST};compauth=pass reason=109')), LINK)
        refused = {
            'falla de autenticación': enrollment(MICROSOFT_FAIL),
            'sin encabezado': enrollment(),
            # What the sender wrote stays below the header Microsoft adds on top.
            'falsificado debajo del de Microsoft': enrollment(MICROSOFT_FAIL, MICROSOFT_PASS),
            'identificador ajeno': enrollment(f'mx.atacante.example; dkim=pass header.d={HOST}', MICROSOFT_PASS),
            'nombre reservado escrito a mano': enrollment(f'{buzon.MICROSOFT_AUTHORITY}; dkim=pass header.d={HOST}'),
            'sin el veredicto de Microsoft': enrollment(f'spf=pass smtp.mailfrom={HOST}; dkim=pass header.d={HOST}'),
            'solo supuesto': enrollment(f'spf=pass (sender IP is 203.0.113.9) smtp.mailfrom={HOST}; dkim=none (message not '
                                        f'signed) header.d=none;dmarc=bestguesspass action=none header.from={HOST};'
                                        'compauth=pass reason=109'),
            'firma de otro dominio': enrollment('spf=pass smtp.mailfrom=ajeno.example.net; dkim=pass header.d=ajeno.example.net;'
                                                f'dmarc=fail action=none header.from={HOST};compauth=fail reason=001'),
            'remitente ajeno': enrollment(MICROSOFT_PASS.replace(HOST, 'ajeno.example.net'), sender='registro@ajeno.example.net'),
        }
        for name, message in refused.items():
            with self.subTest(name):
                self.assertIsNone(lookup(message))
        with self.assertRaises(ValueError):
            lookup(enrollment(MICROSOFT_PASS), enrollment(MICROSOFT_PASS, link=PORTAL + '/cuenta/activar?k=otra'))
        self.assertEqual(set(self.imap.selected), {'EXAMINE'})

    def test_microsofts_report_is_pinned_for_this_mailbox_and_never_learned_by_another(self):
        # Whatever the mailbox holds when it is connected, only Microsoft's own report will count in it.
        named = 'mx.atacante.example; dkim=pass header.d=' + HOST
        for message in (enrollment(named), enrollment(named), enrollment(named)):
            self.imap.add(message, datetime.datetime.now(UTC))
        self.connect()
        self.assertEqual(outlook.account(self.root)['authority'], buzon.MICROSOFT_AUTHORITY)
        self.assertTrue(self.connection().status()['verifica_altas'])
        # Connecting only opens the mailbox: no message is looked at to decide what to trust.
        self.assertEqual(set(self.imap.commands), {'CAPABILITY', 'AUTHENTICATE', 'EXAMINE', 'LOGOUT'})
        self.assertIsNone(registration_email(self.root, PORTAL, PROFILE.email, time.time() - 60, 'activate'))
        # A mailbox with a password never takes Microsoft's unnamed report as its provider's.
        self.imap.messages.clear()
        for _ in range(3):
            self.imap.add(enrollment(MICROSOFT_PASS), day(12))
        self.imap.tokens, self.imap.password = None, 'clave-app-ficticia'
        with buzon.ImapMailbox(self.imap.account(), 'clave-app-ficticia') as box:
            self.assertEqual(box.detect_authority(), '')
            self.assertEqual(buzon.check_account(self.imap.account(), 'clave-app-ficticia'), '')

    def test_invoice_is_found_in_the_mailbox_and_checked_against_ticket_and_profile(self):
        row, _ = self.store.add('fixture', 'factura-por-outlook')
        self.store.save_receipt(row['id'], RECEIPT, ready=True)
        self.store.queue(row['id'], PROFILE)
        self.store.confirm_issued(row['id'])
        self.imap.add(invoice(('ajena.xml', xml_bytes(Rfc='AAA010101AAA', uuid='87654321-4321-4321-8321-BA0987654321'))), day(15))
        self.imap.add(invoice(('Factura_123.xml', xml_bytes()), ('Factura_123.pdf', pdf_bytes())), day(16))
        self.connect()
        self.assertEqual(InvoiceMailRecovery(self.store, lambda: True, lambda: None).round(1000), {row['id']: 'verified'})
        job = self.store.get(row['id'])
        self.assertEqual((job['status'], job['uuid']), ('verified', FISCAL_UUID))
        self.assert_no_secrets(json.dumps(self.store.events(row['id'])))

    def test_request_to_a_merchant_is_sent_with_the_optional_permission(self):
        key = self.ticket()
        self.connect()
        self.assertFalse(correo_envio.can_send(self.root))
        with self.assertRaises(ValueError) as caught:
            MailOutbox(self.store).request(key, MERCHANT, True, PROFILE)
        self.assertEqual(str(caught.exception), NO_SENDER)
        self.assertEqual(self.smtp.commands, [])
        # Sending is asked apart; reading stays as it was.
        outcome = self.connect(send=True)
        self.assertEqual(outcome, {'cuenta': ADDRESS, 'envio': True, 'envio_rechazado': False})
        self.assertEqual(self.microsoft.authorizations[-1]['scope'].split()[-2:], [IMAP_SCOPE, SEND_SCOPE])
        self.assertTrue(correo_envio.can_send(self.root))
        self.assertNotIn('MAIL', self.smtp.commands)  # Checking the outgoing server sends nothing.
        self.smtp.commands.clear()
        self.smtp.presented.clear()
        status = MailOutbox(self.store).request(key, MERCHANT, True, PROFILE)
        self.assertEqual(status['status'], 'sent')
        self.assertEqual([item['to'] for item in self.smtp.received], [[MERCHANT]])
        self.assertEqual(self.smtp.received[0]['from'], ADDRESS)
        sent = parsed(self.smtp.received[0])
        self.assertEqual((sent['From'], sent['To']), (ADDRESS, MERCHANT))
        self.assertIn(status['reference'], sent['Subject'])
        # Encrypted first, then the token after the server's 334 (it does not fit in the AUTH line).
        self.assertEqual(self.smtp.commands[:4], ['EHLO', 'STARTTLS', 'EHLO', 'AUTH'])
        self.assertEqual(self.smtp.presented, [(ADDRESS, 'ACCESO-FICTICIO-4', False)])
        with self.store.connect() as db:
            self.assertEqual(db.execute('SELECT channel FROM mail_requests').fetchone()['channel'], 'outlook')
        for mark in MARKS:
            self.assertNotIn(mark.encode(), self.smtp.received[0]['data'])
        self.assert_no_secrets(json.dumps(self.store.events(key)))
        # Connecting again keeps the permission that was already given.
        self.connect()
        self.assertTrue(correo_envio.can_send(self.root))
        self.assertIn(SEND_SCOPE, self.microsoft.authorizations[-1]['scope'].split())

    def test_other_senders_keep_their_turn_before_outlook(self):
        self.connect(send=True)
        self.assertIsInstance(correo_envio.default_sender(self.root), correo_envio.OutlookSender)
        (self.root / 'gmail_token.json').write_text(json.dumps({'token': 'x', 'scope': 'a https://www.googleapis.com/auth/gmail.send'}))
        self.assertIsInstance(correo_envio.default_sender(self.root), correo_envio.GmailSender)
        buzon.save_account(self.root, {'imap_host': '127.0.0.1', 'imap_port': 1, 'user': 'buzon@proveedor.example',
                                       'address': 'buzon@proveedor.example', 'smtp_host': '127.0.0.1', 'smtp_port': 2,
                                       'authority': ''}, 'clave-app-ficticia')
        self.assertEqual(type(correo_envio.default_sender(self.root)), correo_envio.SmtpSender)

    def test_outgoing_server_that_refuses_leaves_a_mailbox_that_only_reads(self):
        self.smtp.tokens = {}
        outcome = self.connect(send=True)
        self.assertEqual(outcome, {'cuenta': ADDRESS, 'envio': False, 'envio_rechazado': True})
        self.assertEqual(self.smtp.commands[-2:], ['AUTH', 'QUIT'])
        self.assertTrue(buzon.mailbox_connected(self.root))
        self.assertFalse(correo_envio.can_send(self.root))
        self.assertEqual(self.connection().status()['envio'], False)
        self.assertIn('no se activó', SEND_NOT_ACTIVE)
        # When the outgoing server takes the token, asking again turns sending on.
        self.smtp.tokens = self.microsoft.access
        self.assertTrue(self.connect(send=True)['envio'])
        # A work account goes to the server Microsoft documents for Microsoft 365.
        work = FakeSmtp('starttls', tokens=self.microsoft.access)
        self.addCleanup(work.close)
        self.microsoft.accounts[OTHER] = {'tid': WORK_TENANT}
        self.microsoft.signed_in = OTHER
        self.imap.user = OTHER
        with patch.dict(outlook.SMTP_SERVERS, {'trabajo': ('127.0.0.1', work.port)}):
            self.smtp.commands.clear()
            self.assertEqual(self.connect(send=True), {'cuenta': OTHER, 'envio': True, 'envio_rechazado': False})
            self.assertEqual((work.logins, self.smtp.commands), (1, []))
            self.assertEqual(outlook.mail_account(self.root)['smtp_port'], work.port)


class RenewalTests(Case):
    def test_access_is_renewed_before_it_ends_and_each_new_renewal_token_is_kept(self):
        self.connect(send=True)
        self.assertEqual(outlook.access_token(self.root), 'ACCESO-FICTICIO-2')
        self.assertEqual(self.renewals(), [])
        self.expire(60)  # About to end: it is renewed now, not in the middle of a session.
        self.imap.add(invoice(('factura.xml', b'<x/>')), day(16))
        self.assertEqual(len(buzon.search_all(self.root, attachments=True)), 1)
        first, = self.renewals()
        self.assertEqual((first['client_id'], first['refresh_token'], first['tenant']), (CLIENT, 'RENOVACION-FICTICIA-2', 'common'))
        self.assertEqual(first['scope'].split(), [IMAP_SCOPE, SEND_SCOPE, 'openid', 'profile', 'offline_access'])
        self.assertNotIn('client_secret', first)
        self.assertEqual(self.imap.presented[-1], (ADDRESS, 'ACCESO-FICTICIO-3'))
        self.assertAlmostEqual(self.connection().status()['vigente_hasta'], time.time() + 3600, delta=30)
        self.assertEqual(outlook.access_token(self.root), 'ACCESO-FICTICIO-3')
        self.assertEqual(len(self.renewals()), 1)
        # The stand-in refuses a renewal token that was already used: the new one was saved.
        self.expire()
        self.assertEqual(outlook.access_token(self.root), 'ACCESO-FICTICIO-4')
        self.assertEqual([form['refresh_token'] for form in self.renewals()], ['RENOVACION-FICTICIA-2', 'RENOVACION-FICTICIA-3'])
        self.assertTrue(correo_envio.can_send(self.root))
        self.assertEqual(self.connection().status()['estado'], 'conectado')
        self.assert_no_secrets()

    def test_renewal_token_that_microsoft_does_not_replace_is_kept(self):
        self.connect()
        self.microsoft.rotates = self.microsoft.single_use_renewals = False
        for expected in ('ACCESO-FICTICIO-3', 'ACCESO-FICTICIO-4'):
            self.expire()
            self.assertEqual(outlook.access_token(self.root), expected)
        self.assertEqual([form['refresh_token'] for form in self.renewals()], ['RENOVACION-FICTICIA-2'] * 2)

    def test_two_uses_at_once_renew_only_once(self):
        import threading
        self.connect()
        self.expire()
        results = []
        threads = [threading.Thread(target=lambda: results.append(outlook.access_token(self.root))) for _ in range(6)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join(20)
        self.assertEqual(results, ['ACCESO-FICTICIO-3'] * 6)
        self.assertEqual(len(self.renewals()), 1)

    def test_permission_taken_away_asks_to_connect_again_and_says_it_once(self):
        store = Store(self.root)
        notices = Avisos(store)
        watcher = VigilanteAvisos(store, notices)
        self.assertIsNone(watcher.revisar_buzon())  # Nothing connected: nothing to watch.
        self.connect(send=True)
        self.assertIsNone(watcher.revisar_buzon())
        self.microsoft.revoke(ADDRESS)
        self.assertEqual(outlook.access_token(self.root), 'ACCESO-FICTICIO-2')  # Not known until it is needed.
        self.imap.commands.clear()
        with self.assertRaises(ValueError) as caught:
            buzon.search_all(self.root, limit=1)
        self.assertEqual(str(caught.exception), outlook.RECONNECT)
        self.assertEqual(len(self.renewals()), 1)
        status = self.connection().status()
        self.assertEqual((status['estado'], status['cuenta'], status['envio'], status['mensaje'], status['vigente_hasta']),
                         ('reconectar', ADDRESS, False, outlook.RECONNECT, None))
        self.assertTrue(outlook.needs_reconnect(self.root))
        self.assertFalse(buzon.mailbox_connected(self.root))
        self.assertFalse(correo_envio.can_send(self.root))
        self.assertEqual(buzon.open_mailboxes(self.root), [])
        self.assertNotIn('secret', json.loads((self.root / 'auth' / 'correo' / 'outlook.json').read_text(encoding='utf-8')))
        # Microsoft is not asked again with a permission it already refused.
        for use in (lambda: outlook.access_token(self.root), lambda: buzon.check_outlook_account(self.root),
                    lambda: correo_envio.default_sender(self.root)):
            with self.assertRaises(ValueError):
                use()
        self.assertEqual(len(self.renewals()), 1)
        with self.assertRaises(ValueError) as caught:
            buzon.check_outlook_account(self.root)
        self.assertEqual(str(caught.exception), outlook.RECONNECT)

        notice = watcher.revisar_buzon()
        self.assertEqual((notice['tipo'], notice['motivo'], notice['liga']), ('te_necesito', BUZON_RECONECTAR, '/#ajustes'))
        self.assertIsNone(watcher.revisar_buzon())
        self.assertIsNone(VigilanteAvisos(store, notices).revisar_buzon())  # Nor after a restart.
        self.assertEqual([row['motivo'] for row in notices.lista()['avisos']], [BUZON_RECONECTAR])
        self.assertNotIn(ADDRESS, json.dumps(notices.lista()))

        # Connecting again brings everything back, with the permission to send it had.
        self.microsoft.revoked.clear()
        self.assertEqual(self.connect(), {'cuenta': ADDRESS, 'envio': True, 'envio_rechazado': False})
        self.assertEqual(self.connection().status()['estado'], 'conectado')
        self.assertTrue(buzon.mailbox_connected(self.root))
        self.assertIsNone(watcher.revisar_buzon())
        self.microsoft.revoke(ADDRESS)
        self.expire()
        with self.assertRaises(outlook.ReconnectNeeded):
            outlook.access_token(self.root)
        self.assertEqual(watcher.revisar_buzon()['clave'], 'outlook:reconectar:2')
        self.assert_no_secrets(json.dumps(notices.lista()))

    def test_watcher_running_in_the_background_tells_about_the_mailbox_by_itself(self):
        store = Store(self.root)
        notices = Avisos(store)
        self.connect()

        def mark():
            with store.connect() as db:
                row = db.execute("SELECT valor FROM avisos_marcas WHERE clave='outlook'").fetchone()
            return row['valor'] if row else None

        async def scenario():
            watcher = VigilanteAvisos(store, notices, tick=0.05)
            watcher.start()
            try:
                for _ in range(100):
                    if mark() == 'bien':
                        break
                    await asyncio.sleep(0.05)
                self.microsoft.revoke(ADDRESS)
                self.expire()
                with self.assertRaises(outlook.ReconnectNeeded):
                    await asyncio.to_thread(outlook.access_token, self.root)
                for _ in range(100):
                    if notices.lista()['avisos']:
                        break
                    await asyncio.sleep(0.05)
            finally:
                await watcher.close()

        asyncio.run(scenario())
        self.assertEqual([(row['titulo'], row['motivo'], row['liga']) for row in notices.lista()['avisos']],
                         [('Te necesito', BUZON_RECONECTAR, '/#ajustes')])
        self.assertEqual(mark(), 'reconectar')

    def test_token_refused_before_its_time_is_renewed_once_and_not_over_and_over(self):
        self.imap.add(invoice(('factura.xml', b'<x/>')), day(16))
        self.connect()
        self.microsoft.expire_access(ADDRESS)  # The mail server stops taking it although its time is not over.
        self.imap.presented.clear()
        self.assertEqual(len(buzon.search_all(self.root, attachments=True)), 1)
        self.assertEqual(self.imap.presented, [(ADDRESS, 'ACCESO-FICTICIO-2'), (ADDRESS, 'ACCESO-FICTICIO-3')])
        self.assertEqual(len(self.renewals()), 1)
        # A mailbox that keeps refusing (IMAP turned off, for instance) does not make Microsoft be asked each time.
        self.imap.tokens = {}
        for _ in range(3):
            with self.assertRaises(ValueError) as caught:
                buzon.search_all(self.root, limit=1)
            self.assertEqual(str(caught.exception), buzon.OUTLOOK_LOGIN)
        self.assertEqual(len(self.renewals()), 1)
        self.assertEqual(self.connection().status()['estado'], 'conectado')
        with patch.object(outlook.time, 'time', return_value=time.time() + outlook.FORCE_EVERY + 1):
            with self.assertRaises(ValueError):
                buzon.search_all(self.root, limit=1)
        self.assertEqual(len(self.renewals()), 2)

    def test_microsoft_out_of_reach_changes_nothing(self):
        self.connect()
        saved = (self.root / 'auth' / 'correo' / 'outlook.json').read_bytes()
        self.microsoft.unavailable = True
        self.expire(60)
        self.assertEqual(outlook.access_token(self.root), 'ACCESO-FICTICIO-2')  # Still good for a moment.
        self.expire()
        with self.assertRaises(ValueError) as caught:
            outlook.access_token(self.root)
        self.assertEqual(str(caught.exception), outlook.REASONS['sin-respuesta'])
        self.assertNotIsInstance(caught.exception, outlook.ReconnectNeeded)
        with patch.object(outlook, 'TOKEN', 'http://127.0.0.1:9/{tenant}/token'), self.assertRaises(ValueError) as caught:
            outlook.access_token(self.root)
        self.assertEqual(str(caught.exception), outlook.REASONS['sin-respuesta'])
        self.assertEqual(self.connection().status()['estado'], 'conectado')
        self.assertFalse(outlook.needs_reconnect(self.root))
        self.microsoft.unavailable = False
        self.assertEqual(outlook.access_token(self.root), 'ACCESO-FICTICIO-3')
        self.assertNotEqual((self.root / 'auth' / 'correo' / 'outlook.json').read_bytes(), saved)
        # An application Microsoft no longer knows is a matter for whoever installed it, not for the person.
        self.expire()
        with patch.object(self.microsoft, 'client', OTHER_CLIENT), self.assertRaises(ValueError) as caught:
            outlook.access_token(self.root)
        self.assertEqual(str(caught.exception), outlook.REASONS['aplicacion'])
        self.assertEqual(self.connection().status()['estado'], 'conectado')
        self.assert_no_secrets()

    def test_record_changed_by_hand_counts_as_no_connection_and_breaks_nothing(self):
        self.connect()
        path = self.root / 'auth' / 'correo' / 'outlook.json'
        good = json.loads(path.read_text(encoding='utf-8'))
        for change in ({'expires_at': 'mañana'}, {'connected_at': None}, {'address': 'no es un correo'}, {'scope': 5},
                       {'secret': ['x']}, {'state': 1}, {'expires_at': True}):
            with self.subTest(change=change):
                path.write_text(json.dumps({**good, **change}), encoding='utf-8')
                self.assertEqual(self.connection().status()['estado'], 'lista')
                self.assertFalse(outlook.connected(self.root) or outlook.needs_reconnect(self.root))
                self.assertEqual(buzon.open_mailboxes(self.root), [])
                with self.assertRaises(ValueError) as caught:
                    outlook.access_token(self.root)
                self.assertEqual(str(caught.exception), outlook.NOT_CONNECTED)
        path.write_text('esto no es JSON', encoding='utf-8')
        self.assertEqual(self.connection().status()['estado'], 'lista')
        path.write_text(json.dumps({**good, 'secret': 'gAAAAA-esto-no-es-lo-cifrado'}), encoding='utf-8')
        with self.assertRaises(ValueError) as caught:
            outlook.access_token(self.root)
        self.assertEqual(str(caught.exception), outlook.UNREADABLE)
        self.assertEqual(self.connect()['cuenta'], ADDRESS)  # Connecting again puts it right.
        self.assertEqual(outlook.access_token(self.root), 'ACCESO-FICTICIO-4')

    def test_saved_connection_is_encrypted_and_useless_without_its_key(self):
        self.connect()
        folder = self.root / 'auth' / 'correo'
        self.assertEqual(sorted(p.name for p in folder.iterdir()), ['outlook-aplicacion.json', 'outlook-key.json', 'outlook.json'])
        self.assert_no_secrets()
        (folder / 'outlook-key.json').unlink()
        with self.assertRaises(ValueError) as caught:
            outlook.access_token(self.root)
        self.assertEqual(str(caught.exception), outlook.UNREADABLE)
        outlook.remove(self.root)
        self.assertFalse(outlook.connected(self.root))
        with self.assertRaises(ValueError) as caught:
            outlook.access_token(self.root)
        self.assertEqual(str(caught.exception), outlook.NOT_CONNECTED)
        # With a master key the key of the vault is sealed for the owner of the folder.
        use_master_key(self)
        self.connect()
        self.assertIn(secretos.MARK, json.loads((folder / 'outlook-key.json').read_text(encoding='utf-8')))
        self.assertEqual(outlook.access_token(self.root), 'ACCESO-FICTICIO-4')
        secretos.configure(None)
        with self.assertRaises(secretos.SealedSecrets):
            outlook.access_token(self.root)
        self.assert_no_secrets()


class WebRegistrationTests(Case):
    """An installation that registered its application as a web one sends its secret, from the environment only."""
    SECRET = 'SECRETO-FICTICIO-de-la-aplicacion~1'

    def test_secret_travels_only_to_microsoft_and_is_never_shown_or_saved(self):
        self.microsoft.secret = self.SECRET
        with without_outlook_settings(FACTURADOR_OUTLOOK_APLICACION=CLIENT, FACTURADOR_OUTLOOK_SECRETO=self.SECRET):
            connection = self.connection()
            self.assertEqual(self.connect(connection=connection)['cuenta'], ADDRESS)
            self.assertEqual(self.microsoft.token_requests[-1]['client_secret'], self.SECRET)
            self.expire()
            self.assertEqual(outlook.access_token(self.root), 'ACCESO-FICTICIO-3')
            self.assertEqual(self.renewals()[-1]['client_secret'], self.SECRET)
            self.assertNotIn(self.SECRET, self.microsoft.authorizations[-1].values())
            shown = json.dumps(connection.status()) + repr(connection.client()) + repr(outlook.account(self.root))
        self.assertNotIn(self.SECRET, shown + self.logged())
        for path in self.root.rglob('*'):
            if path.is_file():
                self.assertNotIn(self.SECRET.encode(), path.read_bytes(), path.name)
        # A secret for another application, or one pasted in the panel, is never sent along.
        self.expire()
        with without_outlook_settings(FACTURADOR_OUTLOOK_APLICACION=OTHER_CLIENT, FACTURADOR_OUTLOOK_SECRETO=self.SECRET):
            with self.assertRaises(ValueError) as caught:
                outlook.access_token(self.root)
        self.assertNotIn('client_secret', self.renewals()[-1])
        self.assertEqual(str(caught.exception), outlook.REASONS['aplicacion'])
        self.assert_no_secrets()

    def test_public_application_sends_no_secret_even_if_one_is_lying_around(self):
        with without_outlook_settings(FACTURADOR_OUTLOOK_SECRETO=self.SECRET):
            self.connect()
            self.expire()
            outlook.access_token(self.root)
        self.assertFalse(any('client_secret' in form for form in self.microsoft.token_requests))


class EntranceTests(Case):
    """Behind the entrance for several customers: one application for the installation, one mailbox per person."""

    def setUp(self):
        super().setUp()
        use_master_key(self)
        self.accounts = make_accounts(self.root)
        self.ana_account = self.accounts.create_user('ana', PASSWORD)
        self.beto_account = self.accounts.create_user('beto', PASSWORD)
        self.microsoft.redirects.clear()
        self.microsoft.redirects.add(ORIGIN + '/auth/outlook/callback')

    def gate(self):
        entrance = make_entrance(self.root, accounts=self.accounts)
        self.client = self.enterContext(TestClient(entrance, base_url=ORIGIN))
        return entrance

    def visitor(self, name):
        person = Visitor(self.client, name)
        self.assertEqual(person.login().status_code, 200)
        return person

    def call(self, person, method, path, **options):
        response = person.panel(method, path, **options)
        self.replies.append(response.text)
        return response

    def test_without_an_application_of_the_installation_nothing_is_offered(self):
        self.gate()
        ana = self.visitor('ana')
        link = self.call(ana, 'GET', '/api/correo/outlook').json()
        self.assertEqual((link['estado'], link['aplicacion'], link['redireccion']),
                         ('no_disponible', {'configurada': False, 'editable': False, 'identificador': ''}, ''))
        for method, path, body in (('POST', '/api/correo/outlook/aplicacion', {'identificador': CLIENT}),
                                   ('POST', '/api/correo/outlook/conectar', {})):
            refused = self.call(ana, method, path, json=body)
            self.assertEqual((refused.status_code, refused.text), (422, outlook.NOT_OFFERED))
        self.assertFalse(any((self.root / 'usuarios').rglob('outlook*')))

    def test_each_person_connects_their_own_mailbox_with_the_installation_application(self):
        with without_outlook_settings(FACTURADOR_OUTLOOK_APLICACION=CLIENT):
            self.gate()
            ana, beto = self.visitor('ana'), self.visitor('beto')
            link = self.call(ana, 'GET', '/api/correo/outlook').json()
            # Nothing to register or to type: the person only presses Connect.
            self.assertEqual((link['estado'], link['aplicacion'], link['redireccion']),
                             ('lista', {'configurada': True, 'editable': False, 'identificador': ''}, ''))
            self.assertEqual(self.call(ana, 'POST', '/api/correo/outlook/aplicacion', json={'identificador': OTHER_CLIENT}).text,
                             outlook.NOT_OFFERED)
            started = self.call(ana, 'POST', '/api/correo/outlook/conectar', json={})
            address = started.json()['url']
            query = {key: values[0] for key, values in parse_qs(urlsplit(address).query).items()}
            self.assertEqual((query['redirect_uri'], query['response_mode'], query['client_id']),
                             (ORIGIN + '/auth/outlook/callback', 'fragment', CLIENT))
            target, _, fragment = self.visit(address)[1].partition('#')
            answer = dict(parse_qsl(fragment))
            self.assertEqual(target, ORIGIN + '/auth/outlook/callback')
            # Coming from Microsoft the browser does not send the session cookie (SameSite=Strict): the
            # entrance sends it to the sign-in page, which keeps what follows «#» and goes on to the panel.
            arriving = self.client.get('/auth/outlook/callback', headers={'accept': 'text/html'}, follow_redirects=False)
            self.assertEqual((arriving.status_code, arriving.headers['location']), (303, '/entrar'))
            self.assertNotIn('set-cookie', arriving.headers)
            self.assertEqual(ana.get('/acceso/yo').status_code, 200)  # The session is still open.
            with_session = ana.get('/auth/outlook/callback', headers={'accept': 'text/html'})
            self.assertEqual((with_session.status_code, with_session.headers['location']), (303, '/'))
            finish = {'estado': answer['state'], 'codigo': answer['code']}
            # The answer only works in the panel of the person who asked for it.
            self.assertEqual(self.call(beto, 'POST', '/api/correo/outlook/terminar', json=finish).text, outlook.REASONS['caducado'])
            self.assertEqual(self.client.post('/api/correo/outlook/terminar', json=finish, headers={'origin': ORIGIN}).status_code, 401)
            done = self.call(ana, 'POST', '/api/correo/outlook/terminar', json=finish)
            self.assertEqual(done.status_code, 200, done.text)
            self.assertEqual((done.json()['estado'], done.json()['cuenta']), ('conectado', ADDRESS))
            self.assertTrue(self.call(ana, 'GET', '/api/correo').json()['conectado'])
            self.assertEqual(self.microsoft.token_requests[-1]['redirect_uri'], ORIGIN + '/auth/outlook/callback')
            self.assertNotIn('client_secret', self.microsoft.token_requests[-1])
            self.assertFalse(self.call(beto, 'GET', '/api/correo').json()['conectado'])
            ana_root, beto_root = (self.root / 'usuarios' / account['id'] for account in (self.ana_account, self.beto_account))
            self.assertTrue(outlook.connected(ana_root))
            self.assertFalse((beto_root / 'auth' / 'correo' / 'outlook.json').exists())
            # Sealed with the master key for that customer alone.
            key = ana_root / 'auth' / 'correo' / 'outlook-key.json'
            self.assertIn(secretos.MARK, json.loads(key.read_text(encoding='utf-8')))
            (beto_root / 'auth' / 'correo').mkdir(parents=True, exist_ok=True)
            for name in ('outlook.json', 'outlook-key.json'):
                (beto_root / 'auth' / 'correo' / name).write_bytes((ana_root / 'auth' / 'correo' / name).read_bytes())
            with self.assertRaises(secretos.SealedSecrets):
                outlook.access_token(beto_root)
            self.assertEqual(outlook.access_token(ana_root), 'ACCESO-FICTICIO-2')
            self.assert_no_secrets()


if __name__ == '__main__':
    unittest.main()
