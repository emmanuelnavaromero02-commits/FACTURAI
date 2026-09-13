import uuid
from typing import AsyncGenerator, Tuple
from fastapi import UploadFile

from ..errors import (
    ArchivoDemasiadoGrandeException,
    TipoArchivoInvalidoException,
)
from ..storage import StorageService

MAX_FILE_SIZE_BYTES = 10 * 1024 * 1024  # 10 MB
CHUNK_SIZE = 64 * 1024  # 64 KB


def detect_file_type_from_magic_bytes(header: bytes) -> Tuple[str, str]:
    """
    Valida y detecta el tipo de archivo por sus 'magic bytes' iniciales.
    No confía en la extensión ni en el Content-Type que envíe el cliente.
    Soporta: JPEG, PNG, HEIC/HEIF, WebP y PDF.
    """
    if len(header) < 12:
        raise TipoArchivoInvalidoException(
            "El archivo es demasiado pequeño para ser una imagen o PDF válido."
        )

    # 1. JPEG: FF D8 FF
    if header.startswith(b"\xff\xd8\xff"):
        return "jpg", "image/jpeg"

    # 2. PNG: 89 50 4E 47 0D 0A 1A 0A
    if header.startswith(b"\x89PNG\r\n\x1a\n"):
        return "png", "image/png"

    # 3. PDF: %PDF- (25 50 44 46 2D)
    if header.startswith(b"%PDF-"):
        return "pdf", "application/pdf"

    # 4. WebP: RIFF....WEBP
    if header.startswith(b"RIFF") and header[8:12] == b"WEBP":
        return "webp", "image/webp"

    # 5. HEIC / HEIF / ISO Media: ftyp en offset 4
    if header[4:8] == b"ftyp":
        brand = header[8:12]
        compatible = header[8:min(32, len(header))]
        heic_brands = (b"heic", b"heix", b"hevc", b"hevx", b"mif1", b"msf1", b"heim", b"heis")
        if brand in heic_brands or any(b in compatible for b in (b"heic", b"heix", b"mif1", b"hevc")):
            return "heic", "image/heic"

    raise TipoArchivoInvalidoException(
        "El formato del archivo no está soportado. Solo aceptamos fotos en formato JPEG, PNG, HEIC, WebP o documentos PDF."
    )


async def process_and_stream_upload(
    file: UploadFile,
    tenant_id: uuid.UUID,
    ticket_id: uuid.UUID,
    storage: StorageService,
) -> Tuple[str, int, str]:
    """
    Valida los magic bytes y transmite el archivo en streaming a S3/MinIO
    sin cargar el contenido completo en memoria.
    Retorna: (storage_key, total_bytes, mime_type)
    """
    first_chunk = await file.read(CHUNK_SIZE)
    if not first_chunk:
        raise TipoArchivoInvalidoException("El archivo enviado está vacío.")

    ext, mime_type = detect_file_type_from_magic_bytes(first_chunk)
    key = f"tickets/{tenant_id}/{ticket_id}"

    total_bytes = len(first_chunk)
    if total_bytes > MAX_FILE_SIZE_BYTES:
        raise ArchivoDemasiadoGrandeException()

    async def stream_generator() -> AsyncGenerator[bytes, None]:
        nonlocal total_bytes
        yield first_chunk

        while True:
            chunk = await file.read(CHUNK_SIZE)
            if not chunk:
                break
            total_bytes += len(chunk)
            if total_bytes > MAX_FILE_SIZE_BYTES:
                raise ArchivoDemasiadoGrandeException()
            yield chunk

    # Subida en streaming al bucket
    await storage.upload_stream(key, stream_generator(), mime_type)

    return key, total_bytes, mime_type
