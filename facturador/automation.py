"""Process the persisted inbox using the same reader and portal runner as the panel."""
from __future__ import annotations

import asyncio
import json
import logging
import re
import time
import unicodedata
from pathlib import Path

import httpx
from pypdf.errors import PyPdfError

from .ai import AI
from .auth import PlanError, read_json, write_private
from .catalog import CATALOG, CUSTOM_PORTAL, allowed_url, portal_suggestions, public_portal_origin
from .db import READ_DELAYS
from .local_reader import read_local_page
from .models import Receipt
from .portal_discovery import (merchant_matches_source, public_page_links_to, save_search_proof,
                               trusted_search_proof)
from .read_http import ServiceWait, retry_seconds
from .receipt_evidence import local_ticket, portal_evidence, printed_payment

log = logging.getLogger(__name__)


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
    elif receipt.provider not in CATALOG and receipt.provider != CUSTOM_PORTAL:
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
    return printed_portal_confirmed(store, row, receipt) or (
        receipt.provider == CUSTOM_PORTAL and trusted_search_proof(store.root, row, receipt.portal_url))


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
    a al ambas ambos aparece aparecen claramente con confirma confirmada confirmado confirmar
    consta credito cual cuando de debito del determina determinar distingue distinguir el en
    entre es esta estan efectivo especifica especificado fue fueron foto forma falta identificada
    identificado identificar imagen indica indicar informacion inalambrica la las lector linea
    lo medio metodo muestra no observada observado original pago pagado pero por puede pudo que
    recibo se ser si sin tarjeta tarjetas terminal ticket tipo una un usado usada utiliza
    utilizada utilizado ve ver visualmente y
