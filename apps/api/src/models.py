import enum
import uuid
from decimal import Decimal
from datetime import datetime, date, time
from typing import Optional, Dict, Any, List

from sqlalchemy import (
    BigInteger,
    Boolean,
    CheckConstraint,
    Date,
    DateTime,
    Enum,
    ForeignKey,
    Index,
    Integer,
    LargeBinary,
    Numeric,
    String,
    Text,
    Time,
    UniqueConstraint,
    text,
)
from sqlalchemy.dialects.postgresql import CITEXT, JSONB, UUID as PG_UUID
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column, relationship
from sqlalchemy.sql import func


class Base(DeclarativeBase):
    pass


# ---------------------------------------------------------------------------
# Enums
# ---------------------------------------------------------------------------
class TipoMotor(str, enum.Enum):
    API = "api"
    EMAIL = "email"
    WEB = "web"
    MANUAL = "manual"


class MembershipRole(str, enum.Enum):
    OWNER = "owner"
    ADMIN = "admin"
    UPLOADER = "uploader"
    VIEWER = "viewer"


class TicketEstado(str, enum.Enum):
    RECIBIDO = "recibido"
    EXTRAYENDO = "extrayendo"
    EXTRAIDO = "extraido"
    ENCOLADO = "encolado"
    FACTURANDO = "facturando"
    ESPERA_HUMANO = "espera_humano"
    FACTURADO = "facturado"      # Estado final
    RECHAZADO = "rechazado"      # Estado final
    CANCELADO = "cancelado"      # Estado final


class HandoffEstado(str, enum.Enum):
    ESPERANDO = "esperando"
    RESUELTO = "resuelto"
    EXPIRADO = "expirado"
    CANCELADO = "cancelado"


# ---------------------------------------------------------------------------
# Tablas Globales (Sin RLS)
# ---------------------------------------------------------------------------
class Tenant(Base):
    __tablename__ = "tenants"

    id: Mapped[uuid.UUID] = mapped_column(
        PG_UUID(as_uuid=True), primary_key=True, default=uuid.uuid4
    )
    nombre: Mapped[str] = mapped_column(String(255), nullable=False)
    slug: Mapped[str] = mapped_column(String(100), unique=True, nullable=False, index=True)
    plan: Mapped[str] = mapped_column(String(50), default="free", nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now(), nullable=False
    )

    # Relaciones
    memberships: Mapped[List["Membership"]] = relationship(back_populates="tenant", cascade="all, delete-orphan")
    fiscal_profiles: Mapped[List["FiscalProfile"]] = relationship(back_populates="tenant", cascade="all, delete-orphan")
    tickets: Mapped[List["Ticket"]] = relationship(back_populates="tenant", cascade="all, delete-orphan")


class User(Base):
    __tablename__ = "users"

    id: Mapped[uuid.UUID] = mapped_column(
        PG_UUID(as_uuid=True), primary_key=True, default=uuid.uuid4
    )
    email: Mapped[str] = mapped_column(CITEXT, unique=True, nullable=False, index=True)
    nombre: Mapped[str] = mapped_column(String(255), nullable=False)
    avatar_url: Mapped[Optional[str]] = mapped_column(String(500), nullable=True)
    google_sub: Mapped[str] = mapped_column(String(255), unique=True, nullable=False, index=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now(), nullable=False
    )

    memberships: Mapped[List["Membership"]] = relationship(back_populates="user", cascade="all, delete-orphan")


class Merchant(Base):
    """Catálogo global de comercios mantenido por el equipo Facturia (Solo lectura para facturia_app)."""
    __tablename__ = "merchants"

    id: Mapped[uuid.UUID] = mapped_column(
        PG_UUID(as_uuid=True), primary_key=True, default=uuid.uuid4
    )
    slug: Mapped[str] = mapped_column(String(100), unique=True, nullable=False, index=True)
    nombre: Mapped[str] = mapped_column(String(255), nullable=False)
    patrones: Mapped[Dict[str, Any]] = mapped_column(JSONB, nullable=False, server_default=text("'[]'::jsonb"))
    tipo_motor: Mapped[TipoMotor] = mapped_column(
        Enum(
            TipoMotor,
            name="tipo_motor",
            native_enum=True,
            values_callable=lambda obj: [e.value for e in obj],
        ),
        nullable=False,
    )
    engine_slug: Mapped[str] = mapped_column(String(100), nullable=False)
    config: Mapped[Dict[str, Any]] = mapped_column(JSONB, nullable=False, server_default=text("'{}'::jsonb"))
    requiere_captcha: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)
    activo: Mapped[bool] = mapped_column(Boolean, default=True, nullable=False)
    entrega_esperada: Mapped[str] = mapped_column(String(20), default="emisor", server_default="emisor", nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now(), nullable=False
    )


