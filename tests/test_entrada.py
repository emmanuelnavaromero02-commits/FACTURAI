"""The entrance for several customers: modes, signing in, pages, administration and isolation."""
import json
import tempfile
import unittest
from html.parser import HTMLParser
from pathlib import Path
from unittest.mock import AsyncMock, Mock, patch

from fastapi.testclient import TestClient
from uvicorn.middleware.proxy_headers import ProxyHeadersMiddleware

from acceso_dobles import (MASTER, ORIGIN, OTHER_MASTER, PASSWORD, FakeClock, Visitor, key_text, make_accounts,
                           make_entrance, picture, quiet_app, use_master_key)
from facturador import entrada, secretos
from facturador.app import create_app
from facturador.entrada import ConfigError, clean_origin, open_entrance, settings_from_env
from facturador.usuarios import ALLOWANCE, IDLE_SECONDS, LIFETIME_SECONDS

WEB = Path(__file__).resolve().parents[1] / 'web'
PROFILE = {'rfc': 'AAA010101AAA', 'name': 'ANA PRUEBA UNO', 'postal_code': '01000', 'regime': '612',
           'cfdi_use': 'G03', 'email': 'ana.prueba@example.com'}


class ConfigurationTests(unittest.TestCase):
    def setUp(self):
        folder = tempfile.TemporaryDirectory()
        self.addCleanup(folder.cleanup)
        self.root = Path(folder.name) / 'datos'
        self.addCleanup(secretos.reset)

    def env(self, **values):
        return {'FACTURADOR_DATA': str(self.root), **values}

    def test_local_mode_is_the_default(self):
        for env in ({}, {'FACTURADOR_MODO': ''}, {'FACTURADOR_MODO': 'local'}, {'FACTURADOR_MODO': ' Local '}):
            self.assertIsNone(settings_from_env(env))
            self.assertIsNone(open_entrance(self.env(**env)))
        # The local mode does not ask for an origin or a master key, and creates nothing.
        self.assertFalse(self.root.exists())
        with self.assertRaises(ConfigError):
            settings_from_env({'FACTURADOR_MODO': 'multi'})

    def test_example_settings_keep_the_local_mode_without_a_master_key(self):
        from dotenv import dotenv_values
        example = dotenv_values(WEB.parent / '.env.example')
        for name in ('FACTURADOR_MODO', 'FACTURADOR_ORIGEN_PUBLICO', 'FACTURADOR_ARCHIVO_LLAVE', 'FACTURADOR_LLAVE_MAESTRA',
                     'FACTURADOR_PROXIES', 'FACTURADOR_NAVEGADORES_MAX', 'FACTURADOR_INSTANCIA_INACTIVA_MIN'):
            self.assertIn(name, example)  # Every new setting is documented there.
        self.assertIsNone(settings_from_env(example))
        self.assertFalse(secretos.load_from_env(self.root, example))
        # The same file with only the mode, the address and a key turned on is a valid configuration.
        ready = settings_from_env({**example, 'FACTURADOR_MODO': 'usuarios', 'FACTURADOR_ORIGEN_PUBLICO': 'https://facturas.ejemplo.com'})
        self.assertEqual((ready.max_browsers, ready.idle_seconds), (3, 1800))

    def test_users_mode_refuses_to_start_without_a_public_https_origin(self):
        for origin in (None, '', 'http://facturas.example.com', 'facturas.example.com', 'ftp://facturas.example.com',
                       'https://', 'https://facturas.example.com/panel', 'https://usuario:clave@facturas.example.com',
                       'https://facturas.example.com/?a=1', 'https://facturas.example.com:puerto', 'http://192.168.1.20:8765',
                       'https://[::1]:8443', 'https://facturas ejemplo.com'):
            env = self.env(FACTURADOR_MODO='usuarios', FACTURADOR_LLAVE_MAESTRA=key_text())
            if origin is not None:
                env['FACTURADOR_ORIGEN_PUBLICO'] = origin
            with self.subTest(origin=origin), self.assertRaises(ConfigError) as caught:
                open_entrance(env)
            self.assertIn('https://', str(caught.exception))
            self.assertIn('FACTURADOR_ORIGEN_PUBLICO', str(caught.exception))
        self.assertEqual(clean_origin('https://Facturas.Example.com/'), 'https://facturas.example.com')
        self.assertEqual(clean_origin('https://facturas.example.com:443'), 'https://facturas.example.com')
        self.assertEqual(clean_origin('https://facturas.example.com:8443'), 'https://facturas.example.com:8443')
        # Only for trying it out on the same machine.
        self.assertEqual(clean_origin('http://localhost:8765'), 'http://localhost:8765')
        self.assertEqual(clean_origin('http://127.0.0.1:8765/'), 'http://127.0.0.1:8765')

    def test_users_mode_refuses_to_start_without_the_master_key(self):
        base = self.env(FACTURADOR_MODO='usuarios', FACTURADOR_ORIGEN_PUBLICO='https://facturas.example.com')
        with self.assertRaises(ConfigError) as caught:
            open_entrance(base)
        self.assertIn('llave maestra', str(caught.exception))
        self.root.mkdir()
        inside = self.root / 'llave'
        inside.write_text(key_text())
        for extra in ({'FACTURADOR_LLAVE_MAESTRA': 'demasiado-corta'}, {'FACTURADOR_ARCHIVO_LLAVE': str(inside)},
                      {'FACTURADOR_ARCHIVO_LLAVE': str(self.root.parent / 'no-existe')}):
            with self.subTest(extra=sorted(extra)), self.assertRaises(ConfigError) as caught:
                open_entrance({**base, **extra})
            self.assertIn('llave maestra', str(caught.exception))
        self.assertFalse((self.root / 'acceso').exists())

    def test_users_mode_starts_with_origin_and_key_and_refuses_another_key_later(self):
        outside = self.root.parent / 'llave-maestra.key'
        outside.write_text(key_text() + '\n')
        env = self.env(FACTURADOR_MODO='usuarios', FACTURADOR_ORIGEN_PUBLICO='https://facturas.example.com',
                       FACTURADOR_ARCHIVO_LLAVE=str(outside), FACTURADOR_NAVEGADORES_MAX='5',
                       FACTURADOR_INSTANCIA_INACTIVA_MIN='10')
        gate = open_entrance(env, port=9000)
        gate._hashing.shutdown()
        self.assertEqual((gate.settings.public_origin, gate.settings.port, gate.settings.max_browsers,
                          gate.settings.idle_seconds, gate.instances.browsers.limit), ('https://facturas.example.com', 9000, 5, 600, 5))
        self.assertEqual(gate.cookie, '__Host-facturai_sesion')
        self.assertTrue((self.root / 'acceso' / 'acceso.sqlite').is_file())
        check = (self.root / secretos.CHECK_FILE).read_bytes()
        env.pop('FACTURADOR_ARCHIVO_LLAVE')
        with self.assertRaises(ConfigError) as caught:
            open_entrance({**env, 'FACTURADOR_LLAVE_MAESTRA': key_text(OTHER_MASTER)})
        self.assertIn('No se pueden abrir los secretos guardados', str(caught.exception))
        self.assertEqual((self.root / secretos.CHECK_FILE).read_bytes(), check)
        for name, value in (('FACTURADOR_NAVEGADORES_MAX', '0'), ('FACTURADOR_NAVEGADORES_MAX', 'tres'),
                            ('FACTURADOR_INSTANCIA_INACTIVA_MIN', '-1')):
            with self.subTest(name=name, value=value), self.assertRaises(ConfigError):
                settings_from_env({**env, name: value})


