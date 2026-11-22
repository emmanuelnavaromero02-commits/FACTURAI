# FACTURAI

Facturador personal de tickets mexicanos con una bandeja de expedientes, un navegador interno y cotejo de PDF y XML. FastAPI sirve el panel y controla los navegadores mediante Playwright dentro del mismo contenedor. Tu navegador de escritorio muestra el panel.

## Arranque con Docker

Desde un equipo con Docker y el complemento Compose v2:

```bash
git clone https://github.com/emmanuelnavaromero02-commits/FACTURAI.git
cd FACTURAI
docker compose up -d --build --wait
```

Abre [la bandeja](http://127.0.0.1:8765/#bandeja). En Windows también puedes descargar el ZIP del repositorio, extraerlo y dar doble clic en **Iniciar-Facturador.cmd**.

1. En **Navegador**, pega la dirección HTTPS que figura en el ticket, pulsa **Abrir portal** y confirma el dominio del comercio. En **Portales → Consultar comercios guardados** puedes buscar direcciones y guías conocidas.
2. Para probar la bandeja, pulsa **Cargar ticket de ejemplo** y **Revisar**. El ejemplo usa datos ficticios y queda fuera de la cola de emisión.
3. Para procesar tus compras al subirlas, guarda tu perfil fiscal, vincula tu plan en **Ajustes** y activa **Procesar al subir con ChatGPT** en la bandeja. Sube el ticket y sigue ahí la lectura. Al iniciar el trámite, el visor muestra el portal en vivo.

En Linux amd64, Docker instala Google Chrome Stable desde el repositorio firmado de Google y lo abre en modo gráfico con Xvfb. La barra del visor muestra **Google Chrome**. En Linux arm64 se instala Chromium con Firefox de respaldo. Para elegir esta variante en amd64 ejecuta `docker compose build --build-arg FACTURAI_BROWSER=browser-chromium` y después `docker compose up -d --wait`. Si el navegador falla, la bandeja, la lectura local y la recuperación de archivos siguen disponibles. El volumen `expedientes` conserva la cola, los archivos y los perfiles de navegador al reiniciar. El puerto 8765 se publica en el equipo local. Puedes detenerlo con `docker compose stop` y reanudarlo con `docker compose up -d`.

## Herramientas del contenedor

| En el panel | Qué puedes hacer |
| --- | --- |
| Bandeja → Revisar → Leer texto, QR y barras en el contenedor | Leer una foto con Tesseract en español e inglés, girar una imagen y elegir la página de un PDF. El lector distingue texto digital y escaneos. Puedes copiar un código al ticket o elegir una liga impresa como portal. La dirección queda para tu revisión. Funciona sin conectar cuentas. |
| Portales → Enlace impreso en el ticket | Abrir la dirección HTTPS pública del comercio con el folio y los parámetros originales. Confirma el dominio antes de navegar. |
| Navegador → Navegación libre | Abre portales HTTPS y conserva sus sesiones en el volumen privado. **Abrir Gmail en mi Chrome** abre el buzón en una pestaña de tu equipo. **Cuenta de Google** inicia la vinculación de Gmail para FACTURAI o abre Ajustes para importar el cliente OAuth. |
| Navegador → Tomar control | Usar el mouse, teclado y portapapeles de entrada. Puedes buscar en el texto visible y guardar una captura PNG. Al navegar por cuenta propia también están disponibles Atrás y Adelante. |
| Registro en un portal de facturación | En un formulario reconocido, el agente genera y guarda la clave cifrada por comercio y correo. Para el alta puede verificar el mensaje en el Gmail vinculado. Usa el teléfono propio configurado si el formulario lo exige. **Cuenta y datos fiscales** permite retomar un paso pendiente, incluso con un correo de registro exportado. |
| Selector de archivos del portal | En el navegador haz clic en el campo del comercio. El panel muestra el dominio que recibirá el archivo. Elige tu constancia o ticket en PDF o foto y pulsa **Adjuntar al portal**. |
| Navegador → Descargas manuales | Descargar los PDF, XML y ZIP que el navegador recogió mientras tenías el control de un portal. Desde el menú del ticket, **Usar descargas del navegador** permite elegir los archivos y cotejarlos con ese expediente. Los archivos obtenidos en un trámite de la cola se asocian directamente con él. |
| Bandeja → Importar desde Gmail | Buscar por comercio, remitente o reservación. Abre el mensaje y marca el texto o los adjuntos concretos que contienen el ticket. Sirve también para vuelos con los datos en el cuerpo del correo. Requiere la conexión de lectura de Gmail. |
| Navegador → Abrir WhatsApp Web | Vincular el dispositivo, abrir la foto del ticket y llevarla a la bandeja. La acción de WhatsApp del expediente permite gestionar la solicitud con el comercio. |

ChatGPT recibe imágenes y texto para las operaciones de IA que elijas. El OCR, lector PDF, decodificador de QR y barras, controles de Playwright, descargas y cotejo se ejecutan en el contenedor. Las verificaciones interactivas del comercio se completan en la vista en vivo.

## Recuperación ante fallos

En la bandeja o el visor, **Retomar trámite** vuelve a leer la página del expediente. **Recuperar comprobantes** comprueba las descargas y busca los documentos del intento anterior. Para CAPUFE también abre la consulta por código alfanumérico que ofrece el comercio. La aplicación coteja cualquier archivo obtenido con la compra y el perfil fiscal del expediente.

Un CFDI de CAPUFE puede incluir varios peajes. Si el código impreso del ticket identifica una partida y su importe con impuestos coincide, el expediente muestra cuánto corresponde al ticket y cuánto suma el CFDI completo. Los botones PDF y XML entregan los documentos originales del comercio.

Con **Procesar al subir** activo, las lecturas pendientes y las consultas de comprobantes se reintentan con esperas crecientes que sobreviven a un reinicio. El botón permite solicitar otra consulta tras agotar la espera automática. Una pausa por CAPTCHA, permiso o aprobación conserva la pestaña para la persona. Cuando CAPUFE confirma expresamente que todos los códigos siguen sin facturar, un expediente con intervención anterior y sin intento de emisión puede avanzar hasta una nueva revisión para aprobación.

Cuando el comercio pide «No soy un robot», el panel muestra **Resolver verificación** con el ticket correspondiente. Pulsa el botón desde el equipo que tenga acceso al panel, completa el reto en la vista en vivo y pulsa **Devolver control**. FACTURAI comprueba que el formulario esté disponible antes de continuar. El aviso se muestra mientras el panel está abierto. La instancia personal en Azure se abre con el túnel privado de ese equipo.

Abre **Ajustes → Herramientas y recuperación → Comprobar herramientas**. Se prueban lectura y escritura en SQLite, OCR, QR, barras, PDF, ZIP, datos de un CFDI sintético y control del navegador activo. El diagnóstico distingue las pruebas locales del estado de las cuentas vinculadas. Durante un trámite conserva la página activa y muestra el navegador como ocupado.

| Qué ocurrió | Cómo continuar con el mismo ticket |
| --- | --- |
| ChatGPT quedó sin cuota o perdió conexión | **Continuar a mano** abre los controles en la misma página. Al terminar pulsa **Terminar y cotejar**. También puedes iniciar en modo manual desde **Preparar**. |
| El navegador falla al arrancar | En la variante de Google Chrome, comprueba que arranquen Chrome y Xvfb. En la variante de Chromium, la aplicación intenta iniciar Firefox. Cada navegador guarda su propio perfil. |
| Se cerró el navegador | Antes de la emisión y de ceder el control, el trámite intenta reabrir el portal una vez. Después de un posible envío se recupera el CFDI existente. Si el portal ya confirmó la emisión, el expediente recuerda esa confirmación incluso sin UUID visible. En **Ajustes** se puede reiniciar el navegador cuando esté libre. |
| La foto o la lectura con IA falló | La cola intenta leer el texto, códigos, forma de pago y datos impresos con el lector local. Si el plan está desconectado, guarda el borrador y continúa con las otras cargas. **Revisar** permite corregirlo. |
| Necesitas los datos de tu constancia fiscal | En **Ajustes → Subir constancia**, selecciona el PDF o una foto. La lectura local llena los campos del receptor y muestra los regímenes activos que pudo identificar. Revisa el correo de entrega y el uso del CFDI y pulsa **Guardar datos fiscales**. La copia de la constancia que usa el lector se elimina al terminar. |
| El PDF y el XML llegaron por canales distintos | Adjunta el que tengas y conserva el expediente. Después recupera el otro de Gmail, de las descargas del navegador, de un correo exportado `.eml` o de tus archivos. Se cotejan UUID, RFC, importe y datos fiscales. Un PDF escaneado se intenta leer con OCR en sus primeras cuatro páginas. |
| Gmail o WhatsApp están desconectados | En **Bandeja** sube el correo `.eml`, las fotos, el PDF o un ZIP exportado de WhatsApp. Para recuperar una factura de un correo, usa **Adjuntar PDF/XML, ZIP o correo** en el ticket. |
| El comercio tiene un portal propio | En **Revisar**, selecciona **Portal que aparece en mi ticket**, pega la dirección HTTPS pública y guarda. **Preparar** muestra el dominio al que autorizas enviar los datos fiscales. |
| El comercio recibe solicitudes por mensaje | En **Revisar**, selecciona **Otro comercio · solicitar directamente**. Abre WhatsApp desde el expediente para enviar la solicitud a su número de facturación y recuperar la respuesta. También puedes copiar el mensaje si el comercio usa otro canal. |

Si la conexión se pierde mientras alguien controla el trámite, se conserva como pendiente de recuperación. Un archivo aislado o una captura de la página de éxito todavía requieren el cotejo de la pareja PDF/XML.

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
python -m playwright install chromium firefox
python -m facturador
```

En Linux instala las dependencias del navegador con `python -m playwright install --with-deps chromium` si tu distribución lo requiere. Si tu equipo ya tiene Chromium puedes indicar el ejecutable en `FACTURADOR_CHROMIUM_PATH`. `.env.example` documenta las variables y puede copiarse como `.env` para ajustar el directorio de datos.

## Cómo se usa

1. En **Bandeja**, importa fotos, PDF o un ZIP exportado de WhatsApp. También puedes abrir **Navegador → Abrir WhatsApp Web**, vincular tu dispositivo, abrir una foto y pulsar **Importar foto abierta**.
2. Con **Procesar al subir con ChatGPT** activo, la lectura comienza en el servidor y la bandeja muestra el avance. Si falta un dato, abre la acción del ticket y pulsa **Guardar revisión** al completarlo. Para fotos con varios papeles, importa cada ticket por separado. Para una factura conjunta de CAPUFE, registra en un expediente la suma y los códigos que correspondan.
3. Los tickets listos con un comercio reconocido pasan al navegador por orden. Si el ticket trae un portal nuevo, revisa y confirma su dominio en el panel. Puedes volver a la pestaña del trámite con **Ver en vivo**. Ante un CAPTCHA o un inicio de sesión, **Tomar control** habilita clics y teclado en la misma página. **Continuar** devuelve el trámite al agente. Con el procesamiento automático desactivado, usa **Leer con ChatGPT** y después **Preparar**.
4. La emisión muestra una revisión antes del envío. En CAPUFE puedes elegir emisión automática. Solo aplica a su confirmación reconocida cuando el código comprueba la tabla de peajes, fecha, total, agrupación, forma de pago y perfil. En portales que cambien de flujo, revisa la pantalla o guarda el pendiente.
5. La app recoge las descargas del portal. Si ofrece envío por correo, recupera desde **Comprobantes** o desde las acciones del ticket. También admite adjuntar PDF/XML/ZIP. La pareja se entrega como comprobada cuando el XML coteja receptor, importe, pago, timbre y uso, y el PDF refleja UUID y RFC.
6. En un expediente con la pareja cotejada, usa **Consultar al SAT** para conocer la vigencia del CFDI. La consulta envía los identificadores del comprobante al servicio oficial y guarda la respuesta con fecha. Ante un fallo temporal puedes consultar el mismo CFDI otra vez. [Documentación del servicio del SAT](https://www.sat.gob.mx/minisitio/Factura/documentos/cancelacion/consulta_cfdi.pdf)

Cada intento de emisión queda registrado antes del clic que puede timbrar. Ante un resultado incierto se conservan el expediente y los archivos para recuperar la factura. El menú **Preparar solicitud** redacta el mensaje que puedes copiar al canal del comercio.

La actividad del ticket registra los campos que el portal aceptó. El agente vuelve a leer los controles después de elegir la forma de pago, el régimen y el uso fiscal. Si el comercio pide una verificación personal, la misma pestaña queda disponible en **Navegador**. Los avisos de factura previa pasan a recuperación. Con procesamiento automático y Gmail vinculado, un expediente con emisión confirmada y UUID puede buscar sus adjuntos por ese UUID y cotejarlos con el perfil y los archivos ya recibidos.

### Solicitar la factura por WhatsApp

1. Guarda tu perfil fiscal, revisa el ticket y abre **Solicitar por WhatsApp** en su expediente. Si es la primera vez, vincula WhatsApp Web en el navegador de la app.
2. Escribe el número internacional de facturación que proporcionó el comercio. El ticket original se incluye automáticamente. Puedes elegir la constancia y el comprobante de pago, revisar el correo de recepción y confirmar el envío.
3. La app comprueba el número en WhatsApp y envía un mensaje con una referencia del expediente. Si el navegador pierde la confirmación, revisa el chat antes de completar cualquier adjunto pendiente.
4. Puedes activar el seguimiento automático de respuestas mientras Docker esté en ejecución y WhatsApp siga vinculado. Puedes pausarlo desde el expediente. **Buscar PDF y XML en el chat** hace una revisión inmediata. Si hay varias solicitudes al mismo número, la referencia permite distinguirlas.
5. Cuando llegue un solo archivo, puedes agregar su pareja más tarde, incluso si llegó por correo. El panel verifica los archivos contra el perfil fiscal guardado en el momento del envío.

## Conexiones y datos

- **ChatGPT** usa OAuth de aplicación local y la cuota del plan autorizado. La conexión usa HTTPS con OpenAI. Cuando la cuenta informa un límite, conserva los tickets y permite continuar al recuperar cupo. En [Ajustes de uso de ChatGPT](https://chatgpt.com/#settings/usage) puedes consultar los límites y si autorizaste créditos. El panel permite desconectar el plan.
- **Gmail.** Habilita Gmail API en el proyecto de Google Cloud y crea un cliente OAuth de tipo **Aplicación de escritorio**. Si el proyecto está en modo de prueba, añade tu cuenta a los usuarios de prueba. En la aplicación personal, abre **Ajustes → Gmail**, importa el JSON del cliente y pulsa **Conectar Gmail**. En Google Chrome de tu equipo, elige el buzón que usará FACTURAI y concede lectura. Al volver al panel, FACTURAI comprueba que Gmail responde antes de guardar la conexión. Para recuperar comprobantes puedes buscar por remitente, fecha o UUID. [Guía de Google para aplicaciones de escritorio](https://developers.google.com/identity/protocols/oauth2/native-app?hl=es)
- **Gmail por el túnel de Azure.** Mantén el lanzador y el túnel activos hasta volver a Ajustes. Google devuelve la autorización a `127.0.0.1:8765` en el mismo equipo donde abriste Chrome. Una instalación de prueba iniciada con `--port 18765` recibe la respuesta en `127.0.0.1:18765`. Con un túnel, el puerto local de la app y el remoto deben coincidir. Un rechazo o un intento vencido se puede volver a iniciar desde Ajustes. Para cambiar de cliente OAuth con una cuenta conectada, primero usa **Desvincular Gmail**.
- **Historial previo** admite el `historial_sesion.json` del facturador anterior. Los expedientes y originales siguen en el volumen privado. Puedes exportar el registro a CSV.

El alcance de esta entrega es una instancia privada para una persona. La modalidad comercial alojada con el plan de cada cliente depende del [programa de integración de OpenAI](https://developers.openai.com/siwc/request-client-id), autenticación para clientes, separación de sus datos y un despliegue para ese uso. Los portales del catálogo tienen guías de navegación. Su acceso y las emisiones en vivo dependen del portal y de la sesión activa.

## Verificación

```bash
python -m unittest discover -s tests -v
node --check web/app.js
node --check web/visor.js
node --check web/herramientas.js
node --check web/seguimiento.js
node --test tests/seguimiento-panel.test.mjs
docker compose config
```

Las pruebas de integración en `tests/smoke_*.py` usan tickets y comercios simulados para comprobar el lector, la aprobación, los adjuntos y la recuperación ante fallos. El flujo de GitHub Actions se encuentra archivado en `.github/workflows-desactivados/`.

La conexión con una cuenta real se verifica desde **Ajustes** con autorización de su titular. Las facturas de ejemplo se usan para revisión sin abrir un portal comercial.

El perfil de seccomp del contenedor proviene de [Playwright 1.64.1](https://github.com/microsoft/playwright/blob/v1.64.1/utils/docker/seccomp_profile.json). Su licencia está en `docker/LICENSE-playwright.txt`.
