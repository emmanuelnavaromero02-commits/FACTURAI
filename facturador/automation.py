"""Process the persisted inbox using the same reader and portal runner as the panel."""
from __future__ import annotations

import asyncio
import json
import logging
import re
import sqlite3
import threading
import time
import unicodedata
from datetime import datetime
from pathlib import Path

import httpx
from pypdf.errors import PyPdfError

from . import permisos
from .ai import AI
from .auth import PlanError, read_json, write_private
from .uso_ia import en_ticket
from .catalog import CATALOG, CUSTOM_PORTAL, allowed_url, portal_suggestions, public_portal_origin
from .correo_envio import MailOutbox, can_send
from .db import AUTO_ELIGIBLE, READ_DELAYS, now
from .local_reader import read_local_page
from .models import Receipt
from .portal_directory import SOURCE as DIRECTORY, invoice_confirmed, lookup_safely, record_safely, ticket_values
from .portal_discovery import (merchant_matches_source, public_page_links_to, save_search_proof,
                               trusted_search_proof)
from .read_http import ServiceWait, retry_seconds
from .receipt_evidence import local_ticket, portal_evidence, printed_payment

log = logging.getLogger(__name__)

# A merchant without a portal that asks for the request by WhatsApp or by mail.
DIRECT = 'solicitud_directa'
# Source of a portal that only other accounts of the installation know and too few of them verified:
# the person confirms its domain, and until then the ticket counts as having no portal.
SHARED_PROPOSAL = 'directorio_compartido'
DIRECT_CHANNELS = ('correo', 'whatsapp')  # Mail first when the ticket offers both.
DIRECT_PERMISSION = {'correo': 'pedir_por_correo', 'whatsapp': 'pedir_por_whatsapp'}
DIRECT_LABEL = {'correo': 'correo', 'whatsapp': 'WhatsApp'}
DIRECT_TITLE = {'correo': 'Correo', 'whatsapp': 'WhatsApp'}
OWN_PHOTOS = 'leer_mis_fotos_de_whatsapp'
# Refusals raised while the channel is busy, before anything was reserved: try again later.
CHANNEL_BUSY = ('Hay una operación de WhatsApp en curso.', 'Espera a que termine el trámite del navegador.',
                'El navegador está ocupado.', 'Hay un envío de correo en curso.')
# After a restart nothing has opened WhatsApp and its link reads «sin_abrir» although it may still
# be linked; a look that found it loading or without connection did not find out either. The queue
# looks at it for real at most this often (seconds), and only when it needs to.
LINK_UNKNOWN = {'sin_abrir', 'cargando', 'sin_conexion'}
LINK_CHECK_EVERY = 600


def _epoch(value) -> float:
    try:
        return datetime.fromisoformat(value).timestamp()
    except (TypeError, ValueError):
        return 0.0


def permission_since(root, name):
    """Epoch since when a standing permission is on, or None while it is off."""
    for item in permisos.estado(root):
        if item['nombre'] == name and item['activo'] and isinstance(item['desde'], (int, float)):
            return item['desde']
    return None


def direct_channels(receipt: Receipt) -> list[str]:
    return [channel for channel, value in (('correo', receipt.merchant_email),
                                           ('whatsapp', receipt.merchant_whatsapp)) if value]


def direct_note(receipt: Receipt) -> str:
    labels = [DIRECT_LABEL[channel] for channel in direct_channels(receipt)]
    return 'Este comercio factura por ' + ' y por '.join(labels) + '.'


def transient_read_delay(error):
    """Return a delay only for retryable reads, never for invalid input or a refusal."""
    if isinstance(error, ServiceWait):
        return error.seconds
    if isinstance(error, PlanError) and (error.status == 'transient' or (error.status == 'limit' and error.retry_after)):
        return error.retry_after
    cause = error.__cause__ if isinstance(error, PlanError) else error
    if isinstance(cause, httpx.TransportError):
        return 0
    if isinstance(cause, httpx.HTTPStatusError) and cause.response.status_code in {408, 425, 429, 500, 502, 503, 504}:
        return retry_seconds(cause.response.headers.get('retry-after'))
    return None


def review_questions(receipt: Receipt, *, confirmed_portal: bool = False) -> list[str]:
    errors = receipt.ready_errors()
    if not receipt.amount:
        errors = ['Falta importe' if m == 'Importe inválido' else m for m in errors]
    if receipt.provider == CUSTOM_PORTAL and receipt.portal_url and not confirmed_portal:
        errors.append('Confirma el dominio del portal antes de compartir tus datos fiscales: '
                      + (public_portal_origin(receipt.portal_url) or 'revisa la dirección impresa'))
    elif receipt.provider not in CATALOG and receipt.provider not in {CUSTOM_PORTAL, DIRECT}:
        errors.append('Indica el portal de facturación que aparece en el ticket.')
    return errors


def printed_portal_confirmed(store, row, receipt: Receipt) -> bool:
    """Accept one fiscal URL actually decoded from the original ticket."""
    if receipt.provider != CUSTOM_PORTAL or not public_portal_origin(receipt.portal_url):
        return False
    marked = []
    for path in [Path(row['source_path']), *store.companion_paths(row['id'])]:
        try:
            links, _ = portal_evidence(path)
            marked.extend(links)
        except (OSError, ValueError, RuntimeError, PyPdfError):
            continue
    return list(dict.fromkeys(marked)) == [receipt.portal_url]


def portal_confirmed(store, row, receipt: Receipt) -> bool:
    # The directory only confirms the exact portal that already issued a verified
    # invoice of the RFC printed on this ticket; the browser stays on that origin.
    return printed_portal_confirmed(store, row, receipt) or (
        receipt.provider == CUSTOM_PORTAL and (trusted_search_proof(store.root, row, receipt.portal_url)
                                               or invoice_confirmed(store.root, receipt.issuer_rfc,
                                                                    receipt.portal_url)))


def plain_words(value: str) -> str:
    plain = unicodedata.normalize('NFKD', value.casefold())
    return ' '.join(re.findall(r'[a-z0-9]+', ''.join(c for c in plain if not unicodedata.combining(c))))


def catalog_merchant_confirmed(item: dict, visible_text: str, extracted_merchant: str) -> bool:
    """Corroborate a catalog brand in the original, including short station brands."""
    visible = ' ' + plain_words(visible_text) + ' '
    merchant = plain_words(extracted_merchant)
    for issuer in item.get('issuer_rfcs', []):
        if ' ' + plain_words(issuer) + ' ' in visible:
            return True
    for name in item['name'].split(' / '):
        brand = plain_words(name)
        if not brand or ' ' + brand + ' ' not in visible:
            continue
        if len(brand.replace(' ', '')) > 4:
            return True
        # A two-letter brand in a folio is not proof of the merchant. Require the
        # model's fuller merchant name to appear on the original as well.
        if merchant == brand and re.search(r'(?m)^\s*' + re.escape(brand) + r'\s*$',
                                            '\n'.join(plain_words(line) for line in visible_text.splitlines())):
            return True
        brand_words = brand.split()
        merchant_words = merchant.split()
        if merchant_words[:len(brand_words)] == brand_words and len(merchant_words) > len(brand_words) and \
                ' ' + ' '.join(merchant_words[:len(brand_words) + 1]) + ' ' in visible:
            return True
    return False


