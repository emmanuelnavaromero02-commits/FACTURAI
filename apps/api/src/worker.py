import logging
import uuid
from decimal import Decimal
from typing import Optional
from sqlalchemy import select

from .db import sin_tenant, tenant_session
from .models import Merchant, Ticket, TicketEstado
from .state_machine import transition
from .storage import get_storage_service
from .vision.extractor import extract_qr_code, get_vision_extractor
from .vision.merchant_matcher import match_merchant_cascade
from .vision.normalizer import normalize_amount, normalize_date, normalize_time

logger = logging.getLogger(__name__)


async def process_ticket_extraction(
    tenant_id: uuid.UUID,
    ticket_id: uuid.UUID,
    explicit_merchant_slug: Optional[str] = None,
) -> None:
    """
    Job de extracción con visión para ARQ (Regla 1 y Trampa 1 evitadas):
    - El payload recibe 'tenant_id' explícito.
    - Abre tenant_session(tenant_id) ANTES de cualquier consulta.
    - Si el tenant no corresponde, la consulta devuelve cero filas y el job falla limpio.
    """
    storage = get_storage_service()
    vision = get_vision_extractor()

    # 1. Fase inicial: comprobar existencia y cambiar a EXTRAYENDO
    image_key: Optional[str] = None
    async with tenant_session(tenant_id) as session:
        result = await session.execute(
            select(Ticket).where(Ticket.id == ticket_id, Ticket.tenant_id == tenant_id)
        )
        ticket = result.scalar_one_or_none()

        if ticket is None:
            logger.warning(
                "Job de extracción abortado: ticket %s no encontrado para tenant %s",
                ticket_id,
                tenant_id,
            )
            return

        if not ticket.image_key:
            ticket.error_code = "imagen_no_encontrada"
            ticket.error_msg = "No se localizó la imagen del ticket en el almacenamiento."
            await transition(
                session,
                ticket,
                TicketEstado.RECHAZADO,
                "Error crítico: falta la clave de imagen del ticket.",
                meta={"error_code": "imagen_no_encontrada"},
            )
            return

        image_key = ticket.image_key
        await transition(
            session,
            ticket,
            TicketEstado.EXTRAYENDO,
            "Iniciando análisis de imagen con visión artificial.",
        )

    # 2. Descargar imagen efímera desde almacenamiento (fuera de sesión DB)
    try:
        image_bytes = await storage.get_bytes(image_key)
    except Exception as exc:
        async with tenant_session(tenant_id) as session:
            res = await session.execute(
                select(Ticket).where(Ticket.id == ticket_id, Ticket.tenant_id == tenant_id)
            )
            t = res.scalar_one()
            t.error_code = "error_almacenamiento"
            t.error_msg = f"No se pudo descargar la imagen del ticket: {str(exc)}"
            await transition(
                session,
                t,
                TicketEstado.RECHAZADO,
                "Fallo al descargar la imagen desde el almacenamiento efímero.",
                meta={"error_code": "error_almacenamiento"},
            )
        return

    # 3. Intentar decodificar código QR
    qr_url = extract_qr_code(image_bytes)

    # 4. Extracción con el modelo de visión (con reintento de 1 vez si falla)
    extracted = None
    for intento in range(2):
        try:
            extracted = await vision.extract(image_bytes)
            break
        except Exception as exc:
            if intento == 1:
                async with tenant_session(tenant_id) as session:
                    res = await session.execute(
                        select(Ticket).where(Ticket.id == ticket_id, Ticket.tenant_id == tenant_id)
                    )
                    t = res.scalar_one()
                    t.error_code = "error_vision"
                    t.error_msg = "El modelo de visión no pudo interpretar el formato del ticket."
                    await transition(
                        session,
                        t,
                        TicketEstado.RECHAZADO,
                        "Fallo definitivo al procesar con visión tras reintento.",
                        meta={"error": str(exc)},
                    )
                return

    if extracted is None:
        return

    # 5. Normalización de montos y fechas
    total_norm = normalize_amount(extracted.total)
    subtotal_norm = normalize_amount(extracted.subtotal)
    iva_norm = normalize_amount(extracted.iva)
    fecha_norm = normalize_date(extracted.fecha)
    hora_norm = normalize_time(extracted.hora)

    # 6. Regla 5 del worker: verificar legibilidad y completitud
    es_ilegible = (
        extracted.confianza < 0.6
        or not extracted.folio
        or total_norm is None
    )

    # 7. Identificación del comercio en cascada (si no es ilegible)
    matched_merchant = None
    if not es_ilegible:
        async with sin_tenant() as global_session:
            merchants_res = await global_session.execute(
                select(Merchant).where(Merchant.activo == True)
            )
            active_merchants = merchants_res.scalars().all()

        matched_merchant = match_merchant_cascade(
            merchants=active_merchants,
            explicit_slug=explicit_merchant_slug,
            qr_url=qr_url,
            printed_url=extracted.url_facturacion,
            rfc_emisor=extracted.rfc_emisor,
            comercio_nombre=extracted.comercio,
        )

    # 8. Persistir resultados de extracción bajo tenant_session
    async with tenant_session(tenant_id) as session:
        res = await session.execute(
            select(Ticket).where(Ticket.id == ticket_id, Ticket.tenant_id == tenant_id)
        )
        ticket = res.scalar_one()

        if es_ilegible:
            ticket.error_code = "imagen_ilegible"
            ticket.error_msg = (
                "La foto no se pudo leer claramente. Toma la foto con más luz, "
                "asegúrate de que el ticket esté completo y sin dobleces."
            )
            await transition(
                session,
                ticket,
                TicketEstado.RECHAZADO,
                "Ticket rechazado: imagen ilegible o faltan datos mínimos (folio o total).",
                meta={
                    "error_code": "imagen_ilegible",
                    "confianza": extracted.confianza,
                    "tiene_folio": bool(extracted.folio),
                    "tiene_total": total_norm is not None,
                },
            )
            return

        ticket.folio = extracted.folio.strip() if extracted.folio else None
        ticket.web_id = extracted.web_id.strip() if extracted.web_id else None
        ticket.fecha_ticket = fecha_norm
        ticket.hora_ticket = hora_norm
        ticket.total = total_norm
        ticket.subtotal = subtotal_norm
        ticket.iva = iva_norm
        ticket.sucursal = extracted.sucursal
        ticket.caja = extracted.caja
        ticket.transaccion = extracted.transaccion
        ticket.rfc_emisor = extracted.rfc_emisor
        ticket.confianza = Decimal(str(round(extracted.confianza, 2)))
        ticket.extracted = extracted.model_dump()

        if matched_merchant is None:
            # Comercio desconocido: queda en espera de que el usuario lo elija
            ticket.error_code = "comercio_desconocido"
            ticket.error_msg = (
                "No pudimos identificar la cadena o portal del ticket automáticamente. "
                "Por favor selecciona el comercio en la lista para continuar."
            )
            await transition(
                session,
                ticket,
                TicketEstado.EXTRAIDO,
                "Datos extraídos; comercio no identificado, esperando selección manual.",
                meta={"error_code": "comercio_desconocido"},
            )
        else:
            ticket.merchant_id = matched_merchant.id
            ticket.error_code = None
            ticket.error_msg = None
            await transition(
                session,
                ticket,
                TicketEstado.EXTRAIDO,
                f"Datos extraídos correctamente. Comercio identificado: {matched_merchant.nombre}.",
                meta={"merchant_id": str(matched_merchant.id), "merchant_slug": matched_merchant.slug},
            )


# ---------------------------------------------------------------------------
# Configuración del Worker ARQ
# ---------------------------------------------------------------------------
async def extract_ticket_task(ctx, tenant_id: str, ticket_id: str, explicit_merchant_slug: Optional[str] = None):
    """Tarea asíncrona registrada en ARQ."""
    await process_ticket_extraction(
        tenant_id=uuid.UUID(tenant_id),
        ticket_id=uuid.UUID(ticket_id),
        explicit_merchant_slug=explicit_merchant_slug,
    )


class WorkerSettings:
    functions = [extract_ticket_task]
    redis_settings = None  # Se configura con REDIS_URL en ejecución
