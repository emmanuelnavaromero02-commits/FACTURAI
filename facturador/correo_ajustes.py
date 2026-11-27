"""HTTP routes to connect a mailbox (IMAP/SMTP, or Microsoft's sign-in) and to ask a merchant for the invoice by email."""
from __future__ import annotations

import asyncio
import json
import re
from typing import Literal
from urllib.parse import urlencode

from fastapi import APIRouter, Request
from fastapi.responses import RedirectResponse
from pydantic import BaseModel, ConfigDict, Field, SecretStr, ValidationError

from . import buzon, gmail, outlook
from .correo_envio import MailOutbox, can_send
from .invoice_mail import search_job_now
from .permisos import permitido


class MailAccount(BaseModel):
    model_config = ConfigDict(extra='forbid')
    imap_host: str = Field(min_length=1, max_length=253)
    imap_port: int = Field(default=993, ge=1, le=65535)
    smtp_host: str = Field(default='', max_length=253)
    smtp_port: int = Field(default=465, ge=1, le=65535)
    smtp_tls: Literal['', 'ssl', 'starttls'] = ''
    user: str = Field(min_length=1, max_length=254)
    password: SecretStr
    address: str = Field(default='', max_length=254)
    authority: str = Field(default='', max_length=253)


class MailRequest(BaseModel):
    model_config = ConfigDict(extra='forbid')
    to: str = Field(min_length=3, max_length=254)
    consent: bool = False


class OutlookStart(BaseModel):
    model_config = ConfigDict(extra='forbid')
    enviar: bool = False


# Microsoft's mail servers stopped taking a password: the form for other providers says where to go instead.
MICROSOFT_HOST = re.compile(r'(?:^|\.)(?:outlook\.com|office365\.com|office\.com|hotmail\.com|live\.com)$')
USE_OUTLOOK = ('Outlook, Hotmail y Microsoft 365 ya no aceptan contraseña de aplicación. '
               'Conecta ese correo en «Outlook / Hotmail», en esta misma tarjeta.')
SEND_NOT_ACTIVE = ('Outlook quedó conectado para leer tu correo. El envío no se activó: el servidor de salida de '
                   'Microsoft no aceptó la conexión.')


async def _account_body(request: Request) -> tuple[dict, str]:
    """Parse by hand so a rejected body is never echoed back (it carries the password)."""
    raw = await request.body()
    try:
        if len(raw) > 8192:
            raise ValueError
        body = MailAccount.model_validate(json.loads(raw))
    except (ValueError, ValidationError, UnicodeError):
        raise ValueError('Revisa servidor, puerto, usuario y contraseña de aplicación del correo.') from None
    password = body.password.get_secret_value()
    if not 1 <= len(password) <= 512 or any(c in password for c in '\r\n\0'):
        raise ValueError('Revisa la contraseña de aplicación del correo.')
    if MICROSOFT_HOST.search(buzon.valid_host(body.imap_host)):
        raise ValueError(USE_OUTLOOK)
    settings = {'imap_host': buzon.valid_host(body.imap_host), 'imap_port': body.imap_port,
                'smtp_host': buzon.valid_host(body.smtp_host) if body.smtp_host.strip() else '',
                'smtp_port': body.smtp_port, 'smtp_tls': body.smtp_tls, 'user': body.user.strip(),
                'address': buzon.valid_address(body.address or body.user)}
    if any(c in settings['user'] for c in '\r\n\0'):
        raise ValueError('Revisa el usuario del correo.')
    if body.authority.strip():
        authority = body.authority.strip().casefold()
        if not buzon.AUTHORITY.fullmatch(authority):
            raise ValueError('Revisa el identificador de autenticación del proveedor de correo.')
        settings['authority'] = authority
    return settings, password


async def short_texts(request: Request, names: tuple[str, ...]) -> dict:
    """A few short texts, read by hand so that a refused body (it may carry a code) is never echoed back."""
    raw = await request.body()
    try:
        # The codes of Microsoft and Google are opaque and can be long; this is room to spare, not a format.
        if len(raw) > 32768:
            raise ValueError
        body = json.loads(raw)
        if not isinstance(body, dict) or set(body) - set(names):
            raise ValueError
        values = {name: body.get(name, '') for name in names}
        if not all(isinstance(value, str) and len(value) <= 16384 for value in values.values()):
            raise ValueError
    except (ValueError, UnicodeError):
        raise ValueError('La solicitud no se entendió. Vuelve a intentarlo desde Ajustes.') from None
    return values


