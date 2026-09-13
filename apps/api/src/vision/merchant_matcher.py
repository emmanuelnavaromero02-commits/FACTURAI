import re
from typing import List, Optional
from urllib.parse import urlparse

from ..models import Merchant


def match_merchant_cascade(
    merchants: List[Merchant],
    explicit_slug: Optional[str] = None,
    qr_url: Optional[str] = None,
    printed_url: Optional[str] = None,
    rfc_emisor: Optional[str] = None,
    comercio_nombre: Optional[str] = None,
) -> Optional[Merchant]:
    """
    Identifica el comercio en cascada deteniéndose en la primera coincidencia:
      a) merchant_slug enviado explícitamente por el usuario
      b) Host de la URL obtenida del QR o de la URL impresa en el ticket
      c) RFC del emisor contra los patrones configurados del comercio
      d) Expresión regular o coincidencia del nombre del comercio contra patrones
    Si ninguna coincide, retorna None.
    """
    # Nivel a: merchant_slug explícito
    if explicit_slug:
        slug_clean = explicit_slug.strip().lower()
        for m in merchants:
            if m.activo and m.slug.lower() == slug_clean:
                return m

    # Nivel b: Host de la URL del QR o URL impresa
    urls_to_check = [u for u in (qr_url, printed_url) if u]
    for raw_url in urls_to_check:
        try:
            parsed = urlparse(raw_url if "://" in raw_url else f"http://{raw_url}")
            host = (parsed.netloc or parsed.path).lower().split(":")[0]
            if host:
                for m in merchants:
                    if not m.activo:
                        continue
                    # Verificar patrones del comercio
                    patrones = m.patrones if isinstance(m.patrones, list) else []
                    for patron in patrones:
                        patron_str = str(patron).lower()
                        if patron_str in host or host in patron_str:
                            return m
        except Exception:
            pass

    # Nivel c: RFC del emisor contra merchants.patrones
    if rfc_emisor:
        rfc_clean = rfc_emisor.strip().upper()
        for m in merchants:
            if not m.activo:
                continue
            patrones = m.patrones if isinstance(m.patrones, list) else []
            for patron in patrones:
                if str(patron).strip().upper() == rfc_clean:
                    return m

    # Nivel d: Regex / coincidencia del nombre comercial contra merchants.patrones
    if comercio_nombre:
        nombre_clean = comercio_nombre.strip()
        for m in merchants:
            if not m.activo:
                continue
            patrones = m.patrones if isinstance(m.patrones, list) else []
            for patron in patrones:
                patron_str = str(patron).strip()
                try:
                    if re.search(re.escape(patron_str), nombre_clean, re.IGNORECASE):
                        return m
                except Exception:
                    if patron_str.lower() in nombre_clean.lower():
                        return m

    return None
