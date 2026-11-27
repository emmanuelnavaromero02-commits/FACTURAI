from __future__ import annotations

import base64
import email
import hashlib
import json
import logging
import os
import re
import secrets
import threading
import time
from email import policy
from html.parser import HTMLParser
from pathlib import Path
from typing import NamedTuple
from urllib.parse import urlencode

import httpx

from . import secretos
from .auth import read_json, write_private
from .read_http import read_with_retry
from .ingest import IMAGE_EXT, MAX_UPLOAD, save_inputs, write_original

log = logging.getLogger(__name__)

BASE = Path(__file__).resolve().parent.parent
SCOPE = 'https://www.googleapis.com/auth/gmail.readonly'
# Optional and never requested by default: only to send an invoice request the owner confirmed.
SEND_SCOPE = 'https://www.googleapis.com/auth/gmail.send'
AUTHORIZE = 'https://accounts.google.com/o/oauth2/v2/auth'
TOKEN = 'https://oauth2.googleapis.com/token'
GMAIL = 'https://gmail.googleapis.com/gmail/v1/users/me/messages'
PROFILE = 'https://gmail.googleapis.com/gmail/v1/users/me/profile'
CALLBACK = '/auth/gmail/callback'
# Where Google's answer travels after «#», this beginning tells it from the name of a screen.
STATE_PREFIX = 'gmail-'
# One OAuth client for the whole installation: the JSON downloaded from Google Cloud, kept away from the data.
CLIENT_VARIABLE = 'FACTURADOR_GMAIL_CLIENTE'
CLIENT_ID = re.compile(r'[A-Za-z0-9._-]+\.apps\.googleusercontent\.com')

FIXED = 'El cliente de Google lo fija esta instalación. No se puede cambiar desde aquí.'
INSIDE = f'El archivo que indica {CLIENT_VARIABLE} debe estar fuera de la carpeta de datos.'
UNREADABLE = f'No se pudo leer el archivo que indica {CLIENT_VARIABLE}.'
NOT_A_CLIENT = (f'El archivo que indica {CLIENT_VARIABLE} no es el JSON de un cliente OAuth de Google '
                'con su identificador y su secreto.')
NEEDS_WEB = ('Con varios usuarios el cliente de Google debe ser de tipo «Aplicación web», con la dirección de '
             'regreso de este servicio.')
SHARED_BROKEN = 'El cliente de Google de esta instalación no se puede usar. '
SHARED_DOWN = 'El cliente de Google de esta instalación no se pudo leer. Avisa a quien administra FACTURAI.'
CLIENT_CHANGED = 'El cliente de Google con el que se conectó Gmail ya no está. Vuelve a vincular Gmail en Ajustes.'

_LOCKS: dict[str, threading.RLock] = {}
_GUARD = threading.Lock()
_READ: dict[str, tuple[tuple[int, int], 'Installation']] = {}
_WARNED: set[str] = set()


def _lock(root: Path) -> threading.RLock:
    """One lock per data folder: a slow answer from Google for one account never holds the others."""
    key = os.path.normcase(os.path.abspath(root))
    with _GUARD:
        return _LOCKS.setdefault(key, threading.RLock())


class GmailAuthorizationError(ValueError):
    def __init__(self, reason: str, message: str):
        super().__init__(message)
        self.reason = reason


class Attempt(NamedTuple):
    verifier: str
    deadline: float
    client_id: str


class Installation(NamedTuple):
    """The Google client of the whole installation, or why it cannot be used."""
    kind: str = ''               # «installed» (desktop application) or «web»
    client: dict | None = None   # client_id and client_secret: read into memory, never written anywhere
    problem: str = ''


def _unusable(problem: str) -> Installation:
    if problem not in _WARNED:
        _WARNED.add(problem)
        log.error('Gmail con el cliente de Google de la instalación no está disponible. %s', problem)
    return Installation(problem=problem)


