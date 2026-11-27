"""What the agent learns from a portal it already solved, kept as data and never as code.

A screen is known by where it is (origin and path without parameters) and by its
signature: the kinds and labels of its visible fields plus its visible buttons.
Values never enter the signature. For each screen the memory keeps the ordered
steps that solved it. A typed value is stored as a link to the profile or to the
ticket (``$perfil.rfc``, ``$ticket.date|dd/mm/aaaa``), so the steps can be repeated
with another ticket and the file holds no personal data.

Nothing reaches the file until the invoice of that attempt is verified.
"""
from __future__ import annotations

import functools
import hashlib
import json
import re
import sqlite3
import time
import unicodedata
from contextlib import contextmanager
from datetime import date
from decimal import Decimal
from pathlib import Path
from urllib.parse import unquote

from .fiscal_choices import PAYMENTS, SAT_CODES, USES, code_tokens, matches, words
from .models import money
from .tax_document import REGIME_NAMES

FAILURES_TO_DISABLE = 3
FAMILY_SUCCESSES = 2
PENDING_DAYS = 30
KEYS = {'Enter', 'Tab', 'ArrowDown', 'ArrowUp', 'Escape', 'Space'}
EMAIL = re.compile(r'[^\s@]+@[^\s@]+\.[a-z]{2,}', re.I)
RFC = re.compile(r'(?<![A-Z0-9])[A-ZÑ&]{3,4}\d{6}[A-Z0-9]{3}(?![A-Z0-9])')
MONTHS = ('enero', 'febrero', 'marzo', 'abril', 'mayo', 'junio', 'julio', 'agosto', 'septiembre',
          'octubre', 'noviembre', 'diciembre')
SAT_ALIASES = {'payment': PAYMENTS, 'use': USES, 'regime': {code: (name,) for code, name in REGIME_NAMES.items()}}


def fold(value) -> str:
    """Lower case without accents, to compare texts."""
    text = unicodedata.normalize('NFKD', str(value or '').casefold())
    return ''.join(char for char in text if not unicodedata.combining(char))


def plain(value) -> str:
    """A label as the signature sees it: no case, accents, punctuation or digits."""
    return ' '.join(re.findall(r'[a-z#]+', re.sub(r'\d+', '#', fold(value))))[:80]


def screen_signature(fields: list[dict], buttons: list[str]) -> str:
    """Identity of a screen: its visible fields in order and its visible buttons. Values never enter."""
    shape = [[item.get('kind', ''), plain(item.get('label', ''))] for item in fields]
    pressed = sorted({plain(text) for text in buttons} - {''})
    return hashlib.sha256(json.dumps([shape, pressed], separators=(',', ':')).encode()).hexdigest()[:32]


def _equals(fields: list[dict], kind: str, label: str) -> list[dict]:
    return [item for item in fields if item.get('kind') == kind and plain(item.get('label', '')) == label]


def describe(fields: list[dict], number: int) -> dict | None:
    """Stable description of field ``number``: its kind, its label and its place among equal ones."""
    item = next((entry for entry in fields if entry.get('n') == number), None)
    if item is None:
        return None
    label = plain(item.get('label', ''))
    same = _equals(fields, item.get('kind'), label)
    return {'by': 'campo', 'kind': item.get('kind'), 'label': label, 'ordinal': same.index(item), 'count': len(same)}


def locate(fields: list[dict], target: dict, anchor: str = '') -> int | None:
    """The current number of a described field, or None unless exactly that field is there.

    ``anchor`` is the name and id seen when it was described. It only holds inside
    the same page session, so recipes from the memory pass an empty one.
    """
    same = _equals(fields, target.get('kind'), target.get('label', ''))
    if len(same) != target.get('count') or not 0 <= target.get('ordinal', -1) < len(same):
        return None
    item = same[target['ordinal']]
    if anchor and item.get('key', '') != anchor:
        return None
    return item.get('n')


def _part(value: str, index: int) -> str:
    pieces = [piece for piece in re.split(r'[\s/|#-]+', value) if piece]
    return pieces[index] if len(pieces) > max(index, 1) else ''


