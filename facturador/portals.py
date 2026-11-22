from __future__ import annotations

import re
from decimal import Decimal

from .models import Profile, Receipt, money

MONTHS = 'enero febrero marzo abril mayo junio julio agosto septiembre octubre noviembre diciembre'.split()


def normalize(text: str) -> str:
    return ' '.join(text.upper().split())


async def capufe_proof(page, profile: Profile, receipt: Receipt) -> bool:
    """Validate CAPUFE's current receipt table independently of the AI proposal."""
    if receipt.provider != 'capufe' or receipt.payment_code != '01' or not receipt.billing_codes:
        return False
    values = await page.evaluate('''() => {
        const value = selector => document.querySelector(selector)?.value ?? '';
        const selected = id => {
            const field = document.getElementById(id);
            return field?.value ?? field?.querySelector('select')?.value ?? '';
        };
        const separate = document.querySelector('#codigoPorCfdi input[type=checkbox], input#codigoPorCfdi');
        return {
            rfc: value('#rfc'), name: value('#nombre'), postal: value('#domicilioFiscalReceptor'),
            email: value('#correo'), regime: selected('regimenFiscalReceptor'), use: selected('usoCfdi'),
            defaultGrouping: separate?.checked === false,
            rows: Array.from(document.querySelectorAll('table tbody tr'), r => Array.from(r.cells, c => c.innerText.trim()))
        };
    }''')
    if any(normalize(values[key]) != normalize(expected) for key, expected in [
        ('rfc', profile.rfc), ('name', profile.name), ('postal', profile.postal_code),
        ('email', profile.email), ('regime', profile.regime), ('use', profile.cfdi_use),
    ]) or not values['defaultGrouping']:
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
