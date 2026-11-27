"""Directory of merchant billing portals, learned across tickets.

It relates a merchant (issuer RFC, normalized name) to its public billing portal.
It is meant to be shared between people, so it never holds data about the
person: no buyer RFC, e-mail, phone, folios, amounts or ticket-specific links.
Every function is best effort: a directory failure never breaks a read or a match.

With several accounts (``compartido``) each one keeps its own directory exactly as a single
person does, and what it observes also goes to a second file every account of the installation
reads. An account's own knowledge always answers first. What only the others know opens a
portal without asking only when ``compartido.CORROBORATION`` different accounts got a verified
invoice from it; with less it is a proposal and the person confirms the domain.
"""
from __future__ import annotations

import json
import logging
import re
import secrets
import sqlite3
import threading
import time
import unicodedata
import xml.etree.ElementTree as ET
from contextlib import contextmanager
from pathlib import Path
from urllib.parse import unquote, urlsplit

from . import compartido
from .catalog import CATALOG, CUSTOM_PORTAL, public_portal_origin

log = logging.getLogger(__name__)

FILE = 'directorio_portales.sqlite'
SOURCE = 'directorio_portales'
# Evidence levels, strongest last. A level is never lowered by a weaker one.
LEVELS = {'busqueda': 1, 'impreso': 2, 'factura': 3}
RFC = re.compile(r'[A-ZÑ&]{3,4}\d{6}[A-Z0-9]{3}')
CFDI = {'c': 'http://www.sat.gob.mx/cfd/4'}

_SCHEMA = """CREATE TABLE IF NOT EXISTS portals(
    key_kind TEXT NOT NULL CHECK(key_kind IN ('rfc','nombre')),
    key TEXT NOT NULL,
    url TEXT NOT NULL,
    level INTEGER NOT NULL,
    hits INTEGER NOT NULL,
    seen INTEGER NOT NULL,
    first_seen REAL NOT NULL,
    last_seen REAL NOT NULL,
    evidence_url TEXT NOT NULL DEFAULT '',
    PRIMARY KEY(key_kind, key, url))"""


def name_key(merchant: str) -> str:
    """Weak merchant key; same normalization as automation.plain_words."""
    plain = unicodedata.normalize('NFKD', (merchant or '').casefold())
    key = ' '.join(re.findall(r'[a-z0-9]+', ''.join(c for c in plain if not unicodedata.combining(c))))
    return key[:120] if len(key.replace(' ', '')) >= 3 else ''


def rfc_key(value: str) -> str:
    value = (value or '').strip().upper()
    return value if RFC.fullmatch(value) else ''


def _tokens(values) -> list[str]:
    """Pieces of private values long enough to recognise inside a link."""
    result = []
    for value in values:
        for piece in re.split(r'[^0-9A-Za-zÑñ@._&-]+', str(value or '')):
            piece = piece.strip().casefold()
            if len(piece) >= 4:
                result.append(piece)
    return result


def portal_value(url: str, private=()) -> str:
    """Public, ticket-independent form of a billing link, or '' if it is not one.

    Query and fragment often carry the folio, amount or the buyer's data, so they
    are dropped. A path that still contains a private value keeps only the origin."""
    origin = public_portal_origin(url)
    if not origin:
        return ''
    path = urlsplit(url).path or '/'
    value = origin + path
    lowered = path.casefold()
    if any(token in lowered for token in _tokens(private)):
        value = origin + '/'
    return value if public_portal_origin(value) == origin else ''


def person_values(root) -> list[str]:
    """The saved fiscal profile, only to make sure none of it is stored."""
    try:
        profile = json.loads((Path(root) / 'profile.json').read_text(encoding='utf-8'))
        return [profile.get(key, '') for key in ('rfc', 'email', 'whatsapp_phone', 'name')]
    except (OSError, ValueError, AttributeError):
        return []


@contextmanager
def _connect(root):
    """One short transaction; the file is closed right after (Windows keeps open files locked)."""
    db = sqlite3.connect(Path(root) / FILE, timeout=5)
    try:
        db.row_factory = sqlite3.Row
        with db:
            db.execute(_SCHEMA)
            yield db
    finally:
        db.close()


# The directory every account of an installation reads. It has no page of evidence: a search
# another account made is never checked again on its word. ``accounts`` counts the different
# accounts behind a row at its level. It does not say which: each account keeps in its own
# directory (``shared_marks``) the note that it was already counted, for the file ``store`` names.
_SHARED_SCHEMA = """CREATE TABLE IF NOT EXISTS portals(
    key_kind TEXT NOT NULL CHECK(key_kind IN ('rfc','nombre')),
    key TEXT NOT NULL,
    url TEXT NOT NULL,
    level INTEGER NOT NULL,
    hits INTEGER NOT NULL,
    seen INTEGER NOT NULL,
    first_seen REAL NOT NULL,
    last_seen REAL NOT NULL,
    accounts INTEGER NOT NULL DEFAULT 1,
    PRIMARY KEY(key_kind, key, url))"""
