import os
from decimal import Decimal
from functools import lru_cache
from typing import Optional
from pydantic import model_validator
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
    ALLOW_MOCK_AUTH: bool = False
    DEBUG: bool = True
    LOG_LEVEL: str = "INFO"

    @model_validator(mode="after")
    def validate_security_settings(self) -> "Settings":
        if self.ENVIRONMENT in ("production", "staging") and self.ALLOW_MOCK_AUTH:
            raise ValueError(
                "Configuración inválida y prohibida por seguridad: "
                "ALLOW_MOCK_AUTH no puede ser True en entornos de 'production' o 'staging'."
            )
        return self

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

    # Cifrado de credenciales (Regla 6: AES-256-GCM derivado)
    MASTER_ENCRYPTION_KEY: str = "MDEyMzQ1Njc4OTAxMjM0NTY3ODkwMTIzNDU2Nzg5MDE="

    # Google OAuth y Sesiones
    GOOGLE_CLIENT_ID: str = "facturia-local-client-id.apps.googleusercontent.com"
    SESSION_COOKIE_NAME: str = "facturia_session"
    SESSION_MAX_AGE_SECONDS: int = 60 * 60 * 24 * 7  # 7 días
    SESSION_REFRESH_THRESHOLD_SECONDS: int = 60 * 60 * 24 * 2  # 2 días

    # Anthropic Modelos (Regla 11: modelos explícitos sin sustitución)
    ANTHROPIC_API_KEY: Optional[str] = None
    ANTHROPIC_MODEL_VISION: str = "claude-opus-5"
    ANTHROPIC_MODEL_AGENTE: str = "claude-fable-5-1"
    ANTHROPIC_MODEL_FALLBACK: str = "claude-sonnet-5"

    # Agente Genérico Web y Control de Costos (Paso B)
    COSTO_MAXIMO_POR_TICKET_USD: Decimal = Decimal("0.50")
    PASOS_MAXIMOS_AGENTE: int = 30

    # Handoff Humano Interactivo (Paso C)
    HANDOFF_TTL_SEGUNDOS: int = 180
    HANDOFF_MAX_CONCURRENTES: int = 5


# Tabla de precios por millón de tokens en USD.
# Se deja VACÍA por defecto; lee dinámicamente de variables de entorno sin valor por defecto:
#   ANTHROPIC_PRECIO_IN_<MODELO> y ANTHROPIC_PRECIO_OUT_<MODELO>
# Si no están definidas, el costo es None ("costo no disponible").
# Documentación oficial de modelos: https://platform.claude.com/docs/en/models/overview
ANTHROPIC_TOKEN_PRICING_PER_MILLION: dict[str, dict[str, float]] = {}


def normalize_model_name_for_env(model: str) -> str:
    """
    Normaliza el nombre del modelo a mayúsculas con guiones bajos para variables de entorno:
      claude-opus-5    -> CLAUDE_OPUS_5
      claude-fable-5-1 -> CLAUDE_FABLE_5_1
      claude-sonnet-5  -> CLAUDE_SONNET_5
    """
    return model.strip().upper().replace("-", "_").replace(".", "_")


def get_model_token_pricing(model: str) -> Optional[dict[str, float]]:
    """
    Obtiene los precios de tokens por millón (USD) para un modelo dado leyendo de variables de entorno:
      ANTHROPIC_PRECIO_IN_<MODELO_NORMALIZADO>
      ANTHROPIC_PRECIO_OUT_<MODELO_NORMALIZADO>
    (ejemplos: ANTHROPIC_PRECIO_IN_CLAUDE_OPUS_5, ANTHROPIC_PRECIO_OUT_CLAUDE_OPUS_5).
    Si no están definidas ambas variables, retorna None ("costo no disponible").
    Documentación oficial: https://platform.claude.com/docs/en/models/overview
    """
    if model in ANTHROPIC_TOKEN_PRICING_PER_MILLION:
        return ANTHROPIC_TOKEN_PRICING_PER_MILLION[model]

    env_suffix = normalize_model_name_for_env(model)
    in_val = os.environ.get(f"ANTHROPIC_PRECIO_IN_{env_suffix}")
    out_val = os.environ.get(f"ANTHROPIC_PRECIO_OUT_{env_suffix}")

    if in_val is not None and out_val is not None:
        try:
            return {"input": float(in_val), "output": float(out_val)}
        except ValueError:
            return None
    return None


@lru_cache
def get_settings() -> Settings:
    """Devuelve una instancia singleton de configuración cacheada."""
    return Settings()
