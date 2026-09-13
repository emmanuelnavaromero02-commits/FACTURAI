from typing import Optional
from fastapi import APIRouter, Depends, Response, status
from pydantic import BaseModel

from ..auth import (
    authenticate_or_register_user,
    get_google_verifier,
    GoogleTokenVerifier,
    sign_session_token,
)
from ..config import get_settings

router = APIRouter(prefix="/v1/auth", tags=["Auth"])
settings = get_settings()


class GoogleAuthRequest(BaseModel):
    id_token: str


class GoogleAuthResponse(BaseModel):
    user_id: str
    email: str
    nombre: str
    avatar_url: Optional[str] = None
    created_tenant_id: Optional[str] = None


@router.post("/google", response_model=GoogleAuthResponse)
async def login_with_google(
    payload: GoogleAuthRequest,
    response: Response,
    verifier: GoogleTokenVerifier = Depends(get_google_verifier),
):
    """
    Inicia sesión o registra al usuario con un id_token emitido por Google OAuth.
    Valida firma, aud, iss, exp y email_verified.
    Fija la cookie de sesión firmada 'facturia_session'.
    """
    token_payload = await verifier.verify(payload.id_token)
    user, created_tenant = await authenticate_or_register_user(token_payload)

    # Crear token de sesión firmado
    session_token = sign_session_token(user.id, user.email)

    # Fijar cookie HttpOnly, SameSite=Lax, Secure en producción
    response.set_cookie(
        key=settings.SESSION_COOKIE_NAME,
        value=session_token,
        max_age=settings.SESSION_MAX_AGE_SECONDS,
        httponly=True,
        samesite="lax",
        secure=(settings.ENVIRONMENT == "production"),
        path="/",
    )

    return GoogleAuthResponse(
        user_id=str(user.id),
        email=user.email,
        nombre=user.nombre,
        avatar_url=user.avatar_url,
        created_tenant_id=str(created_tenant.id) if created_tenant else None,
    )


@router.post("/logout", status_code=status.HTTP_200_OK)
async def logout(response: Response):
    """Cierra la sesión y borra la cookie HttpOnly."""
    response.delete_cookie(
        key=settings.SESSION_COOKIE_NAME,
        httponly=True,
        samesite="lax",
        secure=(settings.ENVIRONMENT == "production"),
        path="/",
    )
    return {"ok": True}
