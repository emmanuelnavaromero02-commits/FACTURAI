from contextlib import asynccontextmanager
from typing import Dict, Any
from fastapi import FastAPI, status
from fastapi.responses import JSONResponse

from .config import get_settings
from .db import check_database_health, engine
from .errors import register_error_handlers
from .routers.auth import router as auth_router
from .routers.fiscal import router as fiscal_router
from .routers.handoffs import router as handoffs_router
from .routers.me import router as me_router
from .routers.team import router as team_router
from .routers.tenants import router as tenants_router
from .routers.tickets import router as tickets_router

import pillow_heif

settings = get_settings()

# Registrar compatibilidad con formato HEIC de iPhone en Pillow
pillow_heif.register_heif_opener()


from .seed_merchants import seed_official_merchants
import logging

logger = logging.getLogger(__name__)

@asynccontextmanager
async def lifespan(app: FastAPI):
    """Gestor de ciclo de vida para arranque y cierre limpio de la API."""
    try:
        await seed_official_merchants(settings.DATABASE_MIGRATION_URL)
    except Exception as exc:
        logger.warning("No se pudo sembrar el catálogo de comercios oficiales en arranque: %s", exc)
    yield
    await engine.dispose()


app = FastAPI(
    title="FacturAI API",
    description="SaaS multi-tenant mexicano de facturación automática de tickets CFDI 4.0",
    version="0.1.0",
    lifespan=lifespan,
    docs_url="/docs" if settings.DEBUG else None,
    redoc_url="/redoc" if settings.DEBUG else None,
)

from fastapi.middleware.cors import CORSMiddleware

# 1. Registrar manejadores uniformes de error
register_error_handlers(app)

# 2. Configurar CORS para acceso local y pruebas desde celular en la misma red
app.add_middleware(
    CORSMiddleware,
    allow_origins=[
        "http://localhost:3000",
        "http://127.0.0.1:3000",
    ],
    allow_origin_regex=r"^https?://(localhost|127\.0\.0\.1|192\.168\.\d+\.\d+|10\.\d+\.\d+\.\d+|172\.(1[6-9]|2\d|3[01])\.\d+\.\d+)(:\d+)?$",
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
    expose_headers=["Content-Disposition"],
)

# 3. Registrar routers de endpoints
app.include_router(auth_router)
app.include_router(fiscal_router)
app.include_router(handoffs_router)
app.include_router(me_router)
app.include_router(tenants_router)
app.include_router(team_router)
app.include_router(tickets_router)


@app.get("/v1/health", tags=["Health"])
async def health_check() -> JSONResponse:
    """
    Endpoint de salud del sistema.
    Verifica la conectividad con PostgreSQL usando el rol de aplicación.
    """
    health_status: Dict[str, Any] = {
        "status": "healthy",
        "app": "facturai-api",
        "version": "0.1.0",
        "environment": settings.ENVIRONMENT,
        "services": {},
    }

    status_code = status.HTTP_200_OK

    try:
        db_info = await check_database_health()
        health_status["services"]["database"] = db_info
    except Exception as exc:
        health_status["status"] = "unhealthy"
        health_status["services"]["database"] = {
            "status": "error",
            "detail": str(exc),
        }
        status_code = status.HTTP_503_SERVICE_UNAVAILABLE

    return JSONResponse(status_code=status_code, content=health_status)
