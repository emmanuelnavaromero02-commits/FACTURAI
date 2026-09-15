import uuid
import pytest
from src.models import Ticket
from src.services.portal_searcher import (
    clean_search_term,
    search_candidate_portal_urls,
    verify_portal_matches_ticket,
)
from tests.mock_portal_server import run_mock_portal_server


def test_clean_search_term():
    assert clean_search_term("RESTAURANTES TOKS S.A. DE C.V.") == "TOKS"
    assert clean_search_term("OPERADORA DE ALIMENTOS PRB S.A.P.I. DE C.V.") == "DE ALIMENTOS PRB"
    assert clean_search_term("KFC SAN JUAN DEL RIO") == "KFC SAN JUAN DEL RIO"


@pytest.mark.asyncio
async def test_search_candidate_portal_urls_domain_variants():
    cands = await search_candidate_portal_urls(
        comercio="Toks",
        rfc_emisor="RTC840921RE4",
        current_url="https://www.toks.com.mx/menu",
    )
    assert len(cands) > 0
    # Should include domain variants for toks.com.mx
    assert any("toks.com.mx" in c for c in cands)


@pytest.mark.asyncio
async def test_search_candidate_portal_urls_mexican_chains():
    # Soriana
    soriana_cands = await search_candidate_portal_urls(comercio="Tiendas Soriana", rfc_emisor="TSO991022PB6")
    assert any("soriana.com" in c for c in soriana_cands)

    # Costco
    costco_cands = await search_candidate_portal_urls(comercio="Costco Wholesale", rfc_emisor="CCM891107490")
    assert any("costco.com.mx" in c for c in costco_cands)

    # Farmacias del Ahorro
    ahorro_cands = await search_candidate_portal_urls(comercio="Farmacias del Ahorro")
    assert any("fahorro.com.mx" in c for c in ahorro_cands)

    # Little Caesars
    lc_cands = await search_candidate_portal_urls(comercio="Little Caesars Pizza")
    assert any("littlecaesars.com.mx" in c or "facturacionpremier.mx" in c for c in lc_cands)


@pytest.mark.asyncio
async def test_verify_portal_matches_ticket_with_page():
    from playwright.async_api import async_playwright

    async with run_mock_portal_server() as base_url:
        ticket = Ticket(
            id=uuid.uuid4(),
            tenant_id=uuid.uuid4(),
            created_by=uuid.uuid4(),
            rfc_emisor="RTC840921RE4",
            sucursal="SUC. 300 SAN JUAN DEL RIO",
            extracted={"comercio": "Restaurantes Toks"},
        )

        async with async_playwright() as p:
            browser = await p.chromium.launch(headless=True, args=["--no-sandbox"])
            page = await browser.new_page()

            # 1. Página mock simple con formulario pero sin mención a Toks
            await page.goto(f"{base_url}/simple")
            # /simple no menciona Toks ni RTC840921RE4
            matches_simple = await verify_portal_matches_ticket(page, ticket)
            assert matches_simple is False

            # 2. Página con contenido que incluye el RFC emisor
            await page.set_content("""
                <html>
                <head><title>Portal Facturación Toks</title></head>
                <body>
                    <h1>Bienvenido al portal de Toks</h1>
                    <p>RFC: RTC840921RE4</p>
                    <form><input id="folio" /><button type="submit">Facturar</button></form>
                </body>
                </html>
            """)
            matches_toks = await verify_portal_matches_ticket(page, ticket)
            assert matches_toks is True

            await browser.close()


@pytest.mark.asyncio
async def test_deduce_portal_for_ticket_known_chain():
    from src.services.portal_searcher import deduce_portal_for_ticket

    # Deduce from brand keyword
    portal_oxxo = await deduce_portal_for_ticket(comercio="Cadena Comercial Oxxo")
    assert portal_oxxo and "oxxo" in portal_oxxo.lower()

    # Deduce from RFC
    portal_walmart = await deduce_portal_for_ticket(rfc_emisor="NWM9709244W4")
    assert portal_walmart and "walmart" in portal_walmart.lower()

    # Unknown merchant does not return blind synthetic portal
    portal_unknown = await deduce_portal_for_ticket(comercio="Comercio Inexistente XYZ 12345", rfc_emisor="ZZZZ999999ZZZ")
    assert portal_unknown is None


@pytest.mark.asyncio
async def test_update_ticket_portal_and_deduce_endpoints():
    import io
    from httpx import AsyncClient, ASGITransport
    from src.main import app
    from src.config import get_settings
    from tests.test_phase3_tickets_and_vision import VALID_JPEG_HEADER

    settings = get_settings()
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        unique_email = f"portal.{uuid.uuid4().hex[:8]}@test.com"
        log_res = await client.post("/v1/auth/google", json={"id_token": f"mock:{unique_email}"})
        cookie = log_res.cookies.get(settings.SESSION_COOKIE_NAME)
        tenant_id = log_res.json()["created_tenant_id"]

    async with AsyncClient(
        transport=transport,
        base_url="http://test",
        cookies={settings.SESSION_COOKIE_NAME: cookie},
        headers={"X-Tenant-Id": tenant_id},
    ) as auth_client:
        # Subir un ticket
        resp_upload = await auth_client.post(
            "/v1/tickets",
            files={"file": ("ticket.jpg", io.BytesIO(VALID_JPEG_HEADER), "image/jpeg")},
        )
        assert resp_upload.status_code == 202
        ticket_id = resp_upload.json()["id"]

        # 1. Probar deducir portal endpoint
        resp_deduce = await auth_client.post(f"/v1/tickets/{ticket_id}/deduce-portal")
        assert resp_deduce.status_code == 200
        data_deduce = resp_deduce.json()
        assert "candidates" in data_deduce
        assert data_deduce["ticket_id"] == ticket_id

        # 2. Probar actualizar URL del portal manualmente
        custom_url = "https://facturacion.ejemplo-local.com.mx/portal"
        resp_patch = await auth_client.patch(
            f"/v1/tickets/{ticket_id}/portal",
            json={"url_facturacion": custom_url, "facturar_ahora": False},
        )
        assert resp_patch.status_code == 200
        patched_ticket = resp_patch.json()
        assert patched_ticket["url_facturacion"] == custom_url
        assert patched_ticket["error_code"] is None