# How a profile or ticket text was typed. The first that reproduces it is kept.
TEXT_STYLES = {
    '': lambda value: value,
    'mayus': str.upper,
    'minus': str.lower,
    'titulo': str.title,
    'compacto': lambda value: re.sub(r'[^0-9A-Za-zÑñ&]', '', value),
    'digitos': lambda value: re.sub(r'\D', '', value),
    'parte1': lambda value: _part(value, 0),
    'parte2': lambda value: _part(value, 1),
    'parte3': lambda value: _part(value, 2),
}
AMOUNT_STYLES = ('0.00', '0,0.00', 'g', '$0.00', '$0,0.00', '0,00')
DATE_STYLES = ('dd/mm/aaaa', 'aaaa-mm-dd', 'dd-mm-aaaa', 'ddmmaaaa', 'aaaammdd', 'dd/mm/aa', 'aaaa/mm/dd',
               'dd.mm.aaaa', 'd/m/aaaa', 'mm/dd/aaaa', 'ddmmaa', 'aammdd', 'dd', 'mm', 'aaaa', 'd', 'm', 'aa',
               'mes', 'Mes', 'MES', 'mes3', 'Mes3', 'MES3')
DATE_TOKEN = re.compile(r'aaaa|aa|MES3|Mes3|mes3|MES|Mes|mes|mm|m|dd|d')


def amount_text(amount: Decimal, style: str) -> str | None:
    if style == 'g':
        return format(amount.normalize(), 'f')
    sign = '$' if style.startswith('$') else ''
    form = style.lstrip('$')
    if form == '0.00':
        return sign + f'{amount:.2f}'
    if form == '0,0.00':
        return sign + f'{amount:,.2f}'
    if form == '0,00':
        return sign + f'{amount:.2f}'.replace('.', ',')
    return None


def date_text(day: date, style: str) -> str | None:
    if style not in DATE_STYLES:
        return None
    month = MONTHS[day.month - 1]
    parts = {'aaaa': f'{day.year:04d}', 'aa': f'{day.year % 100:02d}', 'mm': f'{day.month:02d}',
             'm': str(day.month), 'dd': f'{day.day:02d}', 'd': str(day.day),
             'mes': month, 'Mes': month.capitalize(), 'MES': month.upper(),
             'mes3': month[:3], 'Mes3': month[:3].capitalize(), 'MES3': month[:3].upper()}
    return DATE_TOKEN.sub(lambda found: parts[found[0]], style)


def sat_like(text: str) -> bool:
    """A text that reads as a SAT regime, CFDI use or payment form, whichever code it is."""
    for kind, aliases in SAT_ALIASES.items():
        if code_tokens(text, kind) & set(SAT_CODES[kind]):
            return True
        if any(words(text) == words(name) for names in aliases.values() for name in names):
            return True
    return False


