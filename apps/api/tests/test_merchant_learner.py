import asyncio
from datetime import date
from decimal import Decimal
import time
import uuid
from playwright.async_api import async_playwright
import pytest

from src.engines.candidate_generator import generate_ticket_candidate_variants
from src.engines.captcha_solver import try_solve_captcha_autonomously
from src.models import FiscalProfile, Ticket
from src.services.merchant_learner import extract_portal_recipe_from_page, learn_merchant_recipe, slugify


def test_slugify():
    assert slugify("Domino's Pizza México") == "dominos-pizza-mexico"
    assert slugify("Starbucks Coffee (Alsea)") == "starbucks-coffee-alsea"
    assert slugify("") == "comercio"


def test_learned_rules_prioritize_candidates():
    t = Ticket(
        id=uuid.uuid4(),
        tenant_id=uuid.uuid4(),
        created_by=uuid.uuid4(),
        folio="181103",
        sucursal="Sucursal Centro Tienda 31681",
        total=Decimal("250.00"),
        fecha_ticket=date(2026, 9, 14),
    )
    p = FiscalProfile(
        id=uuid.uuid4(),
        tenant_id=t.tenant_id,
        rfc="XAXX010101000",
        razon_social="EMPRESA DEMO S.A. DE C.V.",
        cp="06600",
        regimen_fiscal="601",
        uso_cfdi="G03",
        email_receptor="demo@empresa.com",
    )

    # 1. Sin merchant_config: folios sin orden forzado por longitud
    cands_raw = generate_ticket_candidate_variants(t, p)
    assert "181103" in cands_raw["folios"]

    # 2. Con receta aprendida de Alsea (9 dígitos ticket, 5 dígitos tienda, dd/mm/aaaa)
    learned_config = {
        "reglas": {
            "ticket_digits": 9,
            "tienda_digits": 5,
            "fecha_format": "dd/mm/aaaa",
        }
    }
    cands_learned = generate_ticket_candidate_variants(t, p, merchant_config=learned_config)

    # El candidato principal de folio debe tener 9 dígitos
    assert len(cands_learned["folios"][0]) == 9
    assert cands_learned["folios"][0] == "000181103"

    # La tienda debe ser 5 dígitos exactamente
    assert cands_learned["tienda"] == "31681"
    assert cands_learned["tiendas"][0] == "31681"

    # La fecha debe estar en formato dd/mm/aaaa
    assert cands_learned["fechas"][0] == "14/09/2026"


@pytest.mark.asyncio
async def test_extract_portal_recipe_from_page():
    async with async_playwright() as p:
        browser = await p.chromium.launch(headless=True, args=["--no-sandbox"])
        page = await browser.new_page()

        await page.set_content("""
            <!DOCTYPE html>
            <html>
            <head><title>Portal Facturación Demo</title></head>
            <body>
                <form id="facturacion-form">
                    <input id="txtTicket" name="ticket_num" placeholder="Número de Ticket" maxlength="9" />
                    <input id="txtTienda" name="tienda_id" placeholder="No. Tienda / Sucursal" maxlength="5" />
                    <input id="txtFecha" name="fecha_consumo" placeholder="dd/mm/aaaa" />
                    <input id="txtRFC" name="rfc_receptor" placeholder="RFC del Cliente" />
                    <input id="txtTotal" name="importe_total" placeholder="Total Pagado" />
                </form>
            </body>
            </html>
        """)

        dummy_ticket = Ticket(
            id=uuid.uuid4(),
            tenant_id=uuid.uuid4(),
            created_by=uuid.uuid4(),
            rfc_emisor="DEM010101AA1",
            folio="123456789",
        )

        recipe = await extract_portal_recipe_from_page(page, dummy_ticket)
        await browser.close()

        assert "ticket" in recipe["campos_detectados"]
        assert "tienda" in recipe["campos_detectados"]
        assert "fecha" in recipe["campos_detectados"]
        assert "rfc" in recipe["campos_detectados"]
        assert "total" in recipe["campos_detectados"]

        assert recipe["selectores"]["ticket"] == "#txtTicket"
        assert recipe["selectores"]["tienda"] == "#txtTienda"
        assert recipe["selectores"]["fecha"] == "#txtFecha"
        assert recipe["selectores"]["rfc"] == "#txtRFC"
        assert recipe["selectores"]["total"] == "#txtTotal"

        assert recipe["reglas"]["ticket_maxlength"] == 9
        assert recipe["reglas"]["tienda_maxlength"] == 5
        assert recipe["reglas"]["fecha_format"] == "dd/mm/aaaa"


@pytest.mark.asyncio
async def test_learn_merchant_recipe_db_persistence():
    dummy_ticket = Ticket(
        id=uuid.uuid4(),
        tenant_id=uuid.uuid4(),
        created_by=uuid.uuid4(),
        rfc_emisor="TST990101AB1",
        sucursal="Comercio Test Auto-Learned",
        extracted={"comercio": "Comercio Test Auto-Learned"},
    )
    recipe = {
        "campos_detectados": ["ticket", "total"],
        "selectores": {"ticket": "#folio", "total": "#monto"},
        "reglas": {"ticket_digits": 8},
    }

    ok = await learn_merchant_recipe(dummy_ticket, "https://facturacion.test-merchant.com", recipe)
    assert ok is True


@pytest.mark.asyncio
async def test_fast_captcha_timeout_under_3_5s():
    """Garantiza que la resolución autónoma de captcha no congele el sistema y corte en <= 3.5s."""
    async with async_playwright() as p:
        browser = await p.chromium.launch(headless=True, args=["--no-sandbox"])
        page = await browser.new_page()

        # HTML simulando Turnstile que no responde
        await page.set_content("""
            <!DOCTYPE html>
            <html>
            <head><title>Just a moment...</title></head>
            <body>
                <iframe src="https://challenges.cloudflare.com/cdn-cgi/challenge-platform/h/g/turnstile" id="cf-iframe"></iframe>
            </body>
            </html>
        """)

        t0 = time.time()
        res = await try_solve_captcha_autonomously(page)
        elapsed = time.time() - t0
        await browser.close()

        assert res is False
        assert elapsed < 4.0, f"Resolución de captcha tardó demasiado ({elapsed:.2f}s), debe ser <= 3.5s"
