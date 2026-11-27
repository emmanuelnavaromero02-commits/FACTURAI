"""Walmart's own steps around the general engine: its notices, its ticket lookup and its payment page.

Everything here used to live inside the runner's loop. It runs only for Walmart tickets.
"""
import re
from urllib.parse import urlsplit

from playwright.async_api import Error as BrowserError

from .complementos import DONE, GO, Complement, observe
from .models import SAT_PAYMENT_LABELS
from .portals import normalize
from .walmart_issue import (walmart_lookup_continue, walmart_lookup_proof,
                            walmart_lookup_rejection, walmart_payment_issue,
                            walmart_payment_options, walmart_select_payment)

# Still imported from the engine module by the tests of these notices.
ENGINE_NAMES = ('dismiss_walmart_cfdi_notice', 'dismiss_walmart_address_notice')
WALMART_FINAL = re.compile(r'CONTINUAR|ACEPTAR|FACTURAR|GENERAR(?: FACTURA)?|EMITIR(?: FACTURA)?|TIMBRAR|'
                           r'CONFIRMAR|FINALIZAR|SIGUIENTE')
ADDRESS = re.compile(r'^https://facturacion-clientes\.walmart\.com/address(?:[/?#].*)?$')


async def dismiss_walmart_cfdi_notice(page, snapshot: str) -> bool:
    """Close Walmart's informational CFDI 4.0 notice, never a form confirmation."""
    parts = urlsplit(page.url)
    if parts.scheme != 'https' or parts.netloc.lower() != 'facturacion-clientes.walmart.com':
        return False
    if not (re.search(r'CFDI\s+VERSION\s+4\.0', snapshot, re.I)
            and re.search(r'datos fiscales obligatorios[\s\S]{0,130}validados por el SAT', snapshot, re.I)):
        return False
    heading = page.get_by_text('CFDI VERSION 4.0', exact=True)
    try:
        if await heading.count() != 1 or not await heading.is_visible():
            return False
        notice = heading.locator(
            'xpath=ancestor::*[@role="dialog" or contains(concat(" ", normalize-space(@class), " "), " modal ")][1]')
        if await notice.count() != 1 or not await notice.is_visible():
            return False
        button = notice.get_by_role('button', name='Aceptar', exact=True)
        if (await notice.locator('input:visible, textarea:visible, select:visible').count()
                or await button.count() != 1 or not await button.is_visible()
                or not await button.is_enabled()):
            return False
        await button.click(timeout=1500)
    except BrowserError:
        return False
    return True


async def dismiss_walmart_address_notice(page, snapshot: str) -> bool:
    """Acknowledge Walmart's data-entry notice, scoped away from the fiscal form."""
    parts = urlsplit(page.url)
    if (parts.scheme != 'https' or parts.netloc.lower() != 'facturacion-clientes.walmart.com'
            or parts.path.rstrip('/') != '/address'):
        return False
    message = re.compile(
        r'Para proteger sus datos personales[\s\S]{0,230}capturar nuevamente su informaci[oó]n fiscal'
        r'[\s\S]{0,90}cada solicitud de facturaci[oó]n', re.I)
    if not message.search(snapshot):
        return False
    try:
        notices = page.locator('[role="dialog"]:visible, .modal:visible')
        if await notices.count() != 1:
            return False
        notice = notices.first
        notice_text = await notice.inner_text()
        if (not await notice.is_visible() or not message.search(notice_text)
                or not re.search(r'\bImportante\s*:', notice_text, re.I)
                or await notice.locator('input:visible, textarea:visible, select:visible').count()):
            return False
        button = notice.get_by_role('button', name='Aceptar', exact=True)
        if await button.count() != 1 or not await button.is_visible() or not await button.is_enabled():
            return False
        await button.click(timeout=1500)
    except BrowserError:
        return False
    return True


