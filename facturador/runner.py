from __future__ import annotations

import asyncio
import hashlib
import html
import ipaddress
import json
import math
import os
import re
import secrets
import sys
import time
import traceback
import weakref
from collections import deque
from dataclasses import dataclass
from decimal import Decimal, InvalidOperation
from pathlib import Path
from urllib.parse import unquote, urljoin, urlsplit
from playwright.async_api import Error as BrowserError

from .ai import AI, read_receipt
from .account_flow import PortalAccountFlow, SECRET_SCREEN
from .auth import ChatGPTPlan, PlanError, read_json
from .catalog import (CATALOG, CUSTOM_PORTAL, allowed_url, billing_destination, provider_for_url,
                      public_portal_origin, resolves_publicly)
from .captcha_transport import WidgetTransport, widget_origin
from .cfdi import UUID_RE
from .complementos import FINISH, OBSERVE, View, complement_class, moved
from .db import Store
from .documents import job_candidates, reconcile
from .fiscal_choices import NAMES as FISCAL_FIELDS, fiscal_choices, matches as sat_option
from .models import Action, Plan, Profile, Receipt, Target
from .personal_browser import PersonalBrowser
from .portal_accounts import credential_file
from .portal_memory import Attempt, Known, PortalMemory, describe, fold, locate, screen_signature
from .profile_lock import close_context, launch_chrome
from .ticket_lookup import prefill_ticket_lookup
from .ticket_retry import FINAL, REOBSERVE, RecoveryRequested, may_have_issued, open_ticket_portal, recover_ticket, retry_action, ticket_in_result
from .uso_ia import en_ticket
from .viewer import BrowserView

ISSUE = re.compile(r'\b(facturar|emitir|timbrar|generar\s+(?:mi\s+)?(?:factura|cfdi)|solicitar\s+factura|enviar\s+(?:mi\s+)?(?:factura|cfdi)|s[ií] estoy seguro|confirmar (?:la )?emisi[oó]n)\b', re.I)
EMISSION_PATH = re.compile(r'factur|invoice|invoic|cfdi|timbr|emisi|emit|creat|complet|generat|submit', re.I)
FORBIDDEN = re.compile(r'\b(pagar|comprar|transferir|cancelar\s+factura|eliminar|borrar)\b', re.I)
TERMS = re.compile(r'acepto|términos|terminos|condiciones|contrato|privacidad', re.I)
CHALLENGE = re.compile(r'captcha|verificaci[oó]n de seguridad|no soy un robot', re.I)
CHALLENGE_TEXT = re.compile(r'no soy un robot|verifica que eres humano|verify (?:that )?you are human|introdu(?:ce|zca) el captcha|c[oó]digo (?:de )?captcha', re.I)
ACCESS_DENIED = re.compile(r'access denied|error 16\b|blocked by our security service|acceso denegado|err_cert_', re.I)
LOAD_FAILURE = re.compile(r'net::ERR_|(?:error|HTTP)\s*[45]\d\d\b|service unavailable|no se (?:puede acceder|pudo cargar)|bad gateway', re.I)
# Failures the agent can retry from a fresh page because nothing was submitted.
RETRYABLE = re.compile(r'se cerr[oó]|cambi[oó] (?:a un|de) dominio|'
                       r'requiere revisar el resultado del portal|no hay comprobante ni confirmaci[oó]n visible|'
                       r'no avanz[oó]|no est[aá] disponible|no confirm[oó] el clic|destino fuera', re.I)
