"""0006 - Fiscal classification, tax breakdown, and SAT deducibility status on tickets

Revision ID: 0006_fiscal_classification
Revises: 0005_handoff_token_hash
Create Date: 2026-09-14 23:45:00

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects.postgresql import JSONB


revision: str = "0006_fiscal_classification"
down_revision: Union[str, None] = "0005_handoff_token_hash"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column(
        "tickets",
        sa.Column("categoria_gasto", sa.String(length=50), nullable=True),
    )
    op.add_column(
        "tickets",
        sa.Column("desglose_impuestos", JSONB(astext_type=sa.Text()), nullable=True),
    )
    op.add_column(
        "tickets",
        sa.Column("estatus_deducibilidad", sa.String(length=50), nullable=True),
    )
    op.create_index(
        "idx_tickets_categoria_gasto",
        "tickets",
        ["tenant_id", "categoria_gasto"],
        unique=False,
    )


def downgrade() -> None:
    op.drop_index("idx_tickets_categoria_gasto", table_name="tickets")
    op.drop_column("tickets", "estatus_deducibilidad")
    op.drop_column("tickets", "desglose_impuestos")
    op.drop_column("tickets", "categoria_gasto")
