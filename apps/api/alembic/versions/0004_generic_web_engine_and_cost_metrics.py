"""0004 - Generic web engine url_facturacion and cost metrics

Revision ID: 0004_generic_web_engine_and_cost_metrics
Revises: 0003_fiscal_profile_cfdi
Create Date: 2026-09-13 20:30:00

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


revision: str = "0004_generic_web_cost"
down_revision: Union[str, None] = "0003_fiscal_profile_cfdi"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    # Columnas para motor genérico y medición de costos en tickets
    op.add_column(
        "tickets",
        sa.Column("url_facturacion", sa.String(length=500), nullable=True),
    )
    op.add_column(
        "tickets",
        sa.Column("costo_total_usd", sa.Numeric(precision=10, scale=5), nullable=True),
    )
    op.add_column(
        "tickets",
        sa.Column("tokens_input_total", sa.Integer(), nullable=True),
    )
    op.add_column(
        "tickets",
        sa.Column("tokens_output_total", sa.Integer(), nullable=True),
    )
    op.add_column(
        "tickets",
        sa.Column("pasos_agente", sa.Integer(), nullable=True),
    )
    op.add_column(
        "tickets",
        sa.Column("duracion_segundos", sa.Numeric(precision=8, scale=2), nullable=True),
    )


def downgrade() -> None:
    op.drop_column("tickets", "duracion_segundos")
    op.drop_column("tickets", "pasos_agente")
    op.drop_column("tickets", "tokens_output_total")
    op.drop_column("tickets", "tokens_input_total")
    op.drop_column("tickets", "costo_total_usd")
    op.drop_column("tickets", "url_facturacion")
