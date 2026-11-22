"""Lectura local de una página de ticket desde la imagen o PDF original."""

import io
import subprocess
import threading
from functools import lru_cache
from pathlib import Path

from PIL import Image, ImageOps
from pypdf import PdfReader

from apps.api.src.vision.receipt_scan import read_codes, remove_black_letterbox

_pdf_renderer = threading.Lock()
_ocr_workers = threading.BoundedSemaphore(2)


def prepare_image(image: Image.Image) -> Image.Image:
    image = remove_black_letterbox(ImageOps.exif_transpose(image).convert('RGB'))
    image.thumbnail((4500, 4500))
    return image


def printed_codes(image: Image.Image) -> list[str]:
    try:
        import zxingcpp
        return [code[:2048] for code in read_codes(image, zxingcpp)[:16]]
    except (ImportError, OSError, RuntimeError):
        return []


def render_pdf_page(path: Path, index: int) -> Image.Image:
    try:
        import pypdfium2 as pdfium
    except (OSError, ImportError) as exc:
        raise ValueError('El renderizador de PDF no está disponible.') from exc
    # PDFium requires serialized calls even when the documents are different.
    with _pdf_renderer, pdfium.PdfDocument(path) as pdf:
        page = pdf[index]
        try:
            width, height = page.get_size()
            if min(width, height) <= 0:
                raise ValueError('La página del PDF tiene dimensiones inválidas.')
            bitmap = page.render(scale=min(3.0, 3000 / max(width, height)))
            try:
                return prepare_image(bitmap.to_pil())
            finally:
                bitmap.close()
        finally:
            page.close()


@lru_cache(maxsize=1)
def ocr_languages() -> str:
    try:
        result = subprocess.run(['tesseract', '--list-langs'], capture_output=True, text=True,
                                timeout=8, check=True)
    except (OSError, subprocess.SubprocessError):
        return ''
    return '+'.join(lang for lang in ('spa', 'eng') if lang in result.stdout.splitlines())


def ocr_text(image: Image.Image) -> str:
    languages = ocr_languages()
    if not languages:
        raise ValueError('Instala Tesseract con español o inglés, o inicia la app en Docker.')
    gray = ImageOps.autocontrast(ImageOps.grayscale(image))
    gray.thumbnail((3000, 3000))
    with io.BytesIO() as stream:
        gray.save(stream, 'PNG')
        data = stream.getvalue()
    try:
        with _ocr_workers:
            result = subprocess.run(['tesseract', 'stdin', 'stdout', '-l', languages, '--psm', '6'],
                                    input=data, capture_output=True, timeout=25, check=True)
    except (OSError, subprocess.SubprocessError) as exc:
        raise ValueError('El OCR local no pudo leer esta página. Prueba con otra orientación o foto.') from exc
    return result.stdout.decode('utf-8', errors='replace').strip()[:24_000]


def read_local_page(path: Path, number: int = 1, rotation: int = 0, force_ocr: bool = False) -> dict:
    pages = 1
    text = ''
    if path.suffix.lower() == '.txt':
        if number != 1:
            raise ValueError('El texto del correo tiene una sola página.')
        return {'page': 1, 'pages': 1, 'method': 'email', 'text': path.read_text(encoding='utf-8')[:40_000], 'codes': []}
    if path.suffix.lower() == '.pdf':
        reader = PdfReader(path)
        pages = len(reader.pages)
        if not 1 <= number <= pages:
            raise ValueError('La página seleccionada está fuera del documento.')
        text = (reader.pages[number - 1].extract_text() or '').strip()[:24_000]
        try:
            image = render_pdf_page(path, number - 1)
        except (ValueError, RuntimeError, OSError):
            if not text:
                raise ValueError('El PDF requiere una imagen de la página. Sube una foto o captura.')
            return {'page': number, 'pages': pages, 'method': 'pdf', 'text': text, 'codes': [],
                    'notice': 'Se conservó el texto digital. La imagen y sus códigos requieren otra copia.'}
    else:
        if number != 1:
            raise ValueError('La imagen tiene una sola página.')
        with Image.open(path) as original:
            image = prepare_image(original)
    if rotation:
        image = image.rotate(-rotation, expand=True)
    method = 'pdf'
    notice = ''
    codes = printed_codes(image)
    if force_ocr or not text:
        try:
            text = ocr_text(image)
            method = 'ocr'
        except ValueError as exc:
            if not codes and not text:
                raise
            method = 'pdf' if text else 'codes'
            notice = str(exc)
    return {'page': number, 'pages': pages, 'method': method, 'text': text, 'codes': codes, 'notice': notice}
