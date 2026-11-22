from __future__ import annotations

import hashlib
import io
import zipfile
from pathlib import Path
from tempfile import NamedTemporaryFile

from PIL import Image
from pypdf import PdfReader

from .db import Store

MAX_UPLOAD = 25 * 1024 * 1024
IMAGE_EXT = {".jpg", ".jpeg", ".png", ".webp"}
Image.MAX_IMAGE_PIXELS = 40_000_000


def validate_document(name: str, data: bytes) -> str:
    if not data or len(data) > MAX_UPLOAD:
        raise ValueError("Elige un documento de hasta 25 MB")
    suffix = Path(name).suffix.lower()
    if suffix in IMAGE_EXT:
        with Image.open(io.BytesIO(data)) as img:
            if img.width * img.height > 40_000_000:
                raise ValueError("Imagen demasiado grande")
            mime = Image.MIME.get(img.format)
            img.verify()
    elif suffix == ".pdf":
        if not data.startswith(b"%PDF"):
            raise ValueError("PDF inválido")
        if len(PdfReader(io.BytesIO(data)).pages) > 20:
            raise ValueError("Límite de 20 páginas")
        mime = 'application/pdf'
    else:
        raise ValueError("Sube JPG, PNG, WEBP o PDF; ZIP de exportación de WhatsApp también está admitido")
    return mime


def save_input(store: Store, name: str, data: bytes, source: str = "archivo") -> dict:
    return save_inputs(store, [(name, data)], source)[0]


def save_inputs(store: Store, items: list[tuple[str, bytes]], source: str) -> list[dict]:
    for name, data in items:
        if Path(name).suffix.lower() == '.txt' and source == 'Gmail':
            if not data.strip() or len(data) > 256_000:
                raise ValueError('El texto elegido está vacío o supera el límite.')
        else:
            validate_document(name, data)
    return [_store_original(store, name, data, source) for name, data in items]


def _store_original(store: Store, name: str, data: bytes, source: str) -> dict:
    suffix = Path(name).suffix.lower()
    sha = hashlib.sha256(data).hexdigest()
    directory = store.root / "originales"
    directory.mkdir(exist_ok=True, mode=0o700)
    path = directory / (sha + suffix)
    if not path.exists():
        with NamedTemporaryFile(dir=directory, prefix='pending_', delete=False) as output:
            temp = Path(output.name)
        try:
            temp.write_bytes(data)
            temp.replace(path)
        finally:
            temp.unlink(missing_ok=True)
    row, created = store.add(str(path), sha, source + ": " + Path(name).name[:100])
    return {"id": row["id"], "created": created, "status": row["status"]}


def save_upload(store: Store, name: str, data: bytes, source: str = "archivo") -> list[dict]:
    if Path(name).suffix.lower() != ".zip":
        return [save_input(store, name, data, source)]
    if len(data) > MAX_UPLOAD:
        raise ValueError("ZIP supera 25 MB")
    result = []
    with zipfile.ZipFile(io.BytesIO(data)) as archive:
        members = [m for m in archive.infolist() if not m.is_dir() and Path(m.filename).suffix.lower() in IMAGE_EXT | {".pdf"}]
        if len(members) > 100 or sum(m.file_size for m in members) > 100 * 1024 * 1024:
            raise ValueError("ZIP supera 100 documentos o 100 MB descomprimidos")
        for member in members:
            if member.file_size > MAX_UPLOAD:
                raise ValueError("Archivo del ZIP demasiado grande")
            result.append(save_input(store, member.filename, archive.read(member), source + " / ZIP"))
    return result
