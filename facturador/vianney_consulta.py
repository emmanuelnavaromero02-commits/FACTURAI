"""Read-only Vianney document lookup after a rejected sale-number lookup."""
import asyncio
import re
from urllib.parse import urlsplit

from playwright.async_api import Error as BrowserError, TimeoutError as BrowserTimeout

from .ticket_lookup import split_series_ticket


CONSULTA = 'https://poscfdi.vianney.com.mx/Consulta.aspx'
REJECTED = re.compile(
    r'Par[aá]metros incorrectos:\s*El (?:tiquet|ticket) no existe o ya ha sido facturado\.?', re.I)


def lookup_rejected(snapshot: str) -> str | None:
    match = REJECTED.search(snapshot)
    return match.group(0).rstrip('.') + '.' if match else None


async def rejection_for_current_ticket(page, snapshot: str, receipt, *, submitted_here: bool,
                                       prior_submission: bool) -> str | None:
    """Ignore a stale rejection unless this ticket was submitted or still fills the lookup."""
    rejection = lookup_rejected(snapshot)
    if not rejection:
        return None
    if submitted_here:
        return rejection
    if not prior_submission or receipt.provider != 'vianney':
        return None
    parts = split_series_ticket(receipt.ticket_number)
    address = urlsplit(page.url)
    if (not parts or address.scheme != 'https' or address.hostname != 'poscfdi.vianney.com.mx'
            or address.path not in {'/', '/Default.aspx'}):
        return None
    series = page.locator('#MainContent_serie:visible')
    ticket = page.locator('#MainContent_tiquet:visible')
    if await series.count() != 1 or await ticket.count() != 1:
        return None
    return rejection if ((await series.input_value(), await ticket.input_value()) == parts) else None


async def _consulta_form(page, series: str, ticket: str, kind: str) -> bool:
    if (urlsplit(page.url).scheme != 'https' or urlsplit(page.url).hostname != 'poscfdi.vianney.com.mx'
            or urlsplit(page.url).path != '/Consulta.aspx'):
        return False
    fields = [page.locator('#MainContent_serie:visible'), page.locator('#MainContent_tiquet:visible')]
    selector = page.locator('#MainContent_DropDownList2:visible')
    button = page.locator('#MainContent_Button1:visible')
    for field in fields:
        if await field.count() != 1 or not await field.is_editable():
            return False
    if (await selector.count() != 1 or not await selector.is_enabled()
            or await button.count() != 1 or not await button.is_enabled()):
        return False
    options = await selector.locator('option').evaluate_all(
        '(items) => items.map(item => [item.value, item.label])')
    if sorted(options) != [['pdf', 'PDF'], ['xml', 'XML']]:
        return False
    if await button.input_value() != 'Siguiente':
        return False
    await fields[0].fill(series, timeout=1500)
    await fields[1].fill(ticket, timeout=1500)
    await selector.select_option(value=kind, timeout=1500)
    return (await fields[0].input_value() == series and
            await fields[1].input_value() == ticket and
            await selector.input_value() == kind)


async def recover_vianney_consulta(runner, page, key: str, profile, receipt) -> bool:
    """Ask the portal for each format once; only runner.recover can verify a CFDI."""
    if receipt.provider != 'vianney' or not split_series_ticket(receipt.ticket_number):
        return False
    address = urlsplit(page.url)
    if address.scheme != 'https' or address.hostname != 'poscfdi.vianney.com.mx' or address.path not in {'/', '/Default.aspx'}:
        return False
    if await runner.recover(key, profile):
        return True
    series, ticket = split_series_ticket(receipt.ticket_number)
    try:
        for kind in ('pdf', 'xml'):
            await page.goto(CONSULTA, wait_until='domcontentloaded', timeout=15000)
            if not await _consulta_form(page, series, ticket, kind):
                return False
            runner.store.event(key, 'vianney_consulta', 'Se buscó el ' + kind.upper() + ' del mismo ticket, sin emitir.')
            try:
                async with page.expect_download(timeout=6000) as download:
                    await page.locator('#MainContent_Button1:visible').click(timeout=15000)
                await download.value
            except BrowserTimeout:
                pass  # The portal can show a lookup error instead of a file.
            if runner.downloads:
                await asyncio.wait(tuple(runner.downloads), timeout=12)
            if await runner.recover(key, profile):
                return True
        return False
    except BrowserError:
        return False
