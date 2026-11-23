"""Prepare and prove a BP fuel ticket before any CFDI emission."""
import re
import unicodedata
from dataclasses import dataclass
from datetime import date
from decimal import Decimal
from pathlib import Path
from urllib.parse import urlsplit

from playwright.async_api import Error as BrowserError
from playwright.async_api import TimeoutError as BrowserTimeout

from .fiscal_choices import matches
from .local_reader import read_local_page
from .models import Profile, Receipt, money


FORM = re.compile(r'^/Karmi_FacturacionWeb/PAGE_Facturar_Gasolina_CFDI33/[A-Za-z0-9_-]+$')
TIME = re.compile(r'(?<!\d)(1[0-2]|0?[1-9]):([0-5]\d)(?::[0-5]\d)?\s*([ap])\s*\.?\s*m\.?', re.I)
PAYMENT = {'01': 'Efectivo', '28': 'Tarjeta Debito', '04': 'Tarjeta Credito'}
LOAD_LINK = "javascript:if(clWDUtil.pfGetTraitement('PAGE_FACTURAR_GASOLINA_CFDI33',18,undefined)()){_JAEE(_PAGE_,'A17',16,2,48);}"
ADD_LINK = "javascript:if(clWDUtil.pfGetTraitement('PAGE_FACTURAR_GASOLINA_CFDI33',18,undefined)()){_JAEE(_PAGE_,'A24',16,2,48);}"
ISSUE_LINK = "javascript:clWDUtil.pfGetTraitement('A40',0,undefined)();"


def plain(value: str) -> str:
    return ' '.join(''.join(char for char in unicodedata.normalize('NFD', value.casefold())
                            if not unicodedata.combining(char)).split())


def at_form(page, receipt: Receipt) -> bool:
    url = urlsplit(page.url)
    return (receipt.provider == 'bp' and url.scheme == 'https' and
            url.netloc.lower() == 'facturacion.grupocentra.mx' and
            bool(FORM.fullmatch(url.path)) and not url.query and not url.fragment)


@dataclass(frozen=True)
class BPEvidence:
    station_id: str
    ticket_number: str
    ticket_date: str
    ticket_time: str
    amount: Decimal
    payment_code: str
    liters: Decimal | None = None


@dataclass
class BPProgress:
    station_searched: bool = False
    load_attempted: bool = False
    add_attempted: bool = False


def evidence_from_text(receipt: Receipt, ocr_text: str) -> BPEvidence | None:
    """Require the printed ticket, payment and time plus two station clues."""
    if receipt.provider != 'bp' or receipt.payment_code not in PAYMENT:
        return None
    details = plain(receipt.details)
    stations = set(re.findall(r'\b(?:estacion|e\.?\s?s\.?)\s*#?\s*(\d{4,5})\b', details))
    station = next(iter(stations)) if len(stations) == 1 else ''
    if not station or sum(code.isdigit() and code.endswith(station) for code in receipt.billing_codes) != 1:
        return None
    ticket = receipt.ticket_number.strip()
    if not re.fullmatch(r'\d{5,12}', ticket) or not re.search(r'(?<!\d)'+re.escape(ticket)+r'(?!\d)', ocr_text):
        return None
    source = plain(ocr_text)
    if plain(PAYMENT[receipt.payment_code]) not in source.replace('tarjeta de ', 'tarjeta '):
        return None
    times = set()
    for match in TIME.finditer(ocr_text):
        hour, minute, period = int(match[1]), int(match[2]), match[3].lower()
        times.add(f'{hour % 12 + (12 if period == "p" else 0):02}:{minute:02}')
    if len(times) != 1:
        return None
    try:
        day = date.fromisoformat(receipt.date).strftime('%d/%m/%Y')
        amount = money(receipt.amount)
    except ValueError:
        return None
    liters = re.search(r'\b(?:consumo|litros)\s*:\s*(\d+(?:[.,]\d{1,3})?)\s*l\b', details)
    expected_liters = Decimal(liters[1].replace(',', '.')) if liters else None
    return BPEvidence(station, ticket, day, times.pop(), amount, receipt.payment_code, expected_liters)


def evidence_from_original(receipt: Receipt, source_path: Path) -> BPEvidence | None:
    """Read a ticket's four orientations without retaining OCR text in app state."""
    found = set()
    for rotation in (0, 180, 90, 270):
        try:
            text = read_local_page(source_path, rotation=rotation, force_ocr=True)['text']
        except (OSError, ValueError):
            continue
        evidence = evidence_from_text(receipt, text)
        if evidence:
            found.add(evidence)
    return next(iter(found)) if len(found) == 1 else None


