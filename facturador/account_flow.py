"""Resolve a single merchant's billing registration using the owner's confirmed profile."""
import asyncio
import re
import time
from email.utils import getaddresses
from urllib.parse import urlsplit

from playwright.async_api import Error as BrowserError

from .auth import read_json, write_private
from .catalog import public_portal_origin
from .gmail import read_selected_message, search_tickets
from .portal_accounts import REGISTRATION_PAGE as PAGE, confirm_password_change, credential_file, prepare_form, site_password
from .portal_mail import mail_domain, matches_sender, registration_actions


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
    """Look only at messages received by Gmail after this specific enrollment began."""
    now = time.time()
    if not 0 <= now - started <= 30 * 60:
        raise ValueError('La solicitud de alta venció. Revisa el correo del comercio en el navegador.')
    if not re.fullmatch(r'[A-Za-z0-9_.+%-]+@[A-Za-z0-9.-]+', recipient):
        raise ValueError('Abre el correo del comercio en el navegador para verificar este destinatario.')
    host = urlsplit(origin).hostname
    query = f'to:{recipient} from:({mail_domain(host)}) after:{int(started)} -in:spam -in:trash'
    matches = []
    for row in search_tickets(root, query):
        received = row['received_ms'] / 1000
        if not started <= received <= now + 1 or not matches_sender(row['sender'], host):
            continue
        message = read_selected_message(root, row['id'])
        if not authenticated_registration(message, host):
            continue
        try:
            actions = registration_actions(message, origin, recipient, from_automatic_search=True)
        except ValueError:
            continue
        if kind in actions:
            matches.append(actions[kind])
    if len(matches) > 1:
        raise ValueError('Hay varios mensajes de alta que coinciden. Elige el correo correcto en el portal.')
    return matches[0] if matches else None


class PortalAccountFlow:
    def __init__(self, root, approved_url, profile, job_id, progress=None):
        self.root = root
        self.origin = public_portal_origin(approved_url)
        self.profile = profile
        self.job_id = job_id
        self.progress = progress
        self.followed_signup = False
        self.credential_values = set()

    def private_observation(self, snapshot):
        """Read-only guard. Also used when no further account operation is permitted."""
        return bool(SECRET_SCREEN.search(snapshot)) or any(value in snapshot for value in self.credential_values)

    async def _message(self, record, kind):
        if record.get('registration_job') != self.job_id or not record.get('registration_started'):
            raise ValueError('Verifica el correo directamente. Este ticket no inició el alta que muestra el portal.')
        if not (self.root / 'gmail_token.json').is_file():
            raise ValueError('El comercio requiere verificar el alta. Vincula Gmail en Ajustes y pulsa Continuar, '
                             'o usa Tomar control → Cuenta y datos fiscales → Revisar correo en FACTURAI para abrir un .eml. '
                             'También puedes consultar el correo en Navegación libre y volver al trámite.')
        if self.progress:
            self.progress('Esperando el correo de alta del comercio…')
        try:
            async with asyncio.timeout(165):
                for delay in (0, 3, 7, 15, 20, 30, 35, 35):
                    if delay:
                        await asyncio.sleep(delay)
                    value = await asyncio.to_thread(registration_email, self.root, self.origin, self.profile.email,
                                                    record['registration_started'], kind)
                    if value:
                        if self.progress:
                            self.progress('Se encontró el correo de alta. Continuando en el portal…')
                        return value
        except TimeoutError:
            pass
        raise ValueError('Todavía no llegó un correo de alta verificable. Pulsa Continuar para volver a consultarlo.')

    async def step(self, page, snapshot, challenge):
        if SECRET_SCREEN.search(snapshot):
            return 'login', 'El portal muestra una verificación de acceso. Completa el paso en el sitio y pulsa Continuar.'
        if not self.origin or public_portal_origin(page.url) != self.origin or not hasattr(page, 'frames'):
            return None
        contains_credential = any(value in snapshot for value in self.credential_values)
        if not challenge and not contains_credential and not ACCOUNT_UI.search(snapshot):
            return None
        if challenge in {'captcha', 'attachment'}:
            return None
        try:
            return await self._step(page, snapshot, contains_credential)
        except ValueError as exc:
            # Helpers return fixed, user-facing messages, never a raw credential or link.
            return HOLD, str(exc)
        except BrowserError:
            return HOLD, 'La pantalla del registro cambió. Revisa el resultado del paso en el navegador.'

    async def _step(self, page, snapshot, contains_credential):
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
            return None
        if kind == 'protected':
            return 'login', 'Completa la verificación o el acceso en el portal y vuelve al trámite.'
        values = {'email': self.profile.email, 'whatsapp_phone': getattr(self.profile, 'whatsapp_phone', '')}
        if kind == 'required_signup' or (kind == 'login_signup' and not record.get('credential')):
            if self.followed_signup or record.get('submission_attempted'):
                return HOLD, 'El registro ya se abrió. Comprueba el acceso existente en el mismo portal.'
            self.followed_signup = True
            return await self._click(page, 'open_signup', 'Se abrió el registro del comercio.', kind)
        if kind == 'signup':
            if record.get('submission_attempted'):
                return HOLD, 'La solicitud de alta ya quedó registrada. Comprueba la respuesta del comercio.'
            current = read_json(self.root / 'profile.json', {})
            if (current.get('rfc') and current['rfc'] == getattr(self.profile, 'rfc', None)
                    and current.get('email', '').casefold() == self.profile.email.casefold()):
                values['whatsapp_phone'] = current.get('whatsapp_phone', '')
            password = site_password(path, True)
            self.credential_values.add(password)
            result = await prepare_form(page, self.origin, 'create', values, password, path,
                                        job_id=self.job_id)
            if not read_json(path, {}).get('submission_attempted'):
                return HOLD, result['message']
            await self._settle(page, kind)
            return 'advanced', result['message']
        if kind in {'login', 'login_signup'}:
            if not record.get('credential'):
                return HOLD, 'Inicia sesión en esta cuenta del comercio o abre su registro y pulsa Continuar.'
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
            if not {'correo', 'contraseña'}.issubset(fields['filled']):
                return HOLD, 'Comprueba el correo y el campo de acceso del comercio.'
            write_private(path, {**record, 'login_job': self.job_id})
            return await self._click(page, 'submit_login', 'Se usó la cuenta guardada de este comercio.', kind)
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
        result = await page.evaluate(PAGE, {'origin': self.origin, 'operation': operation, 'value': value})
        if result != 'sent':
            return HOLD, 'El formulario cambió o requiere otra confirmación. Revísalo en el portal.'
        if previous:
            await self._settle(page, previous)
        return 'advanced', message
