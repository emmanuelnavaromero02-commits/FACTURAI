import asyncio
import io
import uuid
from datetime import date, datetime
from decimal import Decimal
import pytest
from httpx import ASGITransport, AsyncClient
from sqlalchemy import select, text
from sqlalchemy.ext.asyncio import AsyncSession

from src.config import get_settings
from src.db import tenant_session
from src.main import app
from src.models import (
    Membership,
    MembershipRole,
    Merchant,
    Tenant,
    Ticket,
    TicketEstado,
    TipoMotor,
    User,
)
from src.services.upload import MAX_FILE_SIZE_BYTES
from src.state_machine import TransicionInvalida, transition
from src.storage import InMemoryStorageService, set_storage_service
from src.vision.extractor import (
    FakeVisionExtractor,
    VisionExtractionSchema,
    set_vision_extractor,
)
from src.vision.merchant_matcher import match_merchant_cascade
from src.vision.normalizer import normalize_amount, normalize_date
from src.worker import process_ticket_extraction

settings = get_settings()

VALID_JPEG_HEADER = b"\xff\xd8\xff\xe0\x00\x10JFIF\x00\x01\x01\x01\x00`\x00`\x00\x00" + b"\x00" * 100


@pytest.fixture(autouse=True)
def configure_test_services():
    """Configura storage en memoria y vision extractor simulado para los tests."""
    storage = InMemoryStorageService()
    set_storage_service(storage)
    vision = FakeVisionExtractor()
    set_vision_extractor(vision)
    return storage, vision


@pytest.mark.asyncio
async def test_state_machine_valid_and_invalid_transitions(owner_session: AsyncSession):
    """
    PRUEBA: Toda transición inválida levanta TransicionInvalida.
    Nadie puede saltarse estados a mano.
    """
    t_id = uuid.uuid4()
    u_id = uuid.uuid4()
    ticket_id = uuid.uuid4()

    tenant = Tenant(id=t_id, nombre="Empresa SM", slug=f"sm-{t_id.hex[:6]}", plan="free")
    user = User(id=u_id, email=f"sm-{u_id.hex[:6]}@test.com", nombre="User SM", google_sub=f"sub-{u_id.hex}")
    owner_session.add_all([tenant, user])
    await owner_session.flush()

    ticket = Ticket(
        id=ticket_id,
        tenant_id=t_id,
        created_by=u_id,
        estado=TicketEstado.RECIBIDO,
    )
    await owner_session.execute(text("SELECT set_config('app.tenant_id', :t, true)"), {"t": str(t_id)})
    owner_session.add(ticket)
    await owner_session.commit()

    # Transición válida: recibido -> extrayendo
    await owner_session.execute(text("SELECT set_config('app.tenant_id', :t, true)"), {"t": str(t_id)})
    ev1 = await transition(owner_session, ticket, TicketEstado.EXTRAYENDO, "Extrayendo datos")
    assert ticket.estado == TicketEstado.EXTRAYENDO
    assert ev1.tipo == "cambio_estado"

    # Transición inválida: extrayendo -> facturado (debe pasar por extraido -> encolado -> facturando)
    with pytest.raises(TransicionInvalida):
        await transition(owner_session, ticket, TicketEstado.FACTURADO, "Salto prohibido")

    # Transición inválida: extrayendo -> recibido (no se regresa a recibido)
    with pytest.raises(TransicionInvalida):
        await transition(owner_session, ticket, TicketEstado.RECIBIDO, "Regreso prohibido")

    # Transición válida a rechazado desde extrayendo
    ev2 = await transition(owner_session, ticket, TicketEstado.RECHAZADO, "Rechazado")
    assert ticket.estado == TicketEstado.RECHAZADO

    # Transición inválida desde un estado final (rechazado es final)
    with pytest.raises(TransicionInvalida):
        await transition(owner_session, ticket, TicketEstado.EXTRAIDO, "Revivir no permitido")

    with pytest.raises(TransicionInvalida):
        await transition(owner_session, ticket, TicketEstado.CANCELADO, "Cancelar estado final")


@pytest.mark.asyncio
async def test_file_with_fake_extension_rejected_by_magic_bytes():
    """
    PRUEBA: Un archivo que dice ser JPEG pero no lo es, es rechazado por magic bytes.
    No se confía en la extensión ni en el Content-Type.
    """
    fake_jpeg_content = b"ESTO NO ES UN JPEG REAL, ES TEXTO PLANO"
    files = {"file": ("ticket.jpg", io.BytesIO(fake_jpeg_content), "image/jpeg")}

    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        # Login
        unique_email = f"magic.{uuid.uuid4().hex[:8]}@test.com"
        log_res = await client.post("/v1/auth/google", json={"id_token": f"mock:{unique_email}"})
        cookie = log_res.cookies.get(settings.SESSION_COOKIE_NAME)
        tenant_id = log_res.json()["created_tenant_id"]

    async with AsyncClient(transport=transport, base_url="http://test", cookies={settings.SESSION_COOKIE_NAME: cookie}) as auth_client:
        response = await auth_client.post(
            "/v1/tickets",
            files=files,
            headers={"X-Tenant-Id": tenant_id},
        )

    assert response.status_code == 400
    data = response.json()
    assert data["error"]["code"] == "tipo_archivo_no_soportado"
    assert "formato" in data["error"]["message"].lower()


