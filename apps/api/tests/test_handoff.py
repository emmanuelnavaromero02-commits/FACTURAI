import asyncio
from contextlib import asynccontextmanager
from datetime import datetime, timedelta, timezone
from decimal import Decimal
import hashlib
import json
import secrets
import socket
import uuid
import pytest
import pytest_asyncio
from sqlalchemy import select, text
from sqlalchemy.ext.asyncio import AsyncSession
import uvicorn
import websockets

from src.auth import sign_session_token
from src.config import get_settings
from src.db import sin_tenant, tenant_session
from src.engines import register_engine
from src.engines.agent_brain import AgentDecision, BrowserAction, FakeAgentBrain
from src.engines.generic_web import GenericWebEngine
from src.main import app
from src.models import (
    FiscalProfile,
    HandoffEstado,
    HandoffSession,
    Membership,
    MembershipRole,
    Tenant,
    Ticket,
    TicketEstado,
    TicketEvent,
    User,
)
from src.services.cfdi_storage import InMemoryCfdiStorageService, set_cfdi_storage
from src.services.handoff_manager import ActiveHandoffSession, get_handoff_manager
from src.storage import InMemoryStorageService, set_storage_service
from src.worker import process_ticket_facturacion
from tests.mock_portal_server import run_mock_portal_server

settings = get_settings()


@pytest.fixture(autouse=True)
def setup_services():
    set_storage_service(InMemoryStorageService())
    set_cfdi_storage(InMemoryCfdiStorageService())
    return


@asynccontextmanager
async def run_api_server():
    """Levanta un servidor API FastAPI en un puerto TCP efímero para pruebas WebSocket."""
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.bind(("127.0.0.1", 0))
        port = s.getsockname()[1]

    config = uvicorn.Config(app=app, host="127.0.0.1", port=port, log_level="error")
    server = uvicorn.Server(config)
    task = asyncio.create_task(server.serve())
    while not server.started:
        await asyncio.sleep(0.05)
    try:
        yield f"127.0.0.1:{port}"
    finally:
        server.should_exit = True
        await task


# ---------------------------------------------------------------------------
# TEST 1: Token inválido rechazado (código 4001)
# ---------------------------------------------------------------------------
@pytest.mark.asyncio
async def test_handoff_token_invalido_rechazado(setup_phase4_scenario, owner_session: AsyncSession):
    scenario = setup_phase4_scenario
    t_id = scenario["tenant_id"]
    u_id = scenario["user_id"]

    raw_token = secrets.token_urlsafe(32)
    token_hash = hashlib.sha256(raw_token.encode("utf-8")).hexdigest()
    handoff_id = uuid.uuid4()
    ticket_id = uuid.uuid4()

    ticket = Ticket(
        id=ticket_id,
        tenant_id=t_id,
        created_by=u_id,
        estado=TicketEstado.ESPERA_HUMANO,
        folio="FOLIO-WS-01",
        total=Decimal("100.00"),
    )
    await owner_session.execute(text("SELECT set_config('app.tenant_id', :t, true)"), {"t": str(t_id)})
    owner_session.add(ticket)
    await owner_session.flush()

    handoff_db = HandoffSession(
        id=handoff_id,
        tenant_id=t_id,
        ticket_id=ticket_id,
        motivo="captcha",
        estado=HandoffEstado.ESPERANDO,
        token_hash=token_hash,
        expires_at=datetime.now(timezone.utc) + timedelta(minutes=5),
    )
    owner_session.add(handoff_db)
    await owner_session.commit()

    user_token = sign_session_token(u_id, "test@example.com")

    async with run_api_server() as host:
        # Intento con token falso
        url = f"ws://{host}/v1/handoffs/{handoff_id}/live?token=token_invalido_falso&session_token={user_token}"
        async with websockets.connect(url) as ws:
            msg = await ws.recv() if False else None
            # Debe recibir cierre 4001
            try:
                await ws.recv()
                assert False, "Se esperaba cierre de conexión"
            except websockets.exceptions.ConnectionClosed as exc:
                assert exc.rcvd.code == 4001
                assert "Token de handoff inválido" in exc.rcvd.reason


