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
    TicketEvent,
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
async def test_vision_model_escalation_records_ticket_events(owner_session):
    """
    PRUEBA: Si la confianza es baja (< 0.6) con ANTHROPIC_MODEL_VISION,
    el worker escala UNA vez a ANTHROPIC_MODEL_AGENTE y registra el evento en ticket_events.
    """
    storage = InMemoryStorageService()
    set_storage_service(storage)
    vision = FakeVisionExtractor()
    set_vision_extractor(vision)

    t_id = uuid.uuid4()
    u_id = uuid.uuid4()
    ticket_id = uuid.uuid4()

    tenant = Tenant(id=t_id, nombre="Empresa Escalamiento", slug=f"esc-{t_id.hex[:6]}", plan="free")
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

    # Configurar modo borroso_escalable: con VISION da borroso (0.45), con AGENTE da bueno (0.95)
    vision.set_mode("borroso_escalable")

    await process_ticket_extraction(t_id, ticket_id)

    async with tenant_session(t_id) as session:
        # Verificar ticket_events
        ev_res = await session.execute(
            select(TicketEvent).where(TicketEvent.ticket_id == ticket_id).order_by(TicketEvent.id.asc())
        )
        events = ev_res.scalars().all()
        tipos_evento = [e.tipo for e in events]
        assert "escalamiento_modelo" in tipos_evento

        ev_esc = next(e for e in events if e.tipo == "escalamiento_modelo")
        assert ev_esc.meta["modelo_origen"] == settings.ANTHROPIC_MODEL_VISION
        assert ev_esc.meta["modelo_destino"] == settings.ANTHROPIC_MODEL_AGENTE
        assert ev_esc.meta["confianza_previa"] < 0.6

        # Verificar que el ticket logró procesarse con éxito gracias al escalamiento
        t_res = await session.execute(select(Ticket).where(Ticket.id == ticket_id))
        t = t_res.scalar_one()
        assert t.estado in (TicketEstado.EXTRAIDO, TicketEstado.ENCOLADO)
        assert t.confianza >= Decimal("0.6")


@pytest.mark.asyncio
async def test_sse_stream_leaves_zero_idle_in_transaction(app_engine):
    """
    PRUEBA: El SSE no deja transacciones abiertas (Trampa 2 evitada).
    Levanta un servidor uvicorn real en un puerto dinámico libre, conecta con httpx
    en streaming real por TCP, muta el ticket concurrentemente desde otra tarea,
    verifica que el stream entrega los chunks en vivo y finaliza limpiamente
    al alcanzar el estado final, dejando exactamente 0 conexiones en 'idle in transaction'.
    """
    import socket
    import uvicorn

    # 1. Encontrar puerto libre y arrancar uvicorn en segundo plano
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.bind(("127.0.0.1", 0))
        port = s.getsockname()[1]

    config = uvicorn.Config(app=app, host="127.0.0.1", port=port, log_level="error")
    server = uvicorn.Server(config)
    server_task = asyncio.create_task(server.serve())

    while not server.started:
        await asyncio.sleep(0.05)

    base_url = f"http://127.0.0.1:{port}"

    try:
        unique_email = f"sse.live.{uuid.uuid4().hex[:8]}@test.com"
        async with AsyncClient(base_url=base_url) as client:
            log_res = await client.post("/v1/auth/google", json={"id_token": f"mock:{unique_email}"})
            cookie = log_res.cookies.get(settings.SESSION_COOKIE_NAME)
            tenant_id = uuid.UUID(log_res.json()["created_tenant_id"])

        # Subir ticket en estado inicial RECIBIDO
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

        # Escritor concurrente: cancela el ticket poco después de abrir el stream
        async def cancel_later():
            await asyncio.sleep(0.5)
            async with tenant_session(tenant_id) as session:
                t_res = await session.execute(select(Ticket).where(Ticket.id == ticket_id))
                t = t_res.scalar_one()
                await transition(session, t, TicketEstado.CANCELADO, "Cierre concurrente de prueba")

        cancel_task = asyncio.create_task(cancel_later())

        # Consumir stream real en vivo
        received_lines = []
        async with AsyncClient(base_url=base_url, cookies={settings.SESSION_COOKIE_NAME: cookie}) as auth_client:
            async with auth_client.stream("GET", f"/v1/tickets/{ticket_id}/stream", headers={"X-Tenant-Id": str(tenant_id)}) as response:
                assert response.status_code == 200
                async for line in response.aiter_lines():
                    if line:
                        received_lines.append(line)
                    if "estado_final" in line:
                        break

        await cancel_task
        full_stream = "\n".join(received_lines)
        assert ": connected" in full_stream or "connected" in full_stream
        assert "estado_final" in full_stream

        # Comprobar conexiones idle in transaction en PostgreSQL tras consumir el stream
        async with app_engine.connect() as conn:
            res = await conn.execute(
                text("SELECT count(*) FROM pg_stat_activity WHERE datname = 'facturia' AND state = 'idle in transaction'")
            )
            idle_count = res.scalar()
            assert idle_count == 0, f"Quedaron conexiones en 'idle in transaction': {idle_count}"
    finally:
        server.should_exit = True
        await server_task


