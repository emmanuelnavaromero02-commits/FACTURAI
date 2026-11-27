"""Gmail with one Google client for the whole installation (FACTURADOR_GMAIL_CLIENTE): the person only connects.

Everything runs against a stand-in for Google on this machine. In the mode for several customers the
real entrance stands in front of the real application. Nothing here talks to Google or to a mailbox.
"""
import json
import logging
import tempfile
import time
import unittest
from pathlib import Path
from unittest.mock import patch
from urllib.parse import parse_qs, urlsplit

from dotenv import dotenv_values
from fastapi.testclient import TestClient

from acceso_dobles import ORIGIN, PASSWORD, Visitor, clean_env, make_accounts, make_entrance, use_master_key
from facturador import entrada, gmail, secretos
from facturador.app import create_app
from facturador.auth import read_json, write_private
from google_dobles import (ADDRESS, DESKTOP_CLIENT, MARKS, OTHER_CLIENT, SCOPE, SECRET_MARK, SEND_SCOPE, WEB_CLIENT,
                           FakeGoogle, visit)
from test_entrada import WEB, NoInlineCode

RETURN = '/auth/gmail/callback'
NOT_OFFERED = 'Conecta tu correo en Ajustes con tu dirección y una contraseña de aplicación.'


class Later:
    """The clock of the Gmail module alone, moved forward; nothing else in the process notices."""

    def __init__(self, seconds):
        self.seconds = seconds

    def monotonic(self):
        return time.monotonic() + self.seconds

    def time(self):
        return time.time()


class Case(unittest.TestCase):
    """A data folder, a folder apart for what the owner of the installation keeps, and Google's stand-in."""

    def setUp(self):
        folder = tempfile.TemporaryDirectory(ignore_cleanup_errors=True)
        self.addCleanup(folder.cleanup)
        self.root = Path(folder.name) / 'datos'
        self.apart = Path(folder.name) / 'fuera-de-los-datos'
        self.root.mkdir()
        self.apart.mkdir()
        self.google = FakeGoogle()
        self.addCleanup(self.google.close)
        for name in ('authorize', 'token', 'profile'):
            self.enterContext(patch.object(gmail, name.upper(), getattr(self.google, name)))
        # No setting of this machine takes part: each test says which ones it runs with.
        self.enterContext(clean_env())
        gmail._READ.clear()
        gmail._WARNED.clear()
        self.replies, self.records = [], []
        handler = logging.Handler(logging.DEBUG)
        handler.emit = self.records.append
        log = logging.getLogger()
        self.addCleanup(log.setLevel, log.level)
        self.addCleanup(log.removeHandler, handler)
        log.addHandler(handler)
        log.setLevel(logging.DEBUG)

    def client_file(self, kind, *redirects, client=None, name='cliente-google.json') -> str:
        """The JSON its owner downloads from Google Cloud, saved away from the data folder."""
        path = self.apart / name
        path.write_bytes(self.google.register(client or (WEB_CLIENT if kind == 'web' else DESKTOP_CLIENT), kind, *redirects))
        return str(path)

    def at_google(self, address):
        """The person's browser at Google: it comes back with the address Google sends it to."""
        status, location = visit(self.google.url + '/o/oauth2/v2/auth?' + urlsplit(address).query)
        self.assertEqual(status, 302)
        return location

    @staticmethod
    def answer(location):
        """What the return address carries, as the page that receives it passes it on."""
        values = {key: found[0] for key, found in parse_qs(urlsplit(location).query).items()}
        return {'estado': values.get('state', ''), 'codigo': values.get('code', ''), 'error': values.get('error', '')}

    def expire(self, root, sealed=False):
        """The saved access token is over; only a renewal gives another."""
        path = root / 'gmail_token.json'
        if sealed:
            record = secretos.read_json(path, purpose=secretos.GMAIL_TOKEN)
            secretos.write_json(path, {**record, 'expires_at': time.time() - 1}, purpose=secretos.GMAIL_TOKEN)
        else:
            write_private(path, {**read_json(path), 'expires_at': time.time() - 1})

    # ------------------------------------------------------------ nothing secret is left anywhere

    def logged(self):
        lines = []
        for record in self.records:
            try:
                line = record.getMessage()
            except Exception:
                line = str(record.msg) + repr(record.args)
            # The test's own browser notes the address it visits; that is not a record of the program,
            # which runs without an access log so that the answers of Google are never written down.
            if record.name == 'httpx' and RETURN + '?' in line:
                continue
            lines.append(line)
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

    def assert_nothing_leaked(self, *texts, kept_in_data=()):
        """The client's secret, the codes and the tokens: not in the data folder, the logs or any answer of the API."""
        for path in self.root.rglob('*'):
            if path.is_file():
                for mark in MARKS:
                    if mark not in kept_in_data:
                        self.assertNotIn(mark.encode(), self.content(path), f'{mark} en {path.name}')
        everything = '\n'.join([*self.replies, self.logged(), *texts])
        for mark in MARKS:
            self.assertNotIn(mark, everything)


