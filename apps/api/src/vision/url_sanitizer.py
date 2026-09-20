import asyncio
import ipaddress
import re
import socket
from typing import Iterable, List, Optional
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
        "alsea.com.mx", "www.alsea.com.mx", "facturacion.alsea.com.mx", "alsea.interfactura.com",
        "dominos.com.mx", "www.dominos.com.mx",
        "starbucks.com.mx", "www.starbucks.com.mx",
        "burgerking.com.mx", "www.burgerking.com.mx",
        "vips.com.mx", "www.vips.com.mx",
        "italiannis.com.mx", "www.italiannis.com.mx",
        "chilis.com.mx", "www.chilis.com.mx",
    ):
        return "https://alsea.interfactura.com"

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


# Pistas en el dominio o la ruta que indican un portal de facturación y no publicidad
BILLING_URL_HINTS = (
    "factur",
    "invoice",
    "billing",
    "cfdi",
    "timbr",
    "autofact",
    "efactura",
    "e-factura",
    "comprobante",
)


def looks_like_billing_url(url: Optional[str]) -> bool:
    """True si la URL parece llevar a un portal de facturación."""
    if not url:
        return False
    low = url.lower()
    return any(hint in low for hint in BILLING_URL_HINTS)


def choose_billing_url(qr_url: Optional[str], printed_url: Optional[str]) -> Optional[str]:
    """
    Elige la URL de facturación entre la del código QR y la impresa en el ticket.
    El QR gana si parece de facturación, porque se lee sin errores de OCR.
    Si el QR no lo parece (p. ej. el sitio del restaurante) y la impresa sí, gana la impresa.
    """
    if qr_url and looks_like_billing_url(qr_url):
        return qr_url
    if printed_url and looks_like_billing_url(printed_url):
        return printed_url
    return qr_url or printed_url


# Errores de DNS que significan "este dominio no existe" (no fallas temporales de red)
_DNS_NO_EXISTE = {
    code
    for code in (getattr(socket, "EAI_NONAME", None), getattr(socket, "EAI_NODATA", None))
    if code is not None
}


async def _getaddrinfo(host: str):
    return await asyncio.get_running_loop().getaddrinfo(host, None)


async def host_resolves(url: Optional[str], timeout: float = 3.0) -> bool:
    """
    True si el dominio de la URL existe en DNS.
    Solo devuelve False cuando el DNS confirma que no existe; ante un timeout o una falla
    temporal de red devuelve True para no descartar un portal bueno por un problema pasajero.
    """
    if not url:
        return False
    try:
        host = urlparse(url if "://" in url else f"https://{url}").hostname
    except ValueError:
        return False
    if not host:
        return False
    try:
        ipaddress.ip_address(host)
        return True
    except ValueError:
        pass
    try:
        await asyncio.wait_for(_getaddrinfo(host), timeout)
        return True
    except socket.gaierror as exc:
        return exc.errno not in _DNS_NO_EXISTE
    except (asyncio.TimeoutError, OSError):
        return True
    except UnicodeError:
        return False


async def filter_resolvable_urls(urls: Iterable[str]) -> List[str]:
    """Conserva, en orden, solo las URLs cuyo dominio existe."""
    lista = [u for u in urls if u]
    resultados = await asyncio.gather(*(host_resolves(u) for u in lista))
    return [u for u, ok in zip(lista, resultados) if ok]


async def choose_start_url(*candidates: Optional[str]) -> Optional[str]:
    """
    Elige la URL con la que arranca el motor: la primera cuyo dominio existe.
    Si ninguna existe, devuelve la primera no vacía para que el motor busque el portal en internet.
    """
    opciones = [c for c in candidates if c]
    for c in opciones:
        if await host_resolves(c):
            return c
    return opciones[0] if opciones else None
