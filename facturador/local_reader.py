"""Lectura local de una página de ticket desde la imagen o PDF original."""

import io
import subprocess
import threading
import time
from collections import OrderedDict
from functools import lru_cache
from pathlib import Path

from PIL import Image, ImageOps
from pypdf import PdfReader

from apps.api.src.vision.receipt_scan import read_codes, remove_black_letterbox

_pdf_renderer = threading.Lock()
_ocr_workers = threading.BoundedSemaphore(3)


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


# Originals are stored under their sha256, so (file, size, mtime) identifies a
# page reading. Each ticket read reuses it instead of running Tesseract again.
PAGE_CACHE_SIZE = 32
FAILED_READ_SECONDS = 60  # A failed OCR is retried after this, not on every caller.
_page_cache: OrderedDict = OrderedDict()
_page_cache_lock = threading.Lock()


def _page_key(path: Path, number: int, rotation: int, force_ocr: bool) -> tuple:
    resolved = path.resolve()
    stat = resolved.stat()
    return str(resolved), stat.st_size, stat.st_mtime_ns, number, rotation, bool(force_ocr)


def _cached(key):
    with _page_cache_lock:
        entry = _page_cache.get(key)
        if entry is None:
            return None
        expires, value = entry
        if expires and expires <= time.monotonic():
            del _page_cache[key]
            return None
        _page_cache.move_to_end(key)
        return value


def _remember(key, value) -> None:
    expires = time.monotonic() + FAILED_READ_SECONDS if isinstance(value, Exception) else 0
    with _page_cache_lock:
        _page_cache[key] = (expires, value)
        _page_cache.move_to_end(key)
        while len(_page_cache) > PAGE_CACHE_SIZE:
            _page_cache.popitem(last=False)


def clear_page_cache() -> None:
    with _page_cache_lock:
        _page_cache.clear()


def _copy(page: dict) -> dict:
    return {**page, 'codes': list(page.get('codes', []))}


def _scan(path: Path, number: int, rotation: int, force_ocr: bool):
    """Read one page. Returns (page or the ValueError that explains it, upright image or None)."""
    pages = 1
    text = ''
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
                return ValueError('El PDF requiere una imagen de la página. Sube una foto o captura.'), None
            return {'page': number, 'pages': pages, 'method': 'pdf', 'text': text, 'codes': [],
                    'notice': 'Se conservó el texto digital. La imagen y sus códigos requieren otra copia.'}, None
    else:
        if number != 1:
            raise ValueError('La imagen tiene una sola página.')
        with Image.open(path) as original:
            image = prepare_image(original)
    upright = image
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
                return exc, upright
            method = 'pdf' if text else 'codes'
            notice = str(exc)
    return {'page': number, 'pages': pages, 'method': method, 'text': text, 'codes': codes,
            'notice': notice}, upright


def _page_image(path: Path, number: int):
    """The prepared page image alone, for a page whose reading is already known."""
    if path.suffix.lower() != '.pdf':
        with Image.open(path) as original:
            return prepare_image(original)
    try:
        return render_pdf_page(path, number - 1)
    except (ValueError, RuntimeError, OSError):
        return None


def _read(path: Path, number: int, rotation: int, force_ocr: bool, with_image: bool):
    key = _page_key(path, number, rotation, force_ocr)
    found = _cached(key)
    if found is None:
        found, image = _scan(path, number, rotation, force_ocr)
        _remember(key, found)
    else:
        image = _page_image(path, number) if with_image else None
    return found, image


def read_local_page(path: Path, number: int = 1, rotation: int = 0, force_ocr: bool = False) -> dict:
    path = Path(path)
    if path.suffix.lower() == '.txt':
        if number != 1:
            raise ValueError('El texto del correo tiene una sola página.')
        return {'page': 1, 'pages': 1, 'method': 'email', 'text': path.read_text(encoding='utf-8')[:40_000], 'codes': []}
    found, _ = _read(path, number, rotation, force_ocr, False)
    if isinstance(found, Exception):
        raise ValueError(str(found))
    return _copy(found)


def page_with_image(path: Path, number: int = 1):
    """Upright page image plus its cached local reading, for the model's input.

    Returns (page, image): page is None when neither text nor codes could be read,
    and image is None when only a PDF's digital text is available. Raises
    ValueError when the page has neither an image nor text."""
    path = Path(path)
    found, image = _read(path, number, 0, False, True)
    if isinstance(found, Exception):
        if image is None:
            raise ValueError(str(found))
        return None, image
    return _copy(found), image
