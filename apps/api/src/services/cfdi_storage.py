import abc
import io
import os
import time
import uuid
import zipfile
from typing import Optional
from cryptography.hazmat.primitives.ciphers.aead import AESGCM
import redis.asyncio as aioredis

from ..config import get_settings
from ..security import derive_tenant_key

settings = get_settings()

DEFAULT_CFDI_TTL_SECONDS = 30 * 60  # 30 minutos
MAX_DOWNLOADS_ALLOWED = 5


class CfdiStorageService(abc.ABC):
    """Interfaz abstracta para el almacenamiento temporal cifrado de CFDI."""

    @abc.abstractmethod
    async def save_cfdi_bundle(
        self,
        tenant_id: uuid.UUID,
        ticket_id: uuid.UUID,
        cfdi_uuid: str,
        pdf_bytes: Optional[bytes],
        xml_bytes: Optional[bytes],
        ttl_seconds: int = DEFAULT_CFDI_TTL_SECONDS,
    ) -> None:
        """Cifra y almacena temporalmente los archivos del CFDI en un archivo ZIP con TTL."""
        pass

    @abc.abstractmethod
    async def get_cfdi_bundle(
        self,
        tenant_id: uuid.UUID,
        ticket_id: uuid.UUID,
    ) -> Optional[bytes]:
        """
        Recupera y descifra el archivo ZIP del CFDI si aún no ha expirado
        y no ha superado el límite de 5 descargas.
        Al alcanzar la 5ta descarga, se elimina automáticamente.
        """
        pass

    @abc.abstractmethod
    async def is_expired_or_absent(
        self,
        tenant_id: uuid.UUID,
        ticket_id: uuid.UUID,
    ) -> bool:
        """Verifica si el CFDI ya no existe o expiró."""
        pass

    @abc.abstractmethod
    async def delete_cfdi_bundle(
        self,
        tenant_id: uuid.UUID,
        ticket_id: uuid.UUID,
    ) -> None:
        """Elimina manualmente el bundle temporal de CFDI."""
        pass


def pack_and_encrypt_cfdi(
    tenant_id: uuid.UUID,
    cfdi_uuid: str,
    pdf_bytes: Optional[bytes],
    xml_bytes: Optional[bytes],
) -> bytes:
    """Empaqueta en ZIP y cifra con AES-256-GCM usando la clave derivada del tenant."""
    zip_buf = io.BytesIO()
    with zipfile.ZipFile(zip_buf, mode="w", compression=zipfile.ZIP_DEFLATED) as zf:
        if pdf_bytes:
            zf.writestr(f"{cfdi_uuid}.pdf", pdf_bytes)
        if xml_bytes:
            zf.writestr(f"{cfdi_uuid}.xml", xml_bytes)
    raw_zip = zip_buf.getvalue()

    derived_key = derive_tenant_key(tenant_id)
    aesgcm = AESGCM(derived_key)
    nonce = os.urandom(12)
    aad = str(tenant_id).encode("utf-8")
    ciphertext = aesgcm.encrypt(nonce, raw_zip, aad)
    # Payload almacenado: nonce (12 bytes) + ciphertext autenticado
    return nonce + ciphertext


def decrypt_and_unpack_cfdi(
    tenant_id: uuid.UUID,
    stored_payload: bytes,
) -> bytes:
    """Descifra el payload con AES-256-GCM usando la clave del tenant y retorna los bytes del ZIP."""
    derived_key = derive_tenant_key(tenant_id)
    aesgcm = AESGCM(derived_key)
    nonce = stored_payload[:12]
    ciphertext = stored_payload[12:]
    aad = str(tenant_id).encode("utf-8")
    return aesgcm.decrypt(nonce, ciphertext, aad)