# ---------------------------------------------------------------------------
# Tablas por Tenant (TODAS con Row-Level Security)
# ---------------------------------------------------------------------------
TENANT_TABLES = [
    "memberships",
    "fiscal_profiles",
    "merchant_credentials",
    "tickets",
    "ticket_events",
    "handoff_sessions",
]


class Membership(Base):
    __tablename__ = "memberships"

    tenant_id: Mapped[uuid.UUID] = mapped_column(
        PG_UUID(as_uuid=True), ForeignKey("tenants.id", ondelete="CASCADE"), primary_key=True
    )
    user_id: Mapped[uuid.UUID] = mapped_column(
        PG_UUID(as_uuid=True), ForeignKey("users.id", ondelete="CASCADE"), primary_key=True
    )
    rol: Mapped[MembershipRole] = mapped_column(
        Enum(
            MembershipRole,
            name="membership_role",
            native_enum=True,
            values_callable=lambda obj: [e.value for e in obj],
        ),
        nullable=False,
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now(), nullable=False
    )

    tenant: Mapped["Tenant"] = relationship(back_populates="memberships")
    user: Mapped["User"] = relationship(back_populates="memberships")


class FiscalProfile(Base):
    __tablename__ = "fiscal_profiles"
    __table_args__ = (
        UniqueConstraint("tenant_id", "rfc", name="uq_fiscal_profiles_tenant_rfc"),
        CheckConstraint("rfc ~ '^[A-ZÑ&]{3,4}[0-9]{6}[A-Z0-9]{3}$'", name="ck_fiscal_profiles_rfc"),
        CheckConstraint("cp ~ '^[0-9]{5}$'", name="ck_fiscal_profiles_cp"),
    )

    id: Mapped[uuid.UUID] = mapped_column(
        PG_UUID(as_uuid=True), primary_key=True, default=uuid.uuid4
    )
    tenant_id: Mapped[uuid.UUID] = mapped_column(
        PG_UUID(as_uuid=True), ForeignKey("tenants.id", ondelete="CASCADE"), nullable=False, index=True
    )
    razon_social: Mapped[str] = mapped_column(String(255), nullable=False)
    rfc: Mapped[str] = mapped_column(String(13), nullable=False)
    cp: Mapped[str] = mapped_column(String(5), nullable=False)
    regimen_fiscal: Mapped[str] = mapped_column(String(10), nullable=False)
    uso_cfdi: Mapped[str] = mapped_column(String(10), default="G03", nullable=False)
    email_receptor: Mapped[str] = mapped_column(CITEXT, nullable=False)
    telefono: Mapped[Optional[str]] = mapped_column(String(20), nullable=True)
    calle: Mapped[Optional[str]] = mapped_column(String(255), nullable=True)
    numero_exterior: Mapped[Optional[str]] = mapped_column(String(50), nullable=True)
    numero_interior: Mapped[Optional[str]] = mapped_column(String(50), nullable=True)
    colonia: Mapped[Optional[str]] = mapped_column(String(255), nullable=True)
    municipio_alcaldia: Mapped[Optional[str]] = mapped_column(String(255), nullable=True)
    estado: Mapped[Optional[str]] = mapped_column(String(100), nullable=True)
    pais: Mapped[str] = mapped_column(String(10), default="MEX", server_default="MEX", nullable=False)
    curp: Mapped[Optional[str]] = mapped_column(String(18), nullable=True)
    es_principal: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now(), nullable=False
    )

    tenant: Mapped["Tenant"] = relationship(back_populates="fiscal_profiles")


class MerchantCredential(Base):
    __tablename__ = "merchant_credentials"
    __table_args__ = (
        UniqueConstraint("tenant_id", "merchant_id", name="uq_merchant_credentials_tenant_merchant"),
    )

    id: Mapped[uuid.UUID] = mapped_column(
        PG_UUID(as_uuid=True), primary_key=True, default=uuid.uuid4
    )
    tenant_id: Mapped[uuid.UUID] = mapped_column(
        PG_UUID(as_uuid=True), ForeignKey("tenants.id", ondelete="CASCADE"), nullable=False, index=True
    )
    merchant_id: Mapped[uuid.UUID] = mapped_column(
        PG_UUID(as_uuid=True), ForeignKey("merchants.id", ondelete="CASCADE"), nullable=False
    )
    payload_enc: Mapped[bytes] = mapped_column(LargeBinary, nullable=False)
    nonce: Mapped[bytes] = mapped_column(LargeBinary, nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now(), nullable=False
    )


