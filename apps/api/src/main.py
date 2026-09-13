from contextlib import asynccontextmanager
from typing import Dict, Any
from fastapi import FastAPI, status
from fastapi.responses import JSONResponse

from .config import get_settings
from .db import check_database_health, engine

settings = get_settings()


@asynccontextmanager
async def lifespan(app: FastAPI):
    """Gestor de ciclo de vida para arranque y cierre limpio de la API."""
    yield
    # Cierre de conexiones
    await engine.dispose()


app = FastAPI(
    title="Facturia API",
    description="SaaS multi-tenant mexicano de facturación automática de tickets CFDI 4.0",
    version="0.1.0",
    lifespan=lifespan,
    docs_url="/docs" if settings.DEBUG else None,
    redoc_url="/redoc" if settings.DEBUG else None,
)


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
