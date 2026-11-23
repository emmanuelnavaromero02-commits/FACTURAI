from __future__ import annotations

import io
import hashlib
import re
import unicodedata
import zipfile
from collections import defaultdict
from decimal import Decimal, ROUND_HALF_UP, localcontext
from pathlib import Path
from xml.etree import ElementTree as ET

from pypdf import PdfReader
from pypdf.errors import PyPdfError

from .models import Profile, Receipt, money
from .catalog import CATALOG
from .local_reader import ocr_text, render_pdf_page

NS = {"c": "http://www.sat.gob.mx/cfd/4", "t": "http://www.sat.gob.mx/TimbreFiscalDigital"}
UUID_RE = re.compile(r"[0-9A-Fa-f]{8}(?:-[0-9A-Fa-f]{4}){3}-[0-9A-Fa-f]{12}")
PDF_UUID = re.compile(r'(?<![0-9A-Z])' + UUID_RE.pattern + r'(?![0-9A-Z])', re.I)
MAX_FILE = 25 * 1024 * 1024
BP_STATIONS = {'11739': ('PL/20876/EXP/ES/2018-', 'ACO030325PHA')}


class MissingPair(ValueError):
    """Solo llegó uno de los formatos necesarios para el cotejo."""


def normalized(value: str) -> str:
    return " ".join(unicodedata.normalize("NFC", value).upper().split())


def normalize_capufe_code(value: str) -> str:
    if not isinstance(value, str) or not re.fullmatch(r'[A-Za-z0-9]+(?:[\s-]+[A-Za-z0-9]+)*', value.strip()):
        raise ValueError('Código de peaje inválido.')
    compact = re.sub(r'[\s-]+', '', value).upper()
    if len(compact) != 18:
        raise ValueError('Cada código de peaje debe contener exactamente 18 letras o dígitos.')
    return compact


def bp_ticket_concept(concepts, receipt: Receipt, issuer_rfc: str) -> None:
    """Tie a BP CFDI to a verified station, issuer, and printed sale."""
    ticket = receipt.ticket_number.strip()
    if not re.fullmatch(r'\d{5,12}', ticket):
        raise ValueError('El ticket BP no tiene un número comprobable.')
    if len(concepts) != 1:
        raise ValueError('El CFDI BP debe identificar una sola partida del ticket.')
    stations = set(re.findall(r'\b(?:estaci[oó]n|e\.?\s?s\.?)\s*#?\s*(\d{4,5})\b', receipt.details, re.I))
    station = next(iter(stations)) if len(stations) == 1 else ''
    if station not in BP_STATIONS:
        raise ValueError('No hay mapeo comprobado de estación y emisor BP para este ticket.')
    prefix, expected_issuer = BP_STATIONS[station]
    if issuer_rfc != expected_issuer:
        raise ValueError('El RFC emisor BP no corresponde a la estación del ticket.')
    identifier = (concepts[0].get('NoIdentificacion') or '').strip().upper()
    if identifier != prefix + ticket:
        raise ValueError('El CFDI BP no identifica exactamente este número de ticket y estación.')


def xml_decimal(node, field, *, default=None):
    value = node.get(field)
    if value is None and default is not None:
        return Decimal(default)
    if not isinstance(value, str) or not re.fullmatch(r'[0-9]{1,18}(?:\.[0-9]{1,6})?', value):
        raise ValueError('Importe o valor decimal fiscal inválido en ' + field)
    return Decimal(value)


def exact_product(amount, product):
    # CFDI concepts and tax amounts can carry up to six decimal places.
    # Trailing zeroes do not change the declared amount's precision.
    places = min(-2, amount.normalize().as_tuple().exponent)
    return amount == product.quantize(Decimal(1).scaleb(places), rounding=ROUND_HALF_UP)


def tax_key(tax):
    kind, factor = tax.get('Impuesto'), tax.get('TipoFactor')
    if kind not in {'001', '002', '003'} or factor not in {'Tasa', 'Cuota', 'Exento'}:
        raise ValueError('Impuesto o tipo de factor inválido en el CFDI.')
    if factor == 'Exento':
        if 'Importe' in tax.attrib or 'TasaOCuota' in tax.attrib:
            raise ValueError('Un traslado exento no puede tener tasa ni importe.')
        return kind, factor, None
    return kind, factor, xml_decimal(tax, 'TasaOCuota')


