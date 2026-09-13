import pytest
from httpx import ASGITransport, AsyncClient
from unittest.mock import patch
from src.main import app


@pytest.mark.asyncio
async def test_health_check_healthy():
    """Prueba que /v1/health responda con 200 cuando la base de datos está conectada."""
    with patch("src.main.check_database_health") as mock_db:
        mock_db.return_value = {
            "status": "connected",
            "connected_user": "facturia_app",
            "database": "facturia",
        }

        transport = ASGITransport(app=app)
        async with AsyncClient(transport=transport, base_url="http://test") as client:
            response = await client.get("/v1/health")

        assert response.status_code == 200
        data = response.json()
        assert data["status"] == "healthy"
        assert data["services"]["database"]["status"] == "connected"
        assert data["services"]["database"]["connected_user"] == "facturia_app"


@pytest.mark.asyncio
async def test_health_check_unhealthy():
    """Prueba que /v1/health responda con 503 cuando la base de datos falla."""
    with patch("src.main.check_database_health") as mock_db:
        mock_db.side_effect = Exception("Database connection refused")

        transport = ASGITransport(app=app)
        async with AsyncClient(transport=transport, base_url="http://test") as client:
            response = await client.get("/v1/health")

        assert response.status_code == 503
        data = response.json()
        assert data["status"] == "unhealthy"
        assert data["services"]["database"]["status"] == "error"