def _parse(raw: bytes) -> Installation:
    try:
        data = json.loads(raw)
        kind = next(name for name in ('web', 'installed') if isinstance(data.get(name), dict))
        client = data[kind]
        if (not isinstance(client.get('client_id'), str) or not CLIENT_ID.fullmatch(client['client_id'])
                or not isinstance(client.get('client_secret'), str) or not client['client_secret']):
            raise ValueError
    except (AttributeError, StopIteration, TypeError, UnicodeError, ValueError):
        return _unusable(NOT_A_CLIENT)
    return Installation(kind, {'client_id': client['client_id'], 'client_secret': client['client_secret']})


def installation(env=None, *, data: Path | None = None) -> Installation | None:
    """The Google client of the installation, from the file that ``FACTURADOR_GMAIL_CLIENTE`` names.

    None without the variable: every data folder then keeps the client its owner imported.
    The file is read again whenever it changes, so a new secret needs no restart.
    """
    env = os.environ if env is None else env
    name = (env.get(CLIENT_VARIABLE) or '').strip()
    if not name:
        return None
    try:
        path = Path(name).expanduser().resolve()
    except (OSError, RuntimeError, ValueError):
        return _unusable(UNREADABLE)
    folders = [Path(env.get('FACTURADOR_DATA', str(BASE / 'data'))).resolve()]
    if data is not None:
        folders.append(Path(data).resolve())
    # The secret of this client never lives with what the customers keep.
    if any(path == folder or folder in path.parents for folder in folders):
        return _unusable(INSIDE)
    try:
        facts = path.stat()
        mark = (facts.st_mtime_ns, facts.st_size)
        known = _READ.get(str(path))
        if known and known[0] == mark:
            return known[1]
        if facts.st_size > 65_536:
            raise ValueError
        raw = path.read_bytes()
    except (OSError, ValueError):
        return _unusable(UNREADABLE)
    found = _parse(raw)
    _READ[str(path)] = (mark, found)
    return found


def _problem(found: Installation, public: bool) -> str:
    """Why this client cannot start a connection; nothing when it can."""
    if found.problem:
        return found.problem
    # Google returns to the public address only for a «web» client; a desktop one returns to the same computer.
    return _unusable(NEEDS_WEB).problem if public and found.kind != 'web' else ''


def offered_behind_entrance(env=None, *, data: Path | None = None) -> bool:
    """With several customers, Gmail by its API exists only with a «web» client of the whole installation."""
    found = installation(env, data=data)
    return found is not None and not _problem(found, True)