_MARKS = """CREATE TABLE IF NOT EXISTS shared_marks(
    key_kind TEXT NOT NULL, key TEXT NOT NULL, url TEXT NOT NULL,
    store TEXT NOT NULL, level INTEGER NOT NULL,
    PRIMARY KEY(key_kind, key, url))"""
_setup = threading.Lock()


@contextmanager
def _commons(folder, *, write: bool = False):
    """One short transaction on the directory the accounts share. A writer takes its turn at once."""
    path = Path(folder) / FILE
    if write:
        path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)  # It may have been emptied by hand.
    db = sqlite3.connect(path, timeout=5)
    try:
        db.row_factory = sqlite3.Row
        if write and db.execute('PRAGMA journal_mode').fetchone()[0] != 'wal':
            with _setup:
                # Several accounts write here from their own threads: readers never wait for a writer.
                db.execute('PRAGMA journal_mode=WAL')
                path.chmod(0o600)
        with db:
            if write:
                db.execute('BEGIN IMMEDIATE')
                db.execute(_SHARED_SCHEMA)
                db.execute('CREATE TABLE IF NOT EXISTS store(id TEXT NOT NULL)')
            yield db
    finally:
        db.close()


def _shared_folder(root) -> Path | None:
    """The folder this account shares with the others of its installation; None for a single person."""
    try:
        shared = compartido.shared_root(root)
    except OSError:
        return None
    return shared if shared != Path(root) else None


def _prepared(db) -> bool:
    """False while the shared file is still being created by its first writer: it knows nothing yet."""
    return db.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='portals'").fetchone() is not None


def _store_id(db) -> str:
    """The name of this shared file. One that starts again is another: old notes do not count in it."""
    row = db.execute('SELECT id FROM store').fetchone()
    if row is None:
        db.execute('INSERT INTO store VALUES(?)', (secrets.token_hex(8),))
        row = db.execute('SELECT id FROM store').fetchone()
    return row['id']


def _counted(root, kind: str, key: str, url: str, store: str, rank: int) -> bool:
    """Whether this account is already among the accounts of that shared row at that level."""
    with _connect(root) as db:
        db.execute(_MARKS)
        row = db.execute('SELECT store, level FROM shared_marks WHERE key_kind=? AND key=? AND url=?',
                         (kind, key, url)).fetchone()
    return row is not None and row['store'] == store and row['level'] == rank


def _note(root, kind: str, key: str, url: str, store: str, rank: int) -> None:
    with _connect(root) as db:
        db.execute(_MARKS)
        db.execute("""INSERT INTO shared_marks VALUES(?,?,?,?,?) ON CONFLICT(key_kind,key,url)
                      DO UPDATE SET store=excluded.store, level=excluded.level""", (kind, key, url, store, rank))


def _common_name(key: str, private, words: set[str]) -> bool:
    """A merchant name fit for the shared file: nothing in it reads as the person or as their ticket."""
    if re.search(r'\d{4}', key) or set(key.split()) & words:
        return False
    padded = ' ' + key + ' '
    return not any(len(found.replace(' ', '')) >= 4 and ' ' + found + ' ' in padded
                   for found in (name_key(str(value)) for value in private if value))


def _publish(root, shared: Path, rank: int, value: str, keys: list, private, now: float) -> None:
    """Add what this account just observed to the directory every account reads."""
    words = compartido.personal_words(root)
    if words is None:
        return  # Without knowing what is personal here, nothing leaves the account.
    if not compartido.plain_path(unquote(urlsplit(value).path)):
        # A path that may name one ticket or one visit is not the merchant's portal: only its site is common.
        value = public_portal_origin(value) + '/'
    keys = [(kind, key) for kind, key in keys if kind == 'rfc' or _common_name(key, private, words)]
    if not keys:
        return
    with _commons(shared, write=True) as db:
        store = _store_id(db)
        for kind, key in keys:
            place = (kind, key, value)
            row = db.execute('SELECT level FROM portals WHERE key_kind=? AND key=? AND url=?', place).fetchone()
            if row is not None and rank < row['level']:
                db.execute('UPDATE portals SET seen=seen+1 WHERE key_kind=? AND key=? AND url=?', place)
                continue
            counted = row is not None and rank == row['level'] and _counted(root, *place, store, rank)
            if not counted:
                # The note goes first: if the run dies between the two writes this account counts
                # one time less for the row, never one time more.
                _note(root, *place, store, rank)
            if row is None:
                db.execute('INSERT INTO portals VALUES(?,?,?,?,1,1,?,?,1)', (*place, rank, now, now))
            elif rank > row['level']:
                # Stronger evidence: its own count of hits and of accounts starts now.
                db.execute("""UPDATE portals SET level=?,hits=1,seen=seen+1,last_seen=?,accounts=1
                              WHERE key_kind=? AND key=? AND url=?""", (rank, now, *place))
            else:
                db.execute("""UPDATE portals SET hits=hits+1,seen=seen+1,last_seen=?,accounts=accounts+?
                              WHERE key_kind=? AND key=? AND url=?""", (now, 0 if counted else 1, *place))