@pytest.mark.asyncio
async def test_thermal_wrinkled_ticket_and_enhancement(owner_session: AsyncSession):
    """
    PRUEBA:
    1. enhance_receipt_image procesa imágenes JPEG correctamente.
    2. Si un ticket no tiene etiqueta 'folio' pero tiene web_id o transaccion,
       el worker lo recupera y no lo marca como imagen_ilegible.
    """
    from PIL import Image as PILImage
    from src.vision.extractor import enhance_receipt_image, VisionKeyValue

    # 1. Test unitario de enhance_receipt_image
    buf = io.BytesIO()
    img = PILImage.new("RGB", (120, 120), color=(200, 200, 200))
    img.save(buf, format="JPEG")
    orig_bytes = buf.getvalue()

    enhanced = enhance_receipt_image(orig_bytes)
    assert isinstance(enhanced, bytes)
    assert len(enhanced) > 0
    opened = PILImage.open(io.BytesIO(enhanced))
    assert opened.size == (120, 120)

    # 2. Test de recuperación de folio desde web_id
    storage = InMemoryStorageService()
    set_storage_service(storage)
    vision = FakeVisionExtractor()
    set_vision_extractor(vision)

    t_id = uuid.uuid4()
    u_id = uuid.uuid4()
    tenant = Tenant(id=t_id, nombre="Empresa Termicos", slug=f"term-{t_id.hex[:6]}", plan="free")
    user = User(id=u_id, email=f"user-{u_id.hex[:6]}@test.com", nombre="User", google_sub=f"sub-{u_id.hex}")
    owner_session.add_all([tenant, user])
    await owner_session.flush()

    # Fixture sin folio explícito pero con web_id
    vision.register_fixture(
        "arrugado_con_web_id",
        VisionExtractionSchema(
            comercio="Dominos Pizza",
            url_facturacion="https://alsea.interfactura.com",
            folio=None,
            web_id="WID-ALSEA-998877",
            total="340.00",
            confianza=0.85,
        ),
    )
    vision.set_mode("arrugado_con_web_id")

    ticket_id = uuid.uuid4()
    image_key = f"tickets/{t_id}/{ticket_id}"
    await storage.upload_bytes(image_key, orig_bytes, "image/jpeg")

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

    # Ejecutar worker
    await process_ticket_extraction(t_id, ticket_id)

    # Verificar en BD que el ticket llegó a EXTRAIDO y recuperó el folio
    async with tenant_session(t_id) as session:
        t_db = (await session.execute(select(Ticket).where(Ticket.id == ticket_id))).scalar_one()
        assert t_db.estado == TicketEstado.EXTRAIDO
        assert t_db.folio == "WID-ALSEA-998877"
        assert t_db.total == Decimal("340.00")