async def _named(page, selector: str, label: str):
    field = page.locator(selector)
    caption = page.locator('#lz'+selector.rsplit('#', 1)[-1])
    if (await field.count() != 1 or not await field.is_visible() or
            await caption.count() != 1 or plain(await caption.inner_text()) != plain(label)):
        return None
    return field


async def bp_profile_matches(page, receipt: Receipt, profile: Profile) -> bool:
    if not at_form(page, receipt):
        return False
    fields = {}
    for selector in ('#A4', '#A3', '#A7', '#A71', '#A117'):
        field = page.locator('input'+selector)
        if await field.count() != 1 or not await field.is_visible():
            return False
        fields[selector] = await field.input_value()
    regime = fields['#A71'].strip()
    return (fields['#A4'].strip().upper() == profile.rfc and
            plain(fields['#A3']) == plain(profile.name) and
            fields['#A7'].strip().casefold() == profile.email.casefold() and
            fields['#A117'].strip() == profile.postal_code and
            matches('', regime, 'regime', profile.regime))


def _station_match(label: str, station_id: str) -> bool:
    return bool(re.search(r'(?<!\d)#\s*'+re.escape(station_id)+r'(?!\d)', label))


def _known_city(receipt: Receipt, evidence: BPEvidence) -> str:
    merchant = plain(receipt.merchant.split('(', 1)[0])
    return 'tijuana' if evidence.station_id == '11739' and merchant == 'bp playas' else ''


async def _options(select):
    return await select.locator('option').evaluate_all(
        'options => options.map(option => [option.value, option.text.trim(), option.disabled])')


async def _select_city(page, city, station, value: str):
    previous = await station.evaluate('element => element.innerHTML')
    await city.select_option(value=value, timeout=5000)
    if await station.evaluate('element => element.innerHTML') == previous:
        try:
            await page.wait_for_function("old => document.querySelector('select#A14')?.innerHTML !== old",
                                         arg=previous, timeout=3000)
        except BrowserTimeout:
            if await city.input_value() != value:
                raise
            return False
    return True


async def _find_station(page, receipt: Receipt, evidence: BPEvidence):
    city = await _named(page, 'select#A15', 'Ciudad:')
    station = await _named(page, 'select#A14', 'Sucursal:')
    if not city or not station or not await city.is_enabled() or not await station.is_enabled():
        raise ValueError('BP no mostró los selectores de ciudad y sucursal esperados.')
    cities = await _options(city)
    if not 2 <= len(cities) <= 40 or len({item[0] for item in cities}) != len(cities):
        raise ValueError('BP mostró ciudades ambiguas.')
    # Station 11739 / BP PLAYAS was verified on the public portal under Tijuana.
    # The issuer's fiscal address is in San Luis and is not a station-city clue.
    merchant = plain(receipt.merchant.split('(', 1)[0])
    known_city = _known_city(receipt, evidence)
    if known_city:
        cities = [item for item in cities if plain(item[1]) == known_city and not item[2]]
        if len(cities) != 1:
            raise ValueError('BP no mostró la ciudad verificada para esta estación.')
    matches_found = []
    uncertain = False
    selected = await city.input_value()
    for value, label, disabled in cities:
        if disabled or plain(label) in {'<seleccione>', 'seleccione'}:
            continue
        if selected != value:
            changed = await _select_city(page, city, station, value)
            selected = value
            if not changed:
                # Two cities may have the same empty dropdown. A populated but
                # unchanged dropdown cannot prove which city owns its stations.
                uncertain |= any(not option[2] and plain(option[1]) not in {'<seleccione>', 'seleccione', ''}
                                 for option in await _options(station))
                continue
        candidates = [option for option in await _options(station)
                      if not option[2] and _station_match(option[1], evidence.station_id)]
        matches_found.extend((value, option[0], option[1]) for option in candidates)
    if len(matches_found) != 1 or uncertain:
        raise ValueError('El número de estación no identifica una única sucursal BP.')
    city_value, station_value, label = matches_found[0]
    if selected != city_value:
        if not await _select_city(page, city, station, city_value):
            raise ValueError('BP no confirmó la sucursal al volver a la ciudad verificada.')
    # The receipt's descriptive merchant name is secondary corroboration.
    merchant = re.sub(r'^bp\s*', '', merchant).strip()
    if known_city and merchant not in plain(label):
        raise ValueError('La sucursal BP no coincide con el nombre del ticket.')
    await station.select_option(value=station_value, timeout=5000)
    await page.wait_for_function("expected => Boolean(document.querySelector('#A41')?.value) && "
                                 "Boolean(document.querySelector('#A13')?.value) && "
                                 "document.querySelector('select#A14')?.value === expected",
                                 arg=station_value, timeout=6000)
    return label