_PAYMENT_UNCERTAINTY_WORDS = set('''
    a al ambas ambos aparece aparecen asigna asignada asignado asignar clave claramente con
    confirma confirmada confirmado confirmar consta credito cual cuando de debito del determina
    determinar distingue distinguir el en
    entre es esta estan efectivo especifica especificado fue fueron foto forma falta identificada
    identificado identificar imagen indica indicar informacion inalambrica la las lector linea
    lo medio metodo muestra no o observada observado original pago pagado pero por puede pudo que
    recibo se ser si sin tarjeta tarjetas terminal ticket tipo una un usado usada utiliza
    utilizada utilizado ve ver visualmente y sat
'''.split())


def payment_only_uncertainty(message: str) -> bool:
    """Recognize a payment-only doubt, preserving doubts about any other field."""
    words = plain_words(message).split()
    if not words or not set(words) <= _PAYMENT_UNCERTAINTY_WORDS:
        return False
    return (bool(set(words) & {'pago', 'tarjeta', 'tarjetas', 'debito', 'credito', 'efectivo'})
            and bool(set(words) & {'no', 'falta', 'sin', 'distingue', 'confirmar', 'determinar'}))


_PORTAL_READABILITY_WORDS = set('''
    a al alcanza aparece aparecen aparecio aqui como completa completo con codigo comprobar
    comprobo corresponde
    carece de decodificar decodificada decodificado decodifico del direccion dominio el en enlace
    encuesta es esquema escanear escanearse esta estan extraer extraido factura facturacion
    fiscal foto fotografia fotografias fue ha hay https http identificado identifica imagen
    impresa impreso imprime imprimio
    indicar indicada indicado indica inferior junto la las leer leerse leyo legible liga link lo
    logro muestra muestran no obtener obtuvo o omite omitido original pagina partir pero
    poder por portal posible prefijo protocolo puede pudo qr que se sin solo superior su texto ticket un una
    url ve ver verificable verificada verificado verificar verifico visible visualmente www y
'''.split())
_PORTAL_PROTECTED_WORDS = set('''
    apocrifo banco cliente comercio distinto distinta diferentes diferente discrepancia
    destino emisor erroneo erronea falsificado fecha folio identidad importe incorrecto
    incorrecta marca monto oficial otro otra precio receptor redirige rfc serie sospechoso
    sospechosa tercero total
'''.split())


def catalog_portal_only_uncertainty(message: str, provider: str) -> bool:
    """Discard only QR/scheme readability notes for a corroborated catalog merchant."""
    if provider not in CATALOG:
        return False
    expected_hosts = {host.removeprefix('www.') for host in CATALOG[provider]['hosts']}
    domain = r'\b(?:www\.)?[a-z0-9-]+(?:\.[a-z0-9-]+)+\b'
    printed_hosts = re.findall(domain, message.casefold())
    if any(host.removeprefix('www.') not in expected_hosts for host in printed_hosts):
        return False
    words = plain_words(re.sub(domain, 'dominio', message.casefold())).split()
    tokens = set(words)
    if not tokens or tokens & _PORTAL_PROTECTED_WORDS or 'coincide' in tokens or 'coinciden' in tokens:
        return False
    # Keep an unfamiliar qualifier as a review item. This prevents a mixed
    # warning from disappearing merely because it also mentions QR or HTTPS.
    if not tokens <= _PORTAL_READABILITY_WORDS:
        return False
    if {'qr', 'encuesta'} <= tokens and 'no' not in tokens:
        if 'corresponde' in tokens or ({'inferior', 'superior', 'facturacion'} <= tokens
                                         and tokens & {'identificado', 'identifica', 'es'}):
            return True  # Separately labelled survey QR is an observation.
    absent = bool(tokens & {'no', 'sin', 'carece', 'omite', 'omitido'})
    if not absent:
        return False
    if 'qr' in tokens:
        return bool(tokens & {'verificar', 'verifico', 'decodificar', 'decodifico',
                              'leer', 'leyo', 'leerse', 'legible', 'aparece', 've',
                              'visible', 'impresa', 'impreso', 'texto', 'extraer'})
    if printed_hosts and tokens & {'protocolo', 'prefijo', 'esquema', 'https'}:
        return bool(tokens & {'impresa', 'impreso', 'imprime', 'imprimio', 'aparece',
                              'aparecen', 'muestra', 'muestran', 'dominio', 'direccion'})
    if (tokens & {'url', 'liga', 'enlace', 'direccion'} and
            tokens & {'facturacion', 'fiscal'} and
            tokens & {'imagen', 'foto', 'fotografia', 'fotografias', 'ticket', 'original'} and
            tokens & {'verificar', 'verifico', 'comprobar', 'comprobo', 'leer', 'leerse',
                      'decodificar', 'obtener', 'extraer'}):
        return True
    # A QR note may be split at "y no" after its preceding clause established
    # that the implied feminine noun is the fiscal URL. This fragment only says
    # that the link is not also printed next to the code.
    if re.fullmatch(r'no aparece impresa (?:en|como) texto junto al (?:codigo|qr)',
                    plain_words(message)):
        return True
    return (bool(tokens & {'liga', 'enlace', 'url', 'direccion'})
            and bool(tokens & {'impresa', 'impreso', 'texto', 'legible'})
            and bool(tokens & {'aparece', 'aparecen', 've', 'visible', 'leer', 'legible'}))


def missing_portal_only_uncertainty(message: str) -> bool:
    """A verified custom URL resolves an absent-link note, not a destination warning."""
    return bool(re.fullmatch(
        r'(?:no aparece|no se (?:encontro|identifico|leyo)|falta) (?:una |un |la |el )?'
        r'(?:liga|enlace|url|portal|sitio)(?: de facturacion)?', plain_words(message)))


def reconcile_uncertainties(messages: list[str], *, payment_confirmed: bool,
                            catalog_provider: str, custom_portal_confirmed: bool) -> list[str]:
    remaining = []
    for message in messages:
        # A model paragraph can mix payment, QR, and protected-field doubts.
        # Split semicolons and "y no ..." clauses without splitting "crédito y débito".
        clauses = re.split(r'(?<=[.!?;])\s+|\s+y\s+(?=no\s)', message.strip(), flags=re.I)
        kept = [clause.strip().rstrip(';') for clause in clauses if not (
            (payment_confirmed and payment_only_uncertainty(clause)) or
            (catalog_provider and catalog_portal_only_uncertainty(clause, catalog_provider)) or
            (custom_portal_confirmed and missing_portal_only_uncertainty(clause)))]
        if kept:
            remaining.append(' '.join(kept))
    return remaining


class _TimedLocalReader:
    """Local page reads of one ticket, with the time they took (mostly cache hits)."""
    def __init__(self):
        self.ms = 0

    def __call__(self, path, *args, **options):
        started = time.monotonic()
        try:
            return read_local_page(path, *args, **options)
        finally:
            self.ms += int((time.monotonic() - started) * 1000)


