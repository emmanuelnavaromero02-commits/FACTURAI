import asyncio
import json
import time
import uuid
from datetime import datetime, timezone
from decimal import Decimal
from typing import Any, Dict, List, Optional
from fastapi import APIRouter, Depends, File, Form, Header, HTTPException, Query, Response, UploadFile, status
from fastapi.responses import StreamingResponse
from pydantic import BaseModel
from sqlalchemy import delete, select

from ..auth import sign_session_token

from ..db import tenant_session, sin_tenant
from ..deps import TenantContext, get_tenant_context
from ..errors import (
    AppException,
    CfdiExpiradoException,
    MaximoIntentosExcedidoException,
    RecursoNoEncontradoException,
    ReintentoInvalidoException,
)
from ..models import FiscalProfile, Merchant, MerchantCredential, Ticket, TicketEstado, TicketEvent
from ..security import encrypt_credentials
from ..services.cfdi_storage import get_cfdi_storage
from ..services.fiscal_classifier import analyze_fiscal_classification, parse_cfdi_tax_breakdown
from ..services.queue import enqueue_ticket_extraction, enqueue_ticket_facturacion
from ..services.upload import process_and_stream_upload
from ..state_machine import FINAL_STATES, transition
from ..storage import get_storage_service

router = APIRouter(prefix="/v1/tickets", tags=["Tickets"])


class TicketResponse(BaseModel):
    id: str
    tenant_id: str
    estado: str
    intentos: int = 0
    folio: Optional[str] = None
    web_id: Optional[str] = None
    fecha_ticket: Optional[str] = None
    hora_ticket: Optional[str] = None
    total: Optional[Decimal] = None
    subtotal: Optional[Decimal] = None
    iva: Optional[Decimal] = None
    sucursal: Optional[str] = None
    caja: Optional[str] = None
    rfc_emisor: Optional[str] = None
    confianza: Optional[Decimal] = None
    error_code: Optional[str] = None
    error_msg: Optional[str] = None
    image_key: Optional[str] = None
    cfdi_uuid: Optional[str] = None
    correo_capturado_en_portal: Optional[str] = None
    cfdi_disponible_hasta: Optional[str] = None
    url_facturacion: Optional[str] = None
    costo_total_usd: Optional[Decimal] = None
    tokens_input_total: Optional[int] = None
    tokens_output_total: Optional[int] = None
    pasos_agente: Optional[int] = None
    duracion_segundos: Optional[Decimal] = None
    categoria_gasto: Optional[str] = None
    desglose_impuestos: Optional[Dict[str, Any]] = None
    estatus_deducibilidad: Optional[str] = None
    score_riesgo_fiscal: Optional[int] = None
    auditoria_aritmetica: Optional[Dict[str, Any]] = None
    hash_integridad: Optional[str] = None
    created_at: str

    @classmethod
    def from_model(cls, t: Ticket) -> "TicketResponse":
        return cls(
            id=str(t.id),
            tenant_id=str(t.tenant_id),
            estado=t.estado.value,
            intentos=t.intentos,
            folio=t.folio,
            web_id=t.web_id,
            fecha_ticket=str(t.fecha_ticket) if t.fecha_ticket else None,
            hora_ticket=str(t.hora_ticket) if t.hora_ticket else None,
            total=t.total,
            subtotal=t.subtotal,
            iva=t.iva,
            sucursal=t.sucursal,
            caja=t.caja,
            rfc_emisor=t.rfc_emisor,
            confianza=t.confianza,
            error_code=t.error_code,
            error_msg=t.error_msg,
            image_key=t.image_key,
            cfdi_uuid=t.cfdi_uuid,
            correo_capturado_en_portal=t.correo_capturado_en_portal,
            cfdi_disponible_hasta=t.cfdi_disponible_hasta.isoformat() if t.cfdi_disponible_hasta else None,
            url_facturacion=t.url_facturacion,
            costo_total_usd=t.costo_total_usd,
            tokens_input_total=t.tokens_input_total,
            tokens_output_total=t.tokens_output_total,
            pasos_agente=t.pasos_agente,
            duracion_segundos=t.duracion_segundos,
            categoria_gasto=t.categoria_gasto,
            desglose_impuestos=t.desglose_impuestos,
            estatus_deducibilidad=t.estatus_deducibilidad,
            score_riesgo_fiscal=t.score_riesgo_fiscal,
            auditoria_aritmetica=t.auditoria_aritmetica,
            hash_integridad=t.hash_integridad,
            created_at=t.created_at.isoformat(),
        )


class PaginatedTicketsResponse(BaseModel):
    items: List[TicketResponse]
    next_cursor: Optional[str] = None


class BatchUploadResponse(BaseModel):
    items: List[TicketResponse]
    resumen: Dict[str, Any]


