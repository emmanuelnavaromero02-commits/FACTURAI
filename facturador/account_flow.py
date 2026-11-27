"""Resolve a single merchant's billing registration using the owner's confirmed profile."""
import asyncio
import inspect
import re
import time
from email.utils import getaddresses
from urllib.parse import urlsplit

from playwright.async_api import Error as BrowserError

from .auth import read_json, write_private
from .buzon import mailbox_connected
from .catalog import public_portal_origin
from .gmail import read_selected_message, search_tickets
from .portal_accounts import REGISTRATION_PAGE as PAGE, confirm_password_change, credential_file, prepare_form, site_password
from .portal_mail import mail_domain, matches_sender, registration_actions  # noqa: F401 - kept as patch points
from .portal_mail import registration_email as mailbox_registration_email
from .registro_asistido import REHUSA, AltaAsistida


ACCOUNT_UI = re.compile(r'contrase[ñn]a|password|registr|crear.{0,12}cuenta|activaci[oó]n|activa tu cuenta|'
                        r'iniciar sesi[oó]n|verifica.{0,20}correo|c[oó]digo de (?:activaci[oó]n|verificaci[oó]n)', re.I)
SECRET_SCREEN = re.compile(r'c[oó]digo de (?:acceso|seguridad|autenticaci[oó]n|inicio de sesi[oó]n)|'
                           r'segundo factor|doble factor|verificaci[oó]n (?:en|de) dos pasos|'
                           r'(?:autenticador|authenticator app|two.factor authentication|multi.factor authentication)|'
                           r'one.time (?:code|password)|(?:password|contrase[ñn]a|clave temporal)\s*:\s*\S+|'
                           r'(?:recovery code|c[oó]digo de recuperaci[oó]n)', re.I)
CONFIRMED_PASSWORD = re.compile(r'(?:contrase[ñn]a (?:se (?:ha )?)?(?:cambi[oó]|actualiz[oó]|actualizada|cambiada)'
                                r'|password (?:was )?(?:changed|updated))', re.I)
HOLD = 'unknown'
# Wording of the screens the assisted paths understand beyond the fixed rules. It only opens the
# way when there is something to do with it: a saved account to sign in with, or the owner's
# permission to register.
SIGN_IN_UI = re.compile(r'inici[ao]r? sesi[oó]n|inicio de sesi[oó]n|ingresa a tu cuenta|accede a tu cuenta|'
                        r'acceso (?:a|de) (?:clientes|usuarios)|\blog ?in\b|\bsign in\b', re.I)
SIGN_UP_UI = re.compile(r'reg[ií]str|crea\w* (?:tu |una |mi |su )?cuenta|nuev[oa] (?:usuario|cuenta)|'
                        r'alta de (?:usuarios?|cuenta|clientes?)|\bsign ?up\b|create (?:an |your |my )?account', re.I)
# What a merchant says while an enrollment waits for its e-mail; it counts for the ticket that began it.
ENROLLMENT_UI = re.compile(r'activa\w* (?:tu |su |la )?cuenta|revisa (?:tu |su )?(?:bandeja|correo)|'
                           r'confirma\w* (?:tu |el )?(?:registro|correo)|te enviamos un (?:correo|mensaje)|'
                           r'check your (?:email|inbox)|verify your email', re.I)
_PHONE = r'(?:sms|whatsapp|mensaje de texto|celular|tel[eé]fono|m[oó]vil)'
_CODE = r'(?:c[oó]digo|clave|\bpin\b)'
# A code and the phone it was sent to, named in the same sentence.
PHONE_UI = re.compile(_CODE + r'[^.\n]{0,140}' + _PHONE + '|' + _PHONE + r'[^.\n]{0,140}' + _CODE, re.I)
# The merchant's registration e-mail is awaited this long, looking again at these intervals.
MAIL_WAIT = 600
MAIL_POLLS = (10, 10, 10, 15, 15)
MAIL_POLL = 20
# Codes sent to the phone that one attempt may ask the person for.
PHONE_CODES = 2
# The question names the portal that asks, so the person can tell its message from anybody else's.
PHONE_QUESTION = ('El portal %s dice que mandó un código a tu teléfono por SMS o WhatsApp. '
                  'Si el mensaje que te llegó es de ese comercio, escríbeme aquí solo el código.')
