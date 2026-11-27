"""Generic read-only mailbox: Gmail through its API, or any IMAP server over verified TLS.

The rest of the program searches and reads messages through `Mailbox` and never needs to know
which provider is behind it. The IMAP/SMTP password is kept encrypted, like portal passwords,
and is never returned, logged or attached to events.
"""
from __future__ import annotations

import email
import imaplib
import re
import ssl
import threading
import time
from datetime import datetime, timezone
from email import policy
from pathlib import Path

import httpx
from cryptography.fernet import Fernet, InvalidToken

from . import gmail, secretos
from .auth import read_json, write_private

MAX_MESSAGE = 50 * 1024 * 1024
GMAIL_AUTHORITY = 'mx.google.com'
ACCOUNT_LOCK = threading.RLock()
HOST = re.compile(r'(?=.{1,253}$)(?:(?:[A-Za-z0-9](?:[A-Za-z0-9-]{0,61}[A-Za-z0-9])?\.)+[A-Za-z]{2,63}|localhost|'
                  r'\d{1,3}(?:\.\d{1,3}){3})')
ADDRESS = re.compile(r'[A-Za-z0-9._%+-]{1,64}@[A-Za-z0-9-]{1,63}(?:\.[A-Za-z0-9-]{1,63})*\.[A-Za-z]{2,63}')
AUTHORITY = re.compile(r'[a-z0-9](?:[a-z0-9.-]{0,251}[a-z0-9])?')
SEARCH_VALUE = re.compile(r'[A-Za-z0-9@._%+-]{1,254}')
SEARCH_TEXT = re.compile(r'[A-Za-z0-9@._%+:/ -]{1,200}')
PUBLIC = ('imap_host', 'imap_port', 'smtp_host', 'smtp_port', 'smtp_tls', 'user', 'address', 'authority', 'connected_at')
MONTHS = ('Jan', 'Feb', 'Mar', 'Apr', 'May', 'Jun', 'Jul', 'Aug', 'Sep', 'Oct', 'Nov', 'Dec')

CERTIFICATE = 'El servidor de correo presentó un certificado no válido. Revisa el nombre del servidor.'
CONNECTION = 'No se pudo conectar con el servidor de correo. Revisa el servidor y el puerto.'
LOGIN = 'El servidor de correo rechazó el usuario o la contraseña de aplicación.'
READING = 'El servidor de correo no completó la consulta. Vuelve a intentarlo.'


def tls_context() -> ssl.SSLContext:
    """Certificate chain and host name are always verified; there is no plain-text mode."""
    return ssl.create_default_context()


def valid_address(value: str) -> str:
    value = (value or '').strip()
    if len(value) > 254 or not ADDRESS.fullmatch(value):
        raise ValueError('Escribe una dirección de correo válida.')
    return value


def valid_host(value: str) -> str:
    value = (value or '').strip().rstrip('.').lower()
    if not HOST.fullmatch(value):
        raise ValueError('Escribe el nombre del servidor de correo, por ejemplo imap.tu-proveedor.com.')
    return value


def authserv_id(report: str) -> str:
    """Identifier of the server that wrote an Authentication-Results header (RFC 8601)."""
    head = re.sub(r'\([^)]*\)', ' ', str(report).partition(';')[0]).split()
    value = head[0].casefold() if head else ''
    return value if AUTHORITY.fullmatch(value) else ''


# ---------------------------------------------------------------- encrypted account

def _folder(root) -> Path:
    return Path(root) / 'auth' / 'correo'


def _account_path(root) -> Path:
    return _folder(root) / 'cuenta.json'


def _vault(root, create: bool) -> Fernet:
    key_file = _folder(root) / 'vault-key.json'
    key = secretos.read_json(key_file, None, purpose=secretos.MAIL_VAULT)
    if key is None:
        if not create:
            raise ValueError('Falta la clave privada del almacén del correo. Vuelve a conectar la cuenta.')
        key = {'key': Fernet.generate_key().decode('ascii')}
        secretos.write_json(key_file, key, purpose=secretos.MAIL_VAULT)
    return Fernet(key['key'].encode('ascii'))


def save_account(root, settings: dict, password: str) -> dict:
    with ACCOUNT_LOCK:
        vault = _vault(root, create=True)
        record = {k: settings[k] for k in PUBLIC if k in settings}
        record['connected_at'] = int(time.time())
        record['secret'] = vault.encrypt(password.encode('utf-8')).decode('ascii')
        write_private(_account_path(root), record)
        return load_account(root)