@router.post("", response_model=TicketResponse, status_code=status.HTTP_202_ACCEPTED)
async def upload_ticket(
    file: UploadFile = File(...),
    merchant_slug: Optional[str] = Form(None),
    fiscal_profile_id: Optional[uuid.UUID] = Form(None),
    ctx: TenantContext = Depends(get_tenant_context),
):
    """
    Sube un ticket para procesamiento asíncrono:
    1. Valida el tipo por magic bytes (JPEG, PNG, HEIC, WebP, PDF) y tamaño (máx 10 MB).
    2. Sube en streaming a S3/MinIO sin cargar el archivo completo en memoria y calcula SHA-256.
    3. Detecta si ya existe un ticket idéntico en el tenant para evitar doble facturación o tokens IA duplicados.
    4. Registra el ticket dentro de tenant_session.
    5. Encola la tarea de extracción en ARQ con tenant_id explícito y responde 202.
    """
    ticket_id = uuid.uuid4()
    storage = get_storage_service()

    # Subida streaming a S3 con validación de magic bytes y hash SHA-256
    storage_key, file_size, mime_type, file_hash = await process_and_stream_upload(
        file=file,
        tenant_id=ctx.tenant_id,
        ticket_id=ticket_id,
        storage=storage,
    )

    # Registro en base de datos bajo aislamiento RLS
    async with tenant_session(ctx.tenant_id, user_id=ctx.user.id) as session:
        # Verificar duplicado idéntico por hash en el tenant
        dup_hash_query = select(Ticket).where(
            Ticket.tenant_id == ctx.tenant_id,
            Ticket.hash_integridad == file_hash,
            Ticket.estado.in_([
                TicketEstado.FACTURADO,
                TicketEstado.FACTURANDO,
                TicketEstado.ENCOLADO,
                TicketEstado.EXTRAYENDO,
                TicketEstado.EXTRAIDO,
                TicketEstado.RECIBIDO,
            ]),
        ).order_by(Ticket.created_at.desc()).limit(1)
        dup_hash_res = await session.execute(dup_hash_query)
        existing_dup = dup_hash_res.scalar_one_or_none()

        if existing_dup:
            if existing_dup.estado == TicketEstado.FACTURADO:
                err_msg = f"Comprobante duplicado: este archivo ya fue facturado previamente (Ticket {str(existing_dup.id)[:8]})."
            else:
                err_msg = f"Comprobante duplicado: este archivo ya se encuentra en proceso (Ticket {str(existing_dup.id)[:8]} en estado '{existing_dup.estado.value}')."

            ticket = Ticket(
                id=ticket_id,
                tenant_id=ctx.tenant_id,
                created_by=ctx.user.id,
                fiscal_profile_id=fiscal_profile_id,
                estado=TicketEstado.RECHAZADO,
                image_key=storage_key,
                hash_integridad=file_hash,
                error_code="duplicado",
                error_msg=err_msg,
            )
            session.add(ticket)
            event = TicketEvent(
                tenant_id=ctx.tenant_id,
                ticket_id=ticket.id,
                tipo="rechazado",
                mensaje="Ticket rechazado automáticamente por duplicidad de archivo.",
                meta={"error_code": "duplicado", "ticket_referencia_id": str(existing_dup.id)},
            )
            session.add(event)
            await session.flush()
            return TicketResponse.from_model(ticket)

        ticket = Ticket(
            id=ticket_id,
            tenant_id=ctx.tenant_id,
            created_by=ctx.user.id,
            fiscal_profile_id=fiscal_profile_id,
            estado=TicketEstado.RECIBIDO,
            image_key=storage_key,
            hash_integridad=file_hash,
        )
        session.add(ticket)

        event = TicketEvent(
            tenant_id=ctx.tenant_id,
            ticket_id=ticket.id,
            tipo="recibido",
            mensaje="Ticket recibido exitosamente en el servidor.",
            meta={"file_size": file_size, "mime_type": mime_type, "file_hash": file_hash},
        )
        session.add(event)
        await session.flush()

    # Encolar en ARQ con tenant_id explícito (evita Trampa 1)
    await enqueue_ticket_extraction(
        tenant_id=ctx.tenant_id,
        ticket_id=ticket_id,
        explicit_merchant_slug=merchant_slug,
    )

    return TicketResponse.from_model(ticket)


@router.post("/batch", response_model=BatchUploadResponse, status_code=status.HTTP_202_ACCEPTED)
async def upload_tickets_batch(
    files: List[UploadFile] = File(...),
    merchant_slug: Optional[str] = Form(None),
    fiscal_profile_id: Optional[uuid.UUID] = Form(None),
    ctx: TenantContext = Depends(get_tenant_context),
):
    """
    Subida masiva y concurrente de múltiples tickets a la vez:
    1. Valida y procesa cada archivo en streaming de forma individual para aislar fallos.
    2. Detecta archivos duplicados por hash SHA-256 (tanto contra la BD como dentro del mismo lote).
    3. Registra cada ticket con aislamiento RLS.
    4. Encola en ARQ concurrentemente los tickets válidos sin estancamientos.
    5. Retorna la lista de tickets procesados y el resumen del lote.
    """
    storage = get_storage_service()
    results: List[TicketResponse] = []
    seen_hashes_in_batch: set = set()
    total_encolados = 0
    total_duplicados = 0
    total_fallidos = 0

    for f in files:
        ticket_id = uuid.uuid4()
        file_name = f.filename or "ticket"
        try:
            storage_key, file_size, mime_type, file_hash = await process_and_stream_upload(
                file=f,
                tenant_id=ctx.tenant_id,
                ticket_id=ticket_id,
                storage=storage,
            )
        except Exception as exc:
            total_fallidos += 1
            err_text = str(getattr(exc, "message", str(exc)))
            async with tenant_session(ctx.tenant_id, user_id=ctx.user.id) as session:
                failed_ticket = Ticket(
                    id=ticket_id,
                    tenant_id=ctx.tenant_id,
                    created_by=ctx.user.id,
                    fiscal_profile_id=fiscal_profile_id,
                    estado=TicketEstado.RECHAZADO,
                    error_code="archivo_invalido",
                    error_msg=err_text,
                )
                session.add(failed_ticket)
                ev = TicketEvent(
                    tenant_id=ctx.tenant_id,
                    ticket_id=ticket_id,
                    tipo="rechazado",
                    mensaje=f"Error al procesar archivo {file_name}: {err_text}",
                    meta={"error": err_text, "filename": file_name},
                )
                session.add(ev)
                await session.flush()
                results.append(TicketResponse.from_model(failed_ticket))
            continue

        is_dup = False
        dup_msg = ""
        ref_id = None

        if file_hash in seen_hashes_in_batch:
            is_dup = True
            dup_msg = f"Comprobante duplicado: el archivo '{file_name}' fue seleccionado más de una vez en este mismo lote."
        else:
            seen_hashes_in_batch.add(file_hash)
            async with tenant_session(ctx.tenant_id, user_id=ctx.user.id) as session:
                dup_hash_query = select(Ticket).where(
                    Ticket.tenant_id == ctx.tenant_id,
                    Ticket.hash_integridad == file_hash,
                    Ticket.estado.in_([
                        TicketEstado.FACTURADO,
                        TicketEstado.FACTURANDO,
                        TicketEstado.ENCOLADO,
                        TicketEstado.EXTRAYENDO,
                        TicketEstado.EXTRAIDO,
                        TicketEstado.RECIBIDO,
                    ]),
                ).order_by(Ticket.created_at.desc()).limit(1)
                dup_res = await session.execute(dup_hash_query)
                existing_dup = dup_res.scalar_one_or_none()
                if existing_dup:
                    is_dup = True
                    ref_id = str(existing_dup.id)
                    if existing_dup.estado == TicketEstado.FACTURADO:
                        dup_msg = f"Comprobante duplicado: ya fue facturado previamente (Ticket {ref_id[:8]})."
                    else:
                        dup_msg = f"Comprobante duplicado: ya se encuentra en proceso (Ticket {ref_id[:8]} en estado '{existing_dup.estado.value}')."

        if is_dup:
            total_duplicados += 1
            async with tenant_session(ctx.tenant_id, user_id=ctx.user.id) as session:
                dup_ticket = Ticket(
                    id=ticket_id,
                    tenant_id=ctx.tenant_id,
                    created_by=ctx.user.id,
                    fiscal_profile_id=fiscal_profile_id,
                    estado=TicketEstado.RECHAZADO,
                    image_key=storage_key,
                    hash_integridad=file_hash,
                    error_code="duplicado",
                    error_msg=dup_msg,
                )
                session.add(dup_ticket)
                ev = TicketEvent(
                    tenant_id=ctx.tenant_id,
                    ticket_id=ticket_id,
                    tipo="rechazado",
                    mensaje=f"Ticket {file_name} rechazado por duplicidad.",
                    meta={"error_code": "duplicado", "ticket_referencia_id": ref_id, "filename": file_name},
                )
                session.add(ev)
                await session.flush()
                results.append(TicketResponse.from_model(dup_ticket))
        else:
            total_encolados += 1
            async with tenant_session(ctx.tenant_id, user_id=ctx.user.id) as session:
                valid_ticket = Ticket(
                    id=ticket_id,
                    tenant_id=ctx.tenant_id,
                    created_by=ctx.user.id,
                    fiscal_profile_id=fiscal_profile_id,
                    estado=TicketEstado.RECIBIDO,
                    image_key=storage_key,
                    hash_integridad=file_hash,
                )
                session.add(valid_ticket)
                ev = TicketEvent(
                    tenant_id=ctx.tenant_id,
                    ticket_id=ticket_id,
                    tipo="recibido",
                    mensaje=f"Ticket {file_name} recibido en lote.",
                    meta={"file_size": file_size, "mime_type": mime_type, "file_hash": file_hash, "filename": file_name},
                )
                session.add(ev)
                await session.flush()
                results.append(TicketResponse.from_model(valid_ticket))

            # Encolar en ARQ de forma asíncrona
            await enqueue_ticket_extraction(
                tenant_id=ctx.tenant_id,
                ticket_id=ticket_id,
                explicit_merchant_slug=merchant_slug,
            )

    return BatchUploadResponse(
        items=results,
        resumen={
            "total": len(files),
            "encolados": total_encolados,
            "duplicados": total_duplicados,
            "fallidos": total_fallidos,
        },
    )


