"""Open BP's verified brand form from its image-only franchise chooser."""
import re
from urllib.parse import urljoin, urlsplit

from .catalog import allowed_url
from .models import Receipt


BP_HOME = '/Karmi_FacturacionWeb'
BP_LINK = "javascript:{_JSL(_PAGE_,'A10','_self','','');}"
BP_IMAGE = re.compile(r'/KARMI_FACTURACIONWEB_WEB/res/BP-min__[A-F0-9]+_\.png', re.I)
BACKGROUND = re.compile(r'background-image\s*:\s*url\(([^)]+)\)', re.I)


async def enter_bp_franchise(page, receipt: Receipt, snapshot: str) -> bool:
    """Click BP's lone logo, only on the empty official chooser; never submit a CFDI."""
    url = urlsplit(page.url)
    if (receipt.provider != 'bp' or url.scheme != 'https' or
            url.netloc.lower() != 'facturacion.grupocentra.mx' or
            url.path.rstrip('/') != BP_HOME or url.query or url.fragment or
            'Tiene 3 dias para realizar su factura a partir de la fecha de su compra.' not in snapshot or
            await page.locator('input:visible, select:visible, textarea:visible, button:visible').count()):
        return False
    logo = page.locator('a#A10')
    if await logo.count() != 1 or not await logo.is_visible():
        return False
    image = logo.locator('span#z_A10_IMG')
    if await image.count() != 1 or not await image.is_visible():
        return False
    details = await logo.evaluate("""anchor => {
      const image=anchor.querySelector('span#z_A10_IMG');
      return {href:anchor.getAttribute('href'), target:anchor.getAttribute('target'),
        download:anchor.hasAttribute('download'), onclick:anchor.hasAttribute('onclick'),
        style:image?.getAttribute('style') || '', duplicates:document.querySelectorAll('span[style*="BP-min__"]').length};
    }""")
    if (details['href'] != BP_LINK or details['target'] or details['download'] or
            details['onclick'] or details['duplicates'] != 1):
        return False
    background = BACKGROUND.search(details['style'])
    if not background:
        return False
    image_url = urljoin(page.url, background.group(1).strip('"\' '))
    image = urlsplit(image_url)
    if (not allowed_url(image_url, 'bp') or not BP_IMAGE.fullmatch(image.path)
            or image.query or image.fragment):
        return False
    await logo.click(timeout=6000)
    # The image chooses a franchise. The resulting page has fiscal inputs but
    # the chooser itself has no invoice data and cannot emit a document.
    await page.wait_for_url(re.compile(r'^https://facturacion\.grupocentra\.mx/Karmi_FacturacionWeb/PAGE_Inicio/[^/?#]+$'),
                            timeout=12000)
    return True