class LocalModeTests(unittest.TestCase):
    """What exists today must stay the same when nothing new is configured."""

    def setUp(self):
        folder = tempfile.TemporaryDirectory()
        self.addCleanup(folder.cleanup)
        self.root = Path(folder.name)

    def test_local_application_keeps_its_hosts_origins_and_open_panel(self):
        app = create_app(self.root)
        client = TestClient(app)
        state = client.get('/api/state')
        self.assertEqual(state.status_code, 200)  # No account and no cookie.
        self.assertNotIn('set-cookie', state.headers)
        headers = {'x-app-token': state.json()['token']}
        self.assertEqual(client.put('/api/automation', json={'enabled': False}, headers=headers).status_code, 200)
        for origin in ('http://127.0.0.1:8765', 'http://localhost:8765', 'http://testserver'):
            self.assertEqual(client.put('/api/automation', json={'enabled': False},
                                        headers={**headers, 'origin': origin}).status_code, 200, origin)
        for origin in (ORIGIN, 'https://facturas.example.com', 'http://127.0.0.1:9999'):
            self.assertEqual(client.put('/api/automation', json={'enabled': False},
                                        headers={**headers, 'origin': origin}).status_code, 403, origin)
        for host in ('127.0.0.1:8765', 'localhost:8765', 'testserver'):
            self.assertEqual(client.get('/healthz', headers={'host': host}).status_code, 200, host)
        self.assertEqual(client.get('/healthz', headers={'host': 'facturas.example.com'}).status_code, 400)
        for path in ('/entrar', '/cuenta', '/acceso/yo', '/acceso/entrar.js'):
            self.assertEqual(client.get(path).status_code, 404, path)

    def test_application_behind_the_entrance_only_trusts_the_public_origin(self):
        app = create_app(self.root, public_origin=ORIGIN, lazy_browser=True)
        client = TestClient(app, base_url=ORIGIN)
        headers = {'x-app-token': client.get('/api/state').json()['token']}
        self.assertEqual(client.put('/api/automation', json={'enabled': False}, headers={**headers, 'origin': ORIGIN}).status_code, 200)
        for origin in ('http://127.0.0.1:8765', 'http://localhost:8765', 'http://testserver', 'https://otro.example.com'):
            self.assertEqual(client.put('/api/automation', json={'enabled': False},
                                        headers={**headers, 'origin': origin}).status_code, 403, origin)
        self.assertEqual(client.put('/api/automation', json={'enabled': False}, headers={'origin': ORIGIN}).status_code, 403)
        for host in ('127.0.0.1:8765', 'localhost', 'testserver'):
            self.assertEqual(client.get('/healthz', headers={'host': host}).status_code, 400, host)


class EntranceCase(unittest.TestCase):
    factory = staticmethod(quiet_app)

    def setUp(self):
        folder = tempfile.TemporaryDirectory()
        self.addCleanup(folder.cleanup)
        self.root = Path(folder.name)
        use_master_key(self)
        self.wall = FakeClock()
        self.clock = FakeClock()
        self.accounts = make_accounts(self.root, self.wall)
        self.admin_account = self.accounts.create_user('jefa', PASSWORD, role='admin')
        self.ana_account = self.accounts.create_user('ana', PASSWORD)
        self.beto_account = self.accounts.create_user('beto', PASSWORD)
        self.gate = make_entrance(self.root, accounts=self.accounts, clock=self.clock, factory=self.factory)
        self.client = self.enterContext(TestClient(self.gate, base_url=ORIGIN))

    def visitor(self, name='', login=True) -> Visitor:
        person = Visitor(self.client, name)
        if name and login:
            self.assertEqual(person.login().status_code, 200)
        return person

    def folder(self, account) -> Path:
        return self.root / 'usuarios' / account['id']


