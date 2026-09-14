# Facturia Web (`apps/web`)

Frontend móvil-primero para **Facturia**, construido con **Next.js 15 (App Router)**, **TypeScript**, **Tailwind CSS** y diseño basado en `docs/prototipo.html`.

---

## 📱 Guía Rápida: Probar desde tu Celular en la Misma Red Wi-Fi

Para probar la captura de tickets con la cámara de tu teléfono parado en la tienda:

### 1. Conecta tu computadora y tu celular a la misma red Wi-Fi

Asegúrate de que ambos dispositivos estén en el mismo módem o red local.

### 2. Obtén tu dirección IP local en la computadora

En tu Mac o Linux, ejecuta:
```bash
ipconfig getifaddr en0
```
*(Si usas Wi-Fi normalmente es `en0`; si estás por cable Ethernet, prueba con `en1` o `ip route`).*

Ejemplo de resultado:
```text
192.168.1.85
```

---

### 3. Configura la variable de la API en `apps/web`

Crea o edita tu archivo `.env.local` en `apps/web`:

```bash
NEXT_PUBLIC_ENV=development
NEXT_PUBLIC_API_URL=http://192.168.1.85:8000
```
*(Sustituye `192.168.1.85` por la IP que obtuviste en el paso anterior).*

---

### 4. Levanta el Backend escuchando en todas las interfaces (`0.0.0.0`)

En la terminal del backend (`apps/api`):
```bash
uv run uvicorn src.main:app --host 0.0.0.0 --port 8000 --reload
```

---

### 5. Levanta el Frontend escuchando en todas las interfaces (`0.0.0.0`)

En la terminal del frontend (`apps/web`):
```bash
npm run dev
```
*(El script `dev` ya incluye `-H 0.0.0.0 -p 3000` de forma predeterminada).*

---

### 6. Abre Facturia desde el navegador de tu celular

En Safari (iOS) o Chrome (Android) en tu teléfono, ingresa a:
```text
http://192.168.1.85:3000
```

1. Presiona **"Entrar en modo desarrollo"**.
2. Presiona **"＋ Subir ticket"**.
3. Toca el botón grande de captura: se abrirá la cámara de tu celular.
4. Toma la foto y presiona **"Procesar ticket"**.
5. Verás en vivo la bitácora del agente apareciendo por el stream SSE.

---

## 📸 Nota sobre la Cámara en el Navegador Móvil (HTTP vs HTTPS)

El botón de captura utiliza:
```html
<input type="file" accept="image/*,.heic,.heif,application/pdf" capture="environment" />
```
* **En iOS Safari y Android Chrome:** La etiqueta estándar de archivo con `capture="environment"` invoca la cámara nativa del sistema operativo **directamente sobre HTTP en red local** sin restricciones de certificado, porque no utiliza la API de streaming continuo `getUserMedia`.
* **Si tu navegador o política corporativa bloquea la cámara por no ser HTTPS:**
  
  Puedes activar HTTPS local con el certificado autofirmado integrado de Next.js ejecutando:
  ```bash
  npx next dev --experimental-https -H 0.0.0.0 -p 3000
  ```
  O mediante un túnel seguro gratuito con Cloudflare:
  ```bash
  cloudflared tunnel --url http://localhost:3000
  ```
  Y abres la liga `https://...trycloudflare.com` en tu celular.

---

## 🛠️ Scripts Disponibles

* `npm run dev`: Inicia el servidor de desarrollo en `0.0.0.0:3000`.
* `npm run build`: Compila la aplicación para producción con verificación estricta de tipos.
* `npm run start`: Inicia el servidor compilado en `0.0.0.0:3000`.
* `npm run generate-types`: Regenera los tipos de TypeScript a partir de `openapi.json`.