def load_account(root) -> dict | None:
    """Public settings of the IMAP/SMTP account. The secret is never part of the result."""
    try:
        record = read_json(_account_path(root), None)
    except ValueError:
        return None
    if not isinstance(record, dict) or not record.get('imap_host') or not record.get('secret'):
        return None
    return {k: record[k] for k in PUBLIC if k in record}


def account_password(root) -> str:
    with ACCOUNT_LOCK:
        record = read_json(_account_path(root), None)
        if not isinstance(record, dict) or not record.get('secret'):
            raise ValueError('Conecta tu cuenta de correo en Ajustes.')
        try:
            return _vault(root, create=False).decrypt(record['secret'].encode('ascii')).decode('utf-8')
        except secretos.SealedSecrets:
            raise  # A master key problem is not solved by connecting the account again.
        except (InvalidToken, UnicodeError, ValueError):
            raise ValueError('No se pudo abrir la contraseña guardada del correo. Vuelve a conectar la cuenta.') from None


def remove_account(root) -> None:
    with ACCOUNT_LOCK:
        secretos.check(_folder(root) / 'vault-key.json', purpose=secretos.MAIL_VAULT)
        _account_path(root).unlink(missing_ok=True)
        (_folder(root) / 'vault-key.json').unlink(missing_ok=True)


def gmail_connected(root) -> bool:
    return (Path(root) / 'gmail_token.json').is_file()


def mailbox_connected(root) -> bool:
    return gmail_connected(root) or load_account(root) is not None


# ---------------------------------------------------------------- interface

class Mailbox:
    """Read-only access to one inbox. Use it as a context manager to reuse one session."""
    provider = ''
    authority = ''   # authserv-id this provider writes on inbound mail; '' means unknown.
    address = ''

    def __enter__(self):
        self.open()
        return self

    def __exit__(self, *exc):
        self.close()

    def open(self):
        pass

    def close(self):
        pass

    def search(self, *, since: float | None = None, sender: str = '', recipient: str = '', text: str = '',
               attachments: bool = False, limit: int = 20) -> list[dict]:
        """Newest first. Each row: ref, sender, to, subject, date, received (epoch seconds)."""
        raise NotImplementedError

    def read(self, ref: str):
        """The whole message (headers, text, HTML and attachments), or None above the size limit."""
        raise NotImplementedError


def _criteria(sender, recipient, text, limit):
    for value in (sender, recipient):
        if value and not SEARCH_VALUE.fullmatch(value):
            raise ValueError('La búsqueda de correo contiene caracteres no admitidos.')
    if text and not SEARCH_TEXT.fullmatch(text):
        raise ValueError('La búsqueda de correo contiene caracteres no admitidos.')
    return max(1, min(int(limit), 200))


class GmailMailbox(Mailbox):
    provider = 'gmail'
    authority = GMAIL_AUTHORITY

    def __init__(self, root):
        self.root = Path(root)
        self.client = None

    def __repr__(self):
        return 'GmailMailbox()'

    def open(self):
        if self.client is None:
            self.client = httpx.Client(headers={'Authorization': 'Bearer ' + gmail.access_token(self.root)},
                                       timeout=30, follow_redirects=False)

    def close(self):
        if self.client is not None:
            self.client.close()
            self.client = None

    @staticmethod
    def query(since=None, sender='', recipient='', text='', attachments=False) -> str:
        parts = ['has:attachment'] if attachments else []
        if since:
            parts.append(f'after:{int(since)}')
        if sender:
            parts.append(f'from:({sender})')
        if recipient:
            parts.append(f'to:{recipient}')
        if text:
            parts.append(f'"{text}"')
        return ' '.join(parts + ['-in:spam', '-in:trash'])

    def search(self, *, since=None, sender='', recipient='', text='', attachments=False, limit=20):
        limit = _criteria(sender, recipient, text, limit)
        self.open()
        try:
            rows = []
            for message_id in gmail._ids(self.client, self.query(since, sender, recipient, text, attachments), limit):
                meta = gmail._get(self.client, gmail.GMAIL + '/' + message_id,
                                  {'format': 'metadata', 'metadataHeaders': ['Subject', 'From', 'To', 'Date']}).json()
                headers = {h['name'].lower(): h['value'] for h in meta.get('payload', {}).get('headers', [])}
                rows.append({'ref': 'gmail:' + message_id, 'sender': headers.get('from', '')[:250],
                             'to': headers.get('to', '')[:500], 'subject': headers.get('subject', '')[:250],
                             'date': headers.get('date', '')[:150], 'received': int(meta.get('internalDate') or 0) / 1000})
            return rows
        except httpx.HTTPError as exc:
            raise ValueError('Gmail no completó la búsqueda. Revisa la conexión y vuelve a intentar.') from exc

    def read(self, ref):
        if not ref.startswith('gmail:'):
            raise ValueError('Mensaje de correo desconocido.')
        self.open()
        try:
            return gmail._raw_message(self.client, ref[6:])
        except httpx.HTTPError as exc:
            raise ValueError('Gmail no pudo abrir el mensaje.') from exc


