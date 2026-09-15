import asyncio
import logging
import re
from typing import Any, Dict, List, Optional
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


# Catálogo extendido de cadenas comerciales, franquicias, gasolineras, farmacias y casetas en México
KNOWN_CHAINS_MAP = [
    # Alsea (Domino's, Starbucks, Burger King, Vips, Italianni's, Chili's, P.F. Chang's, Cheesecake Factory, El Portón)
    {
        "keywords": ["domino", "starbucks", "alsea", "vips", "burger king", "italianni", "chili", "cheesecake factory", "pf chang", "el porton", "porton"],
        "rfcs": ["OFA9210138U1", "CGI930623RH5", "OVI961128795", "BKM911204853", "DIA0403167D8", "FCH940523C62", "ESI9405237G4"],
        "urls": ["https://alsea.interfactura.com"],
    },
    # PRB (KFC, Pizza Hut)
    {
        "keywords": ["kfc", "kentucky", "pizza hut", "prb"],
        "rfcs": ["PRB100802H27"],
        "urls": ["https://facturacion.prb.com.mx", "https://kfc.com.mx/facturacion"],
    },
    # CMR (Wings, Olive Garden, Red Lobster, Sushi Itto, La Destilería, El Lago)
    {
        "keywords": ["wings", "olive garden", "red lobster", "sushi itto", "destileria", "cmr"],
        "rfcs": ["CMR0206149Z0", "OCM980402HN5"],
        "urls": ["https://facturacion.cmr.mx"],
    },
    # GRG (Toks, Panda Express, Beer Factory, Shake Shack, El Farolito)
    {
        "keywords": ["toks", "panda express", "beer factory", "shake shack", "farolito"],
        "rfcs": ["RTO840921RE4", "PEH101019688"],
        "urls": ["https://facturacion.toks.com.mx"],
    },
    # Cinemex
    {
        "keywords": ["cinemex"],
        "rfcs": ["OCI9312061T0"],
        "urls": ["https://facturacion.cinemex.com"],
    },
    # Cinépolis
    {
        "keywords": ["cinepolis", "cinépolis"],
        "rfcs": ["EMC8407269J7"],
        "urls": ["https://cinepolis.com/facturacion-electronica"],
    },
    # OXXO
    {
        "keywords": ["oxxo", "cadena comercial oxxo"],
        "rfcs": ["CCO8605231N4"],
        "urls": ["https://www.oxxo.com/facturacion-electronica"],
    },
    # 7-Eleven
    {
        "keywords": ["7-eleven", "7 eleven", "seven eleven"],
        "rfcs": ["SNE001026K59"],
        "urls": ["https://facturacion.7-eleven.com.mx"],
    },
    # Circle K / Tiendas Extra
    {
        "keywords": ["circle k", "circlek", "tiendas extra", "extra"],
        "rfcs": ["CKM110729QD1", "TEX9302097TA"],
        "urls": ["https://facturacion.circlek.com.mx"],
    },
    # Walmart México / Aurrera / Sam's Club
    {
        "keywords": ["walmart", "aurrera", "sams", "superama", "nueva wal mart"],
        "rfcs": ["NWM9709244W4"],
        "urls": ["https://facturacion.walmartmexico.com.mx"],
    },
    # Soriana / City Club
    {
        "keywords": ["soriana", "city club", "tiendas soriana"],
        "rfcs": ["TSO991022PB6"],
        "urls": ["https://facturacion.soriana.com"],
    },
    # Chedraui
    {
        "keywords": ["chedraui", "tiendas chedraui"],
        "rfcs": ["TCH850701RM1"],
        "urls": ["https://facturacion.chedraui.com.mx"],
    },
    # La Comer / Fresko / City Market
    {
        "keywords": ["la comer", "lacomer", "fresko", "city market", "sumesa"],
        "rfcs": ["LCM1510207K0"],
        "urls": ["https://facturacion.lacomer.com.mx"],
    },
    # HEB
    {
        "keywords": ["heb", "h-e-b"],
        "rfcs": ["HEB9611295A2"],
        "urls": ["https://facturacion.heb.com.mx"],
    },
    # Costco México
    {
        "keywords": ["costco", "costco wholesale"],
        "rfcs": ["CCM891107490", "CME910715UB9"],
        "urls": ["https://facturacion.costco.com.mx"],
    },
    # Farmacias Guadalajara
    {
        "keywords": ["farmacias guadalajara", "guadalajara"],
        "rfcs": ["FGU830930PD3"],
        "urls": ["https://facturacion.farmaciasguadalajara.com"],
    },
    # Farmacias del Ahorro
    {
        "keywords": ["farmacias del ahorro", "fahorro", "ahorro"],
        "rfcs": ["FAR971201991"],
        "urls": ["https://facturacion.fahorro.com.mx"],
    },
    # Farmacias San Pablo
    {
        "keywords": ["san pablo", "farmacia san pablo"],
        "rfcs": ["FSP0905187V7"],
        "urls": ["https://facturacion.fsanpablo.com"],
    },
    # Farmacias Benavides
    {
        "keywords": ["benavides", "farmacias benavides"],
        "rfcs": ["FBE9110215Z3"],
        "urls": ["https://facturacion.benavides.com.mx"],
    },
    # Farmacias Similares
    {
        "keywords": ["farmacias similares", "similares"],
        "rfcs": ["FSI9711185G9"],
        "urls": ["https://facturacion.farmaciasimilares.com"],
    },
    # Sanborns
    {
        "keywords": ["sanborns", "sanborn"],
        "rfcs": ["SHE190630V37"],
        "urls": ["https://facturacion.sanborns.com.mx"],
    },
    # Sears México
    {
        "keywords": ["sears"],
        "rfcs": ["SEA630403K46"],
        "urls": ["https://facturacion.sears.com.mx"],
    },
    # Liverpool / Suburbia
    {
        "keywords": ["liverpool", "suburbia"],
        "rfcs": ["DLI931201MI9"],
        "urls": ["https://facturacion.liverpool.com.mx"],
    },
    # Coppel
    {
        "keywords": ["coppel"],
        "rfcs": ["COP920428Q20"],
        "urls": ["https://facturacion.coppel.com"],
    },
    # Elektra
    {
        "keywords": ["elektra"],
        "rfcs": ["ELE8706234L0"],
        "urls": ["https://facturacion.elektra.mx"],
    },
    # Gasolineras: OXXO Gas
    {
        "keywords": ["oxxo gas", "oxxogas"],
        "rfcs": ["CGO0107056S4"],
        "urls": ["https://facturacion.oxxogas.com"],
    },
    # Hidrosina
    {
        "keywords": ["hidrosina"],
        "rfcs": ["GHI921008RN9"],
        "urls": ["https://facturacion.hidrosina.com.mx"],
    },
    # Pemex / Franquicia Pemex
    {
        "keywords": ["pemex", "gasolinera pemex", "petroleos mexicanos"],
        "rfcs": ["PME380607P35"],
        "urls": ["https://facturacion.pemex.com"],
    },
    # G500
    {
        "keywords": ["g500", "g-500", "gasolinera g500"],
        "rfcs": ["GNE140618767"],
        "urls": ["https://facturacion.g500network.com"],
    },
    # Petro-7 / Petro 7
    {
        "keywords": ["petro-7", "petro 7", "petroseven"],
        "rfcs": ["PVE0010268F1"],
        "urls": ["https://facturacion.petro-7.com.mx"],
    },
    # BP Gas
    {
        "keywords": ["bp", "bp gas", "british petroleum"],
        "rfcs": ["BPM170209A32"],
        "urls": ["https://bpgas.com.mx/facturacion"],
    },
    # Mobil
    {
        "keywords": ["mobil", "exxonmobil", "gasolinera mobil"],
        "rfcs": [],
        "urls": ["https://mobilmexico.com.mx/facturacion"],
    },
    # Shell
    {
        "keywords": ["shell", "gasolinera shell"],
        "rfcs": [],
        "urls": ["https://facturacion.shellmexico.com.mx"],
    },
    # TotalEnergies
    {
        "keywords": ["totalenergies", "gasolinera total"],
        "rfcs": [],
        "urls": ["https://facturacion.totalenergies.mx"],
    },
    # Rendichicas
    {
        "keywords": ["rendichicas", "gasolinera rendichicas"],
        "rfcs": [],
        "urls": ["https://facturacion.rendichicas.com"],
    },
    # Gulf
    {
        "keywords": ["gulf", "gasolinera gulf"],
        "rfcs": [],
        "urls": ["https://facturacion.gulf.mx"],
    },
    # Little Caesars
    {
        "keywords": ["little caesars", "caesars", "caesar"],
        "rfcs": [],
        "urls": ["https://facturacion.littlecaesars.com.mx", "https://facturacionpremier.mx"],
    },
    # McDonald's
    {
        "keywords": ["mcdonald", "mcdonalds", "arcos dorados"],
        "rfcs": ["SAD930504781"],
        "urls": ["https://facturacion.mcdonalds.com.mx"],
    },
    # Subway
    {
        "keywords": ["subway"],
        "rfcs": [],
        "urls": ["https://facturacion.subwaymexico.com.mx"],
    },
    # Carl's Jr
    {
        "keywords": ["carls jr", "carl's jr", "carls"],
        "rfcs": [],
        "urls": ["https://facturacion.carlsjr.com.mx"],
    },
    # La Casa de Toño
    {
        "keywords": ["casa de tono", "la casa de tono", "casa de toño"],
        "rfcs": [],
        "urls": ["https://facturacion.lacasadetono.mx"],
    },
    # Sonora Grill
    {
        "keywords": ["sonora grill", "sonora prime"],
        "rfcs": [],
        "urls": ["https://facturacion.sonoragrill.com.mx"],
    },
    # Potzollcalli
    {
        "keywords": ["potzollcalli"],
        "rfcs": [],
        "urls": ["https://facturacion.potzollcalli.com"],
    },
    # Bisquets Obregón
    {
        "keywords": ["bisquets obregon", "bisquets"],
        "rfcs": [],
        "urls": ["https://facturacion.bisquetsobregon.com.mx"],
    },
    # El Fogoncito
    {
        "keywords": ["fogoncito", "el fogoncito"],
        "rfcs": [],
        "urls": ["https://facturacion.fogoncito.com"],
    },
    # Krispy Kreme
    {
        "keywords": ["krispy kreme", "krispy"],
        "rfcs": [],
        "urls": ["https://facturacion.krispykreme.mx"],
    },
    # Dairy Queen
    {
        "keywords": ["dairy queen"],
        "rfcs": [],
        "urls": ["https://facturacion.dq.com.mx"],
    },
    # Tim Hortons
    {
        "keywords": ["tim hortons", "tim horton"],
        "rfcs": [],
        "urls": ["https://facturacion.timhortons.mx"],
    },
    # Cielito Querido Café
    {
        "keywords": ["cielito querido"],
        "rfcs": [],
        "urls": ["https://facturacion.cielitoquerido.com.mx"],
    },
    # Home Depot
    {
        "keywords": ["home depot", "homedepot"],
        "rfcs": ["THD000913BC4"],
        "urls": ["https://facturacion.homedepot.com.mx"],
    },
    # Office Depot / RadioShack
    {
        "keywords": ["office depot", "officedepot", "radioshack"],
        "rfcs": ["ODG9501314T2"],
        "urls": ["https://facturacion.officedepot.com.mx"],
    },
    # Smart Fit
    {
        "keywords": ["smart fit", "smartfit"],
        "rfcs": [],
        "urls": ["https://facturacion.smartfit.com.mx"],
    },
    # CAPUFE
    {
        "keywords": ["capufe", "caminos y puentes"],
        "rfcs": ["CPU6306297W5"],
        "urls": ["https://facturacioncapufe.com.mx"],
    },
    # TeleVía
    {
        "keywords": ["televia", "tele via"],
        "rfcs": [],
        "urls": ["https://facturacion.televia.com.mx"],
    },
    # PASE
    {
        "keywords": ["pase", "pase urbano"],
        "rfcs": [],
        "urls": ["https://facturacion.pase.com.mx"],
    },
    # Red Vía Corta / Autovías
    {
        "keywords": ["red via corta", "redviacorta", "autovias"],
        "rfcs": ["AOC9404287A1"],
        "urls": ["https://facturacion.redviacorta.mx"],
    },
    # IDEAL Autopistas
    {
        "keywords": ["ideal", "autopista"],
        "rfcs": [],
        "urls": ["https://facturacion.ideal.com.mx"],
    },
    # PINFRA
    {
        "keywords": ["pinfra"],
        "rfcs": [],
        "urls": ["https://facturacionpinfra.com.mx"],
    },
    # ADO
    {
        "keywords": ["ado", "autobuses de oriente"],
        "rfcs": [],
        "urls": ["https://facturacion.transpais.com.mx"],
    },
    # Primera Plus
    {
        "keywords": ["primera plus"],
        "rfcs": [],
        "urls": ["https://factura.primeraplus.com.mx"],
    },
    # ETN
    {
        "keywords": ["etn", "etn turistar"],
        "rfcs": [],
        "urls": ["https://facturacion.etn.com.mx"],
    },
    # Aeroméxico
    {
        "keywords": ["aeromexico", "aeroméxico"],
        "rfcs": ["AME880912I89"],
        "urls": ["https://facturacion.aeromexico.com"],
    },
    # Volaris
    {
        "keywords": ["volaris"],
        "rfcs": ["VAB041014389"],
        "urls": ["https://facturacion.volaris.com"],
    },
    # VivaAerobus
    {
        "keywords": ["vivaaerobus", "viva aerobus"],
        "rfcs": ["VIV0607211S3"],
        "urls": ["https://facturacion.vivaaerobus.com"],
    },
    # Uber
    {
        "keywords": ["uber", "uber trip"],
        "rfcs": ["UBV141209355"],
        "urls": ["https://riders.uber.com"],
    },
    # DiDi
    {
        "keywords": ["didi", "didi mobility"],
        "rfcs": ["DMM1804246H2"],
        "urls": ["https://mexico.didiglobal.com"],
    },
]