class Known:
    """The profile and ticket values a typed text can be traced back to."""

    def __init__(self, profile, receipt):
        self.variant = 'F' if len(profile.rfc) == 13 else 'M'
        self.text = {'perfil.rfc': profile.rfc, 'perfil.email': profile.email, 'perfil.name': profile.name,
                     'perfil.postal_code': profile.postal_code, 'ticket.ticket_number': receipt.ticket_number}
        for index, code in enumerate(receipt.billing_codes):
            self.text[f'ticket.billing_codes[{index}]'] = code
        # What identifies the person or the purchase; an address is cleaned of these.
        self.identifiers = dict(self.text)
        self.text.update({'perfil.regime': profile.regime, 'perfil.cfdi_use': profile.cfdi_use,
                          'ticket.payment_code': receipt.payment_code})
        # Any other text the ticket carries (the merchant, and whatever the reading adds later)
        # can be linked too, after everything above.
        for name, value in receipt.model_dump().items():
            if (isinstance(value, str) and value.strip() and 'ticket.' + name not in self.text
                    and name not in {'provider', 'portal_url', 'details', 'source', 'amount', 'date'}):
                self.text['ticket.' + name] = value
        self.sat = {'regime': profile.regime, 'use': profile.cfdi_use, 'payment': receipt.payment_code}
        try:
            self.amount = money(receipt.amount)
        except ValueError:
            self.amount = None
        try:
            self.day = date.fromisoformat(receipt.date) if receipt.date else None
        except ValueError:
            self.day = None
        private = [profile.rfc, profile.email, profile.name, receipt.ticket_number, *receipt.billing_codes]
        self.private = sorted({fold(value).strip() for value in private if value and value.strip()}, key=len, reverse=True)
        self.names = {token for token in re.findall(r'[a-z]{4,}', fold(profile.name))}
        self.amounts = ({amount_text(self.amount, style) for style in ('0.00', '0,0.00', 'g')}
                        if self.amount is not None else set())

    def link(self, kind: str, typed: str | None, label: str = '') -> str | None:
        """The link that reproduces what was typed or chosen; None when it comes from nowhere known."""
        typed = (typed or '').strip()
        if kind == 'press':
            return '=' + typed if typed in KEYS else None
        if kind == 'upload':
            return '=ticket'
        if kind not in {'fill', 'select'}:
            return None
        if not typed:
            return '='
        hint = plain(label)
        # A short number can be a day, a month or a code. Only the label can tell, so a
        # piece of the date or a small amount is linked when the field is named after it.
        dated = bool(re.search(r'\b(?:fecha|dia|mes|ano|anio|date|day|month|year)\b', hint))
        counted = bool(re.search(r'\b(?:importe|total|monto|cantidad|amount)\b', hint))
        searches = {'date': lambda: self._date(typed, dated), 'amount': lambda: self._amount(typed, counted),
                    'text': lambda: self._text(typed)}
        order = ('date', 'amount', 'text') if dated else ('amount', 'text', 'date') if counted else (
            'text', 'amount', 'date')
        for name in order:
            found = searches[name]()
            if found:
                return found
        if kind == 'select':
            for name, expected in self.sat.items():
                if expected and matches(typed, typed, name, expected):
                    return '$sat.' + name
            # A literal is kept only when it cannot be personal or ticket data.
            if not self.reveals(typed) and not sat_like(typed):
                return '=' + typed
        return None

    def _text(self, typed: str) -> str | None:
        for style, render in TEXT_STYLES.items():
            for name, value in self.text.items():
                if value and render(value.strip()) == typed:
                    return '$' + name + ('|' + style if style else '')
        return None

    def _amount(self, typed: str, named: bool) -> str | None:
        if self.amount is None or (len(typed) < 4 and not named):
            return None
        return next(('$ticket.amount|' + style for style in AMOUNT_STYLES
                     if amount_text(self.amount, style) == typed), None)

    def _date(self, typed: str, named: bool) -> str | None:
        if self.day is None or (typed.isdigit() and len(typed) < 6 and not named):
            return None
        return next(('$ticket.date|' + style for style in DATE_STYLES if date_text(self.day, style) == typed), None)

    def value(self, link: str | None) -> str | None:
        """What a stored link means for this profile and ticket; None when it cannot be built here."""
        if link is None or link.startswith('$sat.'):
            return None
        if link.startswith('='):
            return link[1:]
        name, _, style = link[1:].partition('|')
        if name == 'ticket.amount':
            return amount_text(self.amount, style) if self.amount is not None else None
        if name == 'ticket.date':
            return date_text(self.day, style) if self.day is not None else None
        source, render = self.text.get(name), TEXT_STYLES.get(style)
        return (render(source.strip()) or None) if source and render else None

    def reveals(self, text: str) -> bool:
        """True when a text that would be stored could carry personal or ticket data."""
        text = str(text or '')
        if re.search(r'\d{4,}', text) or EMAIL.search(text) or RFC.search(text.upper()):
            return True
        folded = fold(text)
        squeezed = re.sub(r'[^a-z0-9]', '', folded)
        for value in self.private:
            if len(value) < 4:
                if folded.strip() == value:
                    return True
            elif value in folded or re.sub(r'[^a-z0-9]', '', value) in squeezed:
                return True
        # One word of the name is too often an ordinary word of a form («Campos», «Factura»);
        # two of them together are the person or the company.
        named = sum(bool(re.search(r'(?<![a-z])' + token + r'(?![a-z])', folded)) for token in self.names)
        if named >= min(2, len(self.names) or 2):
            return True
        return any(re.search(r'(?<![\d.,])' + re.escape(shown) + r'(?![\d.,])', text) for shown in self.amounts)

    def route(self, path: str) -> str:
        """The path without ticket or personal data nor long identifiers, so it repeats across tickets."""
        text = unquote(path or '/')
        for name, value in sorted(self.identifiers.items(), key=lambda item: -len(item[1] or '')):
            if value and len(value.strip()) >= 4:
                text = re.sub(re.escape(value.strip()), '{' + name + '}', text, flags=re.I)
        text = re.sub(r'[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}', '{id}', text, flags=re.I)
        text = re.sub(r'\d{4,}', '#', text)
        return '{dato}' if self.reveals(text) else text[:300]