@router.post("/upload-cfdi", response_model=TicketResponse, status_code=status.HTTP_201_CREATED)
async def upload_cfdi(
    xml_file: UploadFile = File(...),
    pdf_file: Optional[UploadFile] = File(None),
    ctx: TenantContext = Depends(get_tenant_context),
):
    """
    Carga directa de un comprobante fiscal CFDI emitido (XML y opcionalmente PDF).
    Extrae, analiza el desglose de impuestos (Tasa 16%, 0%, Exento, IEPS, ISH)
    y evalúa la deducibilidad SAT al instante sin pasar por colas ni OCR.
    """
    from datetime import timedelta
    import xml.etree.ElementTree as ET

    xml_bytes = await xml_file.read()
    pdf_bytes = await pdf_file.read() if pdf_file else None

    # Parsear desglose y metadatos
    cfdi_data = parse_cfdi_tax_breakdown(xml_bytes)

    try:
        root = ET.fromstring(xml_bytes)
    except Exception:
        raise HTTPException(status_code=400, detail="El archivo proporcionado no es un XML de CFDI válido.")

    emisor_elem = None
    timbre_elem = None
    folio_val = root.attrib.get("Folio")
    serie_val = root.attrib.get("Serie")

    for elem in root.iter():
        tag = elem.tag.split("}")[-1]
        if tag == "Emisor":
            emisor_elem = elem
        elif tag == "TimbreFiscalDigital":
            timbre_elem = elem

    rfc_emisor = emisor_elem.attrib.get("Rfc") if emisor_elem is not None else None
    nombre_emisor = emisor_elem.attrib.get("Nombre") if emisor_elem is not None else None
    cfdi_uuid = timbre_elem.attrib.get("UUID") if timbre_elem is not None else str(uuid.uuid4())

    fiscal_res = analyze_fiscal_classification(
        comercio=nombre_emisor,
        rfc_emisor=rfc_emisor,
        total=cfdi_data["total"],
        subtotal=cfdi_data["subtotal"],
        iva=cfdi_data["iva_16"],
        xml_bytes=xml_bytes,
    )

    ticket_id = uuid.uuid4()
    now_dt = datetime.now(timezone.utc)

    # Guardar en almacenamiento cifrado de CFDI
    cfdi_storage = get_cfdi_storage()
    await cfdi_storage.save_cfdi_bundle(
        tenant_id=ctx.tenant_id,
        ticket_id=ticket_id,
        cfdi_uuid=cfdi_uuid,
        pdf_bytes=pdf_bytes,
        xml_bytes=xml_bytes,
        ttl_seconds=1800,
    )

    full_folio = f"{serie_val} {folio_val}".strip() if (serie_val and folio_val) else (folio_val or serie_val)

    async with tenant_session(ctx.tenant_id, user_id=ctx.user.id) as session:
        ticket = Ticket(
            id=ticket_id,
            tenant_id=ctx.tenant_id,
            created_by=ctx.user.id,
            estado=TicketEstado.FACTURADO,
            folio=full_folio,
            rfc_emisor=rfc_emisor,
            sucursal=nombre_emisor,
            total=cfdi_data["total"],
            subtotal=cfdi_data["subtotal"],
            iva=cfdi_data["iva_16"],
            cfdi_uuid=cfdi_uuid,
            cfdi_disponible_hasta=now_dt + timedelta(minutes=30),
            categoria_gasto=fiscal_res["categoria"],
            desglose_impuestos=fiscal_res["desglose_impuestos"],
            estatus_deducibilidad=fiscal_res["estatus_deducibilidad"],
            score_riesgo_fiscal=fiscal_res.get("score_riesgo_fiscal"),
            auditoria_aritmetica=fiscal_res.get("auditoria_aritmetica"),
            hash_integridad=fiscal_res.get("hash_integridad"),
            facturado_at=now_dt,
        )
        session.add(ticket)
        await session.flush()

        event = TicketEvent(
            tenant_id=ctx.tenant_id,
            ticket_id=ticket_id,
            tipo="cfdi_cargado_directamente",
            mensaje=f"CFDI recibido y clasificado como '{fiscal_res['categoria']}'. Deducibilidad: {fiscal_res['estatus_deducibilidad']}.",
            meta={"cfdi_uuid": cfdi_uuid, "categoria": fiscal_res["categoria"]},
        )
        session.add(event)
        await session.commit()

    return TicketResponse.from_model(ticket)


