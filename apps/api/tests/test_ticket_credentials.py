import json
import uuid
import pytest
from httpx import ASGITransport, AsyncClient
from sqlalchemy import select, text
from sqlalchemy.ext.asyncio import AsyncSession

from src.auth import sign_session_token
from src.main import app
from src.models import (
    FiscalProfile,
    Membership,
    MembershipRole,
    Merchant,
    MerchantCredential,
    Tenant,
    Ticket,
    TicketEstado,
    TipoMotor,
    User,
)
from src.security import decrypt_credentials


@pytest.mark.asyncio
async def test_ticket_credentials_endpoint(owner_session: AsyncSession):
    """
    Verifica que POST /v1/tickets/{ticket_id}/credentials almacene las credenciales
    cifradas con AES-256-GCM, asocie el comercio y transicione el ticket a 'encolado'.
    """
    tenant_id = uuid.uuid4()
    user_id = uuid.uuid4()
    ticket_id = uuid.uuid4()

    tenant = Tenant(id=tenant_id, nombre="Tenant Creds", slug=f"tc-{tenant_id.hex[:6]}", plan="pro")
    user = User(id=user_id, email=f"user-{user_id.hex[:6]}@test.com", nombre="User Creds", google_sub=f"sub-{user_id.hex}")
    g500_merchant = Merchant(
        id=uuid.uuid4(),
        slug=f"g500-{uuid.uuid4().hex[:6]}",
        nombre="G500 Test",
        patrones=["g500", "fento"],
        tipo_motor=TipoMotor.WEB,
        engine_slug="generico-web",
    )

    owner_session.add_all([tenant, user, g500_merchant])
    await owner_session.flush()

    # Activar RLS para el tenant
    await owner_session.execute(text("SELECT set_config('app.tenant_id', :t, true)"), {"t": str(tenant_id)})

    membership = Membership(tenant_id=tenant_id, user_id=user_id, rol=MembershipRole.OWNER)
    profile = FiscalProfile(
        id=uuid.uuid4(),
        tenant_id=tenant_id,
        rfc="XAXX010101000",
        razon_social="EMPRESA TEST",
        regimen_fiscal="601",
        cp="06000",
        email_receptor="facturas@test.com",
        es_principal=True,
    )
    ticket = Ticket(
        id=ticket_id,
        tenant_id=tenant_id,
        created_by=user_id,
        estado=TicketEstado.EXTRAIDO,
        url_facturacion="https://g500facturagas.azurewebsites.net/?PermisoCRE=PL/1434/EXP/ES/2015",
        folio="2404311",
        web_id="TUUQKO-MSMXQL-LRTLOO-VVVNW",
    )

    owner_session.add_all([membership, profile, ticket])
    await owner_session.commit()

    token = sign_session_token(user_id, user.email)

    async with AsyncClient(
        transport=ASGITransport(app=app),
        base_url="http://testserver",
        headers={
            "Authorization": f"Bearer {token}",
            "X-Tenant-Id": str(tenant_id),
        },
    ) as client:
        resp = await client.post(
            f"/v1/tickets/{ticket_id}/credentials",
            json={
                "usuario": "emmanuelnavaromero02@gmail.com",
                "password": "MiPasswordSeguro123#",
                "merchant_id": str(g500_merchant.id),
                "facturar_ahora": True,
            },
        )

        assert resp.status_code == 200, resp.text
        data = resp.json()
        assert data["id"] == str(ticket_id)
        assert data["estado"] == "encolado"

    # Verificar directamente en la base de datos que se haya guardado cifrado
    await owner_session.execute(text("SELECT set_config('app.tenant_id', :t, true)"), {"t": str(tenant_id)})
    cred_res = await owner_session.execute(
        select(MerchantCredential).where(
            MerchantCredential.tenant_id == tenant_id,
            MerchantCredential.merchant_id == g500_merchant.id,
        )
    )
    cred = cred_res.scalar_one()
    assert cred.payload_enc != b"MiPasswordSeguro123#"
    assert cred.nonce is not None

    # Descifrar y validar contenido
    decrypted_bytes = decrypt_credentials(tenant_id, cred.payload_enc, cred.nonce)
    decrypted_json = json.loads(decrypted_bytes.decode("utf-8"))
    assert decrypted_json["usuario"] == "emmanuelnavaromero02@gmail.com"
    assert decrypted_json["password"] == "MiPasswordSeguro123#"


@pytest.mark.asyncio
async def test_ticket_credentials_auto_deduces_merchant(owner_session: AsyncSession):
    """
    Verifica que si no se proporciona merchant_id, deduzca automáticamente
    el comercio por la URL (ej. g500) y asocie el ticket.
    """
    tenant_id = uuid.uuid4()
    user_id = uuid.uuid4()
    ticket_id = uuid.uuid4()

    tenant = Tenant(id=tenant_id, nombre="Tenant Auto", slug=f"ta-{tenant_id.hex[:6]}", plan="pro")
    user = User(id=user_id, email=f"user-{user_id.hex[:6]}@test.com", nombre="User Auto", google_sub=f"sub-{user_id.hex}")

    # Obtener el comercio g500 existente o crearlo si no existe
    m_res = await owner_session.execute(select(Merchant).where(Merchant.slug == "g500"))
    g500_merchant = m_res.scalars().first()
    if not g500_merchant:
        g500_merchant = Merchant(
            id=uuid.uuid4(),
            slug="g500",
            nombre="G500 Network",
            patrones=["g500"],
            tipo_motor=TipoMotor.WEB,
            engine_slug="generico-web",
        )
        owner_session.add(g500_merchant)

    owner_session.add_all([tenant, user])
    await owner_session.flush()

    await owner_session.execute(text("SELECT set_config('app.tenant_id', :t, true)"), {"t": str(tenant_id)})

    membership = Membership(tenant_id=tenant_id, user_id=user_id, rol=MembershipRole.OWNER)
    profile = FiscalProfile(
        id=uuid.uuid4(),
        tenant_id=tenant_id,
        rfc="XAXX010101000",
        razon_social="EMPRESA TEST",
        regimen_fiscal="601",
        cp="06000",
        email_receptor="facturas@test.com",
        es_principal=True,
    )
    ticket = Ticket(
        id=ticket_id,
        tenant_id=tenant_id,
        created_by=user_id,
        estado=TicketEstado.EXTRAIDO,
        url_facturacion="https://g500facturagas.azurewebsites.net/?PermisoCRE=PL/1434/EXP/ES/2015",
        folio="2404311",
        web_id="TUUQKO-MSMXQL-LRTLOO-VVVNW",
    )

    owner_session.add_all([membership, profile, ticket])
    await owner_session.commit()

    token = sign_session_token(user_id, user.email)

    async with AsyncClient(
        transport=ASGITransport(app=app),
        base_url="http://testserver",
        headers={
            "Authorization": f"Bearer {token}",
            "X-Tenant-Id": str(tenant_id),
        },
    ) as client:
        # No enviamos merchant_id para que lo deduzca por la URL de g500
        resp = await client.post(
            f"/v1/tickets/{ticket_id}/credentials",
            json={
                "usuario": "emmanuelnavaromero02@gmail.com",
                "password": "MiPasswordSeguro123#",
                "facturar_ahora": True,
            },
        )

        assert resp.status_code == 200, resp.text

    await owner_session.execute(text("SELECT set_config('app.tenant_id', :t, true)"), {"t": str(tenant_id)})
    t_res = await owner_session.execute(select(Ticket).where(Ticket.id == ticket_id))
    updated_ticket = t_res.scalar_one()
    assert updated_ticket.merchant_id == g500_merchant.id
