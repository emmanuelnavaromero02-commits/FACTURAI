import asyncio
import json
import time
import uuid
from datetime import datetime
from decimal import Decimal
from typing import Any, Dict, List, Optional
from fastapi import APIRouter, Depends, File, Form, Header, Query, Response, UploadFile, status
from fastapi.responses import StreamingResponse
from pydantic import BaseModel
from sqlalchemy import delete, select

from ..db import tenant_session
from ..deps import TenantContext, get_tenant_context
from ..errors import (
    CfdiExpiradoException,
    MaximoIntentosExcedidoException,
    RecursoNoEncontradoException,
    ReintentoInvalidoException,
)
from ..models import FiscalProfile, Merchant, Ticket, TicketEstado, TicketEvent
from ..services.cfdi_storage import get_cfdi_storage
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
            created_at=t.created_at.isoformat(),
        )


class PaginatedTicketsResponse(BaseModel):
    items: List[TicketResponse]
    next_cursor: Optional[str] = None


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
    2. Sube en streaming a S3/MinIO sin cargar el archivo completo en memoria.
    3. Registra el ticket en estado 'recibido' dentro de tenant_session.
    4. Encola la tarea de extracción en ARQ con tenant_id explícito y responde 202.
    """
    ticket_id = uuid.uuid4()
    storage = get_storage_service()

    # Subida streaming a S3 con validación de magic bytes
    storage_key, file_size, mime_type = await process_and_stream_upload(
        file=file,
        tenant_id=ctx.tenant_id,
        ticket_id=ticket_id,
        storage=storage,
    )

    # Registro en base de datos bajo aislamiento RLS
    async with tenant_session(ctx.tenant_id, user_id=ctx.user.id) as session:
        ticket = Ticket(
            id=ticket_id,
            tenant_id=ctx.tenant_id,
            created_by=ctx.user.id,
            fiscal_profile_id=fiscal_profile_id,
            estado=TicketEstado.RECIBIDO,
            image_key=storage_key,
        )
        session.add(ticket)

        event = TicketEvent(
            tenant_id=ctx.tenant_id,
            ticket_id=ticket.id,
            tipo="recibido",
            mensaje="Ticket recibido exitosamente en el servidor.",
            meta={"file_size": file_size, "mime_type": mime_type},
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


@router.get("", response_model=PaginatedTicketsResponse)
async def list_tickets(
    estado: Optional[TicketEstado] = None,
    merchant_id: Optional[uuid.UUID] = None,
    cursor: Optional[datetime] = None,
    limit: int = Query(20, ge=1, le=100),
    ctx: TenantContext = Depends(get_tenant_context),
):
    """
    Lista tickets con filtros por estado y comercio, y paginación por cursor.
    """
    async with tenant_session(ctx.tenant_id, user_id=ctx.user.id) as session:
        query = select(Ticket).where(Ticket.tenant_id == ctx.tenant_id)

        if estado:
            query = query.where(Ticket.estado == estado)
        if merchant_id:
            query = query.where(Ticket.merchant_id == merchant_id)
        if cursor:
            query = query.where(Ticket.created_at < cursor)

        query = query.order_by(Ticket.created_at.desc(), Ticket.id.desc()).limit(limit + 1)
        res = await session.execute(query)
        tickets = res.scalars().all()

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

        if ticket.estado != TicketEstado.RECHAZADO:
            raise ReintentoInvalidoException(
                f"Solo se pueden reintentar tickets en estado 'rechazado'. Estado actual: '{ticket.estado.value}'."
            )

        es_error_datos = ticket.error_code in ("perfil_incompleto", "datos_no_coinciden")
        if not es_error_datos and ticket.intentos >= 3:
            raise MaximoIntentosExcedidoException(
                f"El ticket ha alcanzado el límite máximo de 3 intentos (intentos actuales: {ticket.intentos})."
            )

        await transition(
            session,
            ticket,
            TicketEstado.ENCOLADO,
            f"Reintento manual solicitado por usuario (intento previo {ticket.intentos}/3).",
            tipo="reintento",
        )
        ticket.error_code = None
        ticket.error_msg = None
        await session.flush()
        response_data = TicketResponse.from_model(ticket)

    # Encolar en la cola ARQ de facturación
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

        return {
            "handoff_id": ev.meta.get("handoff_id"),
            "token": ev.meta.get("token"),
            "motivo": ev.meta.get("motivo"),
            "expires_at": ev.meta.get("expires_at"),
            "url_facturacion": ticket.url_facturacion,
        }


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