@router.get("", response_model=PaginatedTicketsResponse)
async def list_tickets(
    estado: Optional[TicketEstado] = None,
    categoria: Optional[str] = None,
    merchant_id: Optional[uuid.UUID] = None,
    cursor: Optional[datetime] = None,
    limit: int = Query(20, ge=1, le=100),
    ctx: TenantContext = Depends(get_tenant_context),
):
    """
    Lista tickets con filtros por estado, categoría fiscal y comercio, y paginación por cursor.
    """
    async with tenant_session(ctx.tenant_id, user_id=ctx.user.id) as session:
        query = select(Ticket).where(Ticket.tenant_id == ctx.tenant_id)

        if estado:
            query = query.where(Ticket.estado == estado)
        if categoria:
            query = query.where(Ticket.categoria_gasto == categoria)
        if merchant_id:
            query = query.where(Ticket.merchant_id == merchant_id)
        if cursor:
            query = query.where(Ticket.created_at < cursor)

        query = query.order_by(Ticket.created_at.desc(), Ticket.id.desc()).limit(limit + 1)
        res = await session.execute(query)
        tickets = res.scalars().all()

        # Auto-clasificación defensiva si algún ticket histórico carece de categoría o auditoría
        updated_any = False
        for t in tickets:
            if not t.categoria_gasto or not t.estatus_deducibilidad or t.score_riesgo_fiscal is None:
                from ..services.fiscal_classifier import analyze_fiscal_classification
                ext = t.extracted or {}
                com_name = ext.get("comercio") or t.sucursal or ""
                conceptos = " ".join([str(v) for v in ext.values() if v])
                f_res = analyze_fiscal_classification(
                    comercio=com_name,
                    rfc_emisor=t.rfc_emisor,
                    total=t.total,
                    subtotal=t.subtotal,
                    iva=t.iva,
                    forma_pago_raw=ext.get("forma_pago") or "04",
                    conceptos_text=conceptos,
                    fecha_ticket=str(t.fecha_ticket) if t.fecha_ticket else None,
                    folio=t.folio,
                )
                t.categoria_gasto = f_res["categoria"]
                t.desglose_impuestos = f_res["desglose_impuestos"]
                t.estatus_deducibilidad = f_res["estatus_deducibilidad"]
                t.score_riesgo_fiscal = f_res.get("score_riesgo_fiscal")
                t.auditoria_aritmetica = f_res.get("auditoria_aritmetica")
                t.hash_integridad = f_res.get("hash_integridad")
                updated_any = True
        if updated_any:
            await session.commit()

    items = [TicketResponse.from_model(t) for t in tickets[:limit]]
    next_cursor = None
    if len(tickets) > limit:
        next_cursor = tickets[limit - 1].created_at.isoformat()

    return PaginatedTicketsResponse(items=items, next_cursor=next_cursor)


@router.get("/{ticket_id}", response_model=TicketResponse)
async def get_ticket(
    ticket_id: uuid.UUID,
    ctx: TenantContext = Depends(get_tenant_context),
):
    """Consulta el detalle de un ticket específico."""
    async with tenant_session(ctx.tenant_id, user_id=ctx.user.id) as session:
        res = await session.execute(
            select(Ticket).where(Ticket.id == ticket_id, Ticket.tenant_id == ctx.tenant_id)
        )
        ticket = res.scalar_one_or_none()

        if ticket and (not ticket.categoria_gasto or not ticket.estatus_deducibilidad or ticket.score_riesgo_fiscal is None):
            from ..services.fiscal_classifier import analyze_fiscal_classification
            ext = ticket.extracted or {}
            com_name = ext.get("comercio") or ticket.sucursal or ""
            conceptos = " ".join([str(v) for v in ext.values() if v])
            f_res = analyze_fiscal_classification(
                comercio=com_name,
                rfc_emisor=ticket.rfc_emisor,
                total=ticket.total,
                subtotal=ticket.subtotal,
                iva=ticket.iva,
                forma_pago_raw=ext.get("forma_pago") or "04",
                conceptos_text=conceptos,
                fecha_ticket=str(ticket.fecha_ticket) if ticket.fecha_ticket else None,
                folio=ticket.folio,
            )
            ticket.categoria_gasto = f_res["categoria"]
            ticket.desglose_impuestos = f_res["desglose_impuestos"]
            ticket.estatus_deducibilidad = f_res["estatus_deducibilidad"]
            ticket.score_riesgo_fiscal = f_res.get("score_riesgo_fiscal")
            ticket.auditoria_aritmetica = f_res.get("auditoria_aritmetica")
            ticket.hash_integridad = f_res.get("hash_integridad")
            await session.commit()

    if not ticket:
        raise RecursoNoEncontradoException("El ticket no fue encontrado o no tienes permiso para verlo.")

    return TicketResponse.from_model(ticket)


