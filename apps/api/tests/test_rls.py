import uuid
from decimal import Decimal
import pytest
from cryptography.exceptions import InvalidTag
from sqlalchemy import select, text
from sqlalchemy.exc import DBAPIError
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from src.models import TENANT_TABLES, Ticket, TicketEstado
from src.security import encrypt_credentials, decrypt_credentials


@pytest.mark.asyncio
async def test_1_tenant_tables_have_rls_and_force_enabled(app_engine):
    """
    PRUEBA 1: Toda tabla por tenant tiene relrowsecurity y relforcerowsecurity en true.
    Recorre exhaustivamente la lista de tablas por tenant.
    """
    async with app_engine.connect() as conn:
        result = await conn.execute(
            text("""
                SELECT c.relname, c.relrowsecurity, c.relforcerowsecurity
                FROM pg_class c
                JOIN pg_namespace n ON n.oid = c.relnamespace
                WHERE n.nspname = 'public' AND c.relname = ANY(:tables)
            """),
            {"tables": TENANT_TABLES},
        )
        rows = result.fetchall()
        checked_tables = {row[0]: (row[1], row[2]) for row in rows}

    assert len(checked_tables) == len(TENANT_TABLES), (
        f"Faltan tablas por verificar. Esperadas: {TENANT_TABLES}, Encontradas: {list(checked_tables.keys())}"
    )

    for table_name in TENANT_TABLES:
        assert table_name in checked_tables, f"Tabla {table_name} no encontrada en PostgreSQL"
        has_rls, has_force = checked_tables[table_name]
        assert has_rls is True, f"Tabla {table_name} NO tiene relrowsecurity = true"
        assert has_force is True, f"Tabla {table_name} NO tiene relforcerowsecurity = true"


@pytest.mark.asyncio
async def test_2_tenant_cannot_see_other_tenants_tickets(app_engine, setup_tenants):
    """
    PRUEBA 2: Un tenant no ve los tickets del otro.
    Verificado conectando con facturia_app (rol no-dueño).
    """
    t_a = setup_tenants["tenant_a_id"]
    t_b = setup_tenants["tenant_b_id"]
    ticket_a = setup_tenants["ticket_a_id"]
    ticket_b = setup_tenants["ticket_b_id"]

    session_factory = async_sessionmaker(bind=app_engine, class_=AsyncSession, expire_on_commit=False)

    # 1. Consulta como Tenant A
    async with session_factory() as session:
        async with session.begin():
            await session.execute(text("SELECT set_config('app.tenant_id', :t, true)"), {"t": str(t_a)})
            result = await session.execute(select(Ticket))
            tickets = result.scalars().all()
            ticket_ids = [t.id for t in tickets]

            assert ticket_a in ticket_ids, "Tenant A debe ver su propio ticket"
            assert ticket_b not in ticket_ids, "Tenant A NO debe ver el ticket de Tenant B"

    # 2. Consulta como Tenant B
    async with session_factory() as session:
        async with session.begin():
            await session.execute(text("SELECT set_config('app.tenant_id', :t, true)"), {"t": str(t_b)})
            result = await session.execute(select(Ticket))
            tickets = result.scalars().all()
            ticket_ids = [t.id for t in tickets]

            assert ticket_b in ticket_ids, "Tenant B debe ver su propio ticket"
            assert ticket_a not in ticket_ids, "Tenant B NO debe ver el ticket de Tenant A"


@pytest.mark.asyncio
async def test_3_no_tenant_id_sees_zero_rows_without_error(app_engine, setup_tenants):
    """
    PRUEBA 3: Sin app.tenant_id se ven CERO filas, sin lanzar error.
    Valida el funcionamiento de nullif(current_setting('app.tenant_id', true), '')::uuid.
    """
    session_factory = async_sessionmaker(bind=app_engine, class_=AsyncSession, expire_on_commit=False)

    # Caso A: app.tenant_id nunca se definió
    async with session_factory() as session:
        async with session.begin():
            result = await session.execute(select(Ticket))
            tickets = result.scalars().all()
            assert len(tickets) == 0, "Sin fijar app.tenant_id se deben ver 0 tickets"

    # Caso B: app.tenant_id se fijó explícitamente a cadena vacía ''
    async with session_factory() as session:
        async with session.begin():
            await session.execute(text("SELECT set_config('app.tenant_id', '', true)"))
            result = await session.execute(select(Ticket))
            tickets = result.scalars().all()
            assert len(tickets) == 0, "Con app.tenant_id vacío se deben ver 0 tickets"