def mail_status(root) -> dict:
    """Every mailbox together. «conectado» and «envio» also count the one opened with Microsoft's sign-in."""
    account = buzon.load_account(root)
    return {'conectado': buzon.mailbox_connected(root), 'envio': can_send(root),
            'permiso_pedir': permitido(root, 'pedir_por_correo'),
            'imap': ({**account, 'envio': bool(account.get('smtp_host')),
                      'verifica_altas': bool(account.get('authority'))} if account else None),
            'gmail': {'conectado': buzon.gmail_connected(root), 'envio': gmail.can_send(root)}}


def correo_router(root, store, profile, *, outbox=None, search=None):
    router = APIRouter()
    outbox = outbox or MailOutbox(store)
    search = search or (lambda key: search_job_now(store, key))
    connecting = asyncio.Lock()

    @router.get('/api/correo')
    def status():
        return mail_status(root)

    @router.post('/api/correo/cuenta')
    async def connect(request: Request):
        settings, password = await _account_body(request)
        if connecting.locked():
            raise ValueError('Ya se está probando una cuenta de correo.')
        async with connecting:
            settings['authority'] = await asyncio.to_thread(buzon.check_account, settings, password)
            await asyncio.to_thread(buzon.save_account, root, settings, password)
        return mail_status(root)

    @router.post('/api/correo/probar')
    async def check():
        account = buzon.load_account(root)
        if not account:
            raise ValueError('Conecta primero una cuenta de correo.')
        async with connecting:
            await asyncio.to_thread(buzon.check_account, account, buzon.account_password(root))
        return {**mail_status(root), 'prueba': 'ok'}

    @router.delete('/api/correo/cuenta')
    def disconnect():
        buzon.remove_account(root)
        return mail_status(root)

    @router.get('/api/jobs/{key}/correo')
    def request_status(key: str):
        return outbox.status(key)

    @router.post('/api/jobs/{key}/correo/pedir')
    async def ask(key: str, body: MailRequest):
        buyer = profile()
        return await asyncio.to_thread(outbox.request, key, body.to, body.consent, buyer)

    @router.post('/api/jobs/{key}/correo/buscar')
    async def find(key: str):
        return await asyncio.to_thread(search, key)

    return router


def outlook_router(root, *, port: int = 8765, public_origin: str | None = None):
    """Outlook.com, Hotmail and Microsoft 365: connect the mailbox with Microsoft's sign-in.

    Microsoft returns to the panel's own address: the local one, or the public one behind the entrance.
    """
    router = APIRouter()
    microsoft = outlook.OutlookConnection(root, port, public_origin)
    connecting = asyncio.Lock()

    @router.get('/api/correo/outlook')
    def status():
        return microsoft.status()

    @router.post('/api/correo/outlook/aplicacion')
    async def application(request: Request):
        values = await short_texts(request, ('identificador',))
        microsoft.configure(values['identificador'])
        return microsoft.status()

    @router.post('/api/correo/outlook/conectar')
    def start(body: OutlookStart):
        # Sending is optional and asked only when the person chooses it.
        return {'url': microsoft.start(body.enviar)}

    @router.get(outlook.CALLBACK)
    def returned(request: Request):
        # Microsoft's answer travels after «#» and never reaches this server: the browser carries it on to
        # the panel, which hands it over with its own token. An answer that came in the address goes the same way.
        answer = {name: request.query_params[name] for name in ('state', 'code', 'error') if name in request.query_params}
        return RedirectResponse('/#' + urlencode(answer) if answer else '/', 303)

    @router.post('/api/correo/outlook/terminar')
    async def finish(request: Request):
        values = await short_texts(request, ('estado', 'codigo', 'error'))
        if connecting.locked():
            raise ValueError('Ya se está conectando una cuenta de correo.')
        async with connecting:
            outcome = await asyncio.to_thread(microsoft.finish, values['estado'], values['codigo'], values['error'])
        return {**microsoft.status(), 'resultado': 'ok', 'aviso': SEND_NOT_ACTIVE if outcome['envio_rechazado'] else ''}

    @router.post('/api/correo/outlook/probar')
    async def check():
        async with connecting:
            await asyncio.to_thread(buzon.check_outlook_account, root)
        return {**microsoft.status(), 'prueba': 'ok'}

    @router.delete('/api/correo/outlook')
    def disconnect():
        microsoft.disconnect()
        return microsoft.status()

    return router
