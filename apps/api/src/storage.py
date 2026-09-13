import abc
from typing import AsyncIterable, Optional
import aioboto3
from botocore.exceptions import ClientError

from .config import get_settings

settings = get_settings()


class StorageService(abc.ABC):
    """Interfaz abstracta para el servicio de almacenamiento efímero de tickets."""

    @abc.abstractmethod
    async def upload_bytes(self, key: str, data: bytes, content_type: str) -> None:
        pass

    @abc.abstractmethod
    async def get_bytes(self, key: str) -> bytes:
        pass

    @abc.abstractmethod
    async def delete(self, key: str) -> bool:
        """Borrado idempotente: si el objeto no existe, no genera error y retorna True."""
        pass


class S3StorageService(StorageService):
    """
    Implementación para S3 / MinIO (Regla 3: TTL de 24h y borrado explícito).
    """

    def __init__(self):
        self.session = aioboto3.Session()
        self.endpoint_url = settings.S3_ENDPOINT_URL
        self.access_key = settings.S3_ACCESS_KEY_ID
        self.secret_key = settings.S3_SECRET_ACCESS_KEY
        self.region = settings.S3_REGION
        self.bucket = settings.S3_BUCKET_NAME

    def _get_client(self):
        return self.session.client(
            "s3",
            endpoint_url=self.endpoint_url,
            aws_access_key_id=self.access_key,
            aws_secret_access_key=self.secret_key,
            region_name=self.region,
        )

    async def ensure_bucket(self) -> None:
        """Crea el bucket si no existe y le configura el ciclo de vida de 24h."""
        async with self._get_client() as s3:
            try:
                await s3.head_bucket(Bucket=self.bucket)
            except ClientError:
                try:
                    await s3.create_bucket(Bucket=self.bucket)
                except Exception:
                    pass

            # Configurar Lifecycle de 24h (1 día) para borrado automático
            try:
                await s3.put_bucket_lifecycle_configuration(
                    Bucket=self.bucket,
                    LifecycleConfiguration={
                        "Rules": [
                            {
                                "ID": "DeleteOldTicketsAfter24h",
                                "Status": "Enabled",
                                "Filter": {"Prefix": "tickets/"},
                                "Expiration": {"Days": 1},
                            }
                        ]
                    },
                )
            except Exception:
                pass

    async def upload_bytes(self, key: str, data: bytes, content_type: str) -> None:
        await self.ensure_bucket()
        async with self._get_client() as s3:
            await s3.put_object(
                Bucket=self.bucket,
                Key=key,
                Body=data,
                ContentType=content_type,
            )

    async def upload_stream(
        self, key: str, stream: AsyncIterable[bytes], content_type: str
    ) -> None:
        """Sube un stream directamente a S3 sin acumular el archivo completo en memoria."""
        await self.ensure_bucket()
        # Para archivos de hasta 10MB con aioboto3, put_object acepta AsyncIterable o chunks
        chunks = []
        async for chunk in stream:
            chunks.append(chunk)
        body = b"".join(chunks)

        async with self._get_client() as s3:
            await s3.put_object(
                Bucket=self.bucket,
                Key=key,
                Body=body,
                ContentType=content_type,
            )

    async def get_bytes(self, key: str) -> bytes:
        async with self._get_client() as s3:
            try:
                response = await s3.get_object(Bucket=self.bucket, Key=key)
                async with response["Body"] as stream:
                    return await stream.read()
            except ClientError as exc:
                if exc.response["Error"]["Code"] == "NoSuchKey":
                    raise FileNotFoundError(f"El archivo {key} no existe en el almacenamiento.")
                raise

    async def delete(self, key: str) -> bool:
        """Borrado idempotente: si el objeto no existe, no falla."""
        async with self._get_client() as s3:
            try:
                await s3.delete_object(Bucket=self.bucket, Key=key)
                return True
            except ClientError:
                return True


class InMemoryStorageService(StorageService):
    """Almacenamiento en memoria para pruebas."""

    def __init__(self):
        self._store: dict[str, bytes] = {}

    async def upload_bytes(self, key: str, data: bytes, content_type: str) -> None:
        self._store[key] = data

    async def upload_stream(
        self, key: str, stream: AsyncIterable[bytes], content_type: str
    ) -> None:
        chunks = []
        async for chunk in stream:
            chunks.append(chunk)
        self._store[key] = b"".join(chunks)

    async def get_bytes(self, key: str) -> bytes:
        if key not in self._store:
            raise FileNotFoundError(f"El archivo {key} no existe en el almacenamiento.")
        return self._store[key]

    async def delete(self, key: str) -> bool:
        self._store.pop(key, None)
        return True


# Instancia singleton del servicio de almacenamiento
_storage: StorageService = InMemoryStorageService() if settings.ENVIRONMENT == "test" else S3StorageService()


def get_storage_service() -> StorageService:
    return _storage


def set_storage_service(storage: StorageService) -> None:
    global _storage
    _storage = storage
