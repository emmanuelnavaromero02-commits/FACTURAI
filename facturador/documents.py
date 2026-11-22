from __future__ import annotations

import hashlib
import re
import shutil
import sqlite3
import threading
from tempfile import TemporaryDirectory, NamedTemporaryFile
from pathlib import Path

from .cfdi import MissingPair, collect_documents, find_valid_pair
from .db import Store
from .ingest import write_original
from .models import Profile, Receipt

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
    return [p for p in folder.rglob('*') if p.suffix.lower() in {'.pdf', '.xml', '.zip'} and
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
            return _record_pair(store, key, result, xml, pdf)


def _record_pair(store: Store, key: str, result: dict, xml: Path, pdf: Path) -> dict:
    row = store.get(key)
    if row["uuid"] and row["uuid"] != result["uuid"]:
        raise ValueError("El UUID recuperado no coincide con el resultado registrado en el portal")
    for other in store.list():
        if other["id"] != key and other["uuid"] == result["uuid"]:
            store.update(key, status="duplicate", message=f"UUID ya registrado en {other['id']}")
            raise ValueError("El mismo CFDI ya pertenece a otro registro; no se duplicó")
    dest = store.root / "cfdi" / result["uuid"]
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
    if row['status'] == 'verified' and row['uuid'] == result['uuid'] and row['pdf'] and row['xml']:
        return result
    try:
        store.update(key, status="verified", uuid=result["uuid"], xml=str(dest / "factura.xml"),
                     pdf=str(dest / "factura.pdf"), message="PDF/XML coinciden con el perfil, importe, forma de pago y UUID")
    except sqlite3.IntegrityError as exc:
        raise ValueError("UUID ya registrado; revisar el expediente existente") from exc
    store.event(key, "verified", str({**result, "xml_sha256": hashlib.sha256(xml.read_bytes()).hexdigest()}))
    return result
