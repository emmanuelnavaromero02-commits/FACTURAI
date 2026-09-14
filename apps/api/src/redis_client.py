import asyncio
import logging
from typing import Optional
import redis.asyncio as aioredis

from .config import get_settings

logger = logging.getLogger(__name__)

_redis_client: Optional[aioredis.Redis] = None
_client_loop: Optional[asyncio.AbstractEventLoop] = None


def get_redis_client() -> aioredis.Redis:
    """Devuelve una instancia del cliente Redis asíncrono atada al bucle de eventos actual."""
    global _redis_client, _client_loop
    try:
        current_loop = asyncio.get_running_loop()
    except RuntimeError:
        current_loop = None

    if _redis_client is None or _client_loop != current_loop:
        settings = get_settings()
        _redis_client = aioredis.from_url(settings.REDIS_URL, decode_responses=True)
        _client_loop = current_loop
    return _redis_client


async def close_redis_client() -> None:
    """Cierra la conexión activa de Redis si existe."""
    global _redis_client, _client_loop
    if _redis_client is not None:
        try:
            await _redis_client.aclose()
        except Exception as exc:
            logger.warning("Error cerrando cliente Redis: %s", exc)
        finally:
            _redis_client = None
            _client_loop = None
