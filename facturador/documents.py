from __future__ import annotations

import hashlib
import re
import shutil
import sqlite3
from pathlib import Path

from .cfdi import collect_documents, find_valid_pair
from .db import Store
from .models import Profile, Receipt


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


def reconcile(store: Store, key: str, profile: Profile, paths: list[Path]) -> dict:
    row = store.get(key)
    receipt = Receipt(**row["receipt"])
    files = collect_documents(paths, store.root / "downloads" / key / "extracted")
    result, xml, pdf = find_valid_pair(files, profile, receipt)
    if row["uuid"] and row["uuid"] != result["uuid"]:
        raise ValueError("El UUID recuperado no coincide con el resultado registrado en el portal")
    for other in store.list():
        if other["id"] != key and other["uuid"] == result["uuid"]:
            store.update(key, status="duplicate", message=f"UUID ya registrado en {other['id']}")
            raise ValueError("El mismo CFDI ya pertenece a otro registro; no se duplicó")
    dest = store.root / "cfdi" / result["uuid"]
    dest.mkdir(parents=True, exist_ok=True, mode=0o700)
    for source, suffix in [(xml, ".xml"), (pdf, ".pdf")]:
        target = dest / ("factura" + suffix)
        if target.exists() and target.read_bytes() != source.read_bytes():
            raise ValueError("Hay otro archivo distinto guardado con el mismo UUID; requiere revisión")
        if source.resolve() != target.resolve():
            shutil.copyfile(source, target)
        target.chmod(0o600)
    try:
        store.update(key, status="verified", uuid=result["uuid"], xml=str(dest / "factura.xml"),
                     pdf=str(dest / "factura.pdf"), message="PDF/XML coinciden con el perfil, importe, forma de pago y UUID")
    except sqlite3.IntegrityError as exc:
        raise ValueError("UUID ya registrado; revisar el expediente existente") from exc
    store.event(key, "verified", str({**result, "xml_sha256": hashlib.sha256(xml.read_bytes()).hexdigest()}))
    return result
