"""
Pruebas del manejo de portales de facturación:
- el motor arranca con la URL del ticket y usa la del catálogo como respaldo,
- se saltan dominios que no existen,
- el worker no repite portales ya probados y guarda la lista en la base,
- el catálogo de Liverpool y Suburbia apunta al portal real con la marca correcta.
"""
import socket
import uuid
from decimal import Decimal
from typing import Any, Dict, List

import pytest
from sqlalchemy import select, text

from src.db import tenant_session
from src.engines.base import EngineContext, EngineResult, register_engine
from src.engines.generic_web import GenericWebEngine
from src.models import Merchant, Ticket, TicketEstado, TipoMotor
from src.seed_merchants import OFFICIAL_MERCHANTS
from src.services import portal_searcher
from src.storage import InMemoryStorageService, set_storage_service
from src.vision import url_sanitizer
from src.vision.merchant_matcher import match_merchant_cascade
from src.vision.url_sanitizer import choose_start_url, filter_resolvable_urls, host_resolves
from src.worker import process_ticket_facturacion

PORTAL_MUERTO = "https://portal-muerto.invalid/"
ALTERNATIVA_MUERTA = "https://otro-portal-muerto.invalid/x"
ALT_1 = "http://127.0.0.1:9/alternativa-1"
ALT_2 = "http://127.0.0.1:9/alternativa-2"


async def _resolver_falso(url, timeout: float = 3.0) -> bool:
    return bool(url) and ".invalid" not in url


# ---------------------------------------------------------------------------
# Verificación de dominios
# ---------------------------------------------------------------------------
@pytest.mark.asyncio
async def test_host_resolves_distinguishes_missing_domain_from_network_glitch(monkeypatch):
    assert await host_resolves("http://127.0.0.1:8000/x") is True
    assert await host_resolves(None) is False
    assert await host_resolves("") is False

    def dns_que_falla(exc):
        async def _falso(host):
            raise exc
        return _falso

    # El DNS confirma que el dominio no existe: se descarta
    monkeypatch.setattr(url_sanitizer, "_getaddrinfo", dns_que_falla(socket.gaierror(socket.EAI_NONAME, "no existe")))
    assert await host_resolves("https://facturacion.liverpool.com.mx") is False

    # Falla temporal de red: no se descarta un portal que podría ser bueno
    monkeypatch.setattr(url_sanitizer, "_getaddrinfo", dns_que_falla(socket.gaierror(socket.EAI_AGAIN, "temporal")))
    assert await host_resolves("https://facturacionclientes.liverpool.com.mx") is True

    # Respuesta correcta del DNS
    async def dns_ok(host):
        return [("ok",)]
    monkeypatch.setattr(url_sanitizer, "_getaddrinfo", dns_ok)
    assert await host_resolves("https://facturacionclientes.liverpool.com.mx") is True


@pytest.mark.asyncio
async def test_choose_start_url_and_filter(monkeypatch):
    monkeypatch.setattr(url_sanitizer, "host_resolves", _resolver_falso)

    # La primera que existe gana, en orden
    assert await choose_start_url(PORTAL_MUERTO, ALT_1, ALT_2) == ALT_1
    # Si ninguna existe, se devuelve la primera para que el motor busque en internet
    assert await choose_start_url(None, PORTAL_MUERTO, ALTERNATIVA_MUERTA) == PORTAL_MUERTO
    assert await choose_start_url(None, None) is None

    assert await filter_resolvable_urls([ALTERNATIVA_MUERTA, ALT_2, "", ALT_1]) == [ALT_2, ALT_1]


class _Obj:
    def __init__(self, **kw):
        self.__dict__.update(kw)


@pytest.mark.asyncio
async def test_engine_prefers_ticket_url_over_catalog(monkeypatch):
    monkeypatch.setattr(url_sanitizer, "host_resolves", _resolver_falso)
    motor = GenericWebEngine(brain=object())
    comercio = _Obj(config={"url_facturacion": ALT_2})

    # El portal que eligió el worker (o el usuario) en el ticket le gana al catálogo
    ctx = _Obj(ticket=_Obj(url_facturacion=ALT_1), merchant=comercio)
    assert await motor.resolve_start_url(ctx) == ALT_1

    # Si el ticket trae un portal cuyo dominio no existe, se usa el del catálogo
    ctx = _Obj(ticket=_Obj(url_facturacion=PORTAL_MUERTO), merchant=comercio)
    assert await motor.resolve_start_url(ctx) == ALT_2

    # Sin comercio se usa la del ticket
    ctx = _Obj(ticket=_Obj(url_facturacion=ALT_1), merchant=None)
    assert await motor.resolve_start_url(ctx) == ALT_1