# Visible text plus a numbered list of every visible field: its label (or the text
# right before it when the page has no label), what is typed or chosen, and its errors.
# Each field gets data-facturai-campo=n so the agent can target it as by="field".
# The numbers are assigned again on every reading, so they only hold for that reading.
# ``items`` and ``buttons`` describe the same fields and the visible buttons without
# their values: the screen signature and the stable field descriptions come from them.
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
  const described = [];
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
    described.push({n: n, kind: kind, label: label.slice(0, 80), key: (e.name || '') + '|' + (e.id || ''),
      filled: (e.type === 'checkbox' || e.type === 'radio') ? e.checked : Boolean(e.value)});
    return '#' + n + ' ' + kind + ' «' + label.slice(0, 80) + '»: ' + String(value).slice(0, 120) +
      (notes.length ? ' (' + notes.join(', ') + ')' : '');
  });
  const buttons = [...document.querySelectorAll('button, [role="button"], input[type="submit"], ' +
      'input[type="button"], input[type="reset"], input[type="image"]')]
    .filter(e => !!(e.offsetWidth || e.offsetHeight || e.getClientRects().length))
    .map(e => clean(e.innerText || e.value || e.getAttribute('aria-label') || e.title).slice(0, 80))
    .filter(Boolean);
  return {text: document.body ? document.body.innerText : '', fields: fields, count: items.length,
          items: described, buttons: buttons};
}"""
# The address a click target leads to when it is a plain link of the page itself: an <a href>
# outside any form, with no handler of its own, that opens another document. Anything that
# could run code or send data answers null.
PLAIN_LINK = r"""e => {
  const a = e.closest('a[href]');
  if (!a || window !== window.top) return null;
  const role = a.getAttribute('role');
  if ((role && role !== 'link') || a.closest('form, button, [role="button"]') ||
      a.querySelector('button, input, select, textarea')) return null;
  const handlers = ['onclick', 'onmousedown', 'onmouseup', 'onpointerdown', 'onpointerup', 'ontouchstart',
                    'ontouchend', 'onkeydown', 'onkeyup', 'onfocus'];
  for (let node = e; node; node = node === a ? null : node.parentElement)
    if (handlers.some(name => node.hasAttribute(name))) return null;
  if (a.hasAttribute('download') || a.hasAttribute('ping')) return null;
  const target = (a.getAttribute('target') || '').toLowerCase();
  if (!['', '_self', '_blank', '_top', '_parent'].includes(target)) return null;
  if (a.origin + a.pathname + a.search === location.origin + location.pathname + location.search) return null;
  return {href: a.href};
}"""
ASK_TIMEOUT = 120
# A code sent to the person's phone is waited for longer. While that question is open, and for
# this long after it closes, the chat treats what arrives as the secret it is.
SECRET_ASK_TIMEOUT = 300
SECRET_GRACE = 900
# Each look-up tool may be used this many times per attempt; the page text comes in every step.
TOOL_LIMITS = {'view_ticket': 2, 'search_web': 2, 'read_text': 4}
CONFIRM_STEP = re.compile(r'CONTINUAR|ACEPTAR|CONFIRMAR|SI|SÍ|FACTURAR|GENERAR(?: FACTURA)?|EMITIR(?: FACTURA)?|'
                          r'TIMBRAR|FINALIZAR')
MAX_QUESTIONS = 3
IDLE_BEFORE_ASK = 10
TOOLS = {'read_text', 'search_web', 'view_ticket', 'back', 'reload', 'upload'}
SUCCESS = re.compile(r'factura\s+(?:emitida|generada)\b|cfdi\s+(?:emitido|generado)\b|timbrad[ao]\s+correctamente|resultado facturaci[oó]n[^\n]*\n[\s\S]{0,130}facturas generadas|se gener[oó]\s+exitosamente', re.I)
DOWNLOAD = re.compile(r'descargar|download|\bpdf\b|\bxml\b|imprimir', re.I)
ALREADY_BILLED = re.compile(
    r'^(?:(?:el|este|su) )?(?:ticket|boleto|c[oó]digo|comprobante)(?: ingresado| [A-Z0-9]{18})? '
    r'ya (?:(?:fue|ha sido|est[áa]|se encuentra) )?facturad[oa]\b', re.I)
RECOVERY_LINK = re.compile(r'\b(?:recuperar|consultar|reencontrar)\b.*\b(?:factura|cfdi|comprobante)\b', re.I)


def __getattr__(name: str):
    # Helpers that moved to a merchant's complement are still found here by whoever imports or patches them.
    return moved(name)


def normalize(text: str) -> str:
    return ' '.join(text.upper().split())


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
# What one attempt may spend. Time waiting for a person does not count.
MAX_AI_CALLS = 40
MAX_ACTIVE_SECONDS = 600
# Attempts that start over «by another route». A lost browser is reopened apart from these.
MAX_ROUTE_ATTEMPTS = 2
LIMIT_MESSAGE = ('No pude terminar este portal en el tiempo previsto. No se envió nada; '
                 'puedes reintentar o tomar el control.')
# These actions travel alone in a plan: their result changes what comes next.
SOLO = TOOLS | {'ask', 'human', 'done'}
# Consultations in which the model is told that the portal wants a file, before the person is asked.
ATTACHMENT_HINTS = 2
ATTACHMENT_PAUSE = 'Toma el control y abre el selector del portal. Elige el documento en el panel y pulsa Adjuntar al portal.'
# Fiscal lists that load late (often after the RFC is typed) get this long before an emission, looked at this often.
FISCAL_WAIT = 8.0
FISCAL_POLL = 0.5
# Times one attempt may wait for those lists at the emission gate.
FISCAL_WAITS = 3
# A repeated screen gets this long to move on before it counts as not having advanced.
SETTLE_TRIES = 12
SETTLE_PAUSE = 0.5
# Breath between repeated actions, so the page reacts as it does between the agent's own.
REPLAY_PAUSE = 0.15
QUESTION_NOISE = {'el', 'la', 'los', 'las', 'de', 'del', 'que', 'en', 'un', 'una', 'y', 'o', 'a', 'al', 'me',
                  'mi', 'tu', 'su', 'es', 'para', 'por', 'con', 'lo', 'se', 'te'}


# The planner is reached through the class function, so anything that answers
# ``next_action`` (the real client or a stand-in) is asked for a whole plan the same way.
PLAN_SCREEN = AI.next_actions


class AttemptLimit(ValueError):
    """The attempt spent its budget. It stops for good instead of starting another one."""


class Meter:
    """What one attempt spends: consultations, actions and active time."""

    def __init__(self):
        self.started = time.monotonic()
        self.waited = 0.0
        self.ai_calls = self.actions = self.replayed = 0
        self.agent = False
        self.outcome = ''

    def active(self) -> float:
        return time.monotonic() - self.started - self.waited


@dataclass
class Planned:
    """An action decided for the visible screen that waits for its turn."""
    action: Action
    source: str                    # 'ia' or 'memoria'
    index: int = 0                 # place in its plan; only 0 was decided on a fresh observation
    field: dict | None = None      # stable description of the field it writes in or presses
    anchor: str = ''               # name and id of that field when planned (same page session only)
    screen: tuple | None = None    # memory: the screen this step was learned on
    link: str = ''                 # memory: value still to choose among the options on the page
    last: bool = False             # memory: last step of its recipe


class Course:
    """The plan in course. Its actions run one per pass, each through the whole pipeline."""

    def __init__(self):
        self.steps: deque[Planned] = deque()
        self.context: tuple = ()
        self.written: list[Planned] = []

    def start(self, steps: list[Planned], context: tuple) -> None:
        self.steps, self.context, self.written = deque(steps), context, []

    def drop(self) -> None:
        self.steps.clear()
        self.written = []

    def broken(self, error: str, context: tuple, items: list[dict]) -> str:
        """Why the rest of the plan can no longer be trusted; '' when it can go on."""
        if not self.steps:
            return ''
        if error:
            return 'un paso falló'
        if context != self.context:
            return 'la página cambió o intervino una persona'
        if self.steps[0].source != 'ia':
            return ''
        if any(step.field and locate(items, step.field, step.anchor) is None for step in self.steps):
            return 'un campo del plan ya no está'
        for step in self.written:
            number = locate(items, step.field, step.anchor)
            if number is None or not any(item.get('n') == number and item.get('filled') for item in items):
                return 'cambió un campo ya capturado'
        return ''

    def carry(self, items: list[dict], screen: tuple) -> None:
        """The screen changed under a repeated recipe: only its field values still due and valid stay."""
        kept = [step for step in self.steps if step.field and step.action.kind in {'fill', 'select'}
                and locate(items, step.field) is not None]
        for step in kept:
            step.screen, step.last = screen, False
        self.steps = deque(kept)


def question_key(text: str) -> str:
    """The same question worded with other accents, case, numbers or filler words."""
    return ' '.join(word for word in re.findall(r'[a-zñ]+', fold(text)) if word not in QUESTION_NOISE)[:400]


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
        # Tickets whose portal already got something in this run: a value typed, chosen or attached,
        # or an account or a person stepping in. Until then a link that only leads to another page
        # is plain navigation, whatever it is called.
        self.captured: set[str] = set()
        # Questions the agent asked in the chat, answered by the person's next message.
        self.questions: dict[str, asyncio.Future] = {}
        # Tickets that asked in the chat for a secret (a code sent to the phone) and until when a
        # message is still taken for it: without end while the question is open, then a few minutes
        # more. The chat hands such an answer over without storing it or showing it to the model.
        self.secret_questions: dict[str, float] = {}
        # Chat messages the person sent while their ticket was being processed.
        self.live_notes: dict[str, list[str]] = {}
        self.manual_download_error = ''
        # What each solved portal taught, as data. A new portal needs no code.
        self.memory = PortalMemory(store.root)
        self.learning: dict[str, Attempt] = {}
        # Per ticket in progress: what its attempt spends, how many times a person was
        # asked to step in or wrote in the chat, and every question of the whole run
        # with its answer ('' while unanswered), so none is asked twice.
        self.meters: dict[str, Meter] = {}
        self.pauses: dict[str, int] = {}
        self.handled: dict[str, list[str]] = {}
        self.told: dict[str, int] = {}
        self.asked: dict[str, dict[str, str]] = {}
        self.insisted: dict[str, set[str]] = {}

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
        if (not complement_class(receipt.provider).prepares_after_negative_check or receipt.ready_errors()
                or not self.store.queue_after_negative_check(key, receipt, profile)):
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
            # Whatever was planned before a person steps in is decided again afterwards,
            # and the time the person takes is not charged to the attempt.
            self.pauses[key] = self.pauses.get(key, 0) + 1
            if kind in MANUAL:
                # The person may act on this page; the memory will not repeat it by itself.
                self.handled.setdefault(key, []).append(page.url)
            waiting_since = time.monotonic()
            try:
                decision = await future
            finally:
                meter = self.meters.get(key)
                if meter:
                    meter.waited += time.monotonic() - waiting_since
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

    async def visible_text(self, page, receipt: Receipt) -> str:
        """Rendered text of the portal page and its same-portal frames."""
        text, fields = await self.page_fields(page, receipt)
        return text + '\n[Campos]\n' + '\n'.join(fields)

    async def page_fields(self, page, receipt: Receipt) -> tuple[str, list[str]]:
        """Visible text and the numbered fields of the portal page and its same-portal frames."""
        state = await self.page_state(page, receipt)
        return state['text'], state['fields']

    async def page_state(self, page, receipt: Receipt) -> dict:
        """One reading of the page: its text, its numbered fields and what identifies the screen.

        Every reading numbers the fields again. ``items`` describes each field without its
        value and ``signature`` is the hash of those descriptions and the visible buttons.
        """
        parts, fields, items, buttons, start = [], [], [], [], 1
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
            items.extend(result.get('items', []))
            buttons.extend(result.get('buttons', []))
            start += int(result.get('count', 0))
        return {'text': '\n'.join(parts), 'fields': fields, 'items': items, 'buttons': buttons,
                'signature': screen_signature(items, buttons)}

    async def planned_steps(self, page, receipt: Receipt, plan: Plan, items: list[dict]) -> tuple[list[Planned], str]:
        """Turn the model's plan into queued steps. Returns them and, if it had to be cut, why.

        A field picked by its number is kept by what it is (kind, label and place among
        equal ones), because the numbers change on every reading. Steps after the first
        must point at something visible now; the plan is cut where one does not, and
        before any action that has to travel alone.
        """
        steps, cut = [], ''
        for index, action in enumerate(plan.steps):
            if action.kind in SOLO and index:
                cut = 'una herramienta o pregunta va sola'
                break
            step = Planned(action, 'ia', index)
            target, number = action.target, None
            if target is not None and target.by == 'field':
                number = target.name.strip().lstrip('#')
                if not number.isdigit() or describe(items, int(number)) is None:
                    if index:
                        cut = 'un número de campo que no está en la lista'
                        break
                    return [], 'Ese número no está en la lista campos de este paso. Usa uno de los números que ves ahora.'
            elif target is not None and action.kind in {'click', 'fill', 'select', 'press'}:
                try:
                    control = await self.action_control(page, action, receipt)
                    number = await control.get_attribute('data-facturai-campo')
                except (ValueError, BrowserError, AttributeError, TypeError):
                    if index:
                        cut = 'un control que todavía no se ve'
                        break
            if isinstance(number, str) and number.isdigit():
                step.field = describe(items, int(number))
                step.anchor = next((item.get('key', '') for item in items if item.get('n') == int(number)), '')
            steps.append(step)
            if action.kind in SOLO:
                if len(plan.steps) > 1:
                    cut = 'una herramienta o pregunta va sola'
                break
        return steps, cut

    def recalled_steps(self, recipe: list[dict], screen: tuple, items: list[dict], known: Known) -> list[Planned] | None:
        """Build this ticket's actions from a remembered recipe; None when it does not fit this screen or ticket."""
        steps = []
        for index, stored in enumerate(recipe):
            target, field, link = stored.get('target'), None, ''
            value = stored.get('value')
            if stored['kind'] not in {'click', 'fill', 'select', 'press', 'upload'}:
                return None
            if target is not None and target.get('by') == 'campo':
                number = locate(items, target)
                if number is None:
                    return None
                field = {key: target[key] for key in ('by', 'kind', 'label', 'ordinal', 'count')}
                aim = Target(by='field', name=str(number), role=None, exact=True)
            elif target is not None:
                aim = Target(by=target['by'], name=target['name'], role=target.get('role'), exact=bool(target.get('exact')))
            else:
                aim = None
            if value is not None and value.startswith('$sat.'):
                link, text = value, None
            elif value is not None:
                text = known.value(value)
                if text is None:
                    return None
            else:
                text = None
            action = Action(kind=stored['kind'], target=aim, value=text, reason='Paso aprendido de un trámite anterior en este portal.',
                            risk='issue' if stored.get('risk') == 'issue' else 'normal')
            steps.append(Planned(action, 'memoria', index, field, '', screen, link, index == len(recipe) - 1))
        return steps or None

    async def chosen_option(self, page, action: Action, receipt: Receipt, kind: str, expected: str) -> str | None:
        """The single option of a list that means this SAT code for the profile or ticket."""
        try:
            control = await self.action_control(page, action, receipt)
            options = await control.locator('option').evaluate_all('(options) => options.map(o => [o.value, o.label])')
        except (ValueError, BrowserError, AttributeError):
            return None
        found = [value for value, label in options if value and sat_option(value, label, kind, expected)]
        return found[0] if len(found) == 1 else None

    async def chosen_label(self, page, action: Action, receipt: Receipt) -> str | None:
        """The text of the option a list shows after choosing, to remember it by what it says."""
        try:
            control = await self.action_control(page, action, receipt)
            return await control.evaluate("e => e.selectedOptions && e.selectedOptions[0] ? e.selectedOptions[0].label : null")
        except (ValueError, BrowserError, AttributeError):
            return None

    def running_job(self) -> str | None:
        """The ticket the agent is working on right now, if any."""
        key = self.active_id
        task = self.job_tasks.get(key) if key else None
        return key if task is not None and not task.done() else None

    def tell(self, key: str, text: str) -> None:
        """Hand a chat message to the agent of a ticket in progress; it reads it on its next step."""
        text = ' '.join(text.split())[:600]
        if not text:
            return
        notes = self.live_notes.setdefault(key, [])
        notes.append(text)
        del notes[:-6]
        self.told[key] = self.told.get(key, 0) + 1
        self.store.event(key, 'user_note', 'El agente recibió un mensaje del chat.')

    def open_question(self) -> str | None:
        """The ticket whose agent is waiting for an answer in the chat."""
        return next((key for key, future in self.questions.items() if not future.done()), None)

    def answer_question(self, key: str, text: str) -> None:
        future = self.questions.get(key)
        if future and not future.done():
            future.set_result(text[:1000])

    def secret_open(self, key: str) -> bool:
        """The question that ticket has open in the chat asks for a secret."""
        return self.secret_questions.get(key) == math.inf and key in self.questions

    def secret_recent(self) -> str | None:
        """The ticket that asked for a secret in the chat, while its question is open or shortly after."""
        now = time.monotonic()
        for key, until in list(self.secret_questions.items()):
            if until < now:
                del self.secret_questions[key]
        return next(iter(self.secret_questions), None)

    async def ask_chat(self, key: str, question: str, *, secreto: bool = False) -> str:
        """Ask the person in the live chat and wait a few minutes; an empty answer means «assume».

        ``secreto`` is for a code only the person has, such as one sent to their phone. The wait
        is longer and nothing is assumed when nobody answers. The question is marked as secret
        while it is open and for a while after, so the chat hands the answer over here without
        storing it, and a code that arrives late goes nowhere. It is not a question of the model:
        it does not count among them nor enter the record of the ones already asked.
        """
        future = asyncio.get_running_loop().create_future()
        self.questions[key] = future
        question = ' '.join(question.split())[:600]
        if secreto:
            self.secret_questions[key] = math.inf
            closing = (' No guardo el código: solo lo escribo en el portal. Si no lo tienes en unos minutos, '
                       'podrás escribirlo tú en el sitio.')
        else:
            closing = ' Respóndeme aquí; si no me contestas en unos minutos, sigo con lo más probable.'
        self.store.add_assistant_message('Sobre tu ticket en proceso: ' + question + closing, [key])
        self.store.running_message(key, 'Te pregunté en el chat: ' + question[:200])
        self.store.event(key, 'question', question[:200])
        waiting_since = time.monotonic()
        try:
            answer = await asyncio.wait_for(asyncio.shield(future), SECRET_ASK_TIMEOUT if secreto else ASK_TIMEOUT)
        except asyncio.TimeoutError:
            answer = ''
        finally:
            self.questions.pop(key, None)
            if not future.done():
                future.cancel()
            if secreto:
                self.secret_questions[key] = time.monotonic() + SECRET_GRACE
            meter = self.meters.get(key)
            if meter:
                meter.waited += time.monotonic() - waiting_since
        self.store.event(key, 'answer', 'Respuesta recibida en el chat.' if answer else
                         'Sin respuesta en el chat.' if secreto else 'Sin respuesta; el agente asume y sigue.')
        return answer

    def can_ask(self, key: str, question: str) -> bool:
        """A new question while the run has some left, or one already asked that can still be settled."""
        topic = question_key(question)
        asked = self.asked.get(key, {})
        if topic in asked:
            return bool(asked[topic]) or topic not in self.insisted.get(key, ())
        return len(asked) < MAX_QUESTIONS

    async def ask_once(self, key: str, page, question: str) -> tuple[str, str]:
        """Ask only what this run has not asked. Returns the answer and the person's decision, if any.

        A question already answered gives its answer back. One left without an answer is
        not sent again: the ticket waits for the person, once, with the question as its message.
        """
        topic = question_key(question)
        asked = self.asked.setdefault(key, {})
        if topic not in asked:
            asked[topic] = ''
            asked[topic] = await self.ask_chat(key, question)
            return asked[topic], ''
        if asked[topic]:
            return asked[topic], ''
        self.insisted.setdefault(key, set()).add(topic)
        asked[topic], decision = await self.wait_for_answer(key, page, question)
        return asked[topic], decision

    async def wait_for_answer(self, key: str, page, question: str) -> tuple[str, str]:
        """Hold the ticket for the person with the pending question as its message.

        It is an ordinary pause: no attempt is spent and the waiting time is not charged.
        An answer in the chat, or the person resuming from the panel, ends it.
        """
        question = ' '.join(question.split())[:600]
        future = asyncio.get_running_loop().create_future()
        self.questions[key] = future

        async def resume_when_answered():
            await future
            try:
                await self.continue_job(key, 'resume')
            except (ValueError, BrowserError):
                pass  # The panel explains what is missing; the pause stays for the person.

        watcher = asyncio.create_task(resume_when_answered())
        self.store.event(key, 'question_waiting', question[:200])
        try:
            decision = await self.human(key, page, 'unknown', 'Necesito tu respuesta para seguir: ' + question[:400])
        finally:
            answered = future.done() and not future.cancelled()
            if not future.done():
                future.cancel()
            await asyncio.gather(watcher, return_exceptions=True)
            if self.questions.get(key) is future:
                self.questions.pop(key, None)
        answer = future.result() if answered else ''
        self.store.event(key, 'answer', 'Respuesta recibida en el chat.' if answer
                         else 'El titular retomó el trámite sin contestar la pregunta.')
        return answer, decision

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

    @staticmethod
    def identifies(text: str, profile: Profile, receipt: Receipt) -> bool:
        """The text carries the recipient's RFC or e-mail, or the folio or a code of the ticket."""
        upper = text.upper()
        compact = re.sub(r'\s+', '', upper)
        if any(value and re.sub(r'\s+', '', value).upper() in compact for value in (profile.rfc, profile.email)):
            return True
        for value in (receipt.ticket_number, *receipt.billing_codes):
            reference = re.sub(r'\s+', '', value or '').upper()
            if len(reference) >= 4 and reference in compact:
                return True
            # A short folio counts only as a word of its own: «12» is part of any page.
            if 0 < len(reference) < 4 and re.search(r'(?<![0-9A-ZÑ])' + re.escape(reference) + r'(?![0-9A-ZÑ])', upper):
                return True
        return False

    @classmethod
    def shows_ticket_or_recipient(cls, text: str, profile: Profile, receipt: Receipt) -> bool:
        """The page shows something of the recipient (RFC, e-mail, name) or of the ticket (folio, code, amount)."""
        if cls.identifies(text, profile, receipt):
            return True
        spaced = ' '.join(text.upper().split())
        name = ' '.join((profile.name or '').upper().split())
        if name and re.search(r'(?<![0-9A-ZÑ])' + re.escape(name) + r'(?![0-9A-ZÑ])', spaced):
            return True
        try:
            amount = Decimal(receipt.amount)
        except (InvalidOperation, TypeError):
            return True
        shown = {Decimal(raw.replace(',', '')) for raw in re.findall(r'\d{1,3}(?:,\d{3})*(?:\.\d{1,2})|\d+(?:\.\d{1,2})?', text)
                 if raw.replace(',', '').replace('.', '').isdigit()}
        return amount in shown

    async def navigation_link(self, key: str, page, action: Action, profile: Profile, receipt: Receipt,
                              accounts=None) -> bool:
        """A click on a link that only leads to another page, while there is nothing to invoice yet.

        Portals often open with «Ir a facturar» as a link to their form. Such a click is navigation
        and not an emission only when all of this holds; in doubt it stays an emission and goes
        through the gate:
        - the model did not call it an emission and its target is a plain <a href> (PLAIN_LINK) to
          another page of the portal or of an invoicing operator, with nothing of the ticket or of
          the recipient in its address;
        - the page holds no captured field and shows no RFC, e-mail or name of the recipient, nor
          the folio, a code or the amount of the ticket;
        - this run typed, chosen or attached nothing yet, no person stepped in, no emission was
          tried and the program keeps no account of this person on the portal.
        The request it causes still goes through the guard of every navigation.
        """
        if (action.kind != 'click' or action.target is None or action.risk != 'normal' or key in self.captured
                or self.pauses.get(key) or key in self.explicit_approval
                or (accounts is not None and accounts.credential_values)):
            return False
        try:
            row = self.store.get(key)
            if (row['issue_attempted'] or row['human_exposed'] or row['issuance_seen'] or row['uuid']
                    or self.store.checked_preparation(key)):
                return False
            link = await (await self.action_control(page, action, receipt)).evaluate(PLAIN_LINK)
            href = link.get('href') if isinstance(link, dict) else None
            if not isinstance(href, str) or not public_portal_origin(href):
                return False
            if not (allowed_url(href, receipt.provider, receipt.portal_url, self._extra()) or billing_destination(href)):
                return False
            # A portal where this person already has an account may know what to invoice without being told.
            origins = {public_portal_origin(page.url), getattr(accounts, 'origin', None)} - {None}
            if any(read_json(credential_file(self.store.root, origin, profile.email), None) for origin in origins):
                return False
            state = await self.page_state(page, receipt)
        except (KeyError, ValueError, OSError, BrowserError, AttributeError, TypeError):
            return False
        if not isinstance(state.get('text'), str) or any(item.get('filled') for item in state['items']):
            return False
        return not (self.identifies(unquote(href), profile, receipt)
                    or self.shows_ticket_or_recipient(state['text'], profile, receipt))

    async def late_fiscal_choices(self, key: str, page, profile: Profile, receipt: Receipt) -> list[str]:
        """Give fiscal lists that load late a few seconds, choosing each one as it arrives.

        Nothing else on the page is touched. Returns what the portal still does not reflect
        when the time is up; an empty list when régimen, uso and forma de pago all match.
        """
        self.store.running_message(key, 'Esperando a que el portal cargue sus listas fiscales.')
        deadline = time.monotonic() + FISCAL_WAIT
        while True:
            await asyncio.sleep(FISCAL_POLL)
            changed, pending = await fiscal_choices(page, profile, receipt, apply=True, extra=self._extra())
            if changed:
                # Choosing one list may bring or change another: it is looked at again.
                self.captured.add(key)
                self.store.event(key, 'campo_verificado', 'Selección cotejada: ' + FISCAL_FIELDS[changed])
            elif not pending:
                return []
            if time.monotonic() >= deadline:
                if changed:
                    _, pending = await fiscal_choices(page, profile, receipt, extra=self._extra())
                return pending

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

    def out_of_budget(self, key: str, what: str, attempted: bool) -> ValueError:
        """The attempt spent its steps, consultations or time: it stops instead of circling.

        Before any emission the ticket is left stopped with a clear message and is not
        retried by itself. After an emission attempt the result is recovered as always.
        """
        meter = self.meters.get(key)
        if meter:
            meter.outcome = 'limit_' + what
        self.store.event(key, 'limit_reached', what)
        if attempted:
            return ValueError('La factura requiere revisar el resultado del portal.')
        return AttemptLimit(LIMIT_MESSAGE)

    def close_learning(self, key: str, *, spared: bool = False) -> None:
        """Settle what the last attempt noted: it is learned only if its invoice was verified."""
        attempt = self.learning.pop(key, None)
        if attempt is None:
            return
        try:
            row = self.store.get(key)
        except KeyError:
            attempt.abandon(penalize=False)
            return
        if row['status'] == 'verified':
            learned = attempt.commit()
            self.memory.release(key)
            if learned:
                self.store.event(key, 'memory_learned', f'{learned} pantallas aprendidas de este portal.')
        elif row['issuance_seen'] and row['status'] == 'issued_waiting_files':
            # The portal confirmed the invoice; it is learned when its files arrive and match.
            attempt.hold(key)
        else:
            attempt.abandon(penalize=not spared)

    def settle_memory(self) -> None:
        """Learn from earlier attempts whose files arrived and were verified after they ended."""
        def status_of(job_id):
            try:
                return self.store.get(job_id)['status']
            except KeyError:
                return None
        self.memory.settle(status_of)

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
        with en_ticket(key):  # Every model call of this run is counted for this ticket.
            await self._run(key, profile, auto_issue, manual)

    async def _run(self, key: str, profile: Profile, auto_issue: bool = False, manual: bool = False):
        async with self.lock:
            if not self.store.claim(key):
                return
            self.active_id = key
            spared = False
            route_attempts = 1
            try:
                for attempt in range(3):
                    try:
                        await self._session(key, profile, auto_issue, manual)
                        return
                    except RecoveryRequested:
                        spared = True
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
                        # An attempt that spent its budget is never started again by itself.
                        if isinstance(failure, AttemptLimit) or attempt >= 2 or not (lost or retryable):
                            raise
                        if not lost:
                            if route_attempts >= MAX_ROUTE_ATTEMPTS:
                                raise
                            route_attempts += 1
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
                # A ticket the person stopped says nothing against what it repeated from memory.
                spared = spared or isinstance(exc, asyncio.CancelledError) or str(exc) == 'Proceso detenido por el usuario.'
                if isinstance(exc, asyncio.CancelledError):
                    raise
            finally:
                self.active_id = None
                self.explicit_approval.discard(key)
                self.captcha_returned.discard(key)
                self.job_origins.pop(key, None)
                self.session_hints.pop(key, None)
                self.soft_submitted.discard(key)
                self.captured.discard(key)
                self.live_notes.pop(key, None)
                for record in (self.pauses, self.handled, self.told, self.asked, self.insisted, self.meters):
                    record.pop(key, None)
                try:
                    self.close_learning(key, spared=spared)
                except Exception as fault:
                    # The memory is a help, never a reason to leave a ticket half closed.
                    self.store.event(key, 'internal_error', type(fault).__name__ + ' en la memoria de portales')
                await self.viewer.set_manual(False)
                await self.finish_job_browser(key)

    async def _session(self, key: str, profile: Profile, auto_issue: bool, manual: bool):
        """One attempt at the portal, measured from start to end."""
        meter = self.meters[key] = Meter()
        # What an earlier attempt of this run noted is not learned: it did not end well.
        earlier = self.learning.pop(key, None)
        if earlier is not None:
            earlier.abandon()
        try:
            await self._attempt(key, profile, auto_issue, manual, meter)
        except RecoveryRequested:
            meter.outcome = 'recovery'
            raise
        except asyncio.CancelledError:
            meter.outcome = 'cancelled'
            raise
        except Exception:
            meter.outcome = meter.outcome or 'error'
            raise
        finally:
            if self.meters.get(key) is meter:
                self.meters.pop(key, None)
            if meter.agent:
                try:
                    row = self.store.get(key)
                    outcome = meter.outcome or ('verified' if row['status'] == 'verified' else
                                                'issued' if row['issuance_seen'] else 'stopped')
                except KeyError:
                    outcome = meter.outcome or 'stopped'
                self.store.event(key, 'session_metrics', json.dumps({
                    'ai_calls': meter.ai_calls, 'actions': meter.actions, 'replayed_actions': meter.replayed,
                    'seconds': round(meter.active(), 1), 'outcome': outcome}))

    async def _attempt(self, key: str, profile: Profile, auto_issue: bool, manual: bool, meter: Meter):
        committed = issued = False
        await self.close_parked_job_pages(key)
        job = self.store.get(key)
        receipt = Receipt(**job['receipt'])
        errors = receipt.ready_errors()
        if errors or (receipt.provider not in CATALOG and receipt.provider != CUSTOM_PORTAL):
            raise ValueError(' · '.join(errors) or 'Portal sin configurar.')
        # What only this merchant's portal needs, with its own state for this attempt. A portal
        # without a complement gets the plain one: every call to it below lets the engine go on.
        # FACTURADOR_COMPLEMENTOS=0 gives the plain one to every merchant.
        complement = complement_class(receipt.provider)(self, key, profile, receipt, auto_issue)
        await complement.start(job)
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
        fiscal_waits = 0
        stalled_hints = 0
        failures: dict[str, int] = {}
        idle_steps = 0
        meter.agent = True
        # The plan in course, what this attempt notes for the memory and its bookkeeping.
        # An attempt that starts over «by another route» does not repeat the remembered one.
        # FACTURADOR_MEMORIA=0 turns the repetition off; what is solved is still learned.
        remembering = not last_error and os.getenv('FACTURADOR_MEMORIA', '1') != '0'
        known = Known(profile, receipt)
        self.settle_memory()
        learner = self.learning[key] = Attempt(self.memory, known)
        course = Course()
        screen = None              # (origin, path, signature) of the last screen read
        acted_on = None            # screen of the last action that ran
        volatile = False           # the last action was a press or click that may change the screen
        seen_pauses = len(self.handled.get(key, ()))
        settling: dict[tuple, int] = {}
        rounds: list[list] = []    # [what the plan was, state before it, whether anything changed]
        tool_counts: dict[str, int] = {}
        tool_streak = 0
        emission_steps = 0
        last_emission_view = None
        answer_note = ''
        before_action = None
        view = View(recent)        # what the complement is shown on each call

        def take(signal) -> str:
            """Keep what a complement left for the engine; returns how this pass goes on."""
            nonlocal last_error, before_action, answer_note, idle_steps
            if signal.error is not None:
                last_error = signal.error
            if signal.before is not None:
                before_action = signal.before
            if signal.note is not None:
                answer_note = signal.note
            if signal.active:
                idle_steps = 0
            if signal.then == OBSERVE or signal.action is not None:
                self.captured.add(key)  # The complement did something of its own on the portal.
            return signal.then

        unbound_press_count = 0
        # A code the portal sends to the person's phone is asked for in the chat as a secret: it
        # is typed in the portal and kept nowhere (see ask_chat and the chat's side of it).
        accounts = PortalAccountFlow(self.store.root, provider['url'], profile, key,
                                     progress=lambda message: self.store.running_message(key, message),
                                     ai=ai, medidor=meter, ticket=receipt,
                                     pedir_codigo=lambda texto: self.ask_chat(key, texto, secreto=True))
        for number in range(MAX_STEPS):
            own = None             # the complement's own next action for this pass, when it has one
            page = self.viewer.page
            status = self.store.get(key)['status']
            if status in {'verified', 'duplicate', 'historical'}:
                return
            if meter.active() > MAX_ACTIVE_SECONDS:
                raise self.out_of_budget(key, 'tiempo', committed or issued)
            if page.is_closed() or not allowed_url(page.url, receipt.provider, receipt.portal_url, self._extra()):
                raise ValueError('El portal se cerró o cambió a un dominio distinto del autorizado.')
            snapshot = await page.locator('body').aria_snapshot()
            view.read(page, snapshot, not committed and not issued)
            then = take(await complement.after_reading(view))
            if then == FINISH:
                return
            if then == OBSERVE:
                continue
            if before_action is not None and recent:
                recent[-1]['cambio_visible'] = before_action != (page.url, snapshot)
                before_action = None
                if volatile and recent[-1]['cambio_visible'] and course.steps and course.steps[0].source == 'ia':
                    # A button, a link or a key changed the screen: what was planned after it
                    # was decided for the screen before. It is observed and planned again.
                    course.drop()
                    recent.append({'tipo': 'plan_interrumpido', 'control': 'la pantalla cambió'})
            volatile = False
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
                    self.captured.add(key)
                    course.drop()
                    if kind == 'advanced':
                        self.store.event(key, 'portal_account', message)
                    else:
                        await self.human(key, page, kind, message)
                    continue
            attachment_hint = ''
            if challenge == 'attachment' and attachment_tries < ATTACHMENT_HINTS and not committed and not issued:
                # The portal wants a file and the agent may still attach the ticket by itself: a
                # remembered recipe does it without asking, and the model is told how each time it is
                # consulted. A try is spent only when the hint really travels to the model.
                attachment_hint = ('El portal exige un archivo. Si pide la foto o el PDF del ticket, usa upload con '
                                   'value="ticket" sobre el campo o botón del archivo. Si pide otro documento, usa human.')
                challenge = None
            if challenge:
                message = {'captcha': 'Completa la verificación en este navegador.',
                           'login': 'Inicia sesión en el portal y regresa al trámite.',
                           'attachment': ATTACHMENT_PAUSE}[challenge]
                await self.human(key, page, challenge, message, challenge_seen=True)
                continue
            if accounts.private_observation(snapshot):
                await self.human(key, page, 'login', 'El portal muestra una credencial. Ocúltala o avanza en la misma página para continuar.')
                continue
            pending_fields = []
            if not committed and not issued:
                # The merchant's own form comes first, then the engine's rules for any portal.
                signal = await complement.before_fiscal_choices(view)
                then = take(signal)
                if then == FINISH:
                    return
                if then == OBSERVE:
                    continue
                own = signal.action
                changed, pending_fields = await fiscal_choices(page, profile, receipt, apply=True, extra=self._extra())
                pending_fields = [*signal.pending, *pending_fields]
                if changed:
                    self.captured.add(key)
                    self.store.event(key, 'campo_verificado', 'Selección cotejada: ' + FISCAL_FIELDS[changed])
                    continue
                then = take(await complement.after_fiscal_choices(view))
                if then == FINISH:
                    return
                if then == OBSERVE:
                    continue
                try:
                    lookup_field = await prefill_ticket_lookup(page, receipt.ticket_number)
                except BrowserError:
                    lookup_field = None
                if lookup_field:
                    self.captured.add(key)
                    self.store.event(key, 'campo_verificado', 'Dato del ticket cotejado: ' + lookup_field)
                    continue
                then = take(await complement.after_ticket_lookup(view))
                if then == FINISH:
                    return
                if then == OBSERVE:
                    continue
            try:
                state = await self.page_state(page, receipt)
            except BrowserError:
                state = {'fields': [], 'items': [], 'signature': screen_signature([], [])}
            fields, items = state['fields'], state['items']
            # Where the agent stands: the screen every remembered step and every note refers to.
            screen = (public_portal_origin(page.url) or '', known.route(urlsplit(page.url).path), state['signature'])
            touched = self.handled.get(key, [])
            if len(touched) != seen_pauses:
                # A person had the browser. What they did is unknown, so neither the screen
                # they were on nor the one they left is repeated from memory later.
                for address in touched[seen_pauses:]:
                    place = (public_portal_origin(address) or '', known.route(urlsplit(address).path))
                    if acted_on is not None and acted_on[:2] == place:
                        learner.taint(acted_on)
                seen_pauses = len(touched)
                learner.taint(screen)
            if accounts.credential_values and not learner.blocked:
                learner.block(screen)
            # Long without progress: ask in the chat once, then keep going with an assumption.
            if idle_steps >= IDLE_BEFORE_ASK and not committed and not issued and not accounts.credential_values:
                stuck = ('No he podido avanzar en el portal de ' + receipt.merchant[:80] + '. Lo que me frena: ' +
                         (last_error or 'el formulario no acepta lo que capturo')[:250] +
                         ' ¿Me dices qué hago o qué dato falta?')
                if self.can_ask(key, stuck):
                    idle_steps = 0
                    answer, decision = await self.ask_once(key, page, stuck)
                    if decision == 'manual':
                        return await self.manual_job(key, page, profile)
                    answer_note = answer or 'Sin respuesta: asume lo más razonable y toma otra ruta.'
                    continue
            # What must stay the same for a plan to go on: the address, no person stepping in
            # or writing in the chat, and the same side of the emission.
            context = (page.url, self.pauses.get(key, 0), self.told.get(key, 0), committed, issued)
            if course.steps:
                head = course.steps[0]
                reason = 'el ejecutor emite por sus propias reglas' if own is not None else course.broken(
                    last_error, context, items)
                if reason:
                    if head.source == 'memoria' and last_error and learner.replay_failed(head.screen):
                        self.store.event(key, 'memory_disabled', 'Una pantalla aprendida falló tres veces seguidas.')
                    course.drop()
                    recent.append({'tipo': 'plan_interrumpido', 'control': reason})
                elif head.source == 'memoria' and head.screen != screen:
                    # The screen changed under a repeated recipe. Its field values still due and
                    # valid here are kept; whatever else it had belonged to the screen before.
                    learner.replay_done(head.screen)
                    course.carry(items, screen)
            if (not course.steps and own is None and not accounts.credential_values and remembering
                    and not (last_error or tool_result or ticket_view or answer_note or self.live_notes.get(key))):
                # Nothing planned: a screen already solved is repeated from memory before asking the model.
                # Not after an error, nor once the person wrote or answered something: the model must read it.
                progress = learner.state(screen)
                if progress == 'done' and acted_on == screen:
                    if settling.get(screen, 0) < SETTLE_TRIES:
                        # Just repeated and still here: give the portal a moment to move on.
                        settling[screen] = settling.get(screen, 0) + 1
                        await asyncio.sleep(SETTLE_PAUSE)
                        continue
                    # Repeated without advancing: it counts against the recipe and the model takes over.
                    if learner.replay_failed(screen):
                        self.store.event(key, 'memory_disabled', 'Una pantalla aprendida falló tres veces seguidas.')
                elif not progress or (progress == 'running' and learner.starts(screen) < 2):
                    if await page.locator('body').aria_snapshot() != snapshot:
                        continue  # The page is still drawing itself: read it again before recalling anything.
                    related = (allowed_url(page.url, receipt.provider, receipt.portal_url)
                               or public_portal_origin(page.url) in self._extra())
                    recipe, family = learner.recall(screen, related)
                    if recipe and (committed or issued) and any(step.get('risk') == 'issue' for step in recipe):
                        recipe = None  # After an emission nothing remembered may emit again.
                    try:
                        remembered = self.recalled_steps(recipe, screen, items, known) if recipe else None
                    except (KeyError, TypeError, ValueError):
                        remembered = None  # A recipe that cannot be read is the same as none.
                    if remembered and remembered[0].field is None and remembered[0].action.target is not None:
                        try:
                            await self.action_control(page, remembered[0].action, receipt)
                        except (ValueError, BrowserError, AttributeError, TypeError):
                            remembered = None  # Same shape, another screen: its first control is not here.
                    if remembered:
                        course.start(remembered, context)
                        learner.replay_started(screen, recipe, family)
                        self.store.event(key, 'memory_replay', f'{len(remembered)} pasos sin consultar al asistente' +
                                         (' (portal de la misma familia).' if family else '.'))
                    elif screen not in learner.missed:
                        learner.missed.add(screen)
                        self.store.event(key, 'memory_miss', 'Pantalla sin receta aprendida: decide el asistente.')
            # Accessibility snapshots include complete hrefs on separate /url: lines.
            model_snapshot = '\n'.join(line for line in snapshot.splitlines() if not line.lstrip().startswith('- /url:'))
            confirmed = {}
            confirmed_payment = self.store.get(key)['confirmed_payment']
            if confirmed_payment and confirmed_payment == receipt.payment_code:
                confirmed['forma_de_pago_sat'] = confirmed_payment
            observation = json.dumps({'url': public_portal_origin(page.url), 'paso': number, 'pasos_maximos': MAX_STEPS,
                                      'consultas': meter.ai_calls, 'consultas_maximas': MAX_AI_CALLS,
                                      'emision_intentada': committed, 'emision_visible': issued,
                                      'tus_notas': agent_notes, 'acciones_recientes': recent[-15:],
                                      'ultimo_error': ' '.join(filter(None, (last_error, attachment_hint))),
                                      'resultado_herramienta': tool_result,
                                      'campos_fiscales_pendientes': pending_fields,
                                      'campos': fields[:120], 'respuesta_del_titular': answer_note,
                                      'mensajes_del_titular': self.live_notes.get(key, []),
                                      'datos_confirmados': confirmed,
                                      'operadores_autorizados': sorted(self._extra()),
                                      'estado_visible': model_snapshot[:26_000]}, ensure_ascii=False)
            entry = None
            try:
                if own is not None:
                    latest = await page.locator('body').aria_snapshot()
                    if (latest != snapshot or await visible_challenge(page, latest) or self.viewer.page is not page or
                            not allowed_url(page.url, receipt.provider, receipt.portal_url, self._extra())):
                        if attachment_hint:
                            attachment_tries += 1  # Nobody here can attach it: the pause comes as it always did.
                        continue
                    action = own
                elif not course.steps:
                    if meter.ai_calls >= MAX_AI_CALLS:
                        raise self.out_of_budget(key, 'consultas', committed or issued)
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
                    gate = await visible_challenge(page, latest)
                    # The file the portal still asks for is no reason to hide the page while the agent
                    # may attach the ticket: the hint has to reach the model with this observation.
                    if (latest != snapshot or accounts.private_observation(latest)
                            or (gate and not (gate == 'attachment' and attachment_hint)) or
                            self.viewer.page is not page or not allowed_url(page.url, receipt.provider, receipt.portal_url, self._extra())):
                        continue
                    if attachment_hint:
                        attachment_tries += 1
                    # Ordinary steps think briefly; a step after an error or without progress thinks harder.
                    effort = 'high' if (last_error or attachment_hint or idle_steps >= 3 or ticket_view) else 'medium'
                    meter.ai_calls += 1
                    # One consultation decides the whole screen: every safe action it can settle now.
                    plan = await asyncio.to_thread(PLAN_SCREEN, ai, instructions, observation, image, ticket_view,
                                                   effort)
                    ticket_view = None
                    plan_failures = 0
                    tool_result = ''
                    decided, cut = await self.planned_steps(page, receipt, plan, items)
                    if not decided:
                        last_error = cut
                        continue
                    outline = [(step.action.kind, step.action.target.by + '=' + step.action.target.name
                                if step.action.target else '', step.action.value) for step in decided]
                    if rounds and rounds[-1][2] is None:
                        rounds[-1][2] = rounds[-1][1] != (page.url, snapshot)
                    if len(decided) > 1 and len(rounds) >= 2 and all(
                            before[0] == outline and before[2] is False for before in rounds[-2:]):
                        # The same plan twice with nothing to show for it: a third time will not help.
                        if stalled_hints < 2:
                            stalled_hints += 1
                            last_error = ('Repetiste el mismo plan sin cambio visible. Lee los errores con read_text, '
                                          'revisa campos obligatorios vacíos y prueba otro control o ruta.')
                            recent.append({'tipo': 'atasco', 'control': 'plan repetido'})
                            continue
                        decision = await self.human(key, page, 'blocked', 'El portal no avanzó tras varios pasos. Toma el control para revisar.')
                        if decision == 'manual':
                            return await self.manual_job(key, page, profile)
                        continue
                    rounds.append([outline, (page.url, snapshot), None])
                    course.start(decided, context)
                    if cut:
                        recent.append({'tipo': 'plan_recortado', 'control': cut})
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
            if own is None:
                # One action per pass. Whatever its origin, it goes through everything below.
                entry = course.steps.popleft()
                action = entry.action
            located = action
            if entry is not None and entry.field is not None:
                # A field is found again by what it is; its number belongs to this reading only.
                position = locate(items, entry.field, entry.anchor)
                if position is None:
                    if entry.source == 'memoria':
                        learner.replay_failed(entry.screen)
                    course.drop()
                    recent.append({'tipo': 'plan_interrumpido', 'control': 'un campo del plan ya no está'})
                    continue
                located = action.model_copy(update={'target': Target(by='field', name=str(position), role=None, exact=True)})
                if action.target.by == 'field':
                    action = located
            if entry is not None and entry.link:
                # A remembered choice among the SAT options is made with this profile and ticket.
                chosen = await self.chosen_option(page, located, receipt, entry.link[5:], known.sat.get(entry.link[5:], ''))
                if chosen is None:
                    learner.replay_failed(entry.screen)
                    course.drop()
                    recent.append({'tipo': 'plan_interrumpido', 'control': 'la lista no ofrece el dato del expediente'})
                    continue
                action = action.model_copy(update={'value': chosen})
                located = located.model_copy(update={'value': chosen})
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
                question = action.value or action.reason
                if committed or issued or not self.can_ask(key, question):
                    last_error = 'Ya no puedes preguntar más: asume el dato más razonable y sigue.'
                    continue
                idle_steps = 0
                # A question is asked once in the whole run; see ask_once.
                answer, decision = await self.ask_once(key, page, question)
                if decision == 'manual':
                    return await self.manual_job(key, page, profile)
                answer_note = answer or 'Sin respuesta: asume lo más razonable y sigue.'
                recent.append({'tipo': 'ask', 'control': (action.value or '')[:120]})
                continue
            if action.kind in TOOLS:
                tool_counts[action.kind] = tool_counts.get(action.kind, 0) + 1
                tool_streak += 1
                limit = TOOL_LIMITS.get(action.kind)
                if (limit is not None and tool_counts[action.kind] > limit) or (
                        tool_streak > 2 and action.kind in {'read_text', 'view_ticket', 'search_web'}):
                    idle_steps += 1
                    last_error = ('Ya tienes esa información: el texto y los campos vienen en cada paso y el ticket ya '
                                  'lo revisaste. No vuelvas a consultar: haz la siguiente acción del formulario. '
                                  'No investigues si un botón emite; el ejecutor coteja y protege la emisión.')
                    recent.append({'tipo': action.kind, 'control': 'rechazada: ya consultado'})
                    continue
                idle_steps += 1
                if action.kind == 'search_web':
                    if meter.ai_calls >= MAX_AI_CALLS:
                        raise self.out_of_budget(key, 'consultas', committed or issued)
                    meter.ai_calls += 1
                tool_result, ticket_view, failure = await self.use_tool(key, page, located, receipt, ai, committed or issued)
                last_error = failure
                if not failure and action.kind in {'back', 'reload', 'upload'}:
                    before_action = page.url, snapshot
                    meter.actions += 1
                    acted_on = screen
                    if action.kind == 'upload':
                        self.captured.add(key)
                        learner.step(screen, action, entry.field if entry else None, 'normal', entry.source if entry else 'ia')
                        if entry is not None and entry.source == 'memoria':
                            meter.replayed += 1
                            # Nobody asked the model for this: its result must not hold the memory back later.
                            tool_result = ''
                            if entry.last:
                                learner.replay_done(entry.screen)
                    else:
                        learner.mark(screen, action.kind)
                elif failure and entry is not None and entry.source == 'memoria':
                    learner.replay_failed(entry.screen)
                    course.drop()
                label = action.value or (action.target.name if action.target else '')
                recent.append({'tipo': action.kind, 'control': label[:120],
                               **({'origen': 'memoria'} if entry is not None and entry.source == 'memoria' else {})})
                self.store.event(key, 'tool', action.kind + ' · ' + (action.reason or '')[:120])
                continue
            tool_streak = 0
            risk = action_risk(action)
            name = action.target.name if action.target else ''
            if (risk == 'issue' and not committed and not issued
                    and not (entry is not None and entry.source == 'ia' and entry.index)
                    and await self.navigation_link(key, page, located, profile, receipt, accounts)):
                # A link of the portal's first pages that only leads to its form: followed like any other.
                risk = 'normal'
            view.act(action, name, not committed and not issued)
            # The portal's own meaning of this control may raise the risk or lower it.
            risk = complement.adjust_risk(view, risk)
            if (entry is not None and entry.source == 'ia' and entry.index
                    and (action_risk(action) != 'normal' or risk != 'normal')):
                # Lock: an action that emits, confirms or belongs to the person runs only as the
                # first step of a plan just asked for with a fresh observation. Found further
                # back, it and the rest are discarded and the screen is observed and planned again.
                course.drop()
                recent.append({'tipo': 'plan_interrumpido', 'control': 'una acción de riesgo va sola y al final'})
                continue
            if attachment_hint and (risk in {'issue', 'confirm', 'unknown', 'terms', 'blocked'}
                                    or action.kind in {'human', 'done'}):
                # The portal still wants its file and the agent did not attach it: nothing is sent,
                # confirmed or approved before the person attaches it, as it always was.
                attachment_tries = ATTACHMENT_HINTS
                course.drop()
                await self.human(key, page, 'attachment', ATTACHMENT_PAUSE, challenge_seen=True)
                continue
            if risk == 'confirm' and (committed or issued):
                risk = 'normal'
            elif risk == 'confirm':
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
            then = take(await complement.check_action(view))
            if then == FINISH:
                return
            if then == OBSERVE:
                continue
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
                    and not committed and not issued and not accounts.credential_values
                    and self.can_ask(key, action.reason or 'Necesito un dato para continuar con el portal.')):
                idle_steps = 0
                answer, decision = await self.ask_once(
                    key, page, action.reason or 'Necesito un dato para continuar con el portal.')
                if decision == 'manual':
                    return await self.manual_job(key, page, profile)
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
            then = take(await complement.before_acting(view))
            if then == FINISH:
                return
            if then == OBSERVE:
                continue
            # An action that reads like an emission but only opens the portal's own confirmation.
            preparing = complement.prepares_issue(view)
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
                if (not committed and not issued and risk != 'issue' and not preparing
                        and action.kind not in {'fill', 'select', 'wait', 'scroll'}
                        and not complement.known_preparation(view)):
                    await self.human(key, page, 'blocked', 'El siguiente control no se reconoce como preparación del ticket. Revisa este paso en el visor.')
                    continue
            if preparing:
                then = take(await complement.prove_preparation(view))
                if then == FINISH:
                    return
                if then == OBSERVE:
                    continue
            if risk == 'issue' and not preparing:
                if (committed or issued) and entry is not None and entry.source == 'memoria':
                    # A remembered emission step never runs again after an emission: the model decides.
                    course.drop()
                    last_error = 'Ya hubo un intento de emisión: solo quedan descargas o recuperar el resultado.'
                    continue
                if committed or issued:
                    raise ValueError('Ya hubo un intento de emisión. Consulta o recupera ese CFDI.')
                if action.kind != 'click' or not action.target.exact:
                    last_error = 'Para emitir, elige el botón o enlace exacto de la confirmación.'
                    continue
                # The merchant's own proofs come before the engine's; they can only stop the emission.
                then = take(await complement.before_issue(view))
                if then == FINISH:
                    return
                if then == OBSERVE:
                    continue
                _, pending_fields = await fiscal_choices(page, profile, receipt, extra=self._extra())
                if pending_fields and fiscal_waits < FISCAL_WAITS:
                    # The lists may still be loading, whoever decided this step. They get a few
                    # seconds and are chosen as they arrive, before anybody is asked.
                    fiscal_waits += 1
                    pending_fields = await self.late_fiscal_choices(key, page, profile, receipt)
                    if not pending_fields:
                        # The form is not the one this step was decided for: it runs again, whole,
                        # from a fresh reading of the page.
                        if entry is not None:
                            course.steps.appendleft(entry)
                        continue
                if pending_fields:
                    last_error = 'Antes de emitir, el portal debe reflejar ' + ', '.join(pending_fields) + '.'
                    await self.human(key, page, 'blocked', last_error)
                    continue
                try:
                    issue_button = await self.action_control(page, located, receipt)
                    await issue_button.click(trial=True, timeout=1500)
                except (ValueError, BrowserError):
                    detail = 'El botón de emisión está deshabilitado, cubierto o ya cambió. Revisa los campos obligatorios y los avisos del portal.'
                    if last_error == detail:
                        await self.human(key, page, 'blocked', detail)
                    last_error = detail
                    continue
                # A complement with a proof of its own decides by it alone. Any other portal emits
                # on its own when the page itself shows this ticket's folio or code, its amount and
                # the recipient RFC.
                proven = await complement.issue_proof(view)
                if proven is None:
                    proven = await self.generic_issue_proof(page, profile, receipt)
                valid = (auto_issue and key not in self.explicit_approval and not self.store.get(key)['human_exposed']
                         and proven)
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
                then = take(await complement.before_recording(view))
                if then == FINISH:
                    return
                if then == OBSERVE:
                    continue
                self.store.begin_emission(key, name)
                committed = True
                last_emission_view = page.url, snapshot
            elif committed or issued:
                recovery_link = issued and action.kind == 'click' and RECOVERY_LINK.search(name)
                recovery_field = (issued and action.kind == 'fill' and
                                  re.search(r'folio|ticket|c[oó]digo|\brfc\b', name, re.I) and
                                  action.value in {receipt.ticket_number, *receipt.billing_codes, profile.rfc})
                if (action.kind not in {'wait', 'scroll'} and not recovery_link and not recovery_field and
                        not (action.kind == 'click' and DOWNLOAD.search(name))):
                    if (action.kind == 'click' and not issued and CONFIRM_STEP.fullmatch(normalize(name))
                            and emission_steps < 3 and last_emission_view != (page.url, snapshot)):
                        # The portal asks to confirm the emission already started: one click per new screen.
                        emission_steps += 1
                        last_emission_view = page.url, snapshot
                        self.store.event(key, 'emission_step', 'Se confirmó el paso del portal: ' + name[:80])
                    elif action.kind == 'click' and re.fullmatch(r'continuar', name.strip(), re.I) and not issued:
                        await self.human(key, page, 'confirm', 'Revisa la confirmación que muestra el portal y completa ese paso si coincide con tu ticket.')
                        continue
                    elif entry is not None and entry.source == 'memoria':
                        # A remembered step that does not fit after the emission is dropped, not an error.
                        learner.replay_failed(entry.screen)
                        course.drop()
                        last_error = 'Tras la emisión solo quedan descargas o recuperar el resultado.'
                        continue
                    else:
                        raise ValueError('Revisa o recupera la factura del intento actual.')
            worked = False
            try:
                before_action = page.url, snapshot
                if entry is not None and entry.field is not None:
                    # Lock: never write by an old number. The page is read again right before
                    # acting and the field is found by what it is, or the step does not run.
                    position = locate((await self.page_state(page, receipt))['items'], entry.field, entry.anchor)
                    if position is None:
                        raise ValueError('El campo planeado ya no está en la pantalla. Lee de nuevo el formulario.')
                    located = located.model_copy(update={
                        'target': Target(by='field', name=str(position), role=None, exact=True)})
                if action.kind in {'fill', 'select', 'goto'}:
                    self.captured.add(key)  # From here on the portal may hold something of this ticket.
                await self.execute(page, located, receipt)
                await complement.after_acting(view)
                last_error = ''
                idle_steps = 0
                worked = True
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
            logged = {'tipo': action.kind, 'control': name[:120]}
            if worked:
                meter.actions += 1
                acted_on = screen
            if worked and entry is not None:
                if entry.field is not None:
                    logged['campo'] = entry.field['label']
                    if action.kind in {'fill', 'select'} and located.value:
                        course.written.append(entry)
                # A button, a link or a key may change the screen the rest of the plan was decided for.
                volatile = entry.source == 'ia' and entry.field is None and action.kind in {'click', 'press', 'goto'}
                shown = await self.chosen_label(page, located, receipt) if action.kind == 'select' else None
                learner.step(screen, action.model_copy(update={'value': shown}) if shown else action,
                             entry.field, risk, entry.source, '' if entry.field else name)
                if entry.source == 'memoria':
                    logged['origen'] = 'memoria'
                    meter.replayed += 1
                    if entry.last:
                        learner.replay_done(entry.screen)
                    await asyncio.sleep(REPLAY_PAUSE)
            elif entry is not None:
                # A failed step ends its plan; a remembered one also counts against its recipe.
                if entry.source == 'memoria' and learner.replay_failed(entry.screen):
                    self.store.event(key, 'memory_disabled', 'Una pantalla aprendida falló tres veces seguidas.')
                course.drop()
            recent.append(logged)
            # A field picked by number is named by its label, unless the portal echoes personal data there.
            by_number = entry is not None and entry.field and action.target.by == 'field'
            shown_name = entry.field['label'] if by_number and not known.reveals(entry.field['label']) else name
            self.store.event(key, 'action', action.kind + ' · ' + shown_name[:120])
            complement.after_logging(view)
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
        raise self.out_of_budget(key, 'pasos', committed or issued)

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
