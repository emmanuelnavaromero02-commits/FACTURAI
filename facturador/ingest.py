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
HEIF_EXT = {".heic", ".heif"}
MAX_PIXELS = 40_000_000
Image.MAX_IMAGE_PIXELS = MAX_PIXELS


def heif_to_jpeg(name: str, data: bytes) -> tuple[str, bytes]:
    """Convert an iPhone HEIC/HEIF photo to a JPEG that is kept as the original."""
    if Path(name).suffix.lower() not in HEIF_EXT:
        return name, data
    if not data or len(data) > MAX_UPLOAD:
        raise ValueError("Elige un documento de hasta 25 MB")
    from PIL import ImageOps
    import pillow_heif

    pillow_heif.register_heif_opener()
    with Image.open(io.BytesIO(data)) as img:
        if img.format not in {'HEIF', 'AVIF'}:
            raise ValueError('La foto HEIC está dañada o no es válida.')
        if img.width * img.height > MAX_PIXELS:
            raise ValueError("Imagen demasiado grande")
        picture = ImageOps.exif_transpose(img).convert('RGB')
    output = io.BytesIO()
    picture.save(output, 'JPEG', quality=92)
    return Path(name).with_suffix('.jpg').name, output.getvalue()


def validate_document(name: str, data: bytes) -> str:
    if not data or len(data) > MAX_UPLOAD:
        raise ValueError("Elige un documento de hasta 25 MB")
    suffix = Path(name).suffix.lower()
    if suffix in IMAGE_EXT:
        with Image.open(io.BytesIO(data)) as img:
            if img.width * img.height > 40_000_000:
                raise ValueError("Imagen demasiado grande")
            if img.format in {'HEIF', 'AVIF'}:
                # HEIC photos are converted to JPEG on arrival; they are never stored under another extension.
                raise ValueError('La extensión no coincide con el contenido del documento.')
            mime = Image.MIME.get(img.format)
            img.verify()
    elif suffix == ".pdf":
        if not data.startswith(b"%PDF"):
            raise ValueError("PDF inválido")
        if len(PdfReader(io.BytesIO(data)).pages) > 20:
            raise ValueError("Límite de 20 páginas")
        mime = 'application/pdf'
    else:
        raise ValueError("Sube JPG, PNG, WEBP, HEIC o PDF; ZIP de exportación de WhatsApp también está admitido")
    return mime


def save_input(store: Store, name: str, data: bytes, source: str = "archivo", batch_id: str = '') -> dict:
    return save_inputs(store, [(name, data)], source, batch_id=batch_id)[0]


def save_inputs(store: Store, items: list[tuple[str, bytes]], source: str, *, batch_id: str = '') -> list[dict]:
    for name, data in items:
        if Path(name).suffix.lower() == '.txt' and (source == 'Gmail' or source.startswith('EML: ')):
            if not data.strip() or len(data) > 256_000:
                raise ValueError('El texto elegido está vacío o supera el límite.')
        else:
            validate_document(name, data)
    return [_store_original(store, name, data, source, batch_id) for name, data in items]


def _store_original(store: Store, name: str, data: bytes, source: str, batch_id: str = '') -> dict:
    suffix = Path(name).suffix.lower()
    sha = hashlib.sha256(data).hexdigest()
    directory = store.root / "originales"
    directory.mkdir(exist_ok=True, mode=0o700)
    path = directory / (sha + suffix)
    write_original(path, data)
    row, created = store.add(str(path), sha, source + ": " + Path(name).name[:100], batch_id=batch_id)
    return {"id": row["id"], "created": created, "status": row["status"]}


def write_original(path: Path, data: bytes) -> None:
    if path.exists():
        return
    with NamedTemporaryFile(dir=path.parent, prefix='pending_', delete=False) as output:
        temporary = Path(output.name)
    try:
        temporary.write_bytes(data)
        temporary.replace(path)
    finally:
        temporary.unlink(missing_ok=True)


def save_upload(store: Store, name: str, data: bytes, source: str = "archivo", batch_id: str = '') -> list[dict]:
    if Path(name).suffix.lower() != ".zip":
        name, data = heif_to_jpeg(name, data)
        return [save_input(store, name, data, source, batch_id=batch_id)]
    if len(data) > MAX_UPLOAD:
        raise ValueError("ZIP supera 25 MB")
    result = []
    with zipfile.ZipFile(io.BytesIO(data)) as archive:
        members = [m for m in archive.infolist() if not m.is_dir() and Path(m.filename).suffix.lower() in IMAGE_EXT | HEIF_EXT | {".pdf"}]
        if len(members) > 100 or sum(m.file_size for m in members) > 100 * 1024 * 1024:
            raise ValueError("ZIP supera 100 documentos o 100 MB descomprimidos")
        for member in members:
            if member.file_size > MAX_UPLOAD:
                raise ValueError("Archivo del ZIP demasiado grande")
            member_name, member_data = heif_to_jpeg(member.filename, archive.read(member))
            result.append(save_input(store, member_name, member_data, source + " / ZIP", batch_id=batch_id))
    return result