# ---------------------------------------------------------------------------
# TEST 2: Usuario de otro tenant rechazado (código 4003)
# ---------------------------------------------------------------------------
@pytest.mark.asyncio
async def test_handoff_otro_tenant_rechazado(setup_phase4_scenario, owner_session: AsyncSession):
    scenario = setup_phase4_scenario
    t_id = scenario["tenant_id"]
    u_id = scenario["user_id"]

    # Crear tenant B y usuario B
    tenant_b_id = uuid.uuid4()
    user_b_id = uuid.uuid4()
    tenant_b = Tenant(id=tenant_b_id, nombre="Tenant B", slug=f"tenant-b-{tenant_b_id.hex[:6]}")
    user_b = User(id=user_b_id, email=f"user_b_{user_b_id.hex[:6]}@other.com", nombre="User B", google_sub=f"sub-{user_b_id.hex[:8]}")
    owner_session.add_all([tenant_b, user_b])
    await owner_session.flush()

    await owner_session.execute(text("SELECT set_config('app.tenant_id', :t, true)"), {"t": str(tenant_b_id)})
    mem_b = Membership(tenant_id=tenant_b_id, user_id=user_b_id, rol=MembershipRole.ADMIN)
    owner_session.add(mem_b)
    await owner_session.flush()

    # Crear handoff en tenant A
    raw_token = secrets.token_urlsafe(32)
    token_hash = hashlib.sha256(raw_token.encode("utf-8")).hexdigest()
    handoff_id = uuid.uuid4()
    ticket_id = uuid.uuid4()

    ticket = Ticket(
        id=ticket_id,
        tenant_id=t_id,
        created_by=u_id,
        estado=TicketEstado.ESPERA_HUMANO,
        folio="FOLIO-WS-02",
        total=Decimal("200.00"),
    )
    await owner_session.execute(text("SELECT set_config('app.tenant_id', :t, true)"), {"t": str(t_id)})
    owner_session.add(ticket)
    await owner_session.flush()

    handoff_db = HandoffSession(
        id=handoff_id,
        tenant_id=t_id,
        ticket_id=ticket_id,
        motivo="captcha",
        estado=HandoffEstado.ESPERANDO,
        token_hash=token_hash,
        expires_at=datetime.now(timezone.utc) + timedelta(minutes=5),
    )
    owner_session.add(handoff_db)
    await owner_session.commit()

    # Usuario B intenta conectarse al handoff de Tenant A
    user_b_session_token = sign_session_token(user_b_id, user_b.email)

    async with run_api_server() as host:
        url = f"ws://{host}/v1/handoffs/{handoff_id}/live?token={raw_token}&session_token={user_b_session_token}"
        async with websockets.connect(url) as ws:
            try:
                await ws.recv()
                assert False, "Se esperaba cierre 4003"
            except websockets.exceptions.ConnectionClosed as exc:
                assert exc.rcvd.code == 4003
                assert "Usuario no pertenece" in exc.rcvd.reason


