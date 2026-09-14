import asyncio
import logging
import re
from typing import List, Optional
from urllib.parse import unquote, urlparse
import httpx
from playwright.async_api import Page

from ..models import Merchant, Ticket
from ..vision.url_sanitizer import sanitize_and_classify_billing_url

logger = logging.getLogger(__name__)

# Dominios que son blogs de tutoriales, agregadores o redes sociales (NUNCA portales oficiales)
EXCLUDED_DOMAINS = {
    "facturaticket.mx",
    "facturacion-ticket.com.mx",
    "ticketfactura.com",
    "facturamos.com.mx",
    "facturama.mx",
    "sat.gob.mx",
    "verificacfdi.facturaelectronica.sat.gob.mx",
    "youtube.com",
    "facebook.com",
    "x.com",
    "twitter.com",
    "instagram.com",
    "wikipedia.org",
    "tiktok.com",
    "linkedin.com",
    "pinterest.com",
    "google.com",
    "bing.com",
    "duckduckgo.com",
}


def clean_search_term(text: Optional[str]) -> str:
    """Limpia nombres de comercio eliminando sufijos legales para mejorar la búsqueda."""
    if not text:
        return ""
    cleaned = re.sub(
        r"(?i)\b(S\.?A\.?\s*DE\s*C\.?V\.?|S\.?A\.?P\.?I\.?\s*DE\s*C\.?V\.?|S\.?\s*DE\s*R\.?L\.?\s*DE\s*C\.?V\.?|S\.?A\.?|S\.?C\.?|I\.?A\.?P\.?|A\.?C\.?|RESTAURANTES?|GRUPO|OPERADORA)\b\.?",
        "",
        text,
    )
    cleaned = re.sub(r"[.,;:/\\]+", " ", cleaned)
    return re.sub(r"\s+", " ", cleaned).strip()


