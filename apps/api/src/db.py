from typing import AsyncGenerator, Dict, Any
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


async def get_db() -> AsyncGenerator[AsyncSession, None]:
    """Generador de sesiones asíncronas de base de datos para inyección de dependencias."""
    async with AsyncSessionLocal() as session:
        try:
            yield session
        finally:
            await session.close()


async def check_database_health() -> Dict[str, Any]:
    """Verifica la conectividad activa contra PostgreSQL y reporta el usuario conectado."""
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
