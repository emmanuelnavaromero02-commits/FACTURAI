"""Recover a Tesoro CFDI that the merchant says was already issued."""

import asyncio
import re
from urllib.parse import parse_qs, urljoin, urlsplit

from playwright.async_api import Error as BrowserError, TimeoutError as BrowserTimeout

from .models import money


CONSULTA = 'https://buzonfacturas.com/CFDI/Index'
ALREADY_INVOICED = re.compile(
    r'\bEsta venta ya fue facturada con la serie y folio\s*:\s*([A-Z]{1,6}-?\d{4,12})\b', re.I)


def existing_invoice_folio(message: str) -> str | None:
    """Trust only the folio in Tesoro's post-verification sale response."""
    matches = ALREADY_INVOICED.findall(message)
    return matches[0].upper() if len(matches) == 1 else None


def _portal_result(url: str, path: str) -> bool:
    address = urlsplit(url)
    return (address.scheme == 'https' and address.hostname in {'buzonfacturas.com', 'www.buzonfacturas.com'}
            and address.path == path and not address.username and not address.password)


def _folio_matches(value: str, folio: str) -> bool:
    return re.sub(r'[-\s]', '', value.upper()) == re.sub(r'[-\s]', '', folio.upper())


async def _lookup_form(page, profile, folio: str) -> bool:
    if not _portal_result(page.url, '/CFDI/Index'):
        return False
    form = page.locator('form[action="/CFDI/DescargarFactura"]:visible')
    if await form.count() != 1:
        return False
    rfc = form.locator('#RFC:visible')
    invoice = form.locator('#FolioInterno:visible')
    submit = form.get_by_role('button', name='Buscar', exact=True)
    if (await rfc.count() != 1 or await invoice.count() != 1 or await submit.count() != 1
            or not await rfc.is_editable() or not await invoice.is_editable() or not await submit.is_enabled()):
        return False
    await rfc.fill(profile.rfc)
    await invoice.fill(folio)
    if await rfc.input_value() != profile.rfc or not _folio_matches(await invoice.input_value(), folio):
        return False
    async with page.expect_navigation(wait_until='domcontentloaded', timeout=20000):
        await submit.click(timeout=15000)
    return _portal_result(page.url, '/CFDI/DescargarFactura')


async def _matching_row(page, profile, receipt, folio: str):
    rows = page.locator('table.table tr')
    if await rows.count() < 2:
        return None
    headers = [value.strip().casefold() for value in await rows.first.locator('th').all_inner_texts()]
    if (len(headers) != 11 or headers[:4] != ['rfc receptor', 'factura', 'fecha factura', 'estatus']
            or headers[6] != 'total' or headers[-1] != 'opciones'):
        return None
    matches = []
    for index in range(1, await rows.count()):
        row = rows.nth(index)
        cells = [value.strip() for value in await row.locator('td').all_inner_texts()]
        if len(cells) != 11 or cells[0].upper() != profile.rfc or not _folio_matches(cells[1], folio):
            continue
        try:
            if cells[3].upper() != 'ACTIVA' or money(cells[6]) != money(receipt.amount):
                return None
        except ValueError:
            return None
        matches.append(row)
    return matches[0] if len(matches) == 1 else None


async def _download_link(page, row, profile, folio: str, kind: str):
    link = row.locator('a[title="Descargar archivo ' + kind + '"]')
    if await link.count() != 1:
        return None
    href = await link.get_attribute('href')
    if not href:
        return None
    url = urljoin(page.url, href)
    if not _portal_result(url, '/CFDI/DescargarFactura'):
        return None
    query = parse_qs(urlsplit(url).query)
    if (query.get('btn') != ['btn' + kind.upper()] or query.get('rfc') != [profile.rfc]
            or len(query.get('folioInterno', [])) != 1
            or not _folio_matches(query['folioInterno'][0], folio)):
        return None
    return link


async def recover_tesoro_consulta(runner, page, key: str, profile, receipt, rejection: str) -> bool:
    """Query and download an already-issued CFDI once, then let the normal validator decide."""
    folio = existing_invoice_folio(rejection)
    if (receipt.provider != 'tesoro' or not folio or not _portal_result(page.url, '/GenerarCFDI/DatosTicket')):
        return False
    if await runner.recover(key, profile):
        return True
    try:
        # The rejected form stays open in Chrome, as the user's audit trail.
        lookup = await runner.context.new_page()
        runner.page_job[lookup] = key
        await runner.viewer.show(lookup, 'Tesoro · consultar factura existente')
        await lookup.goto(CONSULTA, wait_until='domcontentloaded', timeout=20000)
        if not await _lookup_form(lookup, profile, folio):
            return False
        runner.store.event(key, 'tesoro_consulta', 'Se consultó la factura existente por RFC y folio, sin emitir otra.')
        row = await _matching_row(lookup, profile, receipt, folio)
        if row is None:
            return False
        links = [await _download_link(lookup, row, profile, folio, kind) for kind in ('xml', 'pdf')]
        if any(link is None for link in links):
            return False
        for kind, link in zip(('xml', 'pdf'), links, strict=True):
            async with lookup.expect_download(timeout=20000) as started:
                await link.click(timeout=15000)
            download = await started.value
            if not download.suggested_filename.lower().endswith('.' + kind):
                return False
        if runner.downloads:
            await asyncio.wait(tuple(runner.downloads), timeout=12)
        return await runner.recover(key, profile)
    except (BrowserError, BrowserTimeout, ValueError):
        return False
