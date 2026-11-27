"""Every WhatsApp Web selector and visible text in one place, with ordered fallbacks.

Each element has a list of alternatives tried in order (aria, data-testid, icons,
Spanish and English texts). When none appears the error names the element, never
the chat content, so a change in WhatsApp Web is easy to locate.
"""
from __future__ import annotations

import asyncio
import hashlib
import re
import time
from dataclasses import dataclass
from urllib.parse import urlencode, urlsplit

from playwright.async_api import Error as BrowserError

ORIGEN = ('https', 'web.whatsapp.com')
# File names shown in a bubble (only used to tell that it carries documents).
ARCHIVO = re.compile(r'[\w().,\-]+\.(pdf|xml|zip)\b', re.I)


@dataclass(frozen=True)
class Elemento:
    descripcion: str
    # ('css', selector) | ('role', role, pattern) | ('text', pattern) | ('label', pattern)
    alternativas: tuple


ELEMENTOS: dict[str, Elemento] = {
    'listo': Elemento('la lista de chats', (
        ('css', '#pane-side'), ('css', '[data-testid="chat-list"]'),
        ('role', 'grid', r'^(Lista de chats|Chat list)$'), ('css', '#side'), ('css', '#main'))),
    'qr': Elemento('el código QR para vincular', (
        ('css', 'canvas[aria-label*="scan" i]'), ('css', 'canvas[aria-label*="escanea" i]'),
        ('css', '[data-ref] canvas'), ('css', '[data-testid="qrcode"]'),
        ('text', r'^(Usa WhatsApp en tu (computadora|ordenador)|Use WhatsApp on your computer|'
                 r'Escanea el código QR|Scan the QR code|Pasos para iniciar sesión|Steps to log in)'))),
    'sesion_cerrada': Elemento('el aviso de sesión cerrada', (
        ('css', '[data-testid="logged-out"]'),
        ('text', r'(Tu sesión se cerró|Se cerró (tu|la) sesión|Cerraste (la )?sesión|Sesión cerrada|'
                 r'Dispositivo desvinculado|You(\'ve| have) been logged out|Logged out|Device (was )?unlinked)'))),
    'cargando': Elemento('la pantalla de carga', (
        ('css', 'progress'), ('css', '[data-testid="startup-progress"]'), ('css', '#startup'),
        ('css', '[data-testid="intro-md-beta-logo-dark"], [data-testid="intro-md-beta-logo-light"]'),
        ('text', r'^(Cargando( tus)? (chats|mensajes)|Loading( your)? (chats|messages)|Cargando|Loading)'))),
    'sin_conexion': Elemento('el aviso de conexión', (
        ('css', '[data-icon="alert-computer"]'), ('css', '[data-icon="alert-phone"]'),
        ('css', '[data-testid="alert-computer"], [data-testid="alert-phone"]'),
        ('text', r'(Computadora no conectada|Computer not connected|Teléfono no conectado|Phone not connected|'
                 r'Sin conexión a internet|No internet connection|Revisa tu conexión a internet)'))),
    'otra_ventana': Elemento('el aviso de WhatsApp abierto en otra ventana', (
        ('text', r'(WhatsApp está abierto en otra ventana|WhatsApp is open in another window)'),)),
    'usar_aqui': Elemento('el botón «Usar aquí»', (
        ('role', 'button', r'^(Usar aquí|Use here)$'), ('text', r'^(Usar aquí|Use here)$'))),
    'numero_invalido': Elemento('el aviso de número inválido', (
        ('text', r'(número de teléfono compartido a través de (la )?(dirección )?URL no es válido|'
                 r'Phone number shared via url is invalid|no está en WhatsApp|isn\'t on WhatsApp)'),)),
    'encabezado': Elemento('el encabezado del chat', (
        ('css', '#main header'), ('css', '[data-testid="conversation-header"]'))),
    'titulo_contacto': Elemento('el nombre del contacto', (
        ('css', 'span[title]'), ('css', '[data-testid="conversation-info-header-chat-title"]'))),
    'ficha_contacto': Elemento('la ficha del contacto', (
        ('css', '[data-testid="contact-info-drawer"]'), ('css', '[data-testid="drawer-right"]'),
        ('css', '[data-testid="drawer-right-inner"]'), ('label', r'^(Contact info|Info\. del contacto)$'),
        ('css', '[role="dialog"]'))),
    'editor': Elemento('la caja para escribir', (
        ('css', '#main footer [contenteditable="true"]'), ('css', '#main footer [role="textbox"]'),
        ('css', '[data-testid="conversation-compose-box-input"]'),
        ('label', r'^(Escribe un mensaje|Type a message)$'))),
    'adjuntar': Elemento('el botón para adjuntar', (
        ('role', 'button', r'^(Adjuntar|Attach)( archivos?| files?)?$'),
        ('css', '#main footer [data-icon="plus-rounded"]'), ('css', '#main footer [data-icon="plus"]'),
        ('css', '#main footer [data-icon="attach-menu-plus"]'), ('css', '#main footer [data-icon="clip"]'),
        ('css', '#main footer [title="Adjuntar"], #main footer [title="Attach"]'))),
    'documento': Elemento('la opción «Documento» del menú', (
        ('text', r'^(Documento|Document)$'), ('role', 'menuitem', r'^(Documento|Document)'),
        ('css', '[data-icon="document-filled"]'), ('css', '[data-icon="attach-document"]'))),
    'enviar_adjunto': Elemento('el botón para enviar adjuntos', (
        ('role', 'button', r'^(Enviar|Send)( \(Enter\))?$'), ('css', '[data-icon="send"]'),
        ('css', '[data-icon="wds-ic-send-filled"]'), ('css', '[aria-label="Enviar"], [aria-label="Send"]'))),
    'burbuja': Elemento('los mensajes del chat', (
        ('css', '#main .message-in, #main .message-out'),
        ('css', '#main div[data-id^="false_"], #main div[data-id^="true_"]'))),
    'descargar': Elemento('el botón de descarga del mensaje', (
        ('role', 'button', r'Descargar|Download'), ('css', 'a[download]'),
        ('css', '[data-icon="download"], [data-icon="document-download"], [data-icon="audio-download"]'),
        ('css', '[title^="Descargar"], [title^="Download"]'))),
    'imagen': Elemento('la foto del mensaje', (
        ('css', '[data-testid="image-thumb"]'), ('label', r'^(Abrir (imagen|foto)|Open (picture|photo|image))'),
        ('css', 'img[src^="blob:"]'))),
    'visor_descargar': Elemento('el botón de descarga del visor de fotos', (
        ('role', 'button', r'^(Descargar|Download)$'), ('css', '[data-testid="media-viewer"] [data-icon="download"]'),
        ('css', '[title="Descargar"], [title="Download"]'))),
}

