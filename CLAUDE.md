# Facturia · Guía de Contexto y Desarrollo

> **Facturia**: SaaS multi-tenant mexicano de facturación automática de tickets de compra (CFDI 4.0).
> El usuario fotografía un ticket, un agente extrae los datos con visión, entra al portal del comercio (o usa API/correo), lo llena y el CFDI timbrado llega directo al correo del usuario. Si el portal pide captcha, se activa un handoff humano en vivo por unos segundos para resolverlo y el agente continúa automáticamente.

---

## 1. Stack Tecnológico

- **Web**: Next.js 15 (App Router) + TypeScript + Tailwind CSS + shadcn/ui.
- **Auth**: Auth.js v5 con Google OAuth (aislado por tenants).
- **API**: FastAPI (Python 3.12) + Pydantic v2.
- **ORM / Migraciones**: SQLAlchemy 2 (AsyncIO) + Alembic.
- **Base de Datos**: PostgreSQL 16 con **Row-Level Security (RLS)** estricto.
- **Cola de Tareas**: Redis 7 + ARQ (Async Redis Queue).
- **Automatización de Portales**: Playwright (exclusivamente para portales que carecen de API o buzón de correo).
- **Almacenamiento de Objetos**: S3 / MinIO con ciclo de vida (TTL) de 24 horas (solo para imágenes en tránsito).
- **Entrega de CFDI**: Descarga efímera cifrada en Redis (TTL 30 min, máx. 5 descargas) o envío directo del portal emisor al correo del cliente. Facturia NUNCA envía correos.
- **Gestores de Paquetes**: `pnpm` para frontend web, `uv` para Python (API y Worker).

---

## 2. Estructura del Monorepo

```text
facturia/
├── apps/
│   ├── web/           # Frontend en Next.js 15 (App Router, TypeScript, shadcn/ui)
│   ├── api/           # Backend REST en FastAPI (Python 3.12, Pydantic v2, SQLAlchemy async)
│   └── worker/        # Workers asíncronos en ARQ para extracción, Playwright y emisión
├── packages/
│   └── shared/        # Tipos compartidos, esquemas de validación y constantes
├── infra/
│   ├── docker-compose.yml  # Servicios locales: Postgres 16, Redis 7, MinIO
│   └── postgres/
│       └── init.sql        # Script de inicialización de roles (facturia_owner y facturia_app)
├── docs/              # Especificaciones y prototipo HTML
│   └── prototipo.html # Prototipo interactivo de referencia
├── CLAUDE.md          # Este archivo (reglas y contexto permanente)
└── README.md          # Guía rápida de levantamiento local
```

---

## 3. Las 11 Reglas No Negociables

Si cualquier tarea o cambio entra en conflicto con alguna de estas reglas, **DETENTE Y PREGUNTA**.

1. **Aislamiento por Postgres RLS, nunca en el ORM:**
   El aislamiento entre clientes (`tenants`) lo impone la base de datos mediante **Row-Level Security (RLS)**. Toda tabla con datos de cliente debe incluir la columna `tenant_id` y su correspondiente `POLICY`. Cada petición/transacción debe ejecutar `SET LOCAL app.tenant_id = '<tenant_id>'`. **Nunca** confíes únicamente en filtrar con `WHERE tenant_id = ...` en el ORM o código de la aplicación.

2. **Dos roles de base de datos (Dueño vs Aplicación):**
   - **`facturia_owner`**: Rol dueño de las tablas. Ejecuta las migraciones (Alembic) y crea/modifica esquemas.
   - **`facturia_app`**: Rol no-dueño utilizado por la API y los workers en runtime.
   *Razón fundamental:* En PostgreSQL, `FORCE ROW LEVEL SECURITY` solo surte efecto para roles que **no son dueños** de la tabla. Si la API o workers corrieran con el rol dueño, la RLS se ignoraría silenciosamente, vulnerando el aislamiento multi-tenant.

3. **La imagen del ticket se borra:**
   Las imágenes viven en el bucket S3/MinIO con una regla de ciclo de vida (TTL) de 24 horas y se eliminan **explícitamente** vía API en cuanto el ticket alcanza un estado final (`facturado`, `rechazado_definitivo`). La imagen nunca se copia a carpetas permanentes, cachés ni logs.

4. **El CFDI nunca se persiste en base de datos, S3 ni logs (y Facturia NUNCA manda correos):**
   Facturia nunca envía correos electrónicos. Si el portal del comercio emisor envía el CFDI al correo que le dimos, guardamos el UUID fiscal y el correo capturado. Si el motor obtiene los archivos (PDF/XML), se empaquetan en memoria en un ZIP cifrado con AES-256-GCM (usando la clave derivada del tenant) y se almacenan temporalmente en Redis con un TTL estricto de 30 minutos y un máximo de 5 descargas (tolerante a desconexiones). Pasados los 30 minutos o consumidas las descargas, el archivo se borra definitivamente. Jamás se persisten comprobantes en PostgreSQL, S3 ni logs.