class GmailConnection:
    def __init__(self, root: Path, port: int = 8765, public_origin: str | None = None):
        self.root = root
        self.client = root / 'google_client.json'
        self.token = root / 'gmail_token.json'
        # Behind the entrance for several customers Google returns to its public address.
        self.public = bool(public_origin)
        self.redirect = (public_origin or f'http://127.0.0.1:{port}') + CALLBACK
        self.pending: dict[str, Attempt] = {}

    def shared(self) -> Installation | None:
        return installation(data=self.root)

    def status(self) -> str:
        if self.token.exists():
            return 'connected'
        shared = self.shared()
        if shared is None:
            return 'configured' if self.client.exists() else 'disconnected'
        return 'disconnected' if _problem(shared, self.public) else 'configured'

    def setup(self) -> dict:
        """How the panel offers Gmail: with an imported client, with the installation's, or not at all."""
        shared = self.shared()
        if shared is None:
            return {'mode': 'none' if self.public else 'import', 'notice': ''}
        problem = _problem(shared, self.public)
        if not problem:
            return {'mode': 'shared', 'notice': ''}
        # A customer cannot repair the installation: there the card just stays out of sight.
        return {'mode': 'none', 'notice': ''} if self.public else {'mode': 'broken', 'notice': SHARED_BROKEN + problem}

    def _client(self) -> dict:
        """The client that starts a connection: the installation's when there is one, else the imported one."""
        shared = self.shared()
        if shared is not None:
            problem = _problem(shared, self.public)
            if problem:
                raise ValueError(SHARED_BROKEN + problem)
            return shared.client
        client = secretos.read_json(self.client, {}, purpose=secretos.GMAIL_CLIENT).get('installed')
        if not client:
            raise ValueError('Carga primero el cliente OAuth de escritorio de Google.')
        return client

    def configure(self, raw: bytes):
        if self.shared() is not None:
            raise ValueError(FIXED)
        if len(raw) > 65_536:
            raise ValueError('El JSON de Google supera 64 KB.')
        try:
            client = json.loads(raw)['installed']
            if not CLIENT_ID.fullmatch(client['client_id']):
                raise ValueError
            if not isinstance(client['client_secret'], str) or not client['client_secret']:
                raise ValueError
        except (KeyError, TypeError, UnicodeError, ValueError) as exc:
            raise ValueError('Selecciona el JSON de un cliente OAuth de escritorio de Google.') from exc
        with _lock(self.root):
            existing = secretos.read_json(self.client, {}, purpose=secretos.GMAIL_CLIENT).get('installed', {})
            installed = {'client_id': client['client_id'], 'client_secret': client['client_secret']}
            if existing == installed:
                return
            if self.token.exists() and existing.get('client_id') != installed['client_id']:
                raise ValueError('Desvincula Gmail antes de cambiar de cliente OAuth.')
            secretos.write_json(self.client, {'installed': installed}, purpose=secretos.GMAIL_CLIENT)
            self.pending.clear()

    def start(self, send: bool = False) -> str:
        with _lock(self.root):
            client = self._client()
            state, verifier = STATE_PREFIX + secrets.token_urlsafe(32), secrets.token_urlsafe(64)
            challenge = base64.urlsafe_b64encode(hashlib.sha256(verifier.encode()).digest()).decode().rstrip('=')
            now = time.monotonic()
            self.pending = {s: attempt for s, attempt in self.pending.items() if attempt[1] > now}
            if len(self.pending) >= 8:
                del self.pending[next(iter(self.pending))]
            self.pending[state] = Attempt(verifier, now + 600, client['client_id'])
            params = {'client_id': client['client_id'], 'redirect_uri': self.redirect, 'response_type': 'code',
                      'scope': SCOPE + (' ' + SEND_SCOPE if send else ''), 'access_type': 'offline', 'prompt': 'consent', 'state': state,
                      'code_challenge_method': 'S256', 'code_challenge': challenge}
            return AUTHORIZE + '?' + urlencode(params)

    def finish(self, state: str, code: str, error: str = ''):
        with _lock(self.root):
            pending = self.pending.pop(state, None)
            if not pending or pending[1] <= time.monotonic():
                raise GmailAuthorizationError('caducado', 'La solicitud de Gmail caducó.')
            if error or not code:
                raise GmailAuthorizationError('consentimiento', 'La autorización de Gmail no se completó.')
            try:
                client = self._client()
            except ValueError:
                client = None
            # Only the client that started the attempt can finish it.
            if not client or client['client_id'] != pending.client_id:
                raise GmailAuthorizationError('caducado', 'La solicitud de Gmail caducó.')
            tokens = exchange({'grant_type': 'authorization_code', 'redirect_uri': self.redirect, 'code': code,
                               'code_verifier': pending.verifier, **client})
            record = record_from(tokens)
            if not record['refresh_token']:
                raise GmailAuthorizationError('sin-renovacion', 'Vuelve a autorizar el acceso sin conexión para Gmail.')
            try:
                response = httpx.get(PROFILE, headers={'Authorization': 'Bearer ' + record['token']},
                                     params={'fields': 'emailAddress'}, timeout=15)
                profile = response.raise_for_status().json()
                if not isinstance(profile, dict) or not profile.get('emailAddress'):
                    raise ValueError
            except (ValueError, httpx.HTTPError) as exc:
                raise GmailAuthorizationError('buzon', 'Verifica que Gmail API esté habilitada para el cliente y que la cuenta tenga Gmail.') from exc
            # Which client gave this permission (its identifier is not a secret): only that one renews it.
            record['client_id'] = client['client_id']
            secretos.write_json(self.token, record, purpose=secretos.GMAIL_TOKEN)
            self.pending.clear()

    def disconnect(self):
        with _lock(self.root):
            self.pending.clear()
            secretos.remove(self.token, purpose=secretos.GMAIL_TOKEN)


