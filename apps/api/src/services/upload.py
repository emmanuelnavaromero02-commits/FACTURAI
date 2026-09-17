import hashlib
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
    Soporta: JPEG, PNG, HEIC, WebP y PDF.
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
) -> Tuple[str, int, str, str]:
    """
    Valida los magic bytes y transmite el archivo al almacenamiento efímero (S3/MinIO).
    Calcula simultáneamente el hash SHA-256 para detección instantánea de duplicados.
    Si es una foto, aplica automáticamente escaneo CamScanner HD y genera una versión PDF nítida.
    Retorna: (storage_key, total_bytes, mime_type, file_hash)
    """
    first_chunk = await file.read(CHUNK_SIZE)
    if not first_chunk:
        raise TipoArchivoInvalidoException("El archivo enviado está vacío.")

    ext, mime_type = detect_file_type_from_magic_bytes(first_chunk)
    key = f"tickets/{tenant_id}/{ticket_id}"

    total_bytes = len(first_chunk)
    if total_bytes > MAX_FILE_SIZE_BYTES:
        raise ArchivoDemasiadoGrandeException()

    hasher = hashlib.sha256()
    hasher.update(first_chunk)

    # Si es imagen (JPEG, PNG, HEIC, WebP), procesar escaneo CamScanner HD y generar PDF
    if mime_type.startswith("image/"):
        chunks = [first_chunk]
        while True:
            chunk = await file.read(CHUNK_SIZE)
            if not chunk:
                break
            total_bytes += len(chunk)
            if total_bytes > MAX_FILE_SIZE_BYTES:
                raise ArchivoDemasiadoGrandeException()
            hasher.update(chunk)
            chunks.append(chunk)

        raw_bytes = b"".join(chunks)
        file_hash = hasher.hexdigest()

        # 1. Aplicar filtro CamScanner HD a la imagen
        try:
            from ..vision.extractor import enhance_receipt_image, convert_image_to_camscanner_pdf

            hd_image_bytes = enhance_receipt_image(raw_bytes)
            await storage.upload_bytes(key, hd_image_bytes, "image/jpeg")

            # 2. Generar y almacenar automáticamente el documento PDF escaneado
            pdf_bytes = convert_image_to_camscanner_pdf(hd_image_bytes)
            await storage.upload_bytes(f"{key}.pdf", pdf_bytes, "application/pdf")
        except Exception:
            # En caso de excepción, guardar la imagen original
            await storage.upload_bytes(key, raw_bytes, mime_type)

        return key, total_bytes, mime_type, file_hash

    # Si es documento PDF nativo
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
            hasher.update(chunk)
            yield chunk

    # Subida en streaming al bucket
    await storage.upload_stream(key, stream_generator(), mime_type)
    # También asegurar la referencia .pdf para endpoints uniformes
    try:
        pdf_data = await storage.get_bytes(key)
        await storage.upload_bytes(f"{key}.pdf", pdf_data, "application/pdf")
    except Exception:
        pass

    return key, total_bytes, mime_type, hasher.hexdigest()
