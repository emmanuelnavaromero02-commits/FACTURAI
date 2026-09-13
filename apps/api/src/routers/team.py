import uuid
from typing import List, Optional
from fastapi import APIRouter, Depends, Path, status
from pydantic import BaseModel, EmailStr
from sqlalchemy import func, select

from ..db import sin_tenant, tenant_session
from ..deps import (
    ROLE_HIERARCHY,
    TenantContext,
    get_tenant_context,
    require_role,
)
from ..errors import (
    RecursoNoEncontradoException,
    RolSuperiorNoPermitidoException,
    SinPermisoException,
    UltimoOwnerException,
)
from ..models import Membership, MembershipRole, User

router = APIRouter(prefix="/v1/team", tags=["Team"])


class TeamMemberResponse(BaseModel):
    user_id: str
    nombre: str
    email: str
    rol: str
    avatar_url: Optional[str] = None


class InviteMemberRequest(BaseModel):
    email: EmailStr
    nombre: Optional[str] = None
    rol: MembershipRole


class UpdateMemberRoleRequest(BaseModel):
    rol: MembershipRole


@router.get("", response_model=List[TeamMemberResponse])
async def list_team(ctx: TenantContext = Depends(get_tenant_context)):
    """
    Lista a todos los miembros del equipo de la empresa activa.
    La consulta se ejecuta estrictamente dentro de tenant_session() para cumplir con RLS.
    """
    async with tenant_session(ctx.tenant_id, user_id=ctx.user.id) as session:
        result = await session.execute(
            select(Membership, User)
            .join(User, User.id == Membership.user_id)
            .where(Membership.tenant_id == ctx.tenant_id)
        )
        members = result.all()

    return [
        TeamMemberResponse(
            user_id=str(user.id),
            nombre=user.nombre,
            email=user.email,
            rol=membership.rol.value,
            avatar_url=user.avatar_url,
        )
        for membership, user in members
    ]


@router.post("/invitations", response_model=TeamMemberResponse, status_code=status.HTTP_201_CREATED)
async def invite_team_member(
    body: InviteMemberRequest,
    ctx: TenantContext = Depends(require_role(MembershipRole.OWNER, MembershipRole.ADMIN)),
):
    """
    Invita o agrega a un miembro al equipo.
    Regla: Nadie puede asignar un rol superior al suyo.
    """
    # Regla: Nadie puede asignar un rol superior al suyo
    if ROLE_HIERARCHY[body.rol] > ROLE_HIERARCHY[ctx.role]:
        raise RolSuperiorNoPermitidoException(
            f"Como '{ctx.role.value}' no puedes asignar el rol '{body.rol.value}'."
        )

    # 1. Encontrar o crear usuario
    async with sin_tenant() as global_session:
        res = await global_session.execute(select(User).where(User.email == body.email))
        target_user = res.scalar_one_or_none()

        if target_user is None:
            target_user = User(
                id=uuid.uuid4(),
                email=body.email,
                nombre=body.nombre or body.email.split("@")[0],
                google_sub=f"invited-{uuid.uuid4().hex}",
            )
            global_session.add(target_user)
            await global_session.commit()

    # 2. Agregar o actualizar membresía dentro de tenant_session
    async with tenant_session(ctx.tenant_id, user_id=ctx.user.id) as session:
        res_mem = await session.execute(
            select(Membership).where(
                Membership.tenant_id == ctx.tenant_id,
                Membership.user_id == target_user.id,
            )
        )
        membership = res_mem.scalar_one_or_none()

        if membership is None:
            membership = Membership(
                tenant_id=ctx.tenant_id,
                user_id=target_user.id,
                rol=body.rol,
            )
            session.add(membership)
        else:
            membership.rol = body.rol

        await session.commit()

    return TeamMemberResponse(
        user_id=str(target_user.id),
        nombre=target_user.nombre,
        email=target_user.email,
        rol=membership.rol.value,
        avatar_url=target_user.avatar_url,
    )


@router.patch("/{user_id}", response_model=TeamMemberResponse)
async def update_member_role(
    body: UpdateMemberRoleRequest,
    user_id: uuid.UUID = Path(...),
    ctx: TenantContext = Depends(require_role(MembershipRole.OWNER, MembershipRole.ADMIN)),
):
    """
    Actualiza el rol de un miembro del equipo.
    Reglas:
    1. Nadie puede asignar un rol superior al suyo.
    2. Nadie puede modificar a un usuario con un rol superior al suyo.
    3. Un owner no puede degradarse a sí mismo si es el único owner.
    """
    # Regla 1: Nadie puede asignar un rol superior al suyo
    if ROLE_HIERARCHY[body.rol] > ROLE_HIERARCHY[ctx.role]:
        raise RolSuperiorNoPermitidoException(
            f"Como '{ctx.role.value}' no puedes asignar el rol '{body.rol.value}'."
        )

    async with tenant_session(ctx.tenant_id, user_id=ctx.user.id) as session:
        res = await session.execute(
            select(Membership, User)
            .join(User, User.id == Membership.user_id)
            .where(
                Membership.tenant_id == ctx.tenant_id,
                Membership.user_id == user_id,
            )
        )
        row = res.first()
        if not row:
            raise RecursoNoEncontradoException("El miembro especificado no pertenece a este equipo.")

        membership, target_user = row

        # Regla 2: No modificar a alguien con rol superior al del solicitante
        if ROLE_HIERARCHY[membership.rol] > ROLE_HIERARCHY[ctx.role]:
            raise SinPermisoException("No puedes modificar el rol de un miembro con jerarquía superior a la tuya.")

        # Regla 3: El último owner no puede degradarse
        if membership.rol == MembershipRole.OWNER and body.rol != MembershipRole.OWNER:
            owner_count = await session.scalar(
                select(func.count())
                .select_from(Membership)
                .where(
                    Membership.tenant_id == ctx.tenant_id,
                    Membership.rol == MembershipRole.OWNER,
                )
            )
            if owner_count is not None and owner_count <= 1:
                raise UltimoOwnerException()

        membership.rol = body.rol
        await session.commit()

    return TeamMemberResponse(
        user_id=str(target_user.id),
        nombre=target_user.nombre,
        email=target_user.email,
        rol=membership.rol.value,
        avatar_url=target_user.avatar_url,
    )
