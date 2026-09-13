import uuid
from decimal import Decimal
import pytest
from httpx import ASGITransport, AsyncClient
from sqlalchemy import select, text
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from src.auth import (
    FakeGoogleTokenVerifier,
    GoogleTokenPayload,
    set_google_verifier,
    sign_session_token,
)
from src.config import get_settings
from src.db import sin_tenant
from src.main import app
from src.models import Membership, MembershipRole, Tenant, User

settings = get_settings()


@pytest.fixture(autouse=True)
def setup_fake_verifier():
    """Configura el verificador falso de Google para todos los tests."""
    fake_verifier = FakeGoogleTokenVerifier()
    set_google_verifier(fake_verifier)
    yield fake_verifier


@pytest.mark.asyncio
async def test_user_sees_own_memberships_without_tenant_fixed(app_engine, owner_session: AsyncSession):
    """
    PRUEBA: El usuario ve sus propias membresías sin tenant fijado (usando app.user_id),
    y NO ve las de otro usuario. Valida la migración 0002 con la policy 'propias_membresias'.
    """
    u1_id = uuid.uuid4()
    u2_id = uuid.uuid4()
    t1_id = uuid.uuid4()
    t2_id = uuid.uuid4()
    t3_id = uuid.uuid4()

    # Montar tenants y usuarios con facturia_owner
    t1 = Tenant(id=t1_id, nombre="Empresa 1", slug=f"emp-1-{t1_id.hex[:6]}", plan="free")
    t2 = Tenant(id=t2_id, nombre="Empresa 2", slug=f"emp-2-{t2_id.hex[:6]}", plan="free")
    t3 = Tenant(id=t3_id, nombre="Empresa 3", slug=f"emp-3-{t3_id.hex[:6]}", plan="free")
    owner_session.add_all([t1, t2, t3])

    u1 = User(id=u1_id, email=f"u1-{u1_id.hex[:6]}@test.com", nombre="User 1", google_sub=f"sub-{u1_id.hex}")
    u2 = User(id=u2_id, email=f"u2-{u2_id.hex[:6]}@test.com", nombre="User 2", google_sub=f"sub-{u2_id.hex}")
    owner_session.add_all([u1, u2])
    await owner_session.flush()

    # Usuario 1 pertenece a Tenant 1 y Tenant 2
    # Usuario 2 pertenece a Tenant 3
    # Fijamos app.tenant_id en el montaje para satisfacer la policy de memberships
    await owner_session.execute(text("SELECT set_config('app.tenant_id', :t, true)"), {"t": str(t1_id)})
    owner_session.add(Membership(tenant_id=t1_id, user_id=u1_id, rol=MembershipRole.OWNER))
    await owner_session.flush()

    await owner_session.execute(text("SELECT set_config('app.tenant_id', :t, true)"), {"t": str(t2_id)})
    owner_session.add(Membership(tenant_id=t2_id, user_id=u1_id, rol=MembershipRole.ADMIN))
    await owner_session.flush()

    await owner_session.execute(text("SELECT set_config('app.tenant_id', :t, true)"), {"t": str(t3_id)})
    owner_session.add(Membership(tenant_id=t3_id, user_id=u2_id, rol=MembershipRole.VIEWER))
    await owner_session.flush()
    await owner_session.commit()

    # Ahora consultamos con facturia_app (rol no-dueño) SIN fijar app.tenant_id
    session_factory = async_sessionmaker(bind=app_engine, class_=AsyncSession, expire_on_commit=False)

    # 1. Con app.user_id = u1_id, Usuario 1 debe ver únicamente sus 2 membresías (t1 y t2)
    async with session_factory() as session:
        async with session.begin():
            await session.execute(text("SELECT set_config('app.tenant_id', '', true)"))
            await session.execute(text("SELECT set_config('app.user_id', :u, true)"), {"u": str(u1_id)})
            result = await session.execute(select(Membership))
            memberships = result.scalars().all()
            tenant_ids = {m.tenant_id for m in memberships}

            assert len(memberships) == 2
            assert tenant_ids == {t1_id, t2_id}
            assert t3_id not in tenant_ids, "No debe ver la membresía de Usuario 2 en Tenant 3"

    # 2. Con app.user_id = u2_id, Usuario 2 ve únicamente su membresía (t3)
    async with session_factory() as session:
        async with session.begin():
            await session.execute(text("SELECT set_config('app.tenant_id', '', true)"))
            await session.execute(text("SELECT set_config('app.user_id', :u, true)"), {"u": str(u2_id)})
            result = await session.execute(select(Membership))
            memberships = result.scalars().all()
            assert len(memberships) == 1
            assert memberships[0].tenant_id == t3_id

    # 3. Sin fijar app.user_id ni app.tenant_id, ve 0 membresías
    async with session_factory() as session:
        async with session.begin():
            await session.execute(text("SELECT set_config('app.tenant_id', '', true)"))
            await session.execute(text("SELECT set_config('app.user_id', '', true)"))
            result = await session.execute(select(Membership))
            assert len(result.scalars().all()) == 0


