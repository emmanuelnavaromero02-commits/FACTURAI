import uuid
from dataclasses import dataclass
from typing import List, Optional
from fastapi import Depends, Header, Request, Response
from sqlalchemy import select

from .auth import sign_session_token, verify_session_token
from .config import get_settings
from .db import sin_tenant
from .errors import (
    NoAutenticadoException,
    RolInsuficienteException,
    SinPermisoException,
    TenantRequeridoException,
)
from .models import Membership, MembershipRole, Tenant, User

settings = get_settings()

ROLE_HIERARCHY = {
    MembershipRole.OWNER: 4,
    MembershipRole.ADMIN: 3,
    MembershipRole.UPLOADER: 2,
    MembershipRole.VIEWER: 1,
}


@dataclass
class TenantContext:
    tenant_id: uuid.UUID
    tenant: Tenant
    user: User
    role: MembershipRole


async def get_current_user(
    request: Request, response: Response
) -> User:
    """
    Dependencia 'usuario_actual':
    Lee la cookie de sesión firmada (o cabecera Authorization Bearer),
    valida el token y devuelve el usuario de la base de datos.
    Si la sesión está por expirar, renueva la cookie automáticamente.
    """
    token = request.cookies.get(settings.SESSION_COOKIE_NAME)

    # Soporte para cabecera Authorization Bearer en pruebas y clientes API
    if not token:
        auth_header = request.headers.get("Authorization")
        if auth_header and auth_header.startswith("Bearer "):
            token = auth_header.split(" ", 1)[1]

    if not token:
        raise NoAutenticadoException()

    payload, needs_refresh = verify_session_token(token)
    user_id_str = payload.get("sub")
    if not user_id_str:
        raise NoAutenticadoException()

    try:
        user_uuid = uuid.UUID(user_id_str)
    except ValueError:
        raise NoAutenticadoException()

    async with sin_tenant(user_id=user_uuid) as session:
        result = await session.execute(select(User).where(User.id == user_uuid))
        user = result.scalar_one_or_none()

    if user is None:
        raise NoAutenticadoException()

    # Refresco automático de sesión si está próxima a expirar
    if needs_refresh:
        new_token = sign_session_token(user.id, user.email)
        response.set_cookie(
            key=settings.SESSION_COOKIE_NAME,
            value=new_token,
            max_age=settings.SESSION_MAX_AGE_SECONDS,
            httponly=True,
            samesite="lax",
            secure=(settings.ENVIRONMENT == "production"),
        )

    return user


async def get_tenant_context(
    request: Request,
    user: User = Depends(get_current_user),
    x_tenant_id: Optional[str] = Header(None, alias="X-Tenant-Id"),
) -> TenantContext:
    """
    Dependencia 'tenant_actual':
    Determina el tenant a partir de la cabecera 'X-Tenant-Id'.
    - Si no viene y pertenece a uno solo, se fija automáticamente.
    - Si no viene y pertenece a varios, devuelve 400 'tenant_requerido'.
    - Si no es miembro o el tenant no existe, devuelve 403 'sin_permiso'.
      (Nunca revela si el tenant existe o no).
    """
    async with sin_tenant(user_id=user.id) as session:
        # Consulta las membresías del usuario usando la policy 'propias_membresias'
        result = await session.execute(
            select(Membership, Tenant)
            .join(Tenant, Tenant.id == Membership.tenant_id)
            .where(Membership.user_id == user.id)
        )
        memberships_with_tenants = result.all()

    if not memberships_with_tenants:
        raise SinPermisoException()

    # Mapeo de tenant_id -> (Membership, Tenant)
    user_tenants = {str(m.tenant_id): (m, t) for m, t in memberships_with_tenants}

    target_tenant_id_str: str

    tenant_header_or_param = x_tenant_id or request.query_params.get("tenant_id")
    if not tenant_header_or_param:
        if len(user_tenants) == 1:
            target_tenant_id_str = list(user_tenants.keys())[0]
        else:
            raise TenantRequeridoException()
    else:
        target_tenant_id_str = tenant_header_or_param.strip()

    # Validar pertenencia del usuario al tenant
    if target_tenant_id_str not in user_tenants:
        # Misma respuesta 403 exista o no el tenant para no revelar información
        raise SinPermisoException()

    membership, tenant = user_tenants[target_tenant_id_str]

    return TenantContext(
        tenant_id=tenant.id,
        tenant=tenant,
        user=user,
        role=membership.rol,
    )


def require_role(*allowed_roles: MembershipRole):
    """
    Fábrica de dependencias 'requiere_rol':
    Garantiza que el usuario tenga uno de los roles permitidos en el tenant actual.
    """
    async def role_checker(ctx: TenantContext = Depends(get_tenant_context)) -> TenantContext:
        if ctx.role not in allowed_roles:
            raise RolInsuficienteException(
                f"Tu rol '{ctx.role.value}' no tiene permiso para realizar esta acción. "
                f"Se requiere uno de: {[r.value for r in allowed_roles]}."
            )
        return ctx

    return role_checker
