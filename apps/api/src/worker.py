import hashlib
import json
import logging
import uuid
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from typing import Optional
from sqlalchemy import select, text
from sqlalchemy.exc import IntegrityError

from .config import get_settings
from .db import engine, sin_tenant, tenant_session
from .engines import EngineContext, EngineResult, get_engine
from .models import (
    FiscalProfile,
    Merchant,
    MerchantCredential,
    Ticket,
    TicketEstado,
    TicketEvent,
)
from .security import decrypt_credentials
from .services.cfdi_storage import get_cfdi_storage
from .state_machine import transition
from .storage import get_storage_service
from .vision.extractor import enhance_receipt_image, extract_qr_code, get_vision_extractor
from .vision.merchant_matcher import match_merchant_cascade
from .vision.normalizer import normalize_amount, normalize_date, normalize_time
from .vision.url_sanitizer import sanitize_and_classify_billing_url

logger = logging.getLogger(__name__)


def compute_advisory_lock_key(tenant_id: uuid.UUID, merchant_id: Optional[uuid.UUID], folio: str) -> int:
    """Calcula una clave entera de 64 bits con signo para pg_try_advisory_lock."""
    m_part = str(merchant_id) if merchant_id else "generic"
    raw = f"{tenant_id}:{m_part}:{folio.strip().upper()}".encode("utf-8")
    return int.from_bytes(hashlib.sha256(raw).digest()[:8], byteorder="big", signed=True)


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
    settings = get_settings()
    vision = get_vision_extractor(model=settings.ANTHROPIC_MODEL_VISION)

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

    # 4. Extracción inicial con el modelo de visión ANTHROPIC_MODEL_VISION (reintento de 1 vez ante error de red/parseo)
    extracted = None
    for intento in range(2):
        try:
            extracted = await vision.extract(image_bytes, model=settings.ANTHROPIC_MODEL_VISION)
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

    # Escalamiento al modelo agente si la confianza resulta bajo el umbral (< 0.6)
    if extracted.confianza < 0.6:
        async with tenant_session(tenant_id) as session:
            ev_escalamiento = TicketEvent(
                tenant_id=tenant_id,
                ticket_id=ticket_id,
                tipo="escalamiento_modelo",
                mensaje=(
                    f"Confianza de extracción baja ({extracted.confianza * 100:.1f}%). "
                    f"Reintentando extracción con modelo agente {settings.ANTHROPIC_MODEL_AGENTE}."
                ),
                meta={
                    "confianza_previa": float(extracted.confianza),
                    "modelo_origen": settings.ANTHROPIC_MODEL_VISION,
                    "modelo_destino": settings.ANTHROPIC_MODEL_AGENTE,
                },
            )
            session.add(ev_escalamiento)

        try:
            enhanced_bytes = enhance_receipt_image(image_bytes)
            vision_agente = get_vision_extractor(model=settings.ANTHROPIC_MODEL_AGENTE)
            escalated_extracted = await vision_agente.extract(enhanced_bytes, model=settings.ANTHROPIC_MODEL_AGENTE)
            if escalated_extracted is not None:
                extracted = escalated_extracted
                async with tenant_session(tenant_id) as session:
                    ev_resultado = TicketEvent(
                        tenant_id=tenant_id,
                        ticket_id=ticket_id,
                        tipo="escalamiento_resultado",
                        mensaje=(
                            f"Extracción con modelo agente {settings.ANTHROPIC_MODEL_AGENTE} completada "
                            f"(confianza: {extracted.confianza * 100:.1f}%)."
                        ),
                        meta={
                            "confianza_nueva": float(extracted.confianza),
                            "modelo": settings.ANTHROPIC_MODEL_AGENTE,
                        },
                    )
                    session.add(ev_resultado)
        except Exception as exc:
            logger.warning("Fallo al reintentar extracción con modelo agente %s: %s", settings.ANTHROPIC_MODEL_AGENTE, exc)

    # Recuperación autónoma de folio si no viene explícito pero existen identificadores alternativos (web_id, transacción, otros)
    if not extracted.folio:
        candidate_folio = None
        if extracted.web_id:
            candidate_folio = extracted.web_id
        elif extracted.transaccion:
            candidate_folio = extracted.transaccion
        elif extracted.otros:
            for item in extracted.otros:
                k = getattr(item, "etiqueta", "") if hasattr(item, "etiqueta") else str(item.get("etiqueta", ""))
                v = getattr(item, "valor", "") if hasattr(item, "valor") else str(item.get("valor", ""))
                k_lower = k.lower()
                if any(kw in k_lower for kw in ("ticket", "folio", "orden", "trans", "docto", "ref", "operaci", "control", "código", "codigo", "venta")):
                    if len(str(v).strip()) >= 2:
                        candidate_folio = str(v).strip()
                        break
        if candidate_folio:
            extracted.folio = candidate_folio
            logger.info("Folio de ticket recuperado desde campo alternativo: %s", candidate_folio)

    # 5. Normalización de montos y fechas
    total_norm = normalize_amount(extracted.total)
    subtotal_norm = normalize_amount(extracted.subtotal)
    iva_norm = normalize_amount(extracted.iva)
    fecha_norm = normalize_date(extracted.fecha)
    hora_norm = normalize_time(extracted.hora)

    # Validación estricta anti-malinterpretación para tickets arrugados o borrosos:
    # Si el folio contiene signos de interrogación, asteriscos, puntos suspensivos o longitud inválida
    folio_dudoso = False
    if extracted.folio:
        cleaned_f = extracted.folio.strip()
        if any(c in cleaned_f for c in ("?", "*", "...", "xxx", "XXX")) or len(cleaned_f) < 2:
            folio_dudoso = True

    # 6. Regla 5 del worker: verificar legibilidad y completitud sin malinterpretar
    es_ilegible = (
        extracted.confianza < 0.6
        or not extracted.folio
        or folio_dudoso
        or total_norm is None
        or (total_norm is not None and total_norm <= 0)
    )

    # 7. Limpieza de URLs y discriminación de QR del SAT
    sanitized_qr = sanitize_and_classify_billing_url(qr_url)
    sanitized_printed = sanitize_and_classify_billing_url(extracted.url_facturacion) if extracted else None
    billing_url = sanitized_qr or sanitized_printed

    # 8. Identificación del comercio en cascada (si no es ilegible)
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
            qr_url=sanitized_qr,
            printed_url=sanitized_printed,
            rfc_emisor=extracted.rfc_emisor,
            comercio_nombre=extracted.comercio,
        )

    # 9. Persistir resultados de extracción bajo tenant_session
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
        ticket.url_facturacion = billing_url

        auto_enqueue = False
        if matched_merchant is not None or billing_url:
            fp_check = await session.execute(
                select(FiscalProfile.id).where(
                    FiscalProfile.tenant_id == tenant_id,
                    FiscalProfile.es_principal == True,
                )
            )
            auto_enqueue = fp_check.scalar_one_or_none() is not None

        if matched_merchant is None:
            if billing_url:
                # No hay comercio específico pero sí URL de facturación: listo para generico-web
                ticket.merchant_id = None
                ticket.error_code = None
                ticket.error_msg = None
                await transition(
                    session,
                    ticket,
                    TicketEstado.EXTRAIDO,
                    f"Datos extraídos correctamente. URL de facturación detectada para motor genérico web.",
                    meta={"url_facturacion": billing_url, "motor": "generico-web"},
                )
                if auto_enqueue:
                    await transition(
                        session,
                        ticket,
                        TicketEstado.ENCOLADO,
                        "Ticket encolado automáticamente para facturación con motor genérico web.",
                        tipo="encolado",
                    )
            else:
                # Comercio desconocido y sin URL: queda en espera de que el usuario lo elija
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
            if matched_merchant.config and isinstance(matched_merchant.config, dict) and matched_merchant.config.get("url_facturacion"):
                ticket.url_facturacion = matched_merchant.config["url_facturacion"]
            ticket.error_code = None
            ticket.error_msg = None
            await transition(
                session,
                ticket,
                TicketEstado.EXTRAIDO,
                f"Datos extraídos correctamente. Comercio identificado: {matched_merchant.nombre}.",
                meta={"merchant_id": str(matched_merchant.id), "merchant_slug": matched_merchant.slug, "url_facturacion": ticket.url_facturacion},
            )
            if auto_enqueue:
                await transition(
                    session,
                    ticket,
                    TicketEstado.ENCOLADO,
                    f"Ticket encolado automáticamente para facturación con {matched_merchant.nombre}.",
                    tipo="encolado",
                )

    if auto_enqueue:
        from .services.queue import enqueue_ticket_facturacion
        await enqueue_ticket_facturacion(tenant_id, ticket_id)