@router.get("/{ticket_id}/stream")
async def stream_ticket_events(
    ticket_id: uuid.UUID,
    ctx: TenantContext = Depends(get_tenant_context),
):
    """
    Server-Sent Events (SSE) de los ticket_events (Trampa 2 evitada):
    - NO mantiene transacciones abiertas: cada ciclo abre su propia tenant_session() corta.
    - Emite keep-alive ': keep-alive\\n\\n' cada 15 segundos.
    - Cierra el stream automáticamente al alcanzar un estado final.
    """
    async def event_generator():
        last_seen_event_id = 0
        last_keep_alive_time = time.time()
        keep_alive_interval = 15.0

        # Enviar comentario inicial para abrir la conexión inmediatamente
        yield ": connected\n\n"

        while True:
            events_to_emit = []
            is_terminal = False

            # Ciclo corto de transacción (abrir -> leer -> cerrar inmediatamente)
            async with tenant_session(ctx.tenant_id, user_id=ctx.user.id) as session:
                # 1. Verificar existencia y estado del ticket
                res_t = await session.execute(
                    select(Ticket.estado).where(
                        Ticket.id == ticket_id, Ticket.tenant_id == ctx.tenant_id
                    )
                )
                t_row = res_t.first()
                if not t_row:
                    yield 'event: error\ndata: {"error": "Ticket no encontrado"}\n\n'
                    break

                current_estado = t_row[0]
                if current_estado in FINAL_STATES:
                    is_terminal = True

                # 2. Consultar nuevos eventos
                res_events = await session.execute(
                    select(TicketEvent)
                    .where(
                        TicketEvent.tenant_id == ctx.tenant_id,
                        TicketEvent.ticket_id == ticket_id,
                        TicketEvent.id > last_seen_event_id,
                    )
                    .order_by(TicketEvent.id.asc())
                )
                events_to_emit = res_events.scalars().all()

                if events_to_emit:
                    last_seen_event_id = events_to_emit[-1].id

            # Sesión cerrada: transmitir eventos leídos al cliente
            for event in events_to_emit:
                payload = json.dumps(
                    {
                        "id": event.id,
                        "ticket_id": str(event.ticket_id),
                        "tipo": event.tipo,
                        "mensaje": event.mensaje,
                        "ts": event.ts.isoformat(),
                        "meta": event.meta,
                    },
                    ensure_ascii=False,
                )
                yield f"id: {event.id}\nevent: {event.tipo}\ndata: {payload}\n\n"

            if is_terminal:
                yield 'event: close\ndata: {"motivo": "estado_final"}\n\n'
                break

            # Keep-alive cada 15 segundos
            now = time.time()
            if now - last_keep_alive_time >= keep_alive_interval:
                yield ": keep-alive\n\n"
                last_keep_alive_time = now

            await asyncio.sleep(1.0)

    return StreamingResponse(
        event_generator(),
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-cache",
            "Connection": "keep-alive",
            "X-Accel-Buffering": "no",
        },
    )


@router.post("/{ticket_id}/retry", response_model=TicketResponse, status_code=status.HTTP_202_ACCEPTED)
async def retry_ticket(
    ticket_id: uuid.UUID,
    ctx: TenantContext = Depends(get_tenant_context),
):
    """
    Reintenta el proceso de facturación de un ticket previamente RECHAZADO:
    - Solo permitido si el estado actual es 'rechazado'.
    - Límite estricto de máximo 3 intentos.
    - Transiciona a 'encolado' y encola de nuevo en ARQ.
    """
    async with tenant_session(ctx.tenant_id, user_id=ctx.user.id) as session:
        res = await session.execute(
            select(Ticket).where(Ticket.id == ticket_id, Ticket.tenant_id == ctx.tenant_id)
        )
        ticket = res.scalar_one_or_none()
        if not ticket:
            raise RecursoNoEncontradoException("El ticket no fue encontrado.")

        if ticket.estado not in (TicketEstado.RECHAZADO, TicketEstado.ESPERA_HUMANO):
            raise ReintentoInvalidoException(
                f"Solo se pueden reintentar tickets en estado 'rechazado' o 'espera_humano'. Estado actual: '{ticket.estado.value}'."
            )

        es_error_datos = ticket.error_code in ("perfil_incompleto", "datos_no_coinciden")
        if ticket.estado == TicketEstado.RECHAZADO and not es_error_datos and ticket.intentos >= 3:
            raise MaximoIntentosExcedidoException(
                f"El ticket ha alcanzado el límite máximo de 3 intentos (intentos actuales: {ticket.intentos})."
            )

        intento_previo = ticket.intentos
        if ticket.estado == TicketEstado.ESPERA_HUMANO:
            ticket.intentos = 0

        needs_extraction = not ticket.extracted and not ticket.folio
        target_state = TicketEstado.RECIBIDO if needs_extraction else TicketEstado.ENCOLADO

        await transition(
            session,
            ticket,
            target_state,
            f"Reintento manual solicitado por usuario (intento previo {intento_previo}/3).",
            tipo="reintento",
        )
        ticket.error_code = None
        ticket.error_msg = None
        await session.flush()
        response_data = TicketResponse.from_model(ticket)

    # Encolar en la cola ARQ correspondiente
    if needs_extraction:
        await enqueue_ticket_extraction(ctx.tenant_id, ticket_id)
    else:
        await enqueue_ticket_facturacion(ctx.tenant_id, ticket_id)

    return response_data


@router.post("/{ticket_id}/facturar", response_model=TicketResponse, status_code=status.HTTP_202_ACCEPTED)
async def facturar_ticket(
    ticket_id: uuid.UUID,
    ctx: TenantContext = Depends(get_tenant_context),
):
    """
    Inicia la facturación automática de un ticket en estado 'extraido' o 'rechazado':
    1. Verifica que pertenezca al tenant bajo RLS.
    2. Valida que el tenant cuente con un perfil fiscal principal.
    3. Transiciona el ticket a 'encolado'.
    4. Encola la tarea en ARQ con tenant_id y ticket_id.
    """
    async with tenant_session(ctx.tenant_id, user_id=ctx.user.id) as session:
        t_res = await session.execute(
            select(Ticket).where(Ticket.id == ticket_id, Ticket.tenant_id == ctx.tenant_id)
        )
        ticket = t_res.scalar_one_or_none()
        if not ticket:
            raise RecursoNoEncontradoException(f"Ticket {ticket_id} no encontrado.")

        if ticket.estado == TicketEstado.ENCOLADO:
            return TicketResponse.from_model(ticket)

        if ticket.estado not in (TicketEstado.EXTRAIDO, TicketEstado.RECHAZADO):
            from ..errors import AppException
            raise AppException(
                status_code=400,
                code="estado_invalido",
                message=f"No se puede facturar un ticket en estado '{ticket.estado.value}'. Solo 'extraido' o 'rechazado'."
            )

        # Verificar que el tenant tenga perfil fiscal principal
        fp_res = await session.execute(
            select(FiscalProfile).where(
                FiscalProfile.tenant_id == ctx.tenant_id,
                FiscalProfile.es_principal == True,
            )
        )
        if not fp_res.scalar_one_or_none():
            from ..errors import AppException
            raise AppException(
                status_code=400,
                code="perfil_incompleto",
                message="No tienes configurado un perfil fiscal principal. Ve a 'Datos fiscales' para configurarlo antes de facturar."
            )

        await transition(
            session,
            ticket,
            TicketEstado.ENCOLADO,
            "Facturación iniciada por usuario.",
            tipo="encolado",
        )
        ticket.error_code = None
        ticket.error_msg = None
        await session.flush()
        response_data = TicketResponse.from_model(ticket)

    await enqueue_ticket_facturacion(ctx.tenant_id, ticket_id)
    return response_data


