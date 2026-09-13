# Facturia 🧾⚡

> **SaaS multi-tenant mexicano de facturación automática de tickets de compra (CFDI 4.0)**.
> Fotografía tu ticket: un agente extrae los datos con visión, accede al portal del comercio y emite la factura directo a tu correo. Ante un captcha, se abre un handoff en vivo para resolución humana en segundos.

---

## 🚀 Inicio Rápido

### 1. Requisitos Previos
- [Docker](https://www.docker.com/) y Docker Compose
- [uv](https://github.com/astral-sh/uv) (o Python 3.12+)
- [pnpm](https://pnpm.io/) y Node.js 20+ (para frontend)

---

### 2. Levantar la Infraestructura Local

Desde el directorio `infra/`:
```bash
cd infra
docker compose up -d
```

Esto arrancará los 4 servicios esenciales con sus respectivos healthchecks:
- **PostgreSQL 16** (`localhost:5432`): Base de datos `facturia`, con roles `facturia_owner` y `facturia_app`.
- **Redis 7** (`localhost:6379`): Cola para workers de ARQ.
- **MinIO** (`localhost:9000` / Consola en `http://localhost:9001`): Almacenamiento efímero de tickets (TTL 24h).
- **Mailpit** (`localhost:1025` / Web UI en `http://localhost:8025`): Servidor SMTP de desarrollo para previsualizar CFDIs enviados.

Para verificar que todos los servicios estén saludables:
```bash
docker compose ps
```

---

### 3. Levantar la API (`apps/api`)

Desde el directorio `apps/api`:

```bash
cd apps/api

# 1. Copiar variables de entorno si aún no existen
cp .env.example .env

# 2. Sincronizar entorno virtual con uv
uv sync

# 3. Arrancar el servidor de desarrollo
uv run uvicorn src.main:app --reload --port 8000
```

*(Si no dispones de `uv`, puedes utilizar `python3 -m venv .venv && source .venv/bin/activate && pip install -e .` y luego `uvicorn src.main:app --reload --port 8000`).*

---

### 4. Probar el Endpoint de Salud

Abre otra terminal o ejecuta:
```bash
curl http://localhost:8000/v1/health
```

Respuesta esperada (HTTP 200):
```json
{
  "status": "healthy",
  "app": "facturia-api",
  "version": "0.1.0",
  "environment": "development",
  "services": {
    "database": {
      "status": "connected",
      "connected_user": "facturia_app",
      "database": "facturia"
    }
  }
}
```

Nótese que `connected_user` es **`facturia_app`**, cumpliendo con la **Regla 2** para la aplicación estricta de Row-Level Security (RLS).

---

### 5. Estructura del Proyecto

```text
facturia/
├── apps/
│   ├── web/          # Frontend Next.js 15 (App Router)
│   ├── api/          # FastAPI Backend (Python 3.12)
│   └── worker/       # Workers de ARQ y Playwright
├── packages/
│   └── shared/       # Tipos y utilidades compartidas
├── infra/            # Docker Compose y scripts SQL
├── docs/             # Documentación técnica y prototipo interactivo
├── CLAUDE.md         # Reglas no negociables y contexto de arquitectura
└── README.md         # Esta guía
```

Para más detalles sobre las 9 reglas no negociables de seguridad y diseño, consulta [CLAUDE.md](./CLAUDE.md).
