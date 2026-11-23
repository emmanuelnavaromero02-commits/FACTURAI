"""Fill a clearly split series/ticket lookup when a portal's table inputs lack labels."""
import re
from urllib.parse import urljoin, urlsplit

from .catalog import allowed_url
from .models import Receipt


SERIES_AND_TICKET = re.compile(r'^\s*([A-Z][A-Z0-9-]{0,9})\s*/\s*([0-9][0-9,. -]{0,14})\s*$', re.I)
GROUPED_DIGITS = re.compile(r'^\d{1,3}(?:,\d{3})+$')
TEXT_INPUT = 'input[type="text"]:visible, input:not([type]):visible, input[type="number"]:visible'
TESORO_CODE = re.compile(r'[A-Z0-9]{8,24}')
TESORO_PATH = '/GenerarCFDI/DatosTicket'


def split_series_ticket(value: str) -> tuple[str, str] | None:
    match = SERIES_AND_TICKET.fullmatch(value)
    if not match:
        return None
    series, number = match.group(1), match.group(2).strip()
    if GROUPED_DIGITS.fullmatch(number):
        number = number.replace(',', '')
    if not re.fullmatch(r'\d{1,12}', number):
        return None
    return series, number


async def prefill_ticket_lookup(page, ticket_number: str) -> str | None:
    """Use the row caption, never an input's position alone, and preserve existing values."""
    parts = split_series_ticket(ticket_number)
    if not parts:
        return None
    rows = page.locator('tr:visible')
    labels = await rows.evaluate_all("""rows => {
      const tables = Array.from(document.querySelectorAll('table'));
      const forms = Array.from(document.querySelectorAll('form'));
      return rows.slice(0, 100).map((row, index) => ({
        index, label: row.querySelector(':scope > td')?.innerText?.trim() || '',
        table: tables.indexOf(row.closest('table')),
        form: forms.indexOf(row.closest('form'))
      }));
    }""")
    matches = {}
    for item in labels:
        label = item['label'].casefold()
        group = (item['table'], item['form'])
        if item['table'] < 0:
            continue
        if re.fullmatch(r'serie|series', label):
            matches.setdefault(group, {'series': [], 'ticket': []})['series'].append(item['index'])
        elif re.fullmatch(r'ticket|tiquet|tiquete', label):
            matches.setdefault(group, {'series': [], 'ticket': []})['ticket'].append(item['index'])
    pairs = [group for group in matches.values() if len(group['series']) == len(group['ticket']) == 1]
    if len(pairs) != 1:
        return None
    matches = pairs[0]
    fields = []
    for kind, expected in zip(('series', 'ticket'), parts):
        field = rows.nth(matches[kind][0]).locator(TEXT_INPUT)
        if await field.count() != 1 or not await field.is_enabled():
            return None
        current = await field.input_value()
        if current and current != expected:
            return None
        fields.append((kind, field, current, expected))
    for kind, field, current, expected in fields:
        if not current:
            await field.fill(expected, timeout=1500)
            return kind if await field.input_value() == expected else None
    return None


async def prepare_tesoro_lookup(page, receipt: Receipt, *, verify_attempted: bool = False) -> str | None:
    """Fill and verify one printed code in Tesoro's lookup form, never its issue button.

    The live form posts distinct `btn` values for `VerificarCodigo` and
    `GenerarFactura`. Only the former may be submitted here, once per session.
    """
    if (receipt.provider != 'tesoro' or verify_attempted or
            len(receipt.billing_codes) != 1 or
            not allowed_url(page.url, 'tesoro') or urlsplit(page.url).path != TESORO_PATH):
        return None
    code = receipt.billing_codes[0].strip()
    if not TESORO_CODE.fullmatch(code):
        return None
    field = page.locator('input#CodigoFacturacion')
    if await field.count() != 1 or not await field.is_visible() or not await field.is_editable():
        return None
    metadata = await field.evaluate("""input => ({
      type: input.type, name: input.name,
      label: input.closest('.form-group')?.querySelector('label')?.textContent?.trim() || '',
      method: input.form?.method || '', action: input.form?.getAttribute('action') || '',
      verified: input.form?.querySelector('input#CodigoVerificado')?.value || ''
    })""")
    form_url = urljoin(page.url, metadata['action'])
    if (metadata['type'] != 'text' or metadata['name'] != 'CodigoFacturacion' or
            metadata['label'].casefold() != 'código facturación' or
            metadata['method'].casefold() != 'post' or
            urlsplit(form_url).path != TESORO_PATH or
            not allowed_url(form_url, 'tesoro') or metadata['verified'].casefold() != 'false'):
        return None
    # The lookup action only validates the code; the separately named
    # GenerarFactura action is intentionally untouched until emission approval.
    verify = page.locator('button#btnVerificar')
    issue = page.locator('button[name="btn"][value="GenerarFactura"]')
    if (await verify.count() != 1 or await issue.count() != 1 or
            not await verify.is_visible() or not await verify.is_enabled() or
            await issue.is_enabled()):
        return None
    button = await verify.evaluate("""button => ({
      type: button.type, name: button.name, value: button.value,
      sameForm: button.form?.querySelector('input#CodigoFacturacion') ===
                document.querySelector('input#CodigoFacturacion')
    })""")
    if button != {'type': 'submit', 'name': 'btn', 'value': 'VerificarCodigo', 'sameForm': True}:
        return None
    current = await field.input_value()
    if current and current != code:
        return None
    if not current:
        await field.fill(code, timeout=1500)
        return 'Código facturación' if await field.input_value() == code else None
    await verify.click(timeout=12000)
    return 'Verificación del código de facturación'