class UpdatePortalRequest(BaseModel):
    url_facturacion: str
    facturar_ahora: bool = True


@router.patch("/{ticket_id}/portal", response_model=TicketResponse)
async def update_ticket_portal(
    ticket_id: uuid.UUID,
    payload: UpdatePortalRequest,
    ctx: TenantContext = Depends(get_tenant_context),
):
    """
    Actualiza la URL del portal de facturación del ticket y opcionalmente inicia la facturación.
    Permite rescatar tickets sin URL detectada en el comprobante impreso.
    """
    url_clean = payload.url_facturacion.strip()
    if not url_clean.startswith("http://") and not url_clean.startswith("https://"):
        url_clean = "https://" + url_clean

    should_enqueue = False
    async with tenant_session(ctx.tenant_id, user_id=ctx.user.id) as session:
        res = await session.execute(
            select(Ticket).where(Ticket.id == ticket_id, Ticket.tenant_id == ctx.tenant_id)
        )
        ticket = res.scalar_one_or_none()
        if not ticket:
            raise RecursoNoEncontradoException(f"Ticket {ticket_id} no encontrado.")

        ticket.url_facturacion = url_clean
        ticket.error_code = None
        ticket.error_msg = None

        if payload.facturar_ahora:
            if ticket.estado in (TicketEstado.EXTRAIDO, TicketEstado.ESPERA_HUMANO, TicketEstado.RECHAZADO):
                await transition(
                    session,
                    ticket,
                    TicketEstado.ENCOLADO,
                    f"URL de portal actualizada ({url_clean}). Facturación encolada.",
                    tipo="encolado",
                    meta={"url_facturacion": url_clean},
                )
                should_enqueue = True
        await session.commit()
        resp = TicketResponse.from_model(ticket)

    if should_enqueue:
        await enqueue_ticket_facturacion(ctx.tenant_id, ticket_id)

    return resp


@router.post("/{ticket_id}/deduce-portal")
async def deduce_ticket_portal_endpoint(
    ticket_id: uuid.UUID,
    ctx: TenantContext = Depends(get_tenant_context),
):
    """
    Investiga y deduce de forma autónoma con IA los portales de facturación candidatos en internet.
    """
    from ..services.portal_searcher import search_candidate_portal_urls

    async with tenant_session(ctx.tenant_id, user_id=ctx.user.id) as session:
        res = await session.execute(
            select(Ticket).where(Ticket.id == ticket_id, Ticket.tenant_id == ctx.tenant_id)
        )
        ticket = res.scalar_one_or_none()
        if not ticket:
            raise RecursoNoEncontradoException(f"Ticket {ticket_id} no encontrado.")

        ext = ticket.extracted or {}
        com_name = ext.get("comercio") or ticket.sucursal or ""
        rfc = ticket.rfc_emisor or ext.get("rfc_emisor") or ""
        candidates = await search_candidate_portal_urls(
            comercio=com_name,
            rfc_emisor=rfc,
            sucursal=ticket.sucursal,
            current_url=ticket.url_facturacion,
            include_synthetic=True,
        )

        deduced = candidates[0] if candidates else None
        if deduced and not ticket.url_facturacion:
            ticket.url_facturacion = deduced
            ticket.error_code = None
            ticket.error_msg = None
            await session.commit()

        return {
            "ticket_id": str(ticket_id),
            "candidates": candidates,
            "deduced_url": deduced,
            "url_facturacion_actual": ticket.url_facturacion,
        }


class TicketCredentialsRequest(BaseModel):
    usuario: str
    password: str
    merchant_id: Optional[uuid.UUID] = None
    facturar_ahora: bool = True