ESTADOS = {
    'listo': 'WhatsApp está vinculado y listo.',
    'qr': 'WhatsApp pide vincular de nuevo: abre WhatsApp en el navegador y escanea el código QR con tu celular.',
    'sesion_cerrada': 'La sesión de WhatsApp se cerró o se desvinculó. Vuelve a vincularla con el código QR.',
    'cargando': 'WhatsApp todavía está cargando. Intenta de nuevo en un momento.',
    'sin_conexion': 'WhatsApp no tiene conexión. Revisa el internet de la computadora y del celular.',
    'otra_ventana': 'WhatsApp está abierto en otra ventana. Pulsa «Usar aquí» en el navegador.',
    'sin_abrir': 'WhatsApp no está abierto en el navegador.',
}


class ElementoNoEncontrado(ValueError):
    def __init__(self, nombre: str):
        self.nombre = nombre
        super().__init__(f'WhatsApp Web no mostró {ELEMENTOS[nombre].descripcion} [{nombre}]. '
                         'Puede que haya cambiado su página; revisa el chat en el navegador.')


class SinVincular(ValueError):
    def __init__(self, estado: str):
        self.estado = estado
        super().__init__(ESTADOS[estado])


def localizar(scope, alternativa):
    kind = alternativa[0]
    if kind == 'css':
        return scope.locator(alternativa[1])
    if kind == 'role':
        return scope.get_by_role(alternativa[1], name=re.compile(alternativa[2], re.I))
    if kind == 'text':
        return scope.get_by_text(re.compile(alternativa[1], re.I))
    if kind == 'label':
        return scope.get_by_label(re.compile(alternativa[1], re.I))
    raise AssertionError('Unknown selector kind')