@pytest.mark.asyncio
async def test_id_token_without_email_verified_is_rejected():
    """PRUEBA: Un id_token sin email_verified en true es rechazado con mensaje claro."""
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        response = await client.post(
            "/v1/auth/google",
            json={"id_token": "mock_unverified:unverified.user@test.com"},
        )

    assert response.status_code == 400
    data = response.json()
    assert data["error"]["code"] == "email_no_verificado"
    assert "correo electrónico verificado" in data["error"]["message"].lower()


@pytest.mark.asyncio
async def test_id_token_with_wrong_aud_is_rejected():
    """PRUEBA: Un id_token con audiencia (aud) equivocada es rechazado."""
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        response = await client.post(
            "/v1/auth/google",
            json={"id_token": "mock_invalid_aud:user@test.com"},
        )

    assert response.status_code == 401
    data = response.json()
    assert data["error"]["code"] == "token_invalido"
    assert "audiencia" in data["error"]["message"].lower()


@pytest.mark.asyncio
async def test_user_of_one_tenant_cannot_read_team_of_another(owner_session: AsyncSession):
    """PRUEBA: Un usuario de un tenant no puede leer el equipo de otro (devuelve 403 sin revelar existencia)."""
    t_a = uuid.uuid4()
    t_b = uuid.uuid4()
    u_a = uuid.uuid4()

    tenant_a = Tenant(id=t_a, nombre="Empresa A", slug=f"emp-a-{t_a.hex[:6]}", plan="free")
    tenant_b = Tenant(id=t_b, nombre="Empresa B", slug=f"emp-b-{t_b.hex[:6]}", plan="free")
    user_a = User(id=u_a, email=f"user_a-{u_a.hex[:6]}@test.com", nombre="User A", google_sub=f"sub-{u_a.hex}")
    owner_session.add_all([tenant_a, tenant_b, user_a])
    await owner_session.flush()

    await owner_session.execute(text("SELECT set_config('app.tenant_id', :t, true)"), {"t": str(t_a)})
    owner_session.add(Membership(tenant_id=t_a, user_id=u_a, rol=MembershipRole.OWNER))
    await owner_session.commit()

    # Usuario A tiene cookie de sesión válida
    token = sign_session_token(u_a, user_a.email)

    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test", cookies={settings.SESSION_COOKIE_NAME: token}) as client:
        # Intenta consultar el equipo de Tenant B
        response = await client.get("/v1/team", headers={"X-Tenant-Id": str(t_b)})

    assert response.status_code == 403
    data = response.json()
    assert data["error"]["code"] == "sin_permiso"


@pytest.mark.asyncio
async def test_uploader_cannot_change_roles(owner_session: AsyncSession):
    """PRUEBA: Un usuario con rol 'uploader' no puede cambiar roles (403 rol_insuficiente)."""
    t_id = uuid.uuid4()
    u_uploader = uuid.uuid4()
    u_target = uuid.uuid4()

    tenant = Tenant(id=t_id, nombre="Empresa Uploader", slug=f"emp-up-{t_id.hex[:6]}", plan="free")
    user_up = User(id=u_uploader, email=f"up-{u_uploader.hex[:6]}@test.com", nombre="Uploader", google_sub=f"sub-{u_uploader.hex}")
    user_tgt = User(id=u_target, email=f"tgt-{u_target.hex[:6]}@test.com", nombre="Target", google_sub=f"sub-{u_target.hex}")
    owner_session.add_all([tenant, user_up, user_tgt])
    await owner_session.flush()

    await owner_session.execute(text("SELECT set_config('app.tenant_id', :t, true)"), {"t": str(t_id)})
    owner_session.add(Membership(tenant_id=t_id, user_id=u_uploader, rol=MembershipRole.UPLOADER))
    owner_session.add(Membership(tenant_id=t_id, user_id=u_target, rol=MembershipRole.VIEWER))
    await owner_session.commit()

    token = sign_session_token(u_uploader, user_up.email)

    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test", cookies={settings.SESSION_COOKIE_NAME: token}) as client:
        response = await client.patch(
            f"/v1/team/{u_target}",
            json={"rol": "admin"},
            headers={"X-Tenant-Id": str(t_id)},
        )

    assert response.status_code == 403
    data = response.json()
    assert data["error"]["code"] == "rol_insuficiente"