def _same_place(step: dict, other: dict) -> bool:
    written = {'fill', 'select'}
    same_kind = (step['kind'] in written and other['kind'] in written) or (
        step['kind'] == other['kind'] and step.get('value') == other.get('value'))
    return same_kind and step.get('target') == other.get('target')


def _source(link: str | None) -> str:
    return (link or '').partition('|')[0]


def compact(steps: list[dict]) -> list[dict] | None:
    """What finally solved a screen: the last value written in each field and the last press of each control.

    Returns None when the same field took different pieces of the ticket in turn:
    that is a loop over data and the agent has to drive it.
    """
    result: list[dict] = []
    for step in steps:
        previous = next((item for item in result if _same_place(item, step)), None)
        if previous is not None:
            first, second = _source(previous.get('value')), _source(step.get('value'))
            if (step['kind'] in {'fill', 'select'} and first != second
                    and first.startswith('$ticket.billing_codes') and second.startswith('$ticket.billing_codes')):
                return None
            result.remove(previous)
            target = step.get('target') or {}
            if step['kind'] == 'click' and target.get('by') == 'campo' and target.get('kind') == 'checkbox':
                continue  # Two clicks on the same checkbox leave it as it was.
        result.append(step)
    return result


def guarded(default):
    """A memory that cannot be read or written never stops a ticket: it just does not help."""
    def wrap(method):
        @functools.wraps(method)
        def call(self, *args, **kwargs):
            try:
                return method(self, *args, **kwargs)
            except sqlite3.Error:
                return default
        return call
    return wrap