class Ticket(Base):
    __tablename__ = "tickets"
    __table_args__ = (
        # Cola de procesamiento
        Index("idx_tickets_queue", "tenant_id", "estado", "created_at"),
        # Guardia de idempotencia (Regla 9)
        Index(
            "idx_tickets_idempotency",
            "tenant_id",
            "merchant_id",
            "folio",
            unique=True,
            postgresql_where=text("estado = 'facturado' AND folio IS NOT NULL"),
        ),
    )

    id: Mapped[uuid.UUID] = mapped_column(
        PG_UUID(as_uuid=True), primary_key=True, default=uuid.uuid4
    )
    tenant_id: Mapped[uuid.UUID] = mapped_column(
        PG_UUID(as_uuid=True), ForeignKey("tenants.id", ondelete="CASCADE"), nullable=False, index=True
    )
    created_by: Mapped[uuid.UUID] = mapped_column(
        PG_UUID(as_uuid=True), ForeignKey("users.id"), nullable=False
    )
    merchant_id: Mapped[Optional[uuid.UUID]] = mapped_column(
        PG_UUID(as_uuid=True), ForeignKey("merchants.id"), nullable=True
    )
    fiscal_profile_id: Mapped[Optional[uuid.UUID]] = mapped_column(
        PG_UUID(as_uuid=True), ForeignKey("fiscal_profiles.id"), nullable=True
    )

    estado: Mapped[TicketEstado] = mapped_column(
        Enum(
            TicketEstado,
            name="ticket_estado",
            native_enum=True,
            values_callable=lambda obj: [e.value for e in obj],
        ),
        default=TicketEstado.RECIBIDO,
        nullable=False,
    )

    folio: Mapped[Optional[str]] = mapped_column(String(100), nullable=True)
    web_id: Mapped[Optional[str]] = mapped_column(String(100), nullable=True)
    fecha_ticket: Mapped[Optional[date]] = mapped_column(Date, nullable=True)
    hora_ticket: Mapped[Optional[time]] = mapped_column(Time, nullable=True)

    # Montos: estrictamente Decimal y Numeric(12, 2) (Regla 8)
    total: Mapped[Optional[Decimal]] = mapped_column(Numeric(12, 2), nullable=True)
    subtotal: Mapped[Optional[Decimal]] = mapped_column(Numeric(12, 2), nullable=True)
    iva: Mapped[Optional[Decimal]] = mapped_column(Numeric(12, 2), nullable=True)

    sucursal: Mapped[Optional[str]] = mapped_column(String(255), nullable=True)
    caja: Mapped[Optional[str]] = mapped_column(String(50), nullable=True)
    transaccion: Mapped[Optional[str]] = mapped_column(String(100), nullable=True)
    rfc_emisor: Mapped[Optional[str]] = mapped_column(String(13), nullable=True)

    extracted: Mapped[Optional[Dict[str, Any]]] = mapped_column(JSONB, nullable=True)
    confianza: Mapped[Optional[Decimal]] = mapped_column(Numeric(4, 2), nullable=True)

    # Imagen efímera (Regla 3)
    image_key: Mapped[Optional[str]] = mapped_column(String(500), nullable=True)
    image_deleted_at: Mapped[Optional[datetime]] = mapped_column(DateTime(timezone=True), nullable=True)

    # CFDI nunca persistido (Regla 4: solo UUID y correo capturado en portal)
    cfdi_uuid: Mapped[Optional[str]] = mapped_column(String(36), nullable=True)
    correo_capturado_en_portal: Mapped[Optional[str]] = mapped_column(String(255), nullable=True)
    cfdi_disponible_hasta: Mapped[Optional[datetime]] = mapped_column(DateTime(timezone=True), nullable=True)
    facturado_at: Mapped[Optional[datetime]] = mapped_column(DateTime(timezone=True), nullable=True)

    error_code: Mapped[Optional[str]] = mapped_column(String(50), nullable=True)
    error_msg: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    intentos: Mapped[int] = mapped_column(Integer, default=0, nullable=False)

    # Motor Genérico y Medición de Costos (Paso B)
    url_facturacion: Mapped[Optional[str]] = mapped_column(String(500), nullable=True)
    costo_total_usd: Mapped[Optional[Decimal]] = mapped_column(Numeric(10, 5), nullable=True)
    tokens_input_total: Mapped[Optional[int]] = mapped_column(Integer, nullable=True)
    tokens_output_total: Mapped[Optional[int]] = mapped_column(Integer, nullable=True)
    pasos_agente: Mapped[Optional[int]] = mapped_column(Integer, nullable=True)
    duracion_segundos: Mapped[Optional[Decimal]] = mapped_column(Numeric(8, 2), nullable=True)

    # Inteligencia y Clasificación Fiscal SAT
    categoria_gasto: Mapped[Optional[str]] = mapped_column(String(50), nullable=True, index=True)
    desglose_impuestos: Mapped[Optional[Dict[str, Any]]] = mapped_column(JSONB, nullable=True)
    estatus_deducibilidad: Mapped[Optional[str]] = mapped_column(String(50), nullable=True)

    # Auditoría Matemática SAT Anexo 20 y Prevención de Riesgo Fiscal Art. 69-B CFF
    score_riesgo_fiscal: Mapped[Optional[int]] = mapped_column(Integer, nullable=True)
    auditoria_aritmetica: Mapped[Optional[Dict[str, Any]]] = mapped_column(JSONB, nullable=True)
    hash_integridad: Mapped[Optional[str]] = mapped_column(String(64), nullable=True, index=True)

    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now(), nullable=False
    )

    tenant: Mapped["Tenant"] = relationship(back_populates="tickets")
    events: Mapped[List["TicketEvent"]] = relationship(back_populates="ticket", cascade="all, delete-orphan")
    handoff_sessions: Mapped[List["HandoffSession"]] = relationship(back_populates="ticket", cascade="all, delete-orphan")