def _imap_date(epoch: float) -> str:
    day = datetime.fromtimestamp(epoch, timezone.utc)
    return f'{day.day:02d}-{MONTHS[day.month - 1]}-{day.year}'


def _fetch_parts(data):
    meta, literal = [], b''
    for item in data or []:
        if isinstance(item, tuple):
            meta.append(item[0])
            literal = literal or item[1]
        elif isinstance(item, bytes):
            meta.append(item)
    return b' '.join(meta), literal


class ImapMailbox(Mailbox):
    provider = 'imap'

    def __init__(self, account: dict, password: str):
        self.account = dict(account)
        self.__password = password
        self.authority = self.account.get('authority', '') or ''
        self.address = self.account.get('address', '') or self.account.get('user', '')
        self.conn = None
        self.validity = ''

    def __repr__(self):
        return f'ImapMailbox({self.account.get("imap_host", "")!r})'

    def open(self):
        if self.conn is not None:
            return
        try:
            conn = imaplib.IMAP4_SSL(self.account['imap_host'], int(self.account.get('imap_port') or 993),
                                     ssl_context=tls_context(), timeout=30)
        except ssl.SSLCertVerificationError:
            raise ValueError(CERTIFICATE) from None
        except (OSError, imaplib.IMAP4.error, ssl.SSLError):
            raise ValueError(CONNECTION) from None
        try:
            try:
                conn.login(self.account['user'], self.__password)
            except (imaplib.IMAP4.error, UnicodeError):
                raise ValueError(LOGIN) from None
            typ, _ = conn.select('INBOX', readonly=True)
            if typ != 'OK':
                raise ValueError(READING)
            self.validity = (conn.untagged_responses.get('UIDVALIDITY') or [b''])[-1].decode('ascii', 'replace')
            if not self.validity.isdigit():
                raise ValueError(READING)
        except ValueError:
            self._drop(conn)
            raise
        except (OSError, imaplib.IMAP4.error):
            self._drop(conn)
            raise ValueError(CONNECTION) from None
        self.conn = conn

    @staticmethod
    def _drop(conn):
        try:
            conn.logout()
        except Exception:
            pass

    def close(self):
        if self.conn is not None:
            self._drop(self.conn)
            self.conn = None

    def _uid(self, *args):
        self.open()
        try:
            typ, data = self.conn.uid(*args)
        except (OSError, imaplib.IMAP4.error):
            raise ValueError(READING) from None
        if typ != 'OK':
            raise ValueError(READING)
        return data

    def _search_uids(self, criteria) -> list[bytes]:
        data = self._uid('SEARCH', *criteria)
        return [uid for uid in (data[0] or b'').split() if uid.isdigit()]

    def summary(self, uid: bytes) -> dict | None:
        meta, headers = _fetch_parts(self._uid(
            'FETCH', uid, '(INTERNALDATE RFC822.SIZE BODY.PEEK[HEADER.FIELDS (FROM TO CC SUBJECT DATE CONTENT-TYPE)])'))
        found = re.search(rb'INTERNALDATE "([^"]+)"', meta)
        size = re.search(rb'RFC822\.SIZE (\d+)', meta)
        if not found or not size:
            return None
        try:
            received = datetime.strptime(found.group(1).decode('ascii').strip(), '%d-%b-%Y %H:%M:%S %z').timestamp()
        except ValueError:
            return None
        head = email.message_from_bytes(headers or b'', policy=policy.default)
        kind = head.get_content_type()
        return {'ref': f'imap:{self.validity}.{uid.decode()}', 'sender': str(head.get('From', ''))[:250],
                'to': ', '.join(str(v) for k in ('To', 'Cc') for v in head.get_all(k, []))[:500],
                'subject': str(head.get('Subject', ''))[:250], 'date': str(head.get('Date', ''))[:150],
                'received': received, 'size': int(size.group(1)),
                'attachments': kind.startswith('application/') or (kind.startswith('multipart/') and kind != 'multipart/alternative')}

    def search(self, *, since=None, sender='', recipient='', text='', attachments=False, limit=20):
        limit = _criteria(sender, recipient, text, limit)
        criteria = []
        if since:
            # IMAP dates have no time zone; widen by a day and filter on the server's arrival time.
            criteria += ['SINCE', _imap_date(since - 86400)]
        if sender:
            criteria += ['FROM', f'"{sender}"']
        if recipient:
            criteria += ['TO', f'"{recipient}"']
        if text:
            criteria += ['TEXT', f'"{text}"']
        rows = []
        uids = self._search_uids(criteria or ['ALL'])
        for uid in reversed(uids[-limit * 3:]):
            item = self.summary(uid)
            if item is None or (since and item['received'] < since) or (attachments and not item['attachments']):
                continue
            rows.append(item)
            if len(rows) >= limit:
                break
        return rows

    def read(self, ref):
        found = re.fullmatch(r'imap:(\d+)\.(\d+)', ref or '')
        if not found:
            raise ValueError('Mensaje de correo desconocido.')
        self.open()
        if found.group(1) != self.validity:
            raise ValueError('El buzón cambió. Vuelve a buscar el mensaje.')
        uid = found.group(2).encode()
        meta, _ = _fetch_parts(self._uid('FETCH', uid, '(RFC822.SIZE)'))
        size = re.search(rb'RFC822\.SIZE (\d+)', meta)
        if not size:
            raise ValueError('El mensaje ya no está en el buzón.')
        if int(size.group(1)) > MAX_MESSAGE:
            return None
        _, raw = _fetch_parts(self._uid('FETCH', uid, '(BODY.PEEK[])'))
        if not raw or len(raw) > MAX_MESSAGE:
            return None
        return email.message_from_bytes(raw, policy=policy.default)

    def detect_authority(self, sample: int = 10) -> str:
        """The authserv-id the provider stamps on the newest inbound messages, if it is consistent."""
        seen, stamped = set(), 0
        for uid in self._search_uids(['ALL'])[-sample:]:
            _, headers = _fetch_parts(self._uid('FETCH', uid, '(BODY.PEEK[HEADER.FIELDS (AUTHENTICATION-RESULTS)])'))
            reports = email.message_from_bytes(headers or b'', policy=policy.compat32).get_all('Authentication-Results', [])
            if reports:
                seen.add(authserv_id(reports[0]))
                stamped += 1
        # Require two or more messages that agree. One message alone may carry a forged header.
        if stamped >= 2 and len(seen) == 1 and '' not in seen:
            return seen.pop()
        return ''


