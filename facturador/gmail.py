from __future__ import annotations

import base64
import email
import hashlib
import json
import re
import secrets
import threading
import time
from email import policy
from pathlib import Path
from urllib.parse import urlencode

import httpx

from .auth import read_json, write_private

SCOPE = 'https://www.googleapis.com/auth/gmail.readonly'
TOKEN = 'https://oauth2.googleapis.com/token'
GMAIL = 'https://gmail.googleapis.com/gmail/v1/users/me/messages'
GMAIL_LOCK = threading.RLock()


class GmailConnection:
    def __init__(self, root: Path, port: int = 8765):
        self.root = root
        self.client = root / 'google_client.json'
        self.token = root / 'gmail_token.json'
        self.redirect = f'http://127.0.0.1:{port}/auth/gmail/callback'
        self.pending = None

    def status(self) -> str:
        return 'connected' if self.token.exists() else 'configured' if self.client.exists() else 'disconnected'

    def configure(self, raw: bytes):
        if len(raw) > 65_536:
            raise ValueError('El JSON de Google supera 64 KB.')
        try:
            client = json.loads(raw)['installed']
            if not re.fullmatch(r'[A-Za-z0-9._-]+\.apps\.googleusercontent\.com', client['client_id']):
                raise ValueError
            if not isinstance(client['client_secret'], str) or not client['client_secret']:
                raise ValueError
        except (KeyError, TypeError, UnicodeError, ValueError) as exc:
            raise ValueError('Selecciona el JSON de un cliente OAuth de escritorio de Google.') from exc
        with GMAIL_LOCK:
            write_private(self.client, {'installed': {'client_id': client['client_id'], 'client_secret': client['client_secret']}})
            self.token.unlink(missing_ok=True)
            self.pending = None

    def start(self) -> str:
        with GMAIL_LOCK:
            client = read_json(self.client, {}).get('installed')
            if not client:
                raise ValueError('Carga primero el cliente OAuth de escritorio de Google.')
            state, verifier = secrets.token_urlsafe(32), secrets.token_urlsafe(64)
            challenge = base64.urlsafe_b64encode(hashlib.sha256(verifier.encode()).digest()).decode().rstrip('=')
            self.pending = (state, verifier, time.monotonic() + 600)
            params = {'client_id': client['client_id'], 'redirect_uri': self.redirect, 'response_type': 'code',
                      'scope': SCOPE, 'access_type': 'offline', 'prompt': 'consent', 'state': state,
                      'code_challenge_method': 'S256', 'code_challenge': challenge}
            return 'https://accounts.google.com/o/oauth2/v2/auth?' + urlencode(params)

    def finish(self, state: str, code: str, error: str = ''):
        with GMAIL_LOCK:
            pending = self.pending
            if not pending or not secrets.compare_digest(state, pending[0]) or pending[2] < time.monotonic():
                raise ValueError('La solicitud de Gmail caducó.')
            self.pending = None
            if error or not code:
                raise ValueError('La autorización de Gmail no se completó.')
            client = read_json(self.client)['installed']
            tokens = exchange({'grant_type': 'authorization_code', 'redirect_uri': self.redirect, 'code': code,
                               'code_verifier': pending[1], **client})
            write_private(self.token, record_from(tokens))

    def disconnect(self):
        with GMAIL_LOCK:
            self.pending = None
            self.token.unlink(missing_ok=True)


def exchange(form: dict) -> dict:
    try:
        response = httpx.post(TOKEN, data=form, timeout=20)
        response.raise_for_status()
        return response.json()
    except (ValueError, httpx.HTTPError) as exc:
        raise ValueError('Google rechazó o no completó la conexión. Conecta Gmail desde Ajustes.') from exc


def record_from(tokens: dict, previous: dict | None = None) -> dict:
    previous = previous or {}
    if SCOPE not in (tokens.get('scope') or previous.get('scope', '')).split() or not tokens.get('access_token'):
        raise ValueError('Gmail no concedió lectura del buzón.')
    return {'token': tokens['access_token'], 'refresh_token': tokens.get('refresh_token') or previous.get('refresh_token'),
            'scope': tokens.get('scope') or previous['scope'], 'expires_at': time.time() + int(tokens.get('expires_in', 3600))}


def access_token(root: Path) -> str:
    with GMAIL_LOCK:
        path = root / 'gmail_token.json'
        credentials = read_json(path, {})
        if not credentials.get('token'):
            raise ValueError('Conecta Gmail de solo lectura desde Ajustes.')
        if credentials.get('expires_at', 0) > time.time() + 45:
            return credentials['token']
        if not credentials.get('refresh_token'):
            raise ValueError('La conexión de Gmail expiró. Vuelve a vincular la cuenta.')
        client = read_json(root / 'google_client.json', {}).get('installed')
        if not client:
            raise ValueError('Falta el cliente de escritorio de Google.')
        tokens = exchange({'grant_type': 'refresh_token', 'refresh_token': credentials['refresh_token'], **client})
        record = record_from(tokens, {**credentials, 'scope': credentials.get('scope') or SCOPE})
        write_private(path, record)
        return record['token']


def recover_attachments(root: Path, key: str, query: str) -> list[Path]:
    """Read attached PDF/XML/ZIP files. The Gmail methods in use are GET only."""
    headers = {'Authorization': 'Bearer ' + access_token(root)}
    try:
        with httpx.Client(headers=headers, timeout=30, follow_redirects=False) as client:
            response = client.get(GMAIL, params={'q': query, 'maxResults': 20})
            response.raise_for_status()
            ids = [item['id'] for item in response.json().get('messages', [])][:20]
            target = root / 'downloads' / key / 'gmail'
            target.mkdir(parents=True, exist_ok=True, mode=0o700)
            result = []
            total = 0
            for message_id in ids:
                if not re.fullmatch('[A-Za-z0-9_-]+', message_id):
                    continue
                payload = client.get(GMAIL + '/' + message_id, params={'format': 'raw'})
                payload.raise_for_status()
                raw = payload.json()['raw']
                if len(raw) > 50 * 1024 * 1024:
                    continue
                msg = email.message_from_bytes(base64.urlsafe_b64decode(raw + '=' * (-len(raw) % 4)), policy=policy.default)
                for index, part in enumerate(msg.walk()):
                    filename = part.get_filename() or ''
                    suffix = Path(filename).suffix.lower()
                    if suffix not in {'.pdf', '.xml', '.zip'}:
                        continue
                    data = part.get_payload(decode=True)
                    if not data or len(data) > 25 * 1024 * 1024:
                        continue
                    total += len(data)
                    if total > 100 * 1024 * 1024:
                        raise ValueError('La búsqueda de Gmail supera 100 MB. Acota fecha, remitente o folio.')
                    path = target / f'{message_id}_{index}{suffix}'
                    path.write_bytes(data)
                    path.chmod(0o600)
                    result.append(path)
            return result
    except httpx.HTTPError as exc:
        raise ValueError('No se pudo recuperar el correo. Revisa la conexión de Gmail y la consulta.') from exc
