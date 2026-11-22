"""Consulta de solo lectura de CFDI 4.0 mediante el servicio SOAP del SAT."""
from __future__ import annotations

import re
from xml.etree import ElementTree as ET
from xml.sax.saxutils import escape

import httpx
from fastapi import APIRouter, HTTPException

from .cfdi import MAX_FILE, NS, UUID_RE
from .db import Store, now
from .read_http import read_with_retry

ENDPOINT = 'https://consultaqr.facturaelectronica.sat.gob.mx/ConsultaCFDIService.svc'
SOAP = 'http://schemas.xmlsoap.org/soap/envelope/'
SERVICE = 'http://tempuri.org/'
ACUSE = 'http://schemas.datacontract.org/2004/07/Sat.Cfdi.Negocio.ConsultaCfdi.Servicio'
ACTION = SERVICE + 'IConsultaCFDIService/Consulta'
MAX_REPLY = 64 * 1024
RFC = re.compile(r'[A-ZÑ&]{3,4}[0-9]{6}[A-Z0-9]{3}')
MESSAGES = {
    'sin_consultar': 'Sin consulta registrada.',
    'vigente': 'El SAT reportó el CFDI como vigente.',
    'cancelado': 'El SAT reportó el CFDI como cancelado.',
    'no_encontrado': 'El SAT no encontró el comprobante en esta consulta.',
}


def _xml(data: bytes):
    # Declarations in UTF-16/32 have NUL-padded characters.
    if re.search(b'<!DOCTYPE|<!ENTITY', data.replace(b'\x00', b''), re.I):
        raise ValueError('XML con declaraciones no admitidas.')
    try:
        return ET.fromstring(data)
    except ET.ParseError as exc:
        raise ValueError('XML ilegible.') from exc


def _envelope(store: Store, row: dict) -> bytes:
    fiscal_uuid = row['uuid'] or ''
    if row['status'] != 'verified' or not UUID_RE.fullmatch(fiscal_uuid):
        raise ValueError('Primero coteja el PDF y el XML del CFDI.')
    folder = store.root / 'cfdi' / fiscal_uuid
    for kind in ('pdf', 'xml'):
        expected = folder / ('factura.' + kind)
        if (row[kind] != str(expected) or expected.resolve() != expected or not expected.is_file()
                or not 0 < expected.stat().st_size <= MAX_FILE):
            raise ValueError('Falta la pareja PDF/XML cotejada en el expediente.')
    document = _xml((folder / 'factura.xml').read_bytes())
    issuer = document.find('c:Emisor', NS)
    receiver = document.find('c:Receptor', NS)
    stamp = document.find('c:Complemento/t:TimbreFiscalDigital', NS)
    if (document.tag != f"{{{NS['c']}}}Comprobante" or document.get('Version') != '4.0'
            or issuer is None or receiver is None or stamp is None or stamp.get('UUID', '').upper() != fiscal_uuid):
        raise ValueError('El CFDI guardado no corresponde al UUID cotejado.')
    sender_rfc = issuer.get('Rfc', '').strip().upper()
    recipient_rfc = receiver.get('Rfc', '').strip().upper()
    total = document.get('Total', '').strip()
    seal = ''.join(document.get('Sello', '').split())
    if not (RFC.fullmatch(sender_rfc) and RFC.fullmatch(recipient_rfc)
            and re.fullmatch(r'[0-9]{1,18}(?:\.[0-9]{1,6})?', total)
            and re.fullmatch(r'[A-Za-z0-9+/]{6,}={0,2}', seal) and len(seal) >= 8):
        raise ValueError('El XML carece de un RFC, total o sello del emisor válido para la consulta.')
    integer, _, fraction = total.partition('.')
    amount = f"{int(integer)}.{fraction.rstrip('0') or '0'}"
    # Anexo 20: preserve the stamp suffix and escape RFCs in the printed expression.
    # The SOAP serializer handles the outer XML escaping independently.
    expression = (f'https://verificacfdi.facturaelectronica.sat.gob.mx/default.aspx?id={fiscal_uuid}'
                  f'&re={escape(sender_rfc)}&rr={escape(recipient_rfc)}&tt={amount}&fe={seal[-8:]}')
    envelope = ET.Element(f'{{{SOAP}}}Envelope')
    body = ET.SubElement(envelope, f'{{{SOAP}}}Body')
    consulta = ET.SubElement(body, f'{{{SERVICE}}}Consulta')
    ET.SubElement(consulta, f'{{{SERVICE}}}expresionImpresa').text = expression
    return ET.tostring(envelope, encoding='utf-8', xml_declaration=True)