def capufe_totals(root, receipt, concepts):
    if root.get('TipoDeComprobante') != 'I' or not receipt.payment_code:
        raise ValueError('El cotejo de peajes requiere un CFDI de ingreso y una forma de pago confirmada.')
    codes = [normalize_capufe_code(value) for value in receipt.billing_codes]
    if not codes or len(codes) != len(set(codes)) or not concepts:
        raise ValueError('Faltan códigos o conceptos de peaje inequívocos.')
    invoice_codes, amounts = [], []
    subtotal = discount = Decimal(0)
    bases, transfers, withholdings = defaultdict(Decimal), defaultdict(Decimal), defaultdict(Decimal)
    for item in concepts:
        invoice_codes.append(normalize_capufe_code(item.get('NoIdentificacion')))
        if (len(item.findall('c:Impuestos', NS)) > 1 or len(item.findall('c:Impuestos/c:Traslados', NS)) > 1
                or len(item.findall('c:Impuestos/c:Retenciones', NS)) > 1):
            raise ValueError('El concepto contiene estructuras de impuestos duplicadas.')
        gross = xml_decimal(item, 'Importe')
        quantity, unit = xml_decimal(item, 'Cantidad'), xml_decimal(item, 'ValorUnitario')
        reduction = xml_decimal(item, 'Descuento', default=0)
        if quantity <= 0 or reduction > gross or not exact_product(gross, quantity * unit):
            raise ValueError('Los valores del concepto de peaje no cuadran.')
        subtotal += gross
        discount += reduction
        net = gross - reduction
        for direction, path in [('traslado', 'c:Impuestos/c:Traslados/c:Traslado'), ('retencion', 'c:Impuestos/c:Retenciones/c:Retencion')]:
            seen = set()
            for tax in item.findall(path, NS):
                key = tax_key(tax)
                base = xml_decimal(tax, 'Base')
                if key in seen or base <= 0:
                    raise ValueError('Base inválida o impuesto duplicado dentro de una partida.')
                seen.add(key)
                if direction == 'traslado':
                    bases[key] += base
                if key[1] == 'Exento':
                    if direction != 'traslado':
                        raise ValueError('Una retención no puede ser exenta.')
                    continue
                value = xml_decimal(tax, 'Importe')
                if not exact_product(value, base * key[2]):
                    raise ValueError('El impuesto de la partida no corresponde a su base y tasa.')
                if direction == 'traslado':
                    transfers[key] += value
                    net += value
                else:
                    withholdings[key[0]] += value
                    net -= value
        if net < 0:
            raise ValueError('El importe de la partida de peaje es negativo.')
        amounts.append(net)
    if len(set(invoice_codes)) != len(invoice_codes):
        raise ValueError('El mismo código de peaje figura en más de un concepto del CFDI.')
    positions = {code: index for index, code in enumerate(invoice_codes)}
    if any(code not in positions for code in codes):
        raise ValueError('El CFDI no contiene un concepto identificable para cada código del ticket.')
    indices = [positions[code] for code in codes]
    if subtotal != xml_decimal(root, 'SubTotal') or discount != xml_decimal(root, 'Descuento', default=0):
        raise ValueError('El subtotal o el descuento global no cuadran con las partidas.')
    tax_nodes = root.findall('c:Impuestos', NS)
    if len(tax_nodes) > 1 or ((bases or withholdings) and not tax_nodes):
        raise ValueError('Falta un resumen inequívoco de impuestos del CFDI.')
    if tax_nodes:
        summary = tax_nodes[0]
        if len(summary.findall('c:Traslados', NS)) > 1 or len(summary.findall('c:Retenciones', NS)) > 1:
            raise ValueError('El CFDI contiene resúmenes de impuestos duplicados.')
        summary_bases, summary_transfers, summary_withholdings = {}, {}, {}
        for tax in summary.findall('c:Traslados/c:Traslado', NS):
            key = tax_key(tax)
            if key in summary_bases:
                raise ValueError('Traslado global duplicado.')
            summary_bases[key] = xml_decimal(tax, 'Base')
            if key[1] != 'Exento':
                summary_transfers[key] = xml_decimal(tax, 'Importe')
        for tax in summary.findall('c:Retenciones/c:Retencion', NS):
            kind = tax.get('Impuesto')
            if kind in summary_withholdings:
                raise ValueError('Retención global duplicada.')
            summary_withholdings[kind] = xml_decimal(tax, 'Importe')
        if (summary_bases != bases or summary_transfers != transfers or summary_withholdings != withholdings
                or sum(transfers.values()) != xml_decimal(summary, 'TotalImpuestosTrasladados', default=None if transfers else 0)
                or sum(withholdings.values()) != xml_decimal(summary, 'TotalImpuestosRetenidos', default=None if withholdings else 0)):
            raise ValueError('Los impuestos globales no cuadran con los conceptos.')
    ticket_amount = sum(amounts[i] for i in indices)
    if sum(amounts) != xml_decimal(root, 'Total') or ticket_amount != money(receipt.amount):
        raise ValueError('El total del CFDI o el importe de los peajes identificados no cuadra.')
    return {'match_type': 'agrupado' if len(indices) < len(concepts) else 'total',
            'ticket_total': format(ticket_amount, '.2f'), 'concept_count': len(concepts),
            'matched_concepts': indices, 'matched_codes': codes, 'invoice_codes': invoice_codes}


