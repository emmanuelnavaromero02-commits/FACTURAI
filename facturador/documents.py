from __future__ import annotations

import hashlib
import json
import re
import shutil
import threading
from tempfile import TemporaryDirectory, NamedTemporaryFile
from pathlib import Path

from .cfdi import MissingPair, collect_documents, find_valid_pair
from .db import Store, now
from .ingest import write_original
from .models import Profile, Receipt
from .portal_directory import learn_from_invoice

_finalize = threading.Lock()


def save_candidate(store: Store, key: str, filename: str, data: bytes) -> Path:
    suffix = Path(filename).suffix.lower()
    if suffix not in {'.pdf', '.xml', '.zip'} or not data or len(data) > 50 * 1024 * 1024:
        raise ValueError('Elige un PDF, XML o ZIP válido.')
    folder = store.root / 'downloads' / key / 'recibidos'
    folder.mkdir(parents=True, exist_ok=True, mode=0o700)
    target = folder / (hashlib.sha256(data).hexdigest() + suffix)
    write_original(target, data)
    return target


def job_candidates(store: Store, key: str) -> list[Path]:
    folder = store.root / 'downloads' / key
    receipt = Receipt.model_validate(store.get(key)['receipt'])
    with store.connect() as db:
        invoices = store.invoice_candidates(db, receipt)
    saved = [store.root / 'cfdi' / invoice / ('factura.' + suffix) for invoice in invoices for suffix in ('pdf', 'xml')]
    return [p for p in saved if p.is_file() and p.resolve() == p] + [p for p in folder.rglob('*') if p.suffix.lower() in {'.pdf', '.xml', '.zip'} and
            not p.name.startswith('pending_') and 'extracted' not in p.relative_to(folder).parts
            and p.is_file() and not p.is_symlink()]


def browser_file(root: Path, file_id: str) -> Path:
    if not re.fullmatch(r'[0-9a-f]{16}_[\w.()-]{1,150}', file_id):
        raise ValueError('Descarga desconocida.')
    path = root / 'descargas-navegador' / file_id
    if path.suffix.lower() not in {'.pdf', '.xml', '.zip'} or path.is_symlink() or not path.is_file():
        raise ValueError('Descarga desconocida.')
    return path


def browser_files(root: Path) -> list[dict]:
    directory = root / 'descargas-navegador'
    if not directory.exists():
        return []
    files = []
    for entry in directory.iterdir():
        if entry.name.startswith('pending_'):
            continue
        try:
            path = browser_file(root, entry.name)
            info = path.stat()
        except (OSError, ValueError):
            continue
        files.append({'id': path.name, 'name': path.name[17:], 'size': info.st_size, 'at': info.st_mtime})
    return sorted(files, key=lambda item: item['at'], reverse=True)


def reconcile(store: Store, key: str, profile: Profile, paths: list[Path] | None = None, *, include_partners=False) -> dict:
    row = store.get(key)
    receipt = Receipt(**row["receipt"])
    paths = list(dict.fromkeys(paths)) if paths is not None else job_candidates(store, key)
    with TemporaryDirectory(dir=store.root, prefix='cotejo-') as temp:
        files = collect_documents(paths, Path(temp) / 'elegidos')
        try:
            result, xml, pdf = find_valid_pair(files, profile, receipt, row['uuid'] or '')
        except MissingPair:
            if not include_partners:
                raise
            needed = '.pdf' if files[0].suffix.lower() == '.xml' else '.xml'
            other = [p for p in job_candidates(store, key) if p not in paths and p.suffix.lower() in {needed, '.zip'}]
            partners = [p for p in collect_documents(other, Path(temp) / 'anteriores') if p.suffix.lower() == needed]
            if not partners:
                raise
            result, xml, pdf = find_valid_pair([*files, *partners], profile, receipt, row['uuid'] or '')
        with _finalize:
            try:
                with store.connect() as db:
                    db.execute('BEGIN IMMEDIATE')
                    return _record_pair(store, db, key, receipt, result, xml, pdf)
            except AssignedInvoice as exc:
                with store.connect() as db:
                    db.execute("UPDATE jobs SET status='duplicate',message=?,updated=? WHERE id=? AND status NOT IN ('verified','historical')",
                               (str(exc), now(), key))
                raise


class AssignedInvoice(ValueError):
    """An invoice or concept was already assigned to another ticket."""


