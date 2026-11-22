from __future__ import annotations

import re
from decimal import Decimal

from .fiscal_choices import matches
from .models import Profile, Receipt, money

MONTHS = 'enero febrero marzo abril mayo junio julio agosto septiembre octubre noviembre diciembre'.split()


def normalize(text: str) -> str:
    return ' '.join(text.upper().split())


async def capufe_recipient(page, profile):
    """CAPUFE loads dependent fiscal options after the edited field loses focus."""
    for selector, label, value in (
        ('#rfc', 'RFC', profile.rfc), ('#nombre', 'nombre del receptor', profile.name),
        ('#domicilioFiscalReceptor', 'código postal', profile.postal_code), ('#correo', 'correo', profile.email),
    ):
        field = page.locator('input' + selector + ':visible')
        if await field.count() != 1 or not await field.is_editable():
            continue
        if normalize(await field.input_value()) == normalize(value):
            continue
        await field.fill(value, timeout=1500)
        await field.blur(timeout=1500)
        if normalize(await field.input_value()) == normalize(value):
            return label
    return None


async def capufe_proof(page, profile: Profile, receipt: Receipt, *, preparation=False) -> bool:
    """Validate CAPUFE's current receipt table independently of the AI proposal."""
    if receipt.provider != 'capufe' or not receipt.billing_codes or (not preparation and receipt.payment_code != '01'):
        return False
    values = await page.evaluate('''() => {
        const value = selector => document.querySelector(selector)?.value ?? '';
        const selected = id => {
            const field = document.getElementById(id);
            const select = field?.matches('select') ? field : field?.querySelector('select');
            return select ? [select.value, select.selectedOptions[0]?.label ?? ''] : null;
        };
        const separate = document.querySelector('#codigoPorCfdi input[type=checkbox], input#codigoPorCfdi');
        return {
            rfc: value('#rfc'), name: value('#nombre'), postal: value('#domicilioFiscalReceptor'),
            email: value('#correo'), regime: selected('regimenFiscalReceptor'), use: selected('usoCfdi'),
            payment: selected('formaPago') || selected('formaDePago') || selected('formaPagoReceptor'),
            defaultGrouping: separate ? !separate.checked : null,
            rows: Array.from(document.querySelectorAll('table tbody tr'), r => Array.from(r.cells, c => c.innerText.trim()))
        };
    }''')
    if not preparation or values.get('payment'):
        payment = values.get('payment')
        if not payment or not matches(*payment, 'payment', receipt.payment_code):
            return False
    for kind, code in [('regime', profile.regime), ('use', profile.cfdi_use)]:
        selected = values.get(kind)
        if not selected or not matches(*selected, kind, code):
            return False
    if values['defaultGrouping'] is not True and len(receipt.billing_codes) > 1:
        return False
    if any(normalize(values[key]) != normalize(expected) for key, expected in [
        ('rfc', profile.rfc), ('name', profile.name), ('postal', profile.postal_code),
        ('email', profile.email),
    ]):
        return False
    year, month, day = receipt.date.split('-')
    portal_day = f'{year}-{MONTHS[int(month) - 1]}-{day}'
    codes = {re.sub(r'\s', '', code).upper() for code in receipt.billing_codes}
    if not all(re.fullmatch('[A-Z0-9]{18}', code) for code in codes):
        return False
    seen = set()
    amount = Decimal(0)
    for row in values['rows']:
        if len(row) != 5 or not re.fullmatch('[A-Z0-9]{18}', row[1]):
            continue
        if row[1] in seen or not row[3].lower().startswith((portal_day + ' ', receipt.date + ' ')):
            return False
        seen.add(row[1])
        try:
            amount += money(row[4])
        except ValueError:
            return False
    return seen == codes and amount == money(receipt.amount)