@pytest.mark.asyncio
async def test_nobody_can_assign_role_higher_than_own(owner_session: AsyncSession):
    """PRUEBA: Nadie puede asignar un rol superior al propio (admin intentando asignar owner -> 403)."""
    t_id = uuid.uuid4()
    u_admin = uuid.uuid4()
    u_target = uuid.uuid4()

    tenant = Tenant(id=t_id, nombre="Empresa Admin", slug=f"emp-adm-{t_id.hex[:6]}", plan="free")
    user_adm = User(id=u_admin, email=f"adm-{u_admin.hex[:6]}@test.com", nombre="Admin", google_sub=f"sub-{u_admin.hex}")
    user_tgt = User(id=u_target, email=f"tgt-{u_target.hex[:6]}@test.com", nombre="Target", google_sub=f"sub-{u_target.hex}")
    owner_session.add_all([tenant, user_adm, user_tgt])
    await owner_session.flush()

    await owner_session.execute(text("SELECT set_config('app.tenant_id', :t, true)"), {"t": str(t_id)})
    owner_session.add(Membership(tenant_id=t_id, user_id=u_admin, rol=MembershipRole.ADMIN))
    owner_session.add(Membership(tenant_id=t_id, user_id=u_target, rol=MembershipRole.VIEWER))
    await owner_session.commit()

    token = sign_session_token(u_admin, user_adm.email)

    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test", cookies={settings.SESSION_COOKIE_NAME: token}) as client:
        response = await client.patch(
            f"/v1/team/{u_target}",
            json={"rol": "owner"},
            headers={"X-Tenant-Id": str(t_id)},
        )

    assert response.status_code == 403
    data = response.json()
    assert data["error"]["code"] == "rol_superior_no_permitido"


@pytest.mark.asyncio
async def test_last_owner_cannot_downgrade_themselves(owner_session: AsyncSession):
    """PRUEBA: El último owner no puede degradarse (400 ultimo_owner)."""
    t_id = uuid.uuid4()
    u_owner = uuid.uuid4()

    tenant = Tenant(id=t_id, nombre="Empresa Solo Owner", slug=f"emp-solo-{t_id.hex[:6]}", plan="free")
    user_own = User(id=u_owner, email=f"own-{u_owner.hex[:6]}@test.com", nombre="Owner Unico", google_sub=f"sub-{u_owner.hex}")
    owner_session.add_all([tenant, user_own])
    await owner_session.flush()

    await owner_session.execute(text("SELECT set_config('app.tenant_id', :t, true)"), {"t": str(t_id)})
    owner_session.add(Membership(tenant_id=t_id, user_id=u_owner, rol=MembershipRole.OWNER))
    await owner_session.commit()

    token = sign_session_token(u_owner, user_own.email)

    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test", cookies={settings.SESSION_COOKIE_NAME: token}) as client:
        response = await client.patch(
            f"/v1/team/{u_owner}",
            json={"rol": "admin"},
            headers={"X-Tenant-Id": str(t_id)},
        )

    assert response.status_code == 400
    data = response.json()
    assert data["error"]["code"] == "ultimo_owner"
    assert "único dueño" in data["error"]["message"].lower()


