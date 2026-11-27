"""Standard Web Push (VAPID): one key pair per installation and one subscription per device.

The private key is written once to a private file in the data folder. It never leaves this module:
no route, record or message carries it. Only the public key is offered to the panel.
"""
from __future__ import annotations

import base64
import hashlib
import json
import logging
import os
import re
import threading
import time
from pathlib import Path
from urllib.parse import urlsplit

from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import ec

from . import secretos
from .auth import read_json, write_private  # noqa: F401 - kept as patch points
from .db import now

_LOG = logging.getLogger(__name__)
KEY_LOCK = threading.Lock()
MAX_DEVICES = 20
# Push services of the browsers people use. The server only posts to these hosts.
PUSH_HOSTS = ('fcm.googleapis.com', 'android.googleapis.com', 'push.services.mozilla.com',
              'push.apple.com', 'notify.windows.com')
CONTACT = 'mailto:avisos@facturai.invalid'
LABEL = re.compile(r"[\w .,()/+-]{1,60}")
GONE = (404, 410)


def _b64(data: bytes) -> str:
    return base64.urlsafe_b64encode(data).decode('ascii').rstrip('=')


def _unb64(value: str) -> bytes:
    if not isinstance(value, str) or not re.fullmatch(r'[A-Za-z0-9_-]{1,200}={0,2}', value):
        raise ValueError
    return base64.urlsafe_b64decode(value.rstrip('=') + '=' * (-len(value.rstrip('=')) % 4))


def _key_path(root) -> Path:
    return Path(root) / 'auth' / 'avisos' / 'vapid.json'


def create_tables(db):
    db.execute('''CREATE TABLE IF NOT EXISTS avisos_dispositivos (
        id TEXT PRIMARY KEY, endpoint TEXT NOT NULL UNIQUE, p256dh TEXT NOT NULL, auth TEXT NOT NULL,
        nombre TEXT NOT NULL DEFAULT '', creado TEXT NOT NULL, ultimo_envio TEXT NOT NULL DEFAULT '',
        fallos INTEGER NOT NULL DEFAULT 0)''')


def _keys(root) -> dict:
    """Read the key pair, creating it on the first call. Generated exactly once per data folder."""
    path = _key_path(root)
    with KEY_LOCK:
        # With a master key configured the private key is stored sealed.
        record = secretos.read_json(path, None, purpose=secretos.PUSH_KEYS)
        if isinstance(record, dict) and record.get('private_pem') and record.get('public'):
            return record
        private = ec.generate_private_key(ec.SECP256R1())
        pem = private.private_bytes(serialization.Encoding.PEM, serialization.PrivateFormat.PKCS8,
                                    serialization.NoEncryption()).decode('ascii')
        public = private.public_key().public_bytes(serialization.Encoding.X962,
                                                   serialization.PublicFormat.UncompressedPoint)
        record = {'private_pem': pem, 'public': _b64(public), 'created': int(time.time())}
        secretos.write_json(path, record, purpose=secretos.PUSH_KEYS)
        try:
            path.chmod(0o600)
        except OSError:
            pass
        return record


def public_key(root) -> str:
    """The applicationServerKey for PushManager.subscribe (base64url, uncompressed P-256 point)."""
    return _keys(root)['public']


def _contact() -> str:
    value = os.getenv('FACTURADOR_AVISOS_CONTACTO', '').strip()
    return value if re.fullmatch(r'(mailto:[^\s@]+@[^\s@]+\.[^\s@]+|https://[^\s]+)', value or '-') else CONTACT


def _allowed_hosts() -> tuple[str, ...]:
    extra = tuple(h.strip().lower() for h in os.getenv('FACTURADOR_PUSH_HOSTS', '').split(',') if h.strip())
    return PUSH_HOSTS + extra


def valid_endpoint(value: str) -> str:
    try:
        parts = urlsplit(str(value or ''))
    except ValueError:
        raise ValueError('El navegador entregó una suscripción no válida.') from None
    host = (parts.hostname or '').lower()
    if (len(value) > 2048 or parts.scheme != 'https' or parts.port not in (None, 443) or parts.username
            or parts.password or not any(host == h or host.endswith('.' + h) for h in _allowed_hosts())):
        raise ValueError('Este navegador usa un servicio de avisos que no se reconoce.')
    return value


def device_id(endpoint: str) -> str:
    return hashlib.sha256(endpoint.encode()).hexdigest()[:24]


