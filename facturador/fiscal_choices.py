"""Choose confirmed fiscal data through visible controls and verify the site's selection."""
import re
import unicodedata
from pathlib import Path

from playwright.async_api import Error as BrowserError

from .catalog import allowed_url
from .tax_document import REGIME_NAMES

INFO = (Path(__file__).parent / 'fiscal_choice.js').read_text(encoding='utf-8')
CONTROLS = 'select:visible, [role=combobox]:visible, .p-dropdown:visible'
NAMES = {'payment': 'forma de pago', 'regime': 'régimen fiscal', 'use': 'uso del CFDI'}
PAYMENTS = {'01': ('efectivo',), '03': ('transferencia', 'transferencia electrónica de fondos'),
            '04': ('crédito', 'tarjeta de crédito'), '28': ('débito', 'tarjeta de débito'),
            '99': ('por definir',)}
USES = {'G01': ('adquisición de mercancías',), 'G03': ('gastos en general',),
        'S01': ('sin efectos fiscales',)}
# SAT catCFDI.xsd: https://www.sat.gob.mx/sitio_internet/cfd/catalogos/catCFDI.xsd
# Include valid alternatives a portal could post even when the app only supports
# a subset of payment choices. Other internal IDs may be resolved by a precise label.
SAT_CODES = {
    'payment': set('01 02 03 04 05 06 08 12 13 14 15 17 23 24 25 26 27 28 29 30 31 99'.split()),
    'regime': set(REGIME_NAMES),
    'use': {'G01', 'G02', 'G03', 'P01', 'S01', 'CP01', 'CN01',
            *(f'I{i:02}' for i in range(1, 9)), *(f'D{i:02}' for i in range(1, 11))},
}


def words(value):
    return ' '.join(re.findall(r'[a-z0-9]+', ''.join(
        c for c in unicodedata.normalize('NFD', value.casefold()) if not unicodedata.combining(c))))


def code_tokens(value, kind):
    parts = words(value).upper().split()
    pattern = {'payment': r'\d{2}', 'regime': r'\d{3}', 'use': r'[A-Z]{1,2}\d{2}'}[kind]
    return {part for part in parts if re.fullmatch(pattern, part)}


def matches(value, label, kind, expected):
    native = value.strip().upper()
    if native in SAT_CODES[kind] and native != expected:
        return False
    codes = code_tokens(label, kind)
    if codes:
        return codes == {expected}
    if kind == 'regime':
        aliases = {k: (v,) for k, v in REGIME_NAMES.items()}
    else:
        aliases = PAYMENTS if kind == 'payment' else USES
    named = {code for code, options in aliases.items() if words(label) in {words(o) for o in options}}
    if named:
        return named == {expected}
    return native == expected


def is_selected(info, kind, expected):
    if info['hasBacking']:
        if not info['value'] or not matches(info['value'], info['label'], kind, expected):
            return False
        return info['native'] or matches('', info['display'], kind, expected)
    return not info['expanded'] and matches('', info['display'], kind, expected)


async def choose(locator, frame, info, kind, expected):
    if info['native']:
        options = await locator.locator('option').evaluate_all(
            'os => os.filter(o => !o.disabled && !o.parentElement.disabled).map(o => [o.value, o.label])')
        valid = [value for value, label in options if matches(value, label, kind, expected)]
        if len(valid) != 1 or sum(value == valid[0] for value, _ in options) != 1:
            return False
        await locator.select_option(value=valid[0], timeout=1500)
    else:
        await locator.click(timeout=1500)
        # Scope the option to the control or its explicitly owned popup. A second
        # open list elsewhere must never receive this selection.
        options = locator.get_by_role('option').filter(visible=True)
        if not await options.count():
            popup = await locator.evaluate(r"""e => {
                const triggers = [e, ...e.querySelectorAll('input,button,[role=button],[role=combobox]')]
                  .filter(node => !node.closest('[role=listbox]') &&
                    (!e.matches('.p-dropdown') || node.closest('.p-dropdown') === e));
                const references = node => ['aria-controls', 'aria-owns'].flatMap(name =>
                  (node.getAttribute(name) || '').trim().split(/\s+/).filter(Boolean));
                const ids = new Set(triggers.flatMap(references));
                if (ids.size !== 1) return '';
                const id = [...ids][0];
                const owner = document.getElementById(id);
                if (!owner || (!owner.matches('[role=listbox]') &&
                    owner.querySelectorAll('[role=listbox]').length !== 1)) return '';
                const other = [...document.querySelectorAll('[aria-controls],[aria-owns]')]
                  .some(node => !e.contains(node) && references(node).includes(id));
                return other ? '' : '#' + CSS.escape(id);
            }""")
            if popup:
                options = frame.locator(popup).get_by_role('option').filter(visible=True)
        found = []
        for option in await options.all():
            if await option.is_enabled() and matches('', await option.inner_text(), kind, expected):
                found.append(option)
        if len(found) != 1:
            if await locator.locator('[role=listbox]:visible').count():
                await locator.click(timeout=1000)
            else:
                await locator.press('Escape', timeout=1000)
            return False
        await found[0].click(timeout=1500)
    actual = await locator.evaluate(INFO)
    return actual and is_selected(actual, kind, expected)


async def fiscal_choices(page, profile, receipt, *, apply=False):
    """Only act on unambiguous recipient fields in an approved portal frame."""
    fields = {}
    for frame in getattr(page, 'frames', []):
        if not allowed_url(frame.url, receipt.provider, receipt.portal_url):
            continue
        controls = frame.locator(CONTROLS)
        values = await controls.evaluate_all('elements => elements.map(' + INFO + ')')
        for index, info in enumerate(values):
            if info:
                fields.setdefault(info['kind'], []).append((frame, controls.nth(index), info))
    pending = []
    for kind, entries in fields.items():
        expected = {'payment': receipt.payment_code, 'regime': profile.regime, 'use': profile.cfdi_use}[kind]
        if all(is_selected(info, kind, expected) for _, _, info in entries):
            continue
        if apply and len(entries) == 1:
            frame, control, info = entries[0]
            try:
                current = await control.evaluate(INFO)
                if current and current['kind'] == kind and not current['disabled']:
                    if await choose(control, frame, current, kind, expected):
                        # A selection may replace other controls or alter the available options.
                        return kind, pending
            except BrowserError:
                pass  # A dependent control may be replaced by the website; observe it again.
        pending.append(NAMES[kind] + ' (' + expected + ')')
    return None, pending