def read_ticket(plan, row, automatic, root, companions=(), *, progress=None, timing=None):
    """Read one ticket; `progress(phase)` reports stages and `timing` receives measurements."""
    ai = AI(plan, progress)
    local = _TimedLocalReader()
    try:
        return _read_ticket(ai, local, row, automatic, root, companions)
    finally:
        if timing is not None:
            timing.update(ai.timing)
            timing['ocr_ms'] = ai.timing['ocr_ms'] + local.ms


def _read_ticket(ai, read_local_page, row, automatic, root, companions):
    # read_local_page is the timed reader above; it shadows the module function on purpose.
    path = Path(row['source_path'])
    catalog = {k: v['name'] for k, v in CATALOG.items()}
    if row.get('batch_id', '').startswith('chat-'):
        result = ai.extract(path, catalog, list(companions), split_ticket=True)
    else:
        result = ai.extract(path, catalog, list(companions))
    page = {}
    if not result.is_receipt:
        # A reverse side may contain only the fiscal QR or URL. Keep it readable
        # until the other images of this upload batch have been classified.
        try:
            page = read_local_page(path)
        except (OSError, ValueError, RuntimeError, PyPdfError):
            pass
        fiscal_hint = ('factur' in (result.details + ' ' + page.get('text', '')).casefold()
                       or bool(result.billing_url))
        if (result.provider not in CATALOG or not result.merchant.strip() or not fiscal_hint
                or result.amount or result.date or result.ticket_number or result.billing_codes):
            return None, False, 'El documento no se identificó como ticket de compra. ' + result.details, {}

    try:
        date = Receipt.valid_date((result.date or '').strip())
    except ValueError:
        date = ''
    payment = row.get('confirmed_payment') or ''
    if not payment:
        evidence = printed_payment(result.payment_evidence or '')
        if evidence and evidence == result.payment_code:
            payment = evidence
        else:
            for original in [path, *companions]:
                try:
                    local_page = read_local_page(original)
                    if original == path:
                        page = local_page
                    payment = printed_payment(local_page['text'])
                    if payment:
                        break
                except (ValueError, OSError, RuntimeError, PyPdfError):
                    continue
    details = result.details
    if 'factur' in page.get('text', '').casefold() and 'factur' not in details.casefold():
        details = (details + ' El original incluye acceso a facturación.').strip()
    receipt = Receipt(provider=result.provider, merchant=result.merchant.strip(), amount=(result.amount or '').strip(),
                      date=date, ticket_number=(result.ticket_number or '').strip(),
                      billing_codes=[code.strip() for code in result.billing_codes if code.strip()],
                      payment_code=payment, details=details, source=row['receipt']['source'],
                      issuer_rfc=getattr(result, 'issuer_rfc', None) or '',
                      merchant_whatsapp=getattr(result, 'merchant_whatsapp', None) or '',
                      merchant_email=getattr(result, 'merchant_email', None) or '')
    sources = {field:'vision_modelo' for field in ('provider','merchant','amount','date','ticket_number',
               'billing_codes','details','issuer_rfc','merchant_whatsapp','merchant_email')
               if getattr(receipt, field)}
    tools = ['vision_modelo']
    if row.get('confirmed_payment'):
        sources['payment_code'] = 'correccion_usuario'
    elif payment and printed_payment(result.payment_evidence or '') == payment:
        sources['payment_code'] = 'vision_modelo_linea_pago'
    elif payment:
        sources['payment_code'] = 'lector_local'
    marked, other = [], []
    for original in [path, *companions]:
        try:
            original_page = page or read_local_page(path) if original == path else read_local_page(original)
            if original == path:
                page = original_page
            links, options = portal_evidence(original, original_page)
            marked.extend(links)
            other.extend(options)
        except (OSError, ValueError, RuntimeError, PyPdfError):
            continue
    marked, other = list(dict.fromkeys(marked)), list(dict.fromkeys(other))
    if page.get('method'):
        tools.append(page['method'])
    reported = getattr(result, 'billing_url', None)
    choices = marked or ([reported] if reported in other else [])
    billing_url = choices[0] if len(choices) == 1 else ''
    known = receipt.provider in CATALOG
    if billing_url and (not known or not allowed_url(billing_url, receipt.provider)):
        receipt.provider, receipt.portal_url = CUSTOM_PORTAL, billing_url
        sources['provider'] = sources['portal_url'] = 'texto_o_codigo_del_original'
        known = False
    searched_and_verified = directory_confirmed = False
    directory_notice = ''
    private = [*ticket_values(receipt), row.get('id', '')]
    if billing_url and marked == [billing_url]:
        # A single fiscal link printed on the original teaches this merchant's portal.
        record_safely(root, 'impreso', billing_url, rfc=receipt.issuer_rfc, merchant=receipt.merchant,
                      private=private)
    if receipt.provider == CUSTOM_PORTAL and billing_url:
        # The printed link always wins; only note a different known portal.
        entry = lookup_safely(root, rfc=receipt.issuer_rfc, merchant=receipt.merchant)
        if entry and public_portal_origin(entry['url']) != public_portal_origin(billing_url):
            tools.append(DIRECTORY)
            directory_notice = ('El ticket imprime un portal distinto al que ya se conocía para este comercio ('
                                + public_portal_origin(entry['url']) + '). Se usa el impreso.')
    entry = None
    if not marked and not billing_url and not known and (result.merchant.strip() or receipt.issuer_rfc):
        entry = lookup_safely(root, rfc=receipt.issuer_rfc, merchant=receipt.merchant)
    if entry:
        tools.append(DIRECTORY)
        receipt.provider, receipt.portal_url = CUSTOM_PORTAL, entry['url']
        # What only other accounts know, without enough of them behind it, is a proposal like a web search.
        proposal = bool(entry.get('shared')) and entry['level'] != 'factura'
        sources['provider'] = sources['portal_url'] = SHARED_PROPOSAL if proposal else DIRECTORY
        # Only an invoice verified with this ticket's issuer RFC skips the domain question.
        directory_confirmed = entry['level'] == 'factura' and entry['matched_by'] == 'rfc'
        if entry['level'] == 'busqueda' and entry['evidence_url'] and result.merchant.strip():
            # Re-check the public page that links to it for this ticket; no new web search.
            try:
                searched_and_verified = (merchant_matches_source(result.merchant.strip(), entry['evidence_url'])
                                         and asyncio.run(public_page_links_to(entry['evidence_url'], entry['url'])))
                if searched_and_verified:
                    tools.append('enlace_publico_comprobado')
                    save_search_proof(root, row, entry['url'], entry['evidence_url'])
            except (httpx.HTTPError, OSError, ValueError, asyncio.TimeoutError):
                pass
    elif not marked and not billing_url and not known and result.merchant.strip():
        tools.append('busqueda_web')
        try:
            suggested, evidence = ai.discover_portal(result.merchant.strip())
            if suggested:
                receipt.provider, receipt.portal_url = CUSTOM_PORTAL, suggested
                sources['provider'] = sources['portal_url'] = 'busqueda_web'
                searched_and_verified = (merchant_matches_source(result.merchant.strip(), evidence)
                                        and asyncio.run(public_page_links_to(evidence, suggested)))
                if searched_and_verified:
                    tools.append('enlace_publico_comprobado')
                    save_search_proof(root, row, suggested, evidence)
                    record_safely(root, 'busqueda', suggested, rfc=receipt.issuer_rfc, merchant=receipt.merchant,
                                  evidence_url=evidence, private=private)
        except (PlanError, httpx.HTTPError, OSError, ValueError, asyncio.TimeoutError):
            pass  # Keep the extracted ticket; a failed public search must not lose it.
    if row.get('auto_stage') in {'offline', 'retry_read'}:
        old = Receipt.model_validate(row['receipt'])
        for field in ('amount', 'date', 'merchant', 'billing_codes', 'ticket_number', 'payment_code',
                      'issuer_rfc', 'merchant_whatsapp', 'merchant_email'):
            if not getattr(receipt, field):
                setattr(receipt, field, getattr(old, field))
                if getattr(old, field):
                    sources[field] = 'lectura_previa'
        if receipt.provider == 'unknown' and old.provider != 'unknown':
            receipt.provider, receipt.portal_url = old.provider, old.portal_url
            sources['provider'] = sources['portal_url'] = 'lectura_previa'
            known = receipt.provider in CATALOG
        if old.payment_code and payment and old.payment_code != payment and not row.get('confirmed_payment'):
            receipt.payment_code = ''
        payment = receipt.payment_code
    catalog_confirmed = False
    if automatic and known:
        # Corroborate the catalog choice before deciding whether model prose
        # still warrants review. This never changes the portal destination.
        try:
            page = page or read_local_page(path)
        except (OSError, ValueError, RuntimeError, PyPdfError):
            page = {}
        item = CATALOG[receipt.provider]
        visible_text = page.get('text', '')
        for extra in companions:
            try:
                visible_text += '\n' + read_local_page(extra).get('text', '')
            except (OSError, ValueError, RuntimeError, PyPdfError):
                pass
        printed = catalog_merchant_confirmed(item, visible_text, receipt.merchant)
        if not printed and path.suffix.lower() != '.txt':
            try:
                rotated = read_local_page(path, rotation=180, force_ocr=True)
                printed = catalog_merchant_confirmed(item, rotated.get('text', ''), receipt.merchant)
                if printed:
                    tools.append('ocr_giro_180')
                    sources['provider'] = 'vision_modelo_y_ocr_giro_180'
            except (OSError, ValueError, RuntimeError, PyPdfError):
                pass
        links = portal_suggestions(page.get('codes', []), page.get('text', ''))
        unique_host = any([key for key in CATALOG if allowed_url(link, key)] == [receipt.provider] for link in links)
        catalog_confirmed = printed or unique_host
    uncertainties = list(result.uncertainties)
    if len(choices) > 1:
        uncertainties.append('Se encontraron varios enlaces fiscales impresos. Revisa los destinos en Leer texto, QR y barras.')
    elif not billing_url and not known and not searched_and_verified and not directory_confirmed and (other or reported):
        uncertainties.append('Comprueba el sitio del comercio en el original. El destino para facturar sigue sin confirmar.')
    confirmed_portal = searched_and_verified or (receipt.provider == CUSTOM_PORTAL and (
        marked == [receipt.portal_url] or
        directory_confirmed and invoice_confirmed(root, receipt.issuer_rfc, receipt.portal_url)))
    uncertainties = reconcile_uncertainties(
        uncertainties,
        payment_confirmed=bool(payment and receipt.payment_code == payment and
                               sources.get('payment_code') in {'correccion_usuario', 'vision_modelo_linea_pago',
                                                               'lector_local'}),
        catalog_provider=receipt.provider if catalog_confirmed else '',
        custom_portal_confirmed=confirmed_portal)
    questions = [*review_questions(receipt, confirmed_portal=confirmed_portal), *uncertainties]
    if automatic and known and not questions and not catalog_confirmed:
        questions.append('Confirma que el comercio del ticket corresponde a ' + item['name'] + '.')
    ready = bool(automatic and (known or confirmed_portal) and not questions)
    note = ' '.join(dict.fromkeys(questions)) if questions else (
        'Portal localizado por búsqueda web y enlazado desde una fuente pública. Listo para preparar.'
        if ready and searched_and_verified else
        'Portal conocido: ya se facturó en él con el mismo RFC del comercio. Listo para preparar.'
        if ready and directory_confirmed else
        'Datos completos. Listo para preparar el portal.' if ready else 'Revisa los datos y guarda la revisión.')
    if directory_notice:
        note += ' ' + directory_notice  # Informative only; it never blocks the ticket.
    return receipt, ready, note, {'fields':sources, 'tools':list(dict.fromkeys(tools))}


