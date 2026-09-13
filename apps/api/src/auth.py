import abc
import re
import time
import uuid
from typing import Optional, Dict, Any, Tuple
import httpx
import jwt
from pydantic import BaseModel, EmailStr
from sqlalchemy import select

from .config import get_settings
from .db import sin_tenant
from .errors import (
    EmailNoVerificadoException,
    TokenInvalidoException,
)
from .models import Tenant, User, Membership, MembershipRole
from .security import get_master_key

settings = get_settings()

GOOGLE_ISSUERS = ["https://accounts.google.com", "accounts.google.com"]
GOOGLE_CERTS_URL = "https://www.googleapis.com/oauth2/v3/certs"


class GoogleTokenPayload(BaseModel):
    sub: str
    email: EmailStr
    name: str
    picture: Optional[str] = None
    email_verified: bool = False
    aud: str
    iss: str
    exp: int
    nonce: Optional[str] = None


class GoogleTokenVerifier(abc.ABC):
    """Interfaz abstracta para la verificación de tokens de identidad de Google."""

    @abc.abstractmethod
    async def verify(self, id_token: str) -> GoogleTokenPayload:
        """Verifica el token y retorna el payload validado o lanza una excepción."""
        pass


class ProductionGoogleTokenVerifier(GoogleTokenVerifier):
    """
    Verificador real de Google OAuth: descarga llaves públicas JWKS,
    valida firma criptográfica RS256, audiencia (aud), emisor (iss) y vigencia (exp).
    """

    def __init__(self):
        self._cached_keys: Dict[str, Any] = {}
        self._cache_expires_at: float = 0

    async def _get_jwks(self) -> Dict[str, Any]:
        now = time.time()
        if self._cached_keys and now < self._cache_expires_at:
            return self._cached_keys

        async with httpx.AsyncClient(timeout=10.0) as client:
            resp = await client.get(GOOGLE_CERTS_URL)
            if resp.status_code != 200:
                raise TokenInvalidoException(
                    message="No se pudieron obtener las llaves públicas de Google para verificar el acceso."
                )
            data = resp.json()
            self._cached_keys = data
            # Cache por 1 hora
            self._cache_expires_at = now + 3600
            return self._cached_keys

    async def verify(self, id_token: str) -> GoogleTokenPayload:
        if not id_token:
            raise TokenInvalidoException("No se proporcionó ningún token de autenticación.")

        try:
            jwks_client = jwt.PyJWKClient(GOOGLE_CERTS_URL)
            signing_key = jwks_client.get_signing_key_from_jwt(id_token)

            data = jwt.decode(
                id_token,
                signing_key.key,
                algorithms=["RS256"],
                audience=settings.GOOGLE_CLIENT_ID,
                issuer=GOOGLE_ISSUERS,
                options={"require": ["exp", "iss", "aud", "sub"]},
            )
        except jwt.ExpiredSignatureError:
            raise TokenInvalidoException("Tu sesión de Google ha expirado. Por favor inicia sesión de nuevo.")
        except jwt.InvalidAudienceError:
            raise TokenInvalidoException("El token no corresponde a esta aplicación (audiencia no válida).")
        except jwt.InvalidIssuerError:
            raise TokenInvalidoException("El emisor del token no es válido.")
        except Exception as exc:
            raise TokenInvalidoException(f"Token de Google inválido: {str(exc)}")

        if not data.get("email_verified", False):
            raise EmailNoVerificadoException()

        return GoogleTokenPayload(
            sub=data["sub"],
            email=data["email"],
            name=data.get("name", data["email"].split("@")[0]),
            picture=data.get("picture"),
            email_verified=data["email_verified"],
            aud=data["aud"],
            iss=data["iss"],
            exp=data["exp"],
            nonce=data.get("nonce"),
        )


class FakeGoogleTokenVerifier(GoogleTokenVerifier):
    """Verificador simulado en memoria para pruebas unitarias e integración (no llama a Google)."""

    def __init__(self):
        self._tokens: Dict[str, GoogleTokenPayload] = {}

    def register_token(self, token_str: str, payload: GoogleTokenPayload):
        self._tokens[token_str] = payload

    async def verify(self, id_token: str) -> GoogleTokenPayload:
        if id_token in self._tokens:
            payload = self._tokens[id_token]
        else:
            # Token con formato mock estándar para facilitar pruebas
            if id_token.startswith("mock_invalid_aud:"):
                aud = "otro-client-id.apps.googleusercontent.com"
            else:
                aud = settings.GOOGLE_CLIENT_ID

            email_verified = not id_token.startswith("mock_unverified:")

            # Extraer email si viene en el token: mock_token:juan@empresa.com
            parts = id_token.split(":")
            email = parts[1] if len(parts) > 1 and "@" in parts[1] else "test.user@facturia.mx"

            payload = GoogleTokenPayload(
                sub=f"google-sub-{email}",
                email=email,
                name="Usuario Simulado",
                picture="https://lh3.googleusercontent.com/a/default",
                email_verified=email_verified,
                aud=aud,
                iss="https://accounts.google.com",
                exp=int(time.time()) + 3600,
            )

        if payload.aud != settings.GOOGLE_CLIENT_ID:
            raise TokenInvalidoException("El token no corresponde a esta aplicación (audiencia no válida).")

        if payload.iss not in GOOGLE_ISSUERS:
            raise TokenInvalidoException("El emisor del token no es válido.")

        if payload.exp < time.time():
            raise TokenInvalidoException("Tu sesión de Google ha expirado. Por favor inicia sesión de nuevo.")

        if not payload.email_verified:
            raise EmailNoVerificadoException()

        return payload


