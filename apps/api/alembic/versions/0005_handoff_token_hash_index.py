"""0005 - Index on handoff_sessions token_hash

Revision ID: 0005_handoff_token_hash
Revises: 0004_generic_web_cost
Create Date: 2026-09-13 21:40:00

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


revision: str = "0005_handoff_token_hash"
down_revision: Union[str, None] = "0004_generic_web_cost"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_index(
        "idx_handoff_sessions_token_hash",
        "handoff_sessions",
        ["token_hash"],
        unique=False,
    )
    op.create_index(
        "idx_handoff_sessions_ticket_id",
        "handoff_sessions",
        ["ticket_id"],
        unique=False,
    )
    op.execute("ALTER TABLE handoff_sessions FORCE ROW LEVEL SECURITY;")
    op.execute("""
        CREATE POLICY handoff_owner_admin_policy ON handoff_sessions
            FOR ALL
            TO facturia_owner
            USING (true)
            WITH CHECK (true);
    """)


def downgrade() -> None:
    op.execute("DROP POLICY IF EXISTS handoff_owner_admin_policy ON handoff_sessions;")
    op.drop_index("idx_handoff_sessions_ticket_id", table_name="handoff_sessions")
    op.drop_index("idx_handoff_sessions_token_hash", table_name="handoff_sessions")
