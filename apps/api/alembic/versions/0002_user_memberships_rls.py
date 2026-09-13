"""0002 - Add user memberships select policy for authentication

Revision ID: 0002_user_memberships_rls
Revises: 0001_initial_schema
Create Date: 2026-09-13 16:36:00

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa

revision: str = "0002_user_memberships_rls"
down_revision: Union[str, None] = "0001_initial_schema"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    # Agrega una policy SOLO DE SELECT sobre memberships para que un usuario
    # recién autenticado pueda consultar a qué tenants pertenece sin tener un tenant_id fijado.
    # Se evalúa con OR junto a tenant_isolation_policy para SELECT.
    # Es estrictamente FOR SELECT para impedir auto-asignación de membresías (sin WITH CHECK).
    op.execute("""
        CREATE POLICY propias_membresias ON memberships FOR SELECT
            USING (user_id = nullif(current_setting('app.user_id', true), '')::uuid);
    """)


def downgrade() -> None:
    op.execute("DROP POLICY IF EXISTS propias_membresias ON memberships;")
