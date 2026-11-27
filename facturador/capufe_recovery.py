"""Recover a CAPUFE invoice through the public, anonymous toll-code lookup."""
import asyncio
import base64
import binascii
import json
import os
import re
import time
import unicodedata
from contextlib import asynccontextmanager
from urllib.parse import parse_qs, urlsplit

from playwright.async_api import Error as BrowserError, TimeoutError as BrowserTimeout, async_playwright

from .auth import read_json, write_private
from .catalog import CATALOG
from .documents import save_candidate
from .read_http import ServiceWait, retry_seconds


HOST = 'facturacioncapufe.com.mx'
LOOKUP = '/capufe-quadrum-backend/sinregistro/documentos/descargar_codigo_alfanumerico.json'
MAX_RESPONSE = 24 * 1024 * 1024


class LookupProblem(ValueError):
    """Only messages constructed here may appear in the ticket's activity."""


def _is_lookup(request, code):
    url = urlsplit(request.url)
    return (request.method == 'POST' and url.scheme == 'https' and url.netloc == HOST
            and url.path == LOOKUP and parse_qs(url.query, keep_blank_values=True) == {'codigo': [code]}
            and not request.post_data)


def _unaccent(value):
    return ''.join(c for c in unicodedata.normalize('NFD', value.casefold()) if not unicodedata.combining(c))


def _payload(data):
    if data.startswith(b'PK\x03\x04'):
        return {'zip': data}, False, ''
    if data.startswith(b'%PDF-'):
        return {'pdf': data}, False, ''
    if data.lstrip(b'\xef\xbb\xbf \t\n\r').startswith((b'<?xml', b'<cfdi:Comprobante')):
        return {'xml': data}, False, ''
    try:
        return _reply(json.loads(data))
    except (ValueError, UnicodeError):
        raise LookupProblem('CAPUFE devolvió un formato de respuesta que no permite comprobar la factura.') from None


def _reply(data):
    files, messages = {}, []
    def visit(value, field=''):
        if isinstance(value, dict):
            for k, v in value.items():
                visit(v, str(k).lower())
        elif isinstance(value, list):
            for v in value:
                visit(v, field)
        elif isinstance(value, str):
            value = value.strip()
            if not value:
                return
            if field in ('mensaje', 'message', 'descripcion', 'detalle', 'error', 'mensajeerror', 'respuesta', '') and len(value) < 300:
                messages.append(' '.join(value.split()))
            if value.startswith(('<?xml', '<cfdi:Comprobante')):
                files['xml'] = value.encode('utf-8')
                return
            # Files in the JSON response are base64. Match their content before keeping them.
            if value.startswith('data:') and ';base64,' in value[:100]:
                value = value.split(',', 1)[1]
            if field not in {'xml','pdf','zip'} and not value.startswith(('JVBERi0','PD94bW','PGNmZG','UEsDB')):
                return
            try:
                raw = base64.b64decode(''.join(value.split()), validate=True)
            except (ValueError, binascii.Error):
                return
            prefix = raw.lstrip(b'\xef\xbb\xbf \r\n\t')
            if raw.startswith(b'%PDF-'):
                files['pdf'] = raw
            elif prefix.startswith((b'<?xml', b'<cfdi:Comprobante')):
                files['xml'] = raw
            elif raw.startswith(b'PK\x03\x04'):
                files['zip'] = raw

    visit(data)
    # An unknown code and a temporary lookup failure are never proof against issuance.
    explicit = re.compile(r'(?:el |este )?codigo(?: alfanumerico| de peaje)? (?:aun |todavia )?no (?:ha sido|esta|se (?:ha|encuentra)) facturado[.!]?')
    negative = not files and bool(messages) and all(explicit.fullmatch(_unaccent(m)) for m in messages)
    # Keep a short provider message for the owner. Do not include addresses, references, or query strings.
    note = next((m for m in messages if not re.search(r'https?://|[@<>{}=]|[\w-]{12,}\d|\d[\w-]{10,}', m)), '')
    return files, negative, note[:180]


async def _lookup(page, code):
    landing = await page.goto(CATALOG['capufe']['url'], wait_until='domcontentloaded', timeout=20000)
    if landing and not landing.ok:
        delay = retry_seconds(landing.headers.get('retry-after'))
        if delay:
            raise ServiceWait(delay)
        raise LookupProblem(f'El portal de CAPUFE respondió HTTP {landing.status} al abrir la recuperación.')
    try:
        await page.get_by_role('link', name=re.compile(r'^Recuperar una factura, por c.digo alfan.m[eé]rico$', re.I)).click(timeout=10000)
    except BrowserTimeout:
        raise LookupProblem('CAPUFE abrió la página, pero no mostró el enlace para recuperar una factura.') from None
    dialog = page.get_by_role('dialog').filter(has_text=re.compile(r'Recuperar Factura por C.digo Alfanum.rico', re.I))
    field = dialog.get_by_role('textbox')
    submit = dialog.get_by_role('button', name=re.compile(r'^(?:check\s+)?recuperar$', re.I))
    if await field.count() != 1 or await submit.count() != 1:
        raise LookupProblem('CAPUFE cambió el formulario de recuperación.')
    used = False
    async def lookup_only(route):
        nonlocal used
        if used or not _is_lookup(route.request, code):
            await route.abort()
            return
        used = True
        response = await route.fetch(max_redirects=0, timeout=20000)
        if 300 <= response.status < 400 or int(response.headers.get('content-length') or 0) > MAX_RESPONSE:
            await route.abort()
            return
        await route.fulfill(response=response)

    # From this moment the browser contains a ticket code. The only outgoing request
    # permitted is the lookup that the public recovery dialog constructs for this code.
    await page.route('**/*', lookup_only)
    if urlsplit(page.url).netloc != HOST:
        raise LookupProblem('Cambió el dominio del portal de recuperación.')
    await field.fill(code)
    async with page.expect_response(lambda r: _is_lookup(r.request, code), timeout=22000) as result:
        await submit.click(timeout=3500)
    response = await result.value
    if not response.ok:
        delay = retry_seconds(response.headers.get('retry-after'))
        if delay:
            raise ServiceWait(delay)
        if response.status not in {400,404,422}:
            raise LookupProblem(f'El servicio de recuperación de CAPUFE respondió HTTP {response.status}.')
    payload = await response.body()
    if len(payload) > MAX_RESPONSE:
        raise LookupProblem('La descarga de CAPUFE supera el tamaño permitido.')
    files, negative, note = _payload(payload)
    return files if response.ok else {}, bool(response.ok and negative), note