def record(root, level: str, url: str, *, rfc: str = '', merchant: str = '', evidence_url: str = '',
           private=(), at: float | None = None) -> bool:
    """Store one observation of a merchant portal. Returns whether anything was stored."""
    if level not in LEVELS or root is None:
        return False
    private = [*private, *person_values(root)]
    value = portal_value(url, private)
    evidence = portal_value(evidence_url, private) if level == 'busqueda' else ''
    # Never key the directory by the person's own RFC or name.
    private_rfcs = {str(v).strip().upper() for v in private if v}
    private_names = {name_key(str(v)) for v in private if v} - {''}
    keys = [(kind, key) for kind, key in (('rfc', rfc_key(rfc)), ('nombre', name_key(merchant)))
            if key and key not in (private_rfcs if kind == 'rfc' else private_names)]
    if not value or not keys:
        return False
    now = time.time() if at is None else at
    rank = LEVELS[level]
    with _connect(root) as db:
        for kind, key in keys:
            row = db.execute('SELECT level FROM portals WHERE key_kind=? AND key=? AND url=?',
                             (kind, key, value)).fetchone()
            if row is None:
                db.execute('INSERT INTO portals VALUES(?,?,?,?,?,?,?,?,?)',
                           (kind, key, value, rank, 1, 1, now, now, evidence))
            elif rank > row['level']:
                # Stronger evidence: its own hit count starts now.
                db.execute("""UPDATE portals SET level=?,hits=1,seen=seen+1,last_seen=?,
                              evidence_url=CASE WHEN ?<>'' THEN ? ELSE evidence_url END
                              WHERE key_kind=? AND key=? AND url=?""",
                           (rank, now, evidence, evidence, kind, key, value))
            elif rank == row['level']:
                db.execute("""UPDATE portals SET hits=hits+1,seen=seen+1,last_seen=?,
                              evidence_url=CASE WHEN ?<>'' THEN ? ELSE evidence_url END
                              WHERE key_kind=? AND key=? AND url=?""",
                           (now, evidence, evidence, kind, key, value))
            else:
                # Weaker evidence never lowers the level nor counts as a hit of it.
                db.execute('UPDATE portals SET seen=seen+1 WHERE key_kind=? AND key=? AND url=?',
                           (kind, key, value))
    try:
        shared = _shared_folder(root)
        if shared is not None:
            _publish(root, shared, rank, value, keys, private, now)
    except (sqlite3.Error, OSError):
        # The account keeps what it learned; only the others go without it this time.
        log.warning('No se pudo guardar en el directorio compartido de portales.', exc_info=True)
    return True


def _entries(db, kind, key):
    # Strongest evidence first; between equals, more hits, then the most recent.
    return db.execute("""SELECT * FROM portals WHERE key_kind=? AND key=?
                         ORDER BY level DESC, hits DESC, last_seen DESC""", (kind, key)).fetchall()


def _entry(row, kind):
    level = next(name for name, rank in LEVELS.items() if rank == row['level'])
    return {'url': row['url'], 'level': level, 'matched_by': kind, 'hits': row['hits'],
            'last_seen': row['last_seen'], 'evidence_url': row['evidence_url']}


def _common_entry(row, kind) -> tuple[tuple, dict]:
    """A row of the shared directory as an account may use it, and its place among the others.

    Only a verified invoice that enough different accounts got from that portal keeps its level.
    Anything else is offered as a search nobody checked: the person confirms the domain.
    """
    trusted = row['level'] == LEVELS['factura'] and row['accounts'] >= compartido.CORROBORATION
    entry = {'url': row['url'], 'level': 'factura' if trusted else 'busqueda', 'matched_by': kind,
             'hits': row['hits'], 'last_seen': row['last_seen'], 'evidence_url': '', 'shared': True}
    return (not trusted, -row['level'], -row['accounts'], -row['hits'], -row['last_seen']), entry