async def _fill(page, selector: str, label: str, expected: str):
    field = await _named(page, 'input'+selector, label)
    if not field or not await field.is_editable():
        raise ValueError('BP no mostró el campo verificable: ' + label)
    if await field.input_value() == expected:
        return False
    await field.fill(expected, timeout=1800)
    await field.blur(timeout=1800)
    if await field.input_value() != expected:
        raise ValueError('BP no conservó el valor del campo: ' + label)
    return True


async def _click_lookup(page, selector: str, caption: str, href: str):
    link = page.locator('a'+selector)
    if await link.count() != 1 or not await link.is_visible():
        raise ValueError('BP no mostró el control de consulta: ' + caption)
    attributes = await link.evaluate("a => ({text:a.innerText.trim(),href:a.getAttribute('href'),target:a.getAttribute('target'),download:a.hasAttribute('download'),onclick:a.hasAttribute('onclick')})")
    if (attributes['text'] != caption or attributes['href'] != href or attributes['target'] or
            attributes['download'] or attributes['onclick']):
        raise ValueError('BP cambió el control de consulta: ' + caption)
    await link.click(timeout=6000)


async def _amount_field(page, selector: str) -> Decimal | None:
    field = page.locator('input'+selector)
    if await field.count() != 1 or not await field.is_visible():
        return None
    value = re.sub(r'[^\d.,]', '', await field.input_value()).replace(',', '')
    try:
        return money(value)
    except ValueError:
        return None


async def _decimal_field(page, selector: str) -> Decimal | None:
    field = page.locator('input'+selector)
    if await field.count() != 1 or not await field.is_visible():
        return None
    value = re.sub(r'[^\d.,]', '', await field.input_value()).replace(',', '')
    try:
        return Decimal(value)
    except (ValueError, ArithmeticError):
        return None


async def prepare_bp_ticket(page, receipt: Receipt, profile: Profile,
                            evidence: BPEvidence | None, progress: BPProgress) -> str | None:
    """Perform one verified pre-issue step; never click Facturar."""
    if not at_form(page, receipt):
        return None
    if evidence is None:
        raise ValueError('No se pudo cotejar la hora y estación del ticket BP con la foto.')
    if not await bp_profile_matches(page, receipt, profile):
        raise ValueError('BP no refleja los datos fiscales completos del receptor.')
    if not progress.station_searched:
        await _find_station(page, receipt, evidence)
        progress.station_searched = True
        return 'Sucursal '+evidence.station_id
    station = await _named(page, 'select#A14', 'Sucursal:')
    if not station or not _station_match(await station.evaluate('e => e.selectedOptions[0]?.text || ""'), evidence.station_id):
        raise ValueError('BP perdió la sucursal comprobada.')
    for selector, label, value in (('#A12', 'No. Ticket:', evidence.ticket_number),
                                   ('#A47', 'Fecha Ticket:', evidence.ticket_date),
                                   ('#A48', 'Hora:', evidence.ticket_time)):
        if await _fill(page, selector, label, value):
            return label
    if not progress.load_attempted:
        progress.load_attempted = True
        await _click_lookup(page, '#A17', 'Cargar', LOAD_LINK)
        await page.wait_for_function("() => Boolean(document.querySelector('input#A23')?.value)", timeout=10000)
        if await _amount_field(page, '#A23') != evidence.amount:
            raise ValueError('El importe que BP cargó no coincide con la foto del ticket.')
        return 'Cargar ticket'
    if await _amount_field(page, '#A23') != evidence.amount:
        raise ValueError('El importe cargado por BP cambió o no coincide.')
    if evidence.liters is not None and await _decimal_field(page, '#A19') != evidence.liters:
        raise ValueError('Los litros cargados por BP no coinciden con la foto del ticket.')
    if not progress.add_attempted:
        progress.add_attempted = True
        await _click_lookup(page, '#A24', 'Agregar', ADD_LINK)
        await page.wait_for_function("() => Boolean(document.querySelector('input#A28')?.value)", timeout=10000)
        if await _amount_field(page, '#A28') != evidence.amount:
            raise ValueError('El total de BP no coincide con el ticket.')
        return 'Agregar ticket'
    payment = await _named(page, 'select#A33', 'Forma de Pago')
    if not payment or not await payment.is_enabled():
        raise ValueError('BP aún no permite elegir la forma de pago del ticket.')
    target = PAYMENT[evidence.payment_code]
    choices = [option for option in await _options(payment) if plain(option[1]) == plain(target) and not option[2]]
    if len(choices) != 1:
        raise ValueError('BP no muestra una forma de pago equivalente al ticket.')
    if await payment.input_value() != choices[0][0]:
        await payment.select_option(value=choices[0][0], timeout=5000)
        if await payment.input_value() != choices[0][0]:
            raise ValueError('BP no conservó la forma de pago del ticket.')
        return 'Forma de pago'
    use = await _named(page, 'select#A45', 'Uso de CFDI')
    if not use or not await use.is_enabled():
        raise ValueError('BP no mostró el uso de CFDI verificable.')
    choices = [option for option in await _options(use)
               if matches(option[0], option[1], 'use', profile.cfdi_use) and not option[2]]
    if len(choices) != 1:
        raise ValueError('BP no muestra el uso de CFDI del perfil.')
    if await use.input_value() != choices[0][0]:
        await use.select_option(value=choices[0][0], timeout=5000)
        if await use.input_value() != choices[0][0]:
            raise ValueError('BP no conservó el uso del CFDI.')
        return 'Uso CFDI'
    return None


