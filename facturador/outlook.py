"""Microsoft sign-in for Outlook.com, Hotmail, Live and Microsoft 365 mailboxes.

Microsoft's mail servers no longer take a password: IMAP and SMTP are opened with a short-lived
access token (SASL XOAUTH2) obtained with the OAuth 2.0 authorization code flow and PKCE. The
application is a public client, so there is no client secret unless the installation registered
it as a web application. Tokens are kept encrypted like the other mail secrets; they are never
returned by the API, logged or attached to events.
"""
from __future__ import annotations

import base64
import hashlib
import json
import logging
import os
import re
import secrets
import threading
import time
from pathlib import Path
from typing import NamedTuple
from urllib.parse import urlencode

import httpx
import jwt
from cryptography.fernet import Fernet, InvalidToken

from . import secretos
from .auth import read_json, write_private

log = logging.getLogger(__name__)

# Microsoft identity platform (v2.0). «common» accepts personal accounts and those of any organization.
AUTHORIZE = 'https://login.microsoftonline.com/{tenant}/oauth2/v2.0/authorize'
TOKEN = 'https://login.microsoftonline.com/{tenant}/oauth2/v2.0/token'
IMAP_SCOPE = 'https://outlook.office.com/IMAP.AccessAsUser.All'
# Optional and never requested by default: only to send what the owner confirmed.
SEND_SCOPE = 'https://outlook.office.com/SMTP.Send'
# Who signed in (to know which mailbox it is) and a permission that can be renewed.
IDENTITY_SCOPES = ('openid', 'email', 'profile', 'offline_access')
RENEWAL_SCOPES = ('openid', 'profile', 'offline_access')
IMAP_SERVER = ('outlook.office365.com', 993)
# Submission on 587 with STARTTLS. Outlook.com documents one name and Microsoft 365 another.
SMTP_SERVERS = {'personal': ('smtp-mail.outlook.com', 587), 'trabajo': ('smtp.office365.com', 587)}
# Tenant of every personal Microsoft account (the «tid» of its identity token).
PERSONAL_TENANT = '9188040d-6c67-4c5b-b112-36a304b66dad'
CALLBACK = '/auth/outlook/callback'
STATE_PREFIX = 'outlook-'

CLIENT_VARIABLE = 'FACTURADOR_OUTLOOK_APLICACION'
TENANT_VARIABLE = 'FACTURADOR_OUTLOOK_INQUILINO'
SECRET_VARIABLE = 'FACTURADOR_OUTLOOK_SECRETO'
CLIENT_ID = re.compile(r'[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}')
TENANT = re.compile(r'common|consumers|organizations|' + CLIENT_ID.pattern +
                    r'|(?=.{4,253}$)[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?(?:\.[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?)+')
ADDRESS = re.compile(r'[A-Za-z0-9._%+-]{1,64}@[A-Za-z0-9-]{1,63}(?:\.[A-Za-z0-9-]{1,63})*\.[A-Za-z]{2,63}')
PRINTABLE = re.compile(r'[\x21-\x7e]{1,16384}')
MARGIN = 120          # Seconds before the expiry at which the access token is renewed.
FORCE_EVERY = 300     # An early renewal, asked because the mail server refused the token, at most this often.
RECONNECT_STATE = 'reconnect'
FIELDS = ('address', 'connected_at', 'scope', 'send', 'expires_at', 'personal', 'authority', 'client_id', 'tenant',
          'state')

NOT_CONNECTED = 'Conecta tu correo de Outlook en Ajustes.'
RECONNECT = ('Microsoft dejó de renovar el permiso de tu correo de Outlook: se retiró o caducó. '
             'Vuelve a conectarlo en Ajustes.')
UNREADABLE = 'No se pudo abrir la conexión guardada de Outlook. Vuelve a conectarlo en Ajustes.'
RENEWAL = 'Microsoft no renovó el permiso de tu correo de Outlook. Inténtalo de nuevo en un momento.'
NEEDS_APPLICATION = 'Guarda primero el identificador de tu aplicación de Microsoft.'
NOT_OFFERED = 'Este servicio todavía no ofrece conectar Outlook y Hotmail.'
FIXED = 'La aplicación de Microsoft la fija el servidor. No se puede cambiar desde aquí.'
BAD_CLIENT = ('Pega el «Identificador de aplicación (cliente)» tal como lo muestra Microsoft: '
              '36 letras y números separados por guiones.')
