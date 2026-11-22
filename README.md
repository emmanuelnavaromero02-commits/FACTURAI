# FACTURAI

Facturador personal de tickets mexicanos con una bandeja de expedientes, un navegador Chromium propio y cotejo de PDF y XML. FastAPI sirve el panel y controla el navegador mediante Playwright dentro del mismo contenedor. Tu navegador de escritorio solo muestra el panel.

## Arranque con Docker

Desde un equipo con Docker y el complemento Compose v2:

```bash
git clone https://github.com/emmanuelnavaromero02-commits/FACTURAI.git
cd FACTURAI
docker compose up -d --build --wait
```

Abre [la bandeja](http://127.0.0.1:8765/#bandeja). En Windows también puedes descargar el ZIP del repositorio, extraerlo y dar doble clic en **Iniciar-Facturador.cmd**.

1. En **Navegador → Abrir navegador** aparece el directorio de comercios dentro de Chromium. Pulsa **Tomar control** para buscar y abrir un portal. También puedes abrirlo desde la ficha del comercio en **Portales**. Esto funciona antes de conectar cuentas.
2. Para probar la bandeja, pulsa **Cargar ticket de ejemplo** y **Revisar**. El ejemplo usa datos ficticios y queda fuera de la cola de emisión.
3. Para procesar compras, guarda tu perfil fiscal en **Ajustes**, vincula tu plan y sube un ticket. Las pantallas de acceso o verificación del comercio se atienden en ese mismo navegador.

Docker comprueba que la aplicación y Chromium estén listos antes de dar por terminado el arranque. El volumen `expedientes` conserva la cola, los archivos y el perfil de Chromium al reiniciar. El puerto 8765 se publica en el equipo local. Para detenerlo ejecuta `docker compose stop`; para reanudarlo, `docker compose up -d`.

## Herramientas del contenedor

| En el panel | Qué puedes hacer |
| --- | --- |
| Bandeja → Revisar → Leer texto, QR y barras en el contenedor | Leer una foto con Tesseract en español e inglés, girar una imagen y elegir la página de un PDF. El lector distingue texto digital y escaneos. Puedes agregar un código a los campos del ticket o cargar en Chromium una liga de un portal del catálogo. Funciona sin conectar cuentas. |
| Portales → Enlace impreso en el ticket | Abrir la dirección HTTPS exacta del recibo, con su folio y parámetros, en un dominio del catálogo. |
| Navegador → Tomar control | Usar el mouse, teclado y portapapeles de entrada. Puedes buscar en el texto visible y guardar una captura PNG. Al navegar por cuenta propia también están disponibles Atrás y Adelante. |
| Selector de archivos del portal | En Chromium haz clic en el campo del comercio. El panel muestra el dominio que recibirá el archivo. Elige tu constancia o ticket en PDF o foto y pulsa **Adjuntar al portal**. |
| Navegador → Descargas manuales | Descargar los PDF, XML y ZIP que el navegador recogió mientras tenías el control de un portal. Desde el menú del ticket, **Usar descargas del navegador** permite elegir los archivos y cotejarlos con ese expediente. Los archivos obtenidos en un trámite de la cola se asocian directamente con él. |
| Bandeja → Importar desde Gmail | Buscar por comercio, remitente o reservación. Abre el mensaje y marca el texto o los adjuntos concretos que contienen el ticket. Sirve también para vuelos con los datos en el cuerpo del correo. Requiere la conexión de lectura de Gmail. |
| Navegador → Abrir WhatsApp Web | Vincular el dispositivo, abrir la foto del ticket y llevarla a la bandeja. |

ChatGPT recibe imágenes y texto para las operaciones de IA que elijas. El OCR, lector PDF, decodificador de QR y barras, controles de Playwright, descargas y cotejo se ejecutan en el contenedor. Las verificaciones interactivas del comercio se completan en la vista en vivo.

### Código de este repositorio

El Compose de la raíz inicia la aplicación personal. La API y el frontend multicuenta previos están en `apps/api` y `apps/web`, con su infraestructura de desarrollo en `infra`. Las dos aplicaciones comparten la preparación de fotos y la lectura de QR y códigos de barras. Los originales de los tickets se guardan intactos.

## Arranque sin Docker

Python 3.12, en el directorio del proyecto:

```bash
python -m venv .venv
```

En Windows, usa `.venv\Scripts\python.exe`. En Linux o macOS, usa `.venv/bin/python`. Los comandos siguientes asumen que activaste ese entorno:

```bash
python -m pip install -r requirements.txt
python -m playwright install chromium
python -m facturador
```

En Linux instala las dependencias del navegador con `python -m playwright install --with-deps chromium` si tu distribución lo requiere. Si tu equipo ya tiene Chromium puedes indicar el ejecutable en `FACTURADOR_CHROMIUM_PATH`. `.env.example` documenta las variables y puede copiarse como `.env` para ajustar el directorio de datos.

## Cómo se usa

1. En **Bandeja**, importa fotos, PDF o un ZIP exportado de WhatsApp. También puedes abrir **Navegador → Abrir WhatsApp Web**, vincular tu dispositivo, abrir una foto y pulsar **Importar foto abierta**.
2. El menú del ticket ofrece **Leer con ChatGPT**. Comprueba folio, fecha, importe y forma de pago en **Revisar**. Selecciona el portal y guarda. Para fotos con varios papeles, importa cada ticket por separado. Para una factura conjunta de CAPUFE, registra en un expediente la suma y los códigos que correspondan.
3. Pulsa **Preparar** o **Preparar listos**. El facturador abre las solicitudes en orden. En **Navegador** ves la página activa. Ante un CAPTCHA, inicio de sesión o paso manual, **Tomar control** habilita clics y teclado sobre la misma página. **Continuar** devuelve el trámite al agente.
4. La emisión muestra una revisión antes del envío. En CAPUFE puedes elegir emisión automática. Solo aplica a su confirmación reconocida cuando el código comprueba la tabla de peajes, fecha, total, agrupación, forma de pago y perfil. En portales que cambien de flujo, revisa la pantalla o guarda el pendiente.
5. La app recoge las descargas del portal. Si ofrece envío por correo, recupera desde **Comprobantes** o desde las acciones del ticket. También admite adjuntar PDF/XML/ZIP. La pareja se entrega como comprobada cuando el XML coteja receptor, importe, pago, timbre y uso, y el PDF refleja UUID y RFC. Para vigencia o cancelación consulta al SAT.

Cada intento de emisión queda registrado antes del clic que puede timbrar. Ante un resultado incierto se conservan el expediente y los archivos para recuperar la factura. El menú **Preparar solicitud** redacta el mensaje que puedes copiar al canal del comercio.

## Conexiones y datos

- **ChatGPT** usa OAuth de aplicación local y la cuota del plan autorizado. La conexión usa HTTPS con OpenAI. Cuando la cuenta informa un límite, conserva los tickets y permite continuar al recuperar cupo. En [Ajustes de uso de ChatGPT](https://chatgpt.com/#settings/usage) puedes consultar los límites y si autorizaste créditos. El panel permite desconectar el plan.
- **Gmail** requiere un cliente OAuth de escritorio propio, creado en Google Cloud con Gmail API habilitada. En un proyecto de prueba añade tu cuenta a los usuarios habilitados. Importa el JSON en **Ajustes → Gmail**, conecta y autoriza lectura. Para recuperar comprobantes usa una búsqueda acotada, por ejemplo `from:sistema@quadrum.mx newer_than:7d has:attachment` o el UUID entre comillas. La app consulta mensajes y descarga adjuntos en modo lectura.
- **Historial previo** admite el `historial_sesion.json` del facturador anterior. Los expedientes y originales siguen en el volumen privado. Puedes exportar el registro a CSV.

El alcance de esta entrega es una instancia privada para una persona. La modalidad comercial alojada con el plan de cada cliente depende del [programa de integración de OpenAI](https://developers.openai.com/siwc/request-client-id), autenticación para clientes, separación de sus datos y un despliegue para ese uso. Los portales del catálogo tienen guías de navegación. Su acceso y las emisiones en vivo dependen del portal y de la sesión activa.

## Verificación

```bash
python -m unittest discover -s tests -v
node --check web/app.js
node --check web/visor.js
node --check web/herramientas.js
docker compose config
```

Las pruebas cubren lectura local, importación selectiva de mensajes, deduplicación y recuperación de sesión. GitHub Actions construye la imagen y recorre tres flujos en Chromium. El primero verifica OCR, teclado, lectura de página y captura PNG. El segundo usa un comercio simulado para aprobar un trámite y cotejar el PDF y XML. El tercero usa el panel para navegar por el historial, adjuntar un documento y descargarlo de un portal simulado. Se comprueban los bytes entregados en ambos sentidos.

La conexión con una cuenta real se verifica desde **Ajustes** con autorización de su titular. Las facturas de ejemplo se usan para revisión sin abrir un portal comercial.

El perfil de seccomp del contenedor proviene de [Playwright 1.64.1](https://github.com/microsoft/playwright/blob/v1.64.1/utils/docker/seccomp_profile.json). Su licencia está en `docker/LICENSE-playwright.txt`.
