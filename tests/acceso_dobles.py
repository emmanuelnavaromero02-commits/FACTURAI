"""Shared doubles for the tests of the entrance for several customers. No network, no real browser."""
import io
import os
from pathlib import Path
from unittest.mock import patch

from PIL import Image

from facturador import secretos
from facturador.app import create_app
from facturador.entrada import Entrance, Settings
from facturador.instancias import BrowserPool, Instances
from facturador.usuarios import Accounts

ORIGIN = 'https://facturai.test'
MASTER = bytes(range(32))
OTHER_MASTER = bytes(range(100, 132))
FAST = {'n': 2 ** 10, 'r': 8, 'p': 1}  # The real cost is checked once, in test_usuarios.
PASSWORD = 'frase larga de prueba 1'
POST = {'origin': ORIGIN}


class FakeClock:
    def __init__(self, start=1_000_000.0):
        self.now = start

    def __call__(self):
        return self.now

    def advance(self, seconds):
        self.now += seconds


def picture(color='white', size=(64, 48)) -> bytes:
    output = io.BytesIO()
    Image.new('RGB', size, color).save(output, 'PNG')
    return output.getvalue()


def quiet_app(root, **options):
    """The real application with its ticket queue left stopped, as the panel tests do."""
    app = create_app(root, **options)
    patch.object(app.state.automation, 'start', lambda: None).start()
    return app


def use_master_key(test, key=MASTER):
    secretos.configure(key)
    test.addCleanup(secretos.reset)


def make_accounts(root: Path, clock=None) -> Accounts:
    options = {'clock': clock} if clock else {}
    return Accounts(Path(root) / 'acceso', pepper=secretos.derive(secretos.PASSWORDS), cost=FAST, **options)


def make_entrance(root: Path, *, accounts=None, clock=None, wall=None, factory=quiet_app, max_browsers=3,
                  idle_seconds=1800, origin=ORIGIN, pool=None) -> Entrance:
    clock = clock or FakeClock()
    settings = Settings(public_origin=origin, data_root=Path(root), max_browsers=max_browsers, idle_seconds=idle_seconds)
    instances = Instances(settings.data_root, factory, port=settings.port, public_origin=origin,
                          max_browsers=max_browsers, idle_seconds=idle_seconds, clock=clock,
                          pool=pool or BrowserPool(max_browsers, clock=clock, poll=.02))
    return Entrance(settings, accounts or make_accounts(root, wall), clock=clock, instances=instances)


class Visitor:
    """One browser in front of the entrance: it keeps its own cookie and sends the page's origin."""

    def __init__(self, client, name=''):
        self.client, self.name, self.cookie, self.token = client, name, '', ''

    def request(self, method, path, *, headers=None, **options):
        sent = dict(headers or {})
        if self.cookie and 'cookie' not in {key.lower() for key in sent}:
            sent['cookie'] = self.cookie
        if method != 'GET':
            sent.setdefault('origin', ORIGIN)
        options.setdefault('follow_redirects', False)
        response = self.client.request(method, path, headers=sent, **options)
        self.client.cookies.clear()  # Every visitor carries its cookie by hand.
        for value in response.headers.get_list('set-cookie'):
            pair = value.split(';', 1)[0]
            self.cookie = '' if pair.endswith('=') else pair
        return response

    def get(self, path, **options):
        return self.request('GET', path, **options)

    def post(self, path, **options):
        return self.request('POST', path, **options)

    def login(self, name=None, password=PASSWORD):
        self.name = name or self.name
        return self.post('/acceso/entrar', json={'usuario': self.name, 'contrasena': password})

    def panel(self, method, path, **options):
        """A request of the panel itself, with the application token of this visitor's own instance."""
        if not self.token:
            self.token = self.get('/api/state').json()['token']
        headers = {'x-app-token': self.token, **options.pop('headers', {})}
        return self.request(method, path, headers=headers, **options)

    def upload(self, color):
        reply = self.panel('POST', '/api/upload', files=[('files', (f'ticket-{color}.png', picture(color), 'image/png'))])
        assert reply.status_code == 200, reply.text
        return reply.json()[0]['id']


def key_text(key=MASTER) -> str:
    import base64
    return base64.urlsafe_b64encode(key).decode('ascii')


def clean_env(**values):
    """os.environ without any FACTURADOR_ setting of the machine, plus the given ones."""
    kept = {name: value for name, value in os.environ.items() if not name.startswith('FACTURADOR_')}
    return patch.dict(os.environ, {**kept, **values}, clear=True)