async def search_candidate_portal_urls(
    comercio: Optional[str] = None,
    rfc_emisor: Optional[str] = None,
    sucursal: Optional[str] = None,
    current_url: Optional[str] = None,
    include_synthetic: bool = False,
) -> List[str]:
    """
    Busca en internet candidatos de URLs de portales de facturación para el comercio/RFC.
    Combina:
    1. Catálogo exhaustivo de marcas y cadenas mexicanas.
    2. Coincidencia estricta por RFC emisor.
    3. Variantes de dominio institucional.
    4. Búsqueda web HTTP multi-consulta en DuckDuckGo.
    5. Heurísticas sintéticas de dominio (opcional).
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

    term_clean = clean_search_term(comercio) or clean_search_term(sucursal) or ""
    term_lower = term_clean.lower()
    url_lower = (current_url or "").lower()
    rfc_clean = (rfc_emisor or "").strip().upper()

    # 1. Búsqueda prioritaria en catálogo de cadenas mexicanas por RFC y marcas
    for entry in KNOWN_CHAINS_MAP:
        match_rfc = rfc_clean and any(r == rfc_clean for r in entry["rfcs"])
        match_kw = any(
            re.search(rf"\b{re.escape(k)}\b", term_lower) or k in url_lower
            for k in entry["keywords"]
        )
        if match_rfc or match_kw:
            for u in entry["urls"]:
                add_cand(u)

    # 2. Variantes directas del dominio si se tiene una URL base
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

    # 3. Heurística de dominio por nombre de comercio (solo si include_synthetic=True)
    if include_synthetic and term_lower and len(term_lower) >= 4:
        clean_slug = re.sub(r"[^a-z0-9]", "", term_lower)
        if clean_slug and len(clean_slug) >= 4 and not clean_slug.isdigit():
            add_cand(f"https://facturacion.{clean_slug}.com.mx")
            add_cand(f"https://factura.{clean_slug}.mx")

    # 4. Búsqueda web HTTP mediante DuckDuckGo Lite con múltiples consultas especializadas
    queries_to_try = []
    if term_clean and rfc_clean:
        queries_to_try.append(f"facturacion {term_clean} {rfc_clean} portal mexico")
    if term_clean:
        queries_to_try.append(f"facturacion {term_clean} portal")
        queries_to_try.append(f"facturar ticket {term_clean}")
    if rfc_clean:
        queries_to_try.append(f"facturacion electronica {rfc_clean}")

    headers = {
        "User-Agent": (
            "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
            "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/128.0.0.0 Safari/537.36"
        )
    }

    for query in queries_to_try[:2]:  # Probar hasta 2 consultas para optimizar latencia
        if len(candidates) >= 4:
            break
        try:
            async with httpx.AsyncClient(timeout=6.0, follow_redirects=True) as client:
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
            logger.debug("Búsqueda web en DuckDuckGo no disponible para '%s': %s", query, exc)

    return candidates[:8]


async def deduce_portal_for_ticket(
    comercio: Optional[str] = None,
    rfc_emisor: Optional[str] = None,
    sucursal: Optional[str] = None,
    current_url: Optional[str] = None,
    extracted_data: Optional[Dict[str, Any]] = None,
) -> Optional[str]:
    """
    Deduce de forma inteligente y autónoma el portal oficial de facturación
    del comercio para un ticket dado. Retorna la mejor URL candidata o None.
    """
    # Si ya tiene una URL explícita válida, usarla
    if current_url:
        sanitized = sanitize_and_classify_billing_url(current_url)
        if sanitized:
            return sanitized

    ext = extracted_data or {}
    nom = comercio or ext.get("comercio") or sucursal or ext.get("sucursal")
    rfc = rfc_emisor or ext.get("rfc_emisor")

    candidates = await search_candidate_portal_urls(
        comercio=nom,
        rfc_emisor=rfc,
        sucursal=sucursal,
        current_url=current_url,
    )

    if candidates:
        logger.info("Portal deducido exitosamente para comercio '%s' (RFC: %s): %s", nom, rfc, candidates[0])
        return candidates[0]

    return None


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
