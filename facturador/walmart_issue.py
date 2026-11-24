"""Evidence required before Walmart's fiscal-form Aceptar can issue a CFDI."""

import re
from dataclasses import dataclass
from urllib.parse import urlsplit

from playwright.async_api import Error as BrowserError, Locator

from .fiscal_choices import matches


HOST = 'facturacion-clientes.walmart.com'
RFC = re.compile(r'\bRFC\b', re.I)
POSTAL = re.compile(r'c[oó]digo\s+postal', re.I)
TC_FIELD = re.compile(r'n[uú]mero\s+de\s+ticket|\bTC\s*#', re.I)
TR_FIELD = re.compile(r'(?:#\s*)?transacci[oó]n|\bTR\s*#', re.I)
NAME = re.compile(r'raz[oó]n\s+social|nombre\s+(?:completo|del\s+receptor)', re.I)
EMAIL = re.compile(r'correo\s+electr[oó]nico|\be-?mail\b', re.I)
REGIME = re.compile(r'r[eé]gimen\s+fiscal', re.I)
USE = re.compile(r'uso\s+(?:de\s+)?(?:factura|(?:del\s+)?CFDI)', re.I)
PAYMENT = re.compile(r'(?:forma|tipo|medio)\s+de\s+pago', re.I)
TC_CODE = re.compile(r'\s*(?:TC\s*#?\s*[:=\-]?\s*)?(\d{21,22})\s*', re.I)
TR_CODE = re.compile(r'(?<![A-Z0-9])TR\s*#?\s*[:=\-]?\s*(\d{5})(?![A-Z0-9])', re.I)


@dataclass(frozen=True)
class WalmartLookupProof:
    ticket_number: str
    transaction: str
    rfc: str
    postal_code: str
    payment_code: str


def _host(page) -> bool:
    parts = urlsplit(page.url)
    return parts.scheme == 'https' and parts.netloc.lower() == HOST and not (parts.username or parts.password)


def _ticket_codes(receipt):
    tc = TC_CODE.fullmatch(receipt.ticket_number or '')
    tr = {match.group(1) for code in receipt.billing_codes
          for match in TR_CODE.finditer(code or '')}
    if not tc or len(tr) != 1:
        return None
    return tc.group(1), next(iter(tr))


async def _input_value(form, label):
    fields = form.get_by_label(label).filter(visible=True)
    if await fields.count() != 1:
        return None
    field = fields.first
    if not await field.evaluate("e => e.matches('input:not([type=hidden]), textarea')"):
        return None
    return (await field.input_value()).strip()


async def _selected(form, label, kind, expected, *, required=True):
    fields = form.get_by_label(label).filter(visible=True)
    count = await fields.count()
    if not required and count == 0:
        return True
    if count != 1:
        return False
    data = await fields.first.evaluate("""e => e.matches('select') && e.selectedOptions.length === 1
        ? {value: e.selectedOptions[0].value, label: e.selectedOptions[0].label} : null""")
    if not data or not data['value'] or re.search(r'^\s*(?:seleccione|select)\b', data['label'], re.I):
        return False
    return matches(data['value'], data['label'], kind, expected)


async def walmart_lookup_proof(page, profile, receipt) -> WalmartLookupProof | None:
    """Read the visible lookup fields before the ticket's Continuar request."""
    if not _host(page) or urlsplit(page.url).path.rstrip('/') == '/address':
        return None
    codes = _ticket_codes(receipt)
    if not codes or not receipt.payment_code or receipt.payment_code == '99':
        return None
    try:
        if await page.locator('[role="dialog"]:visible, .modal:visible').count():
            return None
        forms = await page.locator('form:visible').all()
        found = []
        for form in forms or [page.locator('body')]:
            values = [await _input_value(form, pattern) for pattern in (RFC, POSTAL, TC_FIELD, TR_FIELD)]
            if all(value is not None for value in values):
                found.append(values)
        if len(found) != 1:
            return None
        rfc, postal, tc, tr = found[0]
    except BrowserError:
        return None
    if (rfc.upper() != profile.rfc or postal != profile.postal_code
            or tc != codes[0] or tr != codes[1]):
        return None
    return WalmartLookupProof(tc, tr, profile.rfc, profile.postal_code, receipt.payment_code)


async def walmart_issue_button(page, profile, receipt, lookup: WalmartLookupProof | None) -> Locator | None:
    """Return only the fiscal form's final Aceptar after full visible cotejo."""
    if (not lookup or not _host(page) or urlsplit(page.url).path.rstrip('/') != '/address'
            or _ticket_codes(receipt) != (lookup.ticket_number, lookup.transaction)
            or lookup.rfc != profile.rfc or lookup.postal_code != profile.postal_code
            or lookup.payment_code != receipt.payment_code or not receipt.payment_code
            or receipt.payment_code == '99'):
        return None
    try:
        if await page.locator('[role="dialog"]:visible, .modal:visible').count():
            return None
        buttons = page.get_by_role('button', name='Aceptar', exact=True).filter(visible=True)
        if await buttons.count() != 1 or not await buttons.first.is_enabled():
            return None
        button = buttons.first
        form = button.locator('xpath=ancestor::form[1]')
        if await form.count() != 1 or not await form.is_visible():
            return None
        rfc = await _input_value(form, RFC)
        name = await _input_value(form, NAME)
        postal = await _input_value(form, POSTAL)
        email = await _input_value(form, EMAIL)
        if (rfc is None or rfc.upper() != profile.rfc
                or name is None or ' '.join(name.upper().split()) != profile.name
                or postal != profile.postal_code
                or email is None or email.casefold() != profile.email.casefold()):
            return None
        if not await _selected(form, REGIME, 'regime', profile.regime):
            return None
        if not await _selected(form, USE, 'use', profile.cfdi_use):
            return None
        if not await _selected(form, PAYMENT, 'payment', receipt.payment_code, required=False):
            return None
        if await form.locator('input[type=password]:visible, input[autocomplete="one-time-code"]:visible').count():
            return None
    except BrowserError:
        return None
    return button