class PortalMemory:
    """The recipes of every solved screen, in their own file so it can be shared later.

    ``variant`` separates what was learned with a person's RFC from a company's:
    a portal may ask them different things on the same screen.
    """

    def __init__(self, root: Path):
        self.path = Path(root) / 'memoria_portales.sqlite'
        try:
            with self.connect() as db:
                db.executescript("""
                PRAGMA journal_mode=WAL;
                CREATE TABLE IF NOT EXISTS screens(
                  origin TEXT NOT NULL, path TEXT NOT NULL, signature TEXT NOT NULL, variant TEXT NOT NULL,
                  steps TEXT NOT NULL, needs_ai INTEGER NOT NULL DEFAULT 0,
                  successes INTEGER NOT NULL DEFAULT 0, failures INTEGER NOT NULL DEFAULT 0,
                  streak INTEGER NOT NULL DEFAULT 0, active INTEGER NOT NULL DEFAULT 1, updated INTEGER NOT NULL,
                  PRIMARY KEY(origin, path, signature, variant));
                CREATE INDEX IF NOT EXISTS screens_family ON screens(signature, variant);
                CREATE TABLE IF NOT EXISTS pending(job_id TEXT PRIMARY KEY, attempt TEXT NOT NULL, saved INTEGER NOT NULL);
                """)
            self.path.chmod(0o600)
        except (sqlite3.Error, OSError):
            pass

    @contextmanager
    def connect(self):
        db = sqlite3.connect(self.path, timeout=20)
        db.row_factory = sqlite3.Row
        try:
            with db:
                yield db
        finally:
            db.close()

    @guarded(None)
    def screen(self, key: tuple, variant: str) -> dict | None:
        """The row of exactly this screen, whatever its state."""
        with self.connect() as db:
            row = db.execute('SELECT * FROM screens WHERE origin=? AND path=? AND signature=? AND variant=?',
                             (*key, variant)).fetchone()
        return {**dict(row), 'steps': json.loads(row['steps'])} if row else None

    @guarded(None)
    def family(self, signature: str, variant: str) -> list[dict] | None:
        """Steps the same screen takes in other portals, once they worked at least twice."""
        with self.connect() as db:
            row = db.execute("""SELECT steps, SUM(successes) AS total FROM screens
                WHERE signature=? AND variant=? AND needs_ai=0 AND active=1 AND steps<>'[]'
                GROUP BY steps ORDER BY total DESC, MAX(updated) DESC LIMIT 1""", (signature, variant)).fetchone()
        return json.loads(row['steps']) if row and row['total'] >= FAMILY_SUCCESSES else None

    @guarded(False)
    def learn(self, key: tuple, variant: str, steps: list[dict], needs_ai: bool) -> bool:
        """Keep the steps a verified attempt took with the agent deciding. True when they are new."""
        stored = json.dumps([] if needs_ai else steps, ensure_ascii=False, sort_keys=True)
        with self.connect() as db:
            db.execute('BEGIN IMMEDIATE')
            row = db.execute('SELECT steps, needs_ai FROM screens WHERE origin=? AND path=? AND signature=? AND variant=?',
                             (*key, variant)).fetchone()
            if row and row['steps'] == stored and bool(row['needs_ai']) == needs_ai:
                db.execute("""UPDATE screens SET successes=successes+1, streak=0, active=1, updated=?
                    WHERE origin=? AND path=? AND signature=? AND variant=?""", (int(time.time()), *key, variant))
                return False
            db.execute("""INSERT INTO screens(origin,path,signature,variant,steps,needs_ai,successes,failures,streak,active,updated)
                VALUES(?,?,?,?,?,?,1,0,0,1,?) ON CONFLICT(origin,path,signature,variant) DO UPDATE SET
                steps=excluded.steps, needs_ai=excluded.needs_ai, successes=1, streak=0, active=1,
                updated=excluded.updated""", (*key, variant, stored, int(needs_ai), int(time.time())))
        return not needs_ai

    @guarded(None)
    def confirm(self, key: tuple, variant: str, steps: list[dict]) -> None:
        """A repeated screen ended in a verified invoice. Its steps stay as they are."""
        with self.connect() as db:
            db.execute("""INSERT INTO screens(origin,path,signature,variant,steps,needs_ai,successes,failures,streak,active,updated)
                VALUES(?,?,?,?,?,0,1,0,0,1,?) ON CONFLICT(origin,path,signature,variant) DO UPDATE SET
                successes=successes+1, streak=0, active=1, updated=excluded.updated""",
                (*key, variant, json.dumps(steps, ensure_ascii=False, sort_keys=True), int(time.time())))

    @guarded(False)
    def failed(self, key: tuple, variant: str, steps: list[dict]) -> bool:
        """A repeated screen did not work. Three in a row switch it off. True when it got switched off."""
        with self.connect() as db:
            db.execute('BEGIN IMMEDIATE')
            db.execute("""INSERT INTO screens(origin,path,signature,variant,steps,needs_ai,successes,failures,streak,active,updated)
                VALUES(?,?,?,?,?,0,0,1,1,1,?) ON CONFLICT(origin,path,signature,variant) DO UPDATE SET
                failures=failures+1, streak=streak+1, updated=excluded.updated""",
                (*key, variant, json.dumps(steps, ensure_ascii=False, sort_keys=True), int(time.time())))
            disabled = db.execute("""UPDATE screens SET active=0 WHERE origin=? AND path=? AND signature=? AND variant=?
                AND active=1 AND streak>=?""", (*key, variant, FAILURES_TO_DISABLE)).rowcount
        return bool(disabled)

    @guarded(None)
    def hold(self, job_id: str, attempt: dict) -> None:
        """Park an attempt whose portal confirmed the invoice while its files are still on their way."""
        with self.connect() as db:
            db.execute("""INSERT INTO pending(job_id,attempt,saved) VALUES(?,?,?)
                ON CONFLICT(job_id) DO UPDATE SET attempt=excluded.attempt, saved=excluded.saved""",
                (job_id, json.dumps(attempt, ensure_ascii=False), int(time.time())))

    @guarded(None)
    def release(self, job_id: str) -> None:
        with self.connect() as db:
            db.execute('DELETE FROM pending WHERE job_id=?', (job_id,))

    @guarded(0)
    def settle(self, status_of) -> int:
        """Confirm parked attempts whose ticket got verified meanwhile and forget the lost ones.

        ``status_of(job_id)`` returns the ticket status, or None when it no longer exists.
        Returns how many screens were learned.
        """
        with self.connect() as db:
            rows = db.execute('SELECT job_id, attempt, saved FROM pending').fetchall()
        learned = 0
        for row in rows:
            status = status_of(row['job_id'])
            if status == 'verified':
                learned += self.apply(json.loads(row['attempt']))
            elif status in {'issued_waiting_files', 'uncertain'} and row['saved'] > time.time() - PENDING_DAYS * 86400:
                continue
            self.release(row['job_id'])
        return learned

    @guarded(0)
    def apply(self, attempt: dict) -> int:
        """Write a verified attempt. Returns how many screens were learned or changed."""
        learned = 0
        for screen in attempt.get('screens', []):
            key = tuple(screen['key'])
            if screen.get('replayed'):
                self.confirm(key, attempt['variant'], screen['steps'])
            elif self.learn(key, attempt['variant'], screen['steps'], bool(screen.get('needs_ai'))):
                learned += 1
        return learned


