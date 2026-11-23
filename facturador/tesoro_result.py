"""Read Tesoro's result immediately after its code lookup, before alerts fade."""
import re
from decimal import Decimal, InvalidOperation
from urllib.parse import urlsplit

from .catalog import allowed_url
from .fiscal_choices import matches, words
from .models import money


async def tesoro_verification_failure(page, receipt, profile=None, *, issue_gate=False) -> str | None:
    """Return a pre-issuance failure only after the code lookup was submitted.

    The caller owns the one-attempt flag. This function never clicks, repeats a
    lookup, or treats the presence of an issue button as proof of issuance.
    """
    if (receipt.provider != 'tesoro' or not allowed_url(page.url, 'tesoro') or
            urlsplit(page.url).path != '/GenerarCFDI/DatosTicket'):
        return 'La emisión de Tesoro sólo se aprueba en el formulario del ticket verificado.' if issue_gate else None
    form = page.locator('form[action="/GenerarCFDI/DatosTicket"]:visible')
    if await form.count() != 1:
        return 'El portal no mostró un resultado comprobable al verificar el código de facturación.'
    selectors = ('#CodigoVerificado', '#CodigoFacturacion', '#EstacionId',
                 '#NumeroDeVenta', '#TotalVenta', 'button[name="btn"][value="GenerarFactura"]')
    fields = [form.locator(selector) for selector in selectors]
    for field in fields:
        if await field.count() != 1:
            return 'El formulario cambió después de verificar el código de facturación.'
    verified, code, station, sale, total, issue = fields
    value = (await verified.input_value()).strip().casefold()
    if len(receipt.billing_codes) != 1 or (await code.input_value()).strip() != receipt.billing_codes[0].strip():
        return 'El código visible ya no coincide con el ticket. Se detuvo antes de emitir.'
    station_value = (await station.input_value()).strip()
    sale_value = (await sale.input_value()).strip()
    total_value = (await total.input_value()).strip().replace(',', '')
    try:
        positive_total = Decimal(total_value) > 0
    except (InvalidOperation, ValueError):
        positive_total = False
    if value == 'true' and station_value and sale_value and positive_total:
        try:
            if money(total_value) != money(receipt.amount):
                return 'El importe que devolvió Tesoro no coincide con el ticket.'
        except ValueError:
            return 'El importe de Tesoro o del ticket no es comprobable.'
        if receipt.ticket_number and re.fullmatch(r'\d{1,15}', receipt.ticket_number.strip()):
            if sale_value != receipt.ticket_number.strip():
                return 'El número de venta que devolvió Tesoro no coincide con el ticket.'
        if profile is not None:
            rfc = form.locator('#RFC')
            name = form.locator('#RazonSocial')
            if (await rfc.count() != 1 or await name.count() != 1 or
                    (await rfc.input_value()).strip().upper() != profile.rfc or
                    words(await name.input_value()) != words(profile.name)):
                return 'El receptor que muestra Tesoro no coincide con el perfil fiscal.'
        payment = form.locator('#FormaDePago')
        use = form.locator('#UsoCFDI')
        if await payment.count() == 1:
            chosen = await payment.evaluate("e => ({value:e.value,label:e.selectedOptions[0]?.label || ''})")
            if not matches(chosen['value'], chosen['label'], 'payment', receipt.payment_code):
                if issue_gate or not await payment.is_enabled():
                    return 'La forma de pago de Tesoro no coincide con el ticket.'
        if profile is not None and await use.count() == 1:
            chosen = await use.evaluate("e => ({value:e.value,label:e.selectedOptions[0]?.label || ''})")
            if not matches(chosen['value'], chosen['label'], 'use', profile.cfdi_use):
                if issue_gate or not await use.is_enabled():
                    return 'El uso CFDI de Tesoro no coincide con el perfil.'
        if issue_gate:
            if await payment.count() != 1 or await use.count() != 1 or not await issue.is_enabled():
                return 'Tesoro no mostró los controles fiscales completos y un botón de emisión habilitado.'
        return None
    if value != 'false' or station_value or sale_value or positive_total or await issue.is_enabled():
        return 'La verificación del código no dejó datos de venta completos y comprobables.'
    alerts = await page.locator('.alert:visible').all_inner_texts()
    for alert in alerts:
        message = ' '.join(alert.replace('×', ' ').split()).strip()
        if not message:
            continue
        message = message.replace(receipt.billing_codes[0], '[código]')
        if profile is not None:
            message = message.replace(profile.rfc, '[RFC]')
        if len(message) <= 240:
            return message
    return 'El portal no verificó el código de facturación; estación, venta e importe quedaron vacíos.'
