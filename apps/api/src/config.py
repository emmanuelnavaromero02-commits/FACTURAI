from functools import lru_cache
from typing import Optional
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    """Configuración central de la aplicación Facturia basada en variables de entorno."""

    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore",
    )

    # Entorno y logs
    ENVIRONMENT: str = "development"
    DEBUG: bool = True
    LOG_LEVEL: str = "INFO"

    # Base de Datos (Regla 2: facturia_app para runtime)
    DATABASE_URL: str = (
        "postgresql+asyncpg://facturia_app:facturia_app_secret@localhost:5432/facturia"
    )
    DATABASE_MIGRATION_URL: str = (
        "postgresql+asyncpg://facturia_owner:facturia_owner_secret@localhost:5432/facturia"
    )

    # Redis y colas ARQ
    REDIS_URL: str = "redis://localhost:6379/0"

    # Almacenamiento S3 / MinIO (Regla 3: imágenes con TTL de 24h)
    S3_ENDPOINT_URL: str = "http://localhost:9000"
    S3_ACCESS_KEY_ID: str = "minioadmin"
    S3_SECRET_ACCESS_KEY: str = "minioadminpassword"
    S3_BUCKET_NAME: str = "facturia-tickets-temp"
    S3_REGION: str = "us-east-1"

    # SMTP / Mailpit (Regla 4: entrega de CFDI sin persistencia)
    SMTP_HOST: str = "localhost"
    SMTP_PORT: int = 1025
    SMTP_USER: Optional[str] = None
    SMTP_PASSWORD: Optional[str] = None
    SMTP_FROM: str = "no-reply@facturia.mx"

    # Cifrado de credenciales (Regla 6: AES-256-GCM derivado)
    MASTER_ENCRYPTION_KEY: str = "MDEyMzQ1Njc4OTAxMjM0NTY3ODkwMTIzNDU2Nzg5MDE="

    # Google OAuth y Sesiones
    GOOGLE_CLIENT_ID: str = "facturia-local-client-id.apps.googleusercontent.com"
    SESSION_COOKIE_NAME: str = "facturia_session"
    SESSION_MAX_AGE_SECONDS: int = 60 * 60 * 24 * 7  # 7 días
    SESSION_REFRESH_THRESHOLD_SECONDS: int = 60 * 60 * 24 * 2  # 2 días


@lru_cache
def get_settings() -> Settings:
    """Devuelve una instancia singleton de configuración cacheada."""
    return Settings()
