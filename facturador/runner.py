from __future__ import annotations

import asyncio
import hashlib
import html
import ipaddress
import json
import os
import re
import secrets
import sys
import traceback
import weakref
from decimal import Decimal, InvalidOperation
import unicodedata
from pathlib import Path
from urllib.parse import urljoin, urlsplit
from playwright.async_api import Error as BrowserError

from .ai import AI, read_receipt
from .account_flow import PortalAccountFlow, SECRET_SCREEN
from .auth import ChatGPTPlan, PlanError
from .brand_entry import enter_bp_franchise, BP_HOME
from .bp_recipient import prepare_bp_rfc
from .bp_reprint import recover_bp_reprint
from .bp_ticket import BPAlreadyBilled, BPProgress, at_form as bp_at_form, bp_issue_proof, evidence_from_original, prepare_bp_ticket
from .catalog import (CATALOG, CUSTOM_PORTAL, allowed_url, billing_destination, provider_for_url,
                      public_portal_origin, resolves_publicly)
from .captcha_transport import WidgetTransport, widget_origin
from .cfdi import UUID_RE
from .db import Store
from .documents import job_candidates, reconcile
from .fiscal_choices import NAMES as FISCAL_FIELDS, fiscal_choices
from .models import Action, Profile, Receipt, Target
from .personal_browser import PersonalBrowser
from .profile_lock import close_context, launch_chrome
from .portals import capufe_proof, capufe_recipient, normalize
from .ticket_lookup import prefill_ticket_lookup, prepare_tesoro_lookup
from .ticket_retry import FINAL, REOBSERVE, RecoveryRequested, may_have_issued, open_ticket_portal, recover_ticket, retry_action, ticket_in_result
from .tesoro_consulta import existing_invoice_folio, recover_tesoro_consulta
from .tesoro_recipient import tesoro_recipient
from .tesoro_result import tesoro_verification_failure
from .viewer import BrowserView
from .vianney_consulta import lookup_rejected, rejection_for_current_ticket, recover_vianney_consulta
from .walmart_issue import (walmart_issue_button, walmart_lookup_continue,
                           walmart_lookup_proof, walmart_lookup_rejection)

ISSUE = re.compile(r'\b(facturar|emitir|timbrar|generar\s+(?:mi\s+)?(?:factura|cfdi)|solicitar\s+factura|enviar\s+(?:mi\s+)?(?:factura|cfdi)|s[ií] estoy seguro|confirmar (?:la )?emisi[oó]n)\b', re.I)
EMISSION_PATH = re.compile(r'factur|invoice|invoic|cfdi|timbr|emisi|emit|creat|complet|generat|submit', re.I)
FORBIDDEN = re.compile(r'\b(pagar|comprar|transferir|cancelar\s+factura|eliminar|borrar)\b', re.I)
TERMS = re.compile(r'acepto|términos|terminos|condiciones|contrato|privacidad', re.I)
CHALLENGE = re.compile(r'captcha|verificaci[oó]n de seguridad|no soy un robot', re.I)
CHALLENGE_TEXT = re.compile(r'no soy un robot|verifica que eres humano|verify (?:that )?you are human|introdu(?:ce|zca) el captcha|c[oó]digo (?:de )?captcha', re.I)
ACCESS_DENIED = re.compile(r'access denied|error 16\b|blocked by our security service|acceso denegado|err_cert_', re.I)
LOAD_FAILURE = re.compile(r'net::ERR_|(?:error|HTTP)\s*[45]\d\d\b|service unavailable|no se (?:puede acceder|pudo cargar)|bad gateway', re.I)
FISCAL_INPUT = re.compile(r'\b(?:textbox|combobox|spinbutton|radio)\s+"[^"\n]*\b(?:rfc|régimen|regimen|postal|cfdi|forma de pago|folio|ticket|peaje|razón social|razon social)\b', re.I)
# Failures the agent can retry from a fresh page because nothing was submitted.
RETRYABLE = re.compile(r'se cerr[oó]|cambi[oó] (?:a un|de) dominio|'
                       r'requiere revisar el resultado del portal|no hay comprobante ni confirmaci[oó]n visible|'
                       r'no avanz[oó]|no est[aá] disponible|no confirm[oó] el clic|destino fuera', re.I)
# Visible text plus a numbered list of every visible field: its label (or the text
# right before it when the page has no label), what is typed or chosen, and its errors.
# Each field gets data-facturai-campo=n so the agent can target it as by="field".
PAGE_TEXT = r"""(start) => {
  const clean = s => (s || '').replace(/\s+/g, ' ').trim();
  const shown = e => e.type !== 'password' && e.type !== 'hidden' &&
    !!(e.offsetWidth || e.offsetHeight || e.getClientRects().length);
  const near = e => {
    let node = e;
    for (let depth = 0; node && depth < 4; depth++, node = node.parentElement) {
      let prev = node.previousElementSibling;
      while (prev) {
        const t = clean(prev.innerText || prev.textContent);
        if (t) return t.slice(-80);
        prev = prev.previousElementSibling;
      }
    }
    return '';
  };
  const items = [...document.querySelectorAll('input, textarea, select')].filter(shown);
  const fields = items.map((e, i) => {
    const n = start + i;
    e.setAttribute('data-facturai-campo', String(n));
    const ref = e.getAttribute('aria-labelledby');
    const named = ref && document.getElementById(ref);
    const label = clean((e.labels && e.labels[0] && e.labels[0].innerText) || e.getAttribute('aria-label') ||
      (named && named.innerText) || e.placeholder || near(e) || e.name || e.id);
    const kind = e.tagName === 'SELECT' ? 'lista' : e.tagName === 'TEXTAREA' ? 'texto' : (e.type || 'text');
    const value = e.tagName === 'SELECT' ? (e.selectedOptions[0] ? clean(e.selectedOptions[0].text) : '') :
      (e.type === 'checkbox' || e.type === 'radio') ? (e.checked ? 'marcado' : 'sin marcar') : e.value;
    const notes = [e.required ? 'obligatorio' : '', e.disabled ? 'deshabilitado' : '',
                   e.validationMessage ? 'error: ' + clean(e.validationMessage) : ''].filter(Boolean);
    return '#' + n + ' ' + kind + ' «' + label.slice(0, 80) + '»: ' + String(value).slice(0, 120) +
      (notes.length ? ' (' + notes.join(', ') + ')' : '');
  });
  return {text: document.body ? document.body.innerText : '', fields: fields, count: items.length};
}"""
ASK_TIMEOUT = 300
MAX_QUESTIONS = 3
IDLE_BEFORE_ASK = 10
TOOLS = {'read_text', 'search_web', 'view_ticket', 'back', 'reload', 'upload'}
SUCCESS = re.compile(r'factura\s+(?:emitida|generada)\b|cfdi\s+(?:emitido|generado)\b|timbrad[ao]\s+correctamente|resultado facturaci[oó]n[^\n]*\n[\s\S]{0,130}facturas generadas|se gener[oó]\s+exitosamente', re.I)
DOWNLOAD = re.compile(r'descargar|download|\bpdf\b|\bxml\b|imprimir', re.I)
ALREADY_BILLED = re.compile(
    r'^(?:(?:el|este|su) )?(?:ticket|boleto|c[oó]digo|comprobante)(?: ingresado| [A-Z0-9]{18})? '
    r'ya (?:(?:fue|ha sido|est[áa]|se encuentra) )?facturad[oa]\b', re.I)
RECOVERY_LINK = re.compile(r'\b(?:recuperar|consultar|reencontrar)\b.*\b(?:factura|cfdi|comprobante)\b', re.I)
TESORO_HOME = re.compile(r'opciones de facturaci[oó]n', re.I)
TESORO_SINGLE = re.compile(r'generar factura de estaci[oó]n y znak', re.I)


def already_billed(snapshot: str) -> bool:
    for line in snapshot.splitlines():
        message = re.sub(r'^\s*-?\s*(?:(?:alert|status|text|generic|paragraph|heading)\s*[:"]\s*)?', '', line).strip('" ')
        if ALREADY_BILLED.match(message):
            return True
    return False
MANUAL = {'captcha', 'login', 'terms', 'unknown', 'blocked', 'confirm', 'attachment', 'operator'}
# The agent keeps working on its own until these limits; only then it asks the person.
MAX_STEPS = 120
SELF_HELP_LIMIT = 4
PLAN_RETRIES = 3
REOBSERVE_DELAY = 3


def action_risk(action: Action) -> str:
    """Classify an action. Only payments, credentials and CAPTCHA belong to the person.

    Terms checkboxes, privacy notices, generic «Aceptar» buttons and Enter are part
    of every billing form; the agent handles them. A generic confirmation is
    reported as ``confirm`` so the runner can send it through the emission gate
    when the page already shows the ticket being invoiced.
    """
    name = action.target.name if action.target else ''
    if FORBIDDEN.search(name):
        return 'forbidden'
    if action.risk in {'captcha', 'login'}:
        return action.risk
    if CHALLENGE.search(name):
        return 'captcha'
    if (action.kind in {'click', 'press'} and ISSUE.search(name)) or action.risk == 'issue':
        return 'issue'
    if action.kind == 'press' and (action.value or '').lower() in {'enter', 'return'} and name.strip().upper() != 'RFC':
        return 'confirm'
    if action.kind == 'click' and re.fullmatch(r'aceptar|confirmar|enviar|submit|s[ií]', name.strip(), re.I):
        return 'confirm'
    return 'normal'


def press_without_target(action: Action) -> bool:
    return action.kind == 'press' and (action.target is None or not action.target.name.strip())


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


def private_destination(url: str) -> bool:
    hostname = urlsplit(url).hostname or ''
    if hostname and ('.' not in hostname and ':' not in hostname or hostname.endswith(('.localhost', '.local', '.internal'))):
        return True
    try:
        return not ipaddress.ip_address(hostname).is_global
    except ValueError:
        return False


async def visible_challenge(page, snapshot: str) -> str | None:
    if any(CHALLENGE_TEXT.search(line) and not ('checkbox' in line and '[checked]' in line)
           for line in snapshot.splitlines()):
        return 'captcha'
    if SECRET_SCREEN.search(snapshot):
        return 'login'
    if hasattr(page, 'frames'):
        credential_inputs = ('input[type="password"]:visible, input[autocomplete~="one-time-code"]:visible, '
                             'input[autocomplete~="username"]:visible, input[name*="otp" i]:visible')
        if any(re.search(r'\b(?:contraseña temporal|temporary password|verification code|código de (?:verificación|acceso)|one.time (?:code|password))\b', line, re.I)
               for line in snapshot.splitlines()):
            return 'login'
        for frame in page.frames:
            if await frame.locator(credential_inputs).count():
                return 'login'
        frames = page.locator('iframe[src*="/bframe"]:visible, iframe[title*="challenge" i]:visible, iframe[title*="desafío" i]:visible')
        if await frames.count():
            return 'captcha'
        for frame in page.frames[1:]:
            if widget_origin(frame.url):
                unchecked = frame.locator('[role="checkbox"][aria-checked="false"]:visible, input[type=checkbox]:not(:checked):visible')
                if await unchecked.count() and await (await frame.frame_element()).is_visible():
                    return 'captcha'
        if await page.locator('input[type="file"][required]:invalid:visible').count():
            return 'attachment'
    return None