@pytest.mark.asyncio
async def test_4_cannot_insert_row_with_another_tenant_id(app_engine, setup_tenants):
    """
    PRUEBA 4: No se puede insertar una fila con el tenant_id de otro.
    El WITH CHECK de la policy de RLS debe rechazar la inserción con error de violación de policy.
    """
    t_a = setup_tenants["tenant_a_id"]
    t_b = setup_tenants["tenant_b_id"]
    u_id = setup_tenants["user_id"]
    m_id = setup_tenants["merchant_id"]

    session_factory = async_sessionmaker(bind=app_engine, class_=AsyncSession, expire_on_commit=False)

    async with session_factory() as session:
        async with session.begin():
            # Conexión autenticada como Tenant A
            await session.execute(text("SELECT set_config('app.tenant_id', :t, true)"), {"t": str(t_a)})

            # Intento fraudulento de insertar un ticket para Tenant B
            malicious_ticket = Ticket(
                id=uuid.uuid4(),
                tenant_id=t_b,  # Tenant B
                created_by=u_id,
                merchant_id=m_id,
                estado=TicketEstado.RECIBIDO,
                folio="MALICIOUS-001",
                total=Decimal("999.99"),
            )
            session.add(malicious_ticket)

            with pytest.raises(DBAPIError) as exc_info:
                await session.flush()

            # PostgreSQL lanza error 42501 (insufficient_privilege) por violación de RLS WITH CHECK
            error_message = str(exc_info.value).lower()
            assert "row-level security policy" in error_message or "tenant_isolation_policy" in error_message


@pytest.mark.asyncio
async def test_5_update_cannot_move_row_to_another_tenant(app_engine, setup_tenants):
    """
    PRUEBA 5: Un UPDATE no puede mover una fila a otro tenant.
    El WITH CHECK de la policy RLS impide cambiar el tenant_id de una fila existente.
    """
    t_a = setup_tenants["tenant_a_id"]
    t_b = setup_tenants["tenant_b_id"]
    ticket_a = setup_tenants["ticket_a_id"]

    session_factory = async_sessionmaker(bind=app_engine, class_=AsyncSession, expire_on_commit=False)

    async with session_factory() as session:
        async with session.begin():
            # Autenticado como Tenant A
            await session.execute(text("SELECT set_config('app.tenant_id', :t, true)"), {"t": str(t_a)})

            # Intentar mover el ticket de A al tenant B
            result = await session.execute(select(Ticket).where(Ticket.id == ticket_a))
            ticket = result.scalar_one()
            ticket.tenant_id = t_b

            with pytest.raises(DBAPIError) as exc_info:
                await session.flush()

            error_message = str(exc_info.value).lower()
            assert "row-level security policy" in error_message or "tenant_isolation_policy" in error_message


@pytest.mark.asyncio
async def test_6_nonexistent_tenant_sees_nothing(app_engine, setup_tenants):
    """
    PRUEBA 6: Un tenant_id inexistente (UUID válido que no existe o no tiene datos) no ve nada.
    """
    non_existent_tenant = uuid.uuid4()
    session_factory = async_sessionmaker(bind=app_engine, class_=AsyncSession, expire_on_commit=False)

    async with session_factory() as session:
        async with session.begin():
            await session.execute(text("SELECT set_config('app.tenant_id', :t, true)"), {"t": str(non_existent_tenant)})
            result = await session.execute(select(Ticket))
            tickets = result.scalars().all()
            assert len(tickets) == 0


def test_7_decrypt_with_another_tenant_key_fails():
    """
    PRUEBA 7: Descifrar con la clave de otro tenant falla.
    Regla 6: Cifrado AES-256-GCM derivado con HKDF y AAD por tenant_id.
    """
    tenant_a = uuid.uuid4()
    tenant_b = uuid.uuid4()
    secret_payload = b"api-key-secreta-oxxo-portal-12345"

    # Cifrar para Tenant A
    ciphertext, nonce = encrypt_credentials(tenant_a, secret_payload)

    # Descifrado legítimo por Tenant A
    decrypted = decrypt_credentials(tenant_a, ciphertext, nonce)
    assert decrypted == secret_payload

    # Intento ilegítimo de descifrado con la identidad de Tenant B
    with pytest.raises(InvalidTag):
        decrypt_credentials(tenant_b, ciphertext, nonce)