async def buscar(scope, nombre: str, *, visible=True, unico=False, textos=True):
    """First alternative with matches (exactly one when `unico`), or None. Never waits.

    `textos=False` skips text alternatives, so words inside chat bubbles cannot match."""
    for alternativa in ELEMENTOS[nombre].alternativas:
        if not textos and alternativa[0] == 'text':
            continue
        found = localizar(scope, alternativa)
        if visible:
            found = found.filter(visible=True)
        try:
            count = await found.count()
        except BrowserError:
            continue
        if count == 1 or (count and not unico):
            return found if unico else found.first
    return None


async def todos(scope, nombre: str, *, visible=True):
    """Every match of the first alternative that has any, so nested icons are not counted twice."""
    for alternativa in ELEMENTOS[nombre].alternativas:
        found = localizar(scope, alternativa)
        if visible:
            found = found.filter(visible=True)
        try:
            if await found.count():
                return await found.all()
        except BrowserError:
            continue
    return []


async def esperar(scope, nombre: str, *, timeout=6000, visible=True, unico=False):
    limit = time.monotonic() + timeout / 1000
    while True:
        found = await buscar(scope, nombre, visible=visible, unico=unico)
        if found is not None:
            return found
        if time.monotonic() >= limit:
            raise ElementoNoEncontrado(nombre)
        await asyncio.sleep(.2)


async def estado_vinculo(page) -> str:
    """listo, qr, sesion_cerrada, cargando, sin_conexion, otra_ventana or sin_abrir."""
    try:
        if page is None or page.is_closed() or (urlsplit(page.url).scheme, urlsplit(page.url).netloc) != ORIGEN:
            return 'sin_abrir'
        online = await page.evaluate('navigator.onLine')
    except BrowserError:
        return 'sin_abrir'
    if await buscar(page, 'otra_ventana'):
        return 'otra_ventana'
    if await buscar(page, 'listo'):
        # With the chat list on screen, only icons count: bubble texts must not change the state.
        if online is False or await buscar(page, 'sin_conexion', textos=False):
            return 'sin_conexion'
        return 'listo'
    for estado in ('sesion_cerrada', 'qr'):
        if await buscar(page, estado):
            return estado
    if online is False or await buscar(page, 'sin_conexion'):
        return 'sin_conexion'
    return 'cargando'


async def esperar_chat(page, *, timeout=18_000, usar_aqui=True):
    """Wait for the compose box, failing as soon as the session is clearly not usable."""
    limit = time.monotonic() + timeout / 1000
    offline_since = None
    while True:
        editor = await buscar(page, 'editor')
        if editor is not None and not await buscar(page, 'sin_conexion', textos=False):
            return editor
        if await buscar(page, 'numero_invalido'):
            raise ValueError('WhatsApp indica que ese número no es válido o no usa WhatsApp.')
        estado = await estado_vinculo(page)
        if estado in {'qr', 'sesion_cerrada', 'sin_abrir'}:
            raise SinVincular(estado)
        if estado == 'otra_ventana':
            button = await buscar(page, 'usar_aqui', unico=True) if usar_aqui else None
            if button is None:
                raise SinVincular(estado)
            await button.click()
            usar_aqui = False
        if estado == 'sin_conexion':
            offline_since = offline_since or time.monotonic()
            if time.monotonic() - offline_since > 3:
                raise SinVincular(estado)
        else:
            offline_since = None
        if time.monotonic() >= limit:
            if estado == 'cargando':
                raise SinVincular('cargando')
            raise ElementoNoEncontrado('editor')
        await asyncio.sleep(.25)


# One function per operation -------------------------------------------------------------

async def abrir_chat(page, phone: str, *, timeout=18_000):
    await page.goto('https://web.whatsapp.com/send?' + urlencode({'phone': phone}),
                    wait_until='domcontentloaded', timeout=30_000)
    editor = await esperar_chat(page, timeout=timeout)
    if (urlsplit(page.url).scheme, urlsplit(page.url).netloc) != ORIGEN:
        raise ValueError('El chat salió del dominio de WhatsApp.')
    return editor


async def leer_encabezado(page) -> str:
    header = await esperar(page, 'encabezado', timeout=4000)
    return await header.inner_text()