'''.split())


def payment_only_uncertainty(message: str) -> bool:
    """Recognize a payment-only doubt, preserving doubts about any other field."""
    words = plain_words(message).split()
    if not words or not set(words) <= _PAYMENT_UNCERTAINTY_WORDS:
        return False
    return (bool(set(words) & {'pago', 'tarjeta', 'tarjetas', 'debito', 'credito', 'efectivo'})
            and bool(set(words) & {'no', 'falta', 'sin', 'distingue', 'confirmar', 'determinar'}))


def catalog_portal_only_uncertainty(message: str, provider: str) -> bool:
    """Recognize readability notes about HTTPS/QR, not mismatched destinations."""
    if provider not in CATALOG:
        return False
    expected_hosts = {host.removeprefix('www.') for host in CATALOG[provider]['hosts']}
    printed_hosts = re.findall(r'\b(?:www\.)?[a-z0-9-]+(?:\.[a-z0-9-]+)+\b', message.casefold())
    if any(host.removeprefix('www.') not in expected_hosts for host in printed_hosts):
        return False
    words = plain_words(message)
    if re.search(r'\b(?:diferente|distinto|coincide|inseguro|sospechoso|redirige|importe|folio|rfc|identidad)\b', words):
        return False
    return bool(any(re.fullmatch(pattern, words) for pattern in (
        r'(?:el ticket imprime el dominio|se imprime el dominio) [a-z0-9 ]+ sin (?:el )?(?:esquema|prefijo|protocolo) https',
        r'el portal de facturacion aparece impreso como [a-z0-9 ]+ sin (?:el )?(?:esquema|prefijo|protocolo) https',
        r'no se pudo verificar una liga https a partir del qr',
        r'el qr (?:inferior|de facturacion) esta identificado como el de facturacion pero no se pudo decodificar su url',
        r'el qr superior corresponde a una encuesta',
        r'(?:el )?(?:codigo )?qr (?:de facturacion )?no se pudo (?:leer|decodificar)',
    )))


def missing_portal_only_uncertainty(message: str) -> bool:
    """A verified custom URL resolves an absent-link note, not a destination warning."""
    return bool(re.fullmatch(
        r'(?:no aparece|no se (?:encontro|identifico|leyo)|falta) (?:una |un |la |el )?'
        r'(?:liga|enlace|url|portal|sitio)(?: de facturacion)?', plain_words(message)))


def reconcile_uncertainties(messages: list[str], *, payment_confirmed: bool,
                            catalog_provider: str, custom_portal_confirmed: bool) -> list[str]:
    remaining = []
    for message in messages:
        # The model can put payment, fiscal QR, and other observations in one
        # paragraph. Remove only individually resolved sentences.
        sentences = re.split(r'(?<=[.!?])\s+', message.strip())
        kept = [sentence for sentence in sentences if not (
            (payment_confirmed and payment_only_uncertainty(sentence)) or
            (catalog_provider and catalog_portal_only_uncertainty(sentence, catalog_provider)) or
            (custom_portal_confirmed and missing_portal_only_uncertainty(sentence)))]
        if kept:
            remaining.append(' '.join(kept))
    return remaining


def read_ticket(plan, row, automatic, root, companions=()):
    path = Path(row['source_path'])
    result = AI(plan).extract(path, {k: v['name'] for k, v in CATALOG.items()}, list(companions))
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
            try:
                page = read_local_page(path)
                payment = printed_payment(page['text'])
            except (ValueError, OSError, RuntimeError, PyPdfError):
                pass
    details = result.details
    if 'factur' in page.get('text', '').casefold() and 'factur' not in details.casefold():
        details = (details + ' El original incluye acceso a facturación.').strip()
    receipt = Receipt(provider=result.provider, merchant=result.merchant.strip(), amount=(result.amount or '').strip(),
                      date=date, ticket_number=(result.ticket_number or '').strip(),
                      billing_codes=[code.strip() for code in result.billing_codes if code.strip()],
                      payment_code=payment, details=details, source=row['receipt']['source'])
    sources = {field:'vision_modelo' for field in ('provider','merchant','amount','date','ticket_number',
               'billing_codes','details') if getattr(receipt, field)}
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
    searched_and_verified = False
    if not marked and not billing_url and not known and result.merchant.strip():
        tools.append('busqueda_web')
        try:
            suggested, evidence = AI(plan).discover_portal(result.merchant.strip())
            if suggested:
                receipt.provider, receipt.portal_url = CUSTOM_PORTAL, suggested
                sources['provider'] = sources['portal_url'] = 'busqueda_web'
                searched_and_verified = (merchant_matches_source(result.merchant.strip(), evidence)
                                        and asyncio.run(public_page_links_to(evidence, suggested)))
                if searched_and_verified:
                    tools.append('enlace_publico_comprobado')
                    save_search_proof(root, row, suggested, evidence)
        except (PlanError, httpx.HTTPError, OSError, ValueError, asyncio.TimeoutError):
            pass  # Keep the extracted ticket; a failed public search must not lose it.
    if row.get('auto_stage') in {'offline', 'retry_read'}:
        old = Receipt.model_validate(row['receipt'])
        for field in ('amount', 'date', 'merchant', 'billing_codes', 'ticket_number', 'payment_code'):
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
    elif not billing_url and not known and not searched_and_verified and (other or reported):
        uncertainties.append('Comprueba el sitio del comercio en el original. El destino para facturar sigue sin confirmar.')
    confirmed_portal = searched_and_verified or (receipt.provider == CUSTOM_PORTAL and
                                                  marked == [receipt.portal_url])
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
        'Datos completos. Listo para preparar el portal.' if ready else 'Revisa los datos y guarda la revisión.')
    return receipt, ready, note, {'fields':sources, 'tools':list(dict.fromkeys(tools))}


class TicketAutomation:
    def __init__(self, store, runner, plan, get_profile):
        self.store, self.runner, self.plan, self.get_profile = store, runner, plan, get_profile
        self.settings = store.root / 'automation.json'
        settings = read_json(self.settings, {})
        self.enabled = settings.get('enabled') is True
        self.plan_read_after = settings.get('plan_read_after', 0)
        self.wake = asyncio.Event()
        self.task = None
        self.stopping = False
        self._state = {}
        self._reset()

    def _set(self, phase, key, message):
        self._state = {'current_id': key, 'phase': phase, 'message': message}

    def _reset(self):
        if self.enabled:
            self._set('idle', None, 'Esperando tickets.')
        else:
            self._set('off', None, 'Procesamiento automático desactivado.')

    def status(self):
        return {'enabled': self.enabled, **self._state}

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

    async def read_offline(self, row, claim, *, reason, stage='review', read_after=0):
        try:
            receipt, warning, method = await asyncio.to_thread(local_ticket, row)
        except Exception:
            # A broken document must release its read claim so other uploads keep moving.
            receipt, warning, method = Receipt.model_validate(row['receipt']), 'El lector local no encontró texto legible.', 'error'
        notes = [reason, warning, *review_questions(receipt)]
        note = ' '.join(s.rstrip('.') + '.' for s in notes if s)
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
            raise ValueError('ChatGPT solicitó una espera. Mientras tanto puedes usar la lectura local.')
        claimed = self.store.claim_read(key, automatic)
        if not claimed:
            return self.store.get(key)
        row, claim = claimed
        try:
            if offline:
                return await self.read_offline(row, claim, reason='Lectura local',
                                               stage='offline' if automatic else 'review')
            companions = self.store.companion_paths(key)
            receipt, ready, note, provenance = await asyncio.to_thread(
                read_ticket, self.plan, row, automatic, self.store.root, companions)
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
            return await self.read_offline(row, claim, reason=note, stage=stage, read_after=due)

    def _plan_ready(self, key):
        state = self.plan.status()
        if state['status'] == 'ready' and self.plan_read_after > time.time():
            self._set('waiting_read', key, 'ChatGPT solicitó una espera temporal. La bandeja sigue leyendo los originales localmente.')
            return False
        if state['status'] == 'ready':
            return True
        self._set('waiting_plan', key, state.get('message') or 'Conecta o reactiva tu plan de ChatGPT en Ajustes.')
        return False

    def _prepare(self, row):
        key = row['id']
        if self.store.batch_unread(key):
            self._set('reading', key, 'Esperando la lectura de las demás imágenes de esta carga.')
            return False
        receipt = Receipt.model_validate(row['receipt'])
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
        viewer = self.runner.viewer
        if (self.runner.has_active_jobs() or self.runner.lock.locked() or viewer.channel
                or viewer.dialog or viewer.file_request):
            self._set('waiting_browser', key, 'Esperando a que se libere el navegador.')
            return False
        # No await between checking ownership and calling schedule, which atomically claims
        # the job in SQLite and registers its task before yielding control to other handlers.
        self.runner.schedule(key, profile, auto_issue=False,
                             confirmed_url=receipt.portal_url if confirmed_portal else '')
        self._set('queued', key, 'En cola para preparar el portal. La emisión requiere tu aprobación.')
        return True

    async def step(self):
        if not self.enabled:
            return False
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
        ready = self.store.auto_candidates('ready')
        if ready and self._prepare(ready):
            return True
        plan_ready = self.plan.status()['status'] == 'ready' and self.plan_read_after <= time.time()
        incoming = self.store.auto_candidates('review', stage='retry_read', due_only=True) if plan_ready else None
        incoming = incoming or self.store.auto_candidates('new')
        if not incoming and plan_ready:
            incoming = self.store.auto_candidates('review', stage='offline')
        if incoming:
            key = incoming['id']
            if not self._plan_ready(key):
                await self.analyze(key, automatic=True, offline=True)
                return True
            self._set('reading', key, 'Leyendo el ticket con tu plan de ChatGPT.')
            row = await self.analyze(key, automatic=True)
            if self.enabled and row['status'] in ('review', 'blocked', 'duplicate'):
                self._set('review', key, row['message'])
            return True
        if ready:
            return False
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
            else:
                self._reset()
        return False

    async def _work(self):
        while not self.stopping:
            self.wake.clear()
            try:
                if await self.step():
                    await asyncio.sleep(0)
                    continue
            except Exception:
                log.exception('Error al procesar la bandeja de tickets')
                self._set('review', self._state.get('current_id'), 'Revisa el estado del ticket en la bandeja.')
            try:
                await asyncio.wait_for(self.wake.wait(), timeout=2 if self.enabled else None)
            except asyncio.TimeoutError:
                pass