# ---------------------------------------------------------------------------
# Worker de Facturación (Fase 4)
# ---------------------------------------------------------------------------
async def process_ticket_facturacion(
    tenant_id: uuid.UUID,
    ticket_id: uuid.UUID,
) -> None:
    """
    Worker principal de facturación de tickets (Fase 4):
    1. Candado 1 (Mismo ticket, dos workers): UPDATE condicional atómico 'encolado' -> 'facturando'.
    2. Candado 2 (Distinto ticket, mismo folio): Advisory Lock de sesión sobre (tenant_id, merchant_id, folio).
    3. Verificación de duplicados bajo lock antes de invocar el motor.
    4. Ejecución del motor correspondiente del registro desacoplado.
    5. Borrado obligatorio de imagen en bloque finally antes de cerrar el ticket.
    6. Transición a FACTURADO y envío SMTP sin persistencia de PDF ni XML.
    7. Manejo de reintentos con backoff (30s, 5m, 30m) para fallas transitorias.
    """
    storage = get_storage_service()

    # Candado 1: Mismo ticket, dos workers concurrentes (reintento duplicado en la cola)
    async with tenant_session(tenant_id) as session:
        res = await session.execute(
            text(
                "UPDATE tickets SET estado = 'facturando', intentos = intentos + 1 "
                "WHERE id = :t_id AND tenant_id = :tenant_id AND estado = 'encolado' "
                "RETURNING id, merchant_id, fiscal_profile_id, folio, web_id, total, image_key, intentos, url_facturacion"
            ),
            {"t_id": ticket_id, "tenant_id": tenant_id},
        )
        row = res.first()
        if not row:
            logger.info("Ticket %s no está encolado o ya fue tomado por otro worker. Abortando sin error.", ticket_id)
            return

        merchant_id = row.merchant_id
        fiscal_profile_id = row.fiscal_profile_id
        folio = row.folio
        image_key = row.image_key
        intentos = row.intentos
        url_facturacion = row.url_facturacion

        event = TicketEvent(
            tenant_id=tenant_id,
            ticket_id=ticket_id,
            tipo="cambio_estado",
            mensaje="Iniciando proceso de facturación con el portal del comercio.",
            meta={"estado_anterior": "encolado", "estado_nuevo": "facturando", "intento": intentos},
        )
        session.add(event)

    # Candado 2: Distinto ticket, mismo folio (dos subidas del mismo ticket de compra)
    lock_conn = None
    lock_acquired = False
    lock_key = None

    if folio:
        lock_key = compute_advisory_lock_key(tenant_id, merchant_id, folio)
        lock_conn = await engine.connect()
        try:
            res_lock = await lock_conn.execute(
                text("SELECT pg_try_advisory_lock(:k)"), {"k": lock_key}
            )
            lock_acquired = bool(res_lock.scalar())
        except Exception as lock_err:
            logger.error("Error al intentar adquirir advisory lock: %s", lock_err)
            lock_acquired = False

        if not lock_acquired:
            logger.warning("Ticket %s ya está siendo facturado por otro worker en paralelo. Re-encolando con retraso.", ticket_id)
            async with tenant_session(tenant_id) as session:
                await session.execute(
                    text("UPDATE tickets SET estado = 'encolado' WHERE id = :t_id AND tenant_id = :tenant_id"),
                    {"t_id": ticket_id, "tenant_id": tenant_id},
                )
            if lock_conn:
                await lock_conn.close()
            from .services.queue import enqueue_ticket_facturacion
            await enqueue_ticket_facturacion(tenant_id, ticket_id, defer_seconds=10)
            return

    try:
        # Verificación confiable bajo lock: ¿Existe ya una factura previa para este comercio y folio?
        if folio:
            async with tenant_session(tenant_id) as session:
                dup_query = select(Ticket.id).where(
                    Ticket.tenant_id == tenant_id,
                    Ticket.folio == folio,
                    Ticket.estado == TicketEstado.FACTURADO,
                    Ticket.id != ticket_id,
                )
                if merchant_id:
                    dup_query = dup_query.where(Ticket.merchant_id == merchant_id)
                else:
                    dup_query = dup_query.where(Ticket.merchant_id.is_(None))
                dup_res = await session.execute(dup_query)
                if dup_res.first():
                    logger.info("Ticket %s ya cuenta con factura previa. Rechazando como duplicado.", ticket_id)
                    t_res = await session.execute(
                        select(Ticket).where(Ticket.id == ticket_id, Ticket.tenant_id == tenant_id)
                    )
                    t = t_res.scalar_one()
                    t.error_code = "duplicado"
                    t.error_msg = f"El folio {folio} ya fue facturado previamente para este comercio."
                    if t.image_key and not t.image_deleted_at:
                        try:
                            await storage.delete(t.image_key)
                            t.image_deleted_at = datetime.now(timezone.utc)
                        except Exception:
                            pass
                    await transition(
                        session,
                        t,
                        TicketEstado.RECHAZADO,
                        f"Facturación rechazada: el folio {folio} ya fue facturado previamente.",
                        meta={"error_code": "duplicado"},
                    )
                    return

        # Resolver perfil fiscal
        async with tenant_session(tenant_id) as session:
            if fiscal_profile_id:
                fp_res = await session.execute(
                    select(FiscalProfile).where(
                        FiscalProfile.id == fiscal_profile_id,
                        FiscalProfile.tenant_id == tenant_id,
                    )
                )
                perfil_fiscal = fp_res.scalar_one_or_none()
            else:
                fp_res = await session.execute(
                    select(FiscalProfile).where(
                        FiscalProfile.tenant_id == tenant_id,
                        FiscalProfile.es_principal == True,
                    )
                )
                perfil_fiscal = fp_res.scalar_one_or_none()

        if not perfil_fiscal:
            async with tenant_session(tenant_id) as session:
                t_res = await session.execute(select(Ticket).where(Ticket.id == ticket_id))
                t = t_res.scalar_one()
                t.intentos = max(0, t.intentos - 1)  # No gasta intentos
                t.error_code = "perfil_incompleto"
                t.error_msg = "No se localizó un perfil fiscal válido para este ticket."
                if t.image_key and not t.image_deleted_at:
                    try:
                        await storage.delete(t.image_key)
                        t.image_deleted_at = datetime.now(timezone.utc)
                    except Exception:
                        pass
                await transition(
                    session,
                    t,
                    TicketEstado.RECHAZADO,
                    "Faltan datos fiscales del receptor (perfil fiscal no encontrado).",
                    meta={"error_code": "perfil_incompleto"},
                )
            return

        # Resolver comercio
        merchant = None
        if merchant_id:
            async with sin_tenant() as global_session:
                m_res = await global_session.execute(select(Merchant).where(Merchant.id == merchant_id))
                merchant = m_res.scalar_one_or_none()

            if not merchant:
                async with tenant_session(tenant_id) as session:
                    t_res = await session.execute(select(Ticket).where(Ticket.id == ticket_id))
                    t = t_res.scalar_one()
                    t.error_code = "comercio_desconocido"
                    t.error_msg = "El comercio asignado no existe en el catálogo."
                    if t.image_key and not t.image_deleted_at:
                        try:
                            await storage.delete(t.image_key)
                            t.image_deleted_at = datetime.now(timezone.utc)
                        except Exception:
                            pass
                    await transition(session, t, TicketEstado.RECHAZADO, "Comercio no encontrado en el catálogo.")
                return
        else:
            if not url_facturacion:
                async with tenant_session(tenant_id) as session:
                    t_res = await session.execute(select(Ticket).where(Ticket.id == ticket_id))
                    t = t_res.scalar_one()
                    t.error_code = "comercio_desconocido"
                    t.error_msg = "El ticket no tiene asignado ningún comercio del catálogo ni URL de facturación."
                    if t.image_key and not t.image_deleted_at:
                        try:
                            await storage.delete(t.image_key)
                            t.image_deleted_at = datetime.now(timezone.utc)
                        except Exception:
                            pass
                    await transition(session, t, TicketEstado.RECHAZADO, "Comercio no asignado y sin URL de facturación.")
                return

        # Validación previa de entrega por emisor (Corrección B):
        # Si el comercio entrega por emisor y no hay email configurado, el motor NO se ejecuta
        if merchant and merchant.entrega_esperada == "emisor" and not (perfil_fiscal.email_receptor and perfil_fiscal.email_receptor.strip()):
            async with tenant_session(tenant_id) as session:
                t_res = await session.execute(select(Ticket).where(Ticket.id == ticket_id))
                t = t_res.scalar_one()
                t.intentos = max(0, t.intentos - 1)  # Corrección C: No gasta intentos
                t.error_code = "perfil_incompleto"
                t.error_msg = f"El comercio {merchant.nombre} envía la factura por correo, pero tu perfil fiscal no tiene correo configurado."
                if t.image_key and not t.image_deleted_at:
                    try:
                        await storage.delete(t.image_key)
                        t.image_deleted_at = datetime.now(timezone.utc)
                    except Exception:
                        pass
                await transition(
                    session,
                    t,
                    TicketEstado.RECHAZADO,
                    "Facturación detenida antes de iniciar: falta el correo del receptor en el perfil fiscal.",
                    meta={"error_code": "perfil_incompleto", "campo_faltante": "email_receptor"},
                )
            return

        # Descifrar credenciales si existen
        credenciales = None
        if merchant_id:
            async with tenant_session(tenant_id) as session:
                c_res = await session.execute(
                    select(MerchantCredential).where(
                        MerchantCredential.tenant_id == tenant_id,
                        MerchantCredential.merchant_id == merchant_id,
                    )
                )
                cred = c_res.scalar_one_or_none()
                if cred:
                    try:
                        plain_bytes = decrypt_credentials(tenant_id, cred.payload_enc, cred.nonce)
                        credenciales = json.loads(plain_bytes.decode("utf-8"))
                    except Exception as dec_err:
                        logger.error("Error descifrando credenciales para merchant %s: %s", merchant_id, dec_err)

        # Resolver motor registrado (si no hay comercio, se usa generico-web por defecto)
        engine_slug = merchant.engine_slug if merchant else "generico-web"
        engine_instance = get_engine(engine_slug)
        if not engine_instance and merchant:
            engine_instance = get_engine("mock")

        if not engine_instance:
            async with tenant_session(tenant_id) as session:
                t_res = await session.execute(select(Ticket).where(Ticket.id == ticket_id))
                t = t_res.scalar_one()
                t.error_code = "motor_no_disponible"
                t.error_msg = f"No existe motor configurado para {engine_slug}."
                if t.image_key and not t.image_deleted_at:
                    try:
                        await storage.delete(t.image_key)
                        t.image_deleted_at = datetime.now(timezone.utc)
                    except Exception:
                        pass
                await transition(session, t, TicketEstado.RECHAZADO, "Motor de facturación no disponible.")
            return

        # Ejecutar motor
        result: Optional[EngineResult] = None
        engine_exception = None

        async with tenant_session(tenant_id) as session:
            t_res = await session.execute(select(Ticket).where(Ticket.id == ticket_id))
            ticket_obj = t_res.scalar_one()
            from .services.handoff_service import RealHandoffInterface

            ctx = EngineContext(
                ticket=ticket_obj,
                perfil_fiscal=perfil_fiscal,
                merchant=merchant,
                credenciales=credenciales,
                handoff=RealHandoffInterface(),
                _session=session,
                _tenant_id=tenant_id,
            )
            try:
                result = await engine_instance.facturar(ctx)
            except Exception as exc:
                engine_exception = exc
                m_slug = merchant.slug if merchant else "generico-web"
                logger.error("Excepción durante ejecución de motor %s para ticket %s: %s", m_slug, ticket_id, exc)

        # Fallo por excepción no controlada en el motor
        if engine_exception is not None or result is None:
            async with tenant_session(tenant_id) as session:
                t_res = await session.execute(select(Ticket).where(Ticket.id == ticket_id))
                t = t_res.scalar_one()
                t.error_code = "error_motor"
                t.error_msg = f"Excepción en motor de facturación: {str(engine_exception)}"
                # Borrado obligatorio de imagen en error crítico
                if t.image_key and not t.image_deleted_at:
                    try:
                        await storage.delete(t.image_key)
                        t.image_deleted_at = datetime.now(timezone.utc)
                    except Exception:
                        pass
                await transition(
                    session,
                    t,
                    TicketEstado.RECHAZADO,
                    f"Fallo crítico durante ejecución del motor: {str(engine_exception)}",
                    meta={"error": str(engine_exception)},
                )
            return

        # Resultado exitoso
        if result.ok:
            is_duplicate = False
            async with tenant_session(tenant_id) as session:
                t_res = await session.execute(select(Ticket).where(Ticket.id == ticket_id))
                t = t_res.scalar_one()
                t.cfdi_uuid = result.cfdi_uuid
                t.facturado_at = datetime.now(timezone.utc)
                t.costo_total_usd = result.costo_usd
                t.tokens_input_total = result.tokens_input
                t.tokens_output_total = result.tokens_output
                t.pasos_agente = result.pasos
                t.duracion_segundos = result.duracion_segundos

                # Regla 3: La imagen se borra antes de la transición a estado final
                if t.image_key and not t.image_deleted_at:
                    try:
                        await storage.delete(t.image_key)
                        t.image_deleted_at = datetime.now(timezone.utc)
                    except Exception as del_err:
                        logger.warning("Error borrando imagen de S3 para ticket facturado: %s", del_err)

                # Manejo según vía de entrega (Corrección A y B)
                tiene_archivos = bool(result.pdf or result.xml)
                if tiene_archivos or result.entrega == "descarga":
                    if tiene_archivos:
                        cfdi_storage = get_cfdi_storage()
                        await cfdi_storage.save_cfdi_bundle(
                            tenant_id=tenant_id,
                            ticket_id=ticket_id,
                            cfdi_uuid=result.cfdi_uuid,
                            pdf_bytes=result.pdf,
                            xml_bytes=result.xml,
                            ttl_seconds=1800,  # 30 minutos
                        )
                        t.cfdi_disponible_hasta = datetime.now(timezone.utc) + timedelta(minutes=30)
                        event_descarga = TicketEvent(
                            tenant_id=tenant_id,
                            ticket_id=ticket_id,
                            tipo="cfdi_listo_descarga",
                            mensaje="Comprobante fiscal listo para descarga inmediata en FacturAI (PDF/XML).",
                            meta={"cfdi_disponible_hasta": t.cfdi_disponible_hasta.isoformat(), "max_descargas": 5},
                        )
                        session.add(event_descarga)

                if result.entrega == "emisor" or not tiene_archivos:
                    t.correo_capturado_en_portal = result.correo_capturado or perfil_fiscal.email_receptor
                    event_emisor = TicketEvent(
                        tenant_id=tenant_id,
                        ticket_id=ticket_id,
                        tipo="cfdi_enviado_por_emisor",
                        mensaje=f"Comprobante fiscal timbrado. El portal del comercio lo enviará a {t.correo_capturado_en_portal}.",
                        meta={"cfdi_uuid": result.cfdi_uuid, "correo": t.correo_capturado_en_portal},
                    )
                    session.add(event_emisor)

                elif result.entrega == "ninguna":
                    event_ninguna = TicketEvent(
                        tenant_id=tenant_id,
                        ticket_id=ticket_id,
                        tipo="cfdi_timbrado_sin_entrega",
                        mensaje=f"Comprobante fiscal timbrado ante el SAT (UUID: {result.cfdi_uuid}). El portal no ofreció descarga ni confirmó envío.",
                        meta={"cfdi_uuid": result.cfdi_uuid},
                    )
                    session.add(event_ninguna)

                # Verificación de divergencia de entrega esperada vs obtenida (Corrección B)
                if merchant and result.entrega != merchant.entrega_esperada:
                    event_divergencia = TicketEvent(
                        tenant_id=tenant_id,
                        ticket_id=ticket_id,
                        tipo="aviso_entrega_inesperada",
                        mensaje=(
                            f"Vía de entrega inesperada: se esperaba '{merchant.entrega_esperada}' "
                            f"pero el motor reportó '{result.entrega}'."
                        ),
                        meta={
                            "entrega_obtenida": result.entrega,
                            "entrega_esperada": merchant.entrega_esperada,
                        },
                    )
                    session.add(event_divergencia)

                # Intentar transición a FACTURADO capturando colisión con índice único parcial
                try:
                    await transition(
                        session,
                        t,
                        TicketEstado.FACTURADO,
                        f"Facturación completada exitosamente. Folio fiscal: {result.cfdi_uuid}.",
                        meta={"cfdi_uuid": result.cfdi_uuid, "entrega": result.entrega},
                    )
                except IntegrityError:
                    is_duplicate = True
                    t.error_code = "duplicado"
                    t.error_msg = "El ticket ya fue registrado como facturado previamente por otro worker."
                    await transition(
                        session,
                        t,
                        TicketEstado.RECHAZADO,
                        "Rechazado por colisión única con factura previa.",
                        meta={"error_code": "duplicado"},
                    )

            return

        # Resultado fallido del motor
        if not result.ok:
            es_error_datos = result.error_code in ("perfil_incompleto", "datos_no_coinciden")
            if es_error_datos:
                # Corrección C y Regla 10: perfil_incompleto y datos_no_coinciden no gastan intentos
                async with tenant_session(tenant_id) as session:
                    t_res = await session.execute(select(Ticket).where(Ticket.id == ticket_id))
                    t = t_res.scalar_one()
                    t.intentos = max(0, t.intentos - 1)
                    t.error_code = result.error_code
                    t.error_msg = result.mensaje
                    t.costo_total_usd = result.costo_usd
                    t.tokens_input_total = result.tokens_input
                    t.tokens_output_total = result.tokens_output
                    t.pasos_agente = result.pasos
                    t.duracion_segundos = result.duracion_segundos
                    if t.image_key and not t.image_deleted_at:
                        try:
                            await storage.delete(t.image_key)
                            t.image_deleted_at = datetime.now(timezone.utc)
                        except Exception:
                            pass
                    await transition(
                        session,
                        t,
                        TicketEstado.RECHAZADO,
                        f"Facturación rechazada ({result.error_code}): {result.mensaje}. Reintentable tras corregir los datos.",
                        meta={"error_code": result.error_code, "intentos": t.intentos},
                    )
                return

            backoffs = [30, 300, 1800]
            if result.reintentable and (intentos < 3 or result.error_code == "handoff_tope_concurrencia"):
                backoff = 10 if result.error_code == "handoff_tope_concurrencia" else backoffs[min(intentos - 1, len(backoffs) - 1)]
                async with tenant_session(tenant_id) as session:
                    t_res = await session.execute(select(Ticket).where(Ticket.id == ticket_id))
                    t = t_res.scalar_one()
                    if result.error_code == "handoff_tope_concurrencia":
                        t.intentos = max(0, t.intentos - 1)
                    t.error_code = result.error_code
                    t.error_msg = result.mensaje
                    t.costo_total_usd = result.costo_usd
                    t.tokens_input_total = result.tokens_input
                    t.tokens_output_total = result.tokens_output
                    t.pasos_agente = result.pasos
                    t.duracion_segundos = result.duracion_segundos
                    await transition(
                        session,
                        t,
                        TicketEstado.ENCOLADO,
                        f"Fallo transitorio ({result.error_code}): {result.mensaje}. Reintentando en {backoff} s.",
                        meta={"error_code": result.error_code, "intento": t.intentos, "backoff": backoff},
                    )
                from .services.queue import enqueue_ticket_facturacion
                await enqueue_ticket_facturacion(tenant_id, ticket_id, defer_seconds=backoff)
            else:
                async with tenant_session(tenant_id) as session:
                    t_res = await session.execute(select(Ticket).where(Ticket.id == ticket_id))
                    t = t_res.scalar_one()
                    t.error_code = result.error_code
                    t.error_msg = result.mensaje
                    t.costo_total_usd = result.costo_usd
                    t.tokens_input_total = result.tokens_input
                    t.tokens_output_total = result.tokens_output
                    t.pasos_agente = result.pasos
                    t.duracion_segundos = result.duracion_segundos
                    if t.image_key and not t.image_deleted_at:
                        try:
                            await storage.delete(t.image_key)
                            t.image_deleted_at = datetime.now(timezone.utc)
                        except Exception:
                            pass
                    await transition(
                        session,
                        t,
                        TicketEstado.RECHAZADO,
                        f"Facturación rechazada definitivamente: {result.mensaje or result.error_code}",
                        meta={"error_code": result.error_code, "intentos": intentos},
                    )
            return

    except Exception as unhandled_err:
        logger.error("Error no manejado en process_ticket_facturacion para ticket %s: %s", ticket_id, unhandled_err)
        try:
            async with tenant_session(tenant_id) as session:
                t_res = await session.execute(select(Ticket).where(Ticket.id == ticket_id))
                t = t_res.scalar_one_or_none()
                if t and t.image_key and not t.image_deleted_at:
                    await storage.delete(t.image_key)
                    t.image_deleted_at = datetime.now(timezone.utc)
                    await session.commit()
        except Exception:
            pass
        raise
    finally:
        # Liberar advisory lock siempre en la misma conexión
        if lock_conn and lock_acquired and lock_key is not None:
            try:
                await lock_conn.execute(text("SELECT pg_advisory_unlock(:k)"), {"k": lock_key})
            except Exception as unlock_err:
                logger.warning("Error liberando advisory lock: %s", unlock_err)
        if lock_conn:
            await lock_conn.close()