def exchange(form: dict) -> dict:
    try:
        response = httpx.post(TOKEN, data=form, timeout=20)
        response.raise_for_status()
        tokens = response.json()
        if not isinstance(tokens, dict):
            raise ValueError
        return tokens
    except (ValueError, httpx.HTTPError) as exc:
        raise GmailAuthorizationError('intercambio', 'Google rechazó o no completó la conexión. Conecta Gmail desde Ajustes.') from exc


def record_from(tokens: dict, previous: dict | None = None) -> dict:
    previous = previous or {}
    scope = tokens.get('scope') or previous.get('scope', '')
    if not isinstance(scope, str) or SCOPE not in scope.split() or not isinstance(tokens.get('access_token'), str) or not tokens['access_token']:
        raise GmailAuthorizationError('permiso', 'Gmail no concedió lectura del buzón.')
    try:
        lifetime = int(tokens.get('expires_in', 3600))
        if lifetime <= 0:
            raise ValueError
    except (TypeError, ValueError, OverflowError) as exc:
        raise GmailAuthorizationError('intercambio', 'Google devolvió una vigencia de autorización inválida.') from exc
    record = {'token': tokens['access_token'], 'refresh_token': tokens.get('refresh_token') or previous.get('refresh_token'),
              'scope': scope, 'expires_at': time.time() + lifetime}
    if previous.get('client_id'):
        record['client_id'] = previous['client_id']
    return record


def _issuer(root: Path, credentials: dict) -> dict:
    """The OAuth client that gave the saved permission: Google renews it only for that one."""
    shared = installation(data=root)
    issued = credentials.get('client_id')
    if shared is not None and shared.client and shared.client['client_id'] == issued:
        return shared.client
    own = secretos.read_json(root / 'google_client.json', {}, purpose=secretos.GMAIL_CLIENT).get('installed')
    if not issued:
        # Saved before an installation could have its own client: it came from the imported one.
        client = own or (shared.client if shared is not None else None)
        if not client:
            raise ValueError('Falta el cliente de escritorio de Google.')
        return client
    if own and own.get('client_id') == issued:
        return own
    raise ValueError(SHARED_DOWN if shared is not None and not shared.client else CLIENT_CHANGED)


def access_token(root: Path) -> str:
    with _lock(root):
        path = root / 'gmail_token.json'
        credentials = secretos.read_json(path, {}, purpose=secretos.GMAIL_TOKEN)
        if not credentials.get('token'):
            raise ValueError('Conecta Gmail de solo lectura desde Ajustes.')
        if credentials.get('expires_at', 0) > time.time() + 45:
            return credentials['token']
        if not credentials.get('refresh_token'):
            raise ValueError('La conexión de Gmail expiró. Vuelve a vincular la cuenta.')
        client = _issuer(root, credentials)
        tokens = exchange({'grant_type': 'refresh_token', 'refresh_token': credentials['refresh_token'], **client})
        record = record_from(tokens, {**credentials, 'scope': credentials.get('scope') or SCOPE})
        secretos.write_json(path, record, purpose=secretos.GMAIL_TOKEN)
        return record['token']


def _get(client, url, params):
    return read_with_retry(lambda: client.get(url, params=params).raise_for_status())


def _ids(client, query: str, limit: int) -> list[str]:
    response = _get(client, GMAIL, {'q': query, 'maxResults': limit})
    return [item['id'] for item in response.json().get('messages', [])[:limit]
            if re.fullmatch(r'[A-Za-z0-9_-]{1,200}', item.get('id', ''))]


