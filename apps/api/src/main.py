from contextlib import asynccontextmanager
from typing import Dict, Any
from fastapi import FastAPI, status
from fastapi.responses import JSONResponse

from .config import get_settings
from .db import check_database_health, engine
from .errors import register_error_handlers
from .routers.auth import router as auth_router
from .routers.me import router as me_router
from .routers.tenants import router as tenants_router
from .routers.team import router as team_router
from .routers.tickets import router as tickets_router

settings = get_settings()


@asynccontextmanager
async def lifespan(app: FastAPI):
    """Gestor de ciclo de vida para arranque y cierre limpio de la API."""
    yield
    await engine.dispose()


app = FastAPI(
    title="Facturia API",
    description="SaaS multi-tenant mexicano de facturación automática de tickets CFDI 4.0",
    version="0.1.0",
    lifespan=lifespan,
    docs_url="/docs" if settings.DEBUG else None,
    redoc_url="/redoc" if settings.DEBUG else None,
)

# 1. Registrar manejadores uniformes de error
register_error_handlers(app)

# 2. Registrar routers de endpoints
app.include_router(auth_router)
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
        "app": "facturia-api",
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