@pytest.mark.asyncio
async def test_first_login_creates_user_and_default_tenant():
    """PRUEBA: Primer login crea al usuario, su tenant con slug limpio y lo asigna como owner."""
    unique_email = f"juan.perez.{uuid.uuid4().hex[:6]}@empresa.com"
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        response = await client.post(
            "/v1/auth/google",
            json={"id_token": f"mock_first_login:{unique_email}"},
        )

    assert response.status_code == 200
    data = response.json()
    assert data["email"] == unique_email
    assert data["created_tenant_id"] is not None

    # Verificar /v1/me con la cookie devuelta
    session_cookie = response.cookies.get(settings.SESSION_COOKIE_NAME)
    assert session_cookie is not None

    async with AsyncClient(transport=transport, base_url="http://test", cookies={settings.SESSION_COOKIE_NAME: session_cookie}) as client:
        me_resp = await client.get("/v1/me")

    assert me_resp.status_code == 200
    me_data = me_resp.json()
    assert len(me_data["tenants"]) == 1
    assert me_data["tenants"][0]["rol"] == "owner"
    assert me_data["tenants"][0]["slug"].startswith("juan-perez")


@pytest.mark.asyncio
async def test_create_tenant_and_list_tenants():
    """PRUEBA: Crear un nuevo tenant y listarlo con GET /v1/tenants."""
    unique_email = f"creador.{uuid.uuid4().hex[:6]}@test.com"
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        # Login inicial
        login_res = await client.post("/v1/auth/google", json={"id_token": f"mock:{unique_email}"})
        session_cookie = login_res.cookies.get(settings.SESSION_COOKIE_NAME)

    async with AsyncClient(transport=transport, base_url="http://test", cookies={settings.SESSION_COOKIE_NAME: session_cookie}) as auth_client:
        # Crear segundo tenant
        post_res = await auth_client.post(
            "/v1/tenants",
            json={"nombre": "Mi Segunda Empresa S.A."},
        )
        assert post_res.status_code == 201
        created = post_res.json()
        assert created["nombre"] == "Mi Segunda Empresa S.A."
        assert created["rol"] == "owner"

        # Listar tenants
        list_res = await auth_client.get("/v1/tenants")
        assert list_res.status_code == 200
        tenants = list_res.json()
        assert len(tenants) == 2


@pytest.mark.asyncio
async def test_tenant_header_required_when_multiple_tenants():
    """PRUEBA: Si el usuario pertenece a más de un tenant y no manda X-Tenant-Id, recibe 400 tenant_requerido."""
    unique_email = f"multi.{uuid.uuid4().hex[:6]}@test.com"
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        login_res = await client.post("/v1/auth/google", json={"id_token": f"mock:{unique_email}"})
        session_cookie = login_res.cookies.get(settings.SESSION_COOKIE_NAME)

    async with AsyncClient(transport=transport, base_url="http://test", cookies={settings.SESSION_COOKIE_NAME: session_cookie}) as auth_client:
        # Crear segundo tenant
        await auth_client.post(
            "/v1/tenants",
            json={"nombre": "Empresa Dos"},
        )

        # Consulta al equipo sin X-Tenant-Id
        team_res = await auth_client.get("/v1/team")
        assert team_res.status_code == 400
        data = team_res.json()
        assert data["error"]["code"] == "tenant_requerido"
        assert "perteneces a varias empresas" in data["error"]["message"].lower()


@pytest.mark.asyncio
async def test_invite_member_and_list_team():
    """PRUEBA: Owner invita a un nuevo miembro con rol admin y se lista en GET /v1/team."""
    unique_email = f"owner.team.{uuid.uuid4().hex[:6]}@test.com"
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        login_res = await client.post("/v1/auth/google", json={"id_token": f"mock:{unique_email}"})
        session_cookie = login_res.cookies.get(settings.SESSION_COOKIE_NAME)
        tenant_id = login_res.json()["created_tenant_id"]

    async with AsyncClient(transport=transport, base_url="http://test", cookies={settings.SESSION_COOKIE_NAME: session_cookie}) as auth_client:
        # Invitar miembro
        invited_email = f"invitado.{uuid.uuid4().hex[:6]}@test.com"
        inv_res = await auth_client.post(
            "/v1/team/invitations",
            json={"email": invited_email, "nombre": "Invitado Admin", "rol": "admin"},
            headers={"X-Tenant-Id": tenant_id},
        )
        assert inv_res.status_code == 201
        inv_data = inv_res.json()
        assert inv_data["email"] == invited_email
        assert inv_data["rol"] == "admin"

        # Listar equipo
        team_res = await auth_client.get(
            "/v1/team",
            headers={"X-Tenant-Id": tenant_id},
        )
        assert team_res.status_code == 200
        team = team_res.json()
        assert len(team) == 2
        emails = {m["email"] for m in team}
        assert invited_email in emails