# ---------------------------------------------------------------------------
# TEST 3: Segunda conexión cierra la primera automáticamente
# ---------------------------------------------------------------------------
@pytest.mark.asyncio
async def test_handoff_segunda_conexion_cierra_primera(setup_phase4_scenario, owner_session: AsyncSession):
    scenario = setup_phase4_scenario
    t_id = scenario["tenant_id"]
    u_id = scenario["user_id"]

    raw_token = secrets.token_urlsafe(32)
    token_hash = hashlib.sha256(raw_token.encode("utf-8")).hexdigest()
    handoff_id = uuid.uuid4()
    ticket_id = uuid.uuid4()

    ticket = Ticket(
        id=ticket_id,
        tenant_id=t_id,
        created_by=u_id,
        estado=TicketEstado.ESPERA_HUMANO,
        folio="FOLIO-WS-03",
        total=Decimal("300.00"),
    )
    await owner_session.execute(text("SELECT set_config('app.tenant_id', :t, true)"), {"t": str(t_id)})
    owner_session.add(ticket)
    await owner_session.flush()

    handoff_db = HandoffSession(
        id=handoff_id,
        tenant_id=t_id,
        ticket_id=ticket_id,
        motivo="captcha",
        estado=HandoffEstado.ESPERANDO,
        token_hash=token_hash,
        expires_at=datetime.now(timezone.utc) + timedelta(minutes=5),
    )
    owner_session.add(handoff_db)
    await owner_session.commit()

    # Registrar en memoria una sesión simulada en HandoffManager
    class DummyPage:
        class Mouse:
            async def click(self, x, y): pass
        class Keyboard:
            async def type(self, text): pass
            async def press(self, key): pass
        mouse = Mouse()
        keyboard = Keyboard()

    dummy_session = ActiveHandoffSession(
        handoff_id=handoff_id,
        tenant_id=t_id,
        ticket_id=ticket_id,
        page=DummyPage(),
        motivo="captcha",
        submission_attempted=False,
        created_at=datetime.now(timezone.utc),
        expires_at=datetime.now(timezone.utc) + timedelta(minutes=5),
    )
    get_handoff_manager().register_session(dummy_session)

    user_token = sign_session_token(u_id, "test@example.com")
    url = f"/v1/handoffs/{handoff_id}/live?token={raw_token}&session_token={user_token}"

    try:
        async with run_api_server() as host:
            ws_url = f"ws://{host}{url}"
            # Conexión 1
            ws1 = await websockets.connect(ws_url)
            msg1 = await ws1.recv()
            assert json.loads(msg1)["t"] == "state"

            # Conexión 2
            ws2 = await websockets.connect(ws_url)
            msg2 = await ws2.recv()
            assert json.loads(msg2)["t"] == "state"

            # Conexión 1 debe haber sido cerrada por la segunda con código 1000
            try:
                await ws1.recv()
                assert False, "Conexión 1 debió ser cerrada"
            except websockets.exceptions.ConnectionClosed as exc:
                assert exc.rcvd.code == 1000
                assert exc.rcvd.reason == "superseded"

            # Conexión 2 sigue viva
            await ws2.send(json.dumps({"t": "cancel"}))
            await ws2.close()
    finally:
        get_handoff_manager().remove_session(handoff_id)


# ---------------------------------------------------------------------------
# TEST 4: Expiración cierra el navegador y devuelve el ticket a encolado
# ---------------------------------------------------------------------------
@pytest.mark.asyncio
async def test_handoff_expiracion_devuelve_a_encolado(setup_phase4_scenario, owner_session: AsyncSession):
    scenario = setup_phase4_scenario
    t_id = scenario["tenant_id"]
    u_id = scenario["user_id"]
    fp_id = scenario["fiscal_profile_id"]

    async with run_mock_portal_server() as base_url:
        portal_url = f"{base_url}/captcha"

        fake_brain = FakeAgentBrain([
            AgentDecision(
                tipo="request_handoff",
                handoff_motivo="captcha",
            ),
        ])

        # Sobrescribir temporalmente HANDOFF_TTL_SEGUNDOS a 1 segundo para prueba inmediata
        old_ttl = settings.HANDOFF_TTL_SEGUNDOS
        settings.HANDOFF_TTL_SEGUNDOS = 1

        register_engine("generico-web")(lambda: GenericWebEngine(brain=fake_brain))

        try:
            ticket_id = uuid.uuid4()
            ticket = Ticket(
                id=ticket_id,
                tenant_id=t_id,
                created_by=u_id,
                fiscal_profile_id=fp_id,
                merchant_id=None,
                estado=TicketEstado.ENCOLADO,
                folio="FOLIO-EXP-01",
                total=Decimal("450.00"),
                url_facturacion=portal_url,
            )
            await owner_session.execute(text("SELECT set_config('app.tenant_id', :t, true)"), {"t": str(t_id)})
            owner_session.add(ticket)
            await owner_session.commit()

            # Ejecutar facturación en el worker
            await process_ticket_facturacion(t_id, ticket_id)

            # Verificar que tras expirar el TTL de 1 segundo:
            # - La sesión en BD quedó EXPIRADO
            # - El ticket volvió a ENCOLADO (reintento programado)
            async with tenant_session(t_id) as session:
                h_res = await session.execute(
                    select(HandoffSession).where(HandoffSession.ticket_id == ticket_id)
                )
                h_session = h_res.scalar_one_or_none()
                assert h_session is not None
                assert h_session.estado == HandoffEstado.EXPIRADO

                t_res = await session.execute(select(Ticket).where(Ticket.id == ticket_id))
                t_processed = t_res.scalar_one()
                assert t_processed.estado == TicketEstado.ENCOLADO
                assert t_processed.error_code == "handoff_expirado"
        finally:
            settings.HANDOFF_TTL_SEGUNDOS = old_ttl
            register_engine("generico-web")(GenericWebEngine)


