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
LOOKUP_REJECTION = re.compile(
    r'error|rechaz|inv[aá]lid|vencid|venci[oó]|caduc|plazo|facturad|expir|'
    r'no (?:se (?:encontr[oó]|pudo)|es posible)|m[aá]s de \d+ d[ií]as', re.I)


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


async def _input_value(form, label, *, allow_placeholder=False):
    fields = form.get_by_label(label).filter(visible=True)
    count = await fields.count()
    if count == 0 and allow_placeholder:
        # Walmart's lookup form names its inputs through placeholders. Its
        # visible "*" labels have no for/id association with those inputs.
        fields = form.get_by_placeholder(label).filter(visible=True)
        count = await fields.count()
    if count != 1:
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
            values = [await _input_value(form, pattern, allow_placeholder=True)
                      for pattern in (RFC, POSTAL, TC_FIELD, TR_FIELD)]
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


async def walmart_lookup_continue(page, profile, receipt) -> tuple[WalmartLookupProof, Locator] | None:
    """Bind the verified lookup fields to Walmart's unique preparatory button."""
    if urlsplit(page.url).path.rstrip('/') != '/ticket':
        return None
    proof = await walmart_lookup_proof(page, profile, receipt)
    if proof is None:
        return None
    try:
        buttons = page.get_by_role('button', name='Continuar', exact=True).filter(visible=True)
        if await buttons.count() != 1 or not await buttons.first.is_enabled():
            return None
        button = buttons.first
        form = button.locator('xpath=ancestor::form[1]')
        if await form.count() == 1:
            containers = [form]
        else:
            # Legacy layouts can place the action just outside the form. Bind
            # it through one non-root panel containing all four lookup fields.
            containers = await button.locator(
                'xpath=ancestor::*[not(self::body) and not(self::html)]').all()
        matched = False
        for container in containers:
            if await container.locator('form:visible').count() > 1:
                continue
            values = [await _input_value(container, pattern, allow_placeholder=True)
                      for pattern in (RFC, POSTAL, TC_FIELD, TR_FIELD)]
            if (values[0] is not None and values[0].upper() == proof.rfc
                    and values[1:] == [proof.postal_code, proof.ticket_number, proof.transaction]):
                matched = True
                break
        if not matched:
            return None
    except BrowserError:
        return None
    return proof, button


async def walmart_lookup_rejection(page, previous_snapshot: str) -> str:
    """Return one visible merchant rejection after a lookup, with private values removed."""
    if not _host(page) or urlsplit(page.url).path.rstrip('/') != '/ticket':
        return ''
    try:
        alerts = page.locator('[role="alert"]:visible, .alert-danger:visible, '
                              '.validation-summary-errors:visible, .field-validation-error:visible')
        old_text = ' '.join(previous_snapshot.split()).casefold()
        candidates = []
        for item in (await alerts.all())[:5]:
            text = await item.inner_text()
            if ' '.join(text.split()).casefold() not in old_text:
                candidates.append(text)
        current = await page.locator('body').aria_snapshot(timeout=3000)
        old_lines = set(previous_snapshot.splitlines())
        candidates.extend(line for line in current.splitlines() if line not in old_lines)
    except BrowserError:
        return ''
    for candidate in candidates:
        message = re.sub(r'^\s*-?\s*(?:alert|status|text|paragraph|generic)\s*:?', '', candidate).strip(' "')
        if not LOOKUP_REJECTION.search(message):
            continue
        message = re.sub(r'[A-ZÑ&]{3,4}\d{6}[A-Z0-9]{3}|[\w.+-]+@[\w.-]+\.[A-Z]{2,}|\b\d{5,}\b',
                         '[dato]', message, flags=re.I)
        return ' '.join(message.split())[:260]
    return ''


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


PAYMENT_WORDS = re.compile(r'efectivo|tarjeta|d[eé]bito|cr[eé]dito|vales?|transferencia|monedero|cheque', re.I)
PLACEHOLDER = re.compile(r'^\s*(?:-+\s*)?(?:seleccion[ae]|elige|select|escoge)\b', re.I)


async def walmart_payment_issue(page, receipt, lookup: WalmartLookupProof | None) -> bool:
    """True when Walmart's payment page shows exactly the ticket's payment form, ready to submit.

    Walmart asks the payment form on its own page after the fiscal form and its
    «¿Están correctos todos sus datos?» check. That page's submit is the emission.
    """
    if (not lookup or not _host(page) or urlsplit(page.url).path.rstrip('/') in {'', '/ticket', '/address'}
            or _ticket_codes(receipt) != (lookup.ticket_number, lookup.transaction)
            or lookup.payment_code != receipt.payment_code or not receipt.payment_code
            or receipt.payment_code == '99'):
        return False
    try:
        choices = await page.locator('select:visible').evaluate_all("""ss => ss.map(s => ({
            options: Array.from(s.options).map(o => o.label || o.text || ''),
            value: s.selectedOptions.length === 1 ? s.selectedOptions[0].value : '',
            label: s.selectedOptions.length === 1 ? (s.selectedOptions[0].label || s.selectedOptions[0].text) : ''}))""")
        choices = [item for item in choices
                   if sum(bool(PAYMENT_WORDS.search(option)) for option in item['options']) >= 2]
        if not choices:
            choices = await page.locator('input[type=radio]:visible:checked').evaluate_all("""rs => rs.map(r => {
                const own = r.labels && r.labels.length ? r.labels[0].innerText : (r.getAttribute('aria-label') || '');
                return {options: [own], value: r.value || '', label: own};
            })""")
            choices = [item for item in choices if PAYMENT_WORDS.search(item['label'])]
        if len(choices) != 1:
            return False
        chosen = choices[0]
        if not chosen['label'].strip() or PLACEHOLDER.search(chosen['label']):
            return False
        if await page.locator('input[type=password]:visible, input[autocomplete="one-time-code"]:visible').count():
            return False
    except BrowserError:
        return False
    return matches(chosen['value'], chosen['label'], 'payment', receipt.payment_code)


async def _payment_select(page):
    """Walmart's single payment list on the payment page, or None."""
    lists = page.locator('select:visible')
    found = None
    for index in range(await lists.count()):
        control = lists.nth(index)
        options = await control.locator('option').evaluate_all(
            'os => os.filter(o => !o.disabled).map(o => [o.value, o.label || o.text || ""])')
        if sum(bool(PAYMENT_WORDS.search(label)) for _, label in options) >= 2:
            if found is not None:
                return None
            found = control, options
    return found


async def walmart_payment_options(page) -> list[str]:
    """Visible payment choices of Walmart's payment page, for a question in the chat."""
    try:
        found = await _payment_select(page)
    except BrowserError:
        return []
    if not found:
        return []
    return [' '.join(label.split())[:60] for _, label in found[1] if label.strip() and not PLACEHOLDER.search(label)][:12]


async def walmart_select_payment(page, receipt) -> bool:
    """Choose the ticket's payment form in Walmart's payment list. True when it changed the page."""
    if (not _host(page) or urlsplit(page.url).path.rstrip('/') in {'', '/ticket', '/address'}
            or not receipt.payment_code or receipt.payment_code == '99'):
        return False
    try:
        found = await _payment_select(page)
        if not found:
            return False
        control, options = found
        valid = [value for value, label in options
                 if value and not PLACEHOLDER.search(label) and matches(value, label, 'payment', receipt.payment_code)]
        if len(valid) != 1:
            return False
        if await control.input_value() == valid[0]:
            return False
        await control.select_option(valid[0], timeout=3000)
        return await control.input_value() == valid[0]
    except BrowserError:
        return False