5. **Nunca se evaden captchas:**
   Queda estrictamente prohibido integrar servicios de resolución automática de captcha (2captcha, Anti-Captcha, CapSolver, etc.), plugins de evasión/stealth, rotación agresiva de huellas o proxies residenciales oscuros. Si un portal presenta un captcha, se invoca el protocolo de **handoff humano** para que una persona física lo resuelva en vivo en segundos. Si un comercio bloquea la automatización de forma insalvable, su motor se categoriza como manual.

6. **Credenciales cifradas con AES-256-GCM por tenant:**
   Las credenciales guardadas para portales de comercio se cifran usando AES-256-GCM con claves derivadas criptográficamente por tenant (HKDF a partir de una llave maestra). Solo se descifran puntualmente en la memoria del worker que ejecuta la facturación. Jamás se exponen en la API ni se imprimen en logs.

7. **Cero PII (Información Personal Identificable) en logs:**
   Prohibido registrar en logs RFCs, nombres, correos electrónicos, números de ticket o montos. Los logs solo deben registrar `ticket_id`, `tenant_id`, `status` y tiempos de respuesta. El detalle descriptivo para auditoría interna se almacena de forma segura en la tabla de base de datos `ticket_events`.

8. **Montos monetarios en `Decimal`, jamás en `float`:**
   En Python se usa estrictamente la clase `Decimal` de la librería estándar. En PostgreSQL se declaran como `numeric(12,2)`. Nunca utilices `float` para importes, subtotales o impuestos.

9. **Idempotencia estricta:**
   El reprocesamiento de un ticket o reintento de una tarea bajo ninguna circunstancia debe originar una factura duplicada. Se validan folios, emisores y estados previos antes de cualquier llamada de emisión.

10. **El agente nunca inventa un dato fiscal:**
    El agente NUNCA inventa un dato fiscal que el portal solicite y el perfil fiscal no contenga (calle, colonia, número exterior, CURP, etc.). Si falta un dato o no coincide, la facturación falla de inmediato con `error_code="perfil_incompleto"` o `"datos_no_coinciden"`, **NO** gasta reintentos, el ticket queda rechazado pero reintentable sin resubir fotografía, y la UI guía al usuario para que complete o corrija su perfil fiscal.

11. **Fidelidad estricta a valores explícitos dados por el usuario:**
    Cuando el usuario proporcione un valor concreto —un ID de modelo, un precio, un RFC, una URL, una clave del catálogo del SAT o variables de entorno— úsalo tal cual. No lo sustituyas por lo que recuerdes, aunque tu versión te parezca más correcta o más reciente. Si crees que el valor que dio el usuario está mal, dilo y espera confirmación, pero jamás lo cambies en silencio.

---

## 4. Orden de Prioridad de Motores de Facturación

Al interactuar con comercios, el sistema prefiere los métodos de menor fragilidad:
1. **API / Web Service oficial** del comercio (ej. Walmart, Home Depot).
2. **Buzón de correo electrónico** (ej. gasolineras que reciben ticket escaneado).
3. **Portal Web con Playwright** (última opción para portales interactivos).

---

## 5. Comandos de Desarrollo y Operación

### 5.1 Infraestructura Local (Docker)
```bash
# Iniciar servicios (Postgres, Redis, MinIO, Mailpit)
cd infra
docker compose up -d

# Ver logs de servicios
docker compose logs -f

# Detener servicios
docker compose down
```

### 5.2 Backend API (`apps/api`)
```bash
cd apps/api

# Sincronizar dependencias con uv
uv sync

# Ejecutar servidor de desarrollo
uv run uvicorn src.main:app --reload --port 8000

# Probar endpoint de salud
curl http://localhost:8000/v1/health
```

### 5.3 Frontend Web (`apps/web`)
```bash
cd apps/web

# Instalar dependencias con pnpm
pnpm install

# Servidor de desarrollo
pnpm dev
```

### 5.4 Puertos Locales por Defecto
- **FastAPI**: `http://localhost:8000`
- **Next.js**: `http://localhost:3000`
- **PostgreSQL 16**: `localhost:5432` (BD: `facturia`)
- **Redis 7**: `localhost:6379`
- **MinIO Console**: `http://localhost:9001` (API: `9000`, user/pass: `minioadmin` / `minioadminpassword`)
- **Mailpit Web UI**: `http://localhost:8025` (SMTP: `1025`)