# ---------------------------------------------------------------------------
# TEST 5: Flujo completo determinista: /captcha con FakeAgentBrain resuelto por WebSocket
# ---------------------------------------------------------------------------
@pytest.mark.asyncio
async def test_handoff_flujo_completo_captcha_resuelto_cliente(setup_phase4_scenario, owner_session: AsyncSession):
    scenario = setup_phase4_scenario
    t_id = scenario["tenant_id"]
    u_id = scenario["user_id"]
    fp_id = scenario["fiscal_profile_id"]

    async with run_mock_portal_server() as base_url:
        portal_url = f"{base_url}/captcha"

        fake_brain = FakeAgentBrain([
            # Paso 1: Llenar campos
            AgentDecision(
                tipo="batch_actions",
                batch=[
                    BrowserAction(tipo="type", selector="#folio", texto="FOLIO-CP-01"),
                    BrowserAction(tipo="type", selector="#total", texto="500.00"),
                    BrowserAction(tipo="type", selector="#rfc", texto="XAXX010101000"),
                ],
            ),
            # Paso 2: El agente detecta captcha y pide handoff
            AgentDecision(
                tipo="request_handoff",
                handoff_motivo="captcha",
            ),
            # Paso 3: Tras ser resuelto por el humano, el agente realiza el envío final
            AgentDecision(
                tipo="submit_final",
                submit_selector="#btn-submit",
            ),
            # Paso 4: Facturación confirmada
            AgentDecision(
                tipo="facturado_success",
                cfdi_uuid="77777777-6666-5555-4444-333333333333",
                entrega="emisor",
            ),
        ])

        register_engine("generico-web")(lambda: GenericWebEngine(brain=fake_brain))

        ticket_id = uuid.uuid4()
        ticket = Ticket(
            id=ticket_id,
            tenant_id=t_id,
            created_by=u_id,
            fiscal_profile_id=fp_id,
            merchant_id=None,
            estado=TicketEstado.ENCOLADO,
            folio="FOLIO-CP-01",
            total=Decimal("500.00"),
            url_facturacion=portal_url,
        )
        await owner_session.execute(text("SELECT set_config('app.tenant_id', :t, true)"), {"t": str(t_id)})
        owner_session.add(ticket)
        await owner_session.commit()

        user_token = sign_session_token(u_id, "test@example.com")

        worker_task = None
        try:
            async with run_api_server() as host:
                # 1. Arrancar el procesamiento del ticket en segundo plano
                worker_task = asyncio.create_task(process_ticket_facturacion(t_id, ticket_id))

                # 2. Esperar a que se emita el evento de handoff en la BD
                raw_token = None
                handoff_id = None
                for _ in range(50):
                    await asyncio.sleep(0.2)
                    async with tenant_session(t_id) as s_db:
                        res = await s_db.execute(
                            select(TicketEvent).where(
                                TicketEvent.ticket_id == ticket_id,
                                TicketEvent.tipo == "handoff",
                            )
                        )
                        ev = res.scalar_one_or_none()
                        if ev and ev.meta and "token" in ev.meta:
                            raw_token = ev.meta["token"]
                            handoff_id = uuid.UUID(ev.meta["handoff_id"])
                            break

                assert raw_token is not None, "El evento de handoff no fue emitido a tiempo"
                assert handoff_id is not None

                # 3. El cliente se conecta por WebSocket
                ws_url = f"ws://{host}/v1/handoffs/{handoff_id}/live?token={raw_token}&session_token={user_token}"
                async with websockets.connect(ws_url) as ws:
                    # Recibe mensaje de estado inicial
                    msg_state = json.loads(await ws.recv())
                    assert msg_state["t"] == "state"
                    assert msg_state["v"] == "esperando"

                    # Espera recibir al menos un frame de screencast CDP
                    frame_received = False
                    for _ in range(10):
                        raw_msg = await ws.recv()
                        parsed_msg = json.loads(raw_msg)
                        if parsed_msg.get("t") == "frame":
                            assert "jpeg" in parsed_msg
                            assert parsed_msg["w"] == 1280
                            frame_received = True
                            break

                    assert frame_received is True, "No se recibieron frames de CDP screencast"

                    # El cliente envía un clic normalizado sobre la casilla del captcha
                    await ws.send(json.dumps({"t": "click", "x": 0.25, "y": 0.45}))

                    # El cliente indica que terminó la verificación
                    await ws.send(json.dumps({"t": "done"}))

                    # Esperar confirmación done
                    resp = json.loads(await ws.recv())
                    while resp.get("t") == "frame":
                        resp = json.loads(await ws.recv())
                    assert resp.get("t") == "done"
                    assert resp.get("ok") is True

                # 4. Esperar que el worker termine la facturación
                await worker_task

                # 5. Comprobar que el ticket terminó en FACTURADO
                async with tenant_session(t_id) as session:
                    t_res = await session.execute(select(Ticket).where(Ticket.id == ticket_id))
                    t_final = t_res.scalar_one()
                    assert t_final.estado == TicketEstado.FACTURADO
                    assert t_final.cfdi_uuid == "77777777-6666-5555-4444-333333333333"

                # 6. Comprobar que la sesión de handoff quedó RESUELTO
                async with tenant_session(t_id) as session:
                    h_res = await session.execute(
                        select(HandoffSession).where(HandoffSession.id == handoff_id)
                    )
                    h_rec = h_res.scalar_one()
                    assert h_rec.estado == HandoffEstado.RESUELTO

        finally:
            if worker_task and not worker_task.done():
                worker_task.cancel()
                try:
                    await worker_task
                except (asyncio.CancelledError, Exception):
                    pass
            register_engine("generico-web")(GenericWebEngine)


