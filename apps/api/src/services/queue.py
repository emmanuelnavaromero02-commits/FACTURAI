import logging
import uuid
from typing import Optional
from arq import create_pool
from arq.connections import RedisSettings

from ..config import get_settings
from ..worker import process_ticket_extraction

logger = logging.getLogger(__name__)
settings = get_settings()

_arq_pool = None


async def get_arq_pool():
    global _arq_pool
    if _arq_pool is None:
        try:
            # Parse RedisSettings from REDIS_URL
            _arq_pool = await create_pool(RedisSettings.from_dsn(settings.REDIS_URL))
        except Exception as exc:
            logger.warning("No se pudo conectar a Redis para ARQ: %s", exc)
            return None
    return _arq_pool


async def enqueue_ticket_extraction(
    tenant_id: uuid.UUID,
    ticket_id: uuid.UUID,
    explicit_merchant_slug: Optional[str] = None,
) -> None:
    """
    Encola el job de extracción de ticket.
    Si está en entorno de pruebas o Redis no está activo, ejecuta el procesamiento directamente.
    """
    if settings.ENVIRONMENT in ("test", "testing"):
        # Ejecución directa en pruebas
        await process_ticket_extraction(tenant_id, ticket_id, explicit_merchant_slug)
        return

    pool = await get_arq_pool()
    if pool is not None:
        await pool.enqueue_job(
            "extract_ticket_task",
            str(tenant_id),
            str(ticket_id),
            explicit_merchant_slug,
        )
    else:
        # Fallback si Redis está en mantenimiento
        import asyncio
        asyncio.create_task(
            process_ticket_extraction(tenant_id, ticket_id, explicit_merchant_slug)
        )


async def enqueue_ticket_facturacion(
    tenant_id: uuid.UUID,
    ticket_id: uuid.UUID,
    defer_seconds: int = 0,
) -> None:
    """
    Encola el job de facturación con el motor correspondiente en ARQ.
    Trampa 1 evitada: el payload lleva tenant_id explícito.
    """
    from ..worker import process_ticket_facturacion

    if settings.ENVIRONMENT in ("test", "testing"):
        if defer_seconds == 0:
            await process_ticket_facturacion(tenant_id, ticket_id)
        return

    pool = await get_arq_pool()
    if pool is not None:
        kwargs = {}
        if defer_seconds > 0:
            kwargs["_defer_by"] = defer_seconds
        await pool.enqueue_job(
            "facturar_ticket_task",
            str(tenant_id),
            str(ticket_id),
            **kwargs,
        )
    else:
        import asyncio

        async def run_delayed():
            if defer_seconds > 0:
                await asyncio.sleep(defer_seconds)
            await process_ticket_facturacion(tenant_id, ticket_id)

        asyncio.create_task(run_delayed())
