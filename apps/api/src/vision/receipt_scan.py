"""Preparación de comprobantes compartida por las dos aplicaciones."""

import logging

from PIL import Image

logger = logging.getLogger(__name__)
QR_UPSCALE_MAX_EDGE = 2000


def read_codes(image: Image.Image, decoder) -> list[str]:
    gray = image.convert("L")
    texts: dict[str, None] = {}

    def collect(frame):
        for code in decoder.read_barcodes(frame):
            if code.valid and code.text and code.text.strip():
                texts[code.text.strip()] = None

    collect(gray)
    if max(gray.size) <= QR_UPSCALE_MAX_EDGE:
        with gray.resize((gray.width * 2, gray.height * 2), Image.Resampling.LANCZOS) as large:
            collect(large)
    return list(texts)


def remove_black_letterbox(img: Image.Image, threshold: int = 20) -> Image.Image:
    """
    Elimina franjas negras externas (letterbox) producidas por capturas de pantalla de celular
    o aplicaciones de cámara, recortando automáticamente al área útil del comprobante.
    """
    try:
        gray = img.convert("L")
        mask = gray.point(lambda p: 255 if p > threshold else 0)
        bbox = mask.getbbox()
        if bbox:
            w, h = img.size
            bw = bbox[2] - bbox[0]
            bh = bbox[3] - bbox[1]
            if (bw * bh) >= 0.35 * (w * h) and (bw < w or bh < h):
                return img.crop(bbox)
    except Exception as exc:
        logger.debug("No se pudo aplicar recorte de franjas negras: %s", exc)
    return img
