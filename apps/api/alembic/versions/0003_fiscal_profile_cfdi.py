"""0003 - Fiscal profile complete constancia, merchant entrega_esperada and cfdi download

Revision ID: 0003_fiscal_profile_and_cfdi_download
Revises: 0002_user_memberships_rls
Create Date: 2026-09-13 18:15:00

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


revision: str = "0003_fiscal_profile_cfdi"
down_revision: Union[str, None] = "0002_user_memberships_rls"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    # 1. Merchants: entrega esperada ('emisor', 'descarga', 'ninguna')
    op.add_column(
        "merchants",
        sa.Column("entrega_esperada", sa.String(length=20), server_default="emisor", nullable=False),
    )

    # 2. Tickets: renombrar cfdi_sent_to a correo_capturado_en_portal y agregar cfdi_disponible_hasta
    op.alter_column("tickets", "cfdi_sent_to", new_column_name="correo_capturado_en_portal")
    op.add_column(
        "tickets",
        sa.Column("cfdi_disponible_hasta", sa.DateTime(timezone=True), nullable=True),
    )

    # 3. Fiscal profiles: constancia completa de situación fiscal
    op.add_column("fiscal_profiles", sa.Column("calle", sa.String(length=255), nullable=True))
    op.add_column("fiscal_profiles", sa.Column("numero_exterior", sa.String(length=50), nullable=True))
    op.add_column("fiscal_profiles", sa.Column("numero_interior", sa.String(length=50), nullable=True))
    op.add_column("fiscal_profiles", sa.Column("colonia", sa.String(length=255), nullable=True))
    op.add_column("fiscal_profiles", sa.Column("municipio_alcaldia", sa.String(length=255), nullable=True))
    op.add_column("fiscal_profiles", sa.Column("estado", sa.String(length=100), nullable=True))
    op.add_column("fiscal_profiles", sa.Column("pais", sa.String(length=10), server_default="MEX", nullable=False))
    op.add_column("fiscal_profiles", sa.Column("curp", sa.String(length=18), nullable=True))


def downgrade() -> None:
    # 3. Fiscal profiles
    op.drop_column("fiscal_profiles", "curp")
    op.drop_column("fiscal_profiles", "pais")
    op.drop_column("fiscal_profiles", "estado")
    op.drop_column("fiscal_profiles", "municipio_alcaldia")
    op.drop_column("fiscal_profiles", "colonia")
    op.drop_column("fiscal_profiles", "numero_interior")
    op.drop_column("fiscal_profiles", "numero_exterior")
    op.drop_column("fiscal_profiles", "calle")

    # 2. Tickets
    op.drop_column("tickets", "cfdi_disponible_hasta")
    op.alter_column("tickets", "correo_capturado_en_portal", new_column_name="cfdi_sent_to")

    # 1. Merchants
    op.drop_column("merchants", "entrega_esperada")
