from contextlib import asynccontextmanager
from typing import AsyncGenerator, Dict, Any, Optional
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

import os
import sys
from sqlalchemy.pool import NullPool

is_testing = (
    "pytest" in sys.modules
    or os.getenv("PYTEST_CURRENT_TEST") is not None
    or settings.ENVIRONMENT in ("test", "testing")
)

if is_testing:
    engine: AsyncEngine = create_async_engine(
        settings.DATABASE_URL,
        echo=False,
        poolclass=NullPool,
    )
else:
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
async def tenant_session(
    tenant_id: UUID, user_id: Optional[UUID] = None
) -> AsyncGenerator[AsyncSession, None]:
    """
    Context manager para operaciones con aislamiento RLS (Reglas 1 y 2).
    Abre una transacción y fija 'app.tenant_id' y opcionalmente 'app.user_id'
    estrictamente a nivel local de transacción (is_local = true).
    """
    async with AsyncSessionLocal() as session:
        async with session.begin():
            await session.execute(
                text("SELECT set_config('app.tenant_id', :valor, true)"),
                {"valor": str(tenant_id)},
            )
            if user_id is not None:
                await session.execute(
                    text("SELECT set_config('app.user_id', :u, true)"),
                    {"u": str(user_id)},
                )
            else:
                await session.execute(text("SELECT set_config('app.user_id', '', true)"))
            yield session


@asynccontextmanager
async def sin_tenant(
    user_id: Optional[UUID] = None
) -> AsyncGenerator[AsyncSession, None]:
    """
    Context manager para consultas de tablas globales o descubrimiento de membresías.
    Fija 'app.tenant_id' como cadena vacía a nivel local de transacción.
    Si se especifica user_id, fija 'app.user_id', permitiendo consultar 'memberships'
    mediante la policy 'propias_membresias' sin fijar tenant_id.
    """
    async with AsyncSessionLocal() as session:
        async with session.begin():
            await session.execute(text("SELECT set_config('app.tenant_id', '', true)"))
            if user_id is not None:
                await session.execute(
                    text("SELECT set_config('app.user_id', :u, true)"),
                    {"u": str(user_id)},
                )
            else:
                await session.execute(text("SELECT set_config('app.user_id', '', true)"))
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