class PushDevices:
    """Subscriptions of this person's own browsers and phones, and the delivery to them."""

    def __init__(self, store, send=None):
        self.store = store
        self.root = store.root
        # Injected in tests; by default pywebpush.webpush is resolved at call time.
        self._send = send
        with store.connect() as db:
            create_tables(db)

    # ------------------------------------------------------------ subscriptions

    def subscribe(self, endpoint: str, p256dh: str, auth: str, name: str = '') -> dict:
        endpoint = valid_endpoint(endpoint)
        try:
            point, secret = _unb64(p256dh), _unb64(auth)
        except (ValueError, TypeError):
            raise ValueError('El navegador entregó llaves de aviso no válidas.') from None
        if len(point) != 65 or point[0] != 4 or len(secret) != 16:
            raise ValueError('El navegador entregó llaves de aviso no válidas.')
        name = ' '.join(str(name or '').split())[:60]
        if name and not LABEL.fullmatch(name):
            name = ''
        key = device_id(endpoint)
        with self.store.connect() as db:
            db.execute('BEGIN IMMEDIATE')
            db.execute('''INSERT INTO avisos_dispositivos(id,endpoint,p256dh,auth,nombre,creado) VALUES(?,?,?,?,?,?)
                ON CONFLICT(id) DO UPDATE SET p256dh=excluded.p256dh,auth=excluded.auth,
                nombre=CASE WHEN excluded.nombre<>'' THEN excluded.nombre ELSE nombre END,fallos=0''',
                       (key, endpoint, p256dh, auth, name, now()))
            # Keep only the newest devices; an old browser that was never removed stops receiving.
            db.execute('''DELETE FROM avisos_dispositivos WHERE id NOT IN
                (SELECT id FROM avisos_dispositivos ORDER BY creado DESC, id LIMIT ?)''', (MAX_DEVICES,))
        return {'id': key, 'nombre': name}

    def remove(self, key: str = '', endpoint: str = '') -> bool:
        if not key and endpoint:
            key = device_id(str(endpoint))
        if not re.fullmatch(r'[0-9a-f]{24}', key or ''):
            return False
        with self.store.connect() as db:
            return db.execute('DELETE FROM avisos_dispositivos WHERE id=?', (key,)).rowcount == 1

    def devices(self) -> list[dict]:
        """Public list for the panel: never the endpoint or the device keys."""
        with self.store.connect() as db:
            rows = db.execute('SELECT id,nombre,creado,ultimo_envio,fallos FROM avisos_dispositivos '
                              'ORDER BY creado DESC, id').fetchall()
        return [dict(row) for row in rows]

    def count(self) -> int:
        with self.store.connect() as db:
            return db.execute('SELECT COUNT(*) FROM avisos_dispositivos').fetchone()[0]

    # ------------------------------------------------------------ delivery

    def _sender(self):
        if self._send is not None:
            return self._send
        from pywebpush import webpush
        return webpush

    def send(self, payload: dict, *, urgent: bool = False, only: str = '') -> dict:
        """Deliver one short payload to every device. Never raises: a failure only counts."""
        result = {'enviados': 0, 'fallidos': 0, 'quitados': 0}
        rows = []
        try:
            with self.store.connect() as db:
                rows = [dict(r) for r in db.execute('SELECT * FROM avisos_dispositivos ORDER BY creado, id')]
            if only:
                rows = [r for r in rows if r['id'] == only]
            if not rows:
                return result
            from py_vapid import Vapid
            import requests
            vapid = Vapid.from_pem(_keys(self.root)['private_pem'].encode('ascii'))
            data = json.dumps(payload, ensure_ascii=False, separators=(',', ':'))
            sender = self._sender()
        except Exception:
            _LOG.warning('No se pudieron preparar los avisos del navegador.')
            result['fallidos'] = len(rows)
            return result
        for row in rows:
            session = requests.Session()
            session.max_redirects = 0
            try:
                sender(subscription_info={'endpoint': row['endpoint'],
                                          'keys': {'p256dh': row['p256dh'], 'auth': row['auth']}},
                       data=data, vapid_private_key=vapid, vapid_claims={'sub': _contact()},
                       ttl=12 * 3600, timeout=10, headers={'Urgency': 'high' if urgent else 'normal'},
                       requests_session=session)
            except Exception as exc:
                status = getattr(getattr(exc, 'response', None), 'status_code', None)
                with self.store.connect() as db:
                    if status in GONE:
                        # The push service says this subscription no longer exists.
                        db.execute('DELETE FROM avisos_dispositivos WHERE id=?', (row['id'],))
                        result['quitados'] += 1
                    else:
                        db.execute('UPDATE avisos_dispositivos SET fallos=fallos+1 WHERE id=?', (row['id'],))
                        result['fallidos'] += 1
                _LOG.warning('Un dispositivo no recibió el aviso (estado %s).', status or 'sin respuesta')
                continue
            finally:
                session.close()
            with self.store.connect() as db:
                db.execute('UPDATE avisos_dispositivos SET ultimo_envio=?,fallos=0 WHERE id=?', (now(), row['id']))
            result['enviados'] += 1
        return result
