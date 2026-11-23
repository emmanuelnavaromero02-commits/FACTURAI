"""Prepare BP's RFC lookup on its unlabeled WebDev form."""
import re
from urllib.parse import urlsplit

from .models import Profile, Receipt


BP_FORM = re.compile(r'^/Karmi_FacturacionWeb/PAGE_Inicio/[A-Za-z0-9_-]+$')
BP_LOOKUP = re.compile(r'^https://facturacion\.grupocentra\.mx/Karmi_FacturacionWeb/PAGE_Facturar_Gasolina_CFDI33/[A-Za-z0-9_-]+$')
SEARCH_LINK = "javascript:if(clWDUtil.pfGetTraitement('PAGE_FACTURAR_GASOLINA_CFDI33',18,undefined)()){_JSL(_PAGE_,'A1','_self','','');}"


async def prepare_bp_rfc(page, receipt: Receipt, profile: Profile, *, lookup_attempted=False) -> str | None:
    """Fill the corroborated RFC, then run the single pre-issuance Buscar lookup."""
    url = urlsplit(page.url)
    if (receipt.provider != 'bp' or url.scheme != 'https' or
            url.netloc.lower() != 'facturacion.grupocentra.mx' or
            not BP_FORM.fullmatch(url.path) or url.query or url.fragment):
        return None
    field = page.locator('input#A4')
    if await field.count() != 1 or not await field.is_visible() or not await field.is_editable():
        return None
    row = await field.evaluate("e => e.closest('tr')?.innerText?.trim() || ''")
    if not re.fullmatch(r'RFC\s*:', row, re.I):
        return None
    current = await field.input_value()
    if current and current != profile.rfc:
        return None
    if not current:
        await field.fill(profile.rfc, timeout=1500)
        await field.blur(timeout=1500)
        return 'RFC' if await field.input_value() == profile.rfc else None
    if lookup_attempted:
        return None
    link = page.locator('a#A1')
    if await link.count() != 1 or not await link.is_visible():
        return None
    attributes = await link.evaluate("a => ({text:a.innerText.trim(),href:a.getAttribute('href'),target:a.getAttribute('target'),download:a.hasAttribute('download'),onclick:a.hasAttribute('onclick')})")
    if (attributes['text'] != 'Buscar' or attributes['href'] != SEARCH_LINK or attributes['target'] or
            attributes['download'] or attributes['onclick']):
        return None
    await link.click(timeout=6000)
    await page.wait_for_url(BP_LOOKUP, timeout=12000)
    return 'Buscar RFC'
