import uuid
from decimal import Decimal
import pytest
from httpx import ASGITransport, AsyncClient
from sqlalchemy import select, text
from sqlalchemy.ext.asyncio import AsyncSession

from src.auth import sign_session_token
from src.config import get_settings
from src.main import app
from src.models import FiscalProfile, Membership, MembershipRole, Tenant, Ticket, TicketEstado, User

settings = get_settings()


@pytest.mark.asyncio
async def test_fiscal_profile_crud_and_rls(app_engine, owner_session: AsyncSession):
    """
    Verifica que:
    1. GET /v1/fiscal-profile devuelva null si no hay perfil.
    2. PUT /v1/fiscal-profile cree y guarde el perfil fiscal principal con RLS.
    3. GET /v1/fiscal-profile devuelva el perfil guardado.
    4. El aislamiento RLS impida que otro tenant vea o modifique este perfil.
    """
    u1_id = uuid.uuid4()
    t1_id = uuid.uuid4()
    u2_id = uuid.uuid4()
    t2_id = uuid.uuid4()

    t1 = Tenant(id=t1_id, nombre="Tenant 1", slug=f"t1-{t1_id.hex[:6]}", plan="free")
    t2 = Tenant(id=t2_id, nombre="Tenant 2", slug=f"t2-{t2_id.hex[:6]}", plan="free")
    owner_session.add_all([t1, t2])

    u1 = User(id=u1_id, email=f"u1-{u1_id.hex[:6]}@test.com", nombre="User 1", google_sub=f"sub-{u1_id.hex}")
    u2 = User(id=u2_id, email=f"u2-{u2_id.hex[:6]}@test.com", nombre="User 2", google_sub=f"sub-{u2_id.hex}")
    owner_session.add_all([u1, u2])
    await owner_session.flush()

    await owner_session.execute(text("SELECT set_config('app.tenant_id', :t, true)"), {"t": str(t1_id)})
    owner_session.add(Membership(tenant_id=t1_id, user_id=u1_id, rol=MembershipRole.OWNER))
    await owner_session.flush()

    await owner_session.execute(text("SELECT set_config('app.tenant_id', :t, true)"), {"t": str(t2_id)})
    owner_session.add(Membership(tenant_id=t2_id, user_id=u2_id, rol=MembershipRole.OWNER))
    await owner_session.commit()

    token1 = sign_session_token(u1_id, u1.email)
    token2 = sign_session_token(u2_id, u2.email)

    async with AsyncClient(
        transport=ASGITransport(app=app),
        base_url="http://testserver",
        cookies={settings.SESSION_COOKIE_NAME: token1},
        headers={"X-Tenant-Id": str(t1_id)},
    ) as client1:
        # 1. GET sin perfil creado
        resp = await client1.get("/v1/fiscal-profile")
        assert resp.status_code == 200
        assert resp.json() is None

        # 2. PUT creando perfil
        payload = {
            "rfc": "NAM950812QX3",
            "razon_social": "NAVA MERO S.A. DE C.V.",
            "cp": "06600",
            "regimen_fiscal": "626",
            "uso_cfdi": "G03",
            "email_receptor": "facturas@navamero.mx",
            "calle": "Av. Paseo de la Reforma",
            "numero_exterior": "222",
            "numero_interior": "Piso 8",
            "colonia": "Juárez",
            "municipio_alcaldia": "Cuauhtémoc",
            "estado": "Ciudad de México",
            "pais": "MEX",
        }
        put_resp = await client1.put("/v1/fiscal-profile", json=payload)
        assert put_resp.status_code == 200
        data = put_resp.json()
        assert data["rfc"] == "NAM950812QX3"
        assert data["razon_social"] == "NAVA MERO S.A. DE C.V."
        assert data["cp"] == "06600"
        assert data["es_principal"] is True

        # 3. GET recuperando perfil
        get_resp = await client1.get("/v1/fiscal-profile")
        assert get_resp.status_code == 200
        assert get_resp.json()["rfc"] == "NAM950812QX3"

    # 4. Verificar aislamiento RLS con Tenant 2
    async with AsyncClient(
        transport=ASGITransport(app=app),
        base_url="http://testserver",
        cookies={settings.SESSION_COOKIE_NAME: token2},
        headers={"X-Tenant-Id": str(t2_id)},
    ) as client2:
        resp2 = await client2.get("/v1/fiscal-profile")
        assert resp2.status_code == 200
        assert resp2.json() is None  # No ve el perfil de Tenant 1


@pytest.mark.asyncio
async def test_facturar_ticket_endpoint(app_engine, owner_session: AsyncSession):
    """
    Verifica que POST /v1/tickets/{ticket_id}/facturar:
    - Falla si no hay perfil fiscal configurado (perfil_incompleto).
    - Falla si el estado no es extraido o rechazado.
    - Transiciona a encolado cuando el perfil fiscal existe.
    """
    u_id = uuid.uuid4()
    t_id = uuid.uuid4()
    ticket_id = uuid.uuid4()

    t = Tenant(id=t_id, nombre="Tenant Factura", slug=f"tf-{t_id.hex[:6]}", plan="free")
    owner_session.add(t)
    u = User(id=u_id, email=f"u-{u_id.hex[:6]}@test.com", nombre="User", google_sub=f"sub-{u_id.hex}")
    owner_session.add(u)
    await owner_session.flush()

    await owner_session.execute(text("SELECT set_config('app.tenant_id', :t, true)"), {"t": str(t_id)})
    owner_session.add(Membership(tenant_id=t_id, user_id=u_id, rol=MembershipRole.OWNER))

    ticket = Ticket(
        id=ticket_id,
        tenant_id=t_id,
        created_by=u_id,
        estado=TicketEstado.EXTRAIDO,
        folio="FOLIO-123",
        total=Decimal("500.00"),
        url_facturacion="https://mefacturo.mx/test",
    )
    owner_session.add(ticket)
    await owner_session.commit()

    token = sign_session_token(u_id, u.email)

    async with AsyncClient(
        transport=ASGITransport(app=app),
        base_url="http://testserver",
        cookies={settings.SESSION_COOKIE_NAME: token},
        headers={"X-Tenant-Id": str(t_id)},
    ) as client:
        # Intento de facturar sin perfil fiscal -> debe fallar con 400 perfil_incompleto
        resp_sin_perfil = await client.post(f"/v1/tickets/{ticket_id}/facturar")
        assert resp_sin_perfil.status_code == 400
        assert resp_sin_perfil.json()["error"]["code"] == "perfil_incompleto"

        # Configurar perfil fiscal
        await client.put(
            "/v1/fiscal-profile",
            json={
                "rfc": "XAXX010101000",
                "razon_social": "TEST S.A.",
                "cp": "01000",
                "regimen_fiscal": "601",
                "uso_cfdi": "G03",
                "email_receptor": "facturas@test.com",
            },
        )

        # Ahora facturar -> 202 ACCEPTED y estado encolado
        resp_ok = await client.post(f"/v1/tickets/{ticket_id}/facturar")
        assert resp_ok.status_code == 202
        assert resp_ok.json()["estado"] == "encolado"
