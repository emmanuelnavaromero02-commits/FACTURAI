"""0007 - Fiscal auditor Anexo 20, Art 69-B risk scores, and SHA-256 integrity hash on tickets

Revision ID: 0007_fiscal_auditor_risk
Revises: 0006_fiscal_classification
Create Date: 2026-09-15 03:30:00

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects.postgresql import JSONB


revision: str = "0007_fiscal_auditor_risk"
down_revision: Union[str, None] = "0006_fiscal_classification"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column(
        "tickets",
        sa.Column("score_riesgo_fiscal", sa.Integer(), nullable=True),
    )
    op.add_column(
        "tickets",
        sa.Column("auditoria_aritmetica", JSONB(astext_type=sa.Text()), nullable=True),
    )
    op.add_column(
        "tickets",
        sa.Column("hash_integridad", sa.String(length=64), nullable=True),
    )
    op.create_index(
        "idx_tickets_hash_integridad",
        "tickets",
        ["tenant_id", "hash_integridad"],
        unique=False,
    )


def downgrade() -> None:
    op.drop_index("idx_tickets_hash_integridad", table_name="tickets")
    op.drop_column("tickets", "hash_integridad")
    op.drop_column("tickets", "auditoria_aritmetica")
    op.drop_column("tickets", "score_riesgo_fiscal")
