"""Fill a clearly split series/ticket lookup when a portal's table inputs lack labels."""
import re


SERIES_AND_TICKET = re.compile(r'^\s*([A-Z][A-Z0-9-]{0,9})\s*/\s*([0-9][0-9,. -]{0,14})\s*$', re.I)
GROUPED_DIGITS = re.compile(r'^\d{1,3}(?:,\d{3})+$')
TEXT_INPUT = 'input[type="text"]:visible, input:not([type]):visible, input[type="number"]:visible'


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
