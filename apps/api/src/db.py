from contextlib import asynccontextmanager
from typing import AsyncGenerator, Dict, Any
from uuid import UUID
from sqlalchemy import text
from sqlalchemy.ext.asyncio import (
    AsyncEngine,
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)

from .config import get_settings

settings = get_settings()

engine: AsyncEngine = create_async_engine(
    settings.DATABASE_URL,
    echo=settings.DEBUG,
    pool_pre_ping=True,
    pool_size=10,
    max_overflow=20,
)

AsyncSessionLocal = async_sessionmaker(
    bind=engine,
    class_=AsyncSession,
    expire_on_commit=False,
    autoflush=False,
)


@asynccontextmanager
async def tenant_session(tenant_id: UUID) -> AsyncGenerator[AsyncSession, None]:
    """
    Context manager para operaciones con aislamiento RLS (Reglas 1 y 2).
    Abre una transacción y fija 'app.tenant_id' estrictamente a nivel local de transacción.
    El tercer argumento en 'true' garantiza que la variable se limpie al terminar la transacción
    y nunca se contamine la conexión devuelta al pool.
    """
    async with AsyncSessionLocal() as session:
        async with session.begin():
            await session.execute(
                text("SELECT set_config('app.tenant_id', :valor, true)"),
                {"valor": str(tenant_id)},
            )
            yield session


@asynccontextmanager
async def sin_tenant() -> AsyncGenerator[AsyncSession, None]:
    """
    Context manager para consultas de tablas globales (tenants, users, merchants).
    Fija 'app.tenant_id' como cadena vacía a nivel local de transacción.
    Si por error una consulta accede a tablas con RLS bajo este contexto,
    Postgres devolverá cero filas sin lanzar error (gracias al nullif).
    """
    async with AsyncSessionLocal() as session:
        async with session.begin():
            await session.execute(text("SELECT set_config('app.tenant_id', '', true)"))
            yield session


async def get_db() -> AsyncGenerator[AsyncSession, None]:
    """Generador de sesiones asíncronas para inyección de dependencias en FastAPI."""
    async with AsyncSessionLocal() as session:
        try:
            yield session
        finally:
            await session.close()


async def check_database_health() -> Dict[str, Any]:
    """Verifica la conectividad activa contra PostgreSQL y reporta el rol actual."""
    async with engine.connect() as conn:
        result = await conn.execute(
            text("SELECT current_user, current_database(), version()")
        )
        row = result.fetchone()
        if row:
            return {
                "status": "connected",
                "connected_user": row[0],
                "database": row[1],
            }
        return {"status": "connected"}
