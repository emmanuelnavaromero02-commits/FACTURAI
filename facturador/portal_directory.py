"""Directory of merchant billing portals, learned across tickets.

It relates a merchant (issuer RFC, normalized name) to its public billing portal.
It is meant to be shared between people later, so it never holds data about the
person: no buyer RFC, e-mail, phone, folios, amounts or ticket-specific links.
Every function is best effort: a directory failure never breaks a read or a match.
"""
from __future__ import annotations

import json
import logging
import re
import sqlite3
import time
import unicodedata
import xml.etree.ElementTree as ET
from contextlib import contextmanager
from pathlib import Path
from urllib.parse import urlsplit

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
    return True


def _entries(db, kind, key):
    # Strongest evidence first; between equals, more hits, then the most recent.
    return db.execute("""SELECT * FROM portals WHERE key_kind=? AND key=?
                         ORDER BY level DESC, hits DESC, last_seen DESC""", (kind, key)).fetchall()


def _entry(row, kind):
    level = next(name for name, rank in LEVELS.items() if rank == row['level'])
    return {'url': row['url'], 'level': level, 'matched_by': kind, 'hits': row['hits'],
            'last_seen': row['last_seen'], 'evidence_url': row['evidence_url']}


def lookup(root, *, rfc: str = '', merchant: str = '') -> dict | None:
    """Best known portal, by issuer RFC first and then by merchant name."""
    if root is None or not (Path(root) / FILE).exists():
        return None
    with _connect(root) as db:
        for kind, key in (('rfc', rfc_key(rfc)), ('nombre', name_key(merchant))):
            rows = _entries(db, kind, key) if key else []
            rows = [row for row in rows if public_portal_origin(row['url'])]
            if rows:
                best = _entry(rows[0], kind)
                best['alternates'] = [_entry(row, kind) for row in rows[1:]]
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
    """The exact portal already issued a verified invoice of this issuer RFC."""
    key = rfc_key(rfc)
    if not key or not public_portal_origin(url) or root is None or not (Path(root) / FILE).exists():
        return False
    try:
        with _connect(root) as db:
            return db.execute('SELECT 1 FROM portals WHERE key_kind=? AND key=? AND url=? AND level=?',
                              ('rfc', key, url, LEVELS['factura'])).fetchone() is not None
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
