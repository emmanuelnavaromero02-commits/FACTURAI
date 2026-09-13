import asyncio
import io
import uuid
from decimal import Decimal
from typing import Any, Dict
import pytest
import pytest_asyncio
from httpx import ASGITransport, AsyncClient
from pydantic import ValidationError
from sqlalchemy import select, text
from sqlalchemy.ext.asyncio import AsyncSession

from src.auth import (
    FakeGoogleTokenVerifier,
    ProductionGoogleTokenVerifier,
    set_google_verifier,
    sign_session_token,
)
from src.config import Settings, get_settings
from src.db import tenant_session
from src.engines.base import get_engine
from src.engines.mock import MockFacturacionEngine
from src.main import app
from src.models import (
    FiscalProfile,
    Membership,
    MembershipRole,
    Merchant,
    Tenant,
    Ticket,
    TicketEstado,
    TicketEvent,
    TipoMotor,
    User,
)
from src.services.email import set_email_sender
from src.state_machine import transition
from src.storage import InMemoryStorageService, set_storage_service
from src.worker import process_ticket_facturacion

settings = get_settings()


@pytest.fixture(autouse=True)
def configure_test_services():
    """Configura storage en memoria y resetea mocks para cada test."""
    storage = InMemoryStorageService()
    set_storage_service(storage)
    set_email_sender(None)
    MockFacturacionEngine.reset_invocations_count()
    return storage


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

    # Catálogo de comercio con motor mock
    merchant = Merchant(
        id=m_id,
        nombre="Comercio Simulado OXXO",
        slug=f"oxxo-mock-{m_id.hex[:6]}",
        tipo_motor=TipoMotor.API,
        engine_slug="mock",
        activo=True,
    )
    owner_session.add(merchant)

    # Perfil fiscal principal
    fiscal_profile = FiscalProfile(
        id=fp_id,
        tenant_id=t_id,
        razon_social="EMPRESA PRUEBA SA DE CV",
        rfc="XAXX010101000",
        cp="01000",
        regimen_fiscal="601",
        email_receptor="facturas@receptor.com",
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


# ---------------------------------------------------------------------------
# TEST 1: Ticket facturado deja el bucket sin imagen
# ---------------------------------------------------------------------------
@pytest.mark.asyncio
async def test_1_facturado_leaves_bucket_without_image(setup_phase4_scenario: Dict[str, Any]):
    """
    PRUEBA: Un ticket facturado exitosamente elimina su imagen efímera de S3/MinIO
    y fija image_deleted_at antes de quedar en estado final FACTURADO.
    """
    data = setup_phase4_scenario
    t_id = data["tenant_id"]
    ticket_id = uuid.uuid4()
    storage = InMemoryStorageService()
    set_storage_service(storage)

    image_key = f"tickets/{t_id}/{ticket_id}.jpg"
    await storage.upload_bytes(image_key, b"FAKE_TICKET_IMAGE_CONTENT", "image/jpeg")
    assert await storage.exists(image_key) is True

    async with tenant_session(t_id) as session:
        ticket = Ticket(
            id=ticket_id,
            tenant_id=t_id,
            created_by=data["user_id"],
            merchant_id=data["merchant_id"],
            fiscal_profile_id=data["fiscal_profile_id"],
            estado=TicketEstado.ENCOLADO,
            folio="FOLIO-OK-100",
            total=Decimal("350.00"),
            image_key=image_key,
        )
        session.add(ticket)
        await session.flush()

    # Procesar facturación en el worker
    await process_ticket_facturacion(t_id, ticket_id)

    async with tenant_session(t_id) as session:
        res = await session.execute(select(Ticket).where(Ticket.id == ticket_id))
        t = res.scalar_one()

        assert t.estado == TicketEstado.FACTURADO
        assert t.cfdi_uuid is not None
        assert t.image_deleted_at is not None
        # La imagen ya no existe en el almacenamiento
        assert await storage.exists(image_key) is False


# ---------------------------------------------------------------------------
# TEST 2: Ticket eliminado borra imagen efímera y registros
# ---------------------------------------------------------------------------
@pytest.mark.asyncio
async def test_2_delete_endpoint_cleans_storage_and_db(setup_phase4_scenario: Dict[str, Any]):
    """
    PRUEBA: El endpoint DELETE /v1/tickets/{id} borra la imagen de S3/MinIO y
    los registros asociados bajo aislamiento RLS, devolviendo 204.
    """
    data = setup_phase4_scenario
    t_id = data["tenant_id"]
    ticket_id = uuid.uuid4()
    storage = InMemoryStorageService()
    set_storage_service(storage)

    image_key = f"tickets/{t_id}/{ticket_id}.png"
    await storage.upload_bytes(image_key, b"RAW_PNG_DATA", "image/png")
    assert await storage.exists(image_key) is True

    async with tenant_session(t_id) as session:
        ticket = Ticket(
            id=ticket_id,
            tenant_id=t_id,
            created_by=data["user_id"],
            estado=TicketEstado.EXTRAIDO,
            image_key=image_key,
        )
        session.add(ticket)
        event = TicketEvent(
            tenant_id=t_id,
            ticket_id=ticket_id,
            tipo="creado",
            mensaje="Ticket creado",
        )
        session.add(event)
        await session.flush()

    transport = ASGITransport(app=app)
    async with AsyncClient(
        transport=transport,
        base_url="http://test",
        cookies={settings.SESSION_COOKIE_NAME: data["session_token"]},
    ) as client:
        res = await client.delete(
            f"/v1/tickets/{ticket_id}",
            headers={"X-Tenant-Id": str(t_id)},
        )

    assert res.status_code == 204
    # Imagen eliminada de S3
    assert await storage.exists(image_key) is False

    # Registro eliminado de la base de datos
    async with tenant_session(t_id) as session:
        res_db = await session.execute(select(Ticket).where(Ticket.id == ticket_id))
        assert res_db.scalar_one_or_none() is None


# ---------------------------------------------------------------------------
# TEST 3: Excepción a media ejecución del motor borra la imagen
# ---------------------------------------------------------------------------
@pytest.mark.asyncio
async def test_3_engine_exception_in_progress_deletes_image(setup_phase4_scenario: Dict[str, Any]):
    """
    PRUEBA: Si el motor de facturación lanza una excepción catastrófica no controlada
    a mitad del proceso, el bloque de rescate y cierre borra obligatoriamente la imagen.
    """
    data = setup_phase4_scenario
    t_id = data["tenant_id"]
    ticket_id = uuid.uuid4()
    storage = InMemoryStorageService()
    set_storage_service(storage)

    image_key = f"tickets/{t_id}/{ticket_id}.jpg"
    await storage.upload_bytes(image_key, b"CRASH_TEST_IMAGE", "image/jpeg")
    assert await storage.exists(image_key) is True

    async with tenant_session(t_id) as session:
        ticket = Ticket(
            id=ticket_id,
            tenant_id=t_id,
            created_by=data["user_id"],
            merchant_id=data["merchant_id"],
            fiscal_profile_id=data["fiscal_profile_id"],
            estado=TicketEstado.ENCOLADO,
            folio="FOLIO-ERROR-MOTOR-666",
            total=Decimal("99.99"),
            image_key=image_key,
        )
        session.add(ticket)
        await session.flush()

    await process_ticket_facturacion(t_id, ticket_id)

    async with tenant_session(t_id) as session:
        res = await session.execute(select(Ticket).where(Ticket.id == ticket_id))
        t = res.scalar_one()

        assert t.estado == TicketEstado.RECHAZADO
        assert t.error_code == "error_motor"
        assert t.image_deleted_at is not None
        # La imagen en storage se eliminó obligatoriamente
        assert await storage.exists(image_key) is False


# ---------------------------------------------------------------------------
# TEST 4: Concurrencia de 2 workers con mismo folio (Candados 1 y 2)
# ---------------------------------------------------------------------------
@pytest.mark.asyncio
async def test_4_worker_concurrency_same_folio_exactly_one_invoice(setup_phase4_scenario: Dict[str, Any]):
    """
    PRUEBA: Dos tickets distintos con el mismo folio y mismo comercio ejecutados en paralelo:
    - Advisory lock previene carrera (el segundo ticket no obtiene el lock y se re-encola).
    - Al procesar el segundo ticket diferido, la verificación bajo lock detecta la factura previa.
    - Exactamente 1 ticket queda FACTURADO y el otro RECHAZADO como 'duplicado'.
    - El motor solo se invoca 1 vez en total (MockFacturacionEngine.invocations_count == 1).
    """
    data = setup_phase4_scenario
    t_id = data["tenant_id"]
    m_id = data["merchant_id"]
    fp_id = data["fiscal_profile_id"]
    u_id = data["user_id"]
    storage = InMemoryStorageService()
    set_storage_service(storage)

    MockFacturacionEngine.reset_invocations_count()

    t1_id = uuid.uuid4()
    t2_id = uuid.uuid4()
    folio_compartido = f"FOLIO-RACE-{uuid.uuid4().hex[:6].upper()}"

    key1 = f"tickets/{t_id}/{t1_id}.jpg"
    key2 = f"tickets/{t_id}/{t2_id}.jpg"
    await storage.upload_bytes(key1, b"IMG1", "image/jpeg")
    await storage.upload_bytes(key2, b"IMG2", "image/jpeg")

    async with tenant_session(t_id) as session:
        t1 = Ticket(
            id=t1_id,
            tenant_id=t_id,
            created_by=u_id,
            merchant_id=m_id,
            fiscal_profile_id=fp_id,
            estado=TicketEstado.ENCOLADO,
            folio=folio_compartido,
            total=Decimal("500.00"),
            image_key=key1,
        )
        t2 = Ticket(
            id=t2_id,
            tenant_id=t_id,
            created_by=u_id,
            merchant_id=m_id,
            fiscal_profile_id=fp_id,
            estado=TicketEstado.ENCOLADO,
            folio=folio_compartido,
            total=Decimal("500.00"),
            image_key=key2,
        )
        session.add_all([t1, t2])
        await session.flush()

    # Ejecutar concurrentemente ambos workers
    await asyncio.gather(
        process_ticket_facturacion(t_id, t1_id),
        process_ticket_facturacion(t_id, t2_id),
    )

    async with tenant_session(t_id) as session:
        res1 = await session.execute(select(Ticket).where(Ticket.id == t1_id))
        res2 = await session.execute(select(Ticket).where(Ticket.id == t2_id))
        ticket_1 = res1.scalar_one()
        ticket_2 = res2.scalar_one()

    # Uno fue tomado y FACTURADO; el otro no obtuvo el lock y se re-encoló (Candado 2)
    facturado = ticket_1 if ticket_1.estado == TicketEstado.FACTURADO else ticket_2
    re_encolado = ticket_2 if facturado.id == t1_id else ticket_1

    assert facturado.estado == TicketEstado.FACTURADO
    assert re_encolado.estado == TicketEstado.ENCOLADO
    assert MockFacturacionEngine.invocations_count == 1

    # Ejecutar el re-encolado: bajo lock detecta la factura previa y se rechaza como duplicado
    await process_ticket_facturacion(t_id, re_encolado.id)

    async with tenant_session(t_id) as session:
        res_dup = await session.execute(select(Ticket).where(Ticket.id == re_encolado.id))
        ticket_dup = res_dup.scalar_one()

    assert ticket_dup.estado == TicketEstado.RECHAZADO
    assert ticket_dup.error_code == "duplicado"
    # El motor NO fue llamado por segunda vez
    assert MockFacturacionEngine.invocations_count == 1


# ---------------------------------------------------------------------------
# TEST 5: Desacoplamiento SMTP y no re-ejecución del motor
# ---------------------------------------------------------------------------
@pytest.mark.asyncio
async def test_5_smtp_failure_keeps_ticket_facturado_and_never_reruns_engine(setup_phase4_scenario: Dict[str, Any]):
    """
    PRUEBA: Si el motor emite la factura pero el envío por SMTP falla:
    - El ticket queda en estado FACTURADO con su cfdi_uuid intacto.
    - Se registra un evento de error de envío de correo.
    - Si el worker vuelve a correr, el motor NUNCA se vuelve a ejecutar.
    """
    data = setup_phase4_scenario
    t_id = data["tenant_id"]
    ticket_id = uuid.uuid4()
    storage = InMemoryStorageService()
    set_storage_service(storage)

    # Simular caída de servidor SMTP
    async def failing_email_sender(*args, **kwargs):
        raise ConnectionRefusedError("SMTP server down: connection refused on port 25")

    set_email_sender(failing_email_sender)
    MockFacturacionEngine.reset_invocations_count()

    image_key = f"tickets/{t_id}/{ticket_id}.jpg"
    await storage.upload_bytes(image_key, b"IMG_SMTP_TEST", "image/jpeg")

    async with tenant_session(t_id) as session:
        ticket = Ticket(
            id=ticket_id,
            tenant_id=t_id,
            created_by=data["user_id"],
            merchant_id=data["merchant_id"],
            fiscal_profile_id=data["fiscal_profile_id"],
            estado=TicketEstado.ENCOLADO,
            folio="FOLIO-SMTP-FAIL-01",
            total=Decimal("200.00"),
            image_key=image_key,
        )
        session.add(ticket)
        await session.flush()

    # Primera corrida del worker
    await process_ticket_facturacion(t_id, ticket_id)

    async with tenant_session(t_id) as session:
        res = await session.execute(select(Ticket).where(Ticket.id == ticket_id))
        t = res.scalar_one()

        # El ticket está FACTURADO
        assert t.estado == TicketEstado.FACTURADO
        assert t.cfdi_uuid is not None

        # Revisar evento de error de correo
        res_ev = await session.execute(
            select(TicketEvent).where(
                TicketEvent.ticket_id == ticket_id,
                TicketEvent.tipo == "error_envio_correo",
            )
        )
        ev_smtp = res_ev.scalar_one_or_none()
        assert ev_smtp is not None
        assert "SMTP server down" in ev_smtp.mensaje

    assert MockFacturacionEngine.invocations_count == 1

    # Segunda corrida accidental del worker
    await process_ticket_facturacion(t_id, ticket_id)

    # El motor NO fue llamado por segunda vez gracias al Candado 1
    assert MockFacturacionEngine.invocations_count == 1


# ---------------------------------------------------------------------------
# TEST 6: Cero persistencia de PDF y XML
# ---------------------------------------------------------------------------
@pytest.mark.asyncio
async def test_6_zero_persistence_of_pdf_and_xml(setup_phase4_scenario: Dict[str, Any]):
    """
    PRUEBA: Certificar que ni el PDF ni el XML generados se persisten en la
    base de datos (columnas ni jsonb), ni en storage, ni en logs.
    """
    data = setup_phase4_scenario
    t_id = data["tenant_id"]
    ticket_id = uuid.uuid4()
    storage = InMemoryStorageService()
    set_storage_service(storage)

    captured_mail = {}

    async def capturing_email_sender(to, uuid_str, pdf, xml, rfc, razon):
        captured_mail["pdf"] = pdf
        captured_mail["xml"] = xml

    set_email_sender(capturing_email_sender)

    image_key = f"tickets/{t_id}/{ticket_id}.jpg"
    await storage.upload_bytes(image_key, b"IMG_ZERO_PERSIST", "image/jpeg")

    async with tenant_session(t_id) as session:
        ticket = Ticket(
            id=ticket_id,
            tenant_id=t_id,
            created_by=data["user_id"],
            merchant_id=data["merchant_id"],
            fiscal_profile_id=data["fiscal_profile_id"],
            estado=TicketEstado.ENCOLADO,
            folio="FOLIO-ZERO-PERSIST",
            total=Decimal("450.00"),
            image_key=image_key,
        )
        session.add(ticket)
        await session.flush()

    await process_ticket_facturacion(t_id, ticket_id)

    assert "pdf" in captured_mail
    assert "xml" in captured_mail

    # 1. En storage no debe haber ningún archivo .pdf o .xml
    for key in storage._store.keys():
        assert not key.endswith(".pdf")
        assert not key.endswith(".xml")

    # 2. En base de datos no existe contenido de pdf ni xml
    async with tenant_session(t_id) as session:
        res = await session.execute(select(Ticket).where(Ticket.id == ticket_id))
        t = res.scalar_one()

        # Solo UUID y destinatario
        assert t.cfdi_uuid is not None
        assert t.cfdi_sent_to == "facturas@receptor.com"

        # Verificar eventos
        res_events = await session.execute(
            select(TicketEvent).where(TicketEvent.ticket_id == ticket_id)
        )
        events = res_events.scalars().all()
        for ev in events:
            # Ni el mensaje ni los metadatos tienen el contenido binario o XML
            assert "%PDF" not in ev.mensaje
            assert "<cfdi:Comprobante" not in ev.mensaje
            assert "pdf" not in ev.meta
            assert "xml" not in ev.meta


# ---------------------------------------------------------------------------
# TEST 7: Reintentos - error transitorio vs error definitivo
# ---------------------------------------------------------------------------
@pytest.mark.asyncio
async def test_7_retries_transient_vs_definitive_errors(setup_phase4_scenario: Dict[str, Any]):
    """
    PRUEBA:
    - Error definitivo (FOLIO-RECHAZADO) no se reintenta y pasa directo a RECHAZADO.
    - Error definitivo (FOLIO-CREDENCIALES) no se reintenta y pasa directo a RECHAZADO.
    - Error transitorio (FOLIO-CAIDO) se reintenta pasando a ENCOLADO con backoff.
    """
    data = setup_phase4_scenario
    t_id = data["tenant_id"]
    m_id = data["merchant_id"]
    fp_id = data["fiscal_profile_id"]
    u_id = data["user_id"]
    storage = InMemoryStorageService()
    set_storage_service(storage)

    # 1. Error definitivo: FOLIO-RECHAZADO
    t_def_id = uuid.uuid4()
    async with tenant_session(t_id) as session:
        t_def = Ticket(
            id=t_def_id,
            tenant_id=t_id,
            created_by=u_id,
            merchant_id=m_id,
            fiscal_profile_id=fp_id,
            estado=TicketEstado.ENCOLADO,
            folio="FOLIO-RECHAZADO-99",
            total=Decimal("120.00"),
        )
        session.add(t_def)
        await session.flush()

    await process_ticket_facturacion(t_id, t_def_id)

    async with tenant_session(t_id) as session:
        res = await session.execute(select(Ticket).where(Ticket.id == t_def_id))
        t = res.scalar_one()
        assert t.estado == TicketEstado.RECHAZADO
        assert t.error_code == "folio_rechazado"

    # 2. Error definitivo: FOLIO-CREDENCIALES
    t_cred_id = uuid.uuid4()
    async with tenant_session(t_id) as session:
        t_cred = Ticket(
            id=t_cred_id,
            tenant_id=t_id,
            created_by=u_id,
            merchant_id=m_id,
            fiscal_profile_id=fp_id,
            estado=TicketEstado.ENCOLADO,
            folio="FOLIO-CREDENCIALES-99",
            total=Decimal("120.00"),
        )
        session.add(t_cred)
        await session.flush()

    await process_ticket_facturacion(t_id, t_cred_id)

    async with tenant_session(t_id) as session:
        res = await session.execute(select(Ticket).where(Ticket.id == t_cred_id))
        t = res.scalar_one()
        assert t.estado == TicketEstado.RECHAZADO
        assert t.error_code == "credenciales_invalidas"

    # 3. Error transitorio: FOLIO-CAIDO (primer intento -> encolado con backoff)
    t_retry_id = uuid.uuid4()
    async with tenant_session(t_id) as session:
        t_retry = Ticket(
            id=t_retry_id,
            tenant_id=t_id,
            created_by=u_id,
            merchant_id=m_id,
            fiscal_profile_id=fp_id,
            estado=TicketEstado.ENCOLADO,
            folio="FOLIO-CAIDO-99",
            total=Decimal("120.00"),
            intentos=0,
        )
        session.add(t_retry)
        await session.flush()

    await process_ticket_facturacion(t_id, t_retry_id)

    async with tenant_session(t_id) as session:
        res = await session.execute(select(Ticket).where(Ticket.id == t_retry_id))
        t = res.scalar_one()
        # Debe quedar encolado para el siguiente intento
        assert t.estado == TicketEstado.ENCOLADO
        assert t.intentos == 1

        # Verificar evento de backoff
        res_ev = await session.execute(
            select(TicketEvent).where(
                TicketEvent.ticket_id == t_retry_id,
                TicketEvent.tipo == "cambio_estado",
            ).order_by(TicketEvent.id.desc())
        )
        last_ev = res_ev.scalars().first()
        assert last_ev.meta.get("backoff") == 30


# ---------------------------------------------------------------------------
# TEST 8: Endpoints POST /v1/tickets/{id}/retry y DELETE /v1/tickets/{id}
# ---------------------------------------------------------------------------
@pytest.mark.asyncio
async def test_8_retry_and_delete_endpoints(setup_phase4_scenario: Dict[str, Any]):
    """
    PRUEBA:
    - POST /v1/tickets/{id}/retry rechaza tickets que no estén en RECHAZADO (400).
    - POST /v1/tickets/{id}/retry rechaza tickets con intentos >= 3 (400).
    - POST /v1/tickets/{id}/retry acepta ticket en RECHAZADO con intentos < 3 (202).
    - DELETE /v1/tickets/{id} elimina el ticket y devuelve 204.
    """
    data = setup_phase4_scenario
    t_id = data["tenant_id"]
    u_id = data["user_id"]
    transport = ASGITransport(app=app)

    async with AsyncClient(
        transport=transport,
        base_url="http://test",
        cookies={settings.SESSION_COOKIE_NAME: data["session_token"]},
    ) as client:
        # Caso A: Reintentar un ticket FACTURADO (debe fallar 400)
        t_facturado_id = uuid.uuid4()
        async with tenant_session(t_id) as session:
            session.add(
                Ticket(
                    id=t_facturado_id,
                    tenant_id=t_id,
                    created_by=u_id,
                    estado=TicketEstado.FACTURADO,
                )
            )
            await session.flush()

        res_a = await client.post(
            f"/v1/tickets/{t_facturado_id}/retry",
            headers={"X-Tenant-Id": str(t_id)},
        )
        assert res_a.status_code == 400
        assert res_a.json()["error"]["code"] == "reintento_invalido"

        # Caso B: Reintentar un ticket RECHAZADO con intentos == 3 (debe fallar 400)
        t_excedido_id = uuid.uuid4()
        async with tenant_session(t_id) as session:
            session.add(
                Ticket(
                    id=t_excedido_id,
                    tenant_id=t_id,
                    created_by=u_id,
                    estado=TicketEstado.RECHAZADO,
                    intentos=3,
                )
            )
            await session.flush()

        res_b = await client.post(
            f"/v1/tickets/{t_excedido_id}/retry",
            headers={"X-Tenant-Id": str(t_id)},
        )
        assert res_b.status_code == 400
        assert res_b.json()["error"]["code"] == "maximo_intentos_excedido"

        # Caso C: Reintentar un ticket RECHAZADO con intentos == 1 (éxito 202)
        t_ok_id = uuid.uuid4()
        async with tenant_session(t_id) as session:
            session.add(
                Ticket(
                    id=t_ok_id,
                    tenant_id=t_id,
                    created_by=u_id,
                    estado=TicketEstado.RECHAZADO,
                    intentos=1,
                    error_code="error_temporal",
                )
            )
            await session.flush()

        res_c = await client.post(
            f"/v1/tickets/{t_ok_id}/retry",
            headers={"X-Tenant-Id": str(t_id)},
        )
        assert res_c.status_code == 202
        body_c = res_c.json()
        assert body_c["estado"] == "encolado"
        assert body_c["error_code"] is None

        # Caso D: DELETE /v1/tickets/{id}
        res_d = await client.delete(
            f"/v1/tickets/{t_ok_id}",
            headers={"X-Tenant-Id": str(t_id)},
        )
        assert res_d.status_code == 204


# ---------------------------------------------------------------------------
# TEST 9: Corrección 1 - Blindaje de Mock Auth fuera de desarrollo
# ---------------------------------------------------------------------------
@pytest.mark.asyncio
async def test_9_security_mock_auth_refuses_production():
    """
    PRUEBA DE SEGURIDAD (CORRECCIÓN 1):
    1. Si ENVIRONMENT == 'production' o 'staging' y ALLOW_MOCK_AUTH == True,
       Settings levanta ValidationError (falla ruidosa al arranque).
    2. Con ENVIRONMENT == 'production' y ALLOW_MOCK_AUTH == False,
       un intento de login con token mock: correo@dominio devuelve 401.
    """
    # 1. Falla ruidosa al instanciar Settings en production/staging con ALLOW_MOCK_AUTH=True
    with pytest.raises(ValidationError) as exc1:
        Settings(ENVIRONMENT="production", ALLOW_MOCK_AUTH=True)
    assert "ALLOW_MOCK_AUTH no puede ser True" in str(exc1.value)

    with pytest.raises(ValidationError) as exc2:
        Settings(ENVIRONMENT="staging", ALLOW_MOCK_AUTH=True)
    assert "ALLOW_MOCK_AUTH no puede ser True" in str(exc2.value)

    # 2. Con ENVIRONMENT == production y ALLOW_MOCK_AUTH == False, mock login es rechazado con 401
    prod_settings = Settings(ENVIRONMENT="production", ALLOW_MOCK_AUTH=False)
    assert not (prod_settings.ENVIRONMENT == "development" and prod_settings.ALLOW_MOCK_AUTH)

    transport = ASGITransport(app=app)
    # Simular temporalmente settings y verificador en modo producción
    original_env = settings.ENVIRONMENT
    original_mock = settings.ALLOW_MOCK_AUTH
    try:
        settings.ENVIRONMENT = "production"
        settings.ALLOW_MOCK_AUTH = False
        set_google_verifier(ProductionGoogleTokenVerifier())

        async with AsyncClient(transport=transport, base_url="http://test") as client:
            res = await client.post(
                "/v1/auth/google",
                json={"id_token": "mock:hacker@external.com"},
            )
            assert res.status_code == 401
            assert res.json()["error"]["code"] == "token_invalido"
    finally:
        settings.ENVIRONMENT = original_env
        settings.ALLOW_MOCK_AUTH = original_mock
        set_google_verifier(FakeGoogleTokenVerifier())


# ---------------------------------------------------------------------------
# TEST 10: Corrección 2 - Soporte de imágenes HEIC con Pillow y subida real
# ---------------------------------------------------------------------------
@pytest.mark.asyncio
async def test_10_heic_opener_and_upload(setup_phase4_scenario: Dict[str, Any]):
    """
    PRUEBA DE COMPATIBILIDAD (CORRECCIÓN 2):
    1. pillow-heif registra el opener en Pillow y permite guardar/abrir un HEIC real.
    2. process_and_stream_upload acepta un archivo HEIC real identificando su mime type.
    """
    import pillow_heif
    from PIL import Image

    # Garantizar registro de opener
    pillow_heif.register_heif_opener()

    # 1. Crear HEIC real en memoria
    img = Image.new("RGB", (30, 30), color=(255, 128, 0))
    buf = io.BytesIO()
    img.save(buf, format="HEIF")
    heic_bytes = buf.getvalue()

    # Verificar que Pillow puede abrirlo directamente
    opened_img = Image.open(io.BytesIO(heic_bytes))
    assert opened_img.size == (30, 30)
    assert opened_img.format == "HEIF"

    # 2. Subir vía endpoint /v1/tickets
    data = setup_phase4_scenario
    t_id = data["tenant_id"]
    transport = ASGITransport(app=app)

    files = {"file": ("recibo_iphone.heic", io.BytesIO(heic_bytes), "image/heic")}

    async with AsyncClient(
        transport=transport,
        base_url="http://test",
        cookies={settings.SESSION_COOKIE_NAME: data["session_token"]},
    ) as client:
        res = await client.post(
            "/v1/tickets",
            files=files,
            headers={"X-Tenant-Id": str(t_id)},
        )

    assert res.status_code == 202
    res_body = res.json()
    assert res_body["estado"] == "recibido"
    ticket_id = res_body["id"]

    # Verificar registro en base de datos
    async with tenant_session(t_id) as session:
        t_res = await session.execute(select(Ticket).where(Ticket.id == uuid.UUID(ticket_id)))
        ticket = t_res.scalar_one()
        assert ticket.image_key == f"tickets/{t_id}/{ticket_id}"

        ev_res = await session.execute(
            select(TicketEvent).where(
                TicketEvent.ticket_id == ticket.id,
                TicketEvent.tipo == "recibido",
            )
        )
        ev = ev_res.scalar_one()
        assert ev.meta["mime_type"] == "image/heic"


# ---------------------------------------------------------------------------
# TEST 11: Traza completa de ticket_events desde recibido hasta facturado
# ---------------------------------------------------------------------------
@pytest.mark.asyncio
async def test_11_full_ticket_lifecycle_events_trace(setup_phase4_scenario: Dict[str, Any]):
    """
    PRUEBA: Ejecutar el flujo completo de un ticket desde RECIBIDO hasta FACTURADO
    e imprimir/verificar la traza completa y ordenada de ticket_events.
    """
    data = setup_phase4_scenario
    t_id = data["tenant_id"]
    u_id = data["user_id"]
    m_id = data["merchant_id"]
    fp_id = data["fiscal_profile_id"]
    ticket_id = uuid.uuid4()
    storage = InMemoryStorageService()
    set_storage_service(storage)

    image_key = f"tickets/{t_id}/{ticket_id}.jpg"
    await storage.upload_bytes(image_key, b"FULL_LIFECYCLE_IMAGE", "image/jpeg")

    async def successful_email_sender(*args, **kwargs):
        pass

    set_email_sender(successful_email_sender)

    # 1. Crear en estado RECIBIDO
    async with tenant_session(t_id) as session:
        ticket = Ticket(
            id=ticket_id,
            tenant_id=t_id,
            created_by=u_id,
            estado=TicketEstado.RECIBIDO,
            image_key=image_key,
        )
        session.add(ticket)
        ev_recibido = TicketEvent(
            tenant_id=t_id,
            ticket_id=ticket_id,
            tipo="recibido",
            mensaje="Ticket recibido exitosamente en el servidor.",
            meta={"file_size": len(b"FULL_LIFECYCLE_IMAGE"), "mime_type": "image/jpeg"},
        )
        session.add(ev_recibido)
        await session.flush()

    # 2. Simular extracción -> EXTRAIDO
    async with tenant_session(t_id) as session:
        t_res = await session.execute(select(Ticket).where(Ticket.id == ticket_id))
        t = t_res.scalar_one()
        await transition(session, t, TicketEstado.EXTRAYENDO, "Iniciando extracción con visión artificial.")
        t.merchant_id = m_id
        t.fiscal_profile_id = fp_id
        t.folio = "FOLIO-LIFECYCLE-TRACE-1"
        t.total = Decimal("780.00")
        await transition(session, t, TicketEstado.EXTRAIDO, "Datos extraídos correctamente. Comercio: OXXO Mock.")
        await transition(session, t, TicketEstado.ENCOLADO, "Ticket encolado para facturación automática.")
        await session.flush()

    # 3. Correr worker de facturación -> FACTURANDO -> FACTURADO
    await process_ticket_facturacion(t_id, ticket_id)

    # 4. Consultar y verificar la secuencia completa de eventos
    async with tenant_session(t_id) as session:
        t_res = await session.execute(select(Ticket).where(Ticket.id == ticket_id))
        t_final = t_res.scalar_one()
        assert t_final.estado == TicketEstado.FACTURADO

        events_res = await session.execute(
            select(TicketEvent)
            .where(TicketEvent.ticket_id == ticket_id)
            .order_by(TicketEvent.id.asc())
        )
        all_events = events_res.scalars().all()

    # Validar tipos de eventos
    event_types = [ev.tipo for ev in all_events]
    assert "recibido" in event_types
    assert "cambio_estado" in event_types
    assert "motor_iniciado" in event_types
    assert "cfdi_generado" in event_types

    # Imprimir la traza completa para revisión
    print("\n--- TRAZA COMPLETA DE TICKET_EVENTS ---")
    for ev in all_events:
        print(f"[{ev.id}] {ev.ts.isoformat()} | Tipo: {ev.tipo:<16} | Mensaje: {ev.mensaje}")
    print("----------------------------------------\n")