async def search_candidate_portal_urls(
    comercio: Optional[str] = None,
    rfc_emisor: Optional[str] = None,
    sucursal: Optional[str] = None,
    current_url: Optional[str] = None,
) -> List[str]:
    """
    Busca en internet candidatos de URLs de portales de facturación para el comercio/RFC.
    """
    candidates: List[str] = []
    seen = set()

    def add_cand(u: Optional[str]):
        if not u:
            return
        sanitized = sanitize_and_classify_billing_url(u)
        if not sanitized:
            return
        try:
            parsed = urlparse(sanitized)
            domain = (parsed.netloc or "").split(":")[0].lower()
            if any(exc in domain for exc in EXCLUDED_DOMAINS):
                return
            if sanitized not in seen:
                candidates.append(sanitized)
                seen.add(sanitized)
        except Exception:
            pass

    # Mapeos directos de cadenas mexicanas conocidas (Alsea, PRB, GRG, Cinemex, OXXO, Walmart, etc.)
    term_lower = (clean_search_term(comercio) or clean_search_term(sucursal) or "").lower()
    url_lower = (current_url or "").lower()
    rfc_clean = (rfc_emisor or "").strip().upper()

    # Alsea (Domino's, Starbucks, Burger King, Vips, Italianni's, Chili's)
    if any(k in term_lower or k in url_lower for k in ("domino", "starbucks", "alsea", "vips", "burger king", "italianni", "chili")) or rfc_clean in ("OFA9210138U1", "CGI930623RH5", "OVI961128795", "BKM911204853"):
        add_cand("https://facturacion.alsea.com.mx")
        add_cand("https://alsea.interfactura.com")

    # PRB (KFC, Pizza Hut)
    if any(k in term_lower or k in url_lower for k in ("kfc", "kentucky", "pizza hut", "prb")) or rfc_clean == "PRB100802H27":
        add_cand("https://facturacion.prb.com.mx")
        add_cand("https://kfc.com.mx/facturacion")

    # GRG (Toks, Panda Express, Beer Factory)
    if any(k in term_lower or k in url_lower for k in ("toks", "panda express", "beer factory", "shake shack", "farolito")) or rfc_clean in ("RTO840921RE4", "PEH101019688"):
        add_cand("https://facturacion.toks.com.mx")

    # Cinemex
    if "cinemex" in term_lower or "cinemex" in url_lower or rfc_clean == "OCI9312061T0":
        add_cand("https://facturacion.cinemex.com")

    # Cinépolis
    if "cinepolis" in term_lower or "cinépolis" in term_lower or "cinepolis" in url_lower or rfc_clean == "EMC8407269J7":
        add_cand("https://cinepolis.com/facturacion-electronica")

    # OXXO
    if "oxxo" in term_lower or "oxxo" in url_lower or rfc_clean == "CCO8605231N4":
        add_cand("https://www.oxxo.com/facturacion-electronica")

    # Walmart México
    if any(k in term_lower or k in url_lower for k in ("walmart", "aurrera", "sams", "superama")) or rfc_clean == "NWM9709244W4":
        add_cand("https://facturacion.walmartmexico.com.mx")

    # Soriana
    if "soriana" in term_lower or "soriana" in url_lower or rfc_clean == "TSO991022PB6":
        add_cand("https://facturacion.soriana.com")

    # Chedraui
    if "chedraui" in term_lower or "chedraui" in url_lower or rfc_clean == "TCH850701RM1":
        add_cand("https://facturacion.chedraui.com.mx")

    # Costco México
    if "costco" in term_lower or "costco" in url_lower or rfc_clean == "CCM891107490":
        add_cand("https://facturacion.costco.com.mx")

    # Farmacias Guadalajara
    if "guadalajara" in term_lower or rfc_clean == "FGU830930PD3":
        add_cand("https://facturacion.farmaciasguadalajara.com")

    # Farmacias del Ahorro
    if any(k in term_lower for k in ("ahorro", "fahorro")) or rfc_clean == "FAR971201991":
        add_cand("https://facturacion.fahorro.com.mx")

    # Sanborns
    if "sanborns" in term_lower or "sanborns" in url_lower or rfc_clean == "SHE190630V37":
        add_cand("https://facturacion.sanborns.com.mx")

    # Sears México
    if "sears" in term_lower or "sears" in url_lower or rfc_clean == "SEA630403K46":
        add_cand("https://facturacion.sears.com.mx")

    # Liverpool / Suburbia
    if any(k in term_lower for k in ("liverpool", "suburbia")) or rfc_clean == "DLI931201MI9":
        add_cand("https://facturacion.liverpool.com.mx")

    # OXXO Gas
    if "oxxo gas" in term_lower or "oxxogas" in term_lower or rfc_clean == "CGO0107056S4":
        add_cand("https://facturacion.oxxogas.com")

    # Hidrosina
    if "hidrosina" in term_lower or rfc_clean == "GHI921008RN9":
        add_cand("https://facturacion.hidrosina.com.mx")

    # Little Caesars
    if "little caesars" in term_lower or "caesars" in term_lower or "caesar" in term_lower:
        add_cand("https://facturacion.littlecaesars.com.mx")
        add_cand("https://facturacionpremier.mx")

    # Home Depot
    if "home depot" in term_lower or "homedepot" in url_lower or rfc_clean == "THD000913BC4":
        add_cand("https://facturacion.homedepot.com.mx")

    # Office Depot / RadioShack
    if any(k in term_lower for k in ("office depot", "officedepot", "radioshack")) or rfc_clean == "ODG9501314T2":
        add_cand("https://facturacion.officedepot.com.mx")

    # Smart Fit
    if "smart fit" in term_lower or "smartfit" in url_lower:
        add_cand("https://facturacion.smartfit.com.mx")

    # 1. Variantes directas del dominio si se tiene una URL base
    if current_url:
        try:
            parsed = urlparse(current_url)
            host = parsed.netloc.split(":")[0]
            parts = host.split(".")
            base_domain = ".".join(parts[-2:]) if len(parts) >= 2 else host
            if base_domain and not any(exc in base_domain for exc in EXCLUDED_DOMAINS):
                add_cand(f"https://efactura.{base_domain}/facturacion")
                add_cand(f"https://facturacion.{base_domain}/")
                add_cand(f"https://www.{base_domain}/facturacion")
                add_cand(f"https://factura.{base_domain}/")
        except Exception:
            pass

    # 2. Búsqueda web HTTP mediante DuckDuckGo Lite
    term = clean_search_term(comercio) or clean_search_term(sucursal)
    rfc = (rfc_emisor or "").strip()
    query = f"facturacion {term} {rfc} portal mexico".strip()

    if term or rfc:
        try:
            headers = {
                "User-Agent": (
                    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
                    "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/128.0.0.0 Safari/537.36"
                )
            }
            async with httpx.AsyncClient(timeout=8.0, follow_redirects=True) as client:
                resp = await client.get(
                    "https://lite.duckduckgo.com/lite/",
                    params={"q": query},
                    headers=headers,
                )
                if resp.status_code == 200:
                    raw_links = re.findall(
                        r'<a[^>]+class=[\'"]result-link[\'"][^>]+href=[\'"]([^\'"]+)[\'"]',
                        resp.text,
                    )
                    if not raw_links:
                        raw_links = re.findall(r'href=[\'"]([^\'"]*uddg=[^\'"]*)[\'"]', resp.text)

                    for l in raw_links:
                        m = re.search(r"uddg=([^&]+)", l)
                        url_cand = unquote(m.group(1)) if m else l
                        if url_cand.startswith("http") and "duckduckgo" not in url_cand:
                            add_cand(url_cand)
        except Exception as exc:
            logger.debug("Búsqueda web en DuckDuckGo no disponible: %s", exc)

    return candidates[:6]