class SignInTests(EntranceCase):
    def test_cookie_carries_every_protection(self):
        reply = self.visitor().post('/acceso/entrar', json={'usuario': 'ana', 'contrasena': PASSWORD})
        self.assertEqual((reply.status_code, reply.json()), (200, {'ok': True}))
        cookies = reply.headers.get_list('set-cookie')
        self.assertEqual(len(cookies), 1)
        name, _, rest = cookies[0].partition('=')
        token, *attributes = [part.strip() for part in rest.split(';')]
        self.assertEqual(name, '__Host-facturai_sesion')
        self.assertRegex(token, r'^[A-Za-z0-9_-]{43}$')  # 32 random bytes.
        self.assertEqual(sorted(attributes), sorted(['HttpOnly', 'Secure', 'SameSite=Strict', 'Path=/',
                                                     f'Max-Age={LIFETIME_SECONDS}']))
        self.assertNotIn(token, reply.text)
        self.assertEqual(self.accounts.session(token)['nombre'], 'ana')

    def test_unknown_name_and_wrong_password_get_the_same_answer(self):
        answers = []
        for body in ({'usuario': 'ana', 'contrasena': 'equivocada-123456'}, {'usuario': 'nadie', 'contrasena': PASSWORD},
                     {'usuario': 'x', 'contrasena': 'y'}, {'usuario': None, 'contrasena': 5}, {}):
            reply = self.visitor().post('/acceso/entrar', json=body)
            answers.append((reply.status_code, reply.content, sorted(reply.headers)))
            self.assertNotIn('set-cookie', reply.headers)
        self.assertEqual(len({(status, body) for status, body, _ in answers}), 1)
        self.assertEqual(len({tuple(names) for _, _, names in answers}), 1)
        self.assertEqual(answers[0][0], 401)
        self.assertEqual(json.loads(answers[0][1]), {'detail': 'Usuario o contraseña incorrectos.'})

    def test_too_many_attempts_are_told_to_wait_whoever_they_name(self):
        for name in ('ana', 'nadie'):
            person = self.visitor()
            for _ in range(ALLOWANCE['usuario'][0]):
                self.assertEqual(person.post('/acceso/entrar', json={'usuario': name, 'contrasena': 'equivocada-123456'}).status_code, 401)
            reply = person.post('/acceso/entrar', json={'usuario': name, 'contrasena': PASSWORD})
            self.assertEqual((reply.status_code, reply.headers['retry-after']), (429, '5'))
            self.assertIn('Demasiados intentos', reply.json()['detail'])
            self.assertNotIn('set-cookie', reply.headers)
        self.wall.advance(5)
        self.assertEqual(self.visitor().login('ana').status_code, 200)

    def test_sign_in_and_sign_out_check_the_origin(self):
        ana = self.visitor('ana')
        body = {'usuario': 'beto', 'contrasena': PASSWORD}
        for headers in ({'origin': 'https://ajeno.example.com'}, {'origin': 'null'}, {'origin': ORIGIN + '.ajeno.example.com'},
                        {'origin': 'http://facturai.test'}, {'origin': ORIGIN, 'sec-fetch-site': 'cross-site'},
                        {'origin': ORIGIN, 'sec-fetch-site': 'same-site'}):
            for path in ('/acceso/entrar', '/acceso/salir'):
                reply = self.client.post(path, json=body, headers={**headers, 'cookie': ana.cookie})
                self.client.cookies.clear()
                self.assertEqual((reply.status_code, reply.json()), (403, {'detail': 'Origen no admitido'}), (headers, path))
                self.assertNotIn('set-cookie', reply.headers)
        missing = self.client.post('/acceso/salir', json={}, headers={'cookie': ana.cookie})
        self.client.cookies.clear()
        self.assertEqual(missing.status_code, 403)
        self.assertEqual(ana.get('/acceso/yo').json()['usuario'], 'ana')  # Nobody signed her out or in as someone else.
        same = self.client.post('/acceso/entrar', json=body, headers={'origin': ORIGIN, 'sec-fetch-site': 'same-origin'})
        self.client.cookies.clear()
        self.assertEqual(same.status_code, 200)

    def test_only_small_json_bodies_are_read_and_never_echoed(self):
        secret = 'secreto-que-no-debe-volver-1'
        person = self.visitor()
        form = person.post('/acceso/entrar', data={'usuario': 'ana', 'contrasena': secret})
        huge = person.post('/acceso/entrar', json={'usuario': 'ana', 'contrasena': secret, 'relleno': 'x' * 5000})
        broken = person.post('/acceso/entrar', content='{"usuario": "ana", "contrasena": "' + secret,
                             headers={'content-type': 'application/json'})
        listed = person.post('/acceso/entrar', json=[secret])
        self.assertEqual([reply.status_code for reply in (form, huge, broken, listed)], [415, 413, 422, 422])
        for reply in (form, huge, broken, listed):
            self.assertNotIn(secret, reply.text)
            self.assertNotIn('set-cookie', reply.headers)

    def test_session_is_renewed_on_sign_in_and_a_planted_one_is_never_adopted(self):
        ana = self.visitor('ana')
        first = ana.cookie
        self.assertEqual(ana.login().status_code, 200)
        self.assertNotEqual(ana.cookie, first)
        # The identifier that existed before signing in again stopped working on the server.
        self.assertEqual(self.client.get('/api/state', headers={'cookie': first}).status_code, 401)
        self.client.cookies.clear()
        planted = '__Host-facturai_sesion=' + 'A' * 43
        victim = self.visitor()
        victim.cookie = planted
        self.assertEqual(victim.login('beto').status_code, 200)
        self.assertNotEqual(victim.cookie, planted)
        self.assertEqual(self.client.get('/acceso/yo', headers={'cookie': planted}).status_code, 401)
        self.client.cookies.clear()
        self.assertEqual(victim.get('/acceso/yo').json()['usuario'], 'beto')

    def test_sign_out_ends_the_session_on_the_server(self):
        ana = self.visitor('ana')
        copied = ana.cookie
        self.assertEqual(ana.get('/api/state').status_code, 200)
        reply = ana.post('/acceso/salir', json={})
        self.assertEqual(reply.status_code, 200)
        self.assertRegex(reply.headers['set-cookie'], r'^__Host-facturai_sesion=; Max-Age=0; Path=/; HttpOnly; Secure; SameSite=Strict$')
        self.assertEqual(ana.cookie, '')
        stolen = self.client.get('/api/state', headers={'cookie': copied})
        self.client.cookies.clear()
        self.assertEqual((stolen.status_code, stolen.text), (401, 'Tu sesión terminó. Vuelve a entrar.'))
        self.assertEqual(self.visitor().post('/acceso/salir', json={}).status_code, 200)  # Harmless without a session.

    def test_session_expires_when_idle_and_after_seven_days(self):
        ana = self.visitor('ana')
        self.wall.advance(IDLE_SECONDS - 60)
        self.assertEqual(ana.get('/api/state').status_code, 200)
        self.wall.advance(IDLE_SECONDS + 1)
        expired = ana.get('/api/state')
        self.assertEqual((expired.status_code, expired.text), (401, 'Tu sesión terminó. Vuelve a entrar.'))
        self.assertIn('Max-Age=0', expired.headers['set-cookie'])
        beto = self.visitor('beto')
        for _ in range(LIFETIME_SECONDS // 3600):
            self.wall.advance(3600)
            self.assertEqual(beto.get('/acceso/yo').status_code, 200)
        self.wall.advance(3600)
        self.assertEqual(beto.get('/acceso/yo').status_code, 401)

    def test_without_a_session_nothing_of_the_panel_answers(self):
        nobody = self.visitor()
        page = nobody.get('/', headers={'accept': 'text/html,application/xhtml+xml'})
        self.assertEqual((page.status_code, page.headers['location']), (303, '/entrar'))
        account = nobody.get('/cuenta', headers={'accept': 'text/html'})
        self.assertEqual((account.status_code, account.headers['location']), (303, '/entrar'))
        for method, path in (('GET', '/api/state'), ('GET', '/app.js'), ('GET', '/style.css'), ('GET', '/api/jobs'),
                             ('GET', '/api/export.csv'), ('GET', '/acceso/yo'), ('GET', '/acceso/admin/usuarios'),
                             ('POST', '/api/upload'), ('PUT', '/api/profile'), ('POST', '/acceso/contrasena'),
                             ('POST', '/acceso/admin/invitaciones'), ('OPTIONS', '/api/state')):
            reply = nobody.request(method, path)
            self.assertEqual((reply.status_code, reply.text), (401, 'Tu sesión terminó. Vuelve a entrar.'), (method, path))
        self.assertEqual(self.gate.instances._live, {})  # Nobody woke an instance up.
        self.assertFalse((self.root / 'usuarios').exists())
        signed = self.visitor('ana').get('/entrar', headers={'accept': 'text/html'})
        self.assertEqual((signed.status_code, signed.headers['location']), (303, '/'))

    def test_health_check_answers_without_session_or_host_and_reveals_nothing(self):
        for headers in ({}, {'host': '127.0.0.1:8765'}, {'host': 'cualquiera.example.com'}):
            reply = self.client.get('/healthz', headers=headers)
            self.assertEqual((reply.status_code, reply.json()), (200, {'status': 'ok'}))
            self.assertNotIn('set-cookie', reply.headers)
        self.visitor('ana').get('/api/state')
        self.assertEqual(self.client.get('/healthz').json(), {'status': 'ok'})
        self.assertEqual(self.client.post('/healthz', headers={'origin': ORIGIN}).status_code, 405)

    def test_only_the_public_host_is_served(self):
        ana = self.visitor('ana')
        for host in ('ajeno.example.com', '127.0.0.1:8765', 'localhost', 'facturai.test:8443', 'facturai.test.ajeno.example.com'):
            for path in ('/entrar', '/api/state', '/acceso/yo'):
                reply = ana.get(path, headers={'host': host})
                self.assertEqual((reply.status_code, reply.text), (400, 'Dirección no admitida'), (host, path))
        self.assertEqual(ana.get('/acceso/yo', headers={'host': 'FACTURAI.test'}).status_code, 200)


class NoInlineCode(HTMLParser):
    def __init__(self):
        super().__init__()
        self.problems, self.scripts, self.styles = [], [], []

    def handle_starttag(self, tag, attrs):
        values = dict(attrs)
        if tag == 'script':
            self.scripts.append(values.get('src'))
            if not values.get('src'):
                self.problems.append('script en línea')
        if tag == 'style' or 'style' in values:
            self.problems.append('estilo en línea')
        if tag == 'link' and values.get('rel') == 'stylesheet':
            self.styles.append(values.get('href'))
        self.problems += [name for name in values if name.startswith('on')]
        if any(str(value).lower().startswith('javascript:') for value in values.values()):
            self.problems.append('javascript:')


class PagesTests(EntranceCase):
    def test_pages_have_no_inline_code_and_load_only_their_own_files(self):
        for name in ('entrar.html', 'cuenta.html'):
            parser = NoInlineCode()
            parser.feed((WEB / name).read_text(encoding='utf-8'))
            self.assertEqual(parser.problems, [], name)
            self.assertEqual(parser.styles, ['/acceso/entrar.css'], name)
            self.assertEqual(parser.scripts, ['/acceso/' + name.replace('.html', '.js')], name)
        for name in ('entrar.js', 'cuenta.js', 'panel-acceso.js'):
            source = (WEB / name).read_text(encoding='utf-8')
            for forbidden in ('innerHTML', 'outerHTML', 'insertAdjacentHTML', 'document.write', 'eval(', 'localStorage', 'sessionStorage'):
                self.assertNotIn(forbidden, source, name)

    def test_pages_and_files_use_the_same_policy_as_the_panel(self):
        ana = self.visitor('ana')
        panel = ana.get('/')
        self.assertEqual(panel.status_code, 200)
        policy = panel.headers['content-security-policy']
        self.assertIn("script-src 'self'", policy)
        self.assertEqual(entrada.CSP, policy)
        nobody = self.visitor()
        for path, kind in (('/entrar', 'text/html'), ('/invitacion', 'text/html'), ('/acceso/entrar.css', 'text/css'),
                           ('/acceso/entrar.js', 'application/javascript'), ('/acceso/cuenta.js', 'application/javascript'),
                           ('/acceso/panel.js', 'application/javascript')):
            reply = nobody.get(path)
            self.assertEqual(reply.status_code, 200, path)
            self.assertTrue(reply.headers['content-type'].startswith(kind), path)
            self.assertEqual(reply.headers['content-security-policy'], policy, path)
            self.assertEqual(reply.headers['x-content-type-options'], 'nosniff')
            self.assertEqual(reply.headers['referrer-policy'], 'no-referrer')
            self.assertEqual(reply.headers['strict-transport-security'], 'max-age=31536000')
            self.assertIn(reply.headers['cache-control'], {'no-store', 'no-cache'})
        self.assertEqual(panel.headers['strict-transport-security'], 'max-age=31536000')
        self.assertEqual(ana.get('/cuenta').headers['cache-control'], 'no-store')
        script = nobody.get('/acceso/entrar.js')
        self.assertEqual(nobody.get('/acceso/entrar.js', headers={'if-none-match': script.headers['etag']}).status_code, 304)
        self.assertEqual(script.content, (WEB / 'entrar.js').read_bytes())
        for path in ('/acceso/entrar.html', '/acceso/../facturador/app.py', '/acceso/app.js', '/acceso/', '/acceso/usuarios.py'):
            self.assertEqual(ana.get(path).status_code, 404, path)
        self.assertEqual(ana.post('/entrar', json={}).status_code, 404)
        self.assertEqual(ana.get('/acceso/entrar').status_code, 404)

    def test_pages_are_in_plain_spanish_and_made_for_a_phone(self):
        for name in ('entrar.html', 'cuenta.html'):
            page = (WEB / name).read_text(encoding='utf-8')
            self.assertIn('<html lang="es">', page)
            self.assertIn('name="viewport" content="width=device-width, initial-scale=1"', page)
        login = (WEB / 'entrar.html').read_text(encoding='utf-8')
        for expected in ('autocomplete="username"', 'autocomplete="current-password"', 'autocomplete="new-password"',
                         'minlength="12"', 'autocapitalize="none"'):
            self.assertIn(expected, login)
        style = (WEB / 'entrar.css').read_text(encoding='utf-8')
        self.assertIn('font-size: 16px', style)  # Phones zoom in on smaller fields.
        self.assertIn('min-height: 48px', style)


class AccountTests(EntranceCase):
    def test_person_sees_only_their_own_account(self):
        ana = self.visitor('ana')
        self.assertEqual(ana.get('/acceso/yo').json(), {'usuario': 'ana', 'rol': 'usuario', 'sesiones': 1})
        self.assertEqual(ana.get('/cuenta').status_code, 200)
        for method, path, body in (('GET', '/acceso/admin/usuarios', None), ('POST', '/acceso/admin/invitaciones', {}),
                                   ('POST', '/acceso/admin/invitaciones/revocar', {'id': 'x'}),
                                   ('POST', '/acceso/admin/usuarios/estado', {'id': self.beto_account['id'], 'activo': False})):
            reply = ana.request(method, path, json=body) if body is not None else ana.request(method, path)
            self.assertEqual((reply.status_code, reply.json()), (403, {'detail': 'Solo un administrador puede hacer esto.'}), path)
        self.assertTrue(self.accounts.user(self.beto_account['id'])['activo'])

    def test_change_password_asks_for_the_current_one_and_closes_other_sessions(self):
        phone, laptop = self.visitor('ana'), self.visitor('ana')
        new = 'otra frase larga nueva 2'
        wrong = phone.post('/acceso/contrasena', json={'actual': 'no-es-la-actual-1', 'nueva': new})
        self.assertEqual((wrong.status_code, wrong.json()), (422, {'detail': 'La contraseña actual no es correcta.'}))
        short = phone.post('/acceso/contrasena', json={'actual': PASSWORD, 'nueva': 'corta'})
        self.assertEqual(short.status_code, 422)
        self.assertIn('12 caracteres', short.json()['detail'])
        before = phone.cookie
        done = phone.post('/acceso/contrasena', json={'actual': PASSWORD, 'nueva': new})
        self.assertEqual(done.status_code, 200)
        self.assertNotEqual(phone.cookie, before)
        self.assertEqual(phone.get('/acceso/yo').json()['sesiones'], 1)
        self.assertEqual(laptop.get('/acceso/yo').status_code, 401)
        self.assertEqual(self.client.get('/acceso/yo', headers={'cookie': before}).status_code, 401)
        self.client.cookies.clear()
        self.assertEqual(self.visitor().login('ana').status_code, 401)
        self.assertEqual(self.visitor().login('ana', new).status_code, 200)

    def test_person_can_close_their_other_sessions(self):
        phone, laptop, tablet = self.visitor('ana'), self.visitor('ana'), self.visitor('ana')
        beto = self.visitor('beto')
        self.assertEqual(phone.get('/acceso/yo').json()['sesiones'], 3)
        self.assertEqual(phone.post('/acceso/sesiones/cerrar-otras', json={}).json(), {'cerradas': 2})
        self.assertEqual(phone.get('/acceso/yo').json()['sesiones'], 1)
        self.assertEqual(laptop.get('/api/state').status_code, 401)
        self.assertEqual(tablet.get('/acceso/yo').status_code, 401)
        self.assertEqual(beto.get('/acceso/yo').status_code, 200)

    def test_there_is_no_open_sign_up(self):
        nobody = self.visitor()
        for path in ('/acceso/registro', '/acceso/usuarios', '/acceso/admin/usuarios', '/registro'):
            reply = nobody.post(path, json={'usuario': 'intruso', 'contrasena': PASSWORD})
            self.assertIn(reply.status_code, {401, 404}, path)
        missing = nobody.post('/acceso/invitacion', json={'codigo': 'x' * 43, 'usuario': 'intruso', 'contrasena': PASSWORD})
        self.assertEqual(missing.status_code, 404)
        self.assertEqual(len(self.accounts.users()), 3)

    def test_administrator_invites_with_a_code_that_works_once(self):
        admin = self.visitor('jefa')
        made = admin.post('/acceso/admin/invitaciones', json={'horas': 24})
        self.assertEqual(made.status_code, 200)
        code = made.json()['codigo']
        self.assertEqual(made.json()['enlace'], ORIGIN + '/invitacion#' + code)
        listed = admin.get('/acceso/admin/usuarios')
        self.assertNotIn(code, listed.text)
        self.assertEqual(len(listed.json()['invitaciones']), 1)
        guest = self.visitor()
        self.assertEqual(guest.post('/acceso/invitacion/consultar', json={'codigo': code}).json(), {'usuario': ''})
        refused = guest.post('/acceso/invitacion', json={'codigo': code, 'usuario': 'carla', 'contrasena': 'corta'})
        self.assertEqual(refused.status_code, 422)
        accepted = guest.post('/acceso/invitacion', json={'codigo': code, 'usuario': 'Carla', 'contrasena': 'frase de carla 123'})
        self.assertEqual(accepted.status_code, 200)
        self.assertIn('HttpOnly; Secure; SameSite=Strict', accepted.headers['set-cookie'])
        self.assertEqual(guest.get('/acceso/yo').json(), {'usuario': 'carla', 'rol': 'usuario', 'sesiones': 1})
        self.assertEqual(guest.get('/api/state').json()['jobs'], [])
        again = self.visitor().post('/acceso/invitacion', json={'codigo': code, 'usuario': 'dora', 'contrasena': 'frase de dora 1234'})
        self.assertEqual((again.status_code, again.json()['detail']),
                         (404, 'Esta invitación ya no es válida. Pide una nueva a quien administra el servicio.'))
        self.assertEqual(self.visitor().post('/acceso/invitacion/consultar', json={'codigo': code}).status_code, 404)
        self.assertEqual(admin.get('/acceso/admin/usuarios').json()['invitaciones'], [])

    def test_invitation_expires_can_be_cancelled_and_keeps_a_reserved_name(self):
        admin = self.visitor('jefa')
        expiring = admin.post('/acceso/admin/invitaciones', json={'horas': 1}).json()['codigo']
        self.wall.advance(3601)
        late = self.visitor().post('/acceso/invitacion', json={'codigo': expiring, 'usuario': 'dora', 'contrasena': 'frase de dora 1234'})
        self.assertEqual(late.status_code, 404)
        admin = self.visitor('jefa')
        cancelled = admin.post('/acceso/admin/invitaciones', json={}).json()
        pending = admin.get('/acceso/admin/usuarios').json()['invitaciones']
        self.assertEqual(admin.post('/acceso/admin/invitaciones/revocar', json={'id': pending[0]['id']}).json(), {'cancelada': True})
        self.assertEqual(self.visitor().post('/acceso/invitacion/consultar', json={'codigo': cancelled['codigo']}).status_code, 404)
        self.assertEqual(admin.post('/acceso/admin/invitaciones', json={'usuario': 'ana'}).status_code, 422)
        self.assertEqual(admin.post('/acceso/admin/invitaciones', json={'horas': 9999}).status_code, 422)
        reserved = admin.post('/acceso/admin/invitaciones', json={'usuario': 'Elena'}).json()
        guest = self.visitor()
        self.assertEqual(guest.post('/acceso/invitacion/consultar', json={'codigo': reserved['codigo']}).json(), {'usuario': 'elena'})
        guest.post('/acceso/invitacion', json={'codigo': reserved['codigo'], 'usuario': 'otro', 'contrasena': 'frase de elena 123'})
        self.assertEqual(guest.get('/acceso/yo').json()['usuario'], 'elena')

    def test_wrong_codes_are_braked_by_address(self):
        guest = self.visitor()
        for _ in range(ALLOWANCE['direccion'][0]):
            self.assertEqual(guest.post('/acceso/invitacion/consultar', json={'codigo': 'x' * 43}).status_code, 404)
        self.assertEqual(guest.post('/acceso/invitacion/consultar', json={'codigo': 'x' * 43}).status_code, 429)
        self.assertEqual(guest.post('/acceso/invitacion', json={'codigo': 'x' * 43, 'usuario': 'dora',
                                                              'contrasena': 'frase de dora 1234'}).status_code, 429)

    def test_administrator_turns_an_account_off_and_on(self):
        admin, ana = self.visitor('jefa'), self.visitor('ana')
        self.assertEqual(ana.get('/api/state').status_code, 200)
        people = {person['nombre']: person for person in admin.get('/acceso/admin/usuarios').json()['usuarios']}
        self.assertEqual(set(people), {'jefa', 'ana', 'beto'})
        self.assertEqual(set(people['ana']), {'id', 'nombre', 'rol', 'activo', 'creado', 'ultimo_acceso'})
        off = admin.post('/acceso/admin/usuarios/estado', json={'id': people['ana']['id'], 'activo': False})
        self.assertEqual((off.status_code, off.json()['usuario']['activo']), (200, False))
        self.assertEqual(ana.get('/api/state').status_code, 401)  # Her sessions ended at once.
        self.assertEqual(ana.get('/acceso/yo').status_code, 401)
        self.assertIsNone(self.gate.instances.live(people['ana']['id']))
        refused = self.visitor().login('ana')
        self.assertEqual((refused.status_code, refused.json()), (401, {'detail': 'Usuario o contraseña incorrectos.'}))
        own = admin.post('/acceso/admin/usuarios/estado', json={'id': people['jefa']['id'], 'activo': False})
        self.assertEqual(own.status_code, 422)
        for body in ({'id': people['ana']['id']}, {'id': people['ana']['id'], 'activo': 'no'}, {'id': 7, 'activo': False},
                     {'id': 'no-existe', 'activo': False}):
            self.assertEqual(admin.post('/acceso/admin/usuarios/estado', json=body).status_code, 422, body)
        on = admin.post('/acceso/admin/usuarios/estado', json={'id': people['ana']['id'], 'activo': True})
        self.assertTrue(on.json()['usuario']['activo'])
        self.assertEqual(self.visitor().login('ana').status_code, 200)


class Spy:
    """The real application, with a record of every request that reaches it and of the folder it serves."""
    seen: list = []

    def __init__(self, root, **options):
        self.app = quiet_app(root, **options)
        self.state = self.app.state
        self.owner = Path(root).name

    async def __call__(self, scope, receive, send):
        if scope['type'] == 'http':
            Spy.seen.append((self.owner, scope['method'], scope['path'], dict(scope['headers'])))
        await self.app(scope, receive, send)


class IsolationTests(EntranceCase):
    factory = Spy

    def setUp(self):
        Spy.seen = []
        super().setUp()
        self.ana, self.beto, self.admin = self.visitor('ana'), self.visitor('beto'), self.visitor('jefa')
        self.assertEqual(self.ana.panel('PUT', '/api/profile', json=PROFILE).status_code, 200)
        self.ana_job = self.ana.upload('red')
        self.beto_job = self.beto.upload('blue')
        chat = self.ana.panel('POST', '/api/chat', data={'client_id': 'mensaje-privado-0001'},
                              files=[('files', ('foto-de-ana.png', picture('green'), 'image/png'))])
        self.assertEqual(chat.status_code, 200, chat.text)
        self.ana_chat_job = chat.json()['job_ids'][0]
        self.ana_asset = chat.json()['messages'][0]['files'][0]['url']
        note = self.ana.panel('POST', '/api/chat', data={'text': 'nota privada de ana', 'client_id': 'mensaje-privado-0002'})
        self.assertEqual(note.status_code, 200, note.text)
        self.markers = ('ticket-red', 'foto-de-ana', 'nota privada de ana', PROFILE['rfc'], PROFILE['name'],
                        self.ana_job, self.ana_chat_job, self.ana_asset.rsplit('/', 1)[1])

    def ana_view(self):
        events = {key: self.ana.get(f'/api/jobs/{key}/events').json() for key in (self.ana_job, self.ana_chat_job)}
        return self.ana.get('/api/state').json(), self.ana.get('/api/chat').json(), events

    def test_each_person_has_a_folder_and_an_instance_of_their_own(self):
        folders = {path.name for path in (self.root / 'usuarios').iterdir()}
        self.assertEqual(folders, {self.ana_account['id'], self.beto_account['id']})  # The administrator has not used the panel.
        self.assertTrue((self.folder(self.ana_account) / 'facturador.sqlite').is_file())
        self.assertTrue((self.folder(self.ana_account) / 'profile.json').is_file())
        self.assertFalse((self.folder(self.beto_account) / 'profile.json').exists())
        self.assertIsNot(self.gate.instances.live(self.ana_account['id']).app, self.gate.instances.live(self.beto_account['id']).app)
        self.assertNotEqual(self.ana.token, self.beto.token)
        # Every upload of Ana lives under her folder and nowhere else in the data.
        red, green = picture('red'), picture('green')
        holders = {path.relative_to(self.root).parts[:2] for path in self.root.rglob('*')
                   if path.is_file() and path.read_bytes() in (red, green)}
        self.assertEqual(holders, {('usuarios', self.ana_account['id'])})
        self.assertEqual([job['id'] for job in self.beto.get('/api/state').json()['jobs']], [self.beto_job])
        self.assertEqual({job['id'] for job in self.ana.get('/api/state').json()['jobs']}, {self.ana_job, self.ana_chat_job})

    def probes(self, person):
        receipt = {'provider': 'unknown', 'merchant': 'INTRUSO', 'amount': '1.00', 'date': '2026-11-01', 'payment_code': '01',
                   'ticket_number': 'X-1'}
        for key in (self.ana_job, self.ana_chat_job):
            base = f'/api/jobs/{key}'
            for path in ('/events', '/events?after=0', '/source', '/files/pdf', '/files/xml', '/whatsapp', '/correo', '/sat'):
                yield person.panel('GET', base + path)
            for path, body in (('/analyze', {'consent': True, 'mode': 'local'}), ('/run', {'consent': True}),
                               ('/decision', {'decision': 'cancel'}), ('/stop', None), ('/retry', None), ('/read-local', {}),
                               ('/recover-local', None), ('/recover-browser', {'files': ['a' * 16]}), ('/gmail', {'query': 'x'}),
                               ('/whatsapp/recover', None), ('/whatsapp/open', None), ('/correo/buscar', None), ('/sat', None),
                               ('/correo/pedir', {'to': 'ventas@comercio.example.com', 'consent': True})):
                yield person.panel('POST', base + path, json=body) if body is not None else person.panel('POST', base + path)
            yield person.panel('PUT', base, json=receipt)
            yield person.panel('PUT', base + '/whatsapp/watch', json={'watch': True})
            yield person.panel('POST', base + '/attachments', files=[('files', ('factura.xml', b'<x/>', 'application/xml'))])
        for path in (self.ana_asset, self.ana_asset + '?w=200', '/api/browser/downloads/' + 'a' * 16,
                     f'/usuarios/{self.ana_account["id"]}/facturador.sqlite', f'/usuarios/{self.ana_account["id"]}/profile.json',
                     f'/api/jobs/..%2F..%2F{self.ana_account["id"]}/source', f'/api/chat/files/..%2F{self.ana_account["id"]}',
                     f'/%2e%2e/usuarios/{self.ana_account["id"]}/profile.json', '//api/jobs/' + self.ana_job + '/events'):
            yield person.panel('GET', path)

    def assert_cannot_reach_ana(self, person, owner):
        before = self.ana_view()
        Spy.seen.clear()
        for reply in self.probes(person):
            request = f'{reply.request.method} {reply.request.url.path}'
            self.assertIn(reply.status_code, {400, 404, 405, 409, 422}, request)
            for marker in self.markers:
                self.assertNotIn(marker.encode(), reply.content, request)
        # Listings of the person's own instance show nothing of Ana either.
        for path in ('/api/state', '/api/jobs', '/api/chat', '/api/export.csv', '/api/browser/downloads', '/api/correo',
                     '/api/whatsapp/attention', '/'):
            reply = person.panel('GET', path)
            self.assertEqual(reply.status_code, 200, path)
            for marker in self.markers:
                self.assertNotIn(marker.encode(), reply.content, path)
        self.assertIsNone(person.get('/api/state').json()['profile'])
        frame = person.panel('GET', '/api/browser/frame?revision=0')
        self.assertEqual((frame.status_code, frame.content), (204, b''))
        # Every one of those requests was served by this person's own instance, never by Ana's.
        self.assertTrue(Spy.seen)
        self.assertEqual({served for served, *_ in Spy.seen}, {owner})
        self.assertEqual(self.ana_view(), before)  # And nothing of Ana changed.

    def test_another_person_cannot_see_or_touch_anything_by_any_route(self):
        self.assert_cannot_reach_ana(self.beto, self.beto_account['id'])

    def test_administrator_cannot_see_customer_data_or_enter_as_someone_else(self):
        self.assert_cannot_reach_ana(self.admin, self.admin_account['id'])
        listing = self.admin.get('/acceso/admin/usuarios')
        for marker in self.markers:
            self.assertNotIn(marker, listing.text)
        before = self.admin.cookie
        for method, path, body in (('POST', '/acceso/admin/entrar-como', {'id': self.ana_account['id']}),
                                   ('POST', '/acceso/admin/usuarios/' + self.ana_account['id'] + '/entrar', {}),
                                   ('GET', '/acceso/admin/usuarios/' + self.ana_account['id'], None),
                                   ('GET', '/acceso/admin/usuarios/' + self.ana_account['id'] + '/datos', None),
                                   ('POST', '/acceso/entrar', {'usuario': 'ana', 'contrasena': ''}),
                                   ('POST', '/acceso/entrar', {'usuario': 'ana', 'como': 'jefa', 'contrasena': 'no-la-sabe-1234'})):
            reply = self.admin.request(method, path, json=body) if body is not None else self.admin.request(method, path)
            self.assertIn(reply.status_code, {401, 404}, path)
        state = self.admin.post('/acceso/admin/usuarios/estado', json={'id': self.ana_account['id'], 'activo': True, 'entrar': True})
        self.assertNotIn('set-cookie', state.headers)
        self.assertEqual(self.admin.cookie, before)
        self.assertEqual(self.admin.get('/acceso/yo').json()['usuario'], 'jefa')
        self.assertEqual(self.admin.get('/api/state').json()['jobs'], [])

    def test_headers_cookies_and_parameters_do_not_choose_the_instance(self):
        target = self.ana_account['id']
        own = [self.beto_job]
        Spy.seen.clear()
        for headers in ({'x-usuario': target}, {'x-user': target, 'x-user-id': target}, {'x-forwarded-user': 'ana'},
                        {'x-forwarded-for': '127.0.0.1', 'forwarded': 'for=127.0.0.1;host=facturai.test'},
                        {'x-original-url': f'/api/jobs/{self.ana_job}/events', 'x-rewrite-url': '/api/state'},
                        {'authorization': 'Bearer ' + self.ana.cookie.split('=', 1)[1]}, {'x-app-token': self.ana.token},
                        {'cookie': self.beto.cookie + '; usuario=' + target + '; facturai_sesion=' + target},
                        {'x-forwarded-host': 'ana.facturai.test', 'x-real-ip': '10.0.0.1'}):
            reply = self.beto.get(f'/api/state?usuario={target}&user=ana&id={target}', headers=headers)
            self.assertEqual(reply.status_code, 200, headers)
            self.assertEqual([job['id'] for job in reply.json()['jobs']], own, headers)
            self.assertEqual(reply.json()['token'], self.beto.token)
        self.assertEqual({served for served, *_ in Spy.seen}, {self.beto_account['id']})
        # Ana's application token is worth nothing in Beto's instance.
        stolen = self.beto.request('PUT', '/api/automation', json={'enabled': True}, headers={'x-app-token': self.ana.token})
        self.assertEqual((stolen.status_code, stolen.text), (403, 'Solicitud ajena al panel local'))
        # A user identifier is not a session.
        for cookie in ('__Host-facturai_sesion=' + target, '__Host-facturai_sesion=' + self.ana.token,
                       'facturai_sesion=' + self.ana.cookie.split('=', 1)[1]):
            reply = self.client.get('/api/state', headers={'cookie': cookie})
            self.client.cookies.clear()
            self.assertEqual(reply.status_code, 401, cookie)

    def test_live_browser_of_one_person_is_never_shown_or_handed_to_another(self):
        picture_of_ana = b'\xff\xd8 imagen del navegador de ana \xff\xd9'

        class Page:
            url = 'https://comercio-de-ana.example.com/factura?folio=secreto'
            screenshot = AsyncMock(return_value=picture_of_ana)

            def is_closed(self):
                return False

        viewer = self.gate.instances.live(self.ana_account['id']).runner.viewer
        self.client.portal.call(lambda: viewer.show(Page(), 'Comercio de Ana', manual=True))
        revision = viewer.revision
        mine = self.ana.panel('GET', f'/api/browser/frame?revision={revision}')
        self.assertEqual((mine.status_code, mine.content), (200, picture_of_ana))
        self.assertEqual(self.ana.get('/api/state').json()['browser']['url'], 'https://comercio-de-ana.example.com/factura')
        for person in (self.beto, self.admin):
            shown = Page.screenshot.await_count
            state = person.get('/api/state').json()['browser']
            self.assertEqual((state['open'], state['url'], state['label']), (False, '', ''))
            frame = person.panel('GET', f'/api/browser/frame?revision={revision}')
            self.assertEqual((frame.status_code, frame.content), (204, b''))
            for path, body in (('/api/browser/control', {'revision': revision}), ('/api/browser/text', {'channel': 'x'}),
                               ('/api/browser/screenshot', {'channel': 'x'}), ('/api/browser/dialog', {'dialog_id': 'x', 'accept': True}),
                               ('/api/browser/input', {'channel': 'x', 'kind': 'text', 'value': 'intruso'}),
                               ('/api/browser/history', {'channel': 'x', 'direction': 'back'})):
                reply = person.panel('POST', path, json=body)
                self.assertEqual(reply.status_code, 422, path)
                self.assertNotIn(b'comercio-de-ana', reply.content)
            self.assertEqual(Page.screenshot.await_count, shown)  # Nobody else made Ana's page take a picture.
        self.assertEqual((viewer.channel, viewer.revision, viewer.manual), ('', revision, True))  # Her view was not touched.

    def test_application_never_receives_the_session_cookie(self):
        Spy.seen.clear()
        self.assertEqual(self.ana.get('/api/state').status_code, 200)
        self.assertEqual(self.ana.panel('PUT', '/api/automation', json={'enabled': False}).status_code, 200)
        self.assertEqual(len(Spy.seen), 2)
        token = self.ana.cookie.split('=', 1)[1].encode()
        for owner, _, _, headers in Spy.seen:
            self.assertEqual(owner, self.ana_account['id'])
            self.assertNotIn(b'cookie', headers)
            self.assertFalse(any(token in value for value in headers.values()))


class NotOfferedTests(EntranceCase):
    factory = Spy

    def test_chatgpt_and_gmail_authorizations_answer_clearly_without_reaching_the_application(self):
        ana = self.visitor('ana')
        self.assertEqual(ana.panel('GET', '/api/state').status_code, 200)
        Spy.seen.clear()
        chatgpt = ana.panel('POST', '/api/chatgpt/connect', json={})
        self.assertEqual(chatgpt.status_code, 422)
        self.assertIn('no se ofrece en este servicio', chatgpt.text)
        self.assertEqual(chatgpt.headers['content-type'].split(';')[0], 'text/plain')
        for request in (ana.panel('POST', '/api/gmail/client', files={'file': ('cliente.json', b'{}', 'application/json')}),
                        ana.panel('POST', '/api/gmail/connect', json={'send': False})):
            self.assertEqual(request.status_code, 422)
            self.assertIn('Conecta tu correo en Ajustes', request.text)
        back = ana.get('/auth/chatgpt/callback?state=x&code=y&client_id=oaiapp_z')
        self.assertEqual((back.status_code, back.headers['location']), (303, '/?conexion=chatgpt-error#ajustes'))
        back = ana.get('/auth/gmail/callback?state=x&code=y')
        self.assertEqual((back.status_code, back.headers['location']), (303, '/?conexion=gmail-error&motivo=no-disponible#ajustes'))
        self.assertEqual(Spy.seen, [])
        # The rest keeps working: the panel still reports both as not connected.
        state = ana.get('/api/state').json()
        self.assertEqual((state['plan']['status'], state['gmail']), ('disconnected', 'disconnected'))
        self.assertEqual(ana.panel('POST', '/api/gmail/disconnect').status_code, 200)
        self.assertEqual(ana.panel('POST', '/api/chatgpt/retry').status_code, 200)
        # Without a session they are closed like everything else.
        self.assertEqual(self.visitor().get('/auth/gmail/callback?state=x&code=y').status_code, 401)


class UpkeepTests(unittest.TestCase):
    def test_entrance_puts_rested_instances_to_sleep_and_closes_the_rest_when_it_stops(self):
        import time
        folder = tempfile.TemporaryDirectory()
        self.addCleanup(folder.cleanup)
        root = Path(folder.name)
        use_master_key(self)
        accounts = make_accounts(root)
        accounts.create_user('ana', PASSWORD)
        accounts.create_user('beto', PASSWORD)
        # Real clocks here: this checks that the entrance itself runs the upkeep while it serves.
        gate = make_entrance(root, accounts=accounts, clock=time.monotonic, idle_seconds=1)
        with TestClient(gate, base_url=ORIGIN) as client:
            ana, beto = Visitor(client, 'ana'), Visitor(client, 'beto')
            self.assertEqual(ana.login().status_code, 200)
            self.assertEqual(beto.login().status_code, 200)
            self.assertEqual(ana.get('/api/state').status_code, 200)
            first = next(iter(gate.instances._live.values()))
            deadline = time.monotonic() + 10
            while gate.instances._live and time.monotonic() < deadline:
                time.sleep(.1)
            self.assertEqual(gate.instances._live, {})  # Nobody asked for it: the upkeep closed it.
            self.assertTrue(first.app.state.automation.stopping)
            token = ana.get('/api/state').json()['token']  # The next request wakes it up again.
            self.assertTrue(token)
            self.assertEqual(beto.get('/api/state').status_code, 200)
            awake = list(gate.instances._live.values())
            self.assertEqual(len(awake), 2)
        self.assertEqual(gate.instances._live, {})
        for instance in awake:
            self.assertIsNone(instance._task)  # Its shutdown ran when the entrance stopped.
            self.assertTrue(instance.app.state.automation.stopping)


class ProxyAddressTests(unittest.TestCase):
    """Behind the TLS proxy the brake must count the real address, and only the proxy may tell it."""
    PROXY = '10.9.8.7'

    def setUp(self):
        folder = tempfile.TemporaryDirectory()
        self.addCleanup(folder.cleanup)
        use_master_key(self)
        accounts = make_accounts(Path(folder.name), FakeClock())
        accounts.create_user('ana', PASSWORD)
        self.gate = make_entrance(Path(folder.name), accounts=accounts)
        self.addCleanup(self.gate._hashing.shutdown)

    def attempt(self, peer, forwarded, name, password='equivocada-123456'):
        # The same wrapper uvicorn puts in front of the entrance (proxy_headers, forwarded_allow_ips).
        client = TestClient(ProxyHeadersMiddleware(self.gate, trusted_hosts=self.PROXY), base_url=ORIGIN, client=(peer, 50000))
        reply = client.post('/acceso/entrar', json={'usuario': name, 'contrasena': password},
                            headers={'origin': ORIGIN, 'x-forwarded-for': forwarded})
        return reply.status_code

    def test_real_address_comes_from_the_trusted_proxy_and_cannot_be_faked_by_others(self):
        free = ALLOWANCE['direccion'][0]
        for number in range(free):
            self.assertEqual(self.attempt(self.PROXY, '203.0.113.5', f'persona{number}'), 401)
        self.assertEqual(self.attempt(self.PROXY, '203.0.113.5', 'ana', PASSWORD), 429)
        # Someone else entering through the same proxy is not held back.
        self.assertEqual(self.attempt(self.PROXY, '198.51.100.77', 'ana', PASSWORD), 200)
        # Whoever reaches the entrance without the proxy cannot dodge the brake by inventing the header.
        for number in range(free):
            self.assertEqual(self.attempt('192.0.2.50', f'203.0.113.{100 + number}', f'otra{number}'), 401)
        self.assertEqual(self.attempt('192.0.2.50', '203.0.113.250', 'ana', PASSWORD), 429)
        # Nor by naming the trusted proxy's address in front of the header.
        self.assertEqual(self.attempt('192.0.2.50', self.PROXY, 'ana', PASSWORD), 429)


class SecretsOfEachCustomerTests(EntranceCase):
    def test_secrets_written_by_an_instance_are_sealed_with_that_customer_key(self):
        from facturador import buzon
        settings = {'imap_host': 'imap.example.org', 'imap_port': 993, 'user': 'buzon@example.org',
                    'address': 'buzon@example.org', 'smtp_host': '', 'smtp_port': 465, 'authority': ''}
        ana, beto = self.visitor('ana'), self.visitor('beto')
        ana.get('/api/state')
        beto.get('/api/state')
        roots = [self.folder(self.ana_account), self.folder(self.beto_account)]
        for root in roots:
            buzon.save_account(root, settings, 'clave-app-ficticia')
            self.assertIn(secretos.MARK, json.loads((root / 'auth' / 'correo' / 'vault-key.json').read_text()))
        self.assertTrue(ana.get('/api/correo').json()['conectado'])
        # Beto's sealed key does not open in Ana's folder, even with the same master key.
        (roots[0] / 'auth' / 'correo' / 'vault-key.json').write_bytes((roots[1] / 'auth' / 'correo' / 'vault-key.json').read_bytes())
        with self.assertRaisesRegex(ValueError, 'No se pueden abrir los secretos guardados'):
            buzon.account_password(roots[0])
        self.assertEqual(buzon.account_password(roots[1]), 'clave-app-ficticia')


class CommandTests(unittest.TestCase):
    def setUp(self):
        folder = tempfile.TemporaryDirectory()
        self.addCleanup(folder.cleanup)
        self.root = Path(folder.name) / 'datos'
        self.addCleanup(secretos.reset)
        from acceso_dobles import clean_env
        self.clean_env = clean_env
        from facturador import __main__ as command
        self.command = command
        self.enterContext(patch.object(command, 'load_dotenv', lambda *a, **k: None))

    def run_main(self, arguments, env, *, typed=(), terminal=True):
        answers = list(typed)
        with (self.clean_env(FACTURADOR_DATA=str(self.root), **env),
              patch.object(self.command.sys, 'stdin', Mock(isatty=lambda: terminal)),
              patch.object(self.command.getpass, 'getpass', lambda prompt='': answers.pop(0)),
              patch('builtins.input', lambda prompt='': answers.pop(0)),
              patch.object(self.command.uvicorn, 'run') as run,
              patch.object(self.command, 'create_app', return_value='aplicacion-local') as local,
              patch('builtins.print') as printed):
            try:
                self.command.main(arguments)
                failure = None
            except SystemExit as exc:
                failure = exc
        return failure, run, local, printed

    def test_local_start_is_exactly_the_one_of_today(self):
        failure, run, local, _ = self.run_main([], {})
        self.assertIsNone(failure)
        local.assert_called_once_with(port=8765)
        run.assert_called_once_with('aplicacion-local', host='127.0.0.1', port=8765, workers=1, access_log=False)
        failure, run, local, _ = self.run_main(['--port', '9001'], {'FACTURADOR_CONTAINER': '1'})
        local.assert_called_once_with(port=9001)
        run.assert_called_once_with('aplicacion-local', host='0.0.0.0', port=9001, workers=1, access_log=False)
        self.assertFalse(self.root.exists())

    def test_users_mode_refuses_to_start_with_a_clear_message(self):
        key = {'FACTURADOR_LLAVE_MAESTRA': key_text()}
        for env, expected in (({'FACTURADOR_MODO': 'usuarios', **key}, 'FACTURADOR_ORIGEN_PUBLICO'),
                              ({'FACTURADOR_MODO': 'usuarios', 'FACTURADOR_ORIGEN_PUBLICO': 'http://facturas.example.com', **key}, 'https://'),
                              ({'FACTURADOR_MODO': 'usuarios', 'FACTURADOR_ORIGEN_PUBLICO': 'https://facturas.example.com'}, 'llave maestra'),
                              ({'FACTURADOR_MODO': 'otro'}, 'FACTURADOR_MODO'),
                              ({'FACTURADOR_LLAVE_MAESTRA': 'mala'}, 'llave maestra')):
            failure, run, local, _ = self.run_main([], env)
            self.assertIsNotNone(failure, env)
            self.assertIn('FACTURAI no puede arrancar', str(failure))
            self.assertIn(expected, str(failure))
            run.assert_not_called()
            local.assert_not_called()

    def test_users_mode_starts_the_entrance_behind_the_proxy(self):
        env = {'FACTURADOR_MODO': 'usuarios', 'FACTURADOR_ORIGEN_PUBLICO': 'https://facturas.example.com',
               'FACTURADOR_LLAVE_MAESTRA': key_text(), 'FACTURADOR_PROXIES': '172.18.0.0/16'}
        failure, run, local, _ = self.run_main([], env)
        self.assertIsNone(failure)
        local.assert_not_called()
        (served,), options = run.call_args
        served._hashing.shutdown()
        self.assertIsInstance(served, entrada.Entrance)
        self.assertEqual(options, {'host': '127.0.0.1', 'port': 8765, 'workers': 1, 'access_log': False,
                                   'server_header': False, 'proxy_headers': True, 'forwarded_allow_ips': '172.18.0.0/16'})

    def test_local_start_with_another_master_key_is_refused(self):
        self.assertIsNone(self.run_main([], {'FACTURADOR_LLAVE_MAESTRA': key_text()})[0])
        self.assertTrue((self.root / secretos.CHECK_FILE).is_file())
        for env in ({'FACTURADOR_LLAVE_MAESTRA': key_text(OTHER_MASTER)}, {}):
            failure, run, _, _ = self.run_main([], env)
            self.assertIn('No se pueden abrir los secretos guardados', str(failure))
            run.assert_not_called()

    def test_first_administrator_types_the_password_at_the_keyboard(self):
        env = {'FACTURADOR_LLAVE_MAESTRA': key_text()}
        failure, _, _, printed = self.run_main(['crear-admin'], env, typed=['Jefa', PASSWORD, PASSWORD])
        self.assertIsNone(failure)
        self.assertNotIn(PASSWORD, str(printed.call_args_list))
        secretos.configure(MASTER)
        accounts = entrada.open_accounts(self.root)
        self.assertEqual([(user['nombre'], user['rol']) for user in accounts.users()], [('jefa', 'admin')])
        self.assertEqual(accounts.authenticate('jefa', PASSWORD, '')['rol'], 'admin')
        with accounts.connect() as db:
            self.assertEqual(db.execute('SELECT costo FROM usuarios').fetchone()[0], '131072:8:1')
        for typed, expected in ((['otra', PASSWORD, 'no coincide 12345'], 'no coinciden'), (['otra', 'corta'], '12 caracteres'),
                                (['jefa', PASSWORD, PASSWORD], 'ya existe'), (['no vale!'], 'usuario')):
            failure, *_ = self.run_main(['crear-admin'], env, typed=typed)
            self.assertIn('No se creó la cuenta', str(failure))
            self.assertIn(expected, str(failure))
            self.assertNotIn(PASSWORD, str(failure))
        failure, *_ = self.run_main(['crear-admin', '--usuario', 'segunda'], env, typed=[PASSWORD, PASSWORD])
        self.assertIsNone(failure)
        secretos.configure(MASTER)
        self.assertEqual(len(entrada.open_accounts(self.root).users()), 2)

    def test_password_is_never_taken_from_an_argument_a_variable_or_a_pipe(self):
        env = {'FACTURADOR_LLAVE_MAESTRA': key_text()}
        with patch('sys.stderr'):
            for arguments in (['crear-admin', '--contrasena', PASSWORD], ['crear-admin', '--password', PASSWORD],
                              ['crear-admin', '--clave=' + PASSWORD], ['crear-admin', 'jefa', PASSWORD],
                              ['crear-admin', '--usuario', 'jefa', PASSWORD]):
                failure, *_ = self.run_main(arguments, env, typed=['jefa', PASSWORD, PASSWORD])
                self.assertEqual(getattr(failure, 'code', None), 2, arguments)  # The command line itself is refused.
        self.assertFalse((self.root / 'acceso').exists())
        variables = {name: PASSWORD for name in ('FACTURADOR_ADMIN_PASSWORD', 'FACTURADOR_ADMIN_CONTRASENA',
                                                 'FACTURADOR_CONTRASENA', 'FACTURADOR_PASSWORD')}
        with self.assertRaises(IndexError):  # It still asks at the keyboard, and nothing was typed here.
            self.run_main(['crear-admin', '--usuario', 'jefa'], {**env, **variables}, typed=[])
        secretos.configure(MASTER)
        self.assertEqual(entrada.open_accounts(self.root).users(), [])
        piped, *_ = self.run_main(['crear-admin', '--usuario', 'jefa'], env, typed=[PASSWORD, PASSWORD], terminal=False)
        self.assertIn('terminal', str(piped))
        secretos.configure(MASTER)
        self.assertEqual(entrada.open_accounts(self.root).users(), [])
        without_key, *_ = self.run_main(['crear-admin', '--usuario', 'jefa'], {}, typed=[PASSWORD, PASSWORD])
        self.assertIn('llave maestra', str(without_key))

    def test_master_key_file_is_made_by_a_command(self):
        target = self.root.parent / 'llave-maestra.key'
        failure, _, _, printed = self.run_main(['crear-llave', str(target)], {})
        self.assertIsNone(failure)
        key = target.read_text().strip()
        self.assertEqual(len(secretos.parse_key(key)), 32)
        self.assertNotIn(key, str(printed.call_args_list))
        again, *_ = self.run_main(['crear-llave', str(target)], {})
        self.assertIn('ya existe', str(again))
        self.assertEqual(target.read_text().strip(), key)
        inside, *_ = self.run_main(['crear-llave', str(self.root / 'llave.key')], {})
        self.assertIn('fuera de la carpeta de datos', str(inside))


if __name__ == '__main__':
    unittest.main()