@pytest.mark.asyncio
async def test_file_over_10mb_is_rejected():
    """
    PRUEBA: Un archivo de 11 MB es rechazado.
    """
    # 11 MB con cabecera JPEG válida al inicio
    eleven_mb_bytes = VALID_JPEG_HEADER + b"\x00" * (11 * 1024 * 1024)
    files = {"file": ("huge_ticket.jpg", io.BytesIO(eleven_mb_bytes), "image/jpeg")}

    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        unique_email = f"heavy.{uuid.uuid4().hex[:8]}@test.com"
        log_res = await client.post("/v1/auth/google", json={"id_token": f"mock:{unique_email}"})
        cookie = log_res.cookies.get(settings.SESSION_COOKIE_NAME)
        tenant_id = log_res.json()["created_tenant_id"]

    async with AsyncClient(transport=transport, base_url="http://test", cookies={settings.SESSION_COOKIE_NAME: cookie}) as auth_client:
        response = await auth_client.post(
            "/v1/tickets",
            files=files,
            headers={"X-Tenant-Id": tenant_id},
        )

    assert response.status_code == 400
    data = response.json()
    assert data["error"]["code"] == "archivo_demasiado_grande"


def test_amount_and_date_normalization():
    """
    PRUEBA: '1,284.50' se guarda como Decimal('1284.50') y fechas mexicanas se normalizan.
    """
    assert normalize_amount("1,284.50") == Decimal("1284.50")
    assert normalize_amount("$1284.50") == Decimal("1284.50")
    assert normalize_amount("1 284,50") == Decimal("1284.50")
    assert normalize_amount("1.284,50") == Decimal("1284.50")
    assert normalize_amount("$ 1,284.50 MXN") == Decimal("1284.50")
    assert normalize_amount(1284.5) == Decimal("1284.50")

    # Fechas mexicanas: preferir DD/MM sobre MM/DD
    assert normalize_date("13/09/2026") == date(2026, 9, 13)
    assert normalize_date("04/05/2026") == date(2026, 5, 4)
    assert normalize_date("2026-09-13") == date(2026, 9, 13)


def test_merchant_cascade_levels():
    """
    PRUEBA: La cascada de identificación acierta en cada uno de sus 4 niveles.
    """
    m1 = Merchant(
        id=uuid.uuid4(),
        slug="oxxo",
        nombre="Cadena Comercial OXXO",
        tipo_motor=TipoMotor.WEB,
        engine_slug="oxxo_web",
        patrones=["oxxo.com", "CCO8605231N4", "OXXO"],
        activo=True,
    )
    m2 = Merchant(
        id=uuid.uuid4(),
        slug="walmart",
        nombre="Nueva Wal-Mart de México",
        tipo_motor=TipoMotor.API,
        engine_slug="walmart_api",
        patrones=["walmartmexico.com.mx", "NWM9709244W4", "WALMART"],
        activo=True,
    )
    merchants = [m1, m2]

    # Nivel a: Por merchant_slug explícito
    assert match_merchant_cascade(merchants, explicit_slug="oxxo") == m1

    # Nivel b: Por host del QR o URL impresa
    assert match_merchant_cascade(merchants, qr_url="https://factura.oxxo.com/portal/ticket") == m1
    assert match_merchant_cascade(merchants, printed_url="https://www.walmartmexico.com.mx/facturas") == m2

    # Nivel c: Por RFC del emisor contra patrones
    assert match_merchant_cascade(merchants, rfc_emisor="CCO8605231N4") == m1
    assert match_merchant_cascade(merchants, rfc_emisor="NWM9709244W4") == m2

    # Nivel d: Por coincidencia / regex de nombre comercial
    assert match_merchant_cascade(merchants, comercio_nombre="TIENDA OXXO SUCURSAL COAPA") == m1
    assert match_merchant_cascade(merchants, comercio_nombre="WALMART SUPERCENTER TAXQUEÑA") == m2

    # Si ninguno coincide -> None
    assert match_merchant_cascade(merchants, comercio_nombre="Puesto de tacos Don Chuy") is None