@pytest.mark.asyncio
async def test_batch_upload_with_duplicates():
    """
    PRUEBA: Subida masiva de tickets con detección de duplicados en el lote y en la BD.
    1. Envía 3 archivos: dos idénticos (duplicados entre sí) y uno diferente.
    2. El sistema divide los tickets, encola los no duplicados y marca el repetido como rechazado/duplicado.
    """
    transport = ASGITransport(app=app)
    unique_email = f"batch.{uuid.uuid4().hex[:8]}@test.com"
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        log_res = await client.post("/v1/auth/google", json={"id_token": f"mock:{unique_email}"})
        cookie = log_res.cookies.get(settings.SESSION_COOKIE_NAME)
        tenant_id = log_res.json()["created_tenant_id"]

    file1_bytes = VALID_JPEG_HEADER + b"_unique_1_"
    file2_bytes = VALID_JPEG_HEADER + b"_unique_1_"  # Mismo contenido que file1 (duplicado)
    file3_bytes = VALID_JPEG_HEADER + b"_unique_2_"

    files = [
        ("files", ("recibo_1.jpg", io.BytesIO(file1_bytes), "image/jpeg")),
        ("files", ("recibo_1_copia.jpg", io.BytesIO(file2_bytes), "image/jpeg")),
        ("files", ("recibo_2.jpg", io.BytesIO(file3_bytes), "image/jpeg")),
    ]

    async with AsyncClient(transport=transport, base_url="http://test", cookies={settings.SESSION_COOKIE_NAME: cookie}) as auth_client:
        response = await auth_client.post(
            "/v1/tickets/batch",
            files=files,
            headers={"X-Tenant-Id": tenant_id},
        )

    assert response.status_code == 202
    data = response.json()
    assert "items" in data
    assert "resumen" in data
    assert len(data["items"]) == 3
    assert data["resumen"]["total"] == 3
    assert data["resumen"]["encolados"] == 2
    assert data["resumen"]["duplicados"] == 1
    assert data["resumen"]["fallidos"] == 0

    # Verificar que el segundo ticket fue marcado como duplicado
    estados = [item["estado"] for item in data["items"]]
    error_codes = [item["error_code"] for item in data["items"]]

    assert estados.count("recibido") == 2
    assert estados.count("rechazado") == 1
    assert "duplicado" in error_codes


@pytest.mark.asyncio
async def test_upload_duplicate_identical_file():
    """
    PRUEBA: Detección de duplicado por hash SHA-256 contra base de datos previa.
    1. Sube un ticket exitosamente.
    2. Sube la misma foto en una petición posterior.
    3. La segunda petición devuelve el ticket rechazado de inmediato con error_code='duplicado'.
    """
    transport = ASGITransport(app=app)
    unique_email = f"dup.{uuid.uuid4().hex[:8]}@test.com"
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        log_res = await client.post("/v1/auth/google", json={"id_token": f"mock:{unique_email}"})
        cookie = log_res.cookies.get(settings.SESSION_COOKIE_NAME)
        tenant_id = log_res.json()["created_tenant_id"]

    same_bytes = VALID_JPEG_HEADER + b"_identical_ticket_bytes_"

    async with AsyncClient(transport=transport, base_url="http://test", cookies={settings.SESSION_COOKIE_NAME: cookie}) as auth_client:
        # Subida 1
        r1 = await auth_client.post(
            "/v1/tickets",
            files={"file": ("ticket_original.jpg", io.BytesIO(same_bytes), "image/jpeg")},
            headers={"X-Tenant-Id": tenant_id},
        )
        assert r1.status_code == 202
        t1 = r1.json()
        assert t1["estado"] == "recibido"
        assert t1["error_code"] is None

        # Subida 2 (Misma foto exacta)
        r2 = await auth_client.post(
            "/v1/tickets",
            files={"file": ("ticket_repetido.jpg", io.BytesIO(same_bytes), "image/jpeg")},
            headers={"X-Tenant-Id": tenant_id},
        )
        assert r2.status_code == 202
        t2 = r2.json()
        assert t2["estado"] == "rechazado"
        assert t2["error_code"] == "duplicado"
        assert "duplicado" in t2["error_msg"].lower()


