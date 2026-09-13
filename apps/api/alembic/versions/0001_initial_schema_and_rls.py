"""0001 - Initial schema, indexes, RLS and permissions

Revision ID: 0001_initial_schema
Revises: 
Create Date: 2026-09-13 16:35:00

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

revision: str = "0001_initial_schema"
down_revision: Union[str, None] = None
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

TENANT_TABLES = [
    "memberships",
    "fiscal_profiles",
    "merchant_credentials",
    "tickets",
    "ticket_events",
    "handoff_sessions",
]


def upgrade() -> None:
    # 1. Extensiones necesarias
    op.execute("CREATE EXTENSION IF NOT EXISTS citext;")
    op.execute('CREATE EXTENSION IF NOT EXISTS "uuid-ossp";')

    # 2. Tipos Enum nativos de PostgreSQL
    tipo_motor = postgresql.ENUM("api", "email", "web", "manual", name="tipo_motor")
    tipo_motor.create(op.get_bind(), checkfirst=True)

    membership_role = postgresql.ENUM("owner", "admin", "uploader", "viewer", name="membership_role")
    membership_role.create(op.get_bind(), checkfirst=True)

    ticket_estado = postgresql.ENUM(
        "recibido", "extrayendo", "extraido", "encolado", "facturando",
        "espera_humano", "facturado", "rechazado", "cancelado",
        name="ticket_estado"
    )
    ticket_estado.create(op.get_bind(), checkfirst=True)

    handoff_estado = postgresql.ENUM(
        "esperando", "resuelto", "expirado", "cancelado", name="handoff_estado"
    )
    handoff_estado.create(op.get_bind(), checkfirst=True)

    # 3. Tablas Globales (sin RLS)
    op.create_table(
        "tenants",
        sa.Column("id", postgresql.UUID(as_uuid=True), server_default=sa.text("gen_random_uuid()"), primary_key=True),
        sa.Column("nombre", sa.String(255), nullable=False),
        sa.Column("slug", sa.String(100), nullable=False, unique=True),
        sa.Column("plan", sa.String(50), server_default="free", nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.func.now(), onupdate=sa.func.now(), nullable=False),
    )
    op.create_index("idx_tenants_slug", "tenants", ["slug"])

    op.create_table(
        "users",
        sa.Column("id", postgresql.UUID(as_uuid=True), server_default=sa.text("gen_random_uuid()"), primary_key=True),
        sa.Column("email", postgresql.CITEXT(), nullable=False, unique=True),
        sa.Column("nombre", sa.String(255), nullable=False),
        sa.Column("avatar_url", sa.String(500), nullable=True),
        sa.Column("google_sub", sa.String(255), nullable=False, unique=True),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.func.now(), onupdate=sa.func.now(), nullable=False),
    )
    op.create_index("idx_users_email", "users", ["email"])
    op.create_index("idx_users_google_sub", "users", ["google_sub"])

    op.create_table(
        "merchants",
        sa.Column("id", postgresql.UUID(as_uuid=True), server_default=sa.text("gen_random_uuid()"), primary_key=True),
        sa.Column("slug", sa.String(100), nullable=False, unique=True),
        sa.Column("nombre", sa.String(255), nullable=False),
        sa.Column("patrones", postgresql.JSONB(astext_type=sa.Text()), server_default=sa.text("'[]'::jsonb"), nullable=False),
        sa.Column("tipo_motor", postgresql.ENUM("api", "email", "web", "manual", name="tipo_motor", create_type=False), nullable=False),
        sa.Column("engine_slug", sa.String(100), nullable=False),
        sa.Column("config", postgresql.JSONB(astext_type=sa.Text()), server_default=sa.text("'{}'::jsonb"), nullable=False),
        sa.Column("requiere_captcha", sa.Boolean(), server_default=sa.text("false"), nullable=False),
        sa.Column("activo", sa.Boolean(), server_default=sa.text("true"), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.func.now(), onupdate=sa.func.now(), nullable=False),
    )
    op.create_index("idx_merchants_slug", "merchants", ["slug"])

    # 4. Tablas por Tenant
    op.create_table(
        "memberships",
        sa.Column("tenant_id", postgresql.UUID(as_uuid=True), sa.ForeignKey("tenants.id", ondelete="CASCADE"), nullable=False),
        sa.Column("user_id", postgresql.UUID(as_uuid=True), sa.ForeignKey("users.id", ondelete="CASCADE"), nullable=False),
        sa.Column("rol", postgresql.ENUM("owner", "admin", "uploader", "viewer", name="membership_role", create_type=False), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.func.now(), onupdate=sa.func.now(), nullable=False),
        sa.PrimaryKeyConstraint("tenant_id", "user_id"),
    )

    op.create_table(
        "fiscal_profiles",
        sa.Column("id", postgresql.UUID(as_uuid=True), server_default=sa.text("gen_random_uuid()"), primary_key=True),
        sa.Column("tenant_id", postgresql.UUID(as_uuid=True), sa.ForeignKey("tenants.id", ondelete="CASCADE"), nullable=False),
        sa.Column("razon_social", sa.String(255), nullable=False),
        sa.Column("rfc", sa.String(13), nullable=False),
        sa.Column("cp", sa.String(5), nullable=False),
        sa.Column("regimen_fiscal", sa.String(10), nullable=False),
        sa.Column("uso_cfdi", sa.String(10), server_default="G03", nullable=False),
        sa.Column("email_receptor", postgresql.CITEXT(), nullable=False),
        sa.Column("telefono", sa.String(20), nullable=True),
        sa.Column("es_principal", sa.Boolean(), server_default=sa.text("false"), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.func.now(), onupdate=sa.func.now(), nullable=False),
        sa.UniqueConstraint("tenant_id", "rfc", name="uq_fiscal_profiles_tenant_rfc"),
        sa.CheckConstraint("rfc ~ '^[A-ZÑ&]{3,4}[0-9]{6}[A-Z0-9]{3}$'", name="ck_fiscal_profiles_rfc"),
        sa.CheckConstraint("cp ~ '^[0-9]{5}$'", name="ck_fiscal_profiles_cp"),
    )
    op.create_index("idx_fiscal_profiles_tenant", "fiscal_profiles", ["tenant_id"])

    op.create_table(
        "merchant_credentials",
        sa.Column("id", postgresql.UUID(as_uuid=True), server_default=sa.text("gen_random_uuid()"), primary_key=True),
        sa.Column("tenant_id", postgresql.UUID(as_uuid=True), sa.ForeignKey("tenants.id", ondelete="CASCADE"), nullable=False),
        sa.Column("merchant_id", postgresql.UUID(as_uuid=True), sa.ForeignKey("merchants.id", ondelete="CASCADE"), nullable=False),
        sa.Column("payload_enc", sa.LargeBinary(), nullable=False),
        sa.Column("nonce", sa.LargeBinary(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.func.now(), onupdate=sa.func.now(), nullable=False),
        sa.UniqueConstraint("tenant_id", "merchant_id", name="uq_merchant_credentials_tenant_merchant"),
    )
    op.create_index("idx_merchant_credentials_tenant", "merchant_credentials", ["tenant_id"])

    op.create_table(
        "tickets",
        sa.Column("id", postgresql.UUID(as_uuid=True), server_default=sa.text("gen_random_uuid()"), primary_key=True),
        sa.Column("tenant_id", postgresql.UUID(as_uuid=True), sa.ForeignKey("tenants.id", ondelete="CASCADE"), nullable=False),
        sa.Column("created_by", postgresql.UUID(as_uuid=True), sa.ForeignKey("users.id"), nullable=False),
        sa.Column("merchant_id", postgresql.UUID(as_uuid=True), sa.ForeignKey("merchants.id"), nullable=True),
        sa.Column("fiscal_profile_id", postgresql.UUID(as_uuid=True), sa.ForeignKey("fiscal_profiles.id"), nullable=True),
        sa.Column("estado", postgresql.ENUM("recibido", "extrayendo", "extraido", "encolado", "facturando", "espera_humano", "facturado", "rechazado", "cancelado", name="ticket_estado", create_type=False), server_default="recibido", nullable=False),
        sa.Column("folio", sa.String(100), nullable=True),
        sa.Column("web_id", sa.String(100), nullable=True),
        sa.Column("fecha_ticket", sa.Date(), nullable=True),
        sa.Column("hora_ticket", sa.Time(), nullable=True),
        sa.Column("total", sa.Numeric(12, 2), nullable=True),
        sa.Column("subtotal", sa.Numeric(12, 2), nullable=True),
        sa.Column("iva", sa.Numeric(12, 2), nullable=True),
        sa.Column("sucursal", sa.String(255), nullable=True),
        sa.Column("caja", sa.String(50), nullable=True),
        sa.Column("transaccion", sa.String(100), nullable=True),
        sa.Column("rfc_emisor", sa.String(13), nullable=True),
        sa.Column("extracted", postgresql.JSONB(astext_type=sa.Text()), nullable=True),
        sa.Column("confianza", sa.Numeric(4, 2), nullable=True),
        sa.Column("image_key", sa.String(500), nullable=True),
        sa.Column("image_deleted_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("cfdi_uuid", sa.String(36), nullable=True),
        sa.Column("cfdi_sent_to", sa.String(255), nullable=True),
        sa.Column("facturado_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("error_code", sa.String(50), nullable=True),
        sa.Column("error_msg", sa.Text(), nullable=True),
        sa.Column("intentos", sa.Integer(), server_default=sa.text("0"), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.func.now(), onupdate=sa.func.now(), nullable=False),
    )

    # Índices clave de tickets
    op.create_index("idx_tickets_queue", "tickets", ["tenant_id", "estado", "created_at"])
    op.create_index(
        "idx_tickets_idempotency",
        "tickets",
        ["tenant_id", "merchant_id", "folio"],
        unique=True,
        postgresql_where=sa.text("estado = 'facturado' AND folio IS NOT NULL"),
    )

    op.create_table(
        "ticket_events",
        sa.Column("id", sa.BigInteger(), sa.Identity(always=False), primary_key=True),
        sa.Column("tenant_id", postgresql.UUID(as_uuid=True), sa.ForeignKey("tenants.id", ondelete="CASCADE"), nullable=False),
        sa.Column("ticket_id", postgresql.UUID(as_uuid=True), sa.ForeignKey("tickets.id", ondelete="CASCADE"), nullable=False),
        sa.Column("ts", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.Column("tipo", sa.String(100), nullable=False),
        sa.Column("mensaje", sa.Text(), nullable=False),
        sa.Column("meta", postgresql.JSONB(astext_type=sa.Text()), server_default=sa.text("'{}'::jsonb"), nullable=False),
    )
    op.create_index("idx_ticket_events_tenant_ticket", "ticket_events", ["tenant_id", "ticket_id", "ts"])

    op.create_table(
        "handoff_sessions",
        sa.Column("id", postgresql.UUID(as_uuid=True), server_default=sa.text("gen_random_uuid()"), primary_key=True),
        sa.Column("tenant_id", postgresql.UUID(as_uuid=True), sa.ForeignKey("tenants.id", ondelete="CASCADE"), nullable=False),
        sa.Column("ticket_id", postgresql.UUID(as_uuid=True), sa.ForeignKey("tickets.id", ondelete="CASCADE"), nullable=False),
        sa.Column("worker_id", sa.String(100), nullable=True),
        sa.Column("motivo", sa.String(255), nullable=False),
        sa.Column("estado", postgresql.ENUM("esperando", "resuelto", "expirado", "cancelado", name="handoff_estado", create_type=False), server_default="esperando", nullable=False),
        sa.Column("token_hash", sa.String(64), nullable=False),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("resolved_by", postgresql.UUID(as_uuid=True), sa.ForeignKey("users.id"), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
    )
    op.create_index("idx_handoff_sessions_tenant", "handoff_sessions", ["tenant_id"])

    # 5. Configurar Row-Level Security (RLS) en todas las tablas por tenant
    # Predicado estricto con nullif para evitar 500 cuando app.tenant_id esté vacío:
    # tenant_id = nullif(current_setting('app.tenant_id', true), '')::uuid
    predicado_rls = "tenant_id = nullif(current_setting('app.tenant_id', true), '')::uuid"

    for table in TENANT_TABLES:
        op.execute(f"ALTER TABLE {table} ENABLE ROW LEVEL SECURITY;")
        op.execute(f"ALTER TABLE {table} FORCE ROW LEVEL SECURITY;")
        op.execute(f"""
            CREATE POLICY tenant_isolation_policy ON {table}
                FOR ALL
                USING ({predicado_rls})
                WITH CHECK ({predicado_rls});
        """)

    # 6. Otorgar permisos a facturia_app y revocar escrituras sobre merchants
    op.execute("GRANT SELECT, INSERT, UPDATE, DELETE ON ALL TABLES IN SCHEMA public TO facturia_app;")
    op.execute("GRANT USAGE, SELECT ON ALL SEQUENCES IN SCHEMA public TO facturia_app;")
    op.execute("REVOKE INSERT, UPDATE, DELETE ON TABLE merchants FROM facturia_app;")


def downgrade() -> None:
    for table in reversed(TENANT_TABLES):
        op.execute(f"DROP POLICY IF EXISTS tenant_isolation_policy ON {table};")
        op.execute(f"ALTER TABLE {table} NO FORCE ROW LEVEL SECURITY;")
        op.execute(f"ALTER TABLE {table} DISABLE ROW LEVEL SECURITY;")

    for table in [
        "handoff_sessions",
        "ticket_events",
        "tickets",
        "merchant_credentials",
        "fiscal_profiles",
        "memberships",
        "merchants",
        "users",
        "tenants",
    ]:
        op.drop_table(table)

    op.execute("DROP TYPE IF EXISTS handoff_estado;")
    op.execute("DROP TYPE IF EXISTS ticket_estado;")
    op.execute("DROP TYPE IF EXISTS membership_role;")
    op.execute("DROP TYPE IF EXISTS tipo_motor;")