def harmless(default):
    """Noting or recalling must never cost a ticket: on any failure the attempt goes on without the memory."""
    def wrap(method):
        @functools.wraps(method)
        def call(self, *args, **kwargs):
            try:
                return method(self, *args, **kwargs)
            except Exception:
                self.blocked = True
                return default
        return call
    return wrap


class Attempt:
    """What one attempt did, screen by screen. It reaches the memory only when its invoice is verified."""

    def __init__(self, memory: PortalMemory, known: Known):
        self.memory, self.known = memory, known
        self.entries: list[dict] = []
        self.tainted: set[tuple] = set()
        self.replays: dict[tuple, dict] = {}
        self.missed: set[tuple] = set()
        self.blocked = False

    @harmless(None)
    def step(self, key: tuple, action, field: dict | None, risk: str, source: str, label: str = '') -> None:
        """Note an action that just worked. Whatever cannot be linked leaves its screen to the agent."""
        if self.blocked or action.kind in {'wait', 'scroll'}:
            return
        if action.kind == 'goto':
            self.tainted.add(key)
            return
        target = field
        if target is None and action.target is not None and action.target.by != 'field':
            target = {'by': action.target.by, 'name': action.target.name, 'role': action.target.role,
                      'exact': action.target.exact}
        exposed = target is not None and self.known.reveals(target.get('name') or target.get('label') or '')
        value = self.known.link(action.kind, action.value, label or (field or {}).get('label', ''))
        unlinked = action.kind in {'fill', 'select', 'press', 'upload'} and value is None
        if exposed or unlinked or (action.target is not None and target is None):
            # Nothing of this step is kept: only that its screen needs the agent.
            self.entries.append({'key': key, 'step': None, 'source': source, 'needs_ai': True})
            return
        step = {'kind': action.kind, 'target': target, 'value': value, 'risk': 'issue' if risk == 'issue' else 'normal'}
        self.entries.append({'key': key, 'step': step, 'source': source, 'needs_ai': False})

    def mark(self, key: tuple, kind: str) -> None:
        """``back`` undoes the way into a dead end; ``reload`` empties what was typed on the screen."""
        if not self.blocked:
            self.entries.append({'key': key, 'mark': kind})

    def taint(self, key: tuple | None) -> None:
        """A person acted on this screen, or it was left half done: it is not repeated alone."""
        if key is not None:
            self.tainted.add(key)

    def block(self, key: tuple | None) -> None:
        """A credential is in play: nothing else of this attempt is noted."""
        self.taint(key)
        self.blocked = True

    @harmless((None, False))
    def recall(self, key: tuple, family_allowed: bool) -> tuple[list[dict] | None, bool]:
        """Steps the memory has for this screen and whether they come from another portal."""
        row = self.memory.screen(key, self.known.variant)
        if row is not None:
            usable = row['active'] and not row['needs_ai'] and row['steps']
            return (row['steps'] if usable else None), False
        if not family_allowed:
            return None, False
        return self.memory.family(key[2], self.known.variant), True

    def replay_started(self, key: tuple, steps: list[dict], family: bool) -> None:
        previous = self.replays.get(key, {})
        self.replays[key] = {'state': 'running', 'steps': steps, 'family': family,
                             'starts': previous.get('starts', 0) + 1}

    def replay_done(self, key: tuple) -> None:
        if key in self.replays and self.replays[key]['state'] == 'running':
            self.replays[key]['state'] = 'done'

    @harmless(False)
    def replay_failed(self, key: tuple) -> bool:
        """Count a failure of the repeated screen once per attempt. True when it got switched off."""
        replay = self.replays.get(key)
        if not replay or replay['state'] == 'failed':
            return False
        replay['state'] = 'failed'
        return self.memory.failed(key, self.known.variant, replay['steps'])

    def state(self, key: tuple) -> str:
        return self.replays.get(key, {}).get('state', '')

    def starts(self, key: tuple) -> int:
        return self.replays.get(key, {}).get('starts', 0)

    def screens(self) -> list[dict]:
        """The attempt as it would be stored: one entry per screen, with nothing personal in it."""
        walked: list[dict] = []
        for entry in self.entries:
            mark = entry.get('mark')
            if mark:
                while walked and walked[-1]['key'] == entry['key']:
                    walked.pop()
                if mark == 'back' and walked:
                    walked.pop()  # The step that led into the dead end.
            else:
                walked.append(entry)
        grouped: dict[tuple, list[dict]] = {}
        for entry in walked:
            grouped.setdefault(entry['key'], []).append(entry)
        result = []
        for key, entries in grouped.items():
            needs_ai = key in self.tainted or any(entry['needs_ai'] for entry in entries)
            steps = None if needs_ai else compact([entry['step'] for entry in entries])
            replayed = all(entry['source'] == 'memoria' for entry in entries) and self.state(key) == 'done'
            if replayed and key in self.tainted:
                # Repeated, but a person also worked there: it proves nothing for or against the recipe.
                continue
            if replayed and not needs_ai:
                # A repeated screen keeps the steps it already had, even if this time fewer were needed.
                result.append({'key': list(key), 'steps': self.replays[key]['steps'], 'needs_ai': False, 'replayed': True})
            else:
                result.append({'key': list(key), 'steps': steps or [], 'needs_ai': steps is None, 'replayed': False})
        return result

    def export(self) -> dict:
        return {'variant': self.known.variant, 'screens': self.screens()}

    @harmless(0)
    def commit(self) -> int:
        """The invoice was verified: write what was learned. Returns how many screens are new or changed."""
        return self.memory.apply(self.export())

    @harmless(None)
    def hold(self, job_id: str) -> None:
        self.memory.hold(job_id, self.export())

    @harmless(None)
    def abandon(self, penalize: bool = True) -> None:
        """A failed attempt teaches nothing; it only counts against what it repeated."""
        if penalize:
            for key, replay in list(self.replays.items()):
                if replay['state'] in {'running', 'done'}:
                    self.replay_failed(key)
        self.entries.clear()
