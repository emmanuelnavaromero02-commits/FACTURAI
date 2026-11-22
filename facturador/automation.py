"""Process the persisted inbox using the same reader and portal runner as the panel."""
from __future__ import annotations

import asyncio
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


def printed_portal_confirmed(row, receipt: Receipt) -> bool:
    """Accept an unfamiliar portal only when the original and the model agree on one printed fiscal URL."""
    if receipt.provider != CUSTOM_PORTAL or not public_portal_origin(receipt.portal_url):
        return False
    try:
        marked, _ = portal_evidence(Path(row['source_path']))
    except (OSError, ValueError, RuntimeError, PyPdfError):
        return False
    return marked == [receipt.portal_url]


def plain_words(value: str) -> str:
    plain = unicodedata.normalize('NFKD', value.casefold())
    return ' '.join(re.findall(r'[a-z0-9]+', ''.join(c for c in plain if not unicodedata.combining(c))))


def read_ticket(plan, row, automatic):
    path = Path(row['source_path'])
    result = AI(plan).extract(path, {k: v['name'] for k, v in CATALOG.items()})
    if not result.is_receipt:
        return None, False, 'El documento no se identificó como ticket de compra. ' + result.details

    try:
        date = Receipt.valid_date((result.date or '').strip())
    except ValueError:
        date = ''
    payment = row.get('confirmed_payment') or ''
    page = {}
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
    receipt = Receipt(provider=result.provider, merchant=result.merchant.strip(), amount=(result.amount or '').strip(),
                      date=date, ticket_number=(result.ticket_number or '').strip(),
                      billing_codes=[code.strip() for code in result.billing_codes if code.strip()],
                      payment_code=payment, details=result.details, source=row['receipt']['source'])
    try:
        page = page or read_local_page(path)
        marked, other = portal_evidence(path, page)
    except (OSError, ValueError, RuntimeError, PyPdfError):
        marked, other = [], []
    reported = getattr(result, 'billing_url', None)
    choices = marked or ([reported] if reported in other else [])
    billing_url = choices[0] if len(choices) == 1 else ''
    known = receipt.provider in CATALOG
    if billing_url and (not known or not allowed_url(billing_url, receipt.provider)):
        receipt.provider, receipt.portal_url = CUSTOM_PORTAL, billing_url
        known = False
    if row.get('auto_stage') in {'offline', 'retry_read'}:
        old = Receipt.model_validate(row['receipt'])
        for field in ('amount', 'date', 'merchant', 'billing_codes', 'ticket_number', 'payment_code'):
            if not getattr(receipt, field):
                setattr(receipt, field, getattr(old, field))
        if receipt.provider == 'unknown' and old.provider != 'unknown':
            receipt.provider, receipt.portal_url = old.provider, old.portal_url
            known = receipt.provider in CATALOG
        if old.payment_code and payment and old.payment_code != payment and not row.get('confirmed_payment'):
            receipt.payment_code = ''
        payment = receipt.payment_code
    uncertainties = list(result.uncertainties)
    if len(choices) > 1:
        uncertainties.append('Se encontraron varios enlaces fiscales impresos. Revisa los destinos en Leer texto, QR y barras.')
    elif not billing_url and not known and (other or reported):
        uncertainties.append('Comprueba el sitio del comercio en el original. El destino para facturar sigue sin confirmar.')
    if payment:
        # Drop an uncertainty solely about an absent payment when the original or owner resolved it.
        uncertainties = [msg for msg in uncertainties if not re.fullmatch(
            r'(?:el ticket no (?:indica|especifica|muestra)|falta(?: confirmar)?|no (?:consta|se indica))\s+'
            r'(?:la )?forma de pago(?: confirmada)?[.!]?', msg.strip(), re.I)]
    confirmed_portal = (receipt.provider == CUSTOM_PORTAL and len(marked) == 1 and
                        marked[0] == receipt.portal_url and reported == receipt.portal_url)
    questions = [*review_questions(receipt, confirmed_portal=confirmed_portal), *uncertainties]

    if automatic and known and not questions:
        # Corroborate the model's choice against the printed merchant, issuer, or official URL.
        try:
            page = page or read_local_page(path)
        except Exception:
            page = {}
        content = ' ' + plain_words(page.get('text', '')) + ' '
        item = CATALOG[receipt.provider]
        printed = any(' ' + plain_words(name) + ' ' in content
                      for name in [*item['name'].split(' / '), *item.get('issuer_rfcs', [])])
        links = portal_suggestions(page.get('codes', []), page.get('text', ''))
        unique_host = any([key for key in CATALOG if allowed_url(link, key)] == [receipt.provider] for link in links)
        if not printed and not unique_host:
            questions.append('Confirma que el comercio del ticket corresponde a ' + item['name'] + '.')
    ready = bool(automatic and (known or confirmed_portal) and not questions)
    note = ' '.join(dict.fromkeys(questions)) if questions else (
        'Datos completos. Listo para preparar el portal.' if ready else 'Revisa los datos y guarda la revisión.')
    return receipt, ready, note


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
            receipt, warning = await asyncio.to_thread(local_ticket, row)
        except Exception:
            # A broken document must release its read claim so other uploads keep moving.
            receipt, warning = Receipt.model_validate(row['receipt']), 'El lector local no encontró texto legible.'
        notes = [reason, warning, *review_questions(receipt)]
        note = ' '.join(s.rstrip('.') + '.' for s in notes if s)
        return self.store.save_receipt(row['id'], receipt, claim=claim, note=note,
                                       read_stage=stage, read_after=read_after)

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
            receipt, ready, note = await asyncio.to_thread(read_ticket, self.plan, row, automatic)
            if receipt is None:
                return self.store.finish_read(key, claim, 'blocked', note)
            return self.store.save_receipt(key, receipt, ready, claim=claim, note=note)
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
        receipt = Receipt.model_validate(row['receipt'])
        confirmed_portal = printed_portal_confirmed(row, receipt)
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
            questions = review_questions(receipt, confirmed_portal=printed_portal_confirmed(previous, receipt))
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