class LocalTests(Case):
    """One person on their own computer: Google returns to 127.0.0.1 exactly as before."""

    def panel(self, port=8765):
        self.origin = f'http://127.0.0.1:{port}'
        self.client = self.enterContext(TestClient(create_app(self.root, port=port), base_url=self.origin))
        self.headers = {'Origin': self.origin, 'X-App-Token': self.client.get('/api/state').json()['token']}

    def api(self, method, path, **options):
        response = self.client.request(method, path, headers=self.headers, **options)
        self.replies.append(response.text)
        return response

    def state(self):
        return self.api('GET', '/api/state').json()

    def start(self, **body):
        started = self.api('POST', '/api/gmail/connect', **({'json': body} if body else {}))
        self.assertEqual(started.status_code, 200, started.text)
        address = started.json()['url']
        return address, {key: values[0] for key, values in parse_qs(urlsplit(address).query).items()}

    def come_back(self, location):
        """The browser arrives at the panel with Google's answer in the address."""
        parts = urlsplit(location)
        self.assertEqual((f'{parts.scheme}://{parts.netloc}', parts.path), (self.origin, RETURN))
        response = self.client.get(parts.path + '?' + parts.query, follow_redirects=False)
        self.assertEqual(response.status_code, 303)
        return response.headers['location']

    def connect(self, **body):
        address, _ = self.start(**body)
        return self.come_back(self.at_google(address))

    def upload(self, raw):
        return self.api('POST', '/api/gmail/client', files={'file': ('cliente.json', raw, 'application/json')})

    def test_person_only_presses_connect_and_the_client_is_never_kept_with_the_data(self):
        with clean_env(FACTURADOR_GMAIL_CLIENTE=self.client_file('installed')):
            self.panel()
            state = self.state()
            self.assertEqual((state['gmail'], state['gmail_setup']), ('configured', {'mode': 'shared', 'notice': ''}))
            # Nothing to import: the installation fixes the client.
            refused = self.upload(self.google.register(OTHER_CLIENT, 'installed'))
            self.assertEqual((refused.status_code, refused.text), (422, gmail.FIXED))
            self.assertFalse((self.root / 'google_client.json').exists())

            address, asked = self.start()
            self.assertEqual((asked['client_id'], asked['redirect_uri'], asked['response_type'], asked['scope']),
                             (DESKTOP_CLIENT, self.origin + RETURN, 'code', SCOPE))
            self.assertEqual((asked['access_type'], asked['code_challenge_method']), ('offline', 'S256'))
            self.assertTrue(asked['state'].startswith('gmail-'))
            self.assertNotIn('client_secret', asked)
            self.assertNotIn('response_mode', asked)
            location = self.at_google(address)
            self.assertIn('conexion=gmail-ok', self.come_back(location))
            self.assertIn('motivo=caducado', self.come_back(location))  # The same answer does not work twice.
            self.assertEqual(self.state()['gmail'], 'connected')
            exchange = self.google.token_requests[-1]
            self.assertEqual((exchange['client_id'], exchange['redirect_uri']), (DESKTOP_CLIENT, self.origin + RETURN))
            self.assertTrue(exchange['client_secret'].startswith(SECRET_MARK))
            record = read_json(self.root / 'gmail_token.json')
            self.assertEqual(sorted(record), ['client_id', 'expires_at', 'refresh_token', 'scope', 'token'])
            self.assertEqual(record['client_id'], DESKTOP_CLIENT)
            self.assertFalse((self.root / 'google_client.json').exists())
            # In this mode the tokens stay in the person's own folder as before; the client's secret never does.
            self.assert_nothing_leaked(kept_in_data=('ACCESO-FICTICIO', 'RENOVACION-FICTICIA'))

            # The permission is renewed with the client of the installation.
            self.expire(self.root)
            renewed = gmail.access_token(self.root)
            self.assertEqual(self.google.access[renewed], ADDRESS)
            renewal = self.google.token_requests[-1]
            self.assertEqual((renewal['grant_type'], renewal['client_id']), ('refresh_token', DESKTOP_CLIENT))
            kept = read_json(self.root / 'gmail_token.json')
            self.assertEqual((kept['client_id'], kept['refresh_token']), (DESKTOP_CLIENT, record['refresh_token']))

            # Sending is asked apart, only when the person chooses it.
            self.assertFalse(gmail.can_send(self.root))
            address, asked = self.start(send=True)
            self.assertEqual(asked['scope'].split(), [SCOPE, SEND_SCOPE])
            self.assertIn('conexion=gmail-ok', self.come_back(self.at_google(address)))
            self.assertTrue(gmail.can_send(self.root))

            self.assertEqual(self.api('POST', '/api/gmail/disconnect').json(), 'configured')
            self.assertFalse((self.root / 'gmail_token.json').exists())
            self.assertEqual(self.state()['gmail_setup']['mode'], 'shared')
        self.assert_nothing_leaked()  # Disconnected, not even the tokens are left.

    def test_web_client_works_on_the_same_computer_only_at_its_registered_address(self):
        path = self.client_file('web', 'http://127.0.0.1:18765' + RETURN)
        with clean_env(FACTURADOR_GMAIL_CLIENTE=path):
            self.panel(18765)
            self.assertEqual(self.state()['gmail_setup']['mode'], 'shared')
            address, asked = self.start()
            self.assertEqual((asked['client_id'], asked['redirect_uri']), (WEB_CLIENT, 'http://127.0.0.1:18765' + RETURN))
            self.assertIn('conexion=gmail-ok', self.come_back(self.at_google(address)))
            self.assertEqual(read_json(self.root / 'gmail_token.json')['client_id'], WEB_CLIENT)
            # From another port Google does not know the address and never comes back.
            other = gmail.GmailConnection(self.root / 'otra', 8765)
            status, location = visit(self.google.url + '/o/oauth2/v2/auth?' + urlsplit(other.start()).query)
            self.assertEqual((status, location), (400, ''))
        self.assert_nothing_leaked(kept_in_data=('ACCESO-FICTICIO', 'RENOVACION-FICTICIA'))

    def test_answer_handed_over_by_the_panel_connects_too_and_a_refused_one_is_never_repeated(self):
        with clean_env(FACTURADOR_GMAIL_CLIENTE=self.client_file('installed')):
            self.panel()
            address, _ = self.start()
            answer = self.answer(self.at_google(address))
            for body in (b'{"estado": "CODIGO-FICTICIO-de-otro", "codigo": 7}', b'CODIGO-FICTICIO sin forma',
                         json.dumps({**answer, 'otro': 'CODIGO-FICTICIO-extra'}).encode(), b'["CODIGO-FICTICIO"]'):
                refused = self.api('POST', '/api/gmail/terminar', content=body)
                self.assertEqual((refused.status_code, refused.text),
                                 (422, 'La solicitud no se entendió. Vuelve a intentarlo desde Ajustes.'))
            for headers in ({'Origin': self.origin}, {**self.headers, 'Origin': 'https://ajeno.example.com'}):
                foreign = self.client.post('/api/gmail/terminar', json=answer, headers=headers)
                self.assertEqual(foreign.status_code, 403)
            done = self.api('POST', '/api/gmail/terminar', json=answer)
            self.assertEqual((done.status_code, done.json()), (200, {'gmail': 'connected', 'motivo': ''}))
            again = self.api('POST', '/api/gmail/terminar', json=answer)
            self.assertEqual(again.json(), {'gmail': 'connected', 'motivo': 'caducado'})
        self.assert_nothing_leaked(kept_in_data=('ACCESO-FICTICIO', 'RENOVACION-FICTICIA'))

    def test_connection_made_with_an_imported_client_keeps_renewing_with_that_client(self):
        self.panel()
        self.assertEqual(self.state()['gmail_setup'], {'mode': 'import', 'notice': ''})
        self.assertEqual(self.upload(self.google.register(OTHER_CLIENT, 'installed')).status_code, 200)
        self.assertIn('conexion=gmail-ok', self.connect())
        self.assertEqual(read_json(self.root / 'gmail_token.json')['client_id'], OTHER_CLIENT)
        with clean_env(FACTURADOR_GMAIL_CLIENTE=self.client_file('installed')):
            state = self.state()
            self.assertEqual((state['gmail'], state['gmail_setup']['mode']), ('connected', 'shared'))
            self.expire(self.root)
            gmail.access_token(self.root)
            self.assertEqual(self.google.token_requests[-1]['client_id'], OTHER_CLIENT)
            # A permission saved before this setting existed does not say its client: it came from the imported one.
            record = read_json(self.root / 'gmail_token.json')
            del record['client_id']
            write_private(self.root / 'gmail_token.json', {**record, 'expires_at': 0})
            gmail.access_token(self.root)
            self.assertEqual(self.google.token_requests[-1]['client_id'], OTHER_CLIENT)
            self.assertNotIn('client_id', read_json(self.root / 'gmail_token.json'))
            # Connecting again goes through the client of the installation, which renews it from then on.
            self.assertIn('conexion=gmail-ok', self.connect())
            self.assertEqual(read_json(self.root / 'gmail_token.json')['client_id'], DESKTOP_CLIENT)
            self.expire(self.root)
            gmail.access_token(self.root)
            self.assertEqual(self.google.token_requests[-1]['client_id'], DESKTOP_CLIENT)

    def test_permission_of_a_client_that_is_gone_asks_to_connect_again_without_trying_another(self):
        with clean_env(FACTURADOR_GMAIL_CLIENTE=self.client_file('installed')):
            self.panel()
            self.assertIn('conexion=gmail-ok', self.connect())
        self.expire(self.root)
        expired = (self.root / 'gmail_token.json').read_bytes()
        asked = len(self.google.token_requests)
        cases = ((clean_env(), gmail.CLIENT_CHANGED),
                 (clean_env(FACTURADOR_GMAIL_CLIENTE=str(self.apart / 'falta.json')), gmail.SHARED_DOWN),
                 (clean_env(FACTURADOR_GMAIL_CLIENTE=self.client_file('installed', client=OTHER_CLIENT, name='otro.json')),
                  gmail.CLIENT_CHANGED))
        for settings, expected in cases:
            with settings, self.assertRaises(ValueError) as refused:
                gmail.access_token(self.root)
            self.assertEqual(str(refused.exception), expected)
        # Google was never asked with a client that did not give the permission, and the permission is intact.
        self.assertEqual(len(self.google.token_requests), asked)
        self.assertEqual((self.root / 'gmail_token.json').read_bytes(), expired)

    def test_client_that_cannot_be_used_is_said_clearly_and_nothing_takes_its_place(self):
        def good():
            return self.google.register(DESKTOP_CLIENT, 'installed')
        inside = self.root / 'cliente-google.json'
        inside.write_bytes(good())
        broken = {
            'falta': (None, gmail.UNREADABLE),
            'no-es-json': (b'esto no es un JSON', gmail.NOT_A_CLIENT),
            'lista': (b'[]', gmail.NOT_A_CLIENT),
            'sin-secreto': (json.dumps({'web': {'client_id': WEB_CLIENT}}).encode(), gmail.NOT_A_CLIENT),
            'otro-identificador': (json.dumps({'installed': {'client_id': 'algo', 'client_secret': 'x'}}).encode(), gmail.NOT_A_CLIENT),
            'enorme': (b' ' * 70_000 + good(), gmail.UNREADABLE),
        }
        self.panel()
        for name, (raw, problem) in broken.items():
            path = self.apart / (name + '.json')
            if raw is not None:
                path.write_bytes(raw)
            with self.subTest(name), clean_env(FACTURADOR_GMAIL_CLIENTE=str(path)):
                self.check_unusable(problem)
        with self.subTest('dentro de los datos'), clean_env(FACTURADOR_GMAIL_CLIENTE=str(inside)):
            self.check_unusable(gmail.INSIDE)
        with self.subTest('datos por variable'), clean_env(FACTURADOR_GMAIL_CLIENTE=self.client_file('installed'),
                                                          FACTURADOR_DATA=str(self.apart)):
            self.check_unusable(gmail.INSIDE)
        # Each problem is written once for the owner, without the place of the file or anything of it.
        errors = [record.getMessage() for record in self.records if record.levelno >= logging.ERROR and record.name == 'facturador.gmail']
        self.assertEqual(sorted(errors), sorted('Gmail con el cliente de Google de la instalación no está disponible. ' + problem
                                                for problem in (gmail.UNREADABLE, gmail.NOT_A_CLIENT, gmail.INSIDE)))
        self.assertNotIn(self.apart.name, self.logged())
        # Repaired where it was, it works without starting the program again.
        repaired = self.apart / 'no-es-json.json'
        repaired.write_bytes(good())
        with clean_env(FACTURADOR_GMAIL_CLIENTE=str(repaired)):
            state = self.state()
            self.assertEqual((state['gmail'], state['gmail_setup']), ('configured', {'mode': 'shared', 'notice': ''}))
            self.assertIn('conexion=gmail-ok', self.connect())

    def check_unusable(self, problem):
        state = self.state()
        self.assertEqual((state['gmail'], state['gmail_setup']),
                         ('disconnected', {'mode': 'broken', 'notice': gmail.SHARED_BROKEN + problem}))
        refused = self.api('POST', '/api/gmail/connect')
        self.assertEqual((refused.status_code, refused.text), (422, gmail.SHARED_BROKEN + problem))
        refused = self.upload(self.google.register(OTHER_CLIENT, 'installed'))
        self.assertEqual((refused.status_code, refused.text), (422, gmail.FIXED))
        self.assertFalse((self.root / 'google_client.json').exists())
        self.assertEqual(self.google.authorizations, [])

    def test_without_the_setting_the_person_imports_a_client_as_before(self):
        self.assertIn(gmail.CLIENT_VARIABLE, dotenv_values(WEB.parent / '.env.example'))
        with clean_env(**{name: value or '' for name, value in dotenv_values(WEB.parent / '.env.example').items()
                          if name not in {'FACTURADOR_DATA'}}):
            self.assertIsNone(gmail.installation())
            self.panel()
            state = self.state()
            self.assertEqual((state['gmail'], state['gmail_setup']), ('disconnected', {'mode': 'import', 'notice': ''}))
            refused = self.api('POST', '/api/gmail/connect')
            self.assertEqual((refused.status_code, refused.text), (422, 'Carga primero el cliente OAuth de escritorio de Google.'))
            self.assertEqual(self.upload(self.google.register(OTHER_CLIENT, 'installed')).status_code, 200)
            self.assertEqual(self.state()['gmail'], 'configured')
            self.assertIn(SECRET_MARK, json.dumps(read_json(self.root / 'google_client.json')))  # Imported: kept as before.
            _, asked = self.start()
            self.assertEqual((asked['client_id'], asked['redirect_uri']), (OTHER_CLIENT, self.origin + RETURN))
            unknown = self.api('POST', '/api/gmail/terminar', json={'estado': 'gmail-' + 'x' * 43, 'codigo': 'nada', 'error': ''})
            self.assertEqual(unknown.json(), {'gmail': 'configured', 'motivo': 'caducado'})