def _record_pair(store: Store, db, key: str, receipt: Receipt, result: dict, xml: Path, pdf: Path) -> dict:
    fiscal_uuid = result['uuid']
    # All codes printed in a grouped invoice stay private. Return only the coverage of this ticket.
    public = {k:v for k,v in result.items() if k != 'invoice_codes'}
    row = db.execute(store._job_select + ' WHERE j.id=?', (key,)).fetchone()
    if row is None or Receipt.model_validate_json(row['receipt']) != receipt:
        raise ValueError('El ticket cambió durante el cotejo. Vuelve a recuperar los comprobantes.')
    previous_uuid = row['matched_uuid'] or row['observed_uuid'] or row['uuid']
    if previous_uuid and previous_uuid != fiscal_uuid:
        raise ValueError("El UUID recuperado no coincide con el resultado registrado en el portal")
    owner = db.execute(store._job_select + ' WHERE j.uuid=?', (fiscal_uuid,)).fetchone()
    grouped = result['match_type'] == 'agrupado'
    if (grouped and owner and owner['id'] != key and not owner['historical'] and
            not owner['match_type'] and not owner['pdf'] and not owner['xml'] and
            json.loads(owner['receipt'])['provider'] == 'capufe'):
        # A portal observation is not an allocation. The first verified pair becomes
        # the canonical owner while the other ticket retains its observed UUID.
        db.execute('INSERT OR IGNORE INTO observed_shared_invoices VALUES(?,?)', (owner['id'], fiscal_uuid))
        db.execute('UPDATE jobs SET uuid=NULL WHERE id=?', (owner['id'],))
        owner = None
    if owner and owner['id'] != key and not (grouped and owner['match_type'] == 'agrupado' and owner['status'] == 'verified'):
        raise AssignedInvoice('El CFDI completo ya corresponde a otro expediente.')
    if row['status'] in {'historical', 'duplicate'}:
        raise ValueError('Este expediente ya está identificado como histórico o duplicado.')
    xml_hash = hashlib.sha256(xml.read_bytes()).hexdigest()
    evidence = db.execute('SELECT xml_sha256 FROM invoice_matches WHERE invoice_uuid=? LIMIT 1', (fiscal_uuid,)).fetchone()
    if evidence and evidence['xml_sha256'] != xml_hash:
        raise ValueError('Hay otro XML distinto guardado con el mismo UUID.')
    for concept, code in zip(result['matched_concepts'], result['matched_codes'], strict=True):
        previous = db.execute("""SELECT invoice_uuid,concept,job_id FROM invoice_items WHERE job_id IS NOT NULL
                              AND (code=? OR (invoice_uuid=? AND concept=?))""", (code, fiscal_uuid, concept)).fetchone()
        if previous and (previous['job_id'] != key or previous['concept'] != concept or previous['invoice_uuid'] != fiscal_uuid):
            raise AssignedInvoice('La partida de este ticket ya fue imputada a otro expediente.')
    dest = store.root / "cfdi" / fiscal_uuid
    dest.mkdir(parents=True, exist_ok=True, mode=0o700)
    documents = [(xml, ".xml"), (pdf, ".pdf")]
    for source, suffix in documents:
        target = dest / ("factura" + suffix)
        if target.exists() and target.read_bytes() != source.read_bytes():
            raise ValueError("Hay otro archivo distinto guardado con el mismo UUID; requiere revisión")
    for source, suffix in documents:
        target = dest / ("factura" + suffix)
        if not target.exists():
            with NamedTemporaryFile(dir=dest, prefix='pending_', delete=False) as stream:
                interim = Path(stream.name)
            try:
                shutil.copyfile(source, interim)
                interim.replace(target)
            finally:
                interim.unlink(missing_ok=True)
    if row['matched_uuid']:
        return public
    for concept, code in enumerate(result['invoice_codes']):
        db.execute('INSERT OR IGNORE INTO invoice_items(invoice_uuid,concept,code) VALUES(?,?,?)', (fiscal_uuid, concept, code))
    for concept, code in zip(result['matched_concepts'], result['matched_codes'], strict=True):
        if not db.execute('UPDATE invoice_items SET job_id=? WHERE invoice_uuid=? AND concept=? AND code=?',
                          (key, fiscal_uuid, concept, code)).rowcount:
            raise ValueError('La partida cotejada no coincide con el índice del CFDI.')
    db.execute('INSERT INTO invoice_matches(job_id,invoice_uuid,invoice_total,ticket_total,match_type,concept_count,xml_sha256) VALUES(?,?,?,?,?,?,?)',
               (key, fiscal_uuid, result['total'], result['ticket_total'], result['match_type'], result['concept_count'], xml_hash))
    message = ('La partida del ticket coincide con el PDF y XML del CFDI agrupado.' if grouped
               else 'PDF/XML coinciden con el perfil, importe, forma de pago y UUID')
    db.execute("""UPDATE jobs SET status='verified',uuid=?,issuance_seen=1,xml=?,pdf=?,message=?,updated=? WHERE id=?""",
               (fiscal_uuid if not owner or owner['id'] == key else None, str(dest/'factura.xml'), str(dest/'factura.pdf'), message, now(), key))
    learn_from_invoice(store.root, receipt, dest / 'factura.xml')  # Best effort; never raises.
    db.execute('DELETE FROM observed_shared_invoices WHERE job_id=?', (key,))
    db.execute('INSERT INTO events(job_id,at,kind,detail) VALUES(?,?,?,?)',
               (key, now(), 'verified', json.dumps({**public, 'xml_sha256': xml_hash}, ensure_ascii=False)[:5000]))
    if result['invoice_codes']:
        waiting = db.execute("""SELECT id FROM jobs WHERE id<>? AND status IN ('ready','review','blocked','queued','running')
                              AND json_extract(receipt,'$.provider')='capufe'""", (key,)).fetchall()
        for other in waiting:
            store.hold_archived_ticket(db, other['id'])
    return public
