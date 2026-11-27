"""Find the invoice of a ticket that waits for its files in any connected mailbox.

The fiscal UUID is used first when it is known. Otherwise every recent message with XML, PDF or ZIP
attachments is checked against the profile and the ticket with the existing cotejo, and a document is
kept only when exactly one invoice matches exactly one waiting ticket.
"""
from __future__ import annotations

import asyncio
import json
import logging
import time
from contextlib import ExitStack
from datetime import date, datetime, timezone
from pathlib import Path
from tempfile import TemporaryDirectory

from . import buzon
from xml.etree import ElementTree as ET

from .cfdi import NS, MissingPair, PDF_UUID, UUID_RE, collect_documents, normalized, verify_xml
from .correo_envio import create_tables as create_mail_tables
from .documents import reconcile, save_candidate
from .gmail import cfdi_parts
from .models import Profile, Receipt, money
from .read_http import ServiceWait

# (elapsed seconds since the search began, delay until the next search)
SCHEDULE = ((30 * 60, 120), (6 * 3600, 600), (72 * 3600, 3600))
MAX_MESSAGES = 60
MAX_ROUND_BYTES = 100 * 1024 * 1024
SEEN_DAYS = 30

PENDING = 'Aún no llega por correo una factura que coincida con este ticket. Se seguirá buscando.'
HALF = 'Llegó por correo parte de la factura. Falta el PDF o el XML para completar el cotejo.'
AMBIGUOUS = ('Llegaron por correo varias facturas que podrían ser de este ticket. Revisa tu correo y '
             'adjunta al expediente la que corresponde.')
FINISHED = 'La búsqueda automática en el correo terminó. Puedes recuperar los archivos desde el expediente.'
UNREADABLE = 'No se pudo abrir tu correo. Revisa la conexión en Ajustes.'
_LOG = logging.getLogger(__name__)


def next_delay(elapsed: float) -> int | None:
    for limit, delay in SCHEDULE:
        if elapsed < limit:
            return delay
    return None


def create_tables(db):
    db.execute('''CREATE TABLE IF NOT EXISTS invoice_mail_checks (
        job_id TEXT PRIMARY KEY, uuid TEXT NOT NULL, attempts INTEGER NOT NULL, next_check INTEGER NOT NULL)''')
    columns = {row[1] for row in db.execute('PRAGMA table_info(invoice_mail_checks)')}
    if 'started' not in columns:
        db.execute('ALTER TABLE invoice_mail_checks ADD COLUMN started INTEGER')
    if 'state' not in columns:
        db.execute("ALTER TABLE invoice_mail_checks ADD COLUMN state TEXT NOT NULL DEFAULT 'searching'")
        # Rows that already ran out under the former four-step calendar stay finished.
        db.execute("UPDATE invoice_mail_checks SET state='done' WHERE attempts>4")
    if 'detail' not in columns:
        db.execute("ALTER TABLE invoice_mail_checks ADD COLUMN detail TEXT NOT NULL DEFAULT ''")
    db.execute('''CREATE TABLE IF NOT EXISTS invoice_mail_seen (
        ref TEXT PRIMARY KEY, uuids TEXT NOT NULL, checked INTEGER NOT NULL)''')
    create_mail_tables(db)


def _waiting_clause(db) -> str:
    requested = ["j.id IN (SELECT job_id FROM mail_requests WHERE state IN ('sent','uncertain'))"]
    if db.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='whatsapp_requests'").fetchone():
        requested.append("j.id IN (SELECT job_id FROM whatsapp_requests WHERE state IN ('sent','uncertain'))")
    return (f"""j.historical=0 AND j.billing_profile IS NOT NULL AND (
        (j.status='issued_waiting_files' AND j.issuance_seen=1)
        OR (j.status='external' AND j.issue_attempted=1 AND ({' OR '.join(requested)})))""")


def _since(receipt: Receipt, fallback: float) -> float:
    try:
        day = date.fromisoformat(receipt.date)
    except ValueError:
        return fallback - 7 * 86400
    # Local midnight in Mexico is later than UTC midnight; one extra day covers any zone.
    return datetime(day.year, day.month, day.day, tzinfo=timezone.utc).timestamp() - 86400


class _Message:
    def __init__(self, ref, files):
        self.ref = ref
        self.files = files                 # extracted XML and PDF paths of this message
        self.xml = {}                      # path -> stamp summary