def verify_xml(data: bytes, profile: Profile, receipt: Receipt) -> dict:
    if len(data) > MAX_FILE or b"<!DOCTYPE" in data.upper() or b"<!ENTITY" in data.upper():
        raise ValueError("XML demasiado grande o con declaraciones no permitidas")
    root = ET.fromstring(data)
    if root.tag != "{" + NS["c"] + "}Comprobante" or root.get("Version") != "4.0":
        raise ValueError("No es CFDI 4.0")
    receiver = root.find("c:Receptor", NS)
    stamp = root.find("c:Complemento/t:TimbreFiscalDigital", NS)
    if len(root.findall('c:Receptor', NS)) != 1 or len(root.findall('c:Complemento/t:TimbreFiscalDigital', NS)) != 1:
        raise ValueError("Falta receptor o timbre fiscal")
    expected = {"Rfc": profile.rfc, "Nombre": profile.name,
                "DomicilioFiscalReceptor": profile.postal_code,
                "RegimenFiscalReceptor": profile.regime, "UsoCFDI": profile.cfdi_use}
    for key, value in expected.items():
        if normalized(receiver.get(key, "")) != normalized(value):
            raise ValueError(f"CFDI no coincide en {key}")
    if root.get("Moneda") != "MXN":
        raise ValueError("Esta versión requiere comprobante MXN")
    issuer = root.find("c:Emisor", NS)
    allowed_issuers = CATALOG.get(receipt.provider, {}).get("issuer_rfcs", [])
    if allowed_issuers and (len(root.findall('c:Emisor', NS)) != 1 or issuer.get("Rfc") not in allowed_issuers):
        raise ValueError("El RFC emisor no corresponde al portal configurado")
    if receipt.payment_code and root.get("FormaPago") != receipt.payment_code:
        raise ValueError("CFDI no coincide con la forma de pago confirmada")
    fiscal_uuid = stamp.get("UUID", "").upper()
    if not UUID_RE.fullmatch(fiscal_uuid) or not stamp.get("SelloSAT", '').strip():
        raise ValueError("Timbre fiscal incompleto")
    total = money(root.get('Total', ''))
    concepts = root.findall('c:Conceptos/c:Concepto', NS)
    if receipt.provider == 'capufe':
        if len(root.findall('c:Conceptos', NS)) != 1 or not allowed_issuers:
            raise ValueError('Se requiere un emisor CAPUFE autorizado y una sola relación de conceptos.')
        with localcontext() as context:
            context.prec = 64
            matched = capufe_totals(root, receipt, concepts)
    else:
        if total != money(receipt.amount):
            raise ValueError("CFDI no coincide con el importe facturable")
        if receipt.provider == 'bp':
            if len(root.findall('c:Emisor', NS)) != 1:
                raise ValueError('El CFDI BP no tiene un emisor único.')
            bp_ticket_concept(concepts, receipt, issuer.get('Rfc', ''))
        matched = {'match_type':'total', 'ticket_total': str(total), 'concept_count': len(concepts),
                   'matched_concepts': [], 'matched_codes': [], 'invoice_codes': []}
    return {**matched, "uuid": fiscal_uuid, "total": str(total),
            "series": root.get("Serie", ""), "folio": root.get("Folio", ""),
            "payment_code": root.get("FormaPago"), "issued_at": root.get("Fecha")}