# ---------------------------------------------------------------------------
# Reconciliación y Configuración del Worker ARQ
# ---------------------------------------------------------------------------
async def reconcile_stuck_tickets() -> None:
    """
    Reconcilia tickets que hayan quedado colgados en estado 'facturando' o 'extrayendo'
    debido a reinicios de worker o caídas de red (> 10 minutos sin actividad).
    """
    try:
        async with engine.connect() as conn:
            res = await conn.execute(
                text("""
                    SELECT id, tenant_id, folio, estado, updated_at
                    FROM tickets
                    WHERE estado = 'facturando'
                      AND updated_at < (NOW() - INTERVAL '10 minutes')
                """)
            )
            stuck_tickets = res.fetchall()

        if stuck_tickets:
            logger.warning("Reconciliando %d tickets colgados en 'facturando'...", len(stuck_tickets))
            for row in stuck_tickets:
                t_id = row.id
                t_tenant = row.tenant_id
                try:
                    async with tenant_session(t_tenant) as session:
                        t_res = await session.execute(select(Ticket).where(Ticket.id == t_id))
                        ticket = t_res.scalar_one_or_none()
                        if ticket and ticket.estado == TicketEstado.FACTURANDO:
                            ticket.error_code = "tiempo_expirado"
                            ticket.error_msg = "El proceso de facturación anterior excedió el tiempo límite (10 minutos) o el worker fue reiniciado."
                            await transition(
                                session,
                                ticket,
                                TicketEstado.RECHAZADO,
                                "Ticket recuperado automáticamente tras tiempo límite de facturación excedido.",
                                meta={"error_code": "tiempo_expirado"},
                            )
                            logger.info("Ticket colgado %s reconciliado a RECHAZADO (tiempo_expirado)", t_id)
                except Exception as rec_err:
                    logger.error("Error reconciliando ticket %s: %s", t_id, rec_err)
    except Exception as e:
        logger.warning("No se pudo ejecutar la reconciliación inicial de tickets: %s", e)


