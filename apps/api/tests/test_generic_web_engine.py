import asyncio
from decimal import Decimal
import json
import os
import tempfile
import uuid
import pytest
import pytest_asyncio
from sqlalchemy import select, text
from sqlalchemy.ext.asyncio import AsyncSession

from src.config import get_settings
from src.db import tenant_session
from src.engines import EngineContext, EngineResult, register_engine
from src.engines.agent_brain import (
    AgentBrain,
    AgentDecision,
    AnthropicAgentBrain,
    BrowserAction,
    FakeAgentBrain,
)
from src.engines.generic_web import GenericWebEngine
from src.models import (
    FiscalProfile,
    Merchant,
    Ticket,
    TicketEstado,
    TicketEvent,
    TipoMotor,
)
from src.services.cfdi_storage import (
    InMemoryCfdiStorageService,
    get_cfdi_storage,
    set_cfdi_storage,
)
from src.storage import InMemoryStorageService, set_storage_service
from src.vision.url_sanitizer import sanitize_and_classify_billing_url
from src.worker import process_ticket_extraction, process_ticket_facturacion
from tests.mock_portal_server import run_mock_portal_server

settings = get_settings()


@pytest.fixture(autouse=True)
def ensure_in_memory_storage():
    storage = InMemoryStorageService()
    set_storage_service(storage)
    cfdi_storage = InMemoryCfdiStorageService()
    set_cfdi_storage(cfdi_storage)
    return storage


# ---------------------------------------------------------------------------
# TEST 1: Ruta /simple con FakeAgentBrain -> Éxito, métricas y persistencia
# ---------------------------------------------------------------------------
@pytest.mark.asyncio
async def test_generic_web_simple_success(setup_phase4_scenario, owner_session: AsyncSession):
    scenario = setup_phase4_scenario
    t_id = scenario["tenant_id"]
    u_id = scenario["user_id"]
    fp_id = scenario["fiscal_profile_id"]

    async with run_mock_portal_server() as base_url:
        portal_url = f"{base_url}/simple"

        fake_brain = FakeAgentBrain([
            AgentDecision(
                tipo="batch_actions",
                batch=[
                    BrowserAction(tipo="type", selector="#folio", texto="FOLIO-9988"),
                    BrowserAction(tipo="type", selector="#total", texto="1284.50"),
                    BrowserAction(tipo="type", selector="#rfc", texto="XAXX010101000"),
                    BrowserAction(tipo="type", selector="#email", texto="facturas@receptor.com"),
                ],
                tokens_input=120,
                tokens_output=45,
            ),
            AgentDecision(
                tipo="submit_final",
                submit_selector="#btn-submit",
                tokens_input=140,
                tokens_output=20,
            ),
            AgentDecision(
                tipo="facturado_success",
                cfdi_uuid="12345678-1234-1234-1234-123456789abc",
                entrega="emisor",
                correo_capturado="facturas@receptor.com",
                tokens_input=160,
                tokens_output=15,
            ),
        ])

        # Registrar temporalmente el factory del motor con el fake brain para el worker
        register_engine("generico-web")(lambda: GenericWebEngine(brain=fake_brain))

        try:
            # Crear ticket encolado
            ticket_id = uuid.uuid4()
            ticket = Ticket(
                id=ticket_id,
                tenant_id=t_id,
                created_by=u_id,
                fiscal_profile_id=fp_id,
                merchant_id=None,
                estado=TicketEstado.ENCOLADO,
                folio="FOLIO-9988",
                total=Decimal("1284.50"),
                url_facturacion=portal_url,
            )
            await owner_session.execute(text("SELECT set_config('app.tenant_id', :t, true)"), {"t": str(t_id)})
            owner_session.add(ticket)
            await owner_session.commit()

            # Ejecutar worker
            await process_ticket_facturacion(t_id, ticket_id)

            # Verificar resultado en base de datos
            async with tenant_session(t_id) as session:
                res = await session.execute(select(Ticket).where(Ticket.id == ticket_id))
                processed = res.scalar_one()

                assert processed.estado == TicketEstado.FACTURADO
                assert processed.cfdi_uuid == "12345678-1234-1234-1234-123456789abc"
                assert processed.pasos_agente == 3
                assert processed.tokens_input_total == 420  # 120 + 140 + 160
                assert processed.tokens_output_total == 80  # 45 + 20 + 15
                assert processed.duracion_segundos is not None
                assert processed.correo_capturado_en_portal == "facturas@receptor.com"
        finally:
            register_engine("generico-web")(GenericWebEngine)