def collect_documents(paths: list[Path], output: Path) -> list[Path]:
    """Extrae solo XML/PDF por nombre base, con límites para ZIP y sin rutas externas."""
    output.mkdir(parents=True, exist_ok=True, mode=0o700)
    result = []
    count, total = 0, 0
    for path in paths:
        if path.suffix.lower() == ".zip":
            try:
                archive_file = zipfile.ZipFile(path)
            except zipfile.BadZipFile as exc:
                if len(paths) == 1:
                    raise ValueError('El ZIP está incompleto. Conserva o adjunta otra copia.') from exc
                continue
            with archive_file as archive:
                for member in archive.infolist():
                    if member.is_dir() or Path(member.filename).suffix.lower() not in {".pdf", ".xml"}:
                        continue
                    if member.file_size > MAX_FILE:
                        raise ValueError("Adjunto del ZIP demasiado grande")
                    count += 1
                    total += member.file_size
                    if count > 100 or total > 100 * 1024 * 1024:
                        raise ValueError("ZIP supera límites de extracción")
                    name = Path(member.filename.replace("\\", "/")).name
                    safe_name = re.sub(r"[^\w.()-]", "_", name)[:160]
                    target = output / f"{count:03d}_{safe_name}"
                    try:
                        data = archive.read(member)
                    except (zipfile.BadZipFile, RuntimeError, NotImplementedError):
                        continue
                    target.write_bytes(data)
                    result.append(target)
        elif path.suffix.lower() in {".pdf", ".xml"}:
            if path.stat().st_size > MAX_FILE:
                raise ValueError("Documento demasiado grande")
            result.append(path)
    return list(dict.fromkeys(result))


def find_valid_pair(paths: list[Path], profile: Profile, receipt: Receipt, expected_uuid='') -> tuple[dict, Path, Path]:
    matches = {}
    errors = []
    valid_xml = {}
    xml_digests = {}
    conflicting_uuids = set()
    for xml in [p for p in paths if p.suffix.lower() == ".xml"]:
        try:
            data = xml.read_bytes()
            result = verify_xml(data, profile, receipt)
            if not expected_uuid or result['uuid'] == expected_uuid.upper():
                digest = hashlib.sha256(data).digest()
                if xml_digests.setdefault(result['uuid'], digest) != digest:
                    conflicting_uuids.add(result['uuid'])
                valid_xml[result['uuid']] = (result, xml)
            else:
                errors.append('El XML tiene otro UUID.')
        except (ValueError, ET.ParseError) as exc:
            errors.append(str(exc))
    for pdf in (p for p in paths if p.suffix.lower() == '.pdf' and valid_xml):
        try:
            reader = PdfReader(io.BytesIO(pdf.read_bytes()))
            text = '\n'.join(page.extract_text() or '' for page in reader.pages)
            if not PDF_UUID.search(text):
                for index in range(min(len(reader.pages), 4)):
                    try:
                        text += '\n' + ocr_text(render_pdf_page(pdf, index))
                    except (ValueError, RuntimeError):
                        break
            uuids = {u.upper() for u in PDF_UUID.findall(text)}
            if len(uuids) == 1 and re.search(r'(?<![A-Z0-9Ñ&])' + re.escape(profile.rfc) + r'(?![A-Z0-9Ñ&])', text.upper()):
                found = next(iter(uuids))
                if found in valid_xml:
                    matches[found] = (*valid_xml[found], pdf)
        except (ValueError, PyPdfError, OSError, RuntimeError) as exc:
            errors.append(str(exc))
    if set(matches) & conflicting_uuids:
        raise ValueError('Hay XML distintos que declaran el mismo UUID. Requiere revisión.')
    if len(matches) > 1:
        raise ValueError("Hay varios CFDI posibles. Requiere selección manual; no se asignan por importe solamente")
    if not matches:
        suffixes = {p.suffix.lower() for p in paths}
        message = {frozenset({'.xml'}): 'Se conservó el XML. Falta el PDF para cotejarlo.',
                   frozenset({'.pdf'}): 'Se conservó el PDF. Falta el XML para cotejarlo.'}.get(frozenset(suffixes),
                             'Aún no hay pareja PDF/XML válida.')
        error_type = MissingPair if suffixes in ({'.xml'}, {'.pdf'}) and not errors else ValueError
        raise error_type((message + ' ' + ' '.join(dict.fromkeys(errors))[:350]).strip())
    return next(iter(matches.values()))