async def verify_portal_matches_ticket(page: Page, ticket: Ticket) -> bool:
    """
    Verifica estrictamente que la página cargada corresponda al comercio del ticket
    y contenga una interfaz de facturación legítima, para evitar falsos positivos
    o portales incorrectos.
    """
    try:
        page_title = (await page.title() or "").lower()
        page_url = (page.url or "").lower()
        body_text = (await page.inner_text("body") or "")[:4000].lower()

        # 1. Comprobar que no sea un blog de tutoriales
        if any(blog in page_url for blog in ("facturaticket", "facturacion-ticket", "ticketfactura")):
            logger.warning("Portal rechazado por ser un blog de tutoriales: %s", page_url)
            return False

        if any(txt in body_text for txt in ("paso 1 para facturar", "instrucciones para facturar", "cómo facturar tu ticket")):
            inputs_count = await page.locator("input").count()
            if inputs_count == 0:
                logger.warning("Portal rechazado: es un artículo informativo sin formulario de facturación.")
                return False

        # 2. Comprobar que tenga campos de facturación
        inputs_count = await page.locator("input:visible").count()
        buttons_count = await page.locator("button:visible, a.btn:visible").count()
        if inputs_count == 0 and buttons_count == 0:
            return False

        # 3. Identificadores del comercio en el ticket
        comercio = clean_search_term(ticket.extracted.get("comercio") if ticket.extracted else None) or ""
        sucursal = clean_search_term(ticket.sucursal) or ""
        rfc = (ticket.rfc_emisor or "").lower().strip()

        # Extraer palabras clave del comercio (ej. 'toks', 'kfc', 'starbucks', 'vips', 'oxxo')
        keywords = set()
        for src in (comercio, sucursal):
            for word in re.split(r"[^a-zA-Z0-9]+", src.lower()):
                if len(word) >= 3 and word not in ("sucursal", "tienda", "restaurante", "mexico", "san", "del", "rio"):
                    keywords.add(word)

        # Si el RFC emisor aparece en la página o en la URL -> Coincidencia exacta
        if rfc and len(rfc) >= 9 and (rfc in body_text or rfc in page_url):
            logger.info("Portal verificado por coincidencia exacta de RFC emisor: %s", rfc)
            return True

        # Si alguna palabra clave de la marca aparece en el título, URL o contenido
        for kw in keywords:
            if kw in page_url or kw in page_title or kw in body_text:
                logger.info("Portal verificado por coincidencia de marca '%s' en %s", kw, page_url)
                return True

        # Si el ticket no tenía nombre ni RFC registrado, no se puede descartar por marca
        if not keywords and not rfc:
            return True

        logger.warning(
            "La página en %s no parece coincidir con el comercio del ticket (keywords=%s, rfc=%s).",
            page_url,
            keywords,
            rfc,
        )
        return False

    except Exception as exc:
        logger.warning("Error verificando coincidencia de portal con ticket: %s", exc)
        return False