# ---------------------------------------------------------------------------
# TEST 2: Ruta /falta-campo -> Aborta con perfil_incompleto (Regla 10)
# ---------------------------------------------------------------------------
@pytest.mark.asyncio
async def test_generic_web_falta_campo_aborts(setup_phase4_scenario, owner_session: AsyncSession):
    scenario = setup_phase4_scenario
    t_id = scenario["tenant_id"]
    u_id = scenario["user_id"]
    fp_id = scenario["fiscal_profile_id"]

    async with run_mock_portal_server() as base_url:
        portal_url = f"{base_url}/falta-campo"

        fake_brain = FakeAgentBrain([
            AgentDecision(
                tipo="perfil_incompleto",
                campo_faltante="colonia",
                tokens_input=150,
                tokens_output=30,
            ),
        ])

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
                folio="FOLIO-FALTA-01",
                total=Decimal("500.00"),
                url_facturacion=portal_url,
            )
            await owner_session.execute(text("SELECT set_config('app.tenant_id', :t, true)"), {"t": str(t_id)})
            owner_session.add(ticket)
            await owner_session.commit()

            await process_ticket_facturacion(t_id, ticket_id)

            async with tenant_session(t_id) as session:
                res = await session.execute(select(Ticket).where(Ticket.id == ticket_id))
                processed = res.scalar_one()

                assert processed.estado == TicketEstado.RECHAZADO
                assert processed.error_code == "perfil_incompleto"
                # Regla 10 y Corrección C: perfil_incompleto NO consume intentos
                assert processed.intentos == 0
        finally:
            register_engine("generico-web")(GenericWebEngine)


# ---------------------------------------------------------------------------
# TEST 3: Ruta /captcha -> Solicita handoff (stub)
# ---------------------------------------------------------------------------
@pytest.mark.asyncio
async def test_generic_web_captcha_requests_handoff(setup_phase4_scenario, owner_session: AsyncSession):
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
                tokens_input=180,
                tokens_output=25,
            ),
        ])

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
                folio="FOLIO-CAPTCHA-01",
                total=Decimal("850.00"),
                url_facturacion=portal_url,
            )
            await owner_session.execute(text("SELECT set_config('app.tenant_id', :t, true)"), {"t": str(t_id)})
            owner_session.add(ticket)
            await owner_session.commit()

            await process_ticket_facturacion(t_id, ticket_id)

            async with tenant_session(t_id) as session:
                res = await session.execute(select(Ticket).where(Ticket.id == ticket_id))
                processed = res.scalar_one()

                assert processed.estado == TicketEstado.ENCOLADO
                assert processed.error_code == "handoff_expirado"
        finally:
            settings.HANDOFF_TTL_SEGUNDOS = old_ttl
            register_engine("generico-web")(GenericWebEngine)


