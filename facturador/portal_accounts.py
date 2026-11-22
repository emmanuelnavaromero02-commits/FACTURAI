"""Herramientas del operador para el formulario del comercio que está viendo."""
from __future__ import annotations

import hashlib
import secrets
import string
import time
from pathlib import Path
from typing import Literal

from cryptography.fernet import Fernet, InvalidToken
from fastapi import APIRouter
from playwright.async_api import Error as BrowserError
from pydantic import BaseModel, ConfigDict, Field, SecretStr

from .auth import read_json, write_private
from .catalog import public_portal_origin
from .models import Profile


class AccountAction(BaseModel):
    model_config = ConfigDict(extra='forbid')
    channel: str = Field(min_length=1, max_length=64)
    origin: str = Field(min_length=1, max_length=512)
    action: Literal['fiscal', 'register', 'create', 'login', 'change', 'confirm_change', 'temporary']
    email: str = Field(default='', max_length=254)
    temporary: SecretStr | None = None


def credential_file(root: Path, origin: str, email: str) -> Path:
    identity = hashlib.sha256((origin + '\n' + email.strip().casefold()).encode()).hexdigest()
    return root / 'auth' / 'portal-accounts' / (identity + '.json')


def site_password(record_path: Path, create: bool, *, rotation: bool = False) -> str:
    # BrowserView.commands serializes all vault writes in this single-worker service.
    record = read_json(record_path, {})
    slot = 'pending_credential' if rotation else 'credential'
    if not create and not record.get(slot):
        raise ValueError('Aún no hay una contraseña guardada para este correo en este portal.')
    key_file = record_path.parent / 'vault-key.json'
    key = read_json(key_file, None)
    if key is None:
        if record or any(record_path.parent.glob('*.json')):
            raise ValueError('Falta la clave privada del almacén de contraseñas del portal.')
        key = Fernet.generate_key().decode('ascii')
        write_private(key_file, {'key': key})
    else:
        key = key['key']
    vault = Fernet(key.encode('ascii'))
    # If registration is revisited during the first temporary-password exchange, reuse its proposal.
    saved = record.get(slot) or (record.get('pending_credential') if create and not rotation else None)
    if saved:
        try:
            return vault.decrypt(saved.encode('ascii')).decode('ascii')
        except InvalidToken:
            raise ValueError('No se pudo abrir la contraseña guardada para este comercio.') from None
    groups = (string.ascii_uppercase, string.ascii_lowercase, string.digits, '!@#$%_-')
    characters = [secrets.choice(group) for group in groups]
    characters.extend(secrets.choice(''.join(groups)) for _ in range(16))
    secrets.SystemRandom().shuffle(characters)
    password = ''.join(characters)
    write_private(record_path, {**record, slot: vault.encrypt(password.encode('ascii')).decode('ascii')})
    return password


def confirm_password_change(record_path: Path) -> None:
    record = read_json(record_path, {})
    if not record.get('pending_credential'):
        raise ValueError('Este correo no tiene una contraseña nueva pendiente de confirmar en este portal.')
    if not record.get('pending_filled'):
        raise ValueError('Primero prepara la contraseña nueva en el formulario del portal.')
    if record.get('credential'):
        record['previous_credential'] = record['credential']
    record['credential'] = record.pop('pending_credential')
    record['registered_with_password'] = True
    record.pop('pending_filled', None)
    write_private(record_path, record)


def check_target(viewer, channel, origin):
    viewer.require_control(channel)
    if not origin or public_portal_origin(origin) != origin or public_portal_origin(viewer.page.url) != origin:
        raise ValueError('El destino cambió. Comprueba el dominio y vuelve a abrir las herramientas del portal.')


FORM_SCRIPT = (Path(__file__).parent / 'portal_form.js').read_text(encoding='utf-8')
REGISTRATION_PAGE = (Path(__file__).parent / 'registration_page.js').read_text(encoding='utf-8')


async def fill_portal_temporary(viewer, channel: str, origin: str, temporary: str):
    if not temporary or len(temporary) > 512:
        raise ValueError('Comprueba la contraseña temporal del comercio.')
    async with viewer.commands:
        check_target(viewer, channel, origin)
        return await prepare_form(viewer.page, origin, 'temporary', {}, temporary, None)


async def prepare_form(page, origin, action, profile, password, record_path, *, job_id=None):
    handle = None
    try:
        handle = await page.evaluate_handle(FORM_SCRIPT, {'origin': origin, 'action': action, 'profile': profile,
                                                        'password': password})
        result = await handle.evaluate('r => ({filled: r.filled, reason: r.reason, eligible: Boolean(r.submit)})')
        if not result['filled']:
            raise ValueError(result['reason'] or 'Elige un campo del formulario en el visor y vuelve a intentarlo.')
        if action != 'create' or not result['eligible']:
            return {'filled': result['filled'], 'submitted': False, 'message': result['reason'] or 'Campos preparados.'}
        record = read_json(record_path, {})
        if record.get('submission_attempted'):
            return {'filled': result['filled'], 'submitted': False,
                    'message': 'Ya se intentó el alta en este portal. Revisa la página o el correo antes de enviarla otra vez.'}
        # Persist before contacting the site. An uncertain response must never cause a second submission.
        write_private(record_path, {**record, 'submission_attempted': True,
                                    'registered_with_password': 'contraseña' in result['filled'],
                                    'registration_job': job_id, 'registration_started': time.time()})
        try:
            submitted = await handle.evaluate('r => r.submit()')
        except BrowserError:
            submitted = None
        message = {True: 'Solicitud de registro enviada. Revisa la respuesta del comercio y tu correo.',
                   False: 'El formulario cambió. Revísalo en el navegador.',
                   None: 'La página cambió durante el envío. Revisa el resultado y tu correo antes de repetir.'}[submitted]
        return {'filled': result['filled'], 'submitted': submitted is True, 'message': message}
    except BrowserError:
        raise ValueError('La página cambió durante el llenado. Vuelve a revisar el portal.') from None
    finally:
        if handle:
            try:
                await handle.dispose()
            except BrowserError:
                pass


def portal_accounts_router(root: Path, viewer, profile):
    router = APIRouter()

    @router.post('/api/browser/portal-account')
    async def account_action(request: AccountAction):
        if request.action == 'temporary':
            return await fill_portal_temporary(viewer, request.channel, request.origin,
                                               request.temporary.get_secret_value() if request.temporary else '')
        async with viewer.commands:
            check_target(viewer, request.channel, request.origin)
            if request.action == 'fiscal':
                values = profile().model_dump()
            else:
                email = Profile.valid_email(request.email) if request.email else profile().email
                values = {'email': email}
            path = credential_file(root, request.origin, values['email'])
            if request.action == 'confirm_change':
                confirm_password_change(path)
                return {'filled': [], 'submitted': False, 'message': 'Contraseña nueva guardada para los próximos accesos.'}
            rotating = request.action == 'change'
            password = site_password(path, request.action != 'login', rotation=rotating) if request.action != 'fiscal' else ''
            result = await prepare_form(viewer.page, request.origin, request.action, values, password, path)
            if rotating:
                write_private(path, {**read_json(path, {}), 'pending_filled': True})
                result['message'] = 'Envía el cambio en el comercio. Cuando lo acepte, confirma aquí la nueva contraseña.'
            return result

    return router