def _stamp(data: bytes) -> dict:
    """UUID, receiver RFC and total of a CFDI, read only to decide whether it is worth a cotejo."""
    if len(data) > 25 * 1024 * 1024 or b'<!DOCTYPE' in data.upper() or b'<!ENTITY' in data.upper():
        return {}
    try:
        root = ET.fromstring(data)
    except ET.ParseError:
        return {}
    stamp = root.find('c:Complemento/t:TimbreFiscalDigital', NS)
    receiver = root.find('c:Receptor', NS)
    value = (stamp.get('UUID', '') if stamp is not None else '').upper()
    if not UUID_RE.fullmatch(value):
        return {}
    return {'uuid': value, 'rfc': (receiver.get('Rfc', '') if receiver is not None else '').upper(),
            'total': root.get('Total', '')}


def _could_match(entry: dict, jobs) -> bool:
    if entry.get('pdf'):
        return True
    for job in jobs:
        if normalized(entry.get('rfc', '')) != normalized(job['buyer'].rfc):
            continue
        if job['receipt'].provider == 'capufe':
            return True
        try:
            if money(entry.get('total', '')) == money(job['receipt'].amount):
                return True
        except ValueError:
            continue
    return False


def _pdf_uuids(path: Path) -> set[str]:
    from pypdf import PdfReader
    try:
        reader = PdfReader(str(path))
        text = '\n'.join(page.extract_text() or '' for page in reader.pages[:4])
    except Exception:
        return set()
    return {u.upper() for u in PDF_UUID.findall(text)}


