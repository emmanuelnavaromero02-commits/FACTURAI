"""Coteja el receptor de Tesoro antes de guardar su ficha fiscal."""
from urllib.parse import urlsplit

from .fiscal_choices import matches, words


FIELDS = (
    ('#RFC', 'RFC'),
    ('#RazonSocialV4', 'razón social'),
    ('#ClaveRegimenFiscalSAT', 'régimen fiscal'),
    ('#CP', 'código postal'),
    ('#EMail', 'correo electrónico'),
)


async def tesoro_recipient(page, profile, *, apply: bool = False) -> tuple[str | None, list[str]]:
    """Change one unique visible field at a time, or report fields not matching the profile."""
    if (urlsplit(page.url).scheme != 'https' or
            urlsplit(page.url).hostname not in {'buzonfacturas.com', 'www.buzonfacturas.com'}):
        return None, []
    if urlsplit(page.url).path.rstrip('/') != '/GenerarCFDI':
        return None, []
    form = page.locator('form[action="/GenerarCFDI/GuardarCliente"]:visible')
    if await form.count() != 1:
        return None, [label for _, label in FIELDS]
    controls = {}
    for selector, label in FIELDS:
        field = form.locator(selector)
        if await field.count() != 1 or not await field.is_visible():
            return None, [label]
        controls[selector] = field

    rfc, name, regime, postal, email = (controls[selector] for selector, _ in FIELDS)
    pending = []
    if (await rfc.input_value()).strip().upper() != profile.rfc:
        pending.append('RFC')
    changes = (
        (name, 'razón social', profile.name,
         lambda current: words(current) == words(profile.name)),
        (postal, 'código postal', profile.postal_code,
         lambda current: current.strip() == profile.postal_code),
        (email, 'correo electrónico', profile.email,
         lambda current: current.strip().casefold() == profile.email.casefold()),
    )
    for field, label, expected, equal in changes:
        if equal(await field.input_value()):
            continue
        if apply and not pending and await field.is_editable():
            await field.fill(expected, timeout=1500)
            if equal(await field.input_value()):
                return label, []
        pending.append(label)

    selected = await regime.evaluate('''select => ({value: select.value,
        label: select.selectedOptions[0]?.label || '', disabled: select.disabled})''')
    if not selected['disabled'] and matches(selected['value'], selected['label'], 'regime', profile.regime):
        return None, pending
    if apply and not pending and not selected['disabled']:
        options = await regime.locator('option').evaluate_all('''options => options
            .filter(option => !option.disabled && !option.parentElement.disabled)
            .map(option => ({value: option.value, label: option.label}))''')
        valid = [option['value'] for option in options
                 if matches(option['value'], option['label'], 'regime', profile.regime)]
        if len(valid) == 1 and sum(option['value'] == valid[0] for option in options) == 1:
            await regime.select_option(value=valid[0], timeout=1500)
            selected = await regime.evaluate('''select => ({value: select.value,
                label: select.selectedOptions[0]?.label || ''})''')
            if matches(selected['value'], selected['label'], 'regime', profile.regime):
                return 'régimen fiscal', []
    pending.append('régimen fiscal')
    return None, pending
