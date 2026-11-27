# FACTURAI

Facturador de tickets mexicanos con una bandeja de expedientes, un navegador interno y cotejo de PDF y XML. FastAPI sirve el panel y controla los navegadores mediante Playwright dentro del mismo contenedor. Tu navegador de escritorio muestra el panel.

Tiene dos modos de uso. **Local** es el de siempre: una persona, sin cuentas y el panel solo en `127.0.0.1`. **Varios usuarios** pone una puerta de entrada con cuentas y le da a cada cliente sus propios datos (ver [Dos modos de uso](#dos-modos-de-uso)). Sin configurar nada arranca el modo local.

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
3. Para procesar tus compras al subirlas, guarda tu perfil fiscal, conecta una IA en **Ajustes** (tu plan de ChatGPT o una llave de pago por uso, ver [Inteligencia artificial](#inteligencia-artificial)) y activa **Procesar al subir** en la bandeja. Sube el ticket y sigue ahí la lectura. Al iniciar el trámite, el visor muestra el portal en vivo.

En Linux amd64, Docker instala Google Chrome Stable desde el repositorio firmado de Google y lo abre en modo gráfico con Xvfb. La barra del visor muestra **Google Chrome**. En Linux arm64 se instala Chromium con Firefox de respaldo. Para elegir esta variante en amd64 ejecuta `docker compose build --build-arg FACTURAI_BROWSER=browser-chromium` y después `docker compose up -d --wait`. Si el navegador falla, la bandeja, la lectura local y la recuperación de archivos siguen disponibles. El volumen `expedientes` conserva la cola, los archivos y los perfiles de navegador al reiniciar. El puerto 8765 se publica solo en el equipo local (`127.0.0.1`). Puedes detenerlo con `docker compose stop` y reanudarlo con `docker compose up -d`.

## Herramientas del contenedor

| En el panel | Qué puedes hacer |
| --- | --- |
| Bandeja → Revisar → Leer texto, QR y barras en el contenedor | Leer una foto con Tesseract en español e inglés, girar una imagen y elegir la página de un PDF. El lector distingue texto digital y escaneos. Puedes copiar un código al ticket o elegir una liga impresa como portal. La dirección queda para tu revisión. Funciona sin conectar cuentas. |
| Portales → Enlace impreso en el ticket | Abrir la dirección HTTPS pública del comercio con el folio y los parámetros originales. Confirma el dominio antes de navegar. |
| Navegador → Navegación libre | Abre portales HTTPS y conserva sus sesiones en el volumen privado. **Abrir Gmail en mi Chrome** abre el buzón en una pestaña de tu equipo. **Cuenta de Google** inicia la vinculación de Gmail para FACTURAI o abre Ajustes para importar el cliente OAuth. |
| Navegador → Tomar control | Usar el mouse, teclado y portapapeles de entrada. Puedes buscar en el texto visible y guardar una captura PNG. Al navegar por cuenta propia también están disponibles Atrás y Adelante. En el celular el visor ubica bien los toques y tiene un campo para escribir con el teclado del teléfono. |
| Registro en un portal de facturación | Cuando el portal exige cuenta, el agente la crea y guarda la clave cifrada por comercio y correo. Ver [Registro en portales](#registro-en-portales). **Cuenta y datos fiscales** permite retomar un paso pendiente, incluso con un correo de registro exportado. |
| Selector de archivos del portal | En el navegador haz clic en el campo del comercio. El panel muestra el dominio que recibirá el archivo. Elige tu constancia o ticket en PDF o foto y pulsa **Adjuntar al portal**. |
| Navegador → Descargas manuales | Descargar los PDF, XML y ZIP que el navegador recogió mientras tenías el control de un portal. Desde el menú del ticket, **Usar descargas del navegador** permite elegir los archivos y cotejarlos con ese expediente. Los archivos obtenidos en un trámite de la cola se asocian directamente con él. |
| Bandeja → Importar desde Gmail | Buscar por comercio, remitente o reservación. Abre el mensaje y marca el texto o los adjuntos concretos que contienen el ticket. Sirve también para vuelos con los datos en el cuerpo del correo. Requiere la conexión de lectura de Gmail. |
| Navegador → Abrir WhatsApp Web | Vincular el dispositivo, abrir la foto del ticket y llevarla a la bandeja. La acción de WhatsApp del expediente permite gestionar la solicitud con el comercio. |

La IA que elijas recibe imágenes y texto solo para las operaciones de lectura y de llenado de portales. El OCR, lector PDF, decodificador de QR y barras, controles de Playwright, descargas y cotejo se ejecutan en el contenedor. Las verificaciones interactivas del comercio («no soy un robot») se completan siempre en la vista en vivo.

## Recuperación ante fallos

En la bandeja o el visor, **Retomar trámite** vuelve a leer la página del expediente. **Recuperar comprobantes** comprueba las descargas y busca los documentos del intento anterior. Para CAPUFE también abre la consulta por código alfanumérico que ofrece el comercio. La aplicación coteja cualquier archivo obtenido con la compra y el perfil fiscal del expediente.

Un CFDI de CAPUFE puede incluir varios peajes. Si el código impreso del ticket identifica una partida y su importe con impuestos coincide, el expediente muestra cuánto corresponde al ticket y cuánto suma el CFDI completo. Los botones PDF y XML entregan los documentos originales del comercio.

Con **Procesar al subir** activo, las lecturas pendientes y las consultas de comprobantes se reintentan con esperas crecientes que sobreviven a un reinicio. El botón permite solicitar otra consulta tras agotar la espera automática. Una pausa por CAPTCHA, permiso o aprobación conserva la pestaña para la persona. Cuando CAPUFE confirma expresamente que todos los códigos siguen sin facturar, un expediente con intervención anterior y sin intento de emisión puede avanzar hasta una nueva revisión para aprobación.

Cuando el comercio pide «No soy un robot», el panel muestra **Resolver verificación** con el ticket correspondiente. Pulsa el botón desde el equipo que tenga acceso al panel, completa el reto en la vista en vivo y pulsa **Devolver control**. FACTURAI comprueba que el formulario esté disponible antes de continuar. El aviso se muestra mientras el panel está abierto (y llega también como [aviso](#avisos) si lo activaste). La instancia personal en Azure se abre con el túnel privado de ese equipo.

Abre **Ajustes → Herramientas y recuperación → Comprobar herramientas**. Se prueban lectura y escritura en SQLite, OCR, QR, barras, PDF, ZIP, datos de un CFDI sintético y control del navegador activo. El diagnóstico distingue las pruebas locales del estado de las cuentas vinculadas. Durante un trámite conserva la página activa y muestra el navegador como ocupado.

| Qué ocurrió | Cómo continuar con el mismo ticket |
| --- | --- |
| La IA quedó sin cuota o perdió conexión | **Continuar a mano** abre los controles en la misma página. Al terminar pulsa **Terminar y cotejar**. También puedes iniciar en modo manual desde **Preparar**. |
| El navegador falla al arrancar | En la variante de Google Chrome, comprueba que arranquen Chrome y Xvfb. En la variante de Chromium, la aplicación intenta iniciar Firefox. Cada navegador guarda su propio perfil. |
| Se cerró el navegador | Antes de la emisión y de ceder el control, el trámite intenta reabrir el portal una vez. Después de un posible envío se recupera el CFDI existente. Si el portal ya confirmó la emisión, el expediente recuerda esa confirmación incluso sin UUID visible. En **Ajustes** se puede reiniciar el navegador cuando esté libre. |
| La foto o la lectura con IA falló | La cola intenta leer el texto, códigos, forma de pago y datos impresos con el lector local. Si la IA está desconectada, guarda el borrador y continúa con las otras cargas. **Revisar** permite corregirlo. |
| Necesitas los datos de tu constancia fiscal | En **Ajustes → Subir constancia**, selecciona el PDF o una foto. La lectura local llena los campos del receptor y muestra los regímenes activos que pudo identificar. Revisa el correo de entrega y el uso del CFDI y pulsa **Guardar datos fiscales**. La copia de la constancia que usa el lector se elimina al terminar. |
| El PDF y el XML llegaron por canales distintos | Adjunta el que tengas y conserva el expediente. Después recupera el otro de tu correo, de las descargas del navegador, de un correo exportado `.eml` o de tus archivos. Se cotejan UUID, RFC, importe y datos fiscales. Un PDF escaneado se intenta leer con OCR en sus primeras cuatro páginas. |
| El correo o WhatsApp están desconectados | En **Bandeja** sube el correo `.eml`, las fotos, el PDF o un ZIP exportado de WhatsApp. Para recuperar una factura de un correo, usa **Adjuntar PDF/XML, ZIP o correo** en el ticket. |
| El comercio tiene un portal propio | En **Revisar**, selecciona **Portal que aparece en mi ticket**, pega la dirección HTTPS pública y guarda. **Preparar** muestra el dominio al que autorizas enviar los datos fiscales. |
| El comercio recibe solicitudes por mensaje | En **Revisar**, selecciona **Otro comercio · solicitar directamente**. Abre WhatsApp desde el expediente para enviar la solicitud a su número de facturación y recuperar la respuesta, o pídela por correo (ver [Correo](#correo)). También puedes copiar el mensaje si el comercio usa otro canal. |

Si la conexión se pierde mientras alguien controla el trámite, se conserva como pendiente de recuperación. Un archivo aislado o una captura de la página de éxito todavía requieren el cotejo de la pareja PDF/XML.

### Código de este repositorio

El Compose de la raíz inicia la aplicación del facturador (`facturador/` y `web/`). La API y el frontend multicuenta previos están en `apps/api` y `apps/web`, con su infraestructura de desarrollo en `infra`; usan su propia configuración. Las dos aplicaciones comparten la preparación de fotos y la lectura de QR y códigos de barras. Los originales de los tickets se guardan intactos.

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

En Linux instala las dependencias del navegador con `python -m playwright install --with-deps chromium` si tu distribución lo requiere. Si tu equipo ya tiene Chromium puedes indicar el ejecutable en `FACTURADOR_CHROMIUM_PATH`. `.env.example` lista todas las variables de entorno que lee el programa, cada una con su explicación. Cópialo como `.env` para ajustarlas. `python -m facturador --port 9000` cambia el puerto (por omisión 8765).

## Cómo se usa

1. En **Bandeja**, importa fotos, PDF o un ZIP exportado de WhatsApp. También puedes abrir **Navegador → Abrir WhatsApp Web**, vincular tu dispositivo, abrir una foto y pulsar **Importar foto abierta**. Ver [Subir tickets](#subir-tickets).
2. Con **Procesar al subir** activo, la lectura comienza en el servidor y la bandeja muestra el avance. Si falta un dato, abre la acción del ticket y pulsa **Guardar revisión** al completarlo. Para fotos con varios papeles, importa cada ticket por separado. Para una factura conjunta de CAPUFE, registra en un expediente la suma y los códigos que correspondan.
3. Los tickets listos con un comercio reconocido pasan al navegador por orden. Si el ticket trae un portal nuevo, revisa y confirma su dominio en el panel. Puedes volver a la pestaña del trámite con **Ver en vivo**. Ante un CAPTCHA o un inicio de sesión, **Tomar control** habilita clics y teclado en la misma página. **Continuar** devuelve el trámite al agente. Con el procesamiento automático desactivado, usa **Leer ticket** y después **Preparar**.
4. La emisión muestra una revisión antes del envío. En CAPUFE puedes elegir emisión automática. Solo aplica a su confirmación reconocida cuando el código comprueba la tabla de peajes, fecha, total, agrupación, forma de pago y perfil. En portales que cambien de flujo, revisa la pantalla o guarda el pendiente.
5. La app recoge las descargas del portal. Si ofrece envío por correo, recupera desde **Comprobantes** o desde las acciones del ticket. También admite adjuntar PDF/XML/ZIP. La pareja se entrega como comprobada cuando el XML coteja receptor, importe, pago, timbre y uso, y el PDF refleja UUID y RFC.
6. En un expediente con la pareja cotejada, usa **Consultar al SAT** para conocer la vigencia del CFDI. La consulta envía los identificadores del comprobante al servicio oficial y guarda la respuesta con fecha. Ante un fallo temporal puedes consultar el mismo CFDI otra vez. [Documentación del servicio del SAT](https://www.sat.gob.mx/minisitio/Factura/documentos/cancelacion/consulta_cfdi.pdf)

Cada intento de emisión queda registrado antes del clic que puede timbrar. Ante un resultado incierto se conservan el expediente y los archivos para recuperar la factura. El menú **Preparar solicitud** redacta el mensaje que puedes copiar al canal del comercio.

La actividad del ticket registra los campos que el portal aceptó. El agente vuelve a leer los controles después de elegir la forma de pago, el régimen y el uso fiscal. Si el comercio pide una verificación personal, la misma pestaña queda disponible en **Navegador**. Los avisos de factura previa pasan a recuperación. Con procesamiento automático y un correo conectado, un expediente con emisión confirmada y UUID puede buscar sus adjuntos por ese UUID y cotejarlos con el perfil y los archivos ya recibidos.

### Subir tickets

- Las fotos de más de 1.5 MB o de más de 3200 píxeles por el lado mayor se reducen en el navegador a JPEG antes de subirse. Los PDF y los formatos que el navegador no puede abrir se envían tal cual.
- La subida muestra una barra de avance real.
- **Tomar foto** abre la cámara en el celular, en la bandeja y en el chat.
- Las fotos HEIC o HEIF de iPhone se convierten a JPEG al recibirlas en el servidor (con `pillow-heif`). El original que se guarda es ese JPEG.
- Cada ticket muestra su etapa y el tiempo que lleva en ella: Subiendo, Leyendo, Abriendo el portal, Llenando, Descargando y Lista. Si espera algo de ti aparece **Te necesito**. Durante la lectura se distingue si prepara la foto, lee el ticket o busca el portal del comercio.

### Cómo se lee un ticket

- La lectura con IA pide primero razonamiento bajo. Solo repite una vez con razonamiento alto si falta algo esencial: que sea un ticket, el importe, la fecha, o el folio y los códigos. Las dudas sobre la forma de pago o los QR de encuestas no provocan otra lectura.
- Si el modelo rechaza un nivel de razonamiento, se repite en nivel medio y ese nivel no se vuelve a pedir para ese modelo. El nivel por omisión de las demás consultas es medio.
- El texto y los códigos de cada página se leen una sola vez y se reutilizan en la lectura con IA y en los cotejos posteriores (hasta 32 páginas en memoria).
- La cola lee hasta tres tickets a la vez sin frenar la preparación del portal. Un ticket puede traer varias fotos: las otras fotos de un mismo ticket (hasta tres además de la principal) se leen juntas, y el frente y el reverso se emparejan.
- Cada lectura anota cuánto tardó en el texto local y en el modelo, sin datos del ticket. La lectura también toma el RFC del emisor y el WhatsApp o correo que el ticket indique para pedir la factura.

### Cómo resuelve un portal

El motor es general: no hay reglas por comercio en su código.

- **Plan por pantalla.** Una consulta a la IA decide toda la pantalla y devuelve varias acciones (llenar los campos y avanzar). Cada acción pasa por la misma tubería y por los mismos candados de emisión de siempre.
- **Memoria de portales.** Cuando un ticket termina con factura comprobada se guarda cómo se resolvió cada pantalla, en el archivo `memoria_portales.sqlite` de la carpeta de datos. Los valores se guardan como referencias al perfil y al ticket, no como datos de la persona. El siguiente ticket repite esas pantallas sin consultar a la IA y llega a los candados de emisión con sus propios datos. Si la pantalla cambió, decide la IA y se vuelve a aprender. Tres fallas desactivan una receta. Lo aprendido de un proveedor sirve en otro dominio solo con dos éxitos comprobados. `FACTURADOR_MEMORIA=0` apaga la repetición; lo resuelto se sigue aprendiendo.
- **Directorio de portales.** El archivo `directorio_portales.sqlite` relaciona el RFC emisor y el nombre del comercio con su portal, con tres niveles de evidencia: factura comprobada, liga impresa y búsqueda verificada. Se consulta antes de buscar en la web. La liga impresa en el ticket siempre manda sobre el directorio. No guarda datos de la persona.
- **Lo aprendido se comparte entre cuentas.** Solo con varios usuarios (`FACTURADOR_MODO=usuarios`). En el modo local nada cambia: la memoria y el directorio siguen en la carpeta de datos. Con varios usuarios cada cuenta conserva los suyos en su propia carpeta, y lo que comprueba se copia además a la carpeta `compartido/` de la instalación, que leen todas las cuentas. Así, el portal que resolvió un cliente ya no le cuesta consultas a la IA al siguiente.
  - **Qué se comparte.** Solo lo que es igual para cualquier persona y cualquier ticket: referencias al perfil y al ticket (nunca sus valores), los campos y botones del propio portal, teclas, y del directorio el RFC emisor y el nombre del comercio con la dirección de su portal. No se comparte nada de la persona ni de su compra: ni RFC, nombre, correo, teléfono o domicilio, ni folios, importes o códigos, ni contraseñas o sesiones. Tampoco una opción elegida en una lista (puede ser el estado, la sucursal o un domicilio guardado), una pantalla donde intervino una persona, ni una liga que nombre un ticket. Eso se queda en la cuenta que lo aprendió, que lo sigue usando igual.
  - **Cuánto confía una cuenta en las demás.** Lo propio va primero: lo que una cuenta ya comprobó no se sustituye por lo de otra. Una receta de otra cuenta se repite solo en el portal ya aprobado para ese ticket y pasa por los mismos candados de emisión. Un portal que solo conocen otras cuentas se abre sin preguntar únicamente si dos cuentas distintas obtuvieron ahí una factura comprobada del mismo RFC emisor; con menos, es una propuesta y la persona confirma el dominio. Una receta sirve en otro dominio solo con dos cuentas distintas detrás. Una falla cuenta una vez por cuenta: hacen falta tres cuentas seguidas para desactivar una receta para todos, y una sola cuenta solo la desactiva para sí misma.
  - **Límite que conviene conocer.** Dos cuentas puestas de acuerdo (o una persona con dos cuentas) pueden hacer que un portal se abra sin preguntar para las demás. Por eso las cuentas se dan por invitación del administrador, y el compartir se puede apagar.
  - **Arranque.** La carpeta compartida empieza vacía. Lo que cada cuenta ya sabía se queda en su carpeta y no se mezcla solo: se comparte lo que cada cuenta compruebe a partir de ese momento.
  - **Apagarlo o vaciarlo.** `FACTURADOR_COMPARTIDO=0` lo apaga y cada cuenta sigue solo con lo suyo. Para empezar de cero, borra la carpeta `compartido/`: ninguna cuenta pierde nada. La carpeta es parte de los datos; inclúyela en los respaldos.
- **Topes por intento.** Cada intento admite hasta 40 consultas a la IA, 600 segundos de trabajo activo (la espera de una persona no cuenta) y 120 pasos, con un máximo de dos intentos por otra ruta. Al agotarlos, el ticket se detiene sin enviar nada.
- **Preguntas que no se repiten.** El agente hace como máximo tres preguntas por trámite. Una pregunta sin respuesta deja el ticket esperando a la persona una sola vez.
- **Complementos de comercios.** Las ayudas que antes vivían dentro del motor para Walmart, CAPUFE, BP, Tesoro y Vianney están en módulos aparte (`facturador/complemento_*.py`), que el motor llama en puntos fijos. Un portal sin complemento corre solo con el motor general. `FACTURADOR_COMPLEMENTOS=0` manda a todos los comercios, también esos cinco, por el motor general.
- **Banco de portales.** Son 16 portales de facturación inventados con formas distintas (una página, asistente de pasos, iframe, campos sin etiqueta, ticket ya facturado, cuenta obligatoria, factura por correo, «no soy un robot», entre otras). Sirve para medir qué tan bien resuelve el motor portales que nunca vio, sin tocar ningún portal real, ni el perfil, ni la memoria de nadie:

  ```bash
  python -m facturador.banco [--ia-real] [--datos CARPETA] [--portales a,b,c] [--salida archivo.json] [--tiempo SEGUNDOS]
  ```

  | Opción | Qué hace |
  | --- | --- |
  | `--portales` | Corre solo los portales nombrados, separados por coma. Por omisión, todos. |
  | `--ia-real` | Usa la IA configurada en `--datos` en vez de la IA guionizada (determinista). Solo para correrlo a mano. |
  | `--datos` | Carpeta de datos con la IA configurada. Solo se usa con `--ia-real`. |
  | `--salida` | Archivo JSON con todas las cifras. Por omisión `banco-medicion.json`. |
  | `--tiempo` | Segundos máximos por ticket. Por omisión 240. |

  Cada portal se corre en dos pasadas: la segunda mide lo que ahorra la memoria. El comando imprime un resumen en español y guarda las cifras en el JSON.

### Varios tickets a la vez

Por omisión FACTURAI trabaja un ticket a la vez en el portal, y un ticket que espera a la persona (una verificación, una aprobación, un acceso) hace esperar a los demás. `FACTURADOR_TICKETS_A_LA_VEZ` (de 1 a 4; por omisión 1) permite que varios tickets recorran sus portales al mismo tiempo. Con 1 nada cambia.

- **Cada ticket simultáneo usa otro navegador.** El de siempre es el principal. Los demás se abren cuando reciben un ticket, cada uno con su propia carpeta de sesión (`browser_profile_2`, `browser_profile_3`…), y se cierran solos tras un minuto sin trabajo (quince minutos si guardan la página de un ticket de resultado incierto).
- **Un ticket que espera a la persona ya no detiene a los otros:** ocupa solo su navegador.
- **Nunca dos tickets del mismo portal a la vez.** La sesión, la cuenta y el alta de un portal son de una sola visita. El segundo ticket espera con «Esperando a que termine otro ticket del mismo portal.» y después usa el navegador que ya tiene esa sesión.
- **Los candados de emisión son los mismos.** Cada navegador corre el mismo motor, ticket por ticket, con los mismos cotejos y topes.
- **Una sola vista en vivo.** El panel muestra el ticket que más lleva esperando a la persona; si ninguno espera, el que elegiste; si no, el del navegador principal. Con más de un trámite en un portal aparece «Trámite a la vista» sobre el visor para elegir cuál ver, y cada ticket ofrece «Ver en vivo». El control de la página se toma siempre sobre lo que se ve.
- **Una sola pregunta abierta en el chat.** Si dos tickets necesitan preguntarte algo, la segunda pregunta espera a que contestes la primera o a que venza. Esa espera no se le cobra al trámite.
- **WhatsApp, el navegador personal y los envíos de correo** usan solo el navegador principal.
- **Memoria.** En una PC con Windows, con Chrome sin ventana y una página de prueba sencilla, cada navegador extra ocupó unos 380 MB (cerca de 260 MB de Chrome y 120 MB de su controlador). Un portal real pesa más y en el servidor no se midió: sube el valor de uno en uno y vigila la memoria. En Docker la variable va en `environment:` del servicio en `compose.yaml`, y conviene subir `shm_size`.
- **No aplica con varios usuarios** (`FACTURADOR_MODO=usuarios`): ahí cada cuenta trabaja un ticket a la vez y el tope de navegadores entre cuentas es `FACTURADOR_NAVEGADORES_MAX`.

Un valor que no sea de 1 a 4 se toma como 1 y deja un aviso en el registro. Esto se probó con los portales inventados del banco y la IA guionizada, no con portales reales.

### Permisos de una sola vez

En **Ajustes → Permisos** dejas autorizado lo que no quieres confirmar en cada ticket. Todos empiezan apagados y puedes quitarlos cuando quieras. Cada cambio deja un evento de auditoría con su fecha.

| Permiso | Qué permite |
| --- | --- |
| Registrarme en portales | Crear tu cuenta cuando un portal de facturación la exija, con los datos que dejaste. |
| Aceptar los términos del registro | Marcar la casilla de términos y aviso de privacidad al crear esa cuenta. |
| Pedir por WhatsApp | Pedir la factura por WhatsApp al comercio sin confirmarlo en cada ticket. |
| Pedir por correo | Pedir la factura por correo al comercio sin confirmarlo en cada ticket. |
| Leer mis fotos de WhatsApp | Tomar como tickets las fotos y PDF que te mandas a tu propio chat de WhatsApp. |

En **Ajustes → Datos para registros** dejas, si quieres, nombre, apellidos, teléfono, fecha de nacimiento y domicilio (calle, números, colonia, municipio, estado y país). Todo es opcional. Se guarda en un archivo privado de la carpeta de datos y solo sirve para llenar formularios de registro: nunca se manda a la IA ni se escribe en eventos.

### Registro en portales

1. **Alta sencilla.** Es el primer intento y no necesita permiso: un portal con un formulario mínimo se registra por reglas. El agente genera la clave, la guarda cifrada por comercio y correo y la escribe él mismo.
2. **Alta asistida.** Si el formulario pide más (nombre, apellidos, teléfono, fecha de nacimiento, domicilio, listas desplegables, casilla de términos) y diste el permiso **Registrarme en portales**, una consulta a la IA por pantalla dice de dónde sale cada campo, eligiendo de una lista cerrada de fuentes. El programa valida ese plan, llena el formulario, lee de vuelta lo escrito y lo envía. La IA nunca ve valores, contraseñas ni códigos.
3. **Varios pasos.** Admite registros de hasta 6 pantallas. Cada pantalla se anota antes de enviarse y nunca se envía dos veces. Reconoce más botones de registro, botones que no son `<button>` y el acceso en dos pasos. Nunca usa «Entrar con Google, Facebook o Apple» ni botones de pago, compra o emisión.
4. **Términos.** La casilla de términos solo se marca con el permiso **Aceptar los términos del registro**. Sin él, el ticket espera y dice qué hacer.
5. **Correo de alta.** El correo de confirmación del portal se espera hasta 10 minutos, diciendo cuánto lleva, y se lee de cualquier buzón conectado (ver [Correo](#correo)).
6. **Código al teléfono.** Si el portal manda un código por SMS o WhatsApp mientras el ticket abre o usa su cuenta, FACTURAI te lo pide en el chat y espera hasta 5 minutos. Lo escribe en el portal y no lo guarda: en el historial queda «Código enviado.» y la IA nunca lo ve. Si no contestas a tiempo, o el código llega cuando la pregunta ya cerró, lo escribes tú en el sitio y pulsas **Continuar**.

Siempre queda para la persona: resolver el «no soy un robot», cualquier dato que no dejó (no se inventa: la pausa dice cuál agregar), los códigos de segundo factor o recuperación, y los registros con más pasos de los permitidos o que cambian mientras se llenan.

### Correo

Conecta un buzón en **Ajustes → Correo**. Hay tres formas:

- **Gmail**, por su API con un cliente OAuth de escritorio (ver [Conexiones y datos](#conexiones-y-datos)). Solo lee, salvo que pulses **Permitir que Gmail también envíe**.
- **Outlook, Hotmail, Live y Microsoft 365**, entrando con tu cuenta de Microsoft (ver [Conexiones y datos](#conexiones-y-datos)). Microsoft dejó de aceptar contraseñas en estos buzones, también las de aplicación: FACTURAI recibe un permiso que se renueva solo y con él abre el buzón. Tu contraseña no pasa por FACTURAI y el permiso se guarda cifrado. Solo lee, salvo que pulses **Permitir que también envíe**. Si Microsoft deja de renovar el permiso, la tarjeta dice **Requiere volver a conectar** y te llega un aviso.
- **Cualquier otro buzón por IMAP**, con servidor, puerto (993 por omisión), usuario y **contraseña de aplicación** (una contraseña aparte que crea tu proveedor, no la de tu correo). La conexión usa TLS con el certificado validado. La contraseña se guarda cifrada y no vuelve a salir por la API, los eventos ni los registros. Con un servidor de salida (SMTP, puerto 465 por omisión) también puede pedir facturas. **Probar** inicia sesión sin enviar nada.

Qué hace el correo:

- **Buscar la factura aunque no se conozca el folio fiscal.** Se revisan los mensajes con XML, PDF o ZIP desde la fecha del ticket y solo se acepta lo que pasa el cotejo de siempre. Con más de un candidato no se elige y se te avisa. La búsqueda se repite cada 2 minutos la primera media hora, cada 10 minutos hasta las 6 horas y cada hora hasta las 72 horas, y sobrevive a los reinicios.
- **Leer el correo de alta** de un portal, con el remitente del dominio del portal y el mensaje autenticado por el proveedor.
- **Pedir la factura por correo** al comercio, con el correo de facturación leído del ticket (puedes corregirlo en **Revisar**). Sale desde el servidor SMTP, desde Gmail o desde Outlook con permiso de envío (en ese orden si hay varios), con un mensaje fijo, una referencia del expediente y el ticket adjunto. Solo se envía con tu confirmación en ese ticket o con el permiso **Pedir por correo**, una vez por expediente y registrado antes de salir.

### WhatsApp

Todo funciona sobre WhatsApp Web dentro del navegador de FACTURAI (`web.whatsapp.com`).

- **Estado del vínculo.** Se distingue si está listo, pide el código QR, la sesión se cerró, está cargando o no hay conexión. Si no está vinculado, el envío falla de inmediato con un mensaje claro.
- **Número del comercio.** Puede venir del ticket (se lee y se guarda validado en el expediente, y lo puedes corregir en **Revisar**). Antes de enviar se comprueba que el número que muestra el chat sea el correcto.
- **Pedir sin confirmar cada ticket.** Con el permiso **Pedir por WhatsApp** la solicitud sale sola, con las mismas salvaguardas: número verificado, un envío por expediente, registro previo y nunca con un trámite en el navegador.
- **Contestar al robot del comercio.** Con **Contestar al comercio por mí**, cada mensaje del comercio se clasifica en una lista cerrada: nombre, RFC, código postal, régimen, uso de CFDI, correo, folio, fecha, importe, forma de pago, opción de menú, esperar, factura entregada, o «requiere a la persona». La respuesta la arma el programa con los datos del expediente; el texto del comercio nunca se obedece. Máximo 12 respuestas por conversación y 2 por cada dato. Lo que sale de la lista (contraseñas, códigos, datos bancarios, domicilio, identificaciones) se detiene y te avisa.
- **Completar un envío incierto.** Si el envío quedó a medias, **Completar envío** revisa el chat y manda solo lo que falta, una sola vez.
- **Varios archivos.** Baja varios archivos de una misma burbuja y carga más historial del chat.
- **Mis fotos.** Con el permiso **Leer mis fotos de WhatsApp** y tu número en el perfil, las fotos y PDF que te mandas a tu propio chat entran como tickets, una sola vez. Solo se abre ese chat. Estas fotos siguen su camino sin depender del interruptor **Procesar al subir**.

### Cuando el comercio no tiene portal

Si un ticket no tiene portal (ni catálogo, ni liga impresa, ni directorio, ni búsqueda verificada) pero trae un correo o un WhatsApp de facturación, queda como **solicitud directa** en vez de pedir un portal. Con el permiso del canal y el canal disponible, la cola la pide sola: primero por correo y, si no salió, por WhatsApp. Sin permiso o sin canal, el ticket espera con un mensaje que dice qué hacer. Nunca se pide si el ticket tiene portal, nunca dos veces y nunca para un ticket anterior al permiso o a este arranque: encender un permiso no manda tickets viejos. Un error de envío se registra una vez y no se reintenta.

### Avisos

Para que no tengas que vigilar el panel, FACTURAI te avisa cuando algo cambia. Cada aviso es corto, sin datos personales, y lleva una liga al ticket.

- **Campana** en el panel, con la lista de avisos y los que no has leído.
- **Avisos del navegador y del celular** (Web Push). Se activan por dispositivo en **Ajustes → Avisos**. Usan llaves propias que se generan en tu instalación.
- **Correo a tu propia dirección.** Viene apagado. Necesita una cuenta de correo que pueda enviar, y el destino puede ser la cuenta conectada o el correo de tu perfil.

Se avisa una sola vez por cada cambio: un ticket espera algo de ti (con su motivo), una pregunta abierta en el chat, una factura lista, un ticket detenido, WhatsApp desvinculado o que pide atención, o varias facturas por correo que podrían ser de un ticket. Cada tipo y cada canal se enciende o apaga en **Ajustes → Avisos**. Un fallo al avisar nunca frena el trámite.

El panel trae manifiesto e iconos para instalarlo como aplicación con la opción de instalar o «Agregar a inicio» de tu navegador. Para recibir avisos en el celular hace falta:

1. Abrir el panel con una dirección segura (`https`) o en el mismo equipo; por eso necesitas el [modo de varios usuarios](#dos-modos-de-uso) con su dominio, o un acceso seguro propio.
2. En iPhone y iPad, iOS 16.4 o posterior y el panel instalado en la pantalla de inicio. Abre FACTURAI desde ese icono y pulsa **Activar avisos en este dispositivo**.
3. Que el equipo donde corre FACTURAI pueda comunicarse con el servicio de avisos de tu navegador (Google, Mozilla, Apple o Microsoft).

### Inteligencia artificial

En **Ajustes → Inteligencia artificial** eliges qué IA lee tus tickets y llena los portales:

| Proveedor | Cómo se conecta |
| --- | --- |
| Plan de ChatGPT (por omisión) | OAuth con tu cuenta, usa la cuota del plan. Solo en el modo local. |
| OpenAI por llave de pago por uso | Pegas tu llave de API en el panel. |
| Claude de Anthropic por llave de pago por uso | Pegas tu llave de API en el panel. |

- **La llave** se escribe en el panel, se guarda cifrada y solo se ven sus últimos 4 caracteres. No vuelve a salir por la API. Si el servidor la fija por variable de entorno (`FACTURAI_OPENAI_API_KEY` o `FACTURAI_ANTHROPIC_API_KEY`), esa manda y el panel no la puede cambiar.
- **Modelo por nivel.** Puedes elegir un modelo para cada nivel: rápido, normal y difícil.
- **Uso.** Cada llamada deja un registro (tarea, ticket, proveedor, modelo, nivel, duración y tokens) sin guardar el contenido. El panel lo muestra por periodo.
- **Precios.** El costo es un estimado calculado con una tabla de precios que tú editas en el panel (dólares por millón de tokens, de entrada y de salida, por proveedor y modelo). Si un modelo no tiene precio se muestra «sin precio»; el programa no inventa ninguno.

### Medición

La pantalla **Medición** (periodos: hoy, 7 días, 30 días o todo) muestra tickets subidos y facturados, el porcentaje sin intervención, la mediana de lectura del ticket y de tiempo en el portal, el promedio de llamadas a la IA, el costo por ticket y los portales que más fallan, con una tabla de intentos y éxitos por portal. En cada ticket se ve su propia medición. Lo que no se puede medir (por ejemplo en tickets anteriores) dice «sin dato», nunca cero. No incluye RFC, nombres ni importes.

**Sin intervención** significa que el ticket terminó con la factura comprobada y que en toda su historia no hubo ninguna pausa para ti (verificación, inicio de sesión, aprobar la emisión, modo manual o una pregunta en el chat), ni tomaste el control del navegador, ni corregiste algo a mano (por ejemplo la forma de pago).

## Conexiones y datos

- **ChatGPT** usa OAuth de aplicación local y la cuota del plan autorizado. La conexión usa HTTPS con OpenAI. Cuando la cuenta informa un límite, conserva los tickets y permite continuar al recuperar cupo. En [Ajustes de uso de ChatGPT](https://chatgpt.com/#settings/usage) puedes consultar los límites y si autorizaste créditos. El panel permite desconectar el plan.
- **Gmail.** Habilita Gmail API en el proyecto de Google Cloud y crea un cliente OAuth de tipo **Aplicación de escritorio**. Si el proyecto está en modo de prueba, añade tu cuenta a los usuarios de prueba. En la aplicación personal, abre **Ajustes → Gmail**, importa el JSON del cliente y pulsa **Conectar Gmail**. En Google Chrome de tu equipo, elige el buzón que usará FACTURAI y concede lectura. Al volver al panel, FACTURAI comprueba que Gmail responde antes de guardar la conexión. Para recuperar comprobantes puedes buscar por remitente, fecha o UUID. [Guía de Google para aplicaciones de escritorio](https://developers.google.com/identity/protocols/oauth2/native-app?hl=es)
- **Gmail por el túnel de Azure.** Mantén el lanzador y el túnel activos hasta volver a Ajustes. Google devuelve la autorización a `127.0.0.1:8765` en el mismo equipo donde abriste Chrome. Una instalación de prueba iniciada con `--port 18765` recibe la respuesta en `127.0.0.1:18765`. Con un túnel, el puerto local de la app y el remoto deben coincidir. Un rechazo o un intento vencido se puede volver a iniciar desde Ajustes. Para cambiar de cliente OAuth con una cuenta conectada, primero usa **Desvincular**.
- **Outlook, Hotmail y Microsoft 365.** Hay que registrar una vez, gratis, una aplicación propia en Microsoft (pide una cuenta de Azure):
  1. Entra a [entra.microsoft.com](https://entra.microsoft.com) y abre **Entra ID → Registros de aplicaciones → Nuevo registro**. Ponle un nombre, por ejemplo FACTURAI.
  2. En **Tipos de cuenta admitidos** elige **Cualquier inquilino de Entra ID + cuentas personales de Microsoft** y pulsa **Registrar**.
  3. En **Autenticación → Agregar URI de redirección → Aplicaciones móviles y de escritorio** escribe `http://localhost:8765/auth/outlook/callback` (con el puerto de tu panel; Ajustes la muestra lista para copiar) y pulsa **Configurar**. No se crea ningún secreto.
  4. En **Información general** copia el **Identificador de aplicación (cliente)**, pégalo en **Ajustes → Correo → Outlook / Hotmail** y pulsa **Conectar Outlook**.
  5. En tu correo de Outlook activa **Configuración → Correo → Reenvío e IMAP → Permitir que los dispositivos y las aplicaciones usen IMAP**: viene apagado.

  Microsoft abre su página en otra pestaña; al volver, FACTURAI abre el buzón antes de guardar la conexión. La respuesta regresa a `localhost` con el puerto del panel: con un túnel, el puerto local y el remoto deben coincidir, y la pestaña que regresa dice `localhost` aunque hayas abierto el panel en `127.0.0.1` (es el mismo panel). En una cuenta de empresa puede hacer falta que su administrador apruebe la aplicación y que tenga encendido el envío por SMTP. Estos pasos salen de la documentación de Microsoft y de pruebas con servicios simulados: todavía no se probaron contra Microsoft ni contra un buzón real.
- **Otros buzones.** Cualquier otro proveedor (Yahoo, el de tu empresa, tu propio dominio) se conecta por IMAP con contraseña de aplicación, como se explica en [Correo](#correo).
- **Historial previo** admite el `historial_sesion.json` del facturador anterior. Los expedientes y originales siguen en el volumen privado. Puedes exportar el registro a CSV.

El modo local es una instancia privada para una persona. Con el modo de varios usuarios, cada cliente tiene su propio espacio, pero la conexión del plan de ChatGPT y la de Gmail por OAuth no se ofrecen en ese modo (regresan a `127.0.0.1`): ahí la IA se conecta con una llave de pago por uso, o la fija el servidor, y el correo se conecta con contraseña de aplicación o, si quien instala registró la aplicación de Microsoft, con **Conectar Outlook**. La modalidad comercial alojada con el plan de ChatGPT de cada cliente depende del [programa de integración de OpenAI](https://developers.openai.com/siwc/request-client-id). Los portales del catálogo tienen guías de navegación. Su acceso y las emisiones en vivo dependen del portal y de la sesión activa.

## Dos modos de uso

**Local** (por omisión, `FACTURADOR_MODO=local`): una persona, sin cuentas, panel en `127.0.0.1`. No hace falta configurar nada. La llave maestra es opcional.

**Varios usuarios** (`FACTURADOR_MODO=usuarios`): una puerta de entrada maneja cuentas y sesiones y entrega cada petición a la instancia de ese cliente, con su propia carpeta de datos, su perfil, su navegador y sus conexiones. Las instancias arrancan al primer uso y se cierran en reposo solo si no tienen trámites ni tareas pendientes. Las contraseñas se guardan con scrypt, la sesión viaja en una cookie `__Host-` que el navegador manda solo por `https`, hay freno a los intentos de adivinar contraseñas y las invitaciones son de un solo uso. El administrador gestiona cuentas e invitaciones, pero no ve los datos de un cliente ni entra como él. `FACTURADOR_TICKETS_A_LA_VEZ` no aplica en este modo: cada cuenta trabaja un ticket a la vez. Lo único que las cuentas comparten es lo aprendido de los portales de los comercios, sin datos de ninguna persona.

Cómo activarlo (los mismos pasos están comentados en `compose.yaml`, apagados por omisión):

1. Crea la llave maestra **fuera** de la carpeta de datos y del repositorio. Con ella se cifran las llaves de las contraseñas de portales y del correo, las de la IA, la llave privada de los avisos y las conexiones de ChatGPT, Gmail y Outlook. Si falta o es otra, el programa no arranca y nada de lo guardado se toca. Guarda una copia en otro lugar seguro.

   ```bash
   python -m facturador crear-llave ../facturai-llave/llave-maestra.key
   ```

   En Docker, `compose.yaml` explica cómo crearla con `openssl` y montarla como secreto. Otra forma es dar la llave misma (32 bytes en base64) en `FACTURADOR_LLAVE_MAESTRA`; usa solo una de las dos variables. Lo que ya estaba guardado sin cifrar se cifra solo la primera vez que se lee.
2. Quita `#u ` al inicio de cada línea marcada en `compose.yaml` (incluye el servicio `proxy`, un proxy con TLS) y pon tu dominio ahí y en `docker/Caddyfile.ejemplo`. Con varios navegadores a la vez conviene subir `shm_size`. El único servicio que abre puertos hacia fuera es el proxy (80 y 443); el puerto de FACTURAI sigue publicado solo en `127.0.0.1`.
3. Configura las variables:

   | Variable | Qué hace |
   | --- | --- |
   | `FACTURADOR_MODO` | `usuarios` activa el modo. |
   | `FACTURADOR_ORIGEN_PUBLICO` | Dirección `https://…` por la que se entra, sin ruta. Sin ella no arranca. Solo para pruebas se admite `http://localhost`. |
   | `FACTURADOR_ARCHIVO_LLAVE` o `FACTURADOR_LLAVE_MAESTRA` | La llave maestra. Obligatoria en este modo. |
   | `FACTURADOR_PROXIES` | Direcciones o redes del proxy. Solo de ellas se acepta la dirección real de quien entra. |
   | `FACTURADOR_NAVEGADORES_MAX` | Navegadores abiertos a la vez entre todos los clientes (3 por omisión, de 1 a 50). El ticket que no alcanza espera su turno. |
   | `FACTURADOR_INSTANCIA_INACTIVA_MIN` | Minutos sin uso para cerrar el espacio de un cliente sin trabajo pendiente (30 por omisión, de 1 a 1440). |
   | `FACTURADOR_OUTLOOK_APLICACION` | Opcional. Identificador de aplicación (cliente) de Microsoft para toda la instalación: con él cada persona conecta su Outlook o Hotmail con solo pulsar **Conectar Outlook**. Se registra como en [Conexiones y datos](#conexiones-y-datos), con la dirección de regreso `https://tu-dominio/auth/outlook/callback`. Sin ella, Outlook no se ofrece en este modo. |
   | `FACTURADOR_COMPARTIDO` | Con `0` las cuentas no comparten lo que aprenden de los portales. Vacío: lo comparten, sin datos de nadie (ver [Cómo resuelve un portal](#cómo-resuelve-un-portal)). |

4. Arranca y crea el primer administrador desde la terminal. La contraseña (mínimo 12 caracteres) se escribe con el teclado; nunca se pasa por argumento ni por variable.

   ```bash
   docker compose up -d --build
   docker compose exec facturador python -m facturador crear-admin
   ```

5. Entra en `/entrar`. En **Mi cuenta** (`/cuenta`) cambias tu contraseña; el administrador ve ahí a las personas con acceso, crea invitaciones de un solo uso (duran 72 horas por omisión, de 1 a 336 horas) y quita accesos. Cada persona acepta su invitación en `/invitacion` y elige su usuario y contraseña. Los avisos usan el origen público.

## Lo que FACTURAI no hace

- **No resuelve el «no soy un robot».** Siempre lo completa una persona en la vista en vivo. FACTURAI no lo automatiza ni lo rodea.
- **No inventa datos.** Si un portal pide algo que no dejaste (por ejemplo la fecha de nacimiento), el ticket espera y dice qué agregar.
- **Los códigos que no puede contestar se quedan contigo:** los de segundo factor o recuperación y los códigos de seguridad que pide un robot de WhatsApp. El código que un portal manda al teléfono durante el alta o el acceso sí te lo pide por el chat, sin guardarlo (ver [Registro en portales](#registro-en-portales)).
- **Outlook y Hotmail no se conectan con contraseña, y no hay «Outlook con un clic».** Microsoft exige entrar con la cuenta, y para eso hay que registrar antes una aplicación propia en Microsoft (gratis, una sola vez). En el modo de varios usuarios la registra quien instala; sin ella, Outlook no se ofrece.
- **No hay «Gmail con un clic».** Conectar Gmail exige crear en Google Cloud un cliente OAuth de escritorio e importar su JSON.
- **Nunca emite si la página no muestra** el RFC del perfil, el folio o código del ticket y su importe, y la factura recibida siempre se coteja contra el perfil y el ticket.
- **WhatsApp, correo y la IA de pago por uso se probaron con dobles, no contra los servicios reales.** Las pruebas usan una página que imita WhatsApp Web, servidores IMAP y SMTP locales con TLS, un servidor local que imita a Gmail, otro que imita el inicio de sesión de Microsoft (con IMAP y SMTP que solo aceptan sus permisos) y respuestas simuladas de las APIs de OpenAI y Anthropic. Ninguna prueba toca WhatsApp, un buzón, Gmail, Microsoft, un portal de comercio ni una IA de verdad. Conecta tus cuentas y prueba con un ticket tuyo antes de depender de ellas.

## Verificación

```bash
python -m unittest discover -s tests -v
node --check web/app.js
node --check web/visor.js
node --check web/herramientas.js
node --check web/seguimiento.js
node --check web/subida.js
node --check web/avance.js
node --check web/chat.js
node --check web/correo.js
node --check web/ia.js
node --check web/medicion.js
node --check web/avisos.js
node --check web/sw.js
node --test tests/seguimiento-panel.test.mjs
docker compose config
```

También hay otros `.js` en `web/` (`cuentas-portal.js`, `correo-portal.js`, `navegacion.js` y los de la puerta de entrada: `entrar.js`, `cuenta.js`, `panel-acceso.js`) que se revisan igual con `node --check`.

Las pruebas son de tres tipos: unitarias (`tests/test_*.py`), de integración con navegador, que usan tickets, comercios y páginas simuladas (`tests/smoke_*.py`), y pruebas de navegador sobre el panel. Varias abren Chromium con Playwright, así que la suite completa no es instantánea: tarda varios minutos. No se midió una duración exacta. El flujo de GitHub Actions archivado en `.github/workflows-desactivados/` le daba 20 minutos a todo el trabajo, incluidos los guiones de Docker. El banco de portales se corre aparte con `python -m facturador.banco`.

La conexión con una cuenta real se verifica desde **Ajustes** con autorización de su titular. Las facturas de ejemplo se usan para revisión sin abrir un portal comercial.

El perfil de seccomp del contenedor proviene de [Playwright 1.64.1](https://github.com/microsoft/playwright/blob/v1.64.1/utils/docker/seccomp_profile.json). Su licencia está en `docker/LICENSE-playwright.txt`.
