"""Conservative extraction from printed, labelled ticket evidence, including offline reads."""
import re
import unicodedata
from datetime import date
from pathlib import Path

from .catalog import CATALOG, CUSTOM_PORTAL, allowed_url, portal_suggestions
from .local_reader import read_local_page
from .models import Receipt, money


def folded(text):
    return ''.join(c for c in unicodedata.normalize('NFD', text.upper()) if not unicodedata.combining(c))


def printed_payment(text):
    codes = set()
    for line in folded(text).splitlines():
        if re.search(r'\b(?:ACEPTAMOS|ACEPTADAS?|FORMAS DE PAGO DISPONIBLES|METODOS ACEPTADOS|PROMOCION|DESCUENTO)\b', line):
            continue
        label = r'(?:FORMA\s+DE\s+PAGO|MEDIO\s+DE\s+PAGO|PAGADO\s+(?:CON|EN)|PAGO(?:\s+CON|\s+EN)?)'
        code = re.search(r'\b' + label + r'\s*[:=-]?\s*(01|03|04|28)\b', line)
        if code:
            codes.add(code[1])
        annotated = bool(re.search(r'\b' + label + r'\b', line))
        for value, word in [('01', 'EFECTIVO'), ('04', 'CREDITO'), ('28', 'DEBITO'), ('03', 'TRANSFERENCIA')]:
            if not re.search(r'\b' + word + r'\b', line):
                continue
            if annotated or re.search(r'^\s*(?:TARJETA\s+(?:DE\s+)?|VISA\s+|MASTERCARD\s+)?' +
                                      word + r'\s*(?:$|[:$]|\d)', line):
                codes.add(value)
    return codes.pop() if len(codes) == 1 else ''


def unique(matches):
    values = set(value.strip() for value in matches if value.strip())
    return values.pop() if len(values) == 1 else ''


def local_ticket(row):
    old = Receipt.model_validate(row['receipt'])
    path = Path(row['source_path'])
    page = read_local_page(path)
    def score(reading):
        return sum(bool(re.search(word, folded(reading['text']))) for word in
                   (r'\bTOTAL\b', r'\b(FOLIO|TICKET|REFERENCIA)\b', r'\b(FECHA|PAGO)\b'))
    if (not page['text'].strip() or score(page) < 2) and page['method'] == 'ocr':
        # Try sideways/upside-down photos only when the first reading lacks ticket labels.
        for rotation in (90, 270, 180):
            candidate = read_local_page(path, rotation=rotation)
            if score(candidate) > score(page):
                page = candidate
            if score(page) >= 2:
                break
    text = page['text']
    plain = folded(text)
    if re.search(r'CONSTANCIA DE SITUACION FISCAL|TIMBRE FISCAL DIGITAL|SELLO DIGITAL DEL CFDI|'
                 r'COMPROBANTE DE TRANSFERENCIA|ESTADO DE CUENTA', plain):
        return old, 'Verifica el tipo de documento y sube el ticket de compra para facturar.'
    words = ' ' + ' '.join(re.findall(r'[A-Z0-9]+', plain)) + ' '
    recognized = [key for key, item in CATALOG.items() if any(
        ' ' + ' '.join(re.findall(r'[A-Z0-9]+', folded(s))) + ' ' in words
        for s in [*item['name'].split(' / '), *item.get('issuer_rfcs', [])])]
    values = old.model_dump()
    if len(recognized) == 1:
        values.update(provider=recognized[0], merchant=CATALOG[recognized[0]]['name'])
    links = portal_suggestions(page['codes'], text)
    billing = [url for url in links if re.search(r'factur|cfdi', url, re.I) or any(allowed_url(url, k) for k in CATALOG)]
    if len(billing) == 1 and (values['provider'] not in CATALOG or not allowed_url(billing[0], values['provider'])):
        values.update(provider=CUSTOM_PORTAL, portal_url=billing[0])
    totals = re.findall(r'(?im)^\s*(?:GRAN\s+)?TOTAL(?:\s+(?:FACTURABLE|A\s+PAGAR))?[ \t:=$]*(?:MXN[ \t$]*)?'
                        r'(\d[\d,]*\.\d{2})(?=\s|$)', plain)
    amount = unique(totals)
    if amount and 'PROPINA' not in plain:
        try:
            values['amount'] = str(money(amount))
        except ValueError:
            pass
    printed_date = unique(re.findall(r'\bFECHA(?:\s+DE\s+(?:PAGO|COMPRA|VENTA))?\s*[:=]?\s*'
                                     r'(\d{4}-\d{2}-\d{2}|\d{2}/\d{2}/\d{4})\b', plain))
    if printed_date:
        if '/' in printed_date:
            printed_date = '-'.join(reversed(printed_date.split('/')))
        try:
            values['date'] = date.fromisoformat(printed_date).isoformat()
        except ValueError:
            pass
    folio = unique(re.findall(r'(?im)^\s*(?:FOLIO(?:\s+DE\s+TICKET)?|N[ÚU]MERO\s+DE\s+TICKET|TICKET)'
                              r'[ \t]*(?:[:#]|N[Oº]\.)[ \t]*([A-Z0-9][A-Z0-9./-]{1,79})(?=\s|$)', text))
    if folio:
        values['ticket_number'] = folio
    codes = re.findall(r'(?im)^\s*C[ÓO]DIGO\s+DE\s+FACTURACI[ÓO]N\s*:\s*([A-Z0-9][A-Z0-9-]{1,99})(?=\s|$)', text)
    if codes:
        values['billing_codes'] = list(dict.fromkeys(codes))
    values['payment_code'] = row.get('confirmed_payment') or old.payment_code or printed_payment(text)
    return Receipt.model_validate(values), ''