# Each code is looked up in a context of its own, with nothing of the engine's profile in it.
CONTEXT = {'service_workers': 'block', 'accept_downloads': False}
ENGINES = {'Chromium', 'Google Chrome'}


async def _engine_browser(runner):
    """The engine's own browser when it can open private contexts beside its profile; else None."""
    if runner is None:
        return None
    try:
        await runner.start_browser()
        browser = getattr(runner.context, 'browser', None) if runner.engine in ENGINES else None
        if browser is not None:
            # Proves it before any code is typed: a browser that refuses is simply not used.
            await (await browser.new_context(**CONTEXT)).close()
        return browser
    except (BrowserError, ValueError, OSError):
        return None


@asynccontextmanager
async def _lookup_browser(runner=None):
    """The browser the lookup opens its contexts in.

    With the engine at hand it is the engine's own browser: the place it holds among the open
    browsers is already counted, and no second browser is started beside it. Without the engine,
    or when its browser cannot give a private context, one is launched for the lookup alone and
    closed with it, as it always was.
    """
    browser = await _engine_browser(runner)
    if browser is not None:
        yield browser
        return
    async with async_playwright() as pw:
        browser = await pw.chromium.launch(
            executable_path=os.getenv('FACTURADOR_CHROMIUM_PATH') or None,
            headless=os.getenv('FACTURADOR_CHROME_HEADED') != '1', chromium_sandbox=True)
        try:
            yield browser
        finally:
            await browser.close()


async def recover_capufe(store, key, receipt, profile, runner=None):
    """``runner`` is the engine that asks; its browser hosts the lookup when it can (see ``_lookup_browser``)."""
    codes = list(dict.fromkeys(re.sub(r'\s+', '', c).upper() for c in receipt.billing_codes))
    if receipt.provider != 'capufe' or not 1 <= len(codes) <= 20 or not all(re.fullmatch('[0-9A-Z]{18}', c) for c in codes):
        return 'La consulta de CAPUFE necesita el código impreso de 18 caracteres de cada peaje.'
    cooldown = store.root / 'capufe-recovery-wait.json'
    until = int(read_json(cooldown, {}).get('until', 0))
    def defer(when):
        store.defer_recovery_until(key, when)
        return f'CAPUFE pidió esperar {max(1, when - int(time.time()))} segundos antes de volver a consultar.'
    if until > time.time():
        return defer(until)
    absent = 0
    received = set()
    last_note = ''
    try:
        async with _lookup_browser(runner) as browser:
            for code in codes:
                context = await browser.new_context(**CONTEXT)
                try:
                    files, not_billed, last_note = await _lookup(await context.new_page(), code)
                    if last_note:
                        store.event(key, 'capufe_lookup', last_note)
                    for kind, data in files.items():
                        await asyncio.to_thread(save_candidate, store, key, 'capufe.' + kind, data)
                        received.add(kind.upper())
                    absent += int(not_billed)
                finally:
                    await context.close()
    except ServiceWait as exc:
        until = int(time.time()) + exc.seconds + 1
        write_private(cooldown, {'until': until})
        return defer(until)
    except LookupProblem as exc:
        return str(exc)
    except BrowserTimeout:
        return 'CAPUFE agotó el tiempo de espera al abrir la página o consultar el código de peaje.'
    except (BrowserError, ValueError, OSError) as exc:
        if not received:
            error = str(exc).lower()
            if 'err_cert_' in error or 'certificate' in error:
                return 'Chrome no pudo validar el certificado de CAPUFE durante la recuperación.'
            if 'err_name_not_resolved' in error:
                return 'Chrome no pudo encontrar la dirección de CAPUFE.'
            if 'err_connection' in error:
                return 'Se interrumpió la conexión con CAPUFE durante la recuperación.'
            return 'El navegador no pudo terminar la consulta de CAPUFE. Puedes volver a buscar el comprobante.'
    if received:
        return 'CAPUFE entregó ' + ', '.join(sorted(received)) + ' para el cotejo del expediente.'
    if absent == len(codes):
        store.record_not_billed(key, 'capufe', receipt, profile)
        return 'CAPUFE informó que el código de peaje aún no ha sido facturado.'
    return ('CAPUFE respondió «' + last_note + '». Sin confirmación de que el peaje esté pendiente de facturar.'
            if last_note else 'CAPUFE respondió sin documentos ni confirmación de que el peaje esté pendiente de facturar.')