@pytest.mark.asyncio
async def test_vision_fixtures_outcomes(owner_session: AsyncSession):
    """
    PRUEBA: Cada fixture de visión produce el estado y error_code esperados.
    - bueno -> extraido
    - borroso -> rechazado (imagen_ilegible)
    - sin_folio -> rechazado (imagen_ilegible)
    - comercio_desconocido -> extraido (error_code='comercio_desconocido')
    """
    storage = InMemoryStorageService()
    set_storage_service(storage)
    vision = FakeVisionExtractor()
    set_vision_extractor(vision)

    t_id = uuid.uuid4()
    u_id = uuid.uuid4()

    tenant = Tenant(id=t_id, nombre="Empresa Fixtures", slug=f"fix-{t_id.hex[:6]}", plan="free")
    user = User(id=u_id, email=f"user-{u_id.hex[:6]}@test.com", nombre="User", google_sub=f"sub-{u_id.hex}")
    m_res = await owner_session.execute(select(Merchant).where(Merchant.slug == "oxxo"))
    merchant = m_res.scalar_one_or_none()
    if not merchant:
        merchant = Merchant(
            id=uuid.uuid4(),
            slug="oxxo",
            nombre="Cadena Comercial Oxxo",
            tipo_motor=TipoMotor.WEB,
            engine_slug="oxxo_web",
            patrones=["oxxo.com", "CCO8605231N4", "Oxxo"],
            activo=True,
        )
        owner_session.add(merchant)
    owner_session.add_all([tenant, user])
    await owner_session.flush()

    cases = [
        ("bueno", TicketEstado.EXTRAIDO, None),
        ("borroso", TicketEstado.RECHAZADO, "imagen_ilegible"),
        ("sin_folio", TicketEstado.RECHAZADO, "imagen_ilegible"),
        ("comercio_desconocido", TicketEstado.EXTRAIDO, "comercio_desconocido"),
    ]

    for mode, expected_estado, expected_error in cases:
        ticket_id = uuid.uuid4()
        image_key = f"tickets/{t_id}/{ticket_id}"
        await storage.upload_bytes(image_key, VALID_JPEG_HEADER, "image/jpeg")

        ticket = Ticket(
            id=ticket_id,
            tenant_id=t_id,
            created_by=u_id,
            estado=TicketEstado.RECIBIDO,
            image_key=image_key,
        )
        await owner_session.execute(text("SELECT set_config('app.tenant_id', :t, true)"), {"t": str(t_id)})
        owner_session.add(ticket)
        await owner_session.commit()

        # Configurar modo en el fake vision extractor
        vision.set_mode(mode)

        # Ejecutar worker
        await process_ticket_extraction(t_id, ticket_id)

        # Verificar resultado en tenant_session
        async with tenant_session(t_id) as session:
            res = await session.execute(select(Ticket).where(Ticket.id == ticket_id))
            processed_ticket = res.scalar_one()

            assert processed_ticket.estado == expected_estado, f"Modo {mode} debió resultar en {expected_estado}"
            assert processed_ticket.error_code == expected_error, f"Modo {mode} debió tener error {expected_error}"


@pytest.mark.asyncio
async def test_sse_stream_leaves_zero_idle_in_transaction(app_engine):
    """
    PRUEBA: El SSE no deja transacciones abiertas (Trampa 2 evitada).
    Verifica que cada ciclo abre y cierra su transacción corta y no deja conexiones
    'idle in transaction' en PostgreSQL mientras dura el stream.
    Cierra limpiamente al llegar a un estado final.
    """
    transport = ASGITransport(app=app)
    unique_email = f"sse.{uuid.uuid4().hex[:8]}@test.com"
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        log_res = await client.post("/v1/auth/google", json={"id_token": f"mock:{unique_email}"})
        cookie = log_res.cookies.get(settings.SESSION_COOKIE_NAME)
        tenant_id = uuid.UUID(log_res.json()["created_tenant_id"])

    # Subir un ticket y cambiar a estado final
    ticket_id = uuid.uuid4()
    storage = InMemoryStorageService()
    set_storage_service(storage)
    await storage.upload_bytes(f"tickets/{tenant_id}/{ticket_id}", VALID_JPEG_HEADER, "image/jpeg")

    async with tenant_session(tenant_id) as session:
        user_res = await session.execute(select(Membership.user_id).where(Membership.tenant_id == tenant_id))
        user_id = user_res.scalar_one()

        ticket = Ticket(
            id=ticket_id,
            tenant_id=tenant_id,
            created_by=user_id,
            estado=TicketEstado.RECIBIDO,
            image_key=f"tickets/{tenant_id}/{ticket_id}",
        )
        session.add(ticket)
        await transition(session, ticket, TicketEstado.CANCELADO, "Ticket cancelado para test stream")

    # Iniciar stream SSE y verificar respuesta
    async with AsyncClient(transport=transport, base_url="http://test", cookies={settings.SESSION_COOKIE_NAME: cookie}) as auth_client:
        response = await auth_client.get(f"/v1/tickets/{ticket_id}/stream", headers={"X-Tenant-Id": str(tenant_id)})
        assert response.status_code == 200
        content = response.text
        assert ": connected" in content
        assert "estado_final" in content
        assert "cambio_estado" in content

    # Comprobar conexiones idle in transaction en PostgreSQL tras consumir el stream
    async with app_engine.connect() as conn:
        res = await conn.execute(
            text("SELECT count(*) FROM pg_stat_activity WHERE datname = 'facturia' AND state = 'idle in transaction'")
        )
        idle_count = res.scalar()
        assert idle_count == 0, f"Quedaron conexiones en 'idle in transaction': {idle_count}"