PHONE_PAUSE = 'El portal envió un código a tu teléfono. Escríbelo en el sitio y pulsa Continuar.'
# The clock of the waits, apart so a test can move it without touching the event loop's.
monotonic = time.monotonic


def authenticated_registration(message, host):
    """Require Gmail's own inbound aligned DMARC result for unattended handling."""
    sender = str(message.get('From', ''))
    if not matches_sender(sender, host):
        return False
    reports = message.get_all('Authentication-Results', [])
    if not reports:
        return False
    authority, _, results = str(reports[0]).partition(';')
    if authority.strip().casefold() != 'mx.google.com':
        return False
    result = re.search(r'(?:^|;)\s*dmarc=pass\b[^;]*?\bheader\.from=([a-z0-9.-]+)(?:\s|;|$)', results, re.I)
    from_host = getaddresses([sender])[0][1].rpartition('@')[2].casefold()
    return bool(result and result.group(1).lower() == from_host)


def registration_email(root, origin, recipient, started, kind):
    """Look only at messages received after this specific enrollment began, in any connected mailbox."""
    # The Gmail readers and its DMARC rule are passed from here so they stay patchable in this module.
    return mailbox_registration_email(root, origin, recipient, started, kind, search=search_tickets,
                                      read=read_selected_message, authenticated=authenticated_registration)


def waited_text(seconds):
    """How long the wait has lasted, the way a person says it."""
    minutes, rest = divmod(int(seconds), 60)
    if not minutes:
        return f'{rest} s'
    return f'{minutes} min' + (f' {rest} s' if rest else '')


def phone_code(answer):
    """The code inside what the person wrote in the chat, or '' when it is not one clear code."""
    text = str(answer or '').strip()
    if re.fullmatch(r'\d(?:[\s.-]?\d){3,9}', text):
        return re.sub(r'\D', '', text)
    found = {match for match in re.findall(r'(?<![\w-])(?=[A-Za-z0-9]*\d)[A-Za-z0-9]{4,10}(?![\w-])', text)}
    return found.pop() if len(found) == 1 else ''