class WalmartComplement(Complement):
    def __init__(self, *args):
        super().__init__(*args)
        self.lookup = None       # proof of the ticket lookup that reached the fiscal form
        # What is known of the action about to run; set again for every action.
        self.path = ''
        self.payment = False     # the action is the payment page's submit: the emission
        self.candidate = None    # lookup proof read before a «Continuar» decided by the model
        self.issue_url = None
        self.auto = False        # the payment page proved the ticket's payment form

    async def after_reading(self, view):
        page, snapshot = view.page, view.snapshot
        if view.unsent and await dismiss_walmart_cfdi_notice(page, snapshot):
            self.event('notice_dismissed', 'Se cerró el aviso informativo CFDI 4.0 de Walmart.')
            return observe()
        if view.unsent and await dismiss_walmart_address_notice(page, snapshot):
            self.event('notice_dismissed', 'Se cerró el aviso informativo de captura fiscal de Walmart.')
            return observe()
        if self.lookup is not None and urlsplit(page.url).path.rstrip('/') in {'', '/ticket'}:
            self.lookup = None
        return GO

    async def after_fiscal_choices(self, view):
        if await walmart_select_payment(view.page, self.receipt):
            self.event('campo_verificado', 'Forma de pago del ticket elegida en Walmart.')
            return observe()
        return GO

    async def after_ticket_lookup(self, view):
        page, snapshot = view.page, view.snapshot
        if self.lookup is not None:
            return GO
        lookup_step = await walmart_lookup_continue(page, self.profile, self.receipt)
        if lookup_step is None:
            return GO
        proof, control = lookup_step
        bound = await control.element_handle()
        if bound is None:
            raise ValueError('El botón Continuar de Walmart cambió antes de consultar el ticket.')
        try:
            await bound.click(trial=True, timeout=1500)
        except BrowserError as exc:
            raise ValueError('El botón Continuar de Walmart no está disponible para consultar el ticket.') from exc
        checked = await walmart_lookup_continue(page, self.profile, self.receipt)
        if (checked is None or checked[0] != proof or
                await page.locator('body').aria_snapshot() != snapshot):
            return observe()
        before = page.url, snapshot
        try:
            await bound.click(timeout=5000)
        except BrowserError as exc:
            # Chrome may lose the response after the portal receives the lookup.
            # Never repeat a possibly submitted lookup in the same attempt.
            rejected = await walmart_lookup_rejection(page, snapshot)
            if rejected:
                self.event('ticket_lookup_rejected', rejected)
                raise ValueError('Walmart respondió: ' + rejected) from exc
            raise ValueError('Walmart no confirmó el clic de consulta. Revisa el resultado antes de reintentar.') from exc
        view.recent.append({'tipo': 'click', 'control': 'Continuar'})
        self.event('action', 'click · Continuar')
        try:
            await page.wait_for_url(ADDRESS, timeout=8000)
        except BrowserError as exc:
            rejected = await walmart_lookup_rejection(page, snapshot)
            if rejected:
                self.event('ticket_lookup_rejected', rejected)
                raise ValueError('Walmart respondió: ' + rejected) from exc
            raise ValueError('Walmart no confirmó la consulta del ticket. Revisa el mensaje del portal antes de reintentar.') from exc
        self.lookup = proof
        self.event('ticket_lookup_verified',
                   'TC, TR, RFC y código postal coincidieron al continuar.')
        return observe(before=before)

    def adjust_risk(self, view, risk):
        page, action, name = view.page, view.action, view.name
        self.path = urlsplit(page.url).path.rstrip('/')
        self.payment = False
        self.candidate = None
        self.auto = False
        if (view.unsent and action.kind == 'click'
                and self.path not in {'', '/ticket', '/address'}
                and WALMART_FINAL.fullmatch(normalize(name))):
            # After the fiscal form and its «¿Están correctos?» check, Walmart asks the payment
            # form on its own page; that page's submit button is the emission.
            risk = 'issue'
            self.payment = True
        elif (self.path == '/address' and risk in {'confirm', 'issue'}
                and normalize(name) in {'ACEPTAR', 'CONTINUAR'}):
            # Aceptar and the confirmation dialog's Continuar only lead to the payment page.
            risk = 'normal'
        self.issue_url = page.url if self.payment else None
        return risk

    async def check_action(self, view):
        if (view.unsent and self.lookup is None
                and self.path in {'', '/ticket'} and view.action.kind == 'click' and normalize(view.name) == 'CONTINUAR'):
            self.candidate = await walmart_lookup_proof(view.page, self.profile, self.receipt)
            if self.candidate is None:
                return observe(error='Antes de continuar, coteja RFC, código postal, TC y TR con el ticket.')
        return GO

    async def before_issue(self, view):
        runner, key, page, receipt = self.runner, self.key, view.page, self.receipt
        if (self.payment and key not in runner.explicit_approval
                and not await walmart_payment_issue(page, receipt, self.lookup)):
            # Walmart does not show the ticket's payment form chosen: ask once, live, with its options.
            # The question goes through the run's register like any other: it is never sent twice,
            # and if it is still needed without an answer the ticket waits for the person once.
            options = await walmart_payment_options(page)
            question = ('En la página de pago de Walmart no puedo elegir la forma de pago '
                        'del ticket (clave SAT ' + receipt.payment_code + ' ' +
                        SAT_PAYMENT_LABELS.get(receipt.payment_code, '') + '). Opciones que muestra: ' +
                        (', '.join(options) or 'no veo la lista') + '. ¿Cuál elijo?')
            if runner.can_ask(key, question):
                answer, decision = await runner.ask_once(key, page, question)
                if decision == 'manual':
                    await runner.manual_job(key, page, self.profile)
                    return DONE
                return observe(active=True,
                               note=answer or 'Sin respuesta: elige la opción equivalente a la clave del expediente.',
                               error=('Elige en la página de pago la forma de pago del expediente o la que indicó el titular '
                                      'y vuelve a pulsar el botón.'))
        return GO

    async def issue_proof(self, view):
        self.auto = (self.payment and
                     await walmart_payment_issue(view.page, self.receipt, self.lookup))
        return self.auto

    async def before_recording(self, view):
        if (self.auto and (view.page.url != self.issue_url
                or await view.page.locator('body').aria_snapshot() != view.snapshot)):
            return observe(error='El formulario Walmart cambió antes de enviar. Se vuelve a cotejar.')
        return GO

    async def after_acting(self, view):
        if (self.candidate is not None and
                urlsplit(view.page.url).path.rstrip('/') != '/address'):
            try:
                await view.page.wait_for_url(ADDRESS, timeout=5000)
            except BrowserError:
                pass

    def after_logging(self, view):
        if (self.candidate is not None and
                urlsplit(view.page.url).path.rstrip('/') == '/address'):
            self.lookup = self.candidate
            self.event('ticket_lookup_verified', 'TC, TR, RFC y código postal coincidieron al continuar.')
