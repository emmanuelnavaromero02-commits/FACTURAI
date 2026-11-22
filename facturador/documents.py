from __future__ import annotations

import hashlib
import shutil
import sqlite3
from pathlib import Path

from .cfdi import collect_documents, find_valid_pair
from .db import Store
from .models import Profile, Receipt


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
