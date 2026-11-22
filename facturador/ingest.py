from __future__ import annotations

import hashlib
import io
import zipfile
from pathlib import Path

from PIL import Image
from pypdf import PdfReader

from .db import Store

MAX_UPLOAD = 25 * 1024 * 1024
IMAGE_EXT = {".jpg", ".jpeg", ".png", ".webp"}
Image.MAX_IMAGE_PIXELS = 40_000_000


def save_input(store: Store, name: str, data: bytes, source: str = "archivo") -> dict:
    if len(data) > MAX_UPLOAD:
        raise ValueError("Límite de 25 MB por archivo")
    suffix = Path(name).suffix.lower()
    if suffix in IMAGE_EXT:
        with Image.open(io.BytesIO(data)) as img:
            if img.width * img.height > 40_000_000:
                raise ValueError("Imagen demasiado grande")
            img.verify()
    elif suffix == ".pdf":
        if not data.startswith(b"%PDF"):
            raise ValueError("PDF inválido")
        if len(PdfReader(io.BytesIO(data)).pages) > 20:
            raise ValueError("Límite de 20 páginas")
    else:
        raise ValueError("Sube JPG, PNG, WEBP o PDF; ZIP de exportación de WhatsApp también está admitido")
    sha = hashlib.sha256(data).hexdigest()
    directory = store.root / "originales"
    directory.mkdir(exist_ok=True, mode=0o700)
    path = directory / (sha + suffix)
    if not path.exists():
        path.write_bytes(data)
        path.chmod(0o600)
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