class TicketEvent(Base):
    """Registro de auditoría sin PII en logs (Regla 7)."""
    __tablename__ = "ticket_events"
    __table_args__ = (
        Index("idx_ticket_events_tenant_ticket", "tenant_id", "ticket_id", "ts"),
    )

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    tenant_id: Mapped[uuid.UUID] = mapped_column(
        PG_UUID(as_uuid=True), ForeignKey("tenants.id", ondelete="CASCADE"), nullable=False, index=True
    )
    ticket_id: Mapped[uuid.UUID] = mapped_column(
        PG_UUID(as_uuid=True), ForeignKey("tickets.id", ondelete="CASCADE"), nullable=False
    )
    ts: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )
    tipo: Mapped[str] = mapped_column(String(100), nullable=False)
    mensaje: Mapped[str] = mapped_column(Text, nullable=False)
    meta: Mapped[Dict[str, Any]] = mapped_column(JSONB, nullable=False, server_default=text("'{}'::jsonb"))

    ticket: Mapped["Ticket"] = relationship(back_populates="events")


class HandoffSession(Base):
    """Sesión de resolución humana para captchas (Regla 5)."""
    __tablename__ = "handoff_sessions"

    id: Mapped[uuid.UUID] = mapped_column(
        PG_UUID(as_uuid=True), primary_key=True, default=uuid.uuid4
    )
    tenant_id: Mapped[uuid.UUID] = mapped_column(
        PG_UUID(as_uuid=True), ForeignKey("tenants.id", ondelete="CASCADE"), nullable=False, index=True
    )
    ticket_id: Mapped[uuid.UUID] = mapped_column(
        PG_UUID(as_uuid=True), ForeignKey("tickets.id", ondelete="CASCADE"), nullable=False
    )
    worker_id: Mapped[Optional[str]] = mapped_column(String(100), nullable=True)
    motivo: Mapped[str] = mapped_column(String(255), nullable=False)
    estado: Mapped[HandoffEstado] = mapped_column(
        Enum(
            HandoffEstado,
            name="handoff_estado",
            native_enum=True,
            values_callable=lambda obj: [e.value for e in obj],
        ),
        default=HandoffEstado.ESPERANDO,
        nullable=False,
    )
    token_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    resolved_by: Mapped[Optional[uuid.UUID]] = mapped_column(
        PG_UUID(as_uuid=True), ForeignKey("users.id"), nullable=True
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )

    ticket: Mapped["Ticket"] = relationship(back_populates="handoff_sessions")
