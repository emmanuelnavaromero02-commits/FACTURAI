import os
os.environ["ENVIRONMENT"] = "test"

import uuid
from decimal import Decimal
from typing import AsyncGenerator, Dict, Any
import pytest
import pytest_asyncio
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine
from sqlalchemy.pool import NullPool

from src.auth import FakeGoogleTokenVerifier, set_google_verifier, sign_session_token
from src.config import get_settings
from src.models import (
    FiscalProfile,
    Membership,
    MembershipRole,
    Merchant,
    Tenant,
    Ticket,
    TicketEstado,
    TipoMotor,
    User,
)

settings = get_settings()
settings.ENVIRONMENT = "test"


from src.vision.extractor import FakeVisionExtractor, set_vision_extractor

@pytest.fixture(autouse=True)
def setup_fake_verifier():
    """Configura el verificador falso de Google para todas las pruebas."""
    fake_verifier = FakeGoogleTokenVerifier()
    set_google_verifier(fake_verifier)
    yield fake_verifier


@pytest.fixture(autouse=True)
def setup_fake_vision():
    """Configura el extractor de visión simulado por defecto para evitar llamadas externas lentas."""
    fake_vision = FakeVisionExtractor()
    set_vision_extractor(fake_vision)
    yield fake_vision


async def is_postgres_available() -> bool:
    """Verifica si PostgreSQL está disponible para las pruebas."""
    try:
        engine = create_async_engine(settings.DATABASE_URL, poolclass=NullPool)
        async with engine.connect() as conn:
            await conn.execute(text("SELECT 1"))
        await engine.dispose()
        return True
    except Exception:
        return False


@pytest_asyncio.fixture(scope="function")
async def db_check():
    """Omite las pruebas con mensaje descriptivo si PostgreSQL no está disponible."""
    available = await is_postgres_available()
    if not available:
        pytest.skip("PostgreSQL no está disponible en localhost:5432")


@pytest_asyncio.fixture(scope="function")
async def owner_session(db_check) -> AsyncGenerator[AsyncSession, None]:
    """
    Sesión asíncrona conectada como facturia_owner con NullPool y alcance de función.
    Utilizada para el montaje y desmontaje de fixtures de prueba.
    """
    engine = create_async_engine(settings.DATABASE_MIGRATION_URL, poolclass=NullPool)
    session_factory = async_sessionmaker(bind=engine, class_=AsyncSession, expire_on_commit=False)
    async with session_factory() as session:
        yield session
    await engine.dispose()


@pytest_asyncio.fixture(scope="function")
async def app_engine(db_check):
    """
    Engine asíncrono conectado como facturia_app con NullPool y alcance de función.
    El sujeto de prueba para verificar que RLS se impone obligatoriamente.
    """
    engine = create_async_engine(settings.DATABASE_URL, poolclass=NullPool)
    yield engine
    await engine.dispose()


