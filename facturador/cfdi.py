from __future__ import annotations

import io
import re
import unicodedata
import zipfile
from pathlib import Path
from xml.etree import ElementTree as ET

from pypdf import PdfReader
from pypdf.errors import PyPdfError

from .models import Profile, Receipt, money
from .catalog import CATALOG
from .local_reader import ocr_text, render_pdf_page

NS = {"c": "http://www.sat.gob.mx/cfd/4", "t": "http://www.sat.gob.mx/TimbreFiscalDigital"}
UUID_RE = re.compile(r"[0-9A-Fa-f]{8}(?:-[0-9A-Fa-f]{4}){3}-[0-9A-Fa-f]{12}")
MAX_FILE = 25 * 1024 * 1024


class MissingPair(ValueError):
    """Solo llegó uno de los formatos necesarios para el cotejo."""


def normalized(value: str) -> str:
    return " ".join(unicodedata.normalize("NFC", value).upper().split())


def verify_xml(data: bytes, profile: Profile, receipt: Receipt) -> dict:
    if len(data) > MAX_FILE or b"<!DOCTYPE" in data.upper() or b"<!ENTITY" in data.upper():
        raise ValueError("XML demasiado grande o con declaraciones no permitidas")
    root = ET.fromstring(data)
    if root.tag != "{" + NS["c"] + "}Comprobante" or root.get("Version") != "4.0":
        raise ValueError("No es CFDI 4.0")
    receiver = root.find("c:Receptor", NS)
    stamp = root.find("c:Complemento/t:TimbreFiscalDigital", NS)
    if receiver is None or stamp is None:
        raise ValueError("Falta receptor o timbre fiscal")
    expected = {"Rfc": profile.rfc, "Nombre": profile.name,
                "DomicilioFiscalReceptor": profile.postal_code,
                "RegimenFiscalReceptor": profile.regime, "UsoCFDI": profile.cfdi_use}
    for key, value in expected.items():
        if normalized(receiver.get(key, "")) != normalized(value):
            raise ValueError(f"CFDI no coincide en {key}")
    if money(root.get("Total", "")) != money(receipt.amount):
        raise ValueError("CFDI no coincide con el importe facturable")
    if root.get("Moneda") != "MXN":
        raise ValueError("Esta versión requiere comprobante MXN")
    issuer = root.find("c:Emisor", NS)
    allowed_issuers = CATALOG.get(receipt.provider, {}).get("issuer_rfcs", [])
    if allowed_issuers and (issuer is None or issuer.get("Rfc") not in allowed_issuers):
        raise ValueError("El RFC emisor no corresponde al portal configurado")
    if receipt.payment_code and root.get("FormaPago") != receipt.payment_code:
        raise ValueError("CFDI no coincide con la forma de pago confirmada")
    fiscal_uuid = stamp.get("UUID", "").upper()
    if not UUID_RE.fullmatch(fiscal_uuid) or not stamp.get("SelloSAT"):
        raise ValueError("Timbre fiscal incompleto")
    return {"uuid": fiscal_uuid, "total": str(money(root.get("Total", ""))),
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
    for xml in [p for p in paths if p.suffix.lower() == ".xml"]:
        try:
            result = verify_xml(xml.read_bytes(), profile, receipt)
            if not expected_uuid or result['uuid'] == expected_uuid.upper():
                valid_xml[result['uuid']] = (result, xml)
            else:
                errors.append('El XML tiene otro UUID.')
        except (ValueError, ET.ParseError) as exc:
            errors.append(str(exc))
    for pdf in (p for p in paths if p.suffix.lower() == '.pdf' and valid_xml):
        try:
            reader = PdfReader(io.BytesIO(pdf.read_bytes()))
            text = '\n'.join(page.extract_text() or '' for page in reader.pages)
            if not UUID_RE.search(text):
                for index in range(min(len(reader.pages), 4)):
                    try:
                        text += '\n' + ocr_text(render_pdf_page(pdf, index))
                    except (ValueError, RuntimeError):
                        break
            uuids = {u.upper() for u in UUID_RE.findall(text)}
            if len(uuids) == 1 and profile.rfc in text.upper():
                found = next(iter(uuids))
                if found in valid_xml:
                    matches[found] = (*valid_xml[found], pdf)
        except (ValueError, PyPdfError, OSError, RuntimeError) as exc:
            errors.append(str(exc))
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