async def leer_ficha(page) -> str:
    """Open the contact card from the header and read it; always closes it again."""
    header = await esperar(page, 'encabezado', timeout=4000)
    title = await buscar(header, 'titulo_contacto', visible=False) or header
    await title.click(timeout=4500)
    try:
        drawer = await esperar(page, 'ficha_contacto', timeout=4000)
        return await drawer.inner_text()
    finally:
        await page.keyboard.press('Escape')


async def borrador(editor) -> str:
    return (await editor.inner_text()).strip()


async def escribir(editor, text: str):
    await editor.fill(text)


async def enviar_texto(editor):
    await editor.press('Enter')


async def adjuntar(page, pending: list[dict]) -> list[dict]:
    attach = await esperar(page, 'adjuntar', timeout=4000, unico=True)
    await attach.click()
    document = await esperar(page, 'documento', timeout=4000)
    async with page.expect_file_chooser(timeout=6000) as waiting:
        await document.click()
    chooser = await waiting.value
    selected = pending[:] if chooser.is_multiple() else pending[:1]
    await chooser.set_files(selected)
    send = await esperar(page, 'enviar_adjunto', timeout=6000, unico=True)
    await send.click()
    return selected


@dataclass
class Burbuja:
    indice: int
    entrante: bool
    texto: str
    ident: str
    archivos: list[str]
    locator: object

    @property
    def clave(self) -> str:
        return self.ident or hashlib.sha256(f'{self.indice}\0{self.texto}'.encode()).hexdigest()[:24]


async def burbujas(page) -> list[Burbuja]:
    for alternativa in ELEMENTOS['burbuja'].alternativas:
        found = localizar(page, alternativa)
        try:
            rows = await found.evaluate_all("""els => els.map(e => ({
                cls: String(e.className || ''),
                id: e.getAttribute('data-id') || (e.closest('[data-id]') && e.closest('[data-id]').getAttribute('data-id')) || '',
                text: e.innerText || ''}))""")
        except BrowserError:
            continue
        if rows:
            result = []
            for index, row in enumerate(rows):
                entrante = 'message-in' in row['cls'].split() or row['id'].startswith('false_')
                names = [m.group(0).strip() for m in ARCHIVO.finditer(row['text'])]
                result.append(Burbuja(index, entrante, row['text'], row['id'], names, found.nth(index)))
            return result
    return []


async def esperar_saliente(page, text: str, before: int, *, timeout=10_000) -> bool:
    """True once a new outgoing bubble carrying `text` appears after `before` outgoing ones."""
    limit = time.monotonic() + timeout / 1000
    wanted = ' '.join(text.split())
    while time.monotonic() < limit:
        out = [b for b in await burbujas(page) if not b.entrante]
        if len(out) > before and any(wanted in ' '.join(b.texto.split()) for b in out[before:]):
            return True
        await asyncio.sleep(.25)
    return False


async def descargar(page, control, *, timeout=12_000):
    async with page.expect_download(timeout=timeout) as waiting:
        await control.click()
    return await waiting.value


async def mas_historial(page, contiene: str, *, intentos=15) -> bool:
    """Scroll the conversation up until a bubble contains `contiene`. Bounded by `intentos`."""
    stale = 0
    for _ in range(intentos + 1):
        rows = await burbujas(page)
        if any(contiene in b.texto for b in rows):
            return True
        if not rows or stale >= 2:
            return False
        before = len(rows)
        await rows[0].locator.evaluate("""el => {
            let node = el.parentElement;
            while (node && !(node.scrollHeight > node.clientHeight + 2 &&
                   ['auto', 'scroll'].includes(getComputedStyle(node).overflowY))) node = node.parentElement;
            (node || document.scrollingElement).scrollTop = 0;
            el.scrollIntoView({block: 'start'});
        }""")
        await asyncio.sleep(.6)
        stale = stale + 1 if len(await burbujas(page)) == before else 0
    return any(contiene in b.texto for b in await burbujas(page))


async def descargar_foto(page, bubble, *, timeout=15_000):
    """Open a photo bubble in the viewer, download the full image and close the viewer."""
    thumb = await buscar(bubble.locator, 'imagen')
    if thumb is None:
        return None
    await thumb.click()
    try:
        button = await esperar(page, 'visor_descargar', timeout=6000, unico=True)
        return await descargar(page, button, timeout=timeout)
    finally:
        await page.keyboard.press('Escape')