CHANGE_CLIENT = 'Desconecta Outlook antes de cambiar de aplicación.'
REASONS = {
    'caducado': 'La solicitud de conexión caducó. Pulsa Conectar otra vez.',
    'consentimiento': 'No se completó la autorización en Microsoft. Puedes intentarlo de nuevo cuando quieras.',
    'intercambio': ('Microsoft no aceptó la conexión. Vuelve a intentarlo; si se repite, hay que revisar el '
                    'identificador de la aplicación y la dirección de regreso registrados en Microsoft.'),
    'aplicacion': ('Microsoft no reconoce la aplicación de esta instalación. Hay que revisar su identificador '
                   'y cómo se registró.'),
    'sin-respuesta': 'Microsoft no respondió. Inténtalo de nuevo en un momento.',
    'sin-renovacion': 'Microsoft no dio un permiso que se pueda renovar. Vuelve a conectar y acepta mantener el acceso.',
    'permiso': 'Microsoft no concedió leer el buzón. Vuelve a conectar y acepta el permiso.',
    'cuenta': 'Microsoft no indicó el correo de la cuenta. Vuelve a conectar y elige tu cuenta de correo.',
    'buzon': ('Microsoft autorizó la cuenta, pero su buzón no abrió. En Outlook entra a Configuración → Correo → '
              'Reenvío e IMAP, activa «Permitir que los dispositivos y las aplicaciones usen IMAP» y vuelve a conectar.'),
    'revocado': RECONNECT,
}
# Answers of the token endpoint, by what can be done about them.
TEMPORARY_ERRORS = {'temporarily_unavailable', 'server_error'}
REVOKED_ERRORS = {'invalid_grant', 'interaction_required', 'consent_required', 'login_required'}
APPLICATION_ERRORS = {'invalid_client', 'unauthorized_client'}

_LOCKS: dict[str, threading.RLock] = {}
_GUARD = threading.Lock()
_FORCED: dict[str, float] = {}
_WARNED: set[str] = set()


class OutlookAuthorizationError(ValueError):
    def __init__(self, reason: str, message: str = ''):
        super().__init__(message or REASONS.get(reason) or REASONS['intercambio'])
        self.reason = reason


class ReconnectNeeded(ValueError):
    """Microsoft will not renew the permission any more: only the person can give it again."""


class Attempt(NamedTuple):
    verifier: str
    deadline: float
    scope: str
    client_id: str
    tenant: str


# ---------------------------------------------------------------- application

def installation(env=None) -> dict | None:
    """The application registered once for the whole installation, read from the environment."""
    env = os.environ if env is None else env
    client = (env.get(CLIENT_VARIABLE) or '').strip().lower()
    if not client:
        return None
    if not CLIENT_ID.fullmatch(client):
        if CLIENT_VARIABLE not in _WARNED:
            _WARNED.add(CLIENT_VARIABLE)
            log.warning('%s no es un identificador de aplicación de Microsoft; se ignora.', CLIENT_VARIABLE)
        return None
    return {'client_id': client, 'tenant': tenant(env)}


def tenant(env=None) -> str:
    env = os.environ if env is None else env
    value = (env.get(TENANT_VARIABLE) or '').strip().lower()
    if not value:
        return 'common'
    if not TENANT.fullmatch(value):
        if TENANT_VARIABLE not in _WARNED:
            _WARNED.add(TENANT_VARIABLE)
            log.warning('%s no es un inquilino de Microsoft válido; se usa «common».', TENANT_VARIABLE)
        return 'common'
    return value


def _client_secret(client_id: str, env=None) -> str:
    """Only for an installation that registered its application as a web one. It lives in the environment alone."""
    env = os.environ if env is None else env
    fixed = installation(env)
    return (env.get(SECRET_VARIABLE) or '').strip() if fixed and fixed['client_id'] == client_id else ''


# ---------------------------------------------------------------- encrypted record

def _folder(root) -> Path:
    return Path(root) / 'auth' / 'correo'


def _record_path(root) -> Path:
    return _folder(root) / 'outlook.json'


def _key_path(root) -> Path:
    return _folder(root) / 'outlook-key.json'


def _application_path(root) -> Path:
    return _folder(root) / 'outlook-aplicacion.json'


def _lock(root) -> threading.RLock:
    key = str(Path(root).resolve())
    with _GUARD:
        return _LOCKS.setdefault(key, threading.RLock())