def lookup(root, *, rfc: str = '', merchant: str = '') -> dict | None:
    """Best known portal, by issuer RFC first and then by merchant name.

    What the account itself knows answers first. With several accounts, what only the others
    know comes after it, rated by ``_common_entry``.
    """
    if root is None:
        return None
    keys = (('rfc', rfc_key(rfc)), ('nombre', name_key(merchant)))
    if (Path(root) / FILE).exists():
        with _connect(root) as db:
            for kind, key in keys:
                rows = _entries(db, kind, key) if key else []
                rows = [row for row in rows if public_portal_origin(row['url'])]
                if rows:
                    best = _entry(rows[0], kind)
                    best['alternates'] = [_entry(row, kind) for row in rows[1:]]
                    return best
    shared = _shared_folder(root)
    if shared is None or not (shared / FILE).exists():
        return None
    with _commons(shared) as db:
        for kind, key in keys if _prepared(db) else ():
            rows = db.execute('SELECT * FROM portals WHERE key_kind=? AND key=?', (kind, key)).fetchall() if key else []
            found = sorted((_common_entry(row, kind) for row in rows
                            if public_portal_origin(row['url']) and compartido.plain_path(unquote(urlsplit(row['url']).path))),
                           key=lambda item: item[0])
            if found:
                best = found[0][1]
                best['alternates'] = [entry for _, entry in found[1:]]
                return best
    return None


def lookup_safely(root, **keys) -> dict | None:
    try:
        return lookup(root, **keys)
    except (sqlite3.Error, OSError, ValueError, StopIteration):
        log.warning('No se pudo consultar el directorio de portales.', exc_info=True)
        return None


def record_safely(root, level, url, **values) -> bool:
    try:
        return record(root, level, url, **values)
    except (sqlite3.Error, OSError, ValueError):
        log.warning('No se pudo guardar en el directorio de portales.', exc_info=True)
        return False


def invoice_confirmed(root, rfc: str, url: str) -> bool:
    """The exact portal already issued a verified invoice of this issuer RFC.

    To this account, or else to enough different accounts of its installation.
    """
    key = rfc_key(rfc)
    if not key or not public_portal_origin(url) or root is None:
        return False
    place = ('rfc', key, url, LEVELS['factura'])
    try:
        if (Path(root) / FILE).exists():
            with _connect(root) as db:
                if db.execute('SELECT 1 FROM portals WHERE key_kind=? AND key=? AND url=? AND level=?',
                              place).fetchone() is not None:
                    return True
        shared = _shared_folder(root)
        if shared is None or not (shared / FILE).exists() or not compartido.plain_path(unquote(urlsplit(url).path)):
            return False
        with _commons(shared) as db:
            return _prepared(db) and db.execute("""SELECT 1 FROM portals WHERE key_kind=? AND key=? AND url=? AND level=?
                AND accounts>=?""", (*place, compartido.CORROBORATION)).fetchone() is not None
    except (sqlite3.Error, OSError):
        log.warning('No se pudo consultar el directorio de portales.', exc_info=True)
        return False


def receipt_portal(receipt) -> str:
    """The portal a ticket used: its catalog link or its own confirmed portal."""
    if receipt.provider in CATALOG:
        return CATALOG[receipt.provider]['url']
    if receipt.provider == CUSTOM_PORTAL:
        return receipt.portal_url
    return ''  # A direct request by message or an unknown portal teaches nothing.


def invoice_parties(data: bytes) -> tuple[str, str]:
    """Issuer and receiver RFC of an already verified CFDI."""
    if b'<!DOCTYPE' in data.upper() or b'<!ENTITY' in data.upper():
        return '', ''
    root = ET.fromstring(data)
    issuers = root.findall('c:Emisor', CFDI)
    receivers = root.findall('c:Receptor', CFDI)
    issuer = rfc_key(issuers[0].get('Rfc', '')) if len(issuers) == 1 else ''
    receiver = (receivers[0].get('Rfc', '') if len(receivers) == 1 else '').strip().upper()
    return issuer, receiver


def ticket_values(receipt) -> list[str]:
    return [receipt.ticket_number, *receipt.billing_codes, receipt.amount, receipt.merchant_whatsapp,
            receipt.merchant_email]


def learn_from_invoice(root, receipt, xml: Path) -> bool:
    """Remember the portal of a verified invoice. Never raises."""
    try:
        url = receipt_portal(receipt)
        if not url:
            return False
        issuer, receiver = invoice_parties(Path(xml).read_bytes())
        if not issuer or issuer == receiver:
            return False
        return record(root, 'factura', url, rfc=issuer, merchant=receipt.merchant,
                      private=[receiver, *ticket_values(receipt)])
    except Exception:  # The directory is optional; the match must never fail because of it.
        log.warning('No se pudo aprender el portal de una factura comprobada.', exc_info=True)
        return False
