import re
import uuid
from typing import List, Optional
from fastapi import APIRouter, Depends, status
from pydantic import BaseModel, Field
from sqlalchemy import select

from ..db import sin_tenant
from ..deps import get_current_user
from ..errors import AppException
from ..models import Membership, MembershipRole, Tenant, User

router = APIRouter(prefix="/v1/tenants", tags=["Tenants"])


class TenantResponse(BaseModel):
    id: str
    nombre: str
    slug: str
    plan: str
    rol: str


class CreateTenantRequest(BaseModel):
    nombre: str = Field(..., min_length=2, max_length=255)
    slug: Optional[str] = Field(None, min_length=2, max_length=100)


@router.get("", response_model=List[TenantResponse])
async def list_user_tenants(current_user: User = Depends(get_current_user)):
    """Lista todos los tenants a los que pertenece el usuario."""
    async with sin_tenant(user_id=current_user.id) as session:
        result = await session.execute(
            select(Membership, Tenant)
            .join(Tenant, Tenant.id == Membership.tenant_id)
            .where(Membership.user_id == current_user.id)
        )
        items = result.all()

    return [
        TenantResponse(
            id=str(tenant.id),
            nombre=tenant.nombre,
            slug=tenant.slug,
            plan=tenant.plan,
            rol=membership.rol.value,
        )
        for membership, tenant in items
    ]


@router.post("", response_model=TenantResponse, status_code=status.HTTP_201_CREATED)
async def create_tenant(
    body: CreateTenantRequest, current_user: User = Depends(get_current_user)
):
    """
    Crea un nuevo tenant y asigna al usuario actual como 'owner'.
    """
    # Generar o limpiar slug
    if body.slug:
        slug = re.sub(r"[^a-z0-9-]", "", body.slug.lower().strip())
    else:
        slug = re.sub(r"[^a-z0-9-]", "", body.nombre.lower().strip().replace(" ", "-"))
        slug = re.sub(r"-+", "-", slug).strip("-")

    if not slug:
        slug = f"empresa-{uuid.uuid4().hex[:6]}"

    async with sin_tenant(user_id=current_user.id) as session:
        # Verificar unicidad del slug
        existing = await session.execute(select(Tenant).where(Tenant.slug == slug))
        if existing.scalar_one_or_none() is not None:
            slug = f"{slug}-{uuid.uuid4().hex[:4]}"

        new_tenant = Tenant(
            id=uuid.uuid4(),
            nombre=body.nombre.strip(),
            slug=slug,
            plan="free",
        )
        session.add(new_tenant)
        await session.flush()

        # Asignar membresía owner fijando app.tenant_id para cumplir RLS
        from sqlalchemy import text
        await session.execute(
            text("SELECT set_config('app.tenant_id', :t, true)"),
            {"t": str(new_tenant.id)},
        )
        membership = Membership(
            tenant_id=new_tenant.id,
            user_id=current_user.id,
            rol=MembershipRole.OWNER,
        )
        session.add(membership)
        await session.commit()

    return TenantResponse(
        id=str(new_tenant.id),
        nombre=new_tenant.nombre,
        slug=new_tenant.slug,
        plan=new_tenant.plan,
        rol=MembershipRole.OWNER.value,
    )