# Instancia singleton del verificador (configurable en tests)
_verifier: GoogleTokenVerifier = FakeGoogleTokenVerifier() if settings.ENVIRONMENT == "test" else ProductionGoogleTokenVerifier()


def get_google_verifier() -> GoogleTokenVerifier:
    return _verifier


def set_google_verifier(verifier: GoogleTokenVerifier) -> None:
    global _verifier
    _verifier = verifier


# ---------------------------------------------------------------------------
# Gestión de Sesiones mediante Cookie Firmada
# ---------------------------------------------------------------------------
def sign_session_token(user_id: uuid.UUID, email: str) -> str:
    """Crea un token de sesión firmado con HMAC-SHA256 usando la clave maestra."""
    now = int(time.time())
    payload = {
        "sub": str(user_id),
        "email": email,
        "iat": now,
        "exp": now + settings.SESSION_MAX_AGE_SECONDS,
    }
    return jwt.encode(payload, get_master_key(), algorithm="HS256")


def verify_session_token(token: str) -> Tuple[Dict[str, Any], bool]:
    """
    Verifica la firma y expiración del token de sesión.
    Retorna: (payload, needs_refresh)
    """
    try:
        payload = jwt.decode(
            token,
            get_master_key(),
            algorithms=["HS256"],
            options={"require": ["sub", "exp", "iat"]},
        )
    except jwt.ExpiredSignatureError:
        raise TokenInvalidoException("Tu sesión ha expirado. Inicia sesión de nuevo.")
    except Exception:
        raise TokenInvalidoException("La sesión no es válida. Inicia sesión de nuevo.")

    remaining = payload["exp"] - int(time.time())
    needs_refresh = remaining < settings.SESSION_REFRESH_THRESHOLD_SECONDS
    return payload, needs_refresh


# ---------------------------------------------------------------------------
# Lógica de Primer Login y Aprovisionamiento
# ---------------------------------------------------------------------------
def generate_slug_from_email(email: str) -> str:
    """Deriva un slug limpio a partir del correo electrónico."""
    local_part = email.split("@")[0].lower()
    cleaned = re.sub(r"[^a-z0-9]", "-", local_part)
    cleaned = re.sub(r"-+", "-", cleaned).strip("-")
    return cleaned or "tenant"


async def authenticate_or_register_user(
    payload: GoogleTokenPayload,
) -> Tuple[User, Optional[Tenant]]:
    """
    Autentica al usuario en el sistema:
    - Si el usuario no existe, lo crea con sus datos de Google.
    - Si no tiene membresías en ningún tenant, crea su tenant por defecto y lo asigna como owner.
    Usa el contexto global sin_tenant().
    """
    async with sin_tenant() as session:
        # 1. Buscar usuario por google_sub o email
        result = await session.execute(
            select(User).where((User.google_sub == payload.sub) | (User.email == payload.email))
        )
        user = result.scalar_one_or_none()

        if user is None:
            user = User(
                id=uuid.uuid4(),
                email=payload.email,
                nombre=payload.name,
                avatar_url=payload.picture,
                google_sub=payload.sub,
            )
            session.add(user)
            await session.flush()
        else:
            # Actualizar perfil si hubo cambios
            user.nombre = payload.name
            if payload.picture:
                user.avatar_url = payload.picture
            if not user.google_sub:
                user.google_sub = payload.sub
            await session.flush()

        # 2. Consultar si ya tiene membresías
        # Pasamos app.user_id = user.id para habilitar la policy 'propias_membresias'
        await session.execute(
            select(Membership).where(Membership.user_id == user.id)
        )
        # Nota: dentro de sin_tenant() fijamos app.user_id:
        from sqlalchemy import text
        await session.execute(text("SELECT set_config('app.user_id', :u, true)"), {"u": str(user.id)})
        res_mem = await session.execute(select(Membership).where(Membership.user_id == user.id))
        memberships = res_mem.scalars().all()

        created_tenant = None
        if len(memberships) == 0:
            # Primer login: crear tenant propio por defecto
            base_slug = generate_slug_from_email(payload.email)
            slug = base_slug

            # Garantizar unicidad del slug
            existing = await session.execute(select(Tenant).where(Tenant.slug == slug))
            counter = 1
            while existing.scalar_one_or_none() is not None:
                slug = f"{base_slug}-{counter}"
                existing = await session.execute(select(Tenant).where(Tenant.slug == slug))
                counter += 1

            created_tenant = Tenant(
                id=uuid.uuid4(),
                nombre=f"Empresa de {user.nombre.split()[0]}",
                slug=slug,
                plan="free",
            )
            session.add(created_tenant)
            await session.flush()

            # Crear membresía owner fijando app.tenant_id para satisfacer RLS
            await session.execute(
                text("SELECT set_config('app.tenant_id', :t, true)"),
                {"t": str(created_tenant.id)},
            )
            membership = Membership(
                tenant_id=created_tenant.id,
                user_id=user.id,
                rol=MembershipRole.OWNER,
            )
            session.add(membership)
            await session.flush()

        await session.commit()
        return user, created_tenant