class PortalAccountFlow:
    def __init__(self, root, approved_url, profile, job_id, progress=None, *, ai=None, pedir_codigo=None,
                 medidor=None, ticket=None):
        """``ai`` plans registration forms when the owner allowed it; without it they stay with the person.

        ``pedir_codigo(texto) -> str`` (plain or awaitable) asks the person in the chat for a code sent to
        their phone and returns it, or '' when nobody answered. ``medidor`` is the meter of the attempt:
        consultations are counted on it and the wait for the merchant's e-mail is not charged as work.
        ``ticket`` is the receipt being billed: a page that shows it is never taken for a registration.
        """
        self.root = root
        self.origin = public_portal_origin(approved_url)
        self.profile = profile
        self.job_id = job_id
        self.progress = progress
        self.followed_signup = False
        self.credential_values = set()
        self.ai = ai
        self.pedir_codigo = pedir_codigo
        self.meter = medidor
        # What identifies the ticket on a page: its folio, its codes and its amount.
        amount = getattr(ticket, 'amount', '')
        self.ticket_marks = [value for value in (getattr(ticket, 'ticket_number', ''),
                                                 *(getattr(ticket, 'billing_codes', None) or []))
                             if isinstance(value, str) and len(value.strip()) >= 6]
        if isinstance(amount, str) and re.fullmatch(r'\d+\.\d{2}', amount):
            self.ticket_marks.append(amount)
        self.assistant = AltaAsistida(self)
        # A registration step was just sent: the next screen may continue it without saying so.
        self.following = False
        self.identifier_sent = False
        self.codes_asked = 0

    def say(self, message):
        if self.progress:
            self.progress(message)

    def private_observation(self, snapshot):
        """Read-only guard. Also used when no further account operation is permitted."""
        return bool(SECRET_SCREEN.search(snapshot)) or any(value in snapshot for value in self.credential_values)

    async def _message(self, record, kind):
        if record.get('registration_job') != self.job_id or not record.get('registration_started'):
            raise ValueError('Verifica el correo directamente. Este ticket no inició el alta que muestra el portal.')
        if not mailbox_connected(self.root):
            raise ValueError('El comercio requiere verificar el alta. Vincula tu correo en Ajustes y pulsa Continuar, '
                             'o usa Tomar control → Cuenta y datos fiscales → Revisar correo en FACTURAI para abrir un .eml. '
                             'También puedes consultar el correo en Navegación libre y volver al trámite.')
        self.say('Esperando el correo de alta del comercio…')
        started = monotonic()
        asked = 0
        polls = iter(MAIL_POLLS)
        try:
            # A mailbox that stops answering does not hold the ticket beyond the wait either.
            async with asyncio.timeout(MAIL_WAIT + 60):
                while True:
                    value = await asyncio.to_thread(registration_email, self.root, self.origin, self.profile.email,
                                                    record['registration_started'], kind)
                    if value:
                        self.say('Se encontró el correo de alta. Continuando en el portal…')
                        return value
                    # The pauses asked for count too, so the limit holds even if the clock stood still.
                    waited = max(monotonic() - started, asked)
                    if waited >= MAIL_WAIT:
                        break
                    delay = min(next(polls, MAIL_POLL), MAIL_WAIT - waited)
                    if waited >= 1:
                        self.say(f'Esperando el correo de alta del comercio… Llevo {waited_text(waited)} de hasta '
                                 f'{waited_text(MAIL_WAIT)}.')
                    asked += delay
                    await asyncio.sleep(delay)
        except TimeoutError:
            pass
        finally:
            if self.meter is not None:
                # Waiting for the merchant is not work of the attempt.
                self.meter.waited += monotonic() - started
        raise ValueError('Todavía no llegó un correo de alta verificable. Pulsa Continuar para volver a consultarlo.')

    async def step(self, page, snapshot, challenge):
        following, self.following = self.following, False
        if SECRET_SCREEN.search(snapshot):
            typed = None
            if (self.pedir_codigo and self.origin and challenge not in {'captcha', 'attachment'}
                    and public_portal_origin(page.url) == self.origin and hasattr(page, 'frames')):
                try:
                    typed = await self._phone_code(page)
                except (ValueError, BrowserError):
                    typed = None
            return typed or ('login', 'El portal muestra una verificación de acceso. Completa el paso en el sitio y pulsa Continuar.')
        if not self.origin or public_portal_origin(page.url) != self.origin or not hasattr(page, 'frames'):
            return None
        contains_credential = any(value in snapshot for value in self.credential_values)
        if (not challenge and not contains_credential and not following and not ACCOUNT_UI.search(snapshot)
                and not self._wider_wording(snapshot)):
            return None
        if challenge in {'captcha', 'attachment'}:
            return None
        try:
            return await self._step(page, snapshot, contains_credential, following)
        except ValueError as exc:
            # Helpers return fixed, user-facing messages, never a raw credential or link.
            return HOLD, str(exc)
        except BrowserError:
            return HOLD, 'La pantalla del registro cambió. Revisa el resultado del paso en el navegador.'

    def _wider_wording(self, snapshot):
        if self.pedir_codigo and PHONE_UI.search(snapshot):
            return True
        if SIGN_UP_UI.search(snapshot) and self.assistant.permitida():
            return True
        sign_in, enrollment = SIGN_IN_UI.search(snapshot), ENROLLMENT_UI.search(snapshot)
        if not sign_in and not enrollment:
            return False
        record = read_json(credential_file(self.root, self.origin, self.profile.email), {})
        return bool((sign_in and record.get('credential')) or
                    (enrollment and record.get('registration_job') == self.job_id))

    async def _step(self, page, snapshot, contains_credential, following=False):
        args = {'origin': self.origin}
        kind = await page.evaluate(PAGE, args)
        path = credential_file(self.root, self.origin, self.profile.email)
        record = read_json(path, {})
        if (record.get('change_job') == self.job_id and record.get('pending_credential')
                and CONFIRMED_PASSWORD.search(snapshot)):
            confirm_password_change(path)
            return 'advanced', 'El comercio confirmó el cambio y la nueva contraseña quedó guardada.'
        if kind == 'other':
            if contains_credential:
                return HOLD, 'El portal muestra una credencial. Avanza a la pantalla del ticket y pulsa Continuar.'
            return await self._beyond_rules(page, snapshot, path, record, following)
        if kind == 'protected':
            step = await self._beyond_rules(page, snapshot, path, record, following)
            if step and step != REHUSA:
                return step
            return 'login', 'Completa la verificación o el acceso en el portal y vuelve al trámite.'
        values = {'email': self.profile.email, 'whatsapp_phone': getattr(self.profile, 'whatsapp_phone', '')}
        if kind == 'required_signup' or (kind == 'login_signup' and not record.get('credential')):
            if self.followed_signup or record.get('submission_attempted'):
                return HOLD, 'El registro ya se abrió. Comprueba el acceso existente en el mismo portal.'
            self.followed_signup = True
            return await self._click(page, 'open_signup', 'Se abrió el registro del comercio.', kind)
        if kind == 'signup':
            if record.get('submission_attempted'):
                # Only a registration this ticket is sending step by step goes on from here.
                step = await self._assisted(page, path, following)
                if step and step != REHUSA:
                    return step
                return HOLD, 'La solicitud de alta ya quedó registrada. Comprueba la respuesta del comercio.'
            current = read_json(self.root / 'profile.json', {})
            if (current.get('rfc') and current['rfc'] == getattr(self.profile, 'rfc', None)
                    and current.get('email', '').casefold() == self.profile.email.casefold()):
                values['whatsapp_phone'] = current.get('whatsapp_phone', '')
            password = site_password(path, True)
            self.credential_values.add(password)
            try:
                result = await prepare_form(page, self.origin, 'create', values, password, path,
                                            job_id=self.job_id)
            except ValueError:
                # The rules found nothing they could prepare: the assisted sign-up may still take the form.
                step = await self._assisted(page, path, following)
                if step and step != REHUSA:
                    return step
                raise
            if not read_json(path, {}).get('submission_attempted'):
                step = await self._assisted(page, path, following)
                if step and step != REHUSA:
                    return step
                return HOLD, result['message']
            await self._settle(page, kind)
            return 'advanced', result['message']
        if kind in {'login', 'login_signup'}:
            if not record.get('credential'):
                step = await self._assisted(page, path, following) or await self._open_signup(page, record)
                if step and step != REHUSA:
                    return step
                return HOLD, 'Inicia sesión en esta cuenta del comercio o abre su registro y pulsa Continuar.'
            return await self._login(page, snapshot, path, record, values, 'submit_login', kind)
        if kind in {'code', 'mail'} and await self._phone_screen(page):
            # A code sent to the phone never arrives in the mailbox: it is asked in the chat instead.
            return await self._phone_code(page, known=True) or ('login', PHONE_PAUSE)
        mail_kind = {'code': 'code', 'mail': 'activate', 'change': 'temporary'}[kind]
        if (record.get('mail_attempts') or {}).get(mail_kind) == self.job_id:
            return HOLD, 'Ya se aplicó este paso del correo. Revisa la respuesta del comercio antes de continuar.'
        value = await self._message(record, mail_kind)
        self.credential_values.add(value)
        if public_portal_origin(page.url) != self.origin or await page.evaluate(PAGE, args) != kind:
            return HOLD, 'El registro cambió mientras llegaba el correo. Comprueba la página y pulsa Continuar.'
        if kind == 'change':
            temporary = await prepare_form(page, self.origin, 'temporary', {}, value, None)
            proposal = site_password(path, True, rotation=True)
            self.credential_values.add(proposal)
            fresh = await prepare_form(page, self.origin, 'change', values, proposal, path)
            if 'contraseña temporal' not in temporary['filled'] or 'contraseña' not in fresh['filled']:
                return HOLD, 'Comprueba los campos de contraseña temporal y nueva en el sitio del comercio.'
            record = {**read_json(path, {}), 'pending_filled': True, 'change_job': self.job_id}
        write_private(path, {**record, 'mail_attempts': {**record.get('mail_attempts', {}), mail_kind: self.job_id}})
        if kind == 'mail':
            await page.goto(value, wait_until='domcontentloaded', timeout=18000)
            return 'advanced', 'Se abrió la activación de registro en el mismo comercio.'
        if kind == 'change':
            return await self._click(page, 'submit_change', 'Se envió el cambio de la contraseña temporal.', kind)
        return await self._click(page, 'submit_code', 'Se envió el código de alta al mismo comercio.', kind, value)

    async def _login(self, page, snapshot, path, record, values, operation, previous=None, stage=''):
        """Sign in once per ticket with the account saved for this merchant."""
        if record.get('login_job') == self.job_id:
            return 'login', 'Se intentó el acceso una vez. Revisa la respuesta del comercio en el navegador.'
        temporary_login = (record.get('registered_with_password') is False or
                           (record.get('registration_job') == self.job_id and
                            re.search(r'(?:contraseña|clave) (?:temporal|provisional)', snapshot, re.I)))
        if temporary_login:
            candidate = await self._message(record, 'temporary')
        else:
            candidate = site_password(path, False)
        self.credential_values.add(candidate)
        fields = await prepare_form(page, self.origin, 'login', values, candidate, path)
        # In a sign-in of two steps this attempt already typed the e-mail on the screen before.
        needed = {'contraseña'} if self.identifier_sent else {'correo', 'contraseña'}
        if not needed.issubset(fields['filled']):
            return HOLD, 'Comprueba el correo y el campo de acceso del comercio.'
        write_private(path, {**record, 'login_job': self.job_id})
        step = await self._click(page, operation, 'Se usó la cuenta guardada de este comercio.', previous,
                                 self.profile.email if operation == 'submit_access' else '')
        if stage and step[0] == 'advanced':
            await self._left(page, 'access', stage)
        return step

    async def _beyond_rules(self, page, snapshot, path, record, following):
        """Screens the fixed rules leave to the person or to nobody.

        A code sent to the phone, a sign-in the strict rules do not recognize (two steps, a
        scripted button) and, with the owner's permission, the registration form of any portal.
        ``None`` keeps the result the caller already had for the page.
        """
        step = await self._phone_code(page)
        if step:
            return step
        if record.get('credential'):
            stage = await self._ask_page(page, 'access')
            if stage == 'identifier' and not self.identifier_sent and record.get('login_job') != self.job_id:
                self.identifier_sent = True
                step = await self._click(page, 'submit_access', 'Se escribió el correo de la cuenta guardada de este comercio.',
                                         None, self.profile.email)
                if step[0] == 'advanced':
                    await self._left(page, 'access', stage)
                return step
            if stage == 'login' or (stage == 'password' and self.identifier_sent):
                values = {'email': self.profile.email, 'whatsapp_phone': ''}
                return await self._login(page, snapshot, path, record, values, 'submit_access', stage=stage)
        return await self._assisted(page, path, following) or await self._open_signup(page, record)

    async def _assisted(self, page, path, following):
        step = await self.assistant.paso(page, path, following)
        if step and step[0] == 'advanced':
            self.following = True
        return step

    async def _open_signup(self, page, record):
        """Open the merchant's registration from a link the strict rules do not know, once."""
        if (not self.assistant.permitida() or self.followed_signup or record.get('submission_attempted')
                or record.get('credential')):
            return None
        if await self._ask_page(page, 'signup_link') != 'signup_link':
            return None
        self.followed_signup = True
        return await self._click(page, 'open_signup_wide', 'Se abrió el registro del comercio.')

    async def _ask_page(self, page, operation, value=''):
        """One of the operations beyond the fixed rules; none of them answers on a page that shows the ticket."""
        return await page.evaluate(PAGE, {'origin': self.origin, 'operation': operation, 'value': value,
                                          'marks': self.ticket_marks})

    async def _phone_screen(self, page):
        return await self._ask_page(page, 'phone_code') == 'phone_code'

    async def _phone_code(self, page, known=False):
        """Ask the person for the code the portal sent to their phone and type it.

        ``None`` leaves the pause the caller already had. The code never goes to the model, to
        an event or to a log, and from here on it is treated like a password.
        """
        if not self.pedir_codigo or self.codes_asked >= PHONE_CODES:
            return None
        record = read_json(credential_file(self.root, self.origin, self.profile.email), {})
        if not (self.identifier_sent or self.job_id in {record.get('registration_job'), record.get('login_job')}):
            return None  # Only while this ticket is opening or using its account on this portal.
        if not known and not await self._phone_screen(page):
            return None
        self.codes_asked += 1
        try:
            answer = self.pedir_codigo(PHONE_QUESTION % urlsplit(self.origin).hostname)
            if inspect.isawaitable(answer):
                answer = await answer
        except Exception:
            answer = ''  # Whatever fails while asking is the same as no answer.
        code = phone_code(answer)
        # Whatever looks like a code in the answer is kept away from the model from now on.
        self.credential_values.update(re.findall(r'\d{4,10}', str(answer or '')))
        if not code:
            return None
        self.credential_values.add(code)
        if public_portal_origin(page.url) != self.origin:
            return None
        if await self._ask_page(page, 'submit_phone_code', code) != 'sent':
            return 'login', 'No pude escribir el código en el portal. Escríbelo en el sitio y pulsa Continuar.'
        await self._left(page, 'phone_code', 'phone_code')
        return 'advanced', 'Se escribió en el portal el código que llegó a tu teléfono.'

    async def _left(self, page, operation, previous):
        """Give the screen a moment to stop answering ``previous``, and a page that is arriving time to exist."""
        for _ in range(8):
            if page.is_closed() or public_portal_origin(page.url) != self.origin:
                return
            try:
                if await self._ask_page(page, operation) != previous:
                    break
            except BrowserError:
                break
            await asyncio.sleep(.3)
        for _ in range(30):
            try:
                if await page.evaluate('() => document.readyState') != 'loading':
                    return
            except BrowserError:
                if page.is_closed():
                    return
            await asyncio.sleep(.1)

    async def _settle(self, page, previous):
        # Sites often update via AJAX after the click; observe before deciding the form is stuck.
        for _ in range(8):
            if page.is_closed() or public_portal_origin(page.url) != self.origin:
                return
            try:
                if await page.evaluate(PAGE, {'origin': self.origin}) != previous:
                    return
            except BrowserError:
                if page.is_closed():
                    return
            await asyncio.sleep(.3)

    async def _click(self, page, operation, message, previous=None, value=''):
        if await self._ask_page(page, operation, value) != 'sent':
            return HOLD, 'El formulario cambió o requiere otra confirmación. Revísalo en el portal.'
        if previous:
            await self._settle(page, previous)
        return 'advanced', message