async def bp_issue_proof(page, receipt: Receipt, profile: Profile, evidence: BPEvidence | None) -> bool:
    """Fail closed unless the last BP page proves this one receipt and receptor."""
    if evidence is None or not await bp_profile_matches(page, receipt, profile):
        return False
    station = await _named(page, 'select#A14', 'Sucursal:')
    city = await _named(page, 'select#A15', 'Ciudad:')
    if not station or not city or not _station_match(
            await station.evaluate('e => e.selectedOptions[0]?.text || ""'), evidence.station_id):
        return False
    # Automatic issue is limited to the station/city pair checked on the public portal.
    if _known_city(receipt, evidence) != plain(
            await city.evaluate('e => e.selectedOptions[0]?.text || ""')):
        return False
    for selector, label, expected in (('#A12', 'No. Ticket:', evidence.ticket_number),
                                       ('#A47', 'Fecha Ticket:', evidence.ticket_date),
                                       ('#A48', 'Hora:', evidence.ticket_time)):
        field = await _named(page, 'input'+selector, label)
        if not field or await field.input_value() != expected:
            return False
    if await _amount_field(page, '#A23') != evidence.amount or await _amount_field(page, '#A28') != evidence.amount:
        return False
    if evidence.liters is not None and await _decimal_field(page, '#A19') != evidence.liters:
        return False
    company = page.locator('input#A41')
    address = page.locator('input#A13')
    if (await company.count() != 1 or await address.count() != 1 or
            not await company.input_value() or not await address.input_value()):
        return False
    payment = await _named(page, 'select#A33', 'Forma de Pago')
    use = await _named(page, 'select#A45', 'Uso de CFDI')
    if not payment or not use or not await payment.is_enabled():
        return False
    if plain(await payment.evaluate('e => e.selectedOptions[0]?.text || ""')) != plain(PAYMENT[evidence.payment_code]):
        return False
    selected_use = await use.evaluate('e => [e.value,e.selectedOptions[0]?.text || ""]')
    if not matches(selected_use[0], selected_use[1], 'use', profile.cfdi_use):
        return False
    rows = await page.locator('#ctzA49 tr').evaluate_all(
        'rows => rows.filter(row => !row.querySelector("tr") && row.querySelector("td"))'
        '.map(row => row.innerText.trim()).filter(Boolean)')
    if len(rows) != 1:
        return False
    row = rows[0]
    has_ticket = bool(re.search(r'(?<!\d)'+re.escape(evidence.ticket_number)+r'(?!\d)', row))
    has_amount = any(_decimal_or_none(value) == evidence.amount
                     for value in re.findall(r'\d[\d,]*\.\d{2}', row))
    has_liters = (evidence.liters is None or
                  bool(re.search(r'(?<!\d)'+re.escape(str(evidence.liters))+r'(?!\d)', row)))
    if not (has_ticket and has_amount and has_liters):
        return False
    button = page.locator('a#A40')
    if await button.count() != 1 or not await button.is_visible():
        return False
    if await button.evaluate("a => [a.innerText.trim(),a.getAttribute('href'),a.getAttribute('aria-disabled')]") != [
            'Facturar', ISSUE_LINK, None]:
        return False
    try:
        await button.click(trial=True, timeout=1200)
    except BrowserError:
        return False
    return True


def _decimal_or_none(value: str) -> Decimal | None:
    try:
        return money(value)
    except ValueError:
        return None