# ---------------------------------------------------------------------------
# TEST 4: Ruta /inyeccion -> Defensa contra Prompt Injection
# ---------------------------------------------------------------------------
@pytest.mark.asyncio
async def test_generic_web_inyeccion_defense(setup_phase4_scenario, owner_session: AsyncSession):
    scenario = setup_phase4_scenario
    t_id = scenario["tenant_id"]
    u_id = scenario["user_id"]
    fp_id = scenario["fiscal_profile_id"]

    async with run_mock_portal_server() as base_url:
        portal_url = f"{base_url}/inyeccion"

        fake_brain = FakeAgentBrain([
            AgentDecision(
                tipo="batch_actions",
                batch=[
                    BrowserAction(tipo="type", selector="#folio", texto="FOLIO-SEC-01"),
                    BrowserAction(tipo="type", selector="#total", texto="300.00"),
                    BrowserAction(tipo="type", selector="#rfc", texto="XAXX010101000"),  # RFC legítimo
                    BrowserAction(tipo="type", selector="#email", texto="facturas@receptor.com"),  # Correo legítimo
                ],
                tokens_input=200,
                tokens_output=50,
            ),
            AgentDecision(
                tipo="submit_final",
                submit_selector="#btn-submit",
                tokens_input=150,
                tokens_output=20,
            ),
            AgentDecision(
                tipo="facturado_success",
                cfdi_uuid="99999999-8888-7777-6666-555555555555",
                entrega="emisor",
                tokens_input=100,
                tokens_output=15,
            ),
        ])

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
                folio="FOLIO-SEC-01",
                total=Decimal("300.00"),
                url_facturacion=portal_url,
            )
            await owner_session.execute(text("SELECT set_config('app.tenant_id', :t, true)"), {"t": str(t_id)})
            owner_session.add(ticket)
            await owner_session.commit()

            await process_ticket_facturacion(t_id, ticket_id)

            async with tenant_session(t_id) as session:
                res = await session.execute(select(Ticket).where(Ticket.id == ticket_id))
                processed = res.scalar_one()

                assert processed.estado == TicketEstado.FACTURADO
                assert processed.cfdi_uuid == "99999999-8888-7777-6666-555555555555"
        finally:
            register_engine("generico-web")(GenericWebEngine)


# ---------------------------------------------------------------------------
# TEST 5: Ruta /nueva-pestana -> Interacción con nueva pestaña abierta
# ---------------------------------------------------------------------------
@pytest.mark.asyncio
async def test_generic_web_nueva_pestana(setup_phase4_scenario, owner_session: AsyncSession):
    scenario = setup_phase4_scenario
    t_id = scenario["tenant_id"]
    u_id = scenario["user_id"]
    fp_id = scenario["fiscal_profile_id"]

    async with run_mock_portal_server() as base_url:
        portal_url = f"{base_url}/nueva-pestana"

        fake_brain = FakeAgentBrain([
            # Paso 1: Clic en enlace target="_blank"
            AgentDecision(
                tipo="batch_actions",
                batch=[
                    BrowserAction(tipo="click", selector="#btn-abrir-portal"),
                    BrowserAction(tipo="wait"),
                ],
                tokens_input=100,
                tokens_output=30,
            ),
            # Paso 2: Llenar formulario en la pestaña recién abierta
            AgentDecision(
                tipo="batch_actions",
                batch=[
                    BrowserAction(tipo="type", selector="#folio", texto="FOLIO-TAB-01"),
                    BrowserAction(tipo="type", selector="#total", texto="1500.00"),
                    BrowserAction(tipo="type", selector="#rfc", texto="XAXX010101000"),
                    BrowserAction(tipo="type", selector="#email", texto="facturas@receptor.com"),
                ],
                tokens_input=120,
                tokens_output=40,
            ),
            # Paso 3: Envío final
            AgentDecision(
                tipo="submit_final",
                submit_selector="#btn-submit",
                tokens_input=130,
                tokens_output=20,
            ),
            # Paso 4: Confirmación
            AgentDecision(
                tipo="facturado_success",
                cfdi_uuid="12345678-1234-1234-1234-123456789abc",
                entrega="emisor",
                tokens_input=110,
                tokens_output=15,
            ),
        ])

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
                folio="FOLIO-TAB-01",
                total=Decimal("1500.00"),
                url_facturacion=portal_url,
            )
            await owner_session.execute(text("SELECT set_config('app.tenant_id', :t, true)"), {"t": str(t_id)})
            owner_session.add(ticket)
            await owner_session.commit()

            await process_ticket_facturacion(t_id, ticket_id)

            async with tenant_session(t_id) as session:
                res = await session.execute(select(Ticket).where(Ticket.id == ticket_id))
                processed = res.scalar_one()

                assert processed.estado == TicketEstado.FACTURADO
                assert processed.cfdi_uuid == "12345678-1234-1234-1234-123456789abc"
        finally:
            register_engine("generico-web")(GenericWebEngine)