class Runner:
    def __init__(self, store: Store, plan: ChatGPTPlan | None = None):
        self.store = store
        self.plan = plan or ChatGPTPlan(store.root)
        self.owns_plan = plan is None
        self.viewer = BrowserView()
        self.widgets = WidgetTransport()
        self.personal = PersonalBrowser(store.root, lambda download: self.track_download(download, None))
        self.personal_at_revision = None
        self.context = None
        self.engine = ''
        self.playwright = None
        self.lock = asyncio.Lock()
        self.browser_start = asyncio.Lock()
        self.pending: dict[str, dict] = {}
        self.job_tasks: dict[str, asyncio.Task] = {}
        self.retries = asyncio.Lock()
        self.explicit_approval = set()
        self.captcha_returned = set()
        self.downloads: set[asyncio.Task] = set()
        self.page_job = {}
        self.lost_pages = weakref.WeakSet()
        self.active_id: str | None = None
        self.whatsapp = None
        self.portal_page = None
        self.page_portal_origins = weakref.WeakKeyDictionary()
        # Billing operators a merchant portal redirected to during each ticket.
        self.job_origins: dict[str, set[str]] = {}
        # What failed in the previous attempt, so the next one does not repeat it.
        self.session_hints: dict[str, str] = {}
        # Tickets where a generic «Aceptar»/«Enviar» may have submitted something.
        self.soft_submitted: set[str] = set()
        # Questions the agent asked in the chat, answered by the person's next message.
        self.questions: dict[str, asyncio.Future] = {}
        self.manual_download_error = ''

    async def start_browser(self):
        async with self.browser_start:
            if self.context:
                return
            from playwright.async_api import async_playwright

            settings = {'accept_downloads': True, 'service_workers': 'block',
                        'viewport': {'width': 1280, 'height': 850}, 'timeout': 15_000}
            headed_chrome = os.getenv('FACTURADOR_CHROME_HEADED') == '1'
            try:
                if self.playwright is None:
                    self.playwright = await async_playwright().start()
                if headed_chrome:
                    self.context = await launch_chrome(self.playwright.chromium.launch_persistent_context,
                        self.store.root / 'browser_profile_chrome', executable_path='/usr/bin/google-chrome-stable',
                        headless=False, chromium_sandbox=True, **settings)
                    self.engine = 'Google Chrome'
                else:
                    try:
                        self.context = await self.playwright.chromium.launch_persistent_context(
                            self.store.root / 'browser_profile', executable_path=os.getenv('FACTURADOR_CHROMIUM_PATH'), headless=True,
                            chromium_sandbox=True, **settings)
                        self.engine = 'Chromium'
                    except BrowserError:
                        # Keep interception available in Firefox; Playwright blocks worker registration separately.
                        self.context = await self.playwright.firefox.launch_persistent_context(
                            self.store.root / 'browser_profile_firefox', headless=True,
                            firefox_user_prefs={'dom.serviceWorkers.enabled': True}, **settings)
                        self.engine = 'Firefox'
            except Exception as exc:
                if self.playwright is not None:
                    try:
                        await self.playwright.stop()
                    except BrowserError:
                        pass
                    finally:
                        self.playwright = None
                if headed_chrome:
                    raise ValueError('No se pudo iniciar Google Chrome. Comprueba que Google Chrome estable esté instalado y que la pantalla gráfica esté disponible.') from exc
                raise ValueError('No se pudo iniciar el navegador interno. Revisa los navegadores instalados o reconstruye el contenedor.') from exc
            self.context.set_default_timeout(7000)
            def context_closed(context):
                if self.context is context:
                    self.context = None
                    self.engine = ''
            self.context.on('close', context_closed)
            self.context.on('page', self.register_page)
            for page in self.context.pages:
                self.register_page(page)
            await self.context.route('**/*', self.guard_navigation)

    def register_page(self, page):
        async def widget_frame_arrived(frame):
            pending = self.pending.get(self.page_job.get(page))
            if (self.engine == 'Google Chrome' and pending and pending['page'] is page
                    and pending.get('challenge_seen') and pending['kind'] == 'captcha' and page in self.page_portal_origins):
                await self.widgets.arm(frame)
        def observe_manual_request(request):
            pending = self.pending.get(self.page_job.get(page))
            if (not pending or pending['kind'] not in MANUAL or pending['page'] is not page
                    or pending['control_epoch'] == self.viewer.control_epoch
                    or request.resource_type not in {'document', 'fetch', 'xhr'}):
                return
            target = urlsplit(request.url)
            if target.netloc != urlsplit(pending['url']).netloc:
                if request.method not in {'GET', 'HEAD', 'OPTIONS'} and not widget_origin(request.url):
                    pending['uncertain_request'] = True
                return
            if (pending['kind'] == 'captcha' and request.method in {'GET', 'HEAD', 'OPTIONS'}
                    and not EMISSION_PATH.search(target.path)):
                # A solved challenge commonly redirects to another page of the
                # same merchant. Reads alone do not prove an invoice was sent.
                return
            # While helping with an account gate the operator may reach billing
            # themselves. Only known account endpoints are attributable to that gate.
            path = target.path.rstrip('/').split('/')[-1].split('.')[0]
            if pending['kind'] == 'login' and re.fullmatch(
                    r'login|signin|sign-in|registro|register|signup|activar|activate|verificar|verificacion|callback|autenticar', path, re.I):
                return
            if pending['kind'] == 'captcha' and re.search(
                    r'/(?:validar[-_]?captcha|verificar[-_]?captcha|(?:re)?captcha/(?:verify|validate|verificar|validar))/?$', target.path, re.I):
                return
            pending['uncertain_request'] = True

        def on_download(download):
            key = self.page_job.get(page)
            if key or (page is self.portal_page and page is self.viewer.page and self.viewer.manual):
                self.track_download(download, key)

        async def on_popup(popup):
            key = self.page_job.get(page)
            if page in self.page_portal_origins:
                self.page_portal_origins[popup] = self.page_portal_origins[page]
            if key and key == self.active_id:
                self.page_job[popup] = key
                await self.viewer.show(popup, self.viewer.title, manual=self.viewer.manual)
            elif self.viewer.page is page and self.viewer.manual and not self.has_active_jobs():
                if page is self.portal_page:
                    self.portal_page = popup
                await self.viewer.show(popup, self.viewer.title, manual=True)

        async def on_dialog(dialog):
            await self.viewer.receive_dialog(page, dialog)

        async def on_file_request(chooser):
            if self.active_id:
                ticket = self.store.get(self.active_id)['receipt']
                approved = allowed_url(page.url, ticket['provider'], ticket.get('portal_url', ''), self._extra())
            elif page in self.page_portal_origins:
                approved = public_portal_origin(page.url) == self.page_portal_origins[page]
            else:
                approved = any(allowed_url(page.url, key) for key in CATALOG) or urlsplit(page.url).netloc == 'web.whatsapp.com'
            if approved:
                await self.viewer.receive_file_request(page, chooser)

        page.on('download', on_download)
        page.on('request', observe_manual_request)
        page.on('framenavigated', widget_frame_arrived)
        page.on('filechooser', on_file_request)
        page.on('framenavigated', lambda frame: self.viewer.invalidate_file_request(page, frame))
        page.on('popup', on_popup)
        page.on('dialog', on_dialog)
        def page_lost(_):
            self.lost_pages.add(page)
            owner = self.page_job.pop(page, None)
            pending = self.pending.get(owner)
            if self.viewer.page is page and self.viewer.dialog:
                self.viewer.dialog = None
                self.viewer.dialog_key = ''
            if pending and self.viewer.page is page and not pending['future'].done():
                pending['future'].set_exception(ValueError('Se cerró la página del trámite.'))

        page.on('crash', page_lost)
        page.on('close', page_lost)

    async def guard_navigation(self, route):
        request = route.request
        if urlsplit(request.url).scheme not in {'https', 'blob'} or private_destination(request.url):
            await route.abort()
            return
        try:
            frame = request.frame
            owner = frame.page
            main_frame = frame == owner.main_frame
        except BrowserError:
            owner, main_frame = None, True
        ticket = self.store.get(self.active_id)['receipt'] if self.active_id else None
        extra = self.job_origins.get(self.active_id, set()) if self.active_id else set()
        if (ticket and main_frame and request.is_navigation_request()
                and not allowed_url(request.url, ticket['provider'], ticket.get('portal_url', ''), extra)
                and billing_destination(request.url) and await resolves_publicly(request.url)):
            # The merchant portal handed the ticket to its invoicing operator. Follow it
            # for this ticket only; the agent itself still cannot «goto» arbitrary sites.
            origin = public_portal_origin(request.url)
            self.job_origins.setdefault(self.active_id, set()).add(origin)
            extra = self.job_origins[self.active_id]
            self.store.event(self.active_id, 'portal_redirect', 'El portal continuó en su operador de facturación: ' + origin)
        custom_origin = self.page_portal_origins.get(owner) if owner is not None else None
        if ticket and ticket['provider'] == CUSTOM_PORTAL:
            custom_origin = public_portal_origin(ticket.get('portal_url', ''))
        if custom_origin and urlsplit(request.url).scheme == 'https':
            if not await resolves_publicly(request.url):
                await route.abort()
                return
            if (request.method not in {'GET', 'HEAD', 'OPTIONS'} and public_portal_origin(request.url) != custom_origin
                    and public_portal_origin(request.url) not in extra):
                if main_frame or not await self.widgets.allows(self, request, frame, owner, custom_origin):
                    await route.abort()
                    return
        if request.is_navigation_request():
            if main_frame and ticket:
                if not allowed_url(request.url, ticket['provider'], ticket.get('portal_url', ''), extra):
                    self.store.event(self.active_id, 'navigation_blocked', 'Navegación fuera del portal registrado')
                    await route.abort()
                    return
            elif main_frame and custom_origin:
                if public_portal_origin(request.url) not in {custom_origin, *extra}:
                    await route.abort()
                    return
            elif main_frame and not (any(allowed_url(request.url, key) for key in CATALOG)
                                     or urlsplit(request.url).netloc == 'web.whatsapp.com'):
                await route.abort()
                return
        await route.continue_()

    def track_download(self, download, key):
        task = asyncio.create_task(self.save_download(download, key))
        self.downloads.add(task)
        task.add_done_callback(self.downloads.discard)

    async def save_download(self, download, key: str | None):
        directory = self.store.root / 'downloads' / key if key else self.store.root / 'descargas-navegador'
        directory.mkdir(parents=True, exist_ok=True, mode=0o700)
        original = Path(download.suggested_filename.replace('\\', '/')).name
        suffix = Path(original).suffix.lower()
        if suffix not in {'.pdf', '.xml', '.zip'}:
            await download.cancel()
            return
        stem = re.sub(r'[^\w.()-]', '_', Path(original).stem) or 'comprobante'
        # Bound the UTF-8 byte length as well as the character count for the filesystem.
        name = stem.encode('utf-8')[:145].decode('utf-8', errors='ignore') + suffix
        from tempfile import NamedTemporaryFile

        with NamedTemporaryFile(dir=directory, prefix='pending_', delete=False) as stream:
            temporary = Path(stream.name)
        try:
            await download.save_as(temporary)
            if temporary.stat().st_size > 50 * 1024 * 1024:
                raise ValueError('Descarga supera 50 MB')
            digest = hashlib.sha256(temporary.read_bytes()).hexdigest()[:16]
            temporary.replace(directory / (digest + '_' + name))
            if key:
                self.store.event(key, 'download', name)
            else:
                self.manual_download_error = ''
        except Exception:
            message = 'La descarga del comprobante no terminó.'
            if key:
                self.store.event(key, 'download_error', message)
            else:
                self.manual_download_error = message
        finally:
            temporary.unlink(missing_ok=True)

    def _extra(self) -> set[str]:
        """Invoicing-operator origins the active ticket's portal redirected to."""
        return self.job_origins.get(self.active_id, set()) if self.active_id else set()

    def has_active_jobs(self) -> bool:
        return bool(self.job_tasks)

    @property
    def screen(self):
        if self.personal_at_revision is not None and self.personal.context and (
            self.personal_at_revision == self.viewer.revision or self.personal.interacting
        ):
            return self.personal.view
        return self.viewer

    def browser_state(self):
        if self.screen is self.personal.view:
            return {**self.personal.state(), 'engine': self.engine, 'return_job_id': self.active_id}
        state = self.viewer.state()
        job = self.page_job.get(self.viewer.page) if state['open'] else None
        mode = 'idle' if not state['open'] else 'job' if job else 'whatsapp' if self.viewer.page is self.whatsapp else 'portal'
        return {**state, 'mode': mode, 'engine': self.engine, 'job_id': job, 'return_job_id': None, 'tabs': []}

    async def finish_job_browser(self, key: str):
        """Dismiss a finished ticket; retain only pages needed for result recovery."""
        pages = [page for page, owner in list(self.page_job.items()) if owner == key]
        if not pages:
            return
        showing = self.viewer.page in pages
        if showing:
            personal_revision = self.personal_at_revision == self.viewer.revision
            await self.viewer.hide(self.viewer.page)
            if personal_revision:
                self.personal_at_revision = self.viewer.revision
        row = self.store.get(key)
        if row['status'] not in FINAL and (row['status'] != 'blocked' or may_have_issued(row)):
            return
        if self.downloads:
            await asyncio.wait(tuple(self.downloads), timeout=12)
        for page in pages:
            try:
                if not page.is_closed():
                    await page.close()
            except BrowserError:
                pass
            self.page_job.pop(page, None)
        self.store.event(key, 'browser_closed', 'Se cerró la pestaña del trámite; la sesión del navegador permanece guardada.')

    async def close_parked_job_pages(self, active_key: str):
        """Close parked tabs before opening a fresh ticket session."""
        parked = [(page, owner) for page, owner in list(self.page_job.items())
                  if owner == active_key or not (owner in self.job_tasks and not self.job_tasks[owner].done())]
        if parked and self.downloads:
            await asyncio.wait(tuple(self.downloads), timeout=12)
        for page, owner in parked:
            if self.viewer.page is page and self.viewer.visible:
                await self.viewer.hide(page)
            try:
                if not page.is_closed():
                    await page.close()
            except BrowserError:
                pass
            self.page_job.pop(page, None)
            self.store.event(owner, 'browser_closed', 'Se cerró la pestaña anterior; el expediente conserva el resultado.')

    async def open_personal(self, url=None, channel='', *, action=None, tab_id=None):
        async with self.personal.lock:
            previous = self.screen
            if previous.dialog or previous.file_request:
                raise ValueError('Resuelve el aviso o selector de archivo antes de cambiar de página.')
            if previous.channel and not secrets.compare_digest(previous.channel, channel):
                raise ValueError('La página está bajo el control de otra ventana del panel.')
            if previous is self.viewer:
                await previous.release(channel)
                await self.personal.view.release(self.personal.view.channel)
            revision = self.viewer.revision
            await self.start_browser()
            await self.personal.start(self.playwright, self.engine)
            if action:
                await self.personal.change_tab(action, tab_id)
            else:
                await self.personal.open(url)
            self.personal_at_revision = revision
        return self.browser_state()

    async def return_to_job(self, channel):
        if self.screen is self.personal.view:
            self.personal.check_control(channel)
            await self.personal.view.release(channel)
        self.personal_at_revision = None
        return self.browser_state()

    async def open_portal(self, provider: str | None = None, url: str | None = None, confirmed_origin: str = ''):
        custom_origin = None
        if url is not None:
            provider = provider or provider_for_url(url)
            if provider is None:
                custom_origin = public_portal_origin(url)
                if not custom_origin or confirmed_origin != custom_origin:
                    raise ValueError('Confirma el dominio HTTPS público del comercio antes de abrirlo.')
                if not await resolves_publicly(url):
                    raise ValueError('El dominio debe resolver a una dirección pública accesible.')
            elif not allowed_url(url, provider):
                raise ValueError('La liga del ticket debe pertenecer al portal seleccionado.')
        if provider is not None and provider not in CATALOG:
            raise ValueError('Selecciona un portal del catálogo.')
        if self.has_active_jobs():
            raise ValueError('Hay un trámite en curso en el navegador.')
        async with self.lock:
            if self.has_active_jobs():
                raise ValueError('Hay un trámite en curso en el navegador.')
            await self.start_browser()
            if self.portal_page is None or self.portal_page.is_closed():
                self.portal_page = await self.context.new_page()
            page = self.portal_page
            self.personal_at_revision = None
            await self.viewer.set_manual(False)
            if custom_origin:
                self.page_portal_origins[page] = custom_origin
            else:
                self.page_portal_origins.pop(page, None)
            if provider is None and url is None:
                await page.goto('about:blank')
                links = [f'<li><a href="{html.escape(item["url"], quote=True)}">{html.escape(item["name"])}</a></li>'
                         for item in sorted(CATALOG.values(), key=lambda item: item['name'].casefold())]
                template = (Path(__file__).resolve().parent.parent / 'web' / 'inicio-navegador.html').read_text(encoding='utf-8')
                await page.set_content(template.replace('<!--PORTALES-->', '\n'.join(links)))
                await self.viewer.show(page, 'Portales de facturación', manual=True)
            else:
                item = CATALOG[provider] if provider else {'name': custom_origin, 'url': url}
                try:
                    await page.goto(url or item['url'], wait_until='domcontentloaded', timeout=30_000)
                except Exception as exc:
                    await self.viewer.show(page, item['name'], manual=True)
                    raise ValueError(f'No se pudo cargar {item["name"]}. Revisa la página en el navegador.') from exc
                if custom_origin and public_portal_origin(page.url) != custom_origin:
                    await page.goto('about:blank')
                    await self.viewer.show(page, item['name'], manual=False)
                    raise ValueError('La dirección redirigió a otro dominio. Introduce la dirección final para confirmarla.')
                await self.viewer.show(page, item['name'], manual=True)

    async def open_whatsapp(self):
        if self.has_active_jobs():
            raise ValueError('Termina la facturación en curso para abrir WhatsApp.')
        await self.start_browser()
        if self.whatsapp is None or self.whatsapp.is_closed():
            self.whatsapp = await self.context.new_page()
            await self.whatsapp.goto('https://web.whatsapp.com/', wait_until='domcontentloaded')
        self.personal_at_revision = None
        await self.viewer.show(self.whatsapp, 'WhatsApp Web', manual=True)

    async def capture_whatsapp_media(self) -> list[dict]:
        from .ingest import save_upload

        page = self.whatsapp
        if self.has_active_jobs() or not page or page.is_closed() or urlsplit(page.url).hostname != 'web.whatsapp.com':
            raise ValueError('Abre WhatsApp y selecciona una foto en el visor.')
        await self.viewer.set_manual(False)
        try:
            buttons = page.get_by_role('button', name=re.compile(r'^(Descargar|Download)$'))
            if await buttons.count() != 1 or not await buttons.is_visible():
                raise ValueError('Abre la foto de un ticket en WhatsApp y vuelve a importar.')
            async with page.expect_download(timeout=30000) as info:
                await buttons.click()
            download = await info.value
            path = Path(await download.path())
            if path.stat().st_size > 25 * 1024 * 1024:
                raise ValueError('La foto supera 25 MB.')
            return await asyncio.to_thread(save_upload, self.store, download.suggested_filename, path.read_bytes(), 'WhatsApp')
        finally:
            await self.viewer.set_manual(True)

    def schedule(self, key: str, profile: Profile, auto_issue: bool = False, manual: bool = False, confirmed_url: str = ''):
        if not self.store.queue(key, profile, confirmed_url):
            raise ValueError('Solo se puede encolar un ticket revisado y sin emisión previa.')
        if not self.personal.interacting:
            self.personal_at_revision = None
        self.track_job(key, self.run(key, profile, auto_issue, manual))

    def track_job(self, key, coroutine):
        task = asyncio.create_task(coroutine)
        self.job_tasks[key] = task
        def finished(completed):
            if self.job_tasks.get(key) is completed:
                self.job_tasks.pop(key, None)
        task.add_done_callback(finished)

    async def recover_only(self, key, profile):
        try:
            await recover_ticket(self, key, profile)
            self.prepare_checked_ticket(key, profile)
        except (ValueError, BrowserError, OSError):
            self.store.finish_recovery(key, 'La consulta quedó incompleta. Puedes volver a buscar o adjuntar el PDF y el XML.')
            self.store.event(key, 'recovery_incomplete', 'No se completó la búsqueda de comprobantes.')

    def prepare_checked_ticket(self, key, profile):
        row = self.store.get(key)
        if (row['status'] not in {'uncertain', 'blocked'} or row['issue_attempted'] or not row['human_exposed']
                or self.plan.status()['status'] != 'ready'):
            return False
        receipt = Receipt.model_validate(row['receipt'])
        if receipt.provider != 'capufe' or receipt.ready_errors() or not self.store.queue_after_negative_check(key, receipt, profile):
            return False
        self.track_job(key, self.run(key, profile, auto_issue=False))
        return True

    async def recover_queued(self, key, profile):
        async with self.lock:
            if self.store.get(key)['status'] in FINAL:
                return
            self.active_id = key
            try:
                await self.recover_only(key, profile)
            finally:
                self.active_id = None
                await self.finish_job_browser(key)

    def retry_action(self, row):
        pending = self.pending.get(row['id'])
        action = retry_action(row, pending)
        if action == 'resume' and self.operator_may_have_acted(pending):
            return 'recover'
        return action

    def operator_may_have_acted(self, pending):
        page = pending['page']
        return (pending.get('control_epoch') != self.viewer.control_epoch or self.viewer.page is not page
                or page.is_closed() or page in self.lost_pages or pending.get('url') != page.url)

    async def captcha_outcome(self, key, pending):
        page = pending['page']
        row = self.store.get(key)
        receipt = Receipt.model_validate(row['receipt'])
        if (not pending.get('challenge_seen') or page is not self.viewer.page or page.is_closed()
                or page in self.lost_pages or self.page_job.get(page) != key or self.viewer.dialog
                or public_portal_origin(page.url) != public_portal_origin(pending['url'])
                or not allowed_url(page.url, receipt.provider, receipt.portal_url, self._extra())):
            return None
        try:
            snapshot = await page.locator('body').aria_snapshot(timeout=3000)
            if (not snapshot or ACCESS_DENIED.search(snapshot) or LOAD_FAILURE.search(snapshot)
                    or await visible_challenge(page, snapshot)):
                return None
        except BrowserError:
            return None
        if SUCCESS.search(snapshot) or already_billed(snapshot):
            return 'recover' if ticket_in_result(snapshot, row, receipt) else None
        return 'recover' if may_have_issued(row) or pending.get('uncertain_request') else 'reobserve'

    async def release_control(self, channel):
        key = self.active_id
        pending = self.pending.get(key)
        released = await self.viewer.release(channel)
        await self.personal.view.release(channel)
        if (not released or not pending or pending['kind'] != 'captcha' or pending['future'].done()
                or self.viewer.control_epoch == pending.get('control_epoch')):
            return
        revision, epoch = self.viewer.revision, self.viewer.control_epoch
        outcome = await self.captcha_outcome(key, pending)
        if (outcome and self.pending.get(key) is pending and not pending['future'].done()
                and await self.viewer.finish_returned_control(pending['page'], revision, epoch)):
            if pending['future'].done():
                return
            self.explicit_approval.add(key)
            if outcome == 'reobserve':
                self.captcha_returned.add(key)
            self.store.event(key, 'captcha_reviewed', 'La verificación se completó en la pestaña del ticket. Se volvió a leer el estado del portal.')
            self.decide(key, 'recover' if pending.get('uncertain_request') else outcome)

    async def retry_job(self, key, current_profile=None, *, automatic=False):
        async with self.retries:
            row = self.store.get(key)
            pending = self.pending.get(key)
            operation = self.retry_action(row)
            response = {'id': key, 'status': row['status'], 'action': 'active', 'message': row['message']}
            if automatic and (pending or self.has_active_jobs() or row['status'] not in {'uncertain', 'issued_waiting_files'}):
                return response
            if not automatic:
                self.store.pause_automatic_recovery(key, False)
            if row['status'] in FINAL:
                return {**response, 'action': 'complete'}
            if pending and not pending['future'].done():
                if not operation or self.viewer.dialog or self.viewer.file_request:
                    return {**response, 'action': 'needs_user'}
                # Revoke remote input under its command lock before deciding whether any
                # operator took control. Pending futures are checked again after the await.
                await self.viewer.set_manual(False)
                if self.pending.get(key) is not pending or pending['future'].done():
                    return response
                operation = self.retry_action(self.store.get(key))
                if operation == 'resume':
                    self.decide(key, 'reobserve')
                    return {**response, 'action': 'resumed', 'message': 'Revisando la misma pestaña del ticket.'}
                self.decide(key, 'recover')
                return {**response, 'action': 'recovering', 'message': 'Buscando comprobantes sin volver a emitir.'}
            if key in self.job_tasks and not self.job_tasks[key].done():
                return response
            if not operation:
                return {**response, 'action': 'needs_user'}
            if row['billing_profile']:
                profile = Profile.model_validate_json(row['billing_profile'])
            else:
                profile = current_profile() if callable(current_profile) else current_profile
            if profile is None:
                raise ValueError('Guarda tu perfil fiscal para cotejar este ticket.')
            if operation == 'queue':
                if self.plan.status()['status'] != 'ready':
                    return {**response, 'action': 'needs_user', 'message': 'Conecta tu cuenta en Ajustes para retomar el trámite.'}
                self.schedule(key, profile, auto_issue=True,
                              confirmed_url=row['receipt'].get('portal_url') or '')
                return {**response, 'status': 'queued', 'action': 'queued', 'message': 'Ticket en cola para retomar la preparación.'}
            if self.prepare_checked_ticket(key, profile):
                return {**response, 'status': 'queued', 'action': 'queued', 'message': self.store.get(key)['message']}
            if self.store.claim_recovery(key):
                self.track_job(key, self.recover_queued(key, profile))
                return {**response, 'action': 'recovering', 'message': 'Consulta de comprobantes solicitada.'}
            return {**response, 'action': 'recovering', 'message': 'Se acaba de consultar este ticket. ' + row['message']}

    async def stop(self, key: str):
        task = self.job_tasks.get(key)
        if not task:
            raise ValueError('El ticket ya no está en ejecución.')
        self.store.pause_automatic_recovery(key)
        queued = self.store.get(key)['status'] == 'queued'
        task.cancel()
        await asyncio.gather(task, return_exceptions=True)
        if queued:
            self.store.update(key, status='ready', message='Retirado de la cola.')

    async def reobserve_untouched_form(self, key, pending, observed, seen):
        await asyncio.sleep(REOBSERVE_DELAY)
        page = pending['page']
        if (len(seen) >= 2 or self.pending.get(key) is not pending or pending['future'].done()
                or self.operator_may_have_acted(pending) or self.viewer.dialog or self.viewer.file_request):
            return
        try:
            revision, epoch = self.viewer.revision, self.viewer.control_epoch
            snapshot = await page.locator('body').aria_snapshot(timeout=3000)
            signature = hashlib.sha256((page.url + '\n' + snapshot).encode()).digest()
            gated = await visible_challenge(page, snapshot)
            preparatory = re.search(r'\b(?:textbox|combobox|spinbutton|radio)\b', snapshot)
            if (signature in seen or gated or (snapshot == observed and not preparatory)
                    or may_have_issued(self.store.get(key)) or self.operator_may_have_acted(pending)):
                return
            if (self.pending.get(key) is not pending or pending['future'].done()
                    or not await self.viewer.finish_returned_control(page, revision, epoch) or pending['future'].done()):
                return
            seen.add(signature)
            self.store.event(key, 'automatic_reobservation', 'Se vuelve a leer el formulario abierto para preparar el ticket.')
            self.decide(key, 'reobserve')
        except BrowserError:
            return

    async def human(self, key: str, page, kind: str, message: str, *, reobserve=None, challenge_seen=False):
        future = asyncio.get_running_loop().create_future()
        timer = None
        pending = self.pending[key] = {'kind': kind, 'future': future, 'page': page,
                                      'control_epoch': self.viewer.control_epoch, 'url': page.url, 'challenge_seen': challenge_seen}
        try:
            if page.is_closed() or page in self.lost_pages:
                raise ValueError('Se cerró la página antes de iniciar la intervención.')
            self.store.enter_intervention(key, kind, message, kind in MANUAL and kind != 'captcha')
            if (kind == 'captcha' and challenge_seen and self.engine == 'Google Chrome' and page in self.page_portal_origins):
                for frame in page.frames[1:]:
                    await self.widgets.arm(frame)
            await self.viewer.set_manual(kind in MANUAL)
            if reobserve is not None:
                observed, seen = reobserve
                timer = asyncio.create_task(self.reobserve_untouched_form(key, pending, observed, seen))
            decision = await future
            if (decision == 'recover' or (decision != 'cancel' and self.store.checked_preparation(key)
                    and kind in MANUAL and self.operator_may_have_acted(pending))):
                raise RecoveryRequested()
            if decision == 'cancel':
                self.store.pause_automatic_recovery(key)
                raise ValueError('Proceso detenido por el usuario.')
        finally:
            if timer:
                timer.cancel()
                await asyncio.gather(timer, return_exceptions=True)
            await self.viewer.set_manual(False)
            self.pending.pop(key, None)
        self.store.resume_intervention(key)
        return decision

    def decide(self, key: str, decision: str):
        pending = self.pending.get(key)
        if not pending or pending['future'].done():
            raise ValueError('La intervención ya no está activa.')
        pending['future'].set_result(decision)

    async def continue_job(self, key: str, decision: str):
        pending = self.pending.get(key)
        if not pending or pending['future'].done():
            raise ValueError('La intervención ya no está activa.')
        kind = pending['kind']
        if decision == 'manual' and kind not in {'plan', 'blocked', 'unknown'}:
            raise ValueError('Este paso no admite cambiar el modo de trabajo.')
        if decision != 'cancel':
            if (kind == 'issue') != (decision == 'approve'):
                raise ValueError('Revisa la acción que solicita el panel.')
            if self.viewer.dialog:
                raise ValueError('Responde primero al aviso del portal.')
            if kind == 'captcha' and pending.get('challenge_seen'):
                outcome = await self.captcha_outcome(key, pending)
                if not outcome:
                    if (pending['page'].is_closed() or self.viewer.page is not pending['page'] or
                            public_portal_origin(pending['page'].url) != public_portal_origin(pending['url'])):
                        raise ValueError('El portal cambió de página. Detén el trámite y usa Recuperar comprobantes para comprobar el resultado.')
                    raise ValueError('Completa la verificación hasta que el portal muestre el siguiente paso o su resultado.')
                self.explicit_approval.add(key)
                if outcome == 'reobserve':
                    self.captcha_returned.add(key)
                decision = outcome
            if kind in {'captcha', 'login', 'attachment'}:
                page = self.viewer.page
                if await visible_challenge(page, await page.locator('body').aria_snapshot()) == kind:
                    message = 'Todavía falta el archivo que pide el portal.' if kind == 'attachment' else 'El portal todavía muestra la verificación.'
                    raise ValueError(message + ' Completa el paso y pulsa Continuar.')
            if kind == 'plan' and decision != 'manual' and self.plan.status()['status'] != 'ready':
                raise ValueError('Conecta tu cuenta o revisa el cupo antes de continuar.')
        await self.viewer.set_manual(False)
        if decision in {'resume', 'manual'} and (pending.get('uncertain_request')
                or (kind in MANUAL and self.viewer.page is not pending['page']) or (
                kind in REOBSERVE and (self.operator_may_have_acted(pending) or may_have_issued(self.store.get(key))))):
            decision = 'recover'
        self.decide(key, decision)

    @staticmethod
    def locator(page, target):
        if target is None or not target.name.strip():
            raise ValueError('Falta la etiqueta visible de la acción.')
        if target.by == 'field':
            number = target.name.strip().lstrip('#')
            if not number.isdigit():
                raise ValueError('Con by="field" escribe en name el número del campo, por ejemplo «7».')
            return page.locator(f'[data-facturai-campo="{int(number)}"]')
        if target.by == 'role':
            if not target.role:
                raise ValueError('Falta el rol del elemento.')
            return page.get_by_role(target.role, name=target.name, exact=target.exact)
        method = {'label': 'get_by_label', 'text': 'get_by_text', 'placeholder': 'get_by_placeholder'}[target.by]
        return getattr(page, method)(target.name, exact=target.exact)

    async def action_control(self, page, action, receipt):
        locator = self.locator(page, action.target)
        if hasattr(locator, 'filter'):
            visible = locator.filter(visible=True)
            if await visible.count() != 1:
                candidates = []
                for frame in page.frames:
                    if frame.parent_frame and not allowed_url(frame.url, receipt.provider, receipt.portal_url, self._extra()):
                        continue
                    candidate = self.locator(frame, action.target).filter(visible=True)
                    found = await candidate.count()
                    if found > 1 or (found and candidates):
                        raise ValueError('Hay varios controles visibles con esa etiqueta.')
                    if found:
                        candidates.append(candidate)
                if not candidates:
                    raise ValueError('El control ya no está visible en el portal.')
                visible = candidates[0]
            locator = visible
        elif await locator.count() != 1:
            raise ValueError('La etiqueta no identifica un único control visible.')
        return locator

    async def safe_continue_href(self, page, action: Action, receipt: Receipt) -> str | None:
        """Navigate a plain same-origin link after CAPTCHA without running click handlers."""
        target = action.target
        if (action.kind != 'click' or not target or target.by != 'role' or target.role != 'link'
                or not target.exact or target.name.strip().casefold() not in {'continuar', 'siguiente'}):
            return None
        try:
            link = page.get_by_role('link', name=target.name, exact=True)
            if await link.count() != 1 or not await link.is_visible():
                return None
            attributes = await link.evaluate("""a => ({href: a.getAttribute('href'),
                download: a.hasAttribute('download'), target: a.hasAttribute('target'),
                onclick: a.hasAttribute('onclick'), form: Boolean(a.closest('form'))})""")
        except BrowserError:
            return None
        if (not attributes['href'] or attributes['download'] or attributes['target']
                or attributes['onclick'] or attributes['form']):
            return None
        href = urljoin(page.url, attributes['href'])
        parts = urlsplit(href)
        if (parts.query or parts.fragment or parts.path in {'', '/'} or EMISSION_PATH.search(parts.path)
                or public_portal_origin(href) != public_portal_origin(page.url)
                or not allowed_url(href, receipt.provider, receipt.portal_url)):
            return None
        return href

    @staticmethod
    def portal_entry_home(page, snapshot: str, receipt: Receipt) -> bool:
        """The Tesoro menu is a navigation page, not the fiscal submission form."""
        current = urlsplit(page.url)
        return (receipt.provider == 'tesoro' and current.path in {'', '/'} and
                not current.query and not current.fragment and
                bool(TESORO_HOME.search(snapshot)) and bool(TESORO_SINGLE.search(snapshot)) and
                not FISCAL_INPUT.search(snapshot) and not SUCCESS.search(snapshot) and
                not already_billed(snapshot))

    async def safe_portal_entry_href(self, page, action: Action, receipt: Receipt, snapshot: str) -> str | None:
        """Open only the documented single-ticket Tesoro form by a plain same-origin GET."""
        target = action.target
        # The live site's Font Awesome icons are part of Playwright's accessible
        # link name (for example, "Continuar... \uf0a9"). Keep the exact locator
        # below, but ignore those private-use glyphs when classifying its label.
        label = ' '.join(''.join(char for char in (target.name if target else '')
                                 if unicodedata.category(char) != 'Co').split()).casefold()
        if (not self.portal_entry_home(page, snapshot, receipt) or action.kind != 'click' or
                not target or target.by != 'role' or target.role != 'link' or not target.exact or
                label not in {'continuar...', 'generar factura de estación y znak'}):
            return None
        try:
            links = page.get_by_role('link', name=target.name, exact=True)
            count = await links.count()
            if not 1 <= count <= 16:
                return None
            matches = []
            for index in range(count):
                link = links.nth(index)
                if not await link.is_visible():
                    continue
                attributes = await link.evaluate("""a => ({tag: a.tagName, href: a.getAttribute('href'),
                    download: a.hasAttribute('download'), target: a.hasAttribute('target'),
                    onclick: a.hasAttribute('onclick'), form: Boolean(a.closest('form'))})""")
                if (attributes['tag'] != 'A' or not attributes['href'] or attributes['download'] or
                        attributes['target'] or attributes['onclick'] or attributes['form']):
                    continue
                href = urljoin(page.url, attributes['href'])
                parts = urlsplit(href)
                if (parts.path != '/GenerarCFDI/Index' or parts.query != 'avanzada=0' or parts.fragment or
                        public_portal_origin(href) != public_portal_origin(page.url) or
                        not allowed_url(href, receipt.provider, receipt.portal_url)):
                    continue
                matches.append(href)
            return matches[0] if len(matches) == 1 else None
        except BrowserError:
            return None

    async def visible_text(self, page, receipt: Receipt) -> str:
        """Rendered text of the portal page and its same-portal frames."""
        text, fields = await self.page_fields(page, receipt)
        return text + '\n[Campos]\n' + '\n'.join(fields)

    async def page_fields(self, page, receipt: Receipt) -> tuple[str, list[str]]:
        """Visible text and the numbered fields of the portal page and its same-portal frames."""
        parts, fields, start = [], [], 1
        for frame in getattr(page, 'frames', []):
            if frame.parent_frame and not allowed_url(frame.url, receipt.provider, receipt.portal_url, self._extra()):
                continue
            try:
                result = await frame.evaluate(PAGE_TEXT, start)
            except BrowserError:
                continue
            if isinstance(result, str):
                parts.append(result)
                continue
            parts.append(result.get('text', ''))
            fields.extend(result.get('fields', []))
            start += int(result.get('count', 0))
        return '\n'.join(parts), fields

    def open_question(self) -> str | None:
        """The ticket whose agent is waiting for an answer in the chat."""
        return next((key for key, future in self.questions.items() if not future.done()), None)

    def answer_question(self, key: str, text: str) -> None:
        future = self.questions.get(key)
        if future and not future.done():
            future.set_result(text[:1000])

    async def ask_chat(self, key: str, question: str) -> str:
        """Ask the person in the live chat and wait a few minutes; an empty answer means «assume»."""
        future = asyncio.get_running_loop().create_future()
        self.questions[key] = future
        question = ' '.join(question.split())[:600]
        self.store.add_assistant_message('Sobre tu ticket en proceso: ' + question +
                                         ' Respóndeme aquí; si no me contestas en unos minutos, sigo con lo más probable.',
                                         [key])
        self.store.running_message(key, 'Te pregunté en el chat: ' + question[:200])
        self.store.event(key, 'question', question[:200])
        try:
            answer = await asyncio.wait_for(asyncio.shield(future), ASK_TIMEOUT)
        except asyncio.TimeoutError:
            answer = ''
        finally:
            self.questions.pop(key, None)
            if not future.done():
                future.cancel()
        self.store.event(key, 'answer', 'Respuesta recibida en el chat.' if answer else 'Sin respuesta; el agente asume y sigue.')
        return answer

    async def generic_issue_proof(self, page, profile: Profile, receipt: Receipt) -> bool:
        """The page shows this recipient RFC, this ticket's folio or code and its amount."""
        try:
            text = await self.visible_text(page, receipt)
        except BrowserError:
            return False
        compact = re.sub(r'\s+', '', text).upper()
        if not profile.rfc or profile.rfc.upper() not in compact:
            return False
        references = [value for value in (receipt.ticket_number, *receipt.billing_codes) if value and value.strip()]
        # A short folio such as «12» could match any number on the page; require a real reference.
        if not any(len(re.sub(r'\s+', '', value)) >= 4 and re.sub(r'\s+', '', value).upper() in compact
                   for value in references):
            return False
        try:
            amount = Decimal(receipt.amount)
        except (InvalidOperation, TypeError):
            return False
        shown = {Decimal(raw.replace(',', '')) for raw in re.findall(r'\d{1,3}(?:,\d{3})*(?:\.\d{1,2})|\d+(?:\.\d{1,2})?', text)
                 if raw.replace(',', '').replace('.', '').isdigit()}
        return amount in shown

    async def use_tool(self, key: str, page, action: Action, receipt: Receipt, ai: AI, after_issue: bool):
        """Run one of the agent's own tools. Returns (result for the model, ticket images, error)."""
        try:
            if action.kind == 'read_text':
                return (await self.visible_text(page, receipt))[:12_000] or 'La página no tiene texto visible.', None, ''
            if action.kind == 'search_web':
                if not (action.value or '').strip():
                    return '', None, 'Escribe en value la pregunta que quieres investigar.'
                answer = await asyncio.to_thread(ai.research, action.value, receipt.merchant)
                return answer, None, ''
            if action.kind == 'view_ticket':
                job = self.store.get(key)
                paths = [Path(job['source_path']), *self.store.companion_paths(key)][:3]
                images = []
                for path in paths:
                    images.extend(await asyncio.to_thread(read_receipt, path))
                return 'Se adjunta la foto original del ticket en este paso.', images, ''
            if action.kind == 'back':
                await page.go_back(wait_until='domcontentloaded', timeout=18000)
                return '', None, ''
            if action.kind == 'reload':
                if after_issue:
                    return '', None, 'Tras un intento de emisión no se recarga: podría reenviar el formulario.'
                await page.reload(wait_until='domcontentloaded', timeout=18000)
                return '', None, ''
            if action.kind == 'upload':
                if after_issue:
                    return '', None, 'Tras un intento de emisión ya no se adjuntan archivos.'
                if (action.value or 'ticket').strip().casefold() != 'ticket':
                    return '', None, 'Solo puedes adjuntar la foto o PDF del ticket (value="ticket").'
                source = Path(self.store.get(key)['source_path'])
                inputs = []
                for frame in page.frames:
                    if not allowed_url(frame.url, receipt.provider, receipt.portal_url, self._extra()):
                        continue
                    found = frame.locator('input[type=file]')
                    for index in range(await found.count()):
                        inputs.append(found.nth(index))
                if action.target is not None:
                    try:
                        chosen = await self.action_control(page, action, receipt)
                        if await chosen.evaluate("e => e.tagName === 'INPUT' && e.type === 'file'"):
                            inputs = [chosen]
                    except ValueError:
                        pass
                if len(inputs) != 1:
                    return '', None, 'No hay un único campo de archivo en el portal. Usa human para adjuntarlo.'
                await inputs[0].set_input_files(str(source))
                self.store.event(key, 'attachment_uploaded', 'El agente adjuntó el ticket al portal ' +
                                 (public_portal_origin(page.url) or ''))
                return 'Se adjuntó el ticket. Revisa la página.', None, ''
        except PlanError:
            return '', None, 'La búsqueda web no respondió. Continúa con lo que ves en la página.'
        except (BrowserError, OSError, ValueError) as exc:
            return '', None, ('La herramienta falló: ' + str(exc).splitlines()[0])[:300]
        return '', None, 'Herramienta no reconocida.'

    async def execute(self, page, action: Action, receipt: Receipt):
        if receipt.provider == CUSTOM_PORTAL and not allowed_url(page.url, receipt.provider, receipt.portal_url, self._extra()):
            raise ValueError('La página cambió de dominio. Revisa el nuevo destino antes de continuar.')
        if action.kind == 'goto':
            if not allowed_url(action.value or '', receipt.provider, receipt.portal_url, self._extra()):
                raise ValueError('Destino fuera del portal registrado.')
            await page.goto(action.value, wait_until='domcontentloaded')
            return
        if action.kind == 'wait':
            await asyncio.sleep(1)
            return
        if action.kind == 'scroll':
            await page.mouse.wheel(0, max(-1800, min(1800, int(action.value or '600'))))
            return
        locator = await self.action_control(page, action, receipt)
        if action.kind == 'click':
            await locator.click()
        elif action.kind == 'fill':
            value = (action.value or '')[:1000]
            await locator.fill(value)
            actual = await locator.evaluate("e => e.isContentEditable ? e.innerText : e.value")
            if actual != value:
                # Masked fields often reject a pasted value but accept it typed key by key.
                await locator.click()
                await locator.press('Control+A')
                await locator.press('Delete')
                await locator.press_sequentially(value, delay=35)
                actual = await locator.evaluate("e => e.isContentEditable ? e.innerText : e.value")
                if re.sub(r'[\s\-/.]', '', actual or '').upper() != re.sub(r'[\s\-/.]', '', value).upper():
                    raise ValueError('El portal modificó o rechazó el valor escrito. Revisa la máscara y el mensaje del campo.')
        elif action.kind == 'select':
            value = action.value or ''
            choices = await locator.locator('option').evaluate_all('(options) => options.map(o => [o.value, o.label])')
            use_value = [item[0] for item in choices if item[0] == value]
            matches = use_value or [item[0] for item in choices if item[1] == value]
            if len(matches) != 1:
                raise ValueError('La lista del portal no tiene una opción única que coincida.')
            await locator.select_option(value=matches[0])
            if await locator.input_value() != matches[0]:
                raise ValueError('El portal no conservó la opción seleccionada.')
        elif action.kind == 'press' and action.value in {'Enter', 'Tab', 'ArrowDown', 'ArrowUp', 'Escape', 'Space'}:
            await locator.press(action.value)
        else:
            raise ValueError('Tipo de acción o tecla no admitida.')

    async def recover(self, key: str, profile: Profile, *, notes=None) -> bool:
        files = job_candidates(self.store, key)
        if not files:
            return False
        try:
            await asyncio.to_thread(reconcile, self.store, key, profile, files)
        except ValueError as exc:
            if notes is not None:
                # Return a fixed explanation. The original parser errors may contain
                # text supplied by a merchant or values taken from private documents.
                reason = str(exc)
                if 'importe facturable' in reason:
                    notes.append('El importe del CFDI recibido no coincide con el ticket.')
                elif 'CFDI no coincide en' in reason:
                    notes.append('Los datos fiscales del receptor en el XML difieren del perfil del expediente.')
                elif 'forma de pago confirmada' in reason:
                    notes.append('La forma de pago del CFDI difiere de la guardada en el ticket.')
                elif 'RFC emisor no corresponde' in reason:
                    notes.append('El emisor del CFDI difiere del registrado para el comercio.')
            return False
        return True

    async def manual_job(self, key, page, profile):
        row = self.store.get(key)
        if self.store.checked_preparation(key):
            raise RecoveryRequested()
        await self.human(key, page, 'operator', 'El navegador es tuyo. Completa o consulta el trámite, descarga sus archivos y pulsa Terminar y cotejar.')
        if self.downloads:
            await asyncio.wait(list(self.downloads), timeout=12)
        if not await self.recover(key, profile):
            snapshot = await self.viewer.page.locator('body').aria_snapshot()
            row = self.store.get(key)
            if SUCCESS.search(snapshot) or row['issuance_seen'] or row['uuid']:
                self.store.confirm_issued(key)
            else:
                self.store.update(key, status='uncertain', message='Sesión manual terminada sin comprobante confirmado. Recupera el resultado antes de repetir.')

    async def run(self, key: str, profile: Profile, auto_issue: bool = False, manual: bool = False):
        async with self.lock:
            if not self.store.claim(key):
                return
            self.active_id = key
            try:
                for attempt in range(3):
                    try:
                        await self._session(key, profile, auto_issue, manual)
                        return
                    except RecoveryRequested:
                        self.store.claim_recovery(key)
                        await self.recover_only(key, profile)
                        return
                    except Exception:
                        row = self.store.get(key)
                        if row['status'] in {'verified', 'duplicate'}:
                            return
                        if row['issue_attempted'] or row['human_exposed'] or row['uuid'] or row['issuance_seen']:
                            # The browser can disappear after saving both files but before the next page snapshot.
                            if self.downloads:
                                await asyncio.wait(tuple(self.downloads), timeout=12)
                            if await self.recover(key, profile):
                                return
                            raise
                        page = self.viewer.page
                        lost = self.context is None or page in self.lost_pages or (page and page.is_closed())
                        failure = sys.exc_info()[1]
                        retryable = (isinstance(failure, ValueError) and RETRYABLE.search(str(failure))
                                     and key not in self.soft_submitted and not manual)
                        if attempt >= 2 or not (lost or retryable):
                            raise
                        if not lost:
                            # Nothing was submitted: start over on a fresh page and tell the
                            # agent what went wrong so it takes another route.
                            self.session_hints[key] = ('Intento anterior falló: ' + str(failure)[:300] +
                                                       ' Toma otra ruta para llegar a la factura.')
                            self.store.update(key, status='running', message='Reintentando el portal por otra ruta.')
                            self.store.event(key, 'session_retry', str(failure)[:200])
                            continue
                        failed_context = self.context
                        if failed_context:
                            await close_context(failed_context)
                            if self.context is failed_context:
                                self.context = None
                        self.store.update(key, status='running', message='Reabriendo el portal tras perder la sesión.')
                        self.store.event(key, 'browser_reopened', 'Nueva sesión antes de la emisión')
            except (Exception, asyncio.CancelledError) as exc:
                row = self.store.get(key)
                if row['status'] not in {'verified', 'duplicate', 'historical'}:
                    if not isinstance(exc, (ValueError, asyncio.CancelledError)):
                        frames = [frame for frame in traceback.extract_tb(exc.__traceback__)
                                  if Path(frame.filename).parent.name == 'facturador']
                        point = (Path(frames[-1].filename).name + ':' + str(frames[-1].lineno)
                                 if frames else 'runner')
                        self.store.event(key, 'internal_error', type(exc).__name__ + ' en ' + point)
                    attempted = bool(row['issue_attempted'] or row['human_exposed'] or row['uuid'])
                    issued = bool(row['issuance_seen'] or row['uuid'])
                    message = str(exc)[:450] if isinstance(exc, ValueError) else 'El trámite se interrumpió.'
                    if attempted:
                        message += ' Busca los comprobantes de este ticket antes de intentar otra emisión.'
                    self.store.update(key, status='issued_waiting_files' if issued else 'uncertain' if attempted else 'blocked', message=message)
                    self.store.event(key, 'stopped', message)
                if isinstance(exc, asyncio.CancelledError):
                    raise
            finally:
                self.active_id = None
                self.explicit_approval.discard(key)
                self.captcha_returned.discard(key)
                self.job_origins.pop(key, None)
                self.session_hints.pop(key, None)
                self.soft_submitted.discard(key)
                await self.viewer.set_manual(False)
                await self.finish_job_browser(key)

    async def _session(self, key: str, profile: Profile, auto_issue: bool, manual: bool):
        committed = issued = False
        await self.close_parked_job_pages(key)
        job = self.store.get(key)
        receipt = Receipt(**job['receipt'])
        errors = receipt.ready_errors()
        if errors or (receipt.provider not in CATALOG and receipt.provider != CUSTOM_PORTAL):
            raise ValueError(' · '.join(errors) or 'Portal sin configurar.')
        bp_evidence = (await asyncio.to_thread(evidence_from_original, receipt, Path(job['source_path']))
                       if receipt.provider == 'bp' and job.get('source_path') else None)
        bp_progress = BPProgress()
        ai = AI(self.plan)
        if receipt.provider == CUSTOM_PORTAL:
            if not await resolves_publicly(receipt.portal_url):
                raise ValueError('El dominio del ticket debe resolver a una dirección pública accesible.')
            provider = {'name': receipt.merchant, 'url': receipt.portal_url,
                        'guide': ('Portal del comercio impreso o localizado para este ticket. Recórrelo tú hasta la emisión: '
                                  'busca la opción de facturar, captura los datos del ticket y del perfil y sigue al operador '
                                  'de facturación si el portal te redirige. Solo el CAPTCHA y las contraseñas son del titular.')}
        else:
            provider = CATALOG[receipt.provider]
        await self.start_browser()
        page = await self.context.new_page()
        self.page_job[page] = key
        if receipt.provider == CUSTOM_PORTAL:
            self.page_portal_origins[page] = public_portal_origin(receipt.portal_url)
        await self.viewer.show(page, provider['name'])
        self.store.event(key, 'browser_opened', 'Se abrió el portal del comercio en el navegador del trámite.')
        await open_ticket_portal(page, provider['url'], receipt, visible_challenge, self.store, key)
        if receipt.provider == CUSTOM_PORTAL and not allowed_url(page.url, receipt.provider, receipt.portal_url, self._extra()):
            raise ValueError('El portal redirigió a otro dominio. Confirma la dirección final en la revisión del ticket.')
        if manual:
            return await self.manual_job(key, page, profile)
        prompt = (Path(__file__).resolve().parent.parent / 'PROMPT_FACTURADOR.md').read_text(encoding='utf-8')
        ticket_fields = receipt.model_dump(exclude={'portal_url'})
        instructions = prompt + '\n' + json.dumps({'perfil': profile.model_dump(exclude={'whatsapp_phone'}), 'ticket': ticket_fields,
                                                  'portal': {**provider, 'url': public_portal_origin(provider['url'])}}, ensure_ascii=False)
        recent = []
        reobserved = set()
        # A previous attempt's failure becomes the first thing the agent reads.
        last_error = self.session_hints.pop(key, '')
        agent_notes = ''
        tool_result = ''
        ticket_view = None
        self_help = 0
        plan_failures = 0
        done_refusals = 0
        attachment_tries = 0
        stalled_hints = 0
        failures: dict[str, int] = {}
        idle_steps = 0
        questions = 0
        answer_note = ''
        before_action = None
        tesoro_lookup_attempted = False
        bp_entry_attempted = False
        bp_lookup_attempted = False
        walmart_lookup = None
        unbound_press_count = 0
        accounts = PortalAccountFlow(self.store.root, provider['url'], profile, key,
                                     progress=lambda message: self.store.running_message(key, message))
        for number in range(MAX_STEPS):
            bp_auto_ready = False
            page = self.viewer.page
            status = self.store.get(key)['status']
            if status in {'verified', 'duplicate', 'historical'}:
                return
            if page.is_closed() or not allowed_url(page.url, receipt.provider, receipt.portal_url, self._extra()):
                raise ValueError('El portal se cerró o cambió a un dominio distinto del autorizado.')
            snapshot = await page.locator('body').aria_snapshot()
            if (receipt.provider == 'walmart' and not committed and not issued
                    and await dismiss_walmart_cfdi_notice(page, snapshot)):
                self.store.event(key, 'notice_dismissed', 'Se cerró el aviso informativo CFDI 4.0 de Walmart.')
                continue
            if (receipt.provider == 'walmart' and not committed and not issued
                    and await dismiss_walmart_address_notice(page, snapshot)):
                self.store.event(key, 'notice_dismissed', 'Se cerró el aviso informativo de captura fiscal de Walmart.')
                continue
            if (receipt.provider == 'walmart' and walmart_lookup is not None
                    and urlsplit(page.url).path.rstrip('/') != '/address'):
                walmart_lookup = None
            if receipt.provider == 'tesoro' and tesoro_lookup_attempted and not committed and not issued:
                rejected = await tesoro_verification_failure(page, receipt, profile)
                if rejected:
                    self.store.event(key, 'ticket_lookup_rejected', rejected)
                    if existing_invoice_folio(rejected):
                        if await recover_tesoro_consulta(self, page, key, profile, receipt, rejected):
                            return
                        raise ValueError(rejected + ' La consulta de Tesoro no devolvió un PDF y XML cotejables.')
                    raise ValueError(rejected)
            if receipt.provider == 'vianney' and not committed and not issued and lookup_rejected(snapshot):
                submitted_here = any(item.get('tipo') == 'click' and item.get('control', '').casefold() == 'siguiente'
                                     for item in recent)
                prior_submission = any(event['kind'] == 'action' and event['detail'].casefold() == 'click · siguiente'
                                       for event in self.store.events(key)) if not submitted_here else False
                rejected = await rejection_for_current_ticket(
                    page, snapshot, receipt, submitted_here=submitted_here, prior_submission=prior_submission)
                if rejected:
                    self.store.event(key, 'ticket_lookup_rejected', rejected)
                    if await recover_vianney_consulta(self, page, key, profile, receipt):
                        return
                    raise ValueError(rejected + ' La consulta de comprobantes no devolvió un PDF y XML cotejables.')
            if before_action is not None and recent:
                recent[-1]['cambio_visible'] = before_action != (page.url, snapshot)
                before_action = None
            if ACCESS_DENIED.search(snapshot):
                raise ValueError('El portal rechazó el acceso a este navegador. El ticket quedó pendiente.')
            issued = issued or bool(SUCCESS.search(snapshot)) or already_billed(snapshot)
            if issued:
                self.store.confirm_issued(key)
                uuids = set(value.upper() for value in UUID_RE.findall(snapshot))
                if len(uuids) == 1 and not self.store.get(key)['uuid']:
                    self.store.update(key, uuid=next(iter(uuids)))
                if self.downloads:
                    await asyncio.wait(list(self.downloads), timeout=2)
                if await self.recover(key, profile):
                    return
            challenge = await visible_challenge(page, snapshot)
            if not committed and not issued and challenge not in {'captcha', 'attachment'}:
                account_step = await accounts.step(page, snapshot, challenge)
                if account_step:
                    kind, message = account_step
                    if kind == 'advanced':
                        self.store.event(key, 'portal_account', message)
                    else:
                        await self.human(key, page, kind, message)
                    continue
            if challenge == 'attachment' and attachment_tries < 2 and not committed and not issued:
                attachment_tries += 1
                last_error = ('El portal exige un archivo. Si pide la foto o el PDF del ticket, usa upload con '
                              'value="ticket" sobre el campo o botón del archivo. Si pide otro documento, usa human.')
                challenge = None
            if challenge:
                message = {'captcha': 'Completa la verificación en este navegador.',
                           'login': 'Inicia sesión en el portal y regresa al trámite.',
                           'attachment': 'Toma el control y abre el selector del portal. Elige el documento en el panel y pulsa Adjuntar al portal.'}[challenge]
                await self.human(key, page, challenge, message, challenge_seen=True)
                continue
            if accounts.private_observation(snapshot):
                await self.human(key, page, 'login', 'El portal muestra una credencial. Ocúltala o avanza en la misma página para continuar.')
                continue
            pending_fields = []
            if not committed and not issued:
                if (receipt.provider == 'bp' and not bp_entry_attempted and
                        urlsplit(page.url).path.rstrip('/') == BP_HOME):
                    bp_entry_attempted = True
                    prior_url = page.url
                    try:
                        bp_entered = await enter_bp_franchise(page, receipt, snapshot)
                    except BrowserError as exc:
                        raise ValueError('La portada BP no confirmó la selección de la franquicia. Reintenta el ticket.') from exc
                    if bp_entered:
                        before_action = prior_url, snapshot
                        recent.append({'tipo': 'click', 'control': 'Franquicia BP'})
                        self.store.event(key, 'brand_entry', 'Se abrió el formulario de la franquicia BP verificada.')
                        continue
                if receipt.provider == 'bp':
                    try:
                        bp_field = await prepare_bp_rfc(
                            page, receipt, profile, lookup_attempted=bp_lookup_attempted)
                    except BrowserError as exc:
                        # Buscar may have reached the portal even if Chrome lost
                        # its response. Never submit the same lookup twice.
                        bp_lookup_attempted = True
                        raise ValueError('BP no confirmó la consulta del RFC. Revisa el portal antes de reintentar.') from exc
                    if bp_field:
                        if bp_field == 'Buscar RFC':
                            bp_lookup_attempted = True
                        self.store.event(key, 'campo_verificado', 'Consulta fiscal cotejada: ' + bp_field)
                        continue
                    if bp_at_form(page, receipt):
                        try:
                            bp_field = await prepare_bp_ticket(page, receipt, profile, bp_evidence, bp_progress)
                        except BPAlreadyBilled as exc:
                            self.store.event(key, 'ticket_already_billed', 'BP confirmó que la venta ya tiene factura; se buscaron los comprobantes existentes.')
                            if await recover_bp_reprint(self, page, key, profile, receipt, bp_evidence):
                                return
                            raise ValueError(str(exc)) from exc
                        if bp_field:
                            self.store.event(key, 'campo_verificado', 'Ticket BP cotejado: ' + bp_field)
                            continue
                        bp_auto_ready = bool(auto_issue and await bp_issue_proof(page, receipt, profile, bp_evidence))
                tesoro_pending = []
                if receipt.provider == 'tesoro':
                    try:
                        tesoro_changed, tesoro_pending = await tesoro_recipient(page, profile, apply=True)
                    except BrowserError:
                        tesoro_changed, tesoro_pending = None, ['ficha fiscal del receptor']
                    if tesoro_changed:
                        self.store.event(key, 'campo_verificado', 'Campo fiscal cotejado: ' + tesoro_changed)
                        continue
                if (receipt.provider == 'capufe' and
                        urlsplit(page.url).path.rstrip('/').lower() == '/capufe/facturacionrapida'):
                    try:
                        field = await capufe_recipient(page, profile)
                    except BrowserError:
                        field = None
                    if field:
                        self.store.event(key, 'campo_verificado', 'Campo cotejado: ' + field)
                        continue
                changed, pending_fields = await fiscal_choices(page, profile, receipt, apply=True, extra=self._extra())
                pending_fields = tesoro_pending + pending_fields
                if changed:
                    self.store.event(key, 'campo_verificado', 'Selección cotejada: ' + FISCAL_FIELDS[changed])
                    continue
                try:
                    lookup_field = await prefill_ticket_lookup(page, receipt.ticket_number)
                except BrowserError:
                    lookup_field = None
                if lookup_field:
                    self.store.event(key, 'campo_verificado', 'Dato del ticket cotejado: ' + lookup_field)
                    continue
                try:
                    tesoro_field = await prepare_tesoro_lookup(
                        page, receipt, verify_attempted=tesoro_lookup_attempted)
                except BrowserError:
                    # A validation POST may have reached the portal before Chrome
                    # reported a navigation error. Never repeat it automatically.
                    tesoro_lookup_attempted = True
                    continue
                if tesoro_field:
                    if tesoro_field.startswith('Verificación'):
                        tesoro_lookup_attempted = True
                        rejected = await tesoro_verification_failure(page, receipt, profile)
                        if rejected:
                            self.store.event(key, 'ticket_lookup_rejected', rejected)
                            if existing_invoice_folio(rejected):
                                if await recover_tesoro_consulta(self, page, key, profile, receipt, rejected):
                                    return
                                raise ValueError(rejected + ' La consulta de Tesoro no devolvió un PDF y XML cotejables.')
                            raise ValueError(rejected)
                    self.store.event(key, 'campo_verificado', 'Dato del ticket cotejado: ' + tesoro_field)
                    continue
                if receipt.provider == 'walmart' and walmart_lookup is None:
                    lookup_step = await walmart_lookup_continue(page, profile, receipt)
                    if lookup_step is not None:
                        proof, control = lookup_step
                        bound = await control.element_handle()
                        if bound is None:
                            raise ValueError('El botón Continuar de Walmart cambió antes de consultar el ticket.')
                        try:
                            await bound.click(trial=True, timeout=1500)
                        except BrowserError as exc:
                            raise ValueError('El botón Continuar de Walmart no está disponible para consultar el ticket.') from exc
                        checked = await walmart_lookup_continue(page, profile, receipt)
                        if (checked is None or checked[0] != proof or
                                await page.locator('body').aria_snapshot() != snapshot):
                            continue
                        before_action = page.url, snapshot
                        try:
                            await bound.click(timeout=5000)
                        except BrowserError as exc:
                            # Chrome may lose the response after the portal receives the lookup.
                            # Never repeat a possibly submitted lookup in the same attempt.
                            rejected = await walmart_lookup_rejection(page, snapshot)
                            if rejected:
                                self.store.event(key, 'ticket_lookup_rejected', rejected)
                                raise ValueError('Walmart respondió: ' + rejected) from exc
                            raise ValueError('Walmart no confirmó el clic de consulta. Revisa el resultado antes de reintentar.') from exc
                        recent.append({'tipo': 'click', 'control': 'Continuar'})
                        self.store.event(key, 'action', 'click · Continuar')
                        try:
                            await page.wait_for_url(re.compile(
                                r'^https://facturacion-clientes\.walmart\.com/address(?:[/?#].*)?$'), timeout=8000)
                        except BrowserError as exc:
                            rejected = await walmart_lookup_rejection(page, snapshot)
                            if rejected:
                                self.store.event(key, 'ticket_lookup_rejected', rejected)
                                raise ValueError('Walmart respondió: ' + rejected) from exc
                            raise ValueError('Walmart no confirmó la consulta del ticket. Revisa el mensaje del portal antes de reintentar.') from exc
                        walmart_lookup = proof
                        self.store.event(key, 'ticket_lookup_verified',
                                         'TC, TR, RFC y código postal coincidieron al continuar.')
                        continue
            try:
                _, fields = await self.page_fields(page, receipt)
            except BrowserError:
                fields = []
            # Long without progress: ask in the chat once, then keep going with an assumption.
            if (idle_steps >= IDLE_BEFORE_ASK and questions < MAX_QUESTIONS and not committed and not issued
                    and not accounts.credential_values):
                questions += 1
                idle_steps = 0
                answer = await self.ask_chat(key, 'No he podido avanzar en el portal de ' + receipt.merchant[:80] +
                                             '. Lo que me frena: ' + (last_error or 'el formulario no acepta lo que capturo')[:250] +
                                             ' ¿Me dices qué hago o qué dato falta?')
                answer_note = answer or 'Sin respuesta: asume lo más razonable y toma otra ruta.'
                continue
            # Accessibility snapshots include complete hrefs on separate /url: lines.
            model_snapshot = '\n'.join(line for line in snapshot.splitlines() if not line.lstrip().startswith('- /url:'))
            observation = json.dumps({'url': public_portal_origin(page.url), 'paso': number, 'pasos_maximos': MAX_STEPS,
                                      'emision_intentada': committed, 'emision_visible': issued,
                                      'tus_notas': agent_notes, 'acciones_recientes': recent[-15:],
                                      'ultimo_error': last_error, 'resultado_herramienta': tool_result,
                                      'campos_fiscales_pendientes': pending_fields,
                                      'campos': fields[:120], 'respuesta_del_titular': answer_note,
                                      'operadores_autorizados': sorted(self._extra()),
                                      'estado_visible': model_snapshot[:26_000]}, ensure_ascii=False)
            tool_result = ''
            try:
                if bp_auto_ready:
                    latest = await page.locator('body').aria_snapshot()
                    if (latest != snapshot or await visible_challenge(page, latest) or self.viewer.page is not page or
                            not allowed_url(page.url, receipt.provider, receipt.portal_url, self._extra())):
                        continue
                    action = Action(kind='click', target=Target(by='role', role='link', name='Facturar', exact=True),
                                    value=None, reason='Emitir el único ticket BP cotejado con el perfil.', risk='issue')
                else:
                    try:
                        # Once a credential has been used, pixels could echo it outside the
                        # accessibility tree (canvas, QR or a transient toast). Use verified text.
                        image = None if accounts.credential_values else await page.screenshot(
                            type='jpeg', quality=76, scale='css', timeout=6000)
                    except BrowserError:
                        if page.is_closed() or page in self.lost_pages or self.context is None:
                            raise
                        image = None
                        self.store.event(key, 'text_navigation', 'El navegador no produjo la captura. Se usó el árbol visible.')
                    # A portal may render a verification challenge while producing its frame.
                    # Discard both observations and reclassify the current page before sharing.
                    latest = await page.locator('body').aria_snapshot()
                    if (latest != snapshot or accounts.private_observation(latest) or await visible_challenge(page, latest) or
                            self.viewer.page is not page or not allowed_url(page.url, receipt.provider, receipt.portal_url, self._extra())):
                        continue
                    action = await asyncio.to_thread(ai.next_action, instructions, observation, image, ticket_view)
                    ticket_view = None
                    plan_failures = 0
            except PlanError as exc:
                diagnostic = {'status': exc.status}
                for field in ('code', 'event_type'):
                    value = str(getattr(exc, field, ''))
                    if re.fullmatch(r'[a-zA-Z0-9_.-]{1,80}', value):
                        diagnostic[field] = value
                self.store.event(key, 'plan_failure', json.dumps(diagnostic))
                plan_failures += 1
                if plan_failures <= PLAN_RETRIES and exc.status not in {'reconnect', 'identity_only', 'ineligible'}:
                    # A transient model failure must not cost the ticket: wait and ask again.
                    self.store.running_message(key, 'El asistente no respondió; se vuelve a intentar el mismo paso.')
                    await asyncio.sleep(min(30, 4 * plan_failures))
                    continue
                if not committed and not issued:
                    if exc.status in {'reconnect', 'identity_only', 'ineligible', 'model', 'limit'}:
                        raise ValueError('La conexión del asistente requiere atención. Revisa Ajustes y reintenta el ticket; no se envió la factura.') from exc
                    raise ValueError('El asistente no completó el siguiente paso. Puedes reintentar el ticket; no se envió la factura.') from exc
                decision = await self.human(key, page, 'plan', str(exc))
                if decision == 'manual':
                    return await self.manual_job(key, page, profile)
                continue
            if press_without_target(action):
                unbound_press_count += 1
                last_error = 'Indica el control visible exacto antes de pulsar una tecla; no se ejecutó la acción.'
                if unbound_press_count >= 2:
                    decision = await self.human(key, page, 'unknown', last_error)
                    if decision == 'manual':
                        return await self.manual_job(key, page, profile)
                continue
            unbound_press_count = 0
            if action.notes:
                agent_notes = action.notes.strip()[:2000]
            if action.kind == 'ask':
                if questions >= MAX_QUESTIONS or committed or issued:
                    last_error = 'Ya no puedes preguntar más: asume el dato más razonable y sigue.'
                    continue
                questions += 1
                idle_steps = 0
                answer = await self.ask_chat(key, action.value or action.reason)
                answer_note = answer or 'Sin respuesta: asume lo más razonable y sigue.'
                recent.append({'tipo': 'ask', 'control': (action.value or '')[:120]})
                continue
            if action.kind in TOOLS:
                idle_steps += 1
                tool_result, ticket_view, failure = await self.use_tool(key, page, action, receipt, ai, committed or issued)
                last_error = failure
                if not failure and action.kind in {'back', 'reload', 'upload'}:
                    before_action = page.url, snapshot
                label = action.value or (action.target.name if action.target else '')
                recent.append({'tipo': action.kind, 'control': label[:120]})
                self.store.event(key, 'tool', action.kind + ' · ' + (action.reason or '')[:120])
                continue
            risk = action_risk(action)
            name = action.target.name if action.target else ''
            if risk == 'confirm':
                # «Aceptar», «Enviar» or Enter. On a page that already shows this ticket's RFC,
                # folio and amount it may be the final submission, so it goes through the
                # emission gate; otherwise it is an ordinary step of the form.
                if (not committed and not issued and action.kind == 'click'
                        and await self.generic_issue_proof(page, profile, receipt)):
                    risk = 'issue'
                else:
                    risk = 'normal'
                    if not committed and not issued:
                        self.soft_submitted.add(key)
            walmart_issue_control = None
            walmart_lookup_candidate = None
            walmart_issue_element = None
            walmart_issue_url = None
            walmart_auto = False
            if (receipt.provider == 'walmart' and not committed and not issued and walmart_lookup is None
                    and action.kind == 'click' and normalize(name) == 'CONTINUAR'):
                walmart_lookup_candidate = await walmart_lookup_proof(page, profile, receipt)
                if walmart_lookup_candidate is None:
                    last_error = 'Antes de continuar, coteja RFC, código postal, TC y TR con el ticket.'
                    continue
            if (receipt.provider == 'walmart' and not committed and not issued
                    and action.kind == 'click' and action.target and action.target.by == 'role'
                    and action.target.role == 'button' and action.target.exact
                    and normalize(name) == 'ACEPTAR'):
                walmart_issue_control = await walmart_issue_button(page, profile, receipt, walmart_lookup)
                if walmart_issue_control is not None:
                    walmart_issue_url = page.url
                    risk = 'issue'
            if risk == 'forbidden':
                raise ValueError('La acción no corresponde a solicitar el CFDI de esta compra.')
            if action.kind == 'done':
                if not issued and not committed and done_refusals < 2:
                    done_refusals += 1
                    last_error = ('Todavía no hay comprobante ni confirmación visible. Sigue hasta emitir o recuperar la '
                                  'factura. Usa read_text, search_web, view_ticket, back o reload si no ves el camino.')
                    continue
                if not issued:
                    if committed and await self.recover(key, profile):
                        return
                    raise ValueError('No hay comprobante ni confirmación visible. Recupera el resultado antes de volver a intentar.')
                if self.downloads:
                    await asyncio.wait(list(self.downloads), timeout=12)
                await self.recover(key, profile)
                return
            if ((action.kind == 'human' or risk in {'unknown', 'terms', 'blocked'}) and risk not in {'captcha', 'login'}
                    and self_help < SELF_HELP_LIMIT and not accounts.credential_values):
                self_help += 1
                last_error = ('No pidas ayuda todavía: resuelve el paso tú. Lee el mensaje del portal (read_text), '
                              'revisa la foto del ticket (view_ticket), investiga el portal (search_web) o prueba otra '
                              'ruta (back, reload, otro control). Motivo que diste: ' + (action.reason or '')[:200])
                recent.append({'tipo': 'autoayuda', 'control': (action.reason or '')[:120]})
                continue
            if ((action.kind == 'human' or risk in {'unknown', 'terms', 'blocked'}) and risk not in {'captcha', 'login'}
                    and questions < MAX_QUESTIONS and not committed and not issued and not accounts.credential_values):
                questions += 1
                idle_steps = 0
                answer = await self.ask_chat(key, action.reason or 'Necesito un dato para continuar con el portal.')
                answer_note = answer or 'Sin respuesta: asume lo más razonable y sigue.'
                continue
            if risk in MANUAL or action.kind == 'human':
                detail = ('El portal aún no refleja ' + ', '.join(pending_fields) + '. Revisa el formulario.' if pending_fields
                          else 'Revisa el paso pendiente en el navegador.')
                kind = risk if risk in MANUAL else 'unknown'
                may_reobserve = kind == 'unknown' and not (committed or issued or accounts.credential_values)
                decision = await self.human(key, page, kind, detail, reobserve=(snapshot, reobserved) if may_reobserve else None)
                if decision == 'manual':
                    return await self.manual_job(key, page, profile)
                if decision == 'reobserve':
                    last_error = 'Lee otra vez los controles actuales. Sólo se permite preparar datos comprobados hasta la aprobación de emisión.'
                continue
            if (receipt.provider == 'tesoro' and action.kind == 'click' and
                    normalize(''.join(char for char in name if unicodedata.category(char) != 'Co'))
                    == 'GUARDAR Y CONTINUAR' and
                    urlsplit(page.url).path.rstrip('/') == '/GenerarCFDI'):
                _, unresolved = await tesoro_recipient(page, profile)
                if unresolved:
                    last_error = 'Antes de guardar el receptor faltan: ' + ', '.join(unresolved) + '.'
                    if recent and recent[-1].get('bloqueo') == last_error:
                        raise ValueError(last_error)
                    recent.append({'bloqueo': last_error})
                    continue
            if not committed and not issued and self.portal_entry_home(page, snapshot, receipt) and action.kind == 'click' and action.target and action.target.by == 'role' and action.target.role == 'link':
                href = await self.safe_portal_entry_href(page, action, receipt, snapshot)
                if href:
                    before_action = page.url, snapshot
                    await page.goto(href, wait_until='domcontentloaded', timeout=18000)
                    recent.append({'tipo': 'goto', 'control': name[:120]})
                    self.store.event(key, 'portal_entry', 'Se abrió el formulario de un ticket del mismo comercio.')
                    continue
                raise ValueError('El enlace del menú no corresponde al formulario de un ticket verificado. Revisa el destino en el visor.')
            preparing_capufe = (receipt.provider == 'capufe' and normalize(name) == 'FACTURAR CONCEPTOS' and
                                action.kind == 'click' and not committed and not issued and
                                urlsplit(page.url).path.rstrip('/').lower() == '/capufe/facturacionrapida')
            if key in self.captcha_returned and risk == 'normal' and not committed and not issued:
                href = await self.safe_continue_href(page, action, receipt)
                if href:
                    before_action = page.url, snapshot
                    await page.goto(href, wait_until='domcontentloaded', timeout=18000)
                    self.captcha_returned.discard(key)
                    recent.append({'tipo':'goto','control':name[:120]})
                    self.store.event(key, 'captcha_followed_link', 'Se abrió el enlace simple de continuación del mismo comercio.')
                    continue
            if key in self.explicit_approval or self.store.checked_preparation(key):
                if (not committed and not issued and risk != 'issue' and not preparing_capufe
                        and action.kind not in {'fill', 'select', 'wait', 'scroll'}
                        and not (receipt.provider == 'capufe' and action.kind == 'click' and
                                 re.fullmatch(r'agregar (?:c[oó]digo|ticket|concepto)(?: de peaje)?', name.strip(), re.I))):
                    await self.human(key, page, 'blocked', 'El siguiente control no se reconoce como preparación del ticket. Revisa este paso en el visor.')
                    continue
            if preparing_capufe and not await capufe_proof(page, profile, receipt, preparation=True):
                last_error = 'CAPUFE aún no refleja los datos fiscales, agrupación, códigos, fecha o total del expediente.'
                if recent and recent[-1].get('bloqueo') == last_error:
                    await self.human(key, page, 'blocked', last_error)
                recent.append({'bloqueo': last_error})
                continue
            if risk == 'issue' and not preparing_capufe:
                if committed or issued:
                    raise ValueError('Ya hubo un intento de emisión. Consulta o recupera ese CFDI.')
                if action.kind != 'click' or not action.target.exact:
                    last_error = 'Para emitir, elige el botón o enlace exacto de la confirmación.'
                    continue
                if receipt.provider == 'tesoro':
                    mismatch = await tesoro_verification_failure(page, receipt, profile, issue_gate=True)
                    if mismatch:
                        raise ValueError(mismatch)
                if receipt.provider == 'bp' and not await bp_issue_proof(page, receipt, profile, bp_evidence):
                    raise ValueError('BP no mostró una única compra cotejada con el ticket y el perfil fiscal.')
                _, pending_fields = await fiscal_choices(page, profile, receipt, extra=self._extra())
                if pending_fields:
                    last_error = 'Antes de emitir, el portal debe reflejar ' + ', '.join(pending_fields) + '.'
                    await self.human(key, page, 'blocked', last_error)
                    continue
                try:
                    issue_button = (walmart_issue_control if walmart_issue_control is not None
                                    else await self.action_control(page, action, receipt))
                    if walmart_issue_control is not None:
                        walmart_issue_element = await issue_button.element_handle()
                        if walmart_issue_element is None:
                            raise ValueError('El botón de Walmart cambió antes de la emisión.')
                        await walmart_issue_element.click(trial=True, timeout=1500)
                    else:
                        await issue_button.click(trial=True, timeout=1500)
                except (ValueError, BrowserError):
                    detail = 'El botón de emisión está deshabilitado, cubierto o ya cambió. Revisa los campos obligatorios y los avisos del portal.'
                    if last_error == detail:
                        await self.human(key, page, 'blocked', detail)
                    last_error = detail
                    continue
                capufe_auto = (receipt.provider == 'capufe' and
                               normalize(name) in {'SI ESTOY SEGURO', 'SÍ ESTOY SEGURO'} and
                               await capufe_proof(page, profile, receipt))
                bp_auto = (receipt.provider == 'bp' and normalize(name) == 'FACTURAR' and
                           await bp_issue_proof(page, receipt, profile, bp_evidence))
                walmart_auto = (walmart_issue_element is not None and
                                await walmart_issue_button(page, profile, receipt, walmart_lookup) is not None)
                # Any other portal emits on its own when the page itself shows this
                # ticket's folio or code, its amount and the recipient RFC.
                generic_auto = (receipt.provider not in {'capufe', 'bp', 'walmart'} and
                                await self.generic_issue_proof(page, profile, receipt))
                valid = (auto_issue and key not in self.explicit_approval and not self.store.get(key)['human_exposed']
                         and (capufe_auto or bp_auto or walmart_auto or generic_auto))
                if not valid:
                    approved_at = (page.url, snapshot)
                    await self.human(key, page, 'issue', 'Revisa ticket, importe, forma de pago y datos fiscales en el portal para aprobar esta emisión.')
                    if approved_at != (page.url, await page.locator('body').aria_snapshot()):
                        last_error = 'La página cambió durante la revisión. Lee el formulario nuevamente.'
                        continue
                    _, pending_fields = await fiscal_choices(page, profile, receipt, extra=self._extra())
                    if pending_fields:
                        last_error = 'La selección fiscal cambió durante la revisión.'
                        continue
                if (walmart_auto and (page.url != walmart_issue_url
                        or await page.locator('body').aria_snapshot() != snapshot)):
                    last_error = 'El formulario Walmart cambió antes de enviar. Se vuelve a cotejar.'
                    continue
                self.store.begin_emission(key, name)
                committed = True
            elif committed or issued:
                recovery_link = issued and action.kind == 'click' and RECOVERY_LINK.search(name)
                recovery_field = (issued and action.kind == 'fill' and
                                  re.search(r'folio|ticket|c[oó]digo|\brfc\b', name, re.I) and
                                  action.value in {receipt.ticket_number, *receipt.billing_codes, profile.rfc})
                if (action.kind not in {'wait', 'scroll'} and not recovery_link and not recovery_field and
                        not (action.kind == 'click' and DOWNLOAD.search(name))):
                    if action.kind == 'click' and re.fullmatch(r'continuar', name.strip(), re.I) and not issued:
                        await self.human(key, page, 'confirm', 'Revisa la confirmación que muestra el portal y completa ese paso si coincide con tu ticket.')
                        continue
                    raise ValueError('Revisa o recupera la factura del intento actual.')
            try:
                before_action = page.url, snapshot
                if walmart_auto and walmart_issue_element is not None:
                    await walmart_issue_element.click(timeout=5000)
                else:
                    await self.execute(page, action, receipt)
                if (walmart_lookup_candidate is not None and
                        urlsplit(page.url).path.rstrip('/') != '/address'):
                    try:
                        await page.wait_for_url(re.compile(
                            r'^https://facturacion-clientes\.walmart\.com/address(?:[/?#].*)?$'), timeout=5000)
                    except BrowserError:
                        pass
                last_error = ''
                idle_steps = 0
            except Exception as exc:
                if committed:
                    raise ValueError('El portal no confirmó el resultado del intento de emisión. Busca el CFDI antes de repetir.')
                last_error = str(exc) if isinstance(exc, ValueError) else 'No se completó el paso. Revisa la pantalla y el control.'
                idle_steps += 1
                control = action.kind + ':' + (action.target.by + '=' + name if action.target else '')
                failures[control] = failures.get(control, 0) + 1
                if failures[control] >= 2:
                    last_error += (' Ese control ya falló ' + str(failures[control]) + ' veces. Si no tiene etiqueta usable, '
                                   'apúntalo por número: target.by="field" y name con su número de la lista campos.')
            recent.append({'tipo': action.kind, 'control': name[:120]})
            self.store.event(key, 'action', action.kind + ' · ' + name[:120])
            if (walmart_lookup_candidate is not None and
                    urlsplit(page.url).path.rstrip('/') == '/address'):
                walmart_lookup = walmart_lookup_candidate
                self.store.event(key, 'ticket_lookup_verified', 'TC, TR, RFC y código postal coincidieron al continuar.')
            if len(recent) >= 4 and all(event.get('tipo') == action.kind and event.get('control') == name[:120]
                                        and event.get('cambio_visible') is False for event in recent[-4:-1]):
                if stalled_hints < 2:
                    stalled_hints += 1
                    last_error = ('Repetiste la misma acción sin cambio visible. Lee los errores con read_text, revisa '
                                  'campos obligatorios vacíos y prueba otro control o ruta.')
                    recent.append({'tipo': 'atasco', 'control': name[:120]})
                    continue
                decision = await self.human(key, page, 'blocked', 'El portal no avanzó tras varios pasos. Toma el control para revisar.')
                if decision == 'manual':
                    return await self.manual_job(key, page, profile)
        raise ValueError('La factura requiere revisar el resultado del portal.')

    async def close(self):
        jobs = list(self.job_tasks.values())
        for task in jobs:
            task.cancel()
        await asyncio.gather(*jobs, return_exceptions=True)
        if self.viewer.dialog:
            await self.viewer.answer(self.viewer.dialog_key, False)
        if self.context:
            await close_context(self.context)
        await self.personal.close()
        if self.downloads:
            await asyncio.gather(*self.downloads, return_exceptions=True)
        if self.playwright:
            await self.playwright.stop()
        if self.owns_plan:
            self.plan.close()