def _vault(root, create: bool) -> Fernet:
    key = secretos.read_json(_key_path(root), None, purpose=secretos.MAIL_VAULT)
    if key is None:
        if not create:
            raise ValueError(UNREADABLE)
        key = {'key': Fernet.generate_key().decode('ascii')}
        secretos.write_json(_key_path(root), key, purpose=secretos.MAIL_VAULT)
    return Fernet(key['key'].encode('ascii'))


def _stored(root) -> dict | None:
    try:
        record = read_json(_record_path(root), None)
    except (OSError, ValueError):
        return None
    if not isinstance(record, dict) or not isinstance(record.get('address'), str) or not ADDRESS.fullmatch(record['address']):
        return None
    # A file changed by hand counts as no connection; it never breaks the settings screen.
    numbers = [record.get(name, 0) for name in ('expires_at', 'connected_at')]
    texts = [record.get(name, '') for name in ('scope', 'authority', 'client_id', 'tenant', 'state', 'secret')]
    if any(isinstance(value, bool) or not isinstance(value, (int, float)) for value in numbers) or \
            any(not isinstance(value, str) for value in texts):
        return None
    return record


def account(root) -> dict | None:
    """What may be known about the connected mailbox. The tokens are never part of the result."""
    record = _stored(root)
    return {key: record[key] for key in FIELDS if key in record} if record else None


def connected(root) -> bool:
    record = _stored(root)
    return bool(record and record.get('secret') and record.get('state') != RECONNECT_STATE)


def needs_reconnect(root) -> bool:
    record = _stored(root)
    return bool(record and record.get('state') == RECONNECT_STATE)


def can_send(root) -> bool:
    """True only when the person gave the optional permission to send and the outgoing server accepted it."""
    record = _stored(root)
    return bool(record and record.get('send') is True and record.get('secret') and record.get('state') != RECONNECT_STATE)


def _load(root) -> tuple[dict, dict]:
    record = _stored(root)
    if record and record.get('state') == RECONNECT_STATE:
        raise ReconnectNeeded(RECONNECT)
    if not record or not record.get('secret'):
        raise ValueError(NOT_CONNECTED)
    try:
        tokens = json.loads(_vault(root, create=False).decrypt(record['secret'].encode('ascii')))
        if not isinstance(tokens, dict) or not all(isinstance(tokens.get(k), str) and tokens[k] for k in ('access', 'refresh')):
            raise ValueError
    except secretos.SealedSecrets:
        raise  # A master key problem is not solved by connecting the account again.
    except (InvalidToken, UnicodeError, ValueError):
        raise ValueError(UNREADABLE) from None
    return record, tokens


def _save(root, record: dict, tokens: dict) -> None:
    vault = _vault(root, create=True)
    stored = {key: record[key] for key in FIELDS if key in record}
    stored['secret'] = vault.encrypt(json.dumps(tokens).encode('utf-8')).decode('ascii')
    write_private(_record_path(root), stored)


def _mark_reconnect(root, record: dict) -> None:
    """Keep which mailbox it was; the tokens Microsoft no longer honours are thrown away."""
    stored = {key: record[key] for key in FIELDS if key in record}
    stored.update(state=RECONNECT_STATE, send=False)
    write_private(_record_path(root), stored)


def remove(root) -> None:
    with _lock(root):
        secretos.check(_key_path(root), purpose=secretos.MAIL_VAULT)
        _record_path(root).unlink(missing_ok=True)
        _key_path(root).unlink(missing_ok=True)
        _FORCED.pop(str(Path(root).resolve()), None)


# ---------------------------------------------------------------- tokens

def _token_request(tenant_name: str, form: dict) -> dict:
    """One call to Microsoft's token endpoint. Neither the form nor the answer is ever logged or raised."""
    try:
        response = httpx.post(TOKEN.format(tenant=tenant_name), data=form, timeout=20)
        body = response.json()
    except (httpx.HTTPError, ValueError):
        raise OutlookAuthorizationError('sin-respuesta') from None
    if not isinstance(body, dict):
        raise OutlookAuthorizationError('sin-respuesta')
    if response.is_success and isinstance(body.get('access_token'), str) and PRINTABLE.fullmatch(body['access_token']):
        return body
    error = body.get('error') if isinstance(body.get('error'), str) else ''
    if response.status_code >= 500 or error in TEMPORARY_ERRORS:
        raise OutlookAuthorizationError('sin-respuesta')
    if error in REVOKED_ERRORS:
        raise OutlookAuthorizationError('revocado')
    if error in APPLICATION_ERRORS:
        raise OutlookAuthorizationError('aplicacion')
    raise OutlookAuthorizationError('intercambio')