# ---------------------------------------------------------------------------
# TEST 6: Al alcanzar HANDOFF_MAX_CONCURRENTES, el ticket se reencola sin abrir navegador
# ---------------------------------------------------------------------------
@pytest.mark.asyncio
async def test_handoff_max_concurrentes_reencola(setup_phase4_scenario, owner_session: AsyncSession):
    scenario = setup_phase4_scenario
    t_id = scenario["tenant_id"]
    u_id = scenario["user_id"]
    fp_id = scenario["fiscal_profile_id"]

    async with run_mock_portal_server() as base_url:
        portal_url = f"{base_url}/simple"
        fake_brain = FakeAgentBrain([
            AgentDecision(tipo="request_handoff", handoff_motivo="captcha"),
        ])
        register_engine("generico-web")(lambda: GenericWebEngine(brain=fake_brain))

        # Ocupar el cupo registrando HANDOFF_MAX_CONCURRENTES sesiones ficticias activas
        handoff_mgr = get_handoff_manager()
        dummy_ids = []
        for _ in range(settings.HANDOFF_MAX_CONCURRENTES):
            d_id = uuid.uuid4()
            dummy_ids.append(d_id)
            dummy_session = ActiveHandoffSession(
                handoff_id=d_id,
                tenant_id=t_id,
                ticket_id=uuid.uuid4(),
                page=None,
                motivo="captcha",
                submission_attempted=False,
                created_at=datetime.now(timezone.utc),
                expires_at=datetime.now(timezone.utc) + timedelta(minutes=10),
            )
            handoff_mgr.register_session(dummy_session)

        try:
            assert handoff_mgr.get_active_count() >= settings.HANDOFF_MAX_CONCURRENTES

            ticket_id = uuid.uuid4()
            ticket = Ticket(
                id=ticket_id,
                tenant_id=t_id,
                created_by=u_id,
                fiscal_profile_id=fp_id,
                merchant_id=None,
                estado=TicketEstado.ENCOLADO,
                folio="FOLIO-MAX-01",
                total=Decimal("600.00"),
                url_facturacion=portal_url,
            )
            await owner_session.execute(text("SELECT set_config('app.tenant_id', :t, true)"), {"t": str(t_id)})
            owner_session.add(ticket)
            await owner_session.commit()

            # Ejecutar facturación
            await process_ticket_facturacion(t_id, ticket_id)

            # El ticket debe haber vuelto a ENCOLADO sin agotar intentos
            async with tenant_session(t_id) as session:
                t_res = await session.execute(select(Ticket).where(Ticket.id == ticket_id))
                t_processed = t_res.scalar_one()
                assert t_processed.estado == TicketEstado.ENCOLADO
                assert t_processed.error_code == "handoff_tope_concurrencia"
                # No debe haber gastado intento
                assert t_processed.intentos == 0

        finally:
            for d_id in dummy_ids:
                handoff_mgr.remove_session(d_id)
            register_engine("generico-web")(GenericWebEngine)