# Tickets read at the same time. Each read mostly waits on the model; local OCR
# has its own limit in local_reader.
MAX_PARALLEL_READS = 3
READ_PHASES = {
    'preparando': 'Preparando la foto del ticket.',
    'leyendo': 'Leyendo los datos del ticket.',
    'buscando_portal': 'Buscando el portal de facturación del comercio.',
}


class TicketAutomation:
    def __init__(self, store, runner, plan, get_profile, *, whatsapp=None, mail=None, engines=None):
        """`whatsapp` returns the WhatsApp routes (or None); `mail` is a mail outbox, or True for the program's own.

        Without them a ticket that asks for a direct request waits for the person. `engines` is the
        coordinator of several engines (motores.py): with it a ready ticket starts as soon as one of
        them can take it; `runner` stays the main one, the only one that uses WhatsApp and the mail."""
        self.store, self.runner, self.plan, self.get_profile = store, runner, plan, get_profile
        self.engines = engines
        self.whatsapp = whatsapp
        self.mail = MailOutbox(store) if mail is True else mail
        # Only tickets created after this start may be requested by the queue: a new
        # version never sends anything for a ticket that already existed.
        self.started, self.started_at = time.time(), now()
        self._direct: dict[str, dict] = {}  # Direct requests this run may still send.
        self._direct_checked: dict[str, str] = {}  # Last reading examined for a direct channel.
        self._adopted: set[str] = set()  # Own WhatsApp photos already sent down the chat path.
        self._link_check: asyncio.Task | None = None  # The look at the WhatsApp link under way, if any.
        self._link_checked = float('-inf')  # When the queue last asked for one (monotonic clock).
        self.settings = store.root / 'automation.json'
        settings = read_json(self.settings, {})
        self.enabled = settings.get('enabled') is True
        self.plan_read_after = settings.get('plan_read_after', 0)
        self.wake = asyncio.Event()
        self.task = None
        self.stopping = False
        self._state = {}
        self.reads: dict[str, asyncio.Task] = {}  # Background reads started by the queue.
        self._jobs: dict[str, dict] = {}  # Stage of every read in progress, for the panel.
        self._job_claims: dict[str, str] = {}
        self._jobs_lock = threading.Lock()  # Phases are reported from reader threads.
        self._classify = threading.Lock()  # Saving a reading and pairing its batch happen together.
        self._reset()

    def _set(self, phase, key, message):
        self._state = {'current_id': key, 'phase': phase, 'message': message}

    def _reset(self):
        if self.enabled:
            self._set('idle', None, 'Esperando tickets.')
        else:
            self._set('off', None, 'Procesamiento automático desactivado.')

    def status(self):
        with self._jobs_lock:
            jobs = {key: dict(value) for key, value in self._jobs.items()}
        return {'enabled': self.enabled, **self._state, 'jobs': jobs}

    def _phase(self, key, claim, phase):
        with self._jobs_lock:
            # A reader thread left behind by a cancelled read must not touch a newer one.
            if self._job_claims.get(key) == claim:
                self._jobs[key] = {'phase': phase, 'message': READ_PHASES[phase], 'since': time.time()}

    def _track(self, key, claim):
        with self._jobs_lock:
            self._job_claims[key] = claim
        self._phase(key, claim, 'preparando')

    def _untrack(self, key, claim):
        with self._jobs_lock:
            if self._job_claims.get(key) == claim:
                del self._job_claims[key]
                self._jobs.pop(key, None)

    def configure(self, enabled: bool):
        write_private(self.settings, {'enabled': enabled, 'plan_read_after': self.plan_read_after})
        if self.enabled != enabled:
            self.enabled = enabled
            self._reset()
        self.wake.set()
        return self.status()

    def start(self):
        if not self.task or self.task.done():
            self.stopping = False
            self.task = asyncio.create_task(self._work())

    async def close(self):
        self.stopping = True
        self.wake.set()
        if self.task:
            await self.task
        # Each cancelled read releases its claim (see analyze); its thread result is discarded.
        reads = list(self.reads.values())
        if self._link_check and not self._link_check.done():
            reads.append(self._link_check)
        for read in reads:
            read.cancel()
        if reads:
            await asyncio.gather(*reads, return_exceptions=True)

    def _launch_read(self, key, *, offline=False, explain=False):
        """Read one ticket in the background; the caller yields once so it claims the ticket."""
        task = self.reads[key] = asyncio.create_task(self._background_read(key, offline, explain))
        return task

    async def _background_read(self, key, offline, explain):
        try:
            if not offline and self.plan_read_after > time.time():
                offline = True  # A read started together with this one hit the service's wait.
            row = await self.analyze(key, automatic=True, offline=offline)
            if explain and self.enabled and row['status'] in ('review', 'blocked', 'duplicate'):
                self._set('review', key, row['message'])
        except asyncio.CancelledError:
            raise
        except Exception:
            # One failed read never stops the others or the queue.
            log.exception('Error al leer un ticket de la bandeja')
            self._set('review', key, 'Revisa el estado del ticket en la bandeja.')
        finally:
            if self.reads.get(key) is asyncio.current_task():
                del self.reads[key]
            self.wake.set()

    def _read_slots(self):
        return MAX_PARALLEL_READS - len(self.reads)

    async def read_offline(self, row, claim, *, reason, stage='review', read_after=0, timing=None):
        started = time.monotonic()
        try:
            receipt, warning, method = await asyncio.to_thread(local_ticket, row)
        except Exception:
            # A broken document must release its read claim so other uploads keep moving.
            receipt, warning, method = Receipt.model_validate(row['receipt']), 'El lector local no encontró texto legible.', 'error'
        if timing is not None:
            timing['ocr_ms'] = timing.get('ocr_ms', 0) + int((time.monotonic() - started) * 1000)
        notes = [reason, warning, *review_questions(receipt)]
        note = ' '.join(s.rstrip('.') + '.' for s in notes if s)
        with self._classify:
            saved = self.store.save_receipt(row['id'], receipt, claim=claim, note=note,
                                            read_stage=stage, read_after=read_after)
            if saved['receipt'] == receipt.model_dump():
                changed = {field:method for field in ('provider','merchant','amount','date','ticket_number',
                           'billing_codes','payment_code','portal_url') if getattr(receipt, field) != row['receipt'].get(field)}
                self.store.event(row['id'], 'fuentes_de_campos', json.dumps({'fields':changed,'tools':[method]}, ensure_ascii=False))
            self.store.pair_batch_if_unique(row['id'])
        return self.store.get(row['id'])

    async def analyze(self, key: str, automatic=False, offline=False):
        if not offline and self.plan_read_after > time.time():
            raise ValueError('La IA pidió una espera. Mientras tanto puedes usar la lectura local.')
        claimed = self.store.claim_read(key, automatic)
        if not claimed:
            return self.store.get(key)
        row, claim = claimed
        self._track(key, claim)
        started, timing = time.monotonic(), {}
        try:
            with en_ticket(key):  # Every model call of this read is counted for this ticket.
                result = await self._read_claimed(key, row, claim, automatic, offline, timing)
        except asyncio.CancelledError:
            # Shutdown: free the ticket now instead of waiting for the next start.
            self.store.release_read(key, claim)
            raise
        finally:
            self._untrack(key, claim)
        self.store.event(key, 'read_timing', json.dumps({
            'total_ms': int((time.monotonic() - started) * 1000),
            'ocr_ms': int(timing.get('ocr_ms', 0)), 'model_ms': int(timing.get('model_ms', 0)),
            'web_search_ms': int(timing.get('web_search_ms', 0)),
            'effort': str(timing.get('effort', '')), 'escalated': bool(timing.get('escalated', False))}))
        return result

    async def _read_claimed(self, key, row, claim, automatic, offline, timing):
        try:
            if offline:
                return await self.read_offline(row, claim, reason='Lectura local',
                                               stage='offline' if automatic else 'review', timing=timing)
            companions = self.store.companion_paths(key)
            receipt, ready, note, provenance = await asyncio.to_thread(
                read_ticket, self.plan, row, automatic, self.store.root, companions,
                progress=lambda phase: self._phase(key, claim, phase), timing=timing)
            with self._classify:
                if receipt is None:
                    self.store.finish_read(key, claim, 'blocked', note)
                    self.store.pair_batch_if_unique(key)
                    return self.store.get(key)
                saved = self.store.save_receipt(key, receipt, ready, claim=claim, note=note)
                if saved['receipt'] == receipt.model_dump():
                    self.store.event(key, 'fuentes_de_campos', json.dumps(provenance, ensure_ascii=False))
                self.store.pair_batch_if_unique(key)
            return self.store.get(key)
        except Exception as exc:
            note = str(exc) if isinstance(exc, PlanError) else 'No se pudo completar la lectura. Revisa el original o vuelve a leerlo.'
            stage, due = 'review', 0
            wait = transient_read_delay(exc)
            if wait:
                self.plan_read_after = max(self.plan_read_after, time.time() + wait)
                write_private(self.settings, {'enabled': self.enabled, 'plan_read_after': self.plan_read_after})
            if automatic or (self.enabled and row['auto_stage'] == 'retry_read'):
                if wait is None and isinstance(exc, PlanError) and self.plan.status()['status'] != 'ready':
                    stage = 'offline'
                else:
                    attempts = row['read_attempts']
                    if wait is not None and 1 <= attempts <= len(READ_DELAYS):
                        delay = max(wait, READ_DELAYS[attempts - 1])
                        stage, due = 'retry_read', time.time() + delay
                        note += f' Reintento automático de lectura en {delay} segundos.'
                    elif wait is not None:
                        note += ' Se agotaron los reintentos automáticos. Puedes volver a leer el ticket desde la bandeja.'
            return await self.read_offline(row, claim, reason=note, stage=stage, read_after=due, timing=timing)

    def _plan_ready(self, key):
        state = self.plan.status()
        if state['status'] == 'ready' and self.plan_read_after > time.time():
            self._set('waiting_read', key, 'La IA pidió una espera temporal. La bandeja sigue leyendo los originales localmente.')
            return False
        if state['status'] == 'ready':
            return True
        self._set('waiting_plan', key, state.get('message') or 'Conecta o reactiva tu conexión de IA en Ajustes.')
        return False

    def _prepare(self, row):
        key = row['id']
        if self.store.batch_unread(key):
            self._set('reading', key, 'Esperando la lectura de las demás imágenes de esta carga.')
            return False
        receipt = Receipt.model_validate(row['receipt'])
        if receipt.provider == DIRECT:
            # A direct request never opens a portal; it is asked by WhatsApp or by mail.
            message = direct_note(receipt) if direct_channels(receipt) else 'Solicita el CFDI al comercio.'
            self.store.note_review(row, message + ' Pídela desde el ticket.')
            self._set('review', key, message)
            return self.store.get(key)['status'] != 'ready'
        confirmed_portal = portal_confirmed(self.store, row, receipt)
        questions = review_questions(receipt, confirmed_portal=confirmed_portal)
        if questions:
            message = ' '.join(questions)
            self.store.note_review(row, message, keep_ready=receipt.provider == CUSTOM_PORTAL and not receipt.ready_errors())
            self._set('review', key, message)
            return True
        if not self._plan_ready(key):
            return False
        try:
            profile = self.get_profile()
        except ValueError:
            self._set('waiting_profile', key, 'Guarda tu perfil fiscal en Ajustes para preparar el portal.')
            return False
        if self.engines is not None:
            # Several engines: one of them must be free and nobody else on this ticket's portal.
            waiting = self.engines.waits_for(row['receipt'])
            if waiting:
                self._set('waiting_browser', key, waiting)
                return False
        else:
            viewer = self.runner.viewer
            if (self.runner.has_active_jobs() or self.runner.lock.locked() or viewer.channel
                    or viewer.dialog or viewer.file_request):
                self._set('waiting_browser', key, 'Esperando a que se libere el navegador.')
                return False
        # No await between checking ownership and calling schedule, which atomically claims
        # the job in SQLite and registers its task before yielding control to other handlers.
        self.runner.schedule(key, profile, auto_issue=True,
                             confirmed_url=receipt.portal_url if confirmed_portal else '')
        self._set('queued', key, 'En cola para preparar el portal y continuar donde los datos sean comprobables.')
        return True

    # Direct requests: a merchant without a portal that asks for WhatsApp or mail.

    def _without_portal(self, row) -> bool:
        """True only when no portal is known: catalog, printed link, directory or verified search."""
        receipt = Receipt.model_validate(row['receipt'])
        if receipt.provider == CUSTOM_PORTAL:
            # Only an unverified web suggestion is not a portal; the ticket's own instruction wins.
            if portal_confirmed(self.store, row, receipt):
                return False
            sources = next((json.loads(event['detail']) for event in reversed(self.store.events(row['id']))
                            if event['kind'] == 'fuentes_de_campos'), {})
            if (sources.get('fields', {}).get('provider') not in {'busqueda_web', SHARED_PROPOSAL}
                    or 'enlace_publico_comprobado' in sources.get('tools', [])):
                return False
        elif receipt.provider not in {'unknown', DIRECT}:
            return False
        for path in [Path(row['source_path']), *self.store.companion_paths(row['id'])]:
            try:
                links, _ = portal_evidence(path, read_local_page(path))
            except (OSError, ValueError, RuntimeError, PyPdfError):
                continue
            if links:
                return False  # A printed fiscal link (even an ambiguous one) is a portal.
        return True

    async def _direct_from_read(self, row) -> bool:
        """Turn a reading without a portal into a direct request when the ticket names a channel."""
        key = row['id']
        receipt = Receipt.model_validate(row['receipt'])
        if (row['status'] != 'review' or row['auto_stage'] != 'review' or not direct_channels(receipt)
                or row['historical'] or row['issue_attempted'] or row['human_exposed'] or row['issuance_seen']
                or row['uuid'] or receipt.source.startswith('Ejemplo:') or not row['source_path']
                or _epoch(row['updated']) < self.started or self._direct_checked.get(key) == row['updated']):
            return False
        self._direct_checked[key] = row['updated']
        if not await asyncio.to_thread(self._without_portal, row):
            return False
        direct = receipt.model_copy(update={'provider': DIRECT, 'portal_url': ''})
        errors = review_questions(direct)
        note = ' '.join([direct_note(direct), *(error.rstrip('.') + '.' for error in errors)])
        with self._classify:
            current = self.store.get(key)
            if current['updated'] != row['updated']:
                return False  # A person or another read changed it meanwhile.
            # auto_stage 'review' is the token here: any save by a person clears it.
            saved = self.store.save_receipt(key, direct, not errors, claim='review', note=note)
        if saved['receipt'] != direct.model_dump() or saved['status'] not in {'external', 'review'}:
            return False
        self._note_direct(key, note)
        dropped = public_portal_origin(receipt.portal_url) if receipt.portal_url else ''
        self.store.event(key, 'canal_directo', note + (' Sugerencia web sin comprobar descartada: ' + dropped
                                                        if dropped else ''))
        self._direct[key] = {'failed': {}, 'message': ''}
        self._set('review', key, note)
        return True

    def _note_direct(self, key, message):
        with self.store.connect() as db:
            db.execute("UPDATE jobs SET message=?,updated=? WHERE id=? AND status='external' AND issue_attempted=0 "
                       "AND message<>?", (message, now(), key, message))

    def _requested(self, key) -> bool:
        """Either channel already holds a request for this case (sent, uncertain or being prepared)."""
        with self.store.connect() as db:
            for table in ('mail_requests', 'whatsapp_requests'):
                try:
                    if db.execute(f'SELECT 1 FROM {table} WHERE job_id=?', (key,)).fetchone():
                        return True
                except sqlite3.OperationalError:
                    pass  # That channel was never set up in this data folder.
        return False

    def _browser_busy(self):
        viewer = self.runner.viewer
        return bool(self.runner.has_active_jobs() or self.runner.lock.locked() or viewer.channel
                    or viewer.dialog or viewer.file_request)

    def _unknown_link(self, routes) -> str:
        """What to tell the person while nobody knows whether WhatsApp is linked; '' to say «vincula» as before.

        After a restart the state says nothing: the session may be as linked as it was. The queue
        asks for the same look «Comprobar vínculo» takes, in the background, only with the browser
        free, at most once every ten minutes, and never for someone who does not use WhatsApp.
        """
        if self._link_check is not None and not self._link_check.done():
            return 'Comprobando si WhatsApp sigue vinculado.'
        try:
            if not routes.in_use():
                return ''  # Nothing is opened for someone who does not use WhatsApp.
        except Exception:
            return ''
        if time.monotonic() - self._link_checked < LINK_CHECK_EVERY:
            return 'No se pudo comprobar WhatsApp. Se intentará de nuevo en unos minutos.'
        if self._browser_busy():
            return 'Se pedirá por WhatsApp cuando se libere el navegador.'
        self._link_checked = time.monotonic()
        self._link_check = asyncio.create_task(self._look_at_link(routes))
        return 'Comprobando si WhatsApp sigue vinculado.'

    async def _look_at_link(self, routes):
        try:
            await routes.check_link()
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            if isinstance(exc, ValueError) and str(exc) in CHANNEL_BUSY:
                self._link_checked = float('-inf')  # It never got to look: the next round may ask again.
            elif not isinstance(exc, ValueError):
                log.warning('No se pudo comprobar el vínculo de WhatsApp (%s).', type(exc).__name__)
        finally:
            self.wake.set()

    async def _channel_state(self, row, channel, info):
        """('send' | 'wait' | 'manual', message for the person) for one channel of the ticket."""
        if channel in info['failed']:
            return 'manual', (f'No se pudo pedir por {DIRECT_LABEL[channel]}: '
                              f'{info["failed"][channel].rstrip(".")}. Pídela desde el ticket.')
        since = permission_since(self.store.root, DIRECT_PERMISSION[channel])
        if since is None:
            return 'manual', 'Pídela desde el ticket, o activa el permiso en Ajustes para que los próximos se pidan solos.'
        created = _epoch(row['created'])
        if created < since or created < self.started:
            return 'manual', 'Pídela desde el ticket.'  # Turning a permission on never sends old tickets.
        if channel == 'correo':
            if self.mail is None or not can_send(self.store.root):
                return 'wait', 'Conecta un correo con envío en Ajustes para pedirla sola.'
        else:
            routes = self.whatsapp() if self.whatsapp else None
            try:
                link = await routes.link_state() if routes is not None else {}
            except Exception:
                link = {}  # An unreadable WhatsApp page counts as not linked.
            if not link.get('ready'):
                if link.get('state') in LINK_UNKNOWN:
                    unknown = self._unknown_link(routes)
                    if unknown:
                        return 'wait', unknown
                return 'wait', 'Vincula WhatsApp para pedirla sola.'
            if self._browser_busy():
                return 'wait', 'Se pedirá por WhatsApp cuando se libere el navegador.'
        try:
            self.get_profile()
        except ValueError:
            return 'wait', 'Guarda tu perfil fiscal en Ajustes para pedirla sola.'
        return 'send', ''

    async def _send_direct(self, key, channel, receipt):
        """'done' once a request exists, 'busy' to try later, or the error that stopped it."""
        try:
            if channel == 'correo':
                await asyncio.to_thread(self.mail.request, key, receipt.merchant_email, False, self.get_profile())
            else:
                await self.whatsapp().request_automatically(key)
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            if self._requested(key):
                return 'done'  # It may have reached the merchant: its own subsystem follows it up.
            message = str(exc) if isinstance(exc, ValueError) else 'Revisa el canal en Ajustes.'
            if message in CHANNEL_BUSY:
                return 'busy'
            if not isinstance(exc, ValueError):
                log.exception('Error al pedir una factura por %s', DIRECT_LABEL[channel])
            self.store.event(key, 'pedido_directo_error', f'{channel}: {message}')
            return message
        self.store.event(key, 'pedido_directo_auto', channel)
        return 'done'

    async def _direct_request(self, key) -> bool:
        info = self._direct[key]
        try:
            row = self.store.get(key)
        except KeyError:
            self._direct.pop(key, None)
            return False
        receipt = Receipt.model_validate(row['receipt'])
        if row['status'] == 'review' and receipt.provider == DIRECT and not row['issue_attempted']:
            return False  # Still waits for the person to complete the ticket.
        if (row['status'] != 'external' or receipt.provider != DIRECT or row['historical'] or row['issue_attempted']
                or row['human_exposed'] or row['issuance_seen'] or row['uuid'] or receipt.ready_errors()
                or receipt.source.startswith('Ejemplo:') or self._requested(key)):
            self._direct.pop(key, None)  # Already requested, invoiced or changed by the person.
            return False
        states = {}
        for channel in direct_channels(receipt):
            states[channel] = await self._channel_state(row, channel, info)
            if states[channel][0] != 'send':
                continue
            self._set('requesting', key, f'Pidiendo la factura por {DIRECT_LABEL[channel]}.')
            outcome = await self._send_direct(key, channel, receipt)
            if outcome == 'done':
                self._direct.pop(key, None)
                self._set('requested', key, self.store.get(key)['message'])
                return True
            if outcome == 'busy':
                return False  # Nothing was reserved; this channel is tried again on the next round.
            info['failed'][channel] = outcome  # Recorded once; never retried by the queue.
            states[channel] = await self._channel_state(row, channel, info)
        both = len(states) > 1
        message = ' '.join([direct_note(receipt), *dict.fromkeys(
            (DIRECT_TITLE[channel] + ': ' if both else '') + text for channel, (_, text) in states.items())])
        if message != info['message']:
            info['message'] = message
            self._note_direct(key, message)
        if not any(state == 'wait' for state, _ in states.values()):
            self._direct.pop(key, None)  # Nothing left to wait for: the person decides.
        return False

    async def _direct_step(self) -> bool:
        for key in list(self._direct):
            if await self._direct_request(key):
                return True
        return False

    async def _direct_inbox(self) -> bool:
        """Inbox tickets read in this run (the toggle is on) that name a WhatsApp or a mail."""
        with self.store.connect() as db:
            rows = [self.store.decode(row) for row in db.execute(
                f"""SELECT * FROM jobs WHERE {AUTO_ELIGIBLE} AND status='review' AND auto_stage='review'
                    AND updated>=? AND json_extract(receipt,'$.provider') IN (?,?,?)
                    AND (COALESCE(json_extract(receipt,'$.merchant_whatsapp'),'')<>''
                         OR COALESCE(json_extract(receipt,'$.merchant_email'),'')<>'')
                    ORDER BY created,id""", (self.started_at, 'unknown', DIRECT, CUSTOM_PORTAL))]
        for row in rows:
            if await self._direct_from_read(row):
                return True
        return False

    def _own_photos_on(self) -> bool:
        return permisos.permitido(self.store.root, OWN_PHOTOS)

    def _adopt_own_photos(self):
        """Photos the WhatsApp watcher took from the owner's own chat follow the chat path."""
        if not self._own_photos_on():
            return
        try:
            with self.store.connect() as db:
                media = db.execute("SELECT at,job_ids FROM whatsapp_own_media WHERE state='imported' AND at>=?",
                                   (self.started_at,)).fetchall()
        except sqlite3.OperationalError:
            return  # The WhatsApp watcher never ran in this data folder.
        for item in media:
            imported = _epoch(item['at'])
            for key in filter(None, item['job_ids'].split(',')):
                if key in self._adopted:
                    continue
                self._adopted.add(key)
                try:
                    row = self.store.get(key)
                except KeyError:
                    continue
                # Only the ticket this import created now; never an older one with the same photo.
                if (row['status'] == 'new' and row['auto_stage'] == '' and not row['batch_id']
                        and not row['historical'] and row['receipt']['source'].startswith('WhatsApp')
                        and self.started <= _epoch(row['created']) <= imported + 1
                        and imported - _epoch(row['created']) < 300):
                    self.store.queue_chat_job(key)

    async def chat_step(self, wait=True):
        """Process chat uploads independently of the legacy inbox toggle.

        With wait, return after the reads started here finish (one complete round);
        the worker loop passes wait=False so reads run while it keeps going."""
        # A model wait requested by the service is honoured here: while it lasts no
        # new read goes to the model; the originals are read locally instead.
        ready = self.plan.status()['status'] == 'ready' and self.plan_read_after <= time.time()
        launched = []
        self._adopt_own_photos()
        try:
            for row in self.store.chat_jobs():
                key, stage = row['id'], row['auto_stage']
                if key in self.reads or stage.startswith('reading:'):
                    continue  # Its reading is still running.
                if row['status'] == 'new' or (row['status'] == 'review' and stage in {'offline', 'retry_read'}):
                    if stage == 'retry_read' and row['read_after'] > time.time():
                        continue
                    if not ready and row['status'] == 'review':
                        continue
                    if self._read_slots() <= 0:
                        continue
                    self._set('reading', key, 'Leyendo las imágenes del ticket.')
                    launched.append(self._launch_read(key, offline=not ready))
                    continue
                if row['status'] == 'ready':
                    prepared = self._prepare(row)
                    updated = self.store.get(key)
                    if updated['status'] != 'ready':
                        self.store.finish_chat_job(key)
                    if prepared:
                        return True
                    continue
                await self._direct_from_read(row)
                self.store.finish_chat_job(key)
                return True
            return bool(launched)
        finally:
            if launched and wait:
                await asyncio.gather(*launched)
            elif launched:
                await asyncio.sleep(0)  # Let the new reads claim their tickets.

    async def step(self, wait=True):
        """One round of the queue; `wait` as in chat_step."""
        if await self.chat_step(wait):
            return True
        if await self._direct_step():
            return True
        if not self.enabled:
            return False
        if await self._direct_inbox():
            return True
        # Explain already extracted tickets without another model request or a field change.
        previous = self.store.auto_candidates('review')
        if previous:
            receipt = Receipt.model_validate(previous['receipt'])
            questions = review_questions(receipt, confirmed_portal=portal_confirmed(self.store, previous, receipt))
            old_note = previous['message']
            if old_note and old_note not in ('Revisar datos extraídos', 'Datos extraídos; revisar antes de emitir'):
                questions.append(old_note)
            message = ' '.join(dict.fromkeys(questions)) or 'Revisa los datos y guarda la revisión.'
            self.store.note_review(previous, message)
            self._set('review', previous['id'], message)
            return True
        if self.engines is None:
            ready = self.store.auto_candidates('ready')
            if ready and self._prepare(ready):
                return True
        else:
            # Several engines: a ticket whose portal is busy does not hold the ones behind it.
            ready = held = None
            for row in self._ready_inbox():
                if ready is not None and self.engines.waits_for(row['receipt']):
                    # Behind the oldest one, only a ticket that can start right now is prepared.
                    if self.engines.portal_busy(row['receipt']):
                        continue
                    break  # Nobody can take another ticket now.
                ready = ready or row
                if self._prepare(row):
                    return True
                held = held or self._state  # What the oldest one waits for is what the panel says.
                if not self.engines.portal_busy(row['receipt']):
                    break  # It waits for something else, as the only ticket of a single engine would.
            if held:
                self._state = held
        incoming = None
        if self._read_slots() > 0:
            plan_ready = self.plan.status()['status'] == 'ready' and self.plan_read_after <= time.time()
            incoming = self.store.auto_candidates('review', stage='retry_read', due_only=True) if plan_ready else None
            incoming = incoming or self.store.auto_candidates('new')
            if not incoming and plan_ready:
                incoming = self.store.auto_candidates('review', stage='offline')
        if incoming and incoming['id'] not in self.reads:
            key = incoming['id']
            if not self._plan_ready(key):
                read = self._launch_read(key, offline=True)
            else:
                self._set('reading', key, 'Leyendo el ticket con la IA.')
                read = self._launch_read(key, explain=True)
            if wait:
                await read
            else:
                await asyncio.sleep(0)  # The read claims its ticket before the next candidate query.
            return True
        if ready:
            return False
        if self.engines is not None:
            return await self._rest_with_engines()
        if self.runner.has_active_jobs():
            key = self.runner.active_id or next(iter(self.runner.job_tasks))
            self._set('queued', key, self.store.get(key)['message'])
        else:
            if not (self.runner.viewer.channel or self.runner.viewer.dialog or self.runner.viewer.file_request):
                key = self.store.next_recovery(int(time.time()))
                if key:
                    result = await self.runner.retry_job(key, self.get_profile, automatic=True)
                    self._set('recovering', key, result['message'])
                    return True
            self._explain_rest()
        return False

    def _ready_inbox(self):
        """Every inbox ticket ready for its portal, oldest first: what auto_candidates('ready') gives one by one."""
        with self.store.connect() as db:
            rows = db.execute(f"SELECT * FROM jobs WHERE {AUTO_ELIGIBLE} AND status='ready' AND auto_stage='' "
                              "ORDER BY read_after,created,id LIMIT 50").fetchall()
        return [self.store.decode(row) for row in rows]

    async def _rest_with_engines(self):
        """The end of a round with several engines: say what is in progress and recover while one is free."""
        working = self.engines.in_progress()
        if len(working) == 1:
            self._set('queued', working[0], self.store.get(working[0])['message'])
        elif working:
            waiting = self.engines.waiting_for_person()
            self._set('queued', None, f'{len(working)} tickets en trámite a la vez.' + (
                ' Uno espera tu ayuda.' if waiting == 1 else f' {waiting} esperan tu ayuda.' if waiting else ''))
        if self.engines.free_engine() is not None:
            key = self.store.next_recovery(int(time.time()))
            if key:
                result = await self.engines.retry_job(key, self.get_profile, automatic=True)
                self._set('recovering', key, result['message'])
                return True
        if not working:
            self._explain_rest()
        return False

    def _explain_rest(self):
        """No ticket is in a portal: say what the queue waits for, if anything."""
        pending = self.store.auto_candidates('review', stage='review') or self.store.auto_candidates('ready', stage='review')
        offline = self.store.auto_candidates('review', stage='offline')
        retry = self.store.auto_candidates('review', stage='retry_read')
        if retry:
            if self._plan_ready(retry['id']):
                self._set('waiting_read', retry['id'], retry['message'])
        elif pending:
            self._set('review', pending['id'], pending['message'])
        elif offline:
            self._plan_ready(offline['id'])
        elif self.reads:
            count = len(self.reads)
            self._set('reading', next(iter(self.reads)),
                      'Leyendo un ticket.' if count == 1 else f'Leyendo {count} tickets.')
        else:
            self._reset()

    async def _work(self):
        while not self.stopping:
            self.wake.clear()
            try:
                if await self.step(wait=False):
                    await asyncio.sleep(0)
                    continue
            except Exception:
                log.exception('Error al procesar la bandeja de tickets')
                self._set('review', self._state.get('current_id'), 'Revisa el estado del ticket en la bandeja.')
            try:
                # Direct requests waiting for a channel and own WhatsApp photos are not announced by a wake.
                polling = (self.enabled or self.store.has_chat_jobs() or self._direct or self._own_photos_on())
                await asyncio.wait_for(self.wake.wait(), timeout=2 if polling else None)
            except asyncio.TimeoutError:
                pass