class EntranceTests(Case):
    """Several customers behind the real entrance: one Google client for the installation, one mailbox per person."""

    def setUp(self):
        super().setUp()
        use_master_key(self)
        self.accounts = make_accounts(self.root)
        self.ana_account = self.accounts.create_user('ana', PASSWORD)
        self.beto_account = self.accounts.create_user('beto', PASSWORD)

    def gate(self):
        self.client = self.enterContext(TestClient(make_entrance(self.root, accounts=self.accounts), base_url=ORIGIN))

    def visitor(self, name):
        person = Visitor(self.client, name)
        self.assertEqual(person.login().status_code, 200)
        return person

    def call(self, person, method, path, **options):
        response = person.panel(method, path, **options)
        self.replies.append(response.text)
        return response

    def folder(self, account):
        return self.root / 'usuarios' / account['id']

    def start(self, person, **body):
        started = self.call(person, 'POST', '/api/gmail/connect', json=body)
        self.assertEqual(started.status_code, 200, started.text)
        address = started.json()['url']
        return address, {key: values[0] for key, values in parse_qs(urlsplit(address).query).items()}

    def finish(self, person, answer):
        done = self.call(person, 'POST', '/api/gmail/terminar', json=answer)
        self.assertEqual(done.status_code, 200, done.text)
        return done.json()

    def assert_not_offered(self, person):
        state = self.call(person, 'GET', '/api/state').json()
        self.assertEqual((state['gmail'], state['gmail_setup']), ('disconnected', {'mode': 'none', 'notice': ''}))
        for path, body in (('/api/gmail/connect', {'send': False}), ('/api/gmail/terminar', {'estado': 'gmail-x', 'codigo': 'y'})):
            refused = self.call(person, 'POST', path, json=body)
            self.assertEqual(refused.status_code, 422, path)
            self.assertIn(NOT_OFFERED, refused.text)
        back = person.get(RETURN + '?state=gmail-x&code=y', headers={'accept': 'text/html'})
        self.assertEqual((back.status_code, back.headers['location']), (303, '/?conexion=gmail-error&motivo=no-disponible#ajustes'))
        signed_out = self.client.get(RETURN + '?state=gmail-x&code=y', headers={'accept': 'text/html'}, follow_redirects=False)
        self.assertEqual((signed_out.status_code, signed_out.headers['location']), (303, '/entrar'))
        self.assertEqual(self.google.authorizations, [])

    def test_without_a_usable_client_of_the_installation_nothing_changes(self):
        self.gate()
        ana = self.visitor('ana')
        self.assert_not_offered(ana)
        # A desktop client returns to the same computer, so it cannot serve people who enter from elsewhere.
        for path, problem in ((self.client_file('installed'), gmail.NEEDS_WEB), (str(self.apart / 'falta.json'), gmail.UNREADABLE)):
            with self.subTest(problem), clean_env(FACTURADOR_GMAIL_CLIENTE=path):
                self.assert_not_offered(ana)
                self.assertIn(problem, self.logged())
        self.assertFalse(any(self.root.rglob('gmail_token.json')))
        self.assertFalse(any(self.root.rglob('google_client.json')))
        self.assert_nothing_leaked()

    def test_each_person_connects_their_own_gmail_with_the_client_of_the_installation(self):
        with clean_env(FACTURADOR_GMAIL_CLIENTE=self.client_file('web', ORIGIN + RETURN)):
            self.gate()
            ana, beto = self.visitor('ana'), self.visitor('beto')
            state = self.call(ana, 'GET', '/api/state').json()
            # Nothing to create or to import: the person only presses Connect.
            self.assertEqual((state['gmail'], state['gmail_setup']), ('configured', {'mode': 'shared', 'notice': ''}))
            imported = self.call(ana, 'POST', '/api/gmail/client',
                                 files={'file': ('cliente.json', self.google.register(OTHER_CLIENT, 'installed'), 'application/json')})
            self.assertEqual(imported.status_code, 422)
            self.assertIn(NOT_OFFERED, imported.text)

            address, asked = self.start(ana)
            self.assertEqual((asked['client_id'], asked['redirect_uri'], asked['response_type'], asked['scope']),
                             (WEB_CLIENT, ORIGIN + RETURN, 'code', SCOPE))
            self.assertEqual((asked['access_type'], asked['code_challenge_method']), ('offline', 'S256'))
            self.assertTrue(asked['state'].startswith('gmail-'))
            self.assertNotIn('client_secret', asked)
            location = self.at_google(address)
            self.assertTrue(location.startswith(ORIGIN + RETURN + '?'))

            # Coming from Google the browser does not send the session cookie (SameSite=Strict). The entrance
            # answers with a page of its own that anyone may see: it carries nothing of anybody.
            arriving = self.client.get(location.removeprefix(ORIGIN), headers={'accept': 'text/html'}, follow_redirects=False)
            self.assertEqual((arriving.status_code, arriving.content), (200, (WEB / 'regreso.html').read_bytes()))
            self.assertEqual((arriving.headers['cache-control'], arriving.headers['referrer-policy'],
                              arriving.headers['content-security-policy']), ('no-store', 'no-referrer', entrada.CSP))
            self.assertNotIn('set-cookie', arriving.headers)
            script = self.client.get('/acceso/regreso.js')
            self.assertEqual((script.status_code, script.content), (200, (WEB / 'regreso.js').read_bytes()))
            self.assertEqual(ana.get('/acceso/yo').status_code, 200)  # The session is still open.
            self.assertEqual(self.google.token_requests, [])  # Arriving there finished nothing.

            answer = self.answer(location)
            # The answer only works in the panel of the person who asked for it, and with the token of that panel.
            self.assertEqual(self.finish(beto, answer), {'gmail': 'configured', 'motivo': 'caducado'})
            self.assertEqual(self.client.post('/api/gmail/terminar', json=answer, headers={'origin': ORIGIN}).status_code, 401)
            self.assertEqual(ana.post('/api/gmail/terminar', json=answer).status_code, 403)
            self.assertEqual(ana.post('/api/gmail/terminar', json=answer, headers={'x-app-token': beto.token}).status_code, 403)
            self.assertEqual(self.google.token_requests, [])
            self.assertEqual(self.finish(ana, answer), {'gmail': 'connected', 'motivo': ''})
            exchange = self.google.token_requests[-1]
            self.assertEqual((exchange['client_id'], exchange['redirect_uri'], exchange['code']),
                             (WEB_CLIENT, ORIGIN + RETURN, answer['codigo']))
            self.assertTrue(exchange['client_secret'].startswith(SECRET_MARK))
            self.assertGreaterEqual(len(exchange['code_verifier']), 43)
            self.assertEqual(self.finish(ana, answer), {'gmail': 'connected', 'motivo': 'caducado'})  # Once only.

            # Each permission lives in the folder of its own account, sealed for that account alone.
            ana_root, beto_root = self.folder(self.ana_account), self.folder(self.beto_account)
            self.assertEqual(self.call(beto, 'GET', '/api/state').json()['gmail'], 'configured')
            self.assertFalse((beto_root / 'gmail_token.json').exists())
            self.assertIn(secretos.MARK, json.loads((ana_root / 'gmail_token.json').read_text(encoding='utf-8')))
            record = secretos.read_json(ana_root / 'gmail_token.json', purpose=secretos.GMAIL_TOKEN)
            self.assertEqual((record['client_id'], self.google.access[record['token']]), (WEB_CLIENT, ADDRESS))
            # Opened, it holds the permission and which client gave it; never the secret of that client.
            self.assertEqual(sorted(record), ['client_id', 'expires_at', 'refresh_token', 'scope', 'token'])
            self.assertNotIn(SECRET_MARK, json.dumps(record))
            (beto_root / 'gmail_token.json').write_bytes((ana_root / 'gmail_token.json').read_bytes())
            with self.assertRaises(secretos.SealedSecrets):
                gmail.access_token(beto_root)
            (beto_root / 'gmail_token.json').unlink()
            self.assertTrue(self.call(ana, 'GET', '/api/correo').json()['gmail']['conectado'])
            self.assertFalse(self.call(beto, 'GET', '/api/correo').json()['gmail']['conectado'])

            # Renewal and the optional permission to send use the same client.
            self.expire(ana_root, sealed=True)
            self.assertEqual(self.google.access[gmail.access_token(ana_root)], ADDRESS)
            self.assertEqual(self.google.token_requests[-1]['client_id'], WEB_CLIENT)
            address, asked = self.start(ana, send=True)
            self.assertEqual(asked['scope'].split(), [SCOPE, SEND_SCOPE])
            self.assertEqual(self.finish(ana, self.answer(self.at_google(address))), {'gmail': 'connected', 'motivo': ''})
            self.assertTrue(self.call(ana, 'GET', '/api/correo').json()['gmail']['envio'])

            # The other person connects their own, with their own account at Google.
            self.google.signed_in = 'otra@gmail.example'
            address, _ = self.start(beto)
            self.assertEqual(self.finish(beto, self.answer(self.at_google(address))), {'gmail': 'connected', 'motivo': ''})
            theirs = secretos.read_json(beto_root / 'gmail_token.json', purpose=secretos.GMAIL_TOKEN)
            self.assertEqual(self.google.access[theirs['token']], 'otra@gmail.example')
            self.assertEqual(self.google.access[gmail.access_token(ana_root)], ADDRESS)
            self.assertEqual(self.call(ana, 'POST', '/api/gmail/disconnect').json(), 'configured')
            self.assertFalse((ana_root / 'gmail_token.json').exists())
            self.assertTrue((beto_root / 'gmail_token.json').exists())
        # The secret of the client is only in the file of the installation, away from every data folder.
        self.assertFalse(any(self.root.rglob('google_client.json')))
        self.assert_nothing_leaked()

    def test_false_repeated_expired_and_foreign_answers_connect_nothing(self):
        with clean_env(FACTURADOR_GMAIL_CLIENTE=self.client_file('web', ORIGIN + RETURN)):
            self.gate()
            ana, beto = self.visitor('ana'), self.visitor('beto')
            # An answer nobody asked for.
            for state in ('gmail-' + 'x' * 43, 'x' * 43, ''):
                self.assertEqual(self.finish(ana, {'estado': state, 'codigo': '4/0CODIGO-FICTICIO-inventado', 'error': ''}),
                                 {'gmail': 'configured', 'motivo': 'caducado'})
            self.assertEqual(self.google.token_requests, [])

            # One that arrives too late.
            address, _ = self.start(ana)
            late = self.answer(self.at_google(address))
            with patch.object(gmail, 'time', Later(601)):
                self.assertEqual(self.finish(ana, late), {'gmail': 'configured', 'motivo': 'caducado'})
            self.assertEqual(self.finish(ana, late), {'gmail': 'configured', 'motivo': 'caducado'})
            self.assertEqual(self.google.token_requests, [])
            # Shortly before the ten minutes are over it still works.
            address, _ = self.start(beto)
            in_time = self.answer(self.at_google(address))
            with patch.object(gmail, 'time', Later(590)):
                self.assertEqual(self.finish(beto, in_time), {'gmail': 'connected', 'motivo': ''})
            self.assertEqual(self.call(beto, 'POST', '/api/gmail/disconnect').json(), 'configured')

            # The person says no at Google.
            self.google.declines = True
            address, _ = self.start(ana)
            declined = self.answer(self.at_google(address))
            self.assertEqual((declined['codigo'], declined['error']), ('', 'access_denied'))
            self.assertEqual(self.finish(ana, declined), {'gmail': 'configured', 'motivo': 'consentimiento'})
            self.google.declines = False

            # Ana's code handed over by somebody else with an attempt of their own: Google ties each code
            # to the attempt that asked for it, so her mailbox never ends up in another account.
            (first, _), (second, _), (third, _) = self.start(ana), self.start(ana), self.start(ana)
            mine, mine_asked = self.start(beto)
            stolen = self.answer(self.at_google(first))
            self.assertEqual(self.finish(beto, {**stolen, 'estado': mine_asked['state']}),
                             {'gmail': 'configured', 'motivo': 'intercambio'})
            # The same between two attempts of one person.
            crossed = self.answer(self.at_google(second))
            third_state = parse_qs(urlsplit(third).query)['state'][0]
            self.assertEqual(self.finish(ana, {**crossed, 'estado': third_state}), {'gmail': 'configured', 'motivo': 'intercambio'})
            # A code that Google already took does not work again, not even with its own attempt.
            self.assertEqual(self.finish(ana, crossed), {'gmail': 'configured', 'motivo': 'intercambio'})
            self.assertEqual([form['grant_type'] for form in self.google.token_requests[-3:]], ['authorization_code'] * 3)
            self.assertFalse(any(self.root.rglob('gmail_token.json')))

            # Google gives less than what was asked: nothing is kept.
            for change, reason in (({'grants_renewal': False}, 'sin-renovacion'), ({'granted': [SEND_SCOPE]}, 'permiso'),
                                   ({'mailbox_available': False}, 'buzon')):
                with self.subTest(reason), patch.multiple(self.google, **change):
                    address, _ = self.start(ana)
                    self.assertEqual(self.finish(ana, self.answer(self.at_google(address))),
                                     {'gmail': 'configured', 'motivo': reason})
            self.assertFalse(any(self.root.rglob('gmail_token.json')))

            # A body that is not the three short texts is refused without being repeated.
            refused = self.call(ana, 'POST', '/api/gmail/terminar', json={'estado': 'gmail-x', 'codigo': ['CODIGO-FICTICIO']})
            self.assertEqual((refused.status_code, refused.text),
                             (422, 'La solicitud no se entendió. Vuelve a intentarlo desde Ajustes.'))
        self.assert_nothing_leaked()


class ReturnPageTests(unittest.TestCase):
    def test_page_has_no_inline_code_and_its_script_reads_and_keeps_nothing(self):
        parser = NoInlineCode()
        parser.feed((WEB / 'regreso.html').read_text(encoding='utf-8'))
        self.assertEqual((parser.problems, parser.styles, parser.scripts), ([], ['/acceso/entrar.css'], ['/acceso/regreso.js']))
        source = (WEB / 'regreso.js').read_text(encoding='utf-8')
        for forbidden in ('innerHTML', 'outerHTML', 'insertAdjacentHTML', 'document.write', 'eval(', 'localStorage', 'sessionStorage',
                          'indexedDB', 'document.cookie', 'fetch(', 'XMLHttpRequest', 'sendBeacon', 'postMessage', 'import '):
            self.assertNotIn(forbidden, source)
        # It only ever leaves for the panel of this same site.
        self.assertEqual(source.count('location.replace('), 1)
        self.assertIn("location.replace('/#'", source)


if __name__ == '__main__':
    unittest.main()