@router.post("/{ticket_id}/credentials", response_model=TicketResponse)
async def set_ticket_credentials_and_enqueue(
    ticket_id: uuid.UUID,
    payload: TicketCredentialsRequest,
    ctx: TenantContext = Depends(get_tenant_context),
):
    """
    Guarda las credenciales de acceso para el portal/comercio asociado a este ticket
    y opcionalmente encola la facturación para reintento inmediato.
    """
    should_enqueue = False
    async with tenant_session(ctx.tenant_id, user_id=ctx.user.id) as session:
        res = await session.execute(
            select(Ticket).where(Ticket.id == ticket_id, Ticket.tenant_id == ctx.tenant_id)
        )
        ticket = res.scalar_one_or_none()
        if not ticket:
            raise RecursoNoEncontradoException(f"Ticket {ticket_id} no encontrado.")

        target_merchant_id = payload.merchant_id or ticket.merchant_id
        if not target_merchant_id:
            # Deducir merchant a partir de URL, rfc_emisor o slug
            async with sin_tenant() as global_session:
                url_str = (ticket.url_facturacion or "").lower()
                com_str = (ticket.extracted.get("comercio") or "").lower() if ticket.extracted else ""
                if "g500" in url_str or "g500" in com_str or "fento" in com_str:
                    m_res = await global_session.execute(select(Merchant.id).where(Merchant.slug == "g500"))
                    target_merchant_id = m_res.scalar_one_or_none()
                elif "alsea" in url_str or "interfactura" in url_str or "dominos" in url_str:
                    m_res = await global_session.execute(select(Merchant.id).where(Merchant.slug == "alsea"))
                    target_merchant_id = m_res.scalar_one_or_none()

                if not target_merchant_id:
                    m_res = await global_session.execute(select(Merchant.id).where(Merchant.slug == "generico-web"))
                    target_merchant_id = m_res.scalar_one_or_none()

        if not target_merchant_id:
            raise AppException(status_code=400, code="merchant_no_resuelto", message="No se pudo asociar un comercio para guardar las credenciales.")

        # Asociar merchant al ticket si aún no lo tenía
        if not ticket.merchant_id:
            ticket.merchant_id = target_merchant_id

        # Cifrar credenciales con clave derivada del tenant (AES-256-GCM)
        cred_dict = {"usuario": payload.usuario.strip(), "password": payload.password.strip()}
        payload_enc, nonce = encrypt_credentials(ctx.tenant_id, json.dumps(cred_dict).encode("utf-8"))

        # Upsert en merchant_credentials
        c_res = await session.execute(
            select(MerchantCredential).where(
                MerchantCredential.tenant_id == ctx.tenant_id,
                MerchantCredential.merchant_id == target_merchant_id,
            )
        )
        existing_cred = c_res.scalar_one_or_none()
        if existing_cred:
            existing_cred.payload_enc = payload_enc
            existing_cred.nonce = nonce
        else:
            session.add(
                MerchantCredential(
                    tenant_id=ctx.tenant_id,
                    merchant_id=target_merchant_id,
                    payload_enc=payload_enc,
                    nonce=nonce,
                )
            )

        ticket.error_code = None
        ticket.error_msg = None

        if payload.facturar_ahora:
            if ticket.estado in (TicketEstado.EXTRAIDO, TicketEstado.ESPERA_HUMANO, TicketEstado.RECHAZADO):
                await transition(
                    session,
                    ticket,
                    TicketEstado.ENCOLADO,
                    "Credenciales configuradas. Facturación encolada.",
                    tipo="encolado",
                    meta={"merchant_id": str(target_merchant_id)},
                )
                should_enqueue = True

        await session.commit()
        resp = TicketResponse.from_model(ticket)

    if should_enqueue:
        await enqueue_ticket_facturacion(ctx.tenant_id, ticket_id)

    return resp


@router.get("/{ticket_id}/cfdi")
async def download_cfdi_bundle(
    ticket_id: uuid.UUID,
    ctx: TenantContext = Depends(get_tenant_context),
):
    """
    Descarga efímera del CFDI 4.0 (ZIP con PDF y XML):
    - Solo disponible si el portal entregó archivos directos (entrega='descarga').
    - Recupera el archivo de Redis descifrado con la llave del tenant.
    - Máximo 5 descargas dentro de los 30 minutos; al expirar o superar el límite devuelve 404 cfdi_expirado.
    """
    async with tenant_session(ctx.tenant_id, user_id=ctx.user.id) as session:
        res = await session.execute(
            select(Ticket).where(Ticket.id == ticket_id, Ticket.tenant_id == ctx.tenant_id)
        )
        ticket = res.scalar_one_or_none()
        if not ticket:
            raise RecursoNoEncontradoException("El ticket no fue encontrado.")

    cfdi_storage = get_cfdi_storage()
    zip_bytes = await cfdi_storage.get_cfdi_bundle(ctx.tenant_id, ticket_id)
    if not zip_bytes:
        raise CfdiExpiradoException()

    filename = f"cfdi-{ticket.cfdi_uuid or ticket_id}.zip"
    return Response(
        content=zip_bytes,
        media_type="application/zip",
        headers={
            "Content-Disposition": f'attachment; filename="{filename}"',
            "Cache-Control": "no-store, no-cache, must-revalidate",
        },
    )


def extract_single_file_from_zip(zip_bytes: bytes, extension: str) -> Optional[bytes]:
    import io
    import zipfile
    try:
        with zipfile.ZipFile(io.BytesIO(zip_bytes)) as zf:
            for fname in zf.namelist():
                if fname.lower().endswith(extension.lower()):
                    return zf.read(fname)
    except Exception:
        pass
    return None


@router.get("/{ticket_id}/cfdi/pdf")
async def download_cfdi_pdf(
    ticket_id: uuid.UUID,
    ctx: TenantContext = Depends(get_tenant_context),
):
    """
    Descarga o visualización directa del PDF del CFDI 4.0.
    """
    async with tenant_session(ctx.tenant_id, user_id=ctx.user.id) as session:
        res = await session.execute(
            select(Ticket).where(Ticket.id == ticket_id, Ticket.tenant_id == ctx.tenant_id)
        )
        ticket = res.scalar_one_or_none()
        if not ticket:
            raise RecursoNoEncontradoException("El ticket no fue encontrado.")

    cfdi_storage = get_cfdi_storage()
    zip_bytes = await cfdi_storage.get_cfdi_bundle(ctx.tenant_id, ticket_id)
    if not zip_bytes:
        raise CfdiExpiradoException()

    pdf_bytes = extract_single_file_from_zip(zip_bytes, ".pdf")
    if not pdf_bytes:
        raise RecursoNoEncontradoException("El archivo PDF no está disponible en este comprobante.")

    filename = f"cfdi-{ticket.cfdi_uuid or ticket_id}.pdf"
    return Response(
        content=pdf_bytes,
        media_type="application/pdf",
        headers={
            "Content-Disposition": f'inline; filename="{filename}"',
            "Cache-Control": "no-store, no-cache, must-revalidate",
        },
    )