def _granted(tokens: dict, requested: str) -> tuple[str, bool]:
    """The mail permissions of an answer, in our own spelling. Microsoft may omit them or change their case."""
    scope = tokens.get('scope')
    given = {item.lower() for item in (scope if isinstance(scope, str) and scope.strip() else requested).split()}
    if IMAP_SCOPE.lower() not in given:
        raise OutlookAuthorizationError('permiso')
    sends = SEND_SCOPE.lower() in given
    return IMAP_SCOPE + (' ' + SEND_SCOPE if sends else ''), sends


def _expiry(tokens: dict) -> float:
    try:
        lifetime = int(tokens.get('expires_in', 3600))
        if not 0 < lifetime <= 7 * 86400:
            raise ValueError
    except (TypeError, ValueError, OverflowError):
        raise OutlookAuthorizationError('intercambio') from None
    return time.time() + lifetime


def _refresh_token(tokens: dict, previous: str = '') -> str:
    value = tokens.get('refresh_token')
    return value if isinstance(value, str) and value else previous


def access_token(root, *, force: bool = False) -> str:
    """A valid access token for the connected mailbox, renewed when it is about to expire.

    ``force`` asks for an early renewal because the mail server refused the current token.
    """
    root = Path(root)
    key = str(root.resolve())
    with _lock(root):
        record, tokens = _load(root)
        now = time.time()
        fresh = record.get('expires_at', 0) > now + MARGIN
        if force:
            if now - _FORCED.get(key, float('-inf')) < FORCE_EVERY:
                return tokens['access']
            _FORCED[key] = now
        elif fresh:
            return tokens['access']
        client_id, tenant_name = record.get('client_id', ''), record.get('tenant') or 'common'
        if not CLIENT_ID.fullmatch(client_id) or not TENANT.fullmatch(tenant_name):
            raise ValueError(UNREADABLE)
        form = {'grant_type': 'refresh_token', 'client_id': client_id, 'refresh_token': tokens['refresh'],
                'scope': ' '.join((record.get('scope') or IMAP_SCOPE, *RENEWAL_SCOPES))}
        secret = _client_secret(client_id)
        if secret:
            form['client_secret'] = secret
        try:
            answer = _token_request(tenant_name, form)
            scope, sends = _granted(answer, form['scope'])
            expires_at = _expiry(answer)
        except OutlookAuthorizationError as exc:
            if exc.reason in {'revocado', 'permiso'}:
                _mark_reconnect(root, record)
                raise ReconnectNeeded(RECONNECT) from None
            if exc.reason == 'sin-respuesta' and record.get('expires_at', 0) > now:
                return tokens['access']  # Still good for a moment; the next use tries again.
            raise ValueError(REASONS[exc.reason] if exc.reason in {'aplicacion', 'sin-respuesta'} else RENEWAL) from None
        # Microsoft hands a new renewal token with every use: the one just spent is replaced right away.
        _save(root, {**record, 'scope': scope, 'send': bool(record.get('send')) and sends, 'expires_at': expires_at,
                     'state': 'ok'},
              {'access': answer['access_token'], 'refresh': _refresh_token(answer, tokens['refresh'])})
        return answer['access_token']


def sasl(user: str, token: str):
    """SASL XOAUTH2 for imaplib and smtplib: the credentials first, then an empty line to close a refusal."""
    if not ADDRESS.fullmatch(user or '') or not PRINTABLE.fullmatch(token or ''):
        raise ValueError(NOT_CONNECTED)
    message = f'user={user}\x01auth=Bearer {token}\x01\x01'
    sent = []

    def answer(challenge=None):
        if sent:
            return ''
        sent.append(True)
        return message
    return answer


# ---------------------------------------------------------------- mail servers

def settings_for(address: str, personal: bool, authority: str = '') -> dict:
    """Microsoft's servers for one mailbox, in the shape the IMAP and SMTP classes use."""
    imap_host, imap_port = IMAP_SERVER
    smtp_host, smtp_port = SMTP_SERVERS['personal' if personal else 'trabajo']
    return {'imap_host': imap_host, 'imap_port': imap_port, 'smtp_host': smtp_host, 'smtp_port': smtp_port,
            'smtp_tls': 'starttls', 'user': address, 'address': address, 'authority': authority}


def mail_account(root) -> dict | None:
    """Servers and address of the connected mailbox, or None while it is not usable."""
    record = _stored(root)
    if not record or not record.get('secret') or record.get('state') == RECONNECT_STATE:
        return None
    return settings_for(record['address'], record.get('personal') is not False, record.get('authority') or '')


