from typing import List, Optional
from fastapi import APIRouter, Depends
from pydantic import BaseModel
from sqlalchemy import select

from ..db import sin_tenant
from ..deps import get_current_user
from ..models import Membership, Tenant, User

router = APIRouter(prefix="/v1", tags=["User"])


class UserTenantResponse(BaseModel):
    id: str
    nombre: str
    slug: str
    plan: str
    rol: str


class MeResponse(BaseModel):
    id: str
    email: str
    nombre: str
    avatar_url: Optional[str] = None
    tenants: List[UserTenantResponse]


@router.get("/me", response_model=MeResponse)
async def get_me(current_user: User = Depends(get_current_user)):
    """
    Devuelve la información del usuario actual, sus empresas asociadas
    y su rol dentro de cada una.
    """
    async with sin_tenant(user_id=current_user.id) as session:
        result = await session.execute(
            select(Membership, Tenant)
            .join(Tenant, Tenant.id == Membership.tenant_id)
            .where(Membership.user_id == current_user.id)
        )
        memberships_with_tenants = result.all()

    tenants_list = [
        UserTenantResponse(
            id=str(tenant.id),
            nombre=tenant.nombre,
            slug=tenant.slug,
            plan=tenant.plan,
            rol=membership.rol.value,
        )
        for membership, tenant in memberships_with_tenants
    ]

    return MeResponse(
        id=str(current_user.id),
        email=current_user.email,
        nombre=current_user.nombre,
        avatar_url=current_user.avatar_url,
        tenants=tenants_list,
    )