def message_parts(message) -> dict:
    """Headers, plain text, HTML and attachments of a message read through any mailbox."""
    def body(kind):
        part = message.get_body(preferencelist=(kind,))
        if part is None:
            return ''
        try:
            content = part.get_content()
        except (LookupError, UnicodeError, ValueError):
            content = (part.get_payload(decode=True) or b'').decode('utf-8', errors='replace')
        return content[:1_000_000] if isinstance(content, str) else ''

    attachments = []
    for part in message.iter_attachments():
        data = part.get_payload(decode=True) or b''
        attachments.append({'filename': Path((part.get_filename() or '').replace('\\', '/')).name,
                            'content_type': part.get_content_type(), 'data': data})
    headers = {key: str(message.get(key, '')) for key in ('From', 'To', 'Cc', 'Subject', 'Date', 'Message-ID')}
    return {'headers': headers, 'text': body('plain'), 'html': body('html'), 'attachments': attachments}


def imap_mailboxes(root) -> list[Mailbox]:
    account = load_account(root)
    if not account:
        return []
    return [ImapMailbox(account, account_password(root))]


def open_mailboxes(root) -> list[Mailbox]:
    """Every connected mailbox, not yet opened. IMAP comes first, then Gmail."""
    boxes = []
    try:
        boxes.extend(imap_mailboxes(root))
    except ValueError:
        pass
    if gmail_connected(root):
        boxes.append(GmailMailbox(root))
    return boxes


def search_all(root, **criteria) -> list[dict]:
    boxes = open_mailboxes(root)
    if not boxes:
        raise ValueError('Conecta tu correo en Ajustes para buscar este mensaje.')
    rows = []
    for box in boxes:
        with box:
            rows.extend(box.search(**criteria))
    return sorted(rows, key=lambda row: row['received'], reverse=True)


def read_ref(root, ref: str):
    for box in open_mailboxes(root):
        if ref.startswith(box.provider + ':'):
            with box:
                return box.read(ref)
    raise ValueError('El buzón de ese mensaje ya no está conectado.')


def check_account(settings: dict, password: str) -> str:
    """Log in to IMAP (and SMTP when given) without sending anything. Returns the detected authority."""
    with ImapMailbox(settings, password) as box:
        authority = settings.get('authority') or box.detect_authority()
    if settings.get('smtp_host'):
        from .correo_envio import SmtpSender
        sender = SmtpSender(settings, password)
        try:
            sender.connect()
        finally:
            sender.close()
    return authority
