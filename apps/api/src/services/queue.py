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