async def extract_ticket_task(ctx, tenant_id: str, ticket_id: str, explicit_merchant_slug: Optional[str] = None):
    """Tarea asíncrona de extracción registrada en ARQ."""
    await process_ticket_extraction(
        tenant_id=uuid.UUID(tenant_id),
        ticket_id=uuid.UUID(ticket_id),
        explicit_merchant_slug=explicit_merchant_slug,
    )


async def facturar_ticket_task(ctx, tenant_id: str, ticket_id: str):
    """Tarea asíncrona de facturación registrada en ARQ con captura de Timeout / Cancelación."""
    import asyncio
    t_uuid = uuid.UUID(ticket_id)
    ten_uuid = uuid.UUID(tenant_id)
    try:
        await process_ticket_facturacion(
            tenant_id=ten_uuid,
            ticket_id=t_uuid,
        )
    except (asyncio.CancelledError, TimeoutError) as cancel_err:
        logger.warning(
            "Tarea facturar_ticket_task cancelada o tiempo expirado para ticket %s: %s",
            ticket_id,
            cancel_err,
        )
        try:
            async with tenant_session(ten_uuid) as session:
                t_res = await session.execute(select(Ticket).where(Ticket.id == t_uuid))
                ticket = t_res.scalar_one_or_none()
                if ticket and ticket.estado == TicketEstado.FACTURANDO:
                    ticket.error_code = "tiempo_expirado"
                    ticket.error_msg = "El proceso de facturación excedió el tiempo límite permitido en segundo plano (10 minutos)."
                    await transition(
                        session,
                        ticket,
                        TicketEstado.RECHAZADO,
                        "Facturación cancelada por exceso de tiempo en worker.",
                        meta={"error_code": "tiempo_expirado"},
                    )
        except Exception as update_err:
            logger.error("Error actualizando ticket %s tras cancelación: %s", ticket_id, update_err)
        raise


async def on_worker_startup(ctx):
    """Gancho ejecutado al arrancar el proceso ARQ Worker."""
    logger.info("ARQ Worker iniciando. Ejecutando reconciliación de tickets colgados...")
    await reconcile_stuck_tickets()
    try:
        from .seed_merchants import seed_official_merchants
        await seed_official_merchants(get_settings().DATABASE_MIGRATION_URL)
    except Exception as exc:
        logger.warning("No se pudo sembrar catálogo oficial de comercios en startup de worker: %s", exc)


from arq.connections import RedisSettings


class WorkerSettings:
    functions = [extract_ticket_task, facturar_ticket_task]
    redis_settings = RedisSettings.from_dsn(get_settings().REDIS_URL)
    on_startup = on_worker_startup
    job_timeout = 600  # 10 minutos


