"""HTTP routes to connect an IMAP/SMTP mailbox and to ask a merchant for the invoice by email."""
from __future__ import annotations

import asyncio
import json
from typing import Literal

from fastapi import APIRouter, Request
from pydantic import BaseModel, ConfigDict, Field, SecretStr, ValidationError

from . import buzon, gmail
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


def mail_status(root) -> dict:
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