# ---------------------------------------------------------------------------
# TEST 7: Puente Redis Pub/Sub multi-proceso (Worker en otro PID y Uvicorn)
# ---------------------------------------------------------------------------
@pytest.mark.asyncio
async def test_handoff_multiproceso_redis_bridge(setup_phase4_scenario, owner_session: AsyncSession):
    from src.redis_client import get_redis_client

    scenario = setup_phase4_scenario
    t_id = scenario["tenant_id"]
    u_id = scenario["user_id"]

    raw_token = secrets.token_urlsafe(32)
    token_hash = hashlib.sha256(raw_token.encode("utf-8")).hexdigest()
    handoff_id = uuid.uuid4()
    ticket_id = uuid.uuid4()

    ticket = Ticket(
        id=ticket_id,
        tenant_id=t_id,
        created_by=u_id,
        estado=TicketEstado.ESPERA_HUMANO,
        folio="FOLIO-REDIS-01",
        total=Decimal("750.00"),
    )
    await owner_session.execute(text("SELECT set_config('app.tenant_id', :t, true)"), {"t": str(t_id)})
    owner_session.add(ticket)
    await owner_session.flush()

    handoff_db = HandoffSession(
        id=handoff_id,
        tenant_id=t_id,
        ticket_id=ticket_id,
        motivo="cloudflare_turnstile",
        estado=HandoffEstado.ESPERANDO,
        token_hash=token_hash,
        expires_at=datetime.now(timezone.utc) + timedelta(minutes=5),
    )
    owner_session.add(handoff_db)
    await owner_session.commit()

    # Asegurar que NO existe en memoria local del HandoffManager (simula estar en otro proceso)
    get_handoff_manager().remove_session(handoff_id)
    assert get_handoff_manager().get_session(handoff_id) is None

    # Publicar metadatos y frame en caché en Redis (como lo hace el Worker en su PID)
    redis = get_redis_client()
    meta_json = json.dumps({
        "handoff_id": str(handoff_id),
        "tenant_id": str(t_id),
        "ticket_id": str(ticket_id),
        "motivo": "cloudflare_turnstile",
        "submission_attempted": False,
        "active": True,
    })
    await redis.set(f"handoff:{handoff_id}:meta", meta_json, ex=120)
    await redis.set(f"handoff:{handoff_id}:last_frame", "dummy_base64_frame", ex=120)

    # El worker simula escuchar el canal de control
    worker_control_pubsub = redis.pubsub()
    await worker_control_pubsub.subscribe(f"handoff:{handoff_id}:control")

    async def wait_for_pubsub_msg(pubsub, timeout=3.0):
        deadline = asyncio.get_event_loop().time() + timeout
        while asyncio.get_event_loop().time() < deadline:
            msg = await pubsub.get_message(ignore_subscribe_messages=True, timeout=0.2)
            if msg and msg.get("type") == "message":
                return msg
            await asyncio.sleep(0.05)
        return None

    user_token = sign_session_token(u_id, "test@example.com")
    url = f"/v1/handoffs/{handoff_id}/live?token={raw_token}&session_token={user_token}"

    try:
        async with run_api_server() as host:
            ws_url = f"ws://{host}{url}"
            async with websockets.connect(ws_url) as ws:
                # 1. Debe recibir estado inicial desde Redis
                msg1 = json.loads(await ws.recv())
                assert msg1["t"] == "state"
                assert msg1["v"] == "esperando"

                # 2. Debe recibir el frame en caché desde Redis
                msg2 = json.loads(await ws.recv())
                assert msg2["t"] == "frame"
                assert msg2["jpeg"] == "dummy_base64_frame"

                # 3. El worker debe haber recibido la notificación 'ws_connected'
                connected_msg = await wait_for_pubsub_msg(worker_control_pubsub, timeout=3.0)
                assert connected_msg is not None
                assert json.loads(connected_msg["data"])["t"] == "ws_connected"

                # 4. El cliente envía un clic
                await ws.send(json.dumps({"t": "click", "x": 0.5, "y": 0.5}))
                click_msg = await wait_for_pubsub_msg(worker_control_pubsub, timeout=3.0)
                assert click_msg is not None
                assert json.loads(click_msg["data"])["t"] == "click"

                # 5. El worker publica un nuevo frame por Redis stream
                await redis.publish(
                    f"handoff:{handoff_id}:stream",
                    json.dumps({"t": "frame", "seq": 2, "jpeg": "frame_dos", "w": 1280, "h": 800}),
                )
                stream_frame = json.loads(await ws.recv())
                assert stream_frame["t"] == "frame"
                assert stream_frame["seq"] == 2
                assert stream_frame["jpeg"] == "frame_dos"

                # 6. El cliente envía 'done'
                await ws.send(json.dumps({"t": "done"}))
                done_ctrl = await wait_for_pubsub_msg(worker_control_pubsub, timeout=3.0)
                assert done_ctrl is not None
                assert json.loads(done_ctrl["data"])["t"] == "done"

                # 7. El worker publica 'done' por stream y el WebSocket lo recibe
                await redis.publish(f"handoff:{handoff_id}:stream", json.dumps({"t": "done", "ok": True}))
                final_msg = json.loads(await ws.recv())
                assert final_msg["t"] == "done"
                assert final_msg["ok"] is True

    finally:
        await worker_control_pubsub.unsubscribe(f"handoff:{handoff_id}:control")
        await worker_control_pubsub.aclose()
        await redis.delete(
            f"handoff:{handoff_id}:meta",
            f"handoff:{handoff_id}:last_frame",
        )


