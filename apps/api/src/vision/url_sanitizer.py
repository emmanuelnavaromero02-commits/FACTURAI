import re
from typing import Optional
from urllib.parse import urlparse, urlunparse


def sanitize_and_classify_billing_url(raw_url: Optional[str]) -> Optional[str]:
    """
    Limpia, normaliza y clasifica una URL de facturación encontrada en QR o texto.
    
    Reglas del negocio:
    1. Si apunta a verificacfdi.facturaelectronica.sat.gob.mx o cualquier host
       de sat.gob.mx, NO es un portal de facturación (es verificación de CFDI existente).
       Se descarta retornando None para que se busque la URL impresa.
    2. Normaliza: elimina puntuación al final (puntos, comas, barras), corrige mayúsculas en el host,
       y antepone 'https://' si carece de esquema.
    3. Si la URL es inválida o vacía, retorna None.
    """
    if not raw_url:
        return None

    cleaned = raw_url.strip()
    # Eliminar espacios internos si los hubiera y comillas
    cleaned = cleaned.strip("\"'<>`")

    # Eliminar puntuación común de fin de oración impresa en tickets
    cleaned = re.sub(r"[.,;:/\\]+$", "", cleaned).strip()

    if not cleaned:
        return None

    # Si no tiene esquema, anteponer https://
    if not re.match(r"^[a-zA-Z][a-zA-Z0-9+.-]*://", cleaned):
        cleaned = f"https://{cleaned}"

    try:
        parsed = urlparse(cleaned)
    except Exception:
        return None

    netloc = (parsed.netloc or "").lower().strip()
    if not netloc:
        return None

    # 1. Descartar CUALQUIER host que termine en sat.gob.mx, sin importar subdominio
    host = netloc.split(":")[0].strip()
    if host == "sat.gob.mx" or host.endswith(".sat.gob.mx") or "verificacfdi" in host:
        return None

    # 2. Mapeo de portales conocidos donde el ticket imprime el sitio web general en vez del portal de facturación
    if host in (
        "alsea.com.mx", "www.alsea.com.mx", "facturacion.alsea.com.mx",
        "dominos.com.mx", "www.dominos.com.mx",
        "starbucks.com.mx", "www.starbucks.com.mx",
        "burgerking.com.mx", "www.burgerking.com.mx",
        "vips.com.mx", "www.vips.com.mx",
        "italiannis.com.mx", "www.italiannis.com.mx",
        "chilis.com.mx", "www.chilis.com.mx",
    ):
        return "https://facturacion.alsea.com.mx"

    if host in ("kfc.com.mx", "www.kfc.com.mx", "facturacion.prb.com.mx"):
        return "https://facturacion.prb.com.mx:444/index.jsp"
    if host in ("toks.com.mx", "www.toks.com.mx"):
        return "https://efactura.toks.com.mx/facturacion"
    if host in ("oxxo.com", "www.oxxo.com"):
        return "https://www.oxxo.com/facturacion-electronica"
    if host in ("cinepolis.com", "www.cinepolis.com"):
        return "https://cinepolis.com/facturacion-electronica"
    if host in ("cinemex.com", "www.cinemex.com"):
        return "https://facturacion.cinemex.com"

    # Reconstruir URL con netloc normalizado en minúsculas y esquema https por defecto (o http)
    scheme = parsed.scheme.lower()
    if scheme not in ("http", "https"):
        scheme = "https"

    normalized = urlunparse((
        scheme,
        netloc,
        parsed.path,
        parsed.params,
        parsed.query,
        parsed.fragment,
    ))

    return normalized