# ---------------------------------------------------------------------------
# TEST 6: Ruta /descarga -> Intercepción efímera y limpieza en disco
# ---------------------------------------------------------------------------
@pytest.mark.asyncio
async def test_generic_web_descarga_ephemeral_cleanup(setup_phase4_scenario, owner_session: AsyncSession):
    scenario = setup_phase4_scenario
    t_id = scenario["tenant_id"]
    u_id = scenario["user_id"]
    fp_id = scenario["fiscal_profile_id"]

    async with run_mock_portal_server() as base_url:
        portal_url = f"{base_url}/descarga"

        fake_brain = FakeAgentBrain([
            # Paso 1: Llenar y enviar
            AgentDecision(
                tipo="batch_actions",
                batch=[
                    BrowserAction(tipo="type", selector="#folio", texto="FOLIO-DL-01"),
                    BrowserAction(tipo="type", selector="#total", texto="999.00"),
                    BrowserAction(tipo="type", selector="#rfc", texto="XAXX010101000"),
                    BrowserAction(tipo="type", selector="#email", texto="facturas@receptor.com"),
                ],
                tokens_input=100,
                tokens_output=30,
            ),
            # Paso 2: Envío final
            AgentDecision(
                tipo="submit_final",
                submit_selector="#btn-submit",
                tokens_input=100,
                tokens_output=20,
            ),
            # Paso 3: Clic en los enlaces de descarga
            AgentDecision(
                tipo="batch_actions",
                batch=[
                    BrowserAction(tipo="click", selector="#link-pdf"),
                    BrowserAction(tipo="click", selector="#link-xml"),
                    BrowserAction(tipo="wait"),
                ],
                tokens_input=120,
                tokens_output=25,
            ),
            # Paso 4: Éxito con entrega descarga
            AgentDecision(
                tipo="facturado_success",
                cfdi_uuid="55555555-4444-3333-2222-111111111111",
                entrega="descarga",
                tokens_input=110,
                tokens_output=15,
            ),
        ])

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
                folio="FOLIO-DL-01",
                total=Decimal("999.00"),
                url_facturacion=portal_url,
            )
            await owner_session.execute(text("SELECT set_config('app.tenant_id', :t, true)"), {"t": str(t_id)})
            owner_session.add(ticket)
            await owner_session.commit()

            await process_ticket_facturacion(t_id, ticket_id)

            async with tenant_session(t_id) as session:
                res = await session.execute(select(Ticket).where(Ticket.id == ticket_id))
                processed = res.scalar_one()

                assert processed.estado == TicketEstado.FACTURADO
                assert processed.cfdi_uuid == "55555555-4444-3333-2222-111111111111"
                assert processed.cfdi_disponible_hasta is not None
        finally:
            register_engine("generico-web")(GenericWebEngine)


# ---------------------------------------------------------------------------
# TEST 7: Envío final irreversible -> Segundo intento bloqueado
# ---------------------------------------------------------------------------
@pytest.mark.asyncio
async def test_generic_web_irreversible_submit_final(setup_phase4_scenario, owner_session: AsyncSession):
    scenario = setup_phase4_scenario
    t_id = scenario["tenant_id"]
    u_id = scenario["user_id"]
    fp_id = scenario["fiscal_profile_id"]

    async with run_mock_portal_server() as base_url:
        portal_url = f"{base_url}/simple"

        fake_brain = FakeAgentBrain([
            # Paso 1: Envío inicial
            AgentDecision(
                tipo="submit_final",
                submit_selector="#btn-submit",
                tokens_input=100,
                tokens_output=20,
            ),
            # Paso 2: El agente intenta ENVIAR OTRA VEZ (prohibido)
            AgentDecision(
                tipo="submit_final",
                submit_selector="#btn-submit",
                tokens_input=100,
                tokens_output=20,
            ),
            # Paso 3: Al no poder confirmar, termina en error
            AgentDecision(
                tipo="terminar_error",
                mensaje_error="No se pudo confirmar la emisión del comprobante.",
                tokens_input=100,
                tokens_output=20,
            ),
        ])

        engine = GenericWebEngine(brain=fake_brain)

        ticket_id = uuid.uuid4()
        ticket = Ticket(
            id=ticket_id,
            tenant_id=t_id,
            created_by=u_id,
            fiscal_profile_id=fp_id,
            merchant_id=None,
            estado=TicketEstado.ENCOLADO,
            folio="FOLIO-DOUBLE-SUBMIT",
            total=Decimal("400.00"),
            url_facturacion=portal_url,
        )
        await owner_session.execute(text("SELECT set_config('app.tenant_id', :t, true)"), {"t": str(t_id)})
        owner_session.add(ticket)
        await owner_session.commit()

        async with tenant_session(t_id) as session:
            t_res = await session.execute(select(Ticket).where(Ticket.id == ticket_id))
            t_obj = t_res.scalar_one()
            fp_res = await session.execute(select(FiscalProfile).where(FiscalProfile.id == fp_id))
            fp_obj = fp_res.scalar_one()

            ctx = EngineContext(
                ticket=t_obj,
                perfil_fiscal=fp_obj,
                merchant=None,
                _session=session,
                _tenant_id=t_id,
            )
            result = await engine.facturar(ctx)

            assert result.ok is False
            # Regla: una vez ejecutado el envío, termina en entrega_no_confirmada
            assert result.error_code == "entrega_no_confirmada"