@router.get("/{ticket_id}/cfdi/xml")
async def download_cfdi_xml(
    ticket_id: uuid.UUID,
    ctx: TenantContext = Depends(get_tenant_context),
):
    """
    Descarga directa del archivo XML del CFDI 4.0.
    """
    async with tenant_session(ctx.tenant_id, user_id=ctx.user.id) as session:
        res = await session.execute(
            select(Ticket).where(Ticket.id == ticket_id, Ticket.tenant_id == ctx.tenant_id)
        )
        ticket = res.scalar_one_or_none()
        if not ticket:
            raise RecursoNoEncontradoException("El ticket no fue encontrado.")

    cfdi_storage = get_cfdi_storage()
    zip_bytes = await cfdi_storage.get_cfdi_bundle(ctx.tenant_id, ticket_id)
    if not zip_bytes:
        raise CfdiExpiradoException()

    xml_bytes = extract_single_file_from_zip(zip_bytes, ".xml")
    if not xml_bytes:
        raise RecursoNoEncontradoException("El archivo XML no está disponible en este comprobante.")

    filename = f"cfdi-{ticket.cfdi_uuid or ticket_id}.xml"
    return Response(
        content=xml_bytes,
        media_type="application/xml",
        headers={
            "Content-Disposition": f'attachment; filename="{filename}"',
            "Cache-Control": "no-store, no-cache, must-revalidate",
        },
    )



@router.get("/{ticket_id}/handoff", status_code=status.HTTP_200_OK)
async def get_ticket_handoff_info(
    ticket_id: uuid.UUID,
    ctx: TenantContext = Depends(get_tenant_context),
):
    """
    Recupera los datos del handoff activo para el ticket si se encuentra en espera de resolución humana.
    """
    async with tenant_session(ctx.tenant_id, user_id=ctx.user.id) as session:
        res = await session.execute(
            select(Ticket).where(Ticket.id == ticket_id, Ticket.tenant_id == ctx.tenant_id)
        )
        ticket = res.scalar_one_or_none()
        if not ticket:
            raise RecursoNoEncontradoException("El ticket no fue encontrado.")

        ev_res = await session.execute(
            select(TicketEvent).where(
                TicketEvent.ticket_id == ticket_id,
                TicketEvent.tipo == "handoff",
                TicketEvent.tenant_id == ctx.tenant_id,
            ).order_by(TicketEvent.ts.desc()).limit(1)
        )
        ev = ev_res.scalar_one_or_none()
        if not ev or not ev.meta:
            raise RecursoNoEncontradoException("No hay sesión de handoff activa para este ticket.")

        expires_str = ev.meta.get("expires_at")
        is_expired = False
        if expires_str:
            try:
                exp_dt = datetime.fromisoformat(expires_str)
                if exp_dt <= datetime.now(timezone.utc):
                    is_expired = True
            except Exception:
                pass

        session_token = sign_session_token(ctx.user.id, ctx.user.email)

        return {
            "handoff_id": ev.meta.get("handoff_id"),
            "token": ev.meta.get("token"),
            "session_token": session_token,
            "motivo": ev.meta.get("motivo"),
            "expires_at": ev.meta.get("expires_at"),
            "url_facturacion": ticket.url_facturacion,
            "is_expired": is_expired or ticket.estado != TicketEstado.ESPERA_HUMANO,
        }


@router.get("/{ticket_id}/image")
async def get_ticket_image(
    ticket_id: uuid.UUID,
    ctx: TenantContext = Depends(get_tenant_context),
):
    """
    Descarga o visualiza la imagen del ticket cargada por el usuario (si aún no ha sido eliminada por privacidad).
    """
    async with tenant_session(ctx.tenant_id, user_id=ctx.user.id) as session:
        res = await session.execute(
            select(Ticket).where(Ticket.id == ticket_id, Ticket.tenant_id == ctx.tenant_id)
        )
        ticket = res.scalar_one_or_none()
        if not ticket:
            raise RecursoNoEncontradoException("Ticket no encontrado.")

        if not ticket.image_key or ticket.image_deleted_at:
            raise RecursoNoEncontradoException("La imagen del ticket no está disponible o ya fue eliminada por privacidad.")

    storage = get_storage_service()
    try:
        img_bytes = await storage.get_bytes(ticket.image_key)
    except Exception as exc:
        raise RecursoNoEncontradoException(f"No se pudo recuperar la imagen: {exc}")

    content_type = "image/jpeg"
    if img_bytes.startswith(b"%PDF"):
        content_type = "application/pdf"
    elif img_bytes.startswith(b"\x89PNG"):
        content_type = "image/png"
    elif img_bytes.startswith(b"RIFF") and len(img_bytes) > 12 and img_bytes[8:12] == b"WEBP":
        content_type = "image/webp"

    return Response(
        content=img_bytes,
        media_type=content_type,
        headers={"Cache-Control": "private, max-age=3600"},
    )


@router.delete("/{ticket_id}", status_code=status.HTTP_204_NO_CONTENT)
async def delete_ticket(
    ticket_id: uuid.UUID,
    ctx: TenantContext = Depends(get_tenant_context),
):
    """
    Elimina un ticket del tenant:
    - Borra la imagen de S3/MinIO si no ha sido borrada aún.
    - Borra los eventos asociados y el ticket de la base de datos (con RLS activo).
    """
    async with tenant_session(ctx.tenant_id, user_id=ctx.user.id) as session:
        res = await session.execute(
            select(Ticket).where(Ticket.id == ticket_id, Ticket.tenant_id == ctx.tenant_id)
        )
        ticket = res.scalar_one_or_none()
        if not ticket:
            raise RecursoNoEncontradoException("El ticket no fue encontrado.")

        storage = get_storage_service()

        # Borrar imagen en S3/MinIO si existe
        if ticket.image_key and not ticket.image_deleted_at:
            try:
                await storage.delete(ticket.image_key)
            except Exception:
                pass

        # Borrar capturas de depuración en S3 referenciadas en eventos
        events_res = await session.execute(
            select(TicketEvent).where(
                TicketEvent.ticket_id == ticket_id,
                TicketEvent.tenant_id == ctx.tenant_id,
            )
        )
        for ev in events_res.scalars().all():
            if ev.meta and isinstance(ev.meta, dict) and "screenshot_key" in ev.meta:
                try:
                    await storage.delete(ev.meta["screenshot_key"])
                except Exception:
                    pass

        # Borrar eventos del ticket explícitamente y el ticket
        await session.execute(
            delete(TicketEvent).where(
                TicketEvent.ticket_id == ticket_id,
                TicketEvent.tenant_id == ctx.tenant_id,
            )
        )
        await session.delete(ticket)

    return Response(status_code=status.HTTP_204_NO_CONTENT)