def _raw_message(client, message_id: str):
    if not re.fullmatch(r'[A-Za-z0-9_-]{1,200}', message_id):
        raise ValueError('Mensaje de Gmail desconocido.')
    payload = _get(client, GMAIL + '/' + message_id, {'format': 'raw'})
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
                meta = _get(client, GMAIL + '/' + message_id, {'format': 'metadata',
                                  'metadataHeaders': ['Subject', 'From', 'Date']})
                metadata = meta.json()
                headers = {h['name'].lower(): h['value'] for h in metadata.get('payload', {}).get('headers', [])}
                results.append({'id': message_id, 'subject': headers.get('subject', '')[:250],
                                'sender': headers.get('from', '')[:250], 'date': headers.get('date', '')[:150],
                                'received_ms': int(metadata.get('internalDate') or 0)})
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


def read_selected_message(root: Path, message_id: str):
    try:
        with httpx.Client(headers={'Authorization': 'Bearer ' + access_token(root)}, timeout=30) as client:
            msg = _raw_message(client, message_id)
    except httpx.HTTPError as exc:
        raise ValueError('Gmail no pudo abrir el mensaje seleccionado.') from exc
    if msg is None:
        raise ValueError('El correo supera el límite de tamaño de importación.')
    return msg


def ticket_parts(root: Path, message_id: str) -> dict:
    msg = read_selected_message(root, message_id)
    return receipt_mail_parts(msg, message_id)


def receipt_mail_parts(msg, message_id: str) -> dict:
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


def parse_eml(data: bytes):
    if not data or len(data) > MAX_UPLOAD:
        raise ValueError('El correo .eml debe pesar hasta 25 MB.')
    msg = email.message_from_bytes(data, policy=policy.default)
    if not any(msg.get(field) for field in ('From', 'Subject', 'Message-ID', 'Date')):
        raise ValueError('El archivo no contiene un mensaje de correo reconocible.')
    return msg


def cfdi_parts(msg):
    for part in msg.walk():
        filename = Path((part.get_filename() or '').replace('\\', '/')).name
        if Path(filename).suffix.lower() in {'.pdf', '.xml', '.zip'}:
            data = part.get_payload(decode=True)
            if data and len(data) <= MAX_UPLOAD:
                yield filename, data


def import_eml(store, name: str, data: bytes) -> list[dict]:
    msg = parse_eml(data)
    parts = receipt_mail_parts(msg, hashlib.sha256(data).hexdigest()[:12])
    if not parts:
        raise ValueError('El correo no contiene texto ni fotos o PDF de tickets.')
    return save_inputs(store, list(parts.values()), 'EML: ' + Path(name).name[:80])


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
                for filename, data in cfdi_parts(msg):
                    suffix = Path(filename).suffix.lower()
                    total += len(data)
                    if total > 100 * 1024 * 1024:
                        raise ValueError('La búsqueda de Gmail supera 100 MB. Acota fecha, remitente o folio.')
                    path = target / (hashlib.sha256(data).hexdigest() + suffix)
                    write_original(path, data)
                    result.append(path)
            return result
    except httpx.HTTPError as exc:
        raise ValueError('No se pudo recuperar el correo. Revisa la conexión de Gmail y la consulta.') from exc


def can_send(root: Path) -> bool:
    """True only when the owner granted the optional Gmail send permission."""
    scope = secretos.read_json(root / 'gmail_token.json', {}, purpose=secretos.GMAIL_TOKEN).get('scope', '')
    return isinstance(scope, str) and SEND_SCOPE in scope.split()


def send_raw(root: Path, data: bytes) -> str:
    """Send one already composed message. A single POST: it is never retried automatically."""
    if not can_send(root):
        raise ValueError('Gmail no tiene permiso de envío. Usa una cuenta de correo con envío.')
    raw = base64.urlsafe_b64encode(data).decode('ascii')
    response = httpx.post(GMAIL + '/send', headers={'Authorization': 'Bearer ' + access_token(root)},
                          json={'raw': raw}, timeout=60, follow_redirects=False)
    response.raise_for_status()
    sent = response.json()
    return str(sent.get('id', ''))[:200] if isinstance(sent, dict) else ''