# ---------------------------------------------------------------------------
# Ciclo real del worker con portales que fallan
# ---------------------------------------------------------------------------
@register_engine("prueba-portal-invalido")
class MotorPortalInvalido(GenericWebEngine):
    """Usa la misma elección de URL que el motor web, pero no abre navegador: siempre falla el portal."""

    urls_usadas: List[str] = []

    def __init__(self):
        self.brain = None

    async def facturar(self, ctx: EngineContext) -> EngineResult:
        MotorPortalInvalido.urls_usadas.append(await self.resolve_start_url(ctx))
        return EngineResult(
            ok=False,
            error_code="portal_invalido",
            mensaje="Portal no válido (prueba).",
            reintentable=False,
        )


@pytest.mark.asyncio
async def test_worker_tries_each_alternative_once_and_skips_dead_domains(
    setup_phase4_scenario: Dict[str, Any], owner_session, monkeypatch
):
    set_storage_service(InMemoryStorageService())
    monkeypatch.setattr(url_sanitizer, "host_resolves", _resolver_falso)

    async def candidatos_falsos(**kwargs):
        return [ALTERNATIVA_MUERTA, ALT_1, ALT_2]

    monkeypatch.setattr(portal_searcher, "search_candidate_portal_urls", candidatos_falsos)
    MotorPortalInvalido.urls_usadas = []

    t_id = setup_phase4_scenario["tenant_id"]
    m_id = uuid.uuid4()
    ticket_id = uuid.uuid4()
    owner_session.add(
        Merchant(
            id=m_id,
            nombre="Tienda con portal muerto",
            slug=f"portal-muerto-{m_id.hex[:6]}",
            tipo_motor=TipoMotor.WEB,
            engine_slug="prueba-portal-invalido",
            entrega_esperada="emisor",
            config={"url_facturacion": PORTAL_MUERTO},
            activo=True,
        )
    )
    await owner_session.flush()
    await owner_session.execute(text("SELECT set_config('app.tenant_id', :t, true)"), {"t": str(t_id)})
    owner_session.add(
        Ticket(
            id=ticket_id,
            tenant_id=t_id,
            created_by=setup_phase4_scenario["user_id"],
            merchant_id=m_id,
            estado=TicketEstado.ENCOLADO,
            folio=f"PORTAL-{ticket_id.hex[:8]}",
            total=Decimal("1232.00"),
            url_facturacion=PORTAL_MUERTO,
            extracted={"comercio": "Tienda con portal muerto"},
        )
    )
    await owner_session.commit()

    async def estado_ticket() -> Ticket:
        async with tenant_session(t_id) as session:
            return (await session.execute(select(Ticket).where(Ticket.id == ticket_id))).scalar_one()

    # Intento 1: arranca en el portal muerto del ticket; la alternativa muerta se descarta
    await process_ticket_facturacion(t_id, ticket_id)
    t = await estado_ticket()
    assert t.estado == TicketEstado.ENCOLADO
    assert t.url_facturacion == ALT_1
    # La lista de portales probados se guarda de verdad en la base
    assert set(t.extracted["_tried_portals"]) == {PORTAL_MUERTO, ALT_1}

    # Intento 2: el motor usa la alternativa que dejó el worker, no la del catálogo
    await process_ticket_facturacion(t_id, ticket_id)
    t = await estado_ticket()
    assert t.url_facturacion == ALT_2
    assert set(t.extracted["_tried_portals"]) == {PORTAL_MUERTO, ALT_1, ALT_2}

    # Intento 3: se agota el límite y pasa a espera de un humano
    await process_ticket_facturacion(t_id, ticket_id)
    t = await estado_ticket()
    assert t.estado == TicketEstado.ESPERA_HUMANO

    assert MotorPortalInvalido.urls_usadas == [PORTAL_MUERTO, ALT_1, ALT_2]

    async with tenant_session(t_id) as session:
        await session.execute(text("DELETE FROM tickets WHERE id = :i"), {"i": ticket_id})
    await owner_session.execute(text("DELETE FROM merchants WHERE id = :m"), {"m": m_id})
    await owner_session.commit()


# ---------------------------------------------------------------------------
# Catálogo de Liverpool y Suburbia
# ---------------------------------------------------------------------------
def test_catalog_splits_liverpool_and_suburbia_with_brand_portal():
    comercios = [
        Merchant(slug=m["slug"], nombre=m["nombre"], patrones=m["patrones"], activo=True, config=m["config"])
        for m in OFFICIAL_MERCHANTS
    ]
    suburbia = match_merchant_cascade(comercios, rfc_emisor="SUB910603SB3", comercio_nombre="Suburbia")
    liverpool = match_merchant_cascade(comercios, comercio_nombre="Liverpool")

    assert suburbia.slug == "suburbia"
    assert suburbia.config["url_facturacion"].endswith("&uid=suburbia")
    assert liverpool.slug == "liverpool-suburbia"
    assert liverpool.config["url_facturacion"].endswith("&uid=liverpool")

    # El dominio anterior no existe en DNS; no debe quedar en el catálogo
    assert all("facturacion.liverpool.com.mx" not in str(m["config"]) for m in OFFICIAL_MERCHANTS)