# ---------------------------------------------------------------------------
# TEST 8: Control de Costos y Límite de Pasos
# ---------------------------------------------------------------------------
@pytest.mark.asyncio
async def test_generic_web_cost_and_step_limits(setup_phase4_scenario, owner_session: AsyncSession, monkeypatch):
    scenario = setup_phase4_scenario
    t_id = scenario["tenant_id"]
    u_id = scenario["user_id"]
    fp_id = scenario["fiscal_profile_id"]

    async with run_mock_portal_server() as base_url:
        portal_url = f"{base_url}/simple"

        # 8a: Tope de Pasos (configurado en 2 pasos)
        monkeypatch.setattr(settings, "PASOS_MAXIMOS_AGENTE", 2)

        fake_brain_steps = FakeAgentBrain([
            AgentDecision(tipo="batch_actions", batch=[BrowserAction(tipo="wait")], tokens_input=10, tokens_output=10),
            AgentDecision(tipo="batch_actions", batch=[BrowserAction(tipo="wait")], tokens_input=10, tokens_output=10),
            AgentDecision(tipo="batch_actions", batch=[BrowserAction(tipo="wait")], tokens_input=10, tokens_output=10),
        ])

        engine_steps = GenericWebEngine(brain=fake_brain_steps)

        ticket_id = uuid.uuid4()
        ticket = Ticket(
            id=ticket_id,
            tenant_id=t_id,
            created_by=u_id,
            fiscal_profile_id=fp_id,
            merchant_id=None,
            estado=TicketEstado.ENCOLADO,
            folio="FOLIO-STEPS-LIMIT",
            total=Decimal("250.00"),
            url_facturacion=portal_url,
        )
        await owner_session.execute(text("SELECT set_config('app.tenant_id', :t, true)"), {"t": str(t_id)})
        owner_session.add(ticket)
        await owner_session.commit()

        async with tenant_session(t_id) as session:
            t_res = await session.execute(select(Ticket).where(Ticket.id == ticket_id))
            t_obj = t_res.scalar_one()
            fp_res = await session.execute(select(FiscalProfile).where(FiscalProfile.id == fp_id))
            fp_obj = fp_res.scalar_one()
            ctx = EngineContext(ticket=t_obj, perfil_fiscal=fp_obj, merchant=None, _session=session, _tenant_id=t_id)
            res_steps = await engine_steps.facturar(ctx)

            assert res_steps.ok is False
            assert res_steps.error_code == "agente_no_completo"

        # 8b: Tope de Costo en USD (configurado en $0.00010 USD)
        monkeypatch.setattr(settings, "PASOS_MAXIMOS_AGENTE", 30)
        monkeypatch.setattr(settings, "COSTO_MAXIMO_POR_TICKET_USD", Decimal("0.00010"))

        # Simular precios para el modelo
        from src import config
        from src.engines import generic_web as gw_module
        dummy_pricing = {"input": Decimal("1.00"), "output": Decimal("2.00")}
        monkeypatch.setattr(config, "get_model_token_pricing", lambda model: dummy_pricing)
        monkeypatch.setattr(gw_module.config, "get_model_token_pricing", lambda model: dummy_pricing)

        fake_brain_cost = FakeAgentBrain([
            # Genera consumo que supera $0.00010 USD
            AgentDecision(tipo="batch_actions", batch=[BrowserAction(tipo="wait")], tokens_input=1000, tokens_output=1000),
            AgentDecision(tipo="batch_actions", batch=[BrowserAction(tipo="wait")], tokens_input=1000, tokens_output=1000),
        ])

        engine_cost = GenericWebEngine(brain=fake_brain_cost)
        async with tenant_session(t_id) as session:
            t_res = await session.execute(select(Ticket).where(Ticket.id == ticket_id))
            t_obj = t_res.scalar_one()
            ctx = EngineContext(ticket=t_obj, perfil_fiscal=fp_obj, merchant=None, _session=session, _tenant_id=t_id)
            res_cost = await engine_cost.facturar(ctx)

            assert res_cost.ok is False
            assert res_cost.error_code == "agente_costo_excedido"