@pytest_asyncio.fixture(scope="function")
async def setup_tenants(owner_session: AsyncSession) -> AsyncGenerator[Dict[str, Any], None]:
    """
    Monta el escenario de prueba con dos tenants (A y B) usando facturia_owner.
    Inserta un usuario, un comercio del catálogo, y tickets para cada tenant.
    """
    t_a_id = uuid.uuid4()
    t_b_id = uuid.uuid4()
    u_id = uuid.uuid4()
    m_id = uuid.uuid4()
    ticket_a_id = uuid.uuid4()
    ticket_b_id = uuid.uuid4()

    # 1. Crear tenants
    tenant_a = Tenant(id=t_a_id, nombre="Tenant Alfa S.A.", slug=f"alfa-{t_a_id.hex[:6]}", plan="pro")
    tenant_b = Tenant(id=t_b_id, nombre="Tenant Beta S.A.", slug=f"beta-{t_b_id.hex[:6]}", plan="free")
    owner_session.add_all([tenant_a, tenant_b])

    # 2. Crear usuario global
    user = User(
        id=u_id,
        email=f"user-{u_id.hex[:6]}@test.com",
        nombre="Usuario Prueba",
        google_sub=f"sub-{u_id.hex}",
    )
    owner_session.add(user)

    # 3. Crear comercio global
    merchant = Merchant(
        id=m_id,
        slug=f"oxxo-{m_id.hex[:6]}",
        nombre="Oxxo Comercial",
        tipo_motor=TipoMotor.WEB,
        engine_slug="oxxo_web",
        patrones=["oxxo"],
        config={},
    )
    owner_session.add(merchant)
    await owner_session.flush()

    # 4. Crear tickets para Tenant A y Tenant B fijando su respectivo tenant_id
    await owner_session.execute(text("SELECT set_config('app.tenant_id', :t, true)"), {"t": str(t_a_id)})
    ticket_a = Ticket(
        id=ticket_a_id,
        tenant_id=t_a_id,
        created_by=u_id,
        merchant_id=m_id,
        estado=TicketEstado.RECIBIDO,
        folio="FOLIO-A-001",
        total=Decimal("150.50"),
    )
    owner_session.add(ticket_a)
    await owner_session.flush()

    await owner_session.execute(text("SELECT set_config('app.tenant_id', :t, true)"), {"t": str(t_b_id)})
    ticket_b = Ticket(
        id=ticket_b_id,
        tenant_id=t_b_id,
        created_by=u_id,
        merchant_id=m_id,
        estado=TicketEstado.RECIBIDO,
        folio="FOLIO-B-001",
        total=Decimal("300.00"),
    )
    owner_session.add(ticket_b)
    await owner_session.flush()
    await owner_session.commit()

    context_data = {
        "tenant_a_id": t_a_id,
        "tenant_b_id": t_b_id,
        "user_id": u_id,
        "merchant_id": m_id,
        "ticket_a_id": ticket_a_id,
        "ticket_b_id": ticket_b_id,
    }

    yield context_data

    # Desmontaje: limpiar datos fijando tenant_id correspondiente
    await owner_session.rollback()
    await owner_session.execute(text("SELECT set_config('app.tenant_id', :t, true)"), {"t": str(t_a_id)})
    await owner_session.execute(text("DELETE FROM tickets WHERE tenant_id = :t"), {"t": t_a_id})

    await owner_session.execute(text("SELECT set_config('app.tenant_id', :t, true)"), {"t": str(t_b_id)})
    await owner_session.execute(text("DELETE FROM tickets WHERE tenant_id = :t"), {"t": t_b_id})

    await owner_session.execute(text("DELETE FROM tenants WHERE id IN (:a, :b)"), {"a": t_a_id, "b": t_b_id})
    await owner_session.execute(text("DELETE FROM users WHERE id = :u"), {"u": u_id})
    await owner_session.execute(text("DELETE FROM merchants WHERE id = :m"), {"m": m_id})
    await owner_session.commit()


@pytest_asyncio.fixture(scope="function")
async def setup_phase4_scenario(owner_session: AsyncSession) -> Dict[str, Any]:
    """Crea tenant, usuario, membresía, comercio mock y perfil fiscal en la BD."""
    t_id = uuid.uuid4()
    u_id = uuid.uuid4()
    m_id = uuid.uuid4()
    fp_id = uuid.uuid4()

    tenant = Tenant(
        id=t_id,
        nombre="Factura Corp SA",
        slug=f"factura-corp-{t_id.hex[:6]}",
        plan="pro",
    )
    user = User(
        id=u_id,
        email=f"owner-{u_id.hex[:6]}@factura.com",
        nombre="Dueño Factura",
        google_sub=f"google-sub-{u_id.hex[:8]}",
    )
    owner_session.add_all([tenant, user])
    await owner_session.flush()

    # Membresía owner
    await owner_session.execute(text("SELECT set_config('app.tenant_id', :t, true)"), {"t": str(t_id)})
    membership = Membership(
        tenant_id=t_id,
        user_id=u_id,
        rol=MembershipRole.OWNER,
    )
    owner_session.add(membership)

    # Catálogo de comercio con motor mock y entrega_esperada 'emisor'
    merchant = Merchant(
        id=m_id,
        nombre="Comercio Simulado OXXO",
        slug=f"oxxo-mock-{m_id.hex[:6]}",
        tipo_motor=TipoMotor.API,
        engine_slug="mock",
        entrega_esperada="emisor",
        activo=True,
    )
    owner_session.add(merchant)

    # Perfil fiscal principal con constancia fiscal completa
    fiscal_profile = FiscalProfile(
        id=fp_id,
        tenant_id=t_id,
        razon_social="EMPRESA PRUEBA SA DE CV",
        rfc="XAXX010101000",
        cp="01000",
        regimen_fiscal="601",
        email_receptor="facturas@receptor.com",
        calle="Av. Insurgentes Sur",
        numero_exterior="1602",
        numero_interior="Piso 4",
        colonia="Crédito Constructor",
        municipio_alcaldia="Benito Juárez",
        estado="Ciudad de México",
        pais="MEX",
        curp="XAXX010101HDFRRN01",
        es_principal=True,
    )
    owner_session.add(fiscal_profile)

    await owner_session.commit()

    session_token = sign_session_token(u_id, user.email)

    return {
        "tenant_id": t_id,
        "user_id": u_id,
        "merchant_id": m_id,
        "fiscal_profile_id": fp_id,
        "user_email": user.email,
        "session_token": session_token,
        "merchant": merchant,
        "fiscal_profile": fiscal_profile,
    }