class RedisCfdiStorageService(CfdiStorageService):
    """Implementación de almacenamiento efímero cifrado en Redis con TTL de 30m."""

    def __init__(self, redis_url: Optional[str] = None):
        self._redis_url = redis_url or settings.REDIS_URL
        self._client: Optional[aioredis.Redis] = None

    def _get_client(self) -> aioredis.Redis:
        if self._client is None:
            self._client = aioredis.from_url(
                self._redis_url,
                decode_responses=False,
            )
        return self._client

    async def save_cfdi_bundle(
        self,
        tenant_id: uuid.UUID,
        ticket_id: uuid.UUID,
        cfdi_uuid: str,
        pdf_bytes: Optional[bytes],
        xml_bytes: Optional[bytes],
        ttl_seconds: int = DEFAULT_CFDI_TTL_SECONDS,
    ) -> None:
        payload = pack_and_encrypt_cfdi(tenant_id, cfdi_uuid, pdf_bytes, xml_bytes)
        key = f"cfdi:{tenant_id}:{ticket_id}"
        counter_key = f"cfdi_downloads:{tenant_id}:{ticket_id}"

        r = self._get_client()
        async with r.pipeline(transaction=True) as pipe:
            pipe.set(key, payload, ex=ttl_seconds)
            pipe.set(counter_key, 0, ex=ttl_seconds)
            await pipe.execute()

    async def get_cfdi_bundle(
        self,
        tenant_id: uuid.UUID,
        ticket_id: uuid.UUID,
    ) -> Optional[bytes]:
        key = f"cfdi:{tenant_id}:{ticket_id}"
        counter_key = f"cfdi_downloads:{tenant_id}:{ticket_id}"

        r = self._get_client()
        stored_payload = await r.get(key)
        if not stored_payload:
            return None

        # Incrementar contador de descargas (Corrección A: tolerancia a desconexión, max 5)
        downloads = await r.incr(counter_key)
        if downloads >= MAX_DOWNLOADS_ALLOWED:
            # Borrar tras completar la quinta descarga
            await r.delete(key, counter_key)

        return decrypt_and_unpack_cfdi(tenant_id, stored_payload)

    async def is_expired_or_absent(
        self,
        tenant_id: uuid.UUID,
        ticket_id: uuid.UUID,
    ) -> bool:
        r = self._get_client()
        exists = await r.exists(f"cfdi:{tenant_id}:{ticket_id}")
        return not bool(exists)

    async def delete_cfdi_bundle(
        self,
        tenant_id: uuid.UUID,
        ticket_id: uuid.UUID,
    ) -> None:
        r = self._get_client()
        key = f"cfdi:{tenant_id}:{ticket_id}"
        counter_key = f"cfdi_downloads:{tenant_id}:{ticket_id}"
        await r.delete(key, counter_key)


class InMemoryCfdiStorageService(CfdiStorageService):
    """Almacenamiento en memoria para pruebas."""

    def __init__(self):
        # key -> (payload, expires_at)
        self._store: dict[str, tuple[bytes, float]] = {}
        self._downloads: dict[str, int] = {}

    async def save_cfdi_bundle(
        self,
        tenant_id: uuid.UUID,
        ticket_id: uuid.UUID,
        cfdi_uuid: str,
        pdf_bytes: Optional[bytes],
        xml_bytes: Optional[bytes],
        ttl_seconds: int = DEFAULT_CFDI_TTL_SECONDS,
    ) -> None:
        payload = pack_and_encrypt_cfdi(tenant_id, cfdi_uuid, pdf_bytes, xml_bytes)
        key = f"cfdi:{tenant_id}:{ticket_id}"
        self._store[key] = (payload, time.time() + ttl_seconds)
        self._downloads[key] = 0

    async def get_cfdi_bundle(
        self,
        tenant_id: uuid.UUID,
        ticket_id: uuid.UUID,
    ) -> Optional[bytes]:
        key = f"cfdi:{tenant_id}:{ticket_id}"
        if key not in self._store:
            return None

        payload, expires_at = self._store[key]
        if time.time() > expires_at:
            self._store.pop(key, None)
            self._downloads.pop(key, None)
            return None

        self._downloads[key] = self._downloads.get(key, 0) + 1
        if self._downloads[key] >= MAX_DOWNLOADS_ALLOWED:
            self._store.pop(key, None)
            self._downloads.pop(key, None)

        return decrypt_and_unpack_cfdi(tenant_id, payload)

    async def is_expired_or_absent(
        self,
        tenant_id: uuid.UUID,
        ticket_id: uuid.UUID,
    ) -> bool:
        key = f"cfdi:{tenant_id}:{ticket_id}"
        if key not in self._store:
            return True
        _, expires_at = self._store[key]
        if time.time() > expires_at:
            self._store.pop(key, None)
            self._downloads.pop(key, None)
            return True
        return False

    async def delete_cfdi_bundle(
        self,
        tenant_id: uuid.UUID,
        ticket_id: uuid.UUID,
    ) -> None:
        key = f"cfdi:{tenant_id}:{ticket_id}"
        self._store.pop(key, None)
        self._downloads.pop(key, None)


_cfdi_storage: CfdiStorageService = (
    InMemoryCfdiStorageService() if settings.ENVIRONMENT == "test" else RedisCfdiStorageService()
)


def get_cfdi_storage() -> CfdiStorageService:
    return _cfdi_storage


def set_cfdi_storage(storage: CfdiStorageService) -> None:
    global _cfdi_storage
    _cfdi_storage = storage