# ---------------------------------------------------------------------------
# TEST 9: Discriminación de QR del SAT vs URL Comercial
# ---------------------------------------------------------------------------
def test_generic_web_sat_qr_discrimination():
    # 1. URLs reales del SAT deben descartarse (retornan None)
    assert sanitize_and_classify_billing_url("https://verificacfdi.facturaelectronica.sat.gob.mx/default.aspx?id=12345678-ABCD-1234-ABCD-1234567890AB&re=XAXX010101000") is None
    assert sanitize_and_classify_billing_url("https://consulta.sat.gob.mx/consulta_cfdi/index.html") is None
    assert sanitize_and_classify_billing_url("http://www.sat.gob.mx/sitio_internet/asistencia_contribuyente/informacion_frecuente/default.asp") is None
    assert sanitize_and_classify_billing_url("http://sat.gob.mx/consultas/cfdi") is None

    # 2. URL de facturación comercial legítima
    oxxo_url = "https://factura.oxxo.com/portal/login"
    assert sanitize_and_classify_billing_url(oxxo_url) == "https://factura.oxxo.com/portal/login"

    # 3. Limpieza de puntuación y esquema faltante
    raw_url = "  factura.liverpool.com.mx/portal.,;  "
    assert sanitize_and_classify_billing_url(raw_url) == "https://factura.liverpool.com.mx/portal"