def _open_mailbox(addresses: list[str], token: str, *, send: bool, personal: bool) -> tuple[str, str, bool]:
    from . import buzon  # buzon imports this module; it is complete by the time a connection finishes.
    try:
        return buzon.check_outlook(addresses, token, send=send, personal=personal)
    except buzon.LoginRefused:
        raise OutlookAuthorizationError('buzon') from None
    except ValueError as exc:
        raise OutlookAuthorizationError('correo', str(exc)) from None


def _addresses(claims: dict) -> list[str]:
    """The names Microsoft gives for the account that can be a mailbox address, without repeats."""
    found = []
    for name in ('email', 'preferred_username'):
        value = claims.get(name)
        value = value.strip() if isinstance(value, str) else ''
        if len(value) <= 254 and ADDRESS.fullmatch(value) and value.lower() not in {item.lower() for item in found}:
            found.append(value)
    return found


def _identity(token, client_id: str) -> dict:
    """Claims of the identity token.

    It arrives straight from Microsoft's token endpoint over verified TLS, so its signature is not
    checked again (OpenID Connect Core 3.1.3.7). It only suggests which mailbox to open: the mail
    server decides whether the access token really belongs to that mailbox.
    """
    try:
        claims = jwt.decode(token, options={'verify_signature': False})
    except (jwt.PyJWTError, TypeError, ValueError):
        return {}
    if not isinstance(claims, dict):
        return {}
    audience = claims.get('aud')
    if audience is not None and audience != client_id and audience != [client_id]:
        raise OutlookAuthorizationError('intercambio')
    return claims


# ---------------------------------------------------------------- connection