# ---------------------------------------------------------------------------
# TEST 8: Segunda conexión remota cierra la primera automáticamente
# ---------------------------------------------------------------------------
@pytest.mark.asyncio
async def test_handoff_multiproceso_segunda_conexion_cierra_primera(setup_phase4_scenario, owner_session: AsyncSession):
    from src.redis_client import get_redis_client

    scenario = setup_phase4_scenario
    t_id = scenario["tenant_id"]
    u_id = scenario["user_id"]

    raw_token = secrets.token_urlsafe(32)
    token_hash = hashlib.sha256(raw_token.encode("utf-8")).hexdigest()
    handoff_id = uuid.uuid4()
    ticket_id = uuid.uuid4()

    ticket = Ticket(
        id=ticket_id,
        tenant_id=t_id,
        created_by=u_id,
        estado=TicketEstado.ESPERA_HUMANO,
        folio="FOLIO-REDIS-02",
        total=Decimal("820.00"),
    )
    await owner_session.execute(text("SELECT set_config('app.tenant_id', :t, true)"), {"t": str(t_id)})
    owner_session.add(ticket)
    await owner_session.flush()

    handoff_db = HandoffSession(
        id=handoff_id,
        tenant_id=t_id,
        ticket_id=ticket_id,
        motivo="captcha_remoto",
        estado=HandoffEstado.ESPERANDO,
        token_hash=token_hash,
        expires_at=datetime.now(timezone.utc) + timedelta(minutes=5),
    )
    owner_session.add(handoff_db)
    await owner_session.commit()

    # Asegurar que no está en memoria local
    get_handoff_manager().remove_session(handoff_id)

    redis = get_redis_client()
    meta_json = json.dumps({
        "handoff_id": str(handoff_id),
        "tenant_id": str(t_id),
        "ticket_id": str(ticket_id),
        "motivo": "captcha_remoto",
        "submission_attempted": False,
        "active": True,
    })
    await redis.set(f"handoff:{handoff_id}:meta", meta_json, ex=120)

    user_token = sign_session_token(u_id, "test@example.com")
    url = f"/v1/handoffs/{handoff_id}/live?token={raw_token}&session_token={user_token}"

    try:
        async with run_api_server() as host:
            ws_url = f"ws://{host}{url}"
            ws1 = await websockets.connect(ws_url)
            msg1 = json.loads(await ws1.recv())
            assert msg1["t"] == "state"

            ws2 = await websockets.connect(ws_url)
            msg2 = json.loads(await ws2.recv())
            assert msg2["t"] == "state"

            # Conexión 1 debe haber sido cerrada por la segunda con código 1000 y razón 'superseded'
            try:
                await ws1.recv()
                assert False, "Conexión 1 debió ser cerrada por la segunda conexión"
            except websockets.exceptions.ConnectionClosed as exc:
                assert exc.rcvd.code == 1000
                assert exc.rcvd.reason == "superseded"

            # Conexión 2 sigue viva
            await ws2.close()
    finally:
        await redis.delete(f"handoff:{handoff_id}:meta")
