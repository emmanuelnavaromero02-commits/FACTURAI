"""Process the persisted inbox using the same reader and portal runner as the panel."""
from __future__ import annotations

import asyncio
import logging
import re
import unicodedata
from pathlib import Path

from .ai import AI
from .auth import PlanError, read_json, write_private
from .catalog import CATALOG, CUSTOM_PORTAL, allowed_url, portal_suggestions, public_portal_origin
from .local_reader import read_local_page
from .models import Receipt
from .receipt_evidence import local_ticket, printed_payment

log = logging.getLogger(__name__)


def review_questions(receipt: Receipt) -> list[str]:
    errors = receipt.ready_errors()
    if not receipt.amount:
        errors = ['Falta importe' if m == 'Importe inválido' else m for m in errors]
    if receipt.provider == CUSTOM_PORTAL and receipt.portal_url:
        errors.append('Confirma el dominio del portal antes de compartir tus datos fiscales: '
                      + (public_portal_origin(receipt.portal_url) or 'revisa la dirección impresa'))
    elif receipt.provider not in CATALOG and receipt.provider != CUSTOM_PORTAL:
        errors.append('Indica el portal de facturación que aparece en el ticket.')
    return errors


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
            except (ValueError, OSError, RuntimeError):
                pass
    receipt = Receipt(provider=result.provider, merchant=result.merchant.strip(), amount=(result.amount or '').strip(),
                      date=date, ticket_number=(result.ticket_number or '').strip(),
                      billing_codes=[code.strip() for code in result.billing_codes if code.strip()],
                      payment_code=payment, details=result.details, source=row['receipt']['source'])
    reported = getattr(result, 'billing_url', None)
    billing_url = reported if public_portal_origin(reported) else ''
    known = receipt.provider in CATALOG
    if billing_url and (not known or not allowed_url(billing_url, receipt.provider)):
        receipt.provider, receipt.portal_url = CUSTOM_PORTAL, billing_url
        known = False
    if row.get('auto_stage') == 'offline':
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
    if payment:
        # Drop an uncertainty solely about an absent payment when the original or owner resolved it.
        uncertainties = [msg for msg in uncertainties if not re.fullmatch(
            r'(?:el ticket no (?:indica|especifica|muestra)|falta(?: confirmar)?|no (?:consta|se indica))\s+'
            r'(?:la )?forma de pago(?: confirmada)?[.!]?', msg.strip(), re.I)]
    questions = [*review_questions(receipt), *uncertainties]

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
    ready = bool(automatic and known and not questions)
    note = ' '.join(dict.fromkeys(questions)) if questions else (
        'Datos completos. Listo para preparar el portal.' if ready else 'Revisa los datos y guarda la revisión.')
    return receipt, ready, note


class TicketAutomation:
    def __init__(self, store, runner, plan, get_profile):
        self.store, self.runner, self.plan, self.get_profile = store, runner, plan, get_profile
        self.settings = store.root / 'automation.json'
        self.enabled = read_json(self.settings, {}).get('enabled') is True
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
        write_private(self.settings, {'enabled': enabled})
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

    async def read_offline(self, row, claim, *, reason, retry_when_connected):
        try:
            receipt, warning = await asyncio.to_thread(local_ticket, row)
        except Exception:
            # A broken document must release its read claim so other uploads keep moving.
            receipt, warning = Receipt.model_validate(row['receipt']), 'El lector local no encontró texto legible.'
        notes = [reason, warning, *review_questions(receipt)]
        note = ' '.join(s.rstrip('.') + '.' for s in notes if s)
        return self.store.save_receipt(row['id'], receipt, claim=claim, note=note,
                                       read_stage='offline' if retry_when_connected else 'review')

    async def analyze(self, key: str, automatic=False, offline=False):
        claimed = self.store.claim_read(key, automatic)
        if not claimed:
            return self.store.get(key)
        row, claim = claimed
        try:
            if offline:
                return await self.read_offline(row, claim, reason='Lectura local',
                                               retry_when_connected=automatic)
            receipt, ready, note = await asyncio.to_thread(read_ticket, self.plan, row, automatic)
            if receipt is None:
                return self.store.finish_read(key, claim, 'blocked', note)
            return self.store.save_receipt(key, receipt, ready, claim=claim, note=note)
        except Exception as exc:
            # A persisted quota/authentication hold can be cleared from Settings. Ambiguous
            # transport failures may already have consumed a reading and require a manual retry.
            hold = isinstance(exc, PlanError) and self.plan.status()['status'] != 'ready'
            note = str(exc) if isinstance(exc, PlanError) else 'No se pudo completar la lectura. Revisa el original o vuelve a leerlo.'
            return await self.read_offline(row, claim, reason=note, retry_when_connected=automatic and hold)

    def _plan_ready(self, key):
        state = self.plan.status()
        if state['status'] == 'ready':
            return True
        self._set('waiting_plan', key, state.get('message') or 'Conecta o reactiva tu plan de ChatGPT en Ajustes.')
        return False

    def _prepare(self, row):
        key = row['id']
        receipt = Receipt.model_validate(row['receipt'])
        questions = review_questions(receipt)
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
        self.runner.schedule(key, profile, auto_issue=False)
        self._set('queued', key, 'En cola para preparar el portal. La emisión requiere tu aprobación.')
        return True

    async def step(self):
        if not self.enabled:
            return False
        # Explain already extracted tickets without another model request or a field change.
        previous = self.store.auto_candidates('review')
        if previous:
            questions = review_questions(Receipt.model_validate(previous['receipt']))
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
        incoming = self.store.auto_candidates('new')
        if not incoming and self.plan.status()['status'] == 'ready':
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
            pending = self.store.auto_candidates('review', stage='review') or self.store.auto_candidates('ready', stage='review')
            offline = self.store.auto_candidates('review', stage='offline')
            if pending:
                self._set('review', pending['id'], pending['message'])
            elif offline:
                self._set('waiting_plan', offline['id'], offline['message'])
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