class OutlookConnection:
    """Connects one data folder to a Microsoft mailbox. The page returns here with the result after «#»."""

    def __init__(self, root, port: int = 8765, public_origin: str | None = None, *, env=None, probe=None):
        self.root = Path(root)
        self.env = env
        # Behind the entrance only the installation's application is offered: nobody registers one there.
        self.public = bool(public_origin)
        # Microsoft ignores the port of a «localhost» address when it compares it with the registered one.
        self.redirect = (public_origin.rstrip('/') if public_origin else f'http://localhost:{port}') + CALLBACK
        self.probe = probe or _open_mailbox
        self.pending: dict[str, Attempt] = {}
        self._guard = threading.Lock()

    # ------------------------------------------------------------ application

    @property
    def editable(self) -> bool:
        return not self.public and installation(self.env) is None

    def _saved_client(self) -> str:
        try:
            saved = read_json(_application_path(self.root), None)
        except (OSError, ValueError):
            return ''
        value = saved.get('client_id') if isinstance(saved, dict) else ''
        return value if isinstance(value, str) and CLIENT_ID.fullmatch(value) else ''

    def client(self) -> dict | None:
        """The application in use: the installation's, or the one the owner pasted in the local mode."""
        fixed = installation(self.env)
        if fixed:
            return fixed
        saved = '' if self.public else self._saved_client()
        return {'client_id': saved, 'tenant': tenant(self.env)} if saved else None

    def configure(self, value: str) -> None:
        if not self.editable:
            raise ValueError(NOT_OFFERED if self.public else FIXED)
        value = value.strip().lower() if isinstance(value, str) else ''
        if not CLIENT_ID.fullmatch(value):
            raise ValueError(BAD_CLIENT)
        with _lock(self.root):
            if self._saved_client() == value:
                return
            record = _stored(self.root)
            if record and record.get('client_id') != value:
                raise ValueError(CHANGE_CLIENT)
            write_private(_application_path(self.root), {'client_id': value})
        with self._guard:
            self.pending.clear()

    # ------------------------------------------------------------ status

    def status(self) -> dict:
        client = self.client()
        record = account(self.root)
        editable = self.editable
        if record and record.get('state') == RECONNECT_STATE:
            state = 'reconectar'
        elif record and connected(self.root):
            state = 'conectado'
        elif client:
            state = 'lista'
        else:
            state = 'sin_aplicacion' if editable else 'no_disponible'
        linked = record if state == 'conectado' else {}
        return {'estado': state,
                'aplicacion': {'configurada': bool(client), 'editable': editable,
                               'identificador': client['client_id'] if client and editable else ''},
                # The owner copies this address into the application's registration.
                'redireccion': self.redirect if editable else '',
                'cuenta': record['address'] if state in {'conectado', 'reconectar'} else '',
                'envio': linked.get('send') is True,
                'verifica_altas': bool(linked.get('authority')),
                'conectado_desde': linked.get('connected_at'),
                # Until when the current access is good; it renews itself before that.
                'vigente_hasta': int(linked.get('expires_at') or 0) or None,
                'mensaje': RECONNECT if state == 'reconectar' else ''}

    # ------------------------------------------------------------ authorization

    def start(self, send: bool = False) -> str:
        client = self.client()
        if not client:
            raise ValueError(NEEDS_APPLICATION if self.editable else NOT_OFFERED)
        previous = _stored(self.root)
        # A permission to send that was already given is asked again when the mailbox is connected anew.
        send = send is True or bool(previous and SEND_SCOPE in str(previous.get('scope') or '').split())
        scope = ' '.join((*IDENTITY_SCOPES, IMAP_SCOPE, *((SEND_SCOPE,) if send else ())))
        state, verifier = STATE_PREFIX + secrets.token_urlsafe(32), secrets.token_urlsafe(64)
        challenge = base64.urlsafe_b64encode(hashlib.sha256(verifier.encode()).digest()).decode().rstrip('=')
        now = time.monotonic()
        with self._guard:
            self.pending = {key: attempt for key, attempt in self.pending.items() if attempt.deadline > now}
            if len(self.pending) >= 8:
                del self.pending[next(iter(self.pending))]
            self.pending[state] = Attempt(verifier, now + 600, scope, client['client_id'], client['tenant'])
        # The answer comes back after «#»: the code never travels in the address of a request, and the
        # panel hands it over with its own token. It also reaches a panel whose session cookie does not
        # travel on a navigation that starts at another site.
        params = {'client_id': client['client_id'], 'response_type': 'code', 'redirect_uri': self.redirect,
                  'response_mode': 'fragment', 'scope': scope, 'state': state, 'code_challenge': challenge,
                  'code_challenge_method': 'S256', 'prompt': 'select_account'}
        return AUTHORIZE.format(tenant=client['tenant']) + '?' + urlencode(params)

    def finish(self, state: str, code: str, error: str = '') -> dict:
        """Redeem one answer of Microsoft, open the mailbox and only then save the connection."""
        with self._guard:
            attempt = self.pending.pop(state, None) if isinstance(state, str) else None
        if not attempt or attempt.deadline <= time.monotonic():
            raise OutlookAuthorizationError('caducado')
        if error or not isinstance(code, str) or not code:
            raise OutlookAuthorizationError('consentimiento')
        form = {'grant_type': 'authorization_code', 'client_id': attempt.client_id, 'code': code,
                'redirect_uri': self.redirect, 'code_verifier': attempt.verifier, 'scope': attempt.scope}
        secret = _client_secret(attempt.client_id, self.env)
        if secret:
            form['client_secret'] = secret
        try:
            tokens = _token_request(attempt.tenant, form)
        except OutlookAuthorizationError as exc:
            # Here «invalid_grant» speaks of the code or its proof, not of a permission taken away.
            raise OutlookAuthorizationError('intercambio' if exc.reason == 'revocado' else exc.reason) from None
        scope, wanted = _granted(tokens, attempt.scope)
        refresh = _refresh_token(tokens)
        if not refresh:
            raise OutlookAuthorizationError('sin-renovacion')
        expires_at = _expiry(tokens)
        claims = _identity(tokens.get('id_token'), attempt.client_id)
        addresses = _addresses(claims)
        if not addresses:
            raise OutlookAuthorizationError('cuenta')
        personal = claims.get('tid') in (None, PERSONAL_TENANT)
        address, authority, sends = self.probe(addresses, tokens['access_token'], send=wanted, personal=personal)
        with _lock(self.root):
            _save(self.root, {'address': address, 'connected_at': int(time.time()), 'scope': scope, 'send': bool(sends),
                              'expires_at': expires_at, 'personal': personal, 'authority': authority,
                              'client_id': attempt.client_id, 'tenant': attempt.tenant, 'state': 'ok'},
                  {'access': tokens['access_token'], 'refresh': refresh})
            _FORCED.pop(str(self.root.resolve()), None)
        with self._guard:
            self.pending.clear()
        return {'cuenta': address, 'envio': bool(sends), 'envio_rechazado': wanted and not sends}

    def disconnect(self) -> None:
        with self._guard:
            self.pending.clear()
        remove(self.root)
