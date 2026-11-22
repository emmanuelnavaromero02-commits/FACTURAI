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
from html.parser import HTMLParser
from pathlib import Path
from urllib.parse import urlencode

import httpx

from .auth import read_json, write_private
from .ingest import IMAGE_EXT, MAX_UPLOAD, save_inputs

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


def _ids(client, query: str, limit: int) -> list[str]:
    response = client.get(GMAIL, params={'q': query, 'maxResults': limit})
    response.raise_for_status()
    return [item['id'] for item in response.json().get('messages', [])[:limit]
            if re.fullmatch(r'[A-Za-z0-9_-]{1,200}', item.get('id', ''))]


def _raw_message(client, message_id: str):
    if not re.fullmatch(r'[A-Za-z0-9_-]{1,200}', message_id):
        raise ValueError('Mensaje de Gmail desconocido.')
    payload = client.get(GMAIL + '/' + message_id, params={'format': 'raw'})
    payload.raise_for_status()
    raw = payload.json()['raw']
    if len(raw) > 50 * 1024 * 1024:
        return None
    return email.message_from_bytes(base64.urlsafe_b64decode(raw + '=' * (-len(raw) % 4)), policy=policy.default)


def search_tickets(root: Path, query: str) -> list[dict]:
    if not query.strip():
        raise ValueError('Escribe el comercio, remitente o reservación que quieres buscar.')
    try:
        with httpx.Client(headers={'Authorization': 'Bearer ' + access_token(root)}, timeout=30) as client:
            results = []
            for message_id in _ids(client, query.strip(), 10):
                meta = client.get(GMAIL + '/' + message_id, params={'format': 'metadata',
                                  'metadataHeaders': ['Subject', 'From', 'Date']})
                meta.raise_for_status()
                headers = {h['name'].lower(): h['value'] for h in meta.json().get('payload', {}).get('headers', [])}
                results.append({'id': message_id, 'subject': headers.get('subject', '')[:250],
                                'sender': headers.get('from', '')[:250], 'date': headers.get('date', '')[:150]})
            return results
    except httpx.HTTPError as exc:
        raise ValueError('Gmail no completó la búsqueda. Revisa la conexión y vuelve a intentar.') from exc


class _MailText(HTMLParser):
    def __init__(self):
        super().__init__()
        self.lines = []
        self.hidden = 0

    def handle_starttag(self, tag, attrs):
        if tag in {'head', 'style', 'script'}:
            self.hidden += 1
        elif tag in {'br', 'tr', 'td', 'div', 'p', 'li'}:
            self.lines.append('\n')

    def handle_endtag(self, tag):
        if tag in {'head', 'style', 'script'}:
            self.hidden = max(0, self.hidden - 1)

    def handle_data(self, data):
        if not self.hidden:
            self.lines.append(data)


def ticket_parts(root: Path, message_id: str) -> dict:
    try:
        with httpx.Client(headers={'Authorization': 'Bearer ' + access_token(root)}, timeout=30) as client:
            msg = _raw_message(client, message_id)
    except httpx.HTTPError as exc:
        raise ValueError('Gmail no pudo abrir el mensaje seleccionado.') from exc
    if msg is None:
        raise ValueError('El correo supera el límite de tamaño de importación.')
    parts = {}
    body = msg.get_body(preferencelist=('plain', 'html'))
    if body:
        try:
            content = body.get_content()
        except (LookupError, UnicodeError):
            content = (body.get_payload(decode=True) or b'').decode('utf-8', errors='replace')
        if body.get_content_type() == 'text/html':
            parser = _MailText()
            parser.feed(content)
            content = ''.join(parser.lines)
        text = '\n'.join(line.strip() for line in content.splitlines() if line.strip())[:40_000]
        if text:
            parts['body'] = ('correo-' + message_id + '.txt', text.encode('utf-8'))
    for index, part in enumerate(msg.walk()):
        filename = Path((part.get_filename() or '').replace('\\', '/')).name
        if Path(filename).suffix.lower() not in IMAGE_EXT | {'.pdf'}:
            continue
        data = part.get_payload(decode=True)
        if data and len(data) <= MAX_UPLOAD:
            parts[str(index)] = (filename, data)
    return parts


def import_tickets(store, message_id: str, selected: list[str]) -> list[dict]:
    parts = ticket_parts(store.root, message_id)
    keys = list(dict.fromkeys(selected))
    if not keys or any(key not in parts for key in keys):
        raise ValueError('Elige el texto o los adjuntos que quieres importar.')
    return save_inputs(store, [parts[key] for key in keys], 'Gmail')


def recover_attachments(root: Path, key: str, query: str) -> list[Path]:
    """Read attached PDF/XML/ZIP files. The Gmail methods in use are GET only."""
    headers = {'Authorization': 'Bearer ' + access_token(root)}
    try:
        with httpx.Client(headers=headers, timeout=30, follow_redirects=False) as client:
            ids = _ids(client, query, 20)
            target = root / 'downloads' / key / 'gmail'
            target.mkdir(parents=True, exist_ok=True, mode=0o700)
            result = []
            total = 0
            for message_id in ids:
                msg = _raw_message(client, message_id)
                if msg is None:
                    continue
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