class InvoiceMailRecovery:
    def __init__(self, store, enabled, active_job, mailboxes=None, tick: int = 60):
        self.store = store
        self.enabled = enabled
        self.active_job = active_job
        self.mailboxes = mailboxes or (lambda: buzon.open_mailboxes(store.root))
        self.tick = tick
        self.task = None
        self.stop = asyncio.Event()
        with store.connect() as db:
            create_tables(db)

    def start(self):
        self.stop.clear()
        self.task = asyncio.create_task(self.run())

    async def close(self):
        self.stop.set()
        if self.task:
            await self.task

    # ------------------------------------------------------------ calendar

    def due(self, now: int) -> list[dict]:
        """Claim every waiting ticket whose next search is due and book the following one."""
        claimed, finished = [], []
        # The ticket a browser is working on now; several of them when several engines work at once.
        active = self.active_job()
        busy = list(active) if isinstance(active, (list, tuple, set)) else [active or '']
        with self.store.connect() as db:
            db.execute('BEGIN IMMEDIATE')
            rows = db.execute(f'''SELECT j.id,j.uuid,j.billing_profile,j.receipt,j.status,
                    COALESCE(c.attempts,0) AS attempts,c.started,COALESCE(c.detail,'') AS detail
                FROM jobs j LEFT JOIN invoice_mail_checks c ON c.job_id=j.id
                WHERE {_waiting_clause(db)} AND j.id NOT IN ({','.join('?' * len(busy)) or "''"})
                  AND COALESCE(c.state,'searching')='searching'
                  AND COALESCE(c.next_check,0)<=? ORDER BY j.updated,j.id''',
                              (*busy, now)).fetchall()
            for row in rows:
                started = row['started'] or now
                delay = next_delay(now - started)
                if delay is None:
                    db.execute("UPDATE invoice_mail_checks SET state='done',detail=? WHERE job_id=?", (FINISHED, row['id']))
                    finished.append(row['id'])
                    continue
                db.execute('''INSERT INTO invoice_mail_checks(job_id,uuid,attempts,next_check,started,state)
                    VALUES(?,?,?,?,?,'searching') ON CONFLICT(job_id) DO UPDATE SET uuid=excluded.uuid,
                    attempts=excluded.attempts,next_check=excluded.next_check,started=excluded.started''',
                           (row['id'], row['uuid'] or '', row['attempts'] + 1, now + delay, started))
                claimed.append({**dict(row), 'attempts': row['attempts'] + 1, 'started': started})
        for key in finished:
            self.store.event(key, 'gmail_recovery', FINISHED)
        return claimed

    def _note(self, key, detail, state='searching'):
        with self.store.connect() as db:
            previous = db.execute('SELECT detail FROM invoice_mail_checks WHERE job_id=?', (key,)).fetchone()
            db.execute('UPDATE invoice_mail_checks SET state=?,detail=? WHERE job_id=?', (state, detail, key))
        # One event per change of situation, not one per search.
        if previous is None or previous['detail'] != detail:
            self.store.event(key, 'gmail_recovery', detail)

    def _postpone(self, rows, seconds):
        with self.store.connect() as db:
            for row in rows:
                db.execute('UPDATE invoice_mail_checks SET next_check=MAX(next_check,?) WHERE job_id=?',
                           (int(time.time()) + seconds + 1, row['id']))

    # ------------------------------------------------------------ search

    def recover(self, rows: list[dict]) -> dict:
        """One session per mailbox for every claimed ticket. Returns key -> outcome."""
        waiting = []
        for row in rows:
            job = self.store.get(row['id'])
            if job['status'] in {'issued_waiting_files', 'external', 'uncertain'} and job['billing_profile']:
                waiting.append({'id': row['id'], 'uuid': (job['uuid'] or '').upper(),
                                'buyer': Profile.model_validate_json(job['billing_profile']),
                                'receipt': Receipt(**job['receipt']), 'attempts': row.get('attempts', 1)})
        outcome = {}
        if not waiting:
            return outcome
        boxes = self.mailboxes()
        if not boxes:
            return outcome
        with ExitStack() as stack, TemporaryDirectory(dir=self.store.root, prefix='correo-') as temp:
            opened = []
            for box in boxes:
                try:
                    stack.enter_context(box)
                    opened.append(box)
                except ServiceWait:
                    raise
                except ValueError:
                    _LOG.warning('No se abrió un buzón para buscar comprobantes.')
            if not opened:
                for job in waiting:
                    self._note(job['id'], UNREADABLE)
                    outcome[job['id']] = 'unreadable'
                return outcome
            for job in waiting:
                if job['uuid'] and UUID_RE.fullmatch(job['uuid']):
                    outcome[job['id']] = self._by_uuid(opened, job)
            remaining = [job for job in waiting if outcome.get(job['id']) != 'verified']
            if remaining:
                messages = self._collect(opened, remaining, Path(temp))
                outcome.update(self._assign(remaining, messages, outcome))
        return outcome

    def _by_uuid(self, boxes, job) -> str:
        """The former exact search: messages that mention the confirmed UUID."""
        key = job['id']
        for box in boxes:
            for row in box.search(text=job['uuid'], attachments=True, limit=20):
                message = box.read(row['ref'])
                if message is None:
                    continue
                for filename, data in cfdi_parts(message):
                    save_candidate(self.store, key, filename, data)
        return self._reconcile(job)

    def _reconcile(self, job) -> str:
        key = job['id']
        try:
            reconcile(self.store, key, job['buyer'])
        except MissingPair:
            return 'half' if any(p.suffix.lower() in {'.xml', '.pdf'} for p in
                                 (self.store.root / 'downloads' / key).rglob('*')) else 'pending'
        except ValueError as exc:
            return 'ambiguous' if 'varios CFDI' in str(exc) else 'pending'
        with self.store.connect() as db:
            db.execute("UPDATE invoice_mail_checks SET state='done',detail='' WHERE job_id=?", (key,))
        return 'verified'

    def _known(self, ref):
        with self.store.connect() as db:
            row = db.execute('SELECT uuids FROM invoice_mail_seen WHERE ref=?', (ref,)).fetchone()
        if row is None:
            return None
        try:
            entries = json.loads(row['uuids'])
        except ValueError:
            return None
        return entries if isinstance(entries, list) and all(isinstance(e, dict) for e in entries) else None

    def _remember(self, ref, entries):
        with self.store.connect() as db:
            db.execute('INSERT OR REPLACE INTO invoice_mail_seen VALUES(?,?,?)',
                       (ref, json.dumps(entries, sort_keys=True), int(time.time())))
            db.execute('DELETE FROM invoice_mail_seen WHERE checked<?', (int(time.time()) - SEEN_DAYS * 86400,))

    def _owned(self, uuid_value, key=None) -> bool:
        with self.store.connect() as db:
            return bool(db.execute('''SELECT 1 FROM jobs WHERE uuid=? AND id<>? UNION
                SELECT 1 FROM invoice_matches WHERE invoice_uuid=? AND job_id<>? LIMIT 1''',
                                   (uuid_value, key or '', uuid_value, key or '')).fetchone())

    def _collect(self, boxes, jobs, temp: Path) -> list[_Message]:
        since = min(_since(job['receipt'], time.time()) for job in jobs)
        messages, total = [], 0
        for box in boxes:
            for row in box.search(since=since, attachments=True, limit=MAX_MESSAGES):
                known = self._known(row['ref'])
                # Messages without a CFDI, or with invoices of other receivers, amounts or tickets,
                # are not downloaded again.
                if known is not None and not any(_could_match(e, jobs) and not self._owned(e['uuid']) for e in known):
                    continue
                message = box.read(row['ref'])
                if message is None:
                    continue
                folder = temp / str(len(messages))
                folder.mkdir()
                originals = []
                for index, (filename, data) in enumerate(cfdi_parts(message)):
                    total += len(data)
                    if total > MAX_ROUND_BYTES:
                        break
                    path = folder / f'{index:02d}{Path(filename).suffix.lower()}'
                    path.write_bytes(data)
                    originals.append(path)
                try:
                    files = collect_documents(originals, folder / 'extraidos')
                except ValueError:
                    files = []
                item = _Message(row['ref'], files)
                for path in files:
                    if path.suffix.lower() == '.xml':
                        item.xml[path] = _stamp(path.read_bytes())
                entries = [e for e in item.xml.values() if e]
                if not entries:
                    entries = [{'uuid': u, 'pdf': True} for p in files if p.suffix.lower() == '.pdf'
                               for u in sorted(_pdf_uuids(p))]
                self._remember(row['ref'], entries)
                if files:
                    messages.append(item)
                if total > MAX_ROUND_BYTES:
                    return messages
        return messages

    def _assign(self, jobs, messages, previous) -> dict:
        passing = {}     # job id -> {uuid: [(message, xml path)]}
        for job in jobs:
            found = {}
            for message in messages:
                for path in message.xml:
                    try:
                        result = verify_xml(path.read_bytes(), job['buyer'], job['receipt'])
                    except Exception:
                        continue
                    if job['uuid'] and result['uuid'] != job['uuid']:
                        continue
                    if self._owned(result['uuid'], job['id']):
                        continue
                    found.setdefault(result['uuid'], []).append((message, path))
            passing[job['id']] = found
        claims = {}
        for job in jobs:
            for uuid_value in passing[job['id']]:
                claims.setdefault(uuid_value, []).append(job)
        outcome = {}
        for job in jobs:
            key, found = job['id'], passing[job['id']]
            shared = [u for u in found if len(claims[u]) > 1 and not job['uuid']]
            if len(found) > 1 or shared:
                outcome[key] = 'ambiguous'
            elif found:
                fiscal_uuid, hits = next(iter(found.items()))
                for message, path in hits:
                    save_candidate(self.store, key, 'correo' + path.suffix, path.read_bytes())
                for message in messages:
                    for path in message.files:
                        if path.suffix.lower() == '.pdf' and (
                                any(m is message for m, _ in hits) or _pdf_uuids(path) == {fiscal_uuid}):
                            save_candidate(self.store, key, 'correo.pdf', path.read_bytes())
                outcome[key] = self._reconcile(job)
            else:
                outcome[key] = previous.get(key) if previous.get(key) in {'half', 'ambiguous'} else 'pending'
        for job in jobs:
            result = outcome[job['id']]
            if result == 'ambiguous':
                self._note(job['id'], AMBIGUOUS, 'ambiguous')
            elif result == 'half':
                self._note(job['id'], HALF)
            elif result == 'pending':
                self._note(job['id'], PENDING)
        return outcome

    # ------------------------------------------------------------ loop

    def round(self, now: int) -> dict:
        rows = self.due(now)
        if not rows:
            return {}
        try:
            return self.recover(rows)
        except ServiceWait as exc:
            self._postpone(rows, exc.seconds)
            for row in rows:
                self._note(row['id'], str(exc))
            return {}

    async def run(self):
        while not self.stop.is_set():
            try:
                if self.enabled() and buzon.mailbox_connected(self.store.root):
                    await asyncio.to_thread(self.round, int(time.time()))
            except Exception:
                _LOG.warning('No se completó el seguimiento de comprobantes por correo.')
            try:
                await asyncio.wait_for(self.stop.wait(), timeout=self.tick)
            except asyncio.TimeoutError:
                pass


def search_job_now(store, key: str, mailboxes=None) -> dict:
    """Search the mailboxes for one ticket right away, at the person's request."""
    job = store.get(key)
    if not job['billing_profile'] or job['status'] not in {'issued_waiting_files', 'external', 'uncertain'}:
        raise ValueError('Este expediente no espera archivos de factura.')
    worker = InvoiceMailRecovery(store, lambda: True, lambda: None, mailboxes)
    with store.connect() as db:
        db.execute('''INSERT INTO invoice_mail_checks(job_id,uuid,attempts,next_check,started,state)
            VALUES(?,?,0,0,?,'searching') ON CONFLICT(job_id) DO NOTHING''', (key, job['uuid'] or '', int(time.time())))
    if not worker.mailboxes():
        raise ValueError('Conecta tu correo en Ajustes para buscar la factura.')
    try:
        result = worker.recover([{'id': key, 'attempts': 1}]).get(key, 'pending')
    except ServiceWait as exc:
        return {'status': 'pending', 'message': str(exc)}
    messages = {'verified': 'PDF y XML cotejados.', 'half': HALF, 'ambiguous': AMBIGUOUS,
                'unreadable': UNREADABLE, 'pending': PENDING}
    return {'status': result, 'message': messages.get(result, PENDING)}