# ---------------------------------------------------------------------------
# TEST 10: Prueba en Vivo con Modelo Real de Anthropic (Excluida por defecto)
# ---------------------------------------------------------------------------
@pytest.mark.live
@pytest.mark.asyncio
async def test_generic_web_live_anthropic_model(setup_phase4_scenario, owner_session: AsyncSession):
    """
    PRUEBA EN VIVO: Llama a la API real de Anthropic usando ANTHROPIC_MODEL_AGENTE contra /simple.
    Excluida por defecto para no generar costo en pytest rutinario.
    Ejecutar manualmente con:
      uv run pytest -m live tests/test_generic_web_engine.py
    """
    if not settings.ANTHROPIC_API_KEY:
        pytest.skip("Se requiere ANTHROPIC_API_KEY configurada para ejecutar la prueba en vivo.")

    scenario = setup_phase4_scenario
    t_id = scenario["tenant_id"]
    u_id = scenario["user_id"]
    fp_id = scenario["fiscal_profile_id"]

    async with run_mock_portal_server() as base_url:
        portal_url = f"{base_url}/simple"

        live_brain = AnthropicAgentBrain(model=settings.ANTHROPIC_MODEL_AGENTE)
        engine = GenericWebEngine(brain=live_brain)

        ticket_id = uuid.uuid4()
        ticket = Ticket(
            id=ticket_id,
            tenant_id=t_id,
            created_by=u_id,
            fiscal_profile_id=fp_id,
            merchant_id=None,
            estado=TicketEstado.ENCOLADO,
            folio="FOLIO-LIVE-001",
            total=Decimal("1284.50"),
            url_facturacion=portal_url,
        )
        await owner_session.execute(text("SELECT set_config('app.tenant_id', :t, true)"), {"t": str(t_id)})
        owner_session.add(ticket)
        await owner_session.commit()

        async with tenant_session(t_id) as session:
            t_res = await session.execute(select(Ticket).where(Ticket.id == ticket_id))
            t_obj = t_res.scalar_one()
            fp_res = await session.execute(select(FiscalProfile).where(FiscalProfile.id == fp_id))
            fp_obj = fp_res.scalar_one()

            ctx = EngineContext(ticket=t_obj, perfil_fiscal=fp_obj, merchant=None, _session=session, _tenant_id=t_id)
            result = await engine.facturar(ctx)

            # Imprimir desglose para auditoría
            print("\n" + "=" * 60)
            print(f"RESULTADO EN VIVO ({settings.ANTHROPIC_MODEL_AGENTE}):")
            print(f"  OK: {result.ok}")
            print(f"  CFDI UUID: {result.cfdi_uuid}")
            print(f"  Pasos: {result.pasos}")
            print(f"  Tokens IN: {result.tokens_input}")
            print(f"  Tokens OUT: {result.tokens_output}")
            print(f"  Costo USD: ${result.costo_usd}")
            print(f"  Duración: {result.duracion_segundos} s")
            print("\n--- EVIDENCIA CRUDA DE RESPUESTAS ANTHROPIC (POR PASO) ---")
            for c in getattr(live_brain, "last_calls", []):
                print(f"Paso {c['paso']}:")
                print(f"  Request ID: {c['request_id']}")
                print(f"  Message ID: {c['message_id']}")
                print(f"  Usage: {json.dumps(c['usage'])}")
            print("=" * 60)

            assert result.ok is True
            assert result.cfdi_uuid is not None


# ---------------------------------------------------------------------------
# TEST 10: Detección nativa de diálogo 'El ticket ya se encuentra facturado'
# ---------------------------------------------------------------------------
@pytest.mark.asyncio
async def test_generic_web_detects_already_invoiced_dialog(setup_phase4_scenario, owner_session: AsyncSession):
    scenario = setup_phase4_scenario
    t_id = scenario["tenant_id"]
    u_id = scenario["user_id"]
    fp_id = scenario["fiscal_profile_id"]

    async with run_mock_portal_server() as base_url:
        portal_url = f"{base_url}/alerta-ya-facturado"

        fake_brain = FakeAgentBrain([
            AgentDecision(
                tipo="batch_actions",
                batch=[BrowserAction(tipo="type", selector="#folio", texto="0584103332625683")],
            ),
        ])

        engine = GenericWebEngine(brain=fake_brain)

        ticket_id = uuid.uuid4()
        ticket = Ticket(
            id=ticket_id,
            tenant_id=t_id,
            created_by=u_id,
            fiscal_profile_id=fp_id,
            merchant_id=None,
            estado=TicketEstado.ENCOLADO,
            folio="0584103332625683",
            total=Decimal("289.00"),
            url_facturacion=portal_url,
        )
        await owner_session.execute(text("SELECT set_config('app.tenant_id', :t, true)"), {"t": str(t_id)})
        owner_session.add(ticket)
        await owner_session.commit()

        async with tenant_session(t_id) as session:
            t_res = await session.execute(select(Ticket).where(Ticket.id == ticket_id))
            t_obj = t_res.scalar_one()
            fp_res = await session.execute(select(FiscalProfile).where(FiscalProfile.id == fp_id))
            fp_obj = fp_res.scalar_one()

            ctx = EngineContext(ticket=t_obj, perfil_fiscal=fp_obj, merchant=None, _session=session, _tenant_id=t_id)
            result = await engine.facturar(ctx)

            assert result.ok is False
            assert result.error_code == "ticket_ya_facturado"
            assert result.reintentable is False
            assert "ya se encuentra facturado" in (result.mensaje or "").lower()