def _read_status(data: bytes) -> tuple[str, str]:
    root = _xml(data)
    result = root.find(f'{{{SOAP}}}Body/{{{SERVICE}}}ConsultaResponse/{{{SERVICE}}}ConsultaResult')
    if root.tag != f'{{{SOAP}}}Envelope' or result is None:
        raise ValueError('El SAT no devolvió un acuse de consulta válido.')
    code = (result.findtext(f'{{{ACUSE}}}CodigoEstatus') or '').strip()
    state = ' '.join((result.findtext(f'{{{ACUSE}}}Estado') or '').casefold().split())
    if re.match(r'^N\s*[-–:]?\s*602\b', code, re.I):
        status = 'no_encontrado'
    elif re.match(r'^N\s*[-–:]?\s*601\b', code, re.I):
        return 'no_disponible', 'El SAT rechazó los datos de consulta (601). Revisa el XML.'
    elif re.match(r'^S\b', code, re.I) and state in {'vigente', 'cancelado', 'no encontrado'}:
        status = state.replace(' ', '_')
    else:
        raise ValueError('El SAT devolvió una respuesta sin un estado reconocido.')
    return status, MESSAGES[status]


def query_sat(payload: bytes) -> tuple[str, str]:
    headers = {'SOAPAction': f'"{ACTION}"', 'Content-Type': 'text/xml; charset=utf-8', 'Accept': 'text/xml'}
    def lookup():
        with httpx.stream('POST', ENDPOINT, content=payload, headers=headers, timeout=12, follow_redirects=False) as reply:
            reply.raise_for_status()
            data = bytearray()
            for chunk in reply.iter_bytes(chunk_size=8192):
                data.extend(chunk)
                if len(data) > MAX_REPLY:
                    raise ValueError('La respuesta del SAT excedió el tamaño esperado.')
        return _read_status(bytes(data))
    try:
        return read_with_retry(lookup)
    except httpx.HTTPError:
        return 'no_disponible', 'No se pudo consultar el servicio del SAT. Puedes volver a intentarlo.'
    except ValueError as exc:
        return 'no_disponible', str(exc)


def sat_router(store: Store) -> APIRouter:
    with store.connect() as db:
        db.execute("""CREATE TABLE IF NOT EXISTS sat_checks (
            id INTEGER PRIMARY KEY, job_id TEXT NOT NULL, uuid TEXT NOT NULL,
            checked_at TEXT NOT NULL, status TEXT NOT NULL, message TEXT NOT NULL)""")
        db.execute('CREATE INDEX IF NOT EXISTS sat_job_checks ON sat_checks(job_id, uuid, checked_at DESC, id DESC)')
    router = APIRouter()

    @router.get('/api/jobs/{key}/sat')
    def last_check(key: str):
        fiscal_uuid = store.get(key)['uuid']
        where = ' FROM sat_checks WHERE job_id=? AND uuid=?'
        order = ' ORDER BY checked_at DESC, id DESC LIMIT 1'
        with store.connect() as db:
            db.execute('BEGIN')
            latest = db.execute('SELECT status, checked_at, message' + where + order, (key, fiscal_uuid)).fetchone()
            confirmed = db.execute('SELECT status, checked_at' + where + " AND status <> 'no_disponible'" + order,
                                   (key, fiscal_uuid)).fetchone()
        response = dict(latest) if latest else {
            'status': 'sin_consultar', 'checked_at': None, 'message': MESSAGES['sin_consultar']}
        return {**response, 'uuid': fiscal_uuid, 'last_confirmed': dict(confirmed) if confirmed else None}

    @router.post('/api/jobs/{key}/sat')
    def refresh(key: str):
        row = store.get(key)
        try:
            payload = _envelope(store, row)
        except (ValueError, OSError) as exc:
            raise HTTPException(409, detail=str(exc) if isinstance(exc, ValueError) else 'No se pudo leer el CFDI cotejado.') from exc
        started = now()
        status, message = query_sat(payload)
        with store.connect() as db:
            db.execute('INSERT INTO sat_checks(job_id,uuid,checked_at,status,message) VALUES(?,?,?,?,?)',
                       (key, row['uuid'], started, status, message))
        return last_check(key)

    return router
