import re
import uuid
from typing import Optional
from fastapi import APIRouter, Depends, status
from pydantic import BaseModel, Field, field_validator
from sqlalchemy import select

from ..db import tenant_session
from ..deps import TenantContext, get_tenant_context
from ..errors import AppException
from ..models import FiscalProfile, Ticket, TicketEstado
from ..services.queue import enqueue_ticket_facturacion
from ..state_machine import transition

router = APIRouter(prefix="/v1/fiscal-profile", tags=["Fiscal Profile"])


class FiscalProfileSchema(BaseModel):
    id: Optional[uuid.UUID] = None
    rfc: str = Field(..., min_length=12, max_length=13)
    razon_social: str = Field(..., min_length=2, max_length=255)
    cp: str = Field(..., min_length=5, max_length=5)
    regimen_fiscal: str = Field(..., min_length=3, max_length=10)
    uso_cfdi: str = Field(default="G03", min_length=3, max_length=10)
    email_receptor: str = Field(..., min_length=5, max_length=255)
    telefono: Optional[str] = None
    calle: Optional[str] = None
    numero_exterior: Optional[str] = None
    numero_interior: Optional[str] = None
    colonia: Optional[str] = None
    municipio_alcaldia: Optional[str] = None
    estado: Optional[str] = None
    pais: str = Field(default="MEX")
    curp: Optional[str] = None
    es_principal: bool = True

    @field_validator("rfc")
    @classmethod
    def validate_rfc(cls, v: str) -> str:
        clean = v.strip().upper()
        if not re.match(r"^[A-ZÑ&]{3,4}[0-9]{6}[A-Z0-9]{3}$", clean):
            raise ValueError("El RFC no tiene un formato válido ante el SAT (12 o 13 caracteres alfanuméricos).")
        return clean

    @field_validator("cp")
    @classmethod
    def validate_cp(cls, v: str) -> str:
        clean = v.strip()
        if not (len(clean) == 5 and clean.isdigit()):
            raise ValueError("El código postal debe constar de 5 dígitos numéricos.")
        return clean

    @field_validator("razon_social")
    @classmethod
    def clean_razon_social(cls, v: str) -> str:
        return v.strip().upper()

    @field_validator("email_receptor")
    @classmethod
    def clean_email(cls, v: str) -> str:
        return v.strip().lower()


@router.get("", response_model=Optional[FiscalProfileSchema])
async def get_fiscal_profile(ctx: TenantContext = Depends(get_tenant_context)):
    """Obtiene el perfil fiscal principal del tenant activo."""
    async with tenant_session(ctx.tenant_id, user_id=ctx.user.id) as session:
        result = await session.execute(
            select(FiscalProfile).where(
                FiscalProfile.tenant_id == ctx.tenant_id,
                FiscalProfile.es_principal == True,
            )
        )
        profile = result.scalar_one_or_none()
        if not profile:
            return None
        return FiscalProfileSchema(
            id=profile.id,
            rfc=profile.rfc,
            razon_social=profile.razon_social,
            cp=profile.cp,
            regimen_fiscal=profile.regimen_fiscal,
            uso_cfdi=profile.uso_cfdi,
            email_receptor=profile.email_receptor,
            telefono=profile.telefono,
            calle=profile.calle,
            numero_exterior=profile.numero_exterior,
            numero_interior=profile.numero_interior,
            colonia=profile.colonia,
            municipio_alcaldia=profile.municipio_alcaldia,
            estado=profile.estado,
            pais=profile.pais,
            curp=profile.curp,
            es_principal=profile.es_principal,
        )


@router.put("", response_model=FiscalProfileSchema)
async def upsert_fiscal_profile(
    body: FiscalProfileSchema, ctx: TenantContext = Depends(get_tenant_context)
):
    """
    Crea o actualiza el perfil fiscal principal del tenant activo bajo RLS.
    Si existen tickets en estado 'extraido' listos para facturación, los encola automáticamente.
    """
    tickets_a_encolar = []

    async with tenant_session(ctx.tenant_id, user_id=ctx.user.id) as session:
        result = await session.execute(
            select(FiscalProfile).where(
                FiscalProfile.tenant_id == ctx.tenant_id,
                FiscalProfile.es_principal == True,
            )
        )
        profile = result.scalar_one_or_none()

        if profile is None:
            # Buscar si existe por RFC
            result_rfc = await session.execute(
                select(FiscalProfile).where(
                    FiscalProfile.tenant_id == ctx.tenant_id,
                    FiscalProfile.rfc == body.rfc,
                )
            )
            profile = result_rfc.scalar_one_or_none()

        if profile is None:
            profile = FiscalProfile(
                id=uuid.uuid4(),
                tenant_id=ctx.tenant_id,
                rfc=body.rfc,
                razon_social=body.razon_social,
                cp=body.cp,
                regimen_fiscal=body.regimen_fiscal,
                uso_cfdi=body.uso_cfdi,
                email_receptor=body.email_receptor,
                telefono=body.telefono,
                calle=body.calle,
                numero_exterior=body.numero_exterior,
                numero_interior=body.numero_interior,
                colonia=body.colonia,
                municipio_alcaldia=body.municipio_alcaldia,
                estado=body.estado,
                pais=body.pais or "MEX",
                curp=body.curp,
                es_principal=True,
            )
            session.add(profile)
        else:
            profile.rfc = body.rfc
            profile.razon_social = body.razon_social
            profile.cp = body.cp
            profile.regimen_fiscal = body.regimen_fiscal
            profile.uso_cfdi = body.uso_cfdi
            profile.email_receptor = body.email_receptor
            profile.telefono = body.telefono
            profile.calle = body.calle
            profile.numero_exterior = body.numero_exterior
            profile.numero_interior = body.numero_interior
            profile.colonia = body.colonia
            profile.municipio_alcaldia = body.municipio_alcaldia
            profile.estado = body.estado
            profile.pais = body.pais or "MEX"
            profile.curp = body.curp
            profile.es_principal = True

        await session.flush()

        # Auto-encolar tickets en 'extraido' que tengan URL o comercio
        extraidos_res = await session.execute(
            select(Ticket).where(
                Ticket.tenant_id == ctx.tenant_id,
                Ticket.estado == TicketEstado.EXTRAIDO,
                (Ticket.url_facturacion.isnot(None)) | (Ticket.merchant_id.isnot(None)),
            )
        )
        for t in extraidos_res.scalars().all():
            await transition(
                session,
                t,
                TicketEstado.ENCOLADO,
                "Perfil fiscal configurado. Ticket encolado automáticamente para facturación.",
                tipo="encolado",
            )
            tickets_a_encolar.append(t.id)

        response_profile = FiscalProfileSchema(
            id=profile.id,
            rfc=profile.rfc,
            razon_social=profile.razon_social,
            cp=profile.cp,
            regimen_fiscal=profile.regimen_fiscal,
            uso_cfdi=profile.uso_cfdi,
            email_receptor=profile.email_receptor,
            telefono=profile.telefono,
            calle=profile.calle,
            numero_exterior=profile.numero_exterior,
            numero_interior=profile.numero_interior,
            colonia=profile.colonia,
            municipio_alcaldia=profile.municipio_alcaldia,
            estado=profile.estado,
            pais=profile.pais,
            curp=profile.curp,
            es_principal=profile.es_principal,
        )

    # Fuera de la transacción de DB, encolar en Redis ARQ
    for tid in tickets_a_encolar:
        await enqueue_ticket_facturacion(ctx.tenant_id, tid)

    return response_profile
