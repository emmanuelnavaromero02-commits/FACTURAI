"""Entrance for several customers: accounts and sessions in front of one private application each.

The application stays single-person. This ASGI application signs people in and hands every other
request to the instance created with the data folder of that account. Which instance receives a
request is decided by the session alone, never by anything else the request carries.
"""
from __future__ import annotations

import asyncio
import hashlib
import json
import logging
import os
import re
import sqlite3
import time
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
from pathlib import Path
from urllib.parse import urlsplit

from starlette.requests import Request
from starlette.responses import JSONResponse, PlainTextResponse, RedirectResponse, Response

from . import secretos
from .instancias import Instances, Unavailable
from .usuarios import (INVITATION_HOURS, LIFETIME_SECONDS, AccessError, Accounts, BadCredentials, Forbidden,
                       InvalidInvitation, Throttled)

log = logging.getLogger(__name__)

BASE = Path(__file__).resolve().parent.parent
MODE_VARIABLE = 'FACTURADOR_MODO'
ORIGIN_VARIABLE = 'FACTURADOR_ORIGEN_PUBLICO'
HOST = re.compile(r'(?=.{1,253}$)[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?(?:\.[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?)*')
TOKEN = re.compile(r'[A-Za-z0-9_-]{43}')
# The same policy the panel sends (facturador/app.py).
CSP = ("default-src 'self'; script-src 'self'; style-src 'self'; img-src 'self' data: blob:; "
       "frame-ancestors 'none'; base-uri 'none'; object-src 'none'; form-action 'self'")
BODY_LIMIT = 4096
WAITING_LOGINS = 16
WORKER_THREADS = 32
PAGES = {'/entrar': 'entrar.html', '/invitacion': 'entrar.html', '/cuenta': 'cuenta.html'}
FILES = {'/acceso/entrar.css': ('entrar.css', 'text/css; charset=utf-8'),
         '/acceso/entrar.js': ('entrar.js', 'application/javascript; charset=utf-8'),
         '/acceso/cuenta.js': ('cuenta.js', 'application/javascript; charset=utf-8'),
         # Not used yet: the panel shows it once index.html includes this script.
         '/acceso/panel.js': ('panel-acceso.js', 'application/javascript; charset=utf-8')}

NEEDS_ORIGIN = ('El modo de usuarios necesita FACTURADOR_ORIGEN_PUBLICO con la dirección https://… por la que se entra, '
                'sin ruta. Solo se admite http://localhost para pruebas.')
NEEDS_KEY = ('El modo de usuarios necesita la llave maestra: configura FACTURADOR_ARCHIVO_LLAVE (un archivo fuera de la '
             'carpeta de datos) o FACTURADOR_LLAVE_MAESTRA.')
SESSION_OVER = 'Tu sesión terminó. Vuelve a entrar.'
BUSY = 'El servicio está ocupado. Intenta de nuevo en un momento.'
CHATGPT = 'Esta conexión no se ofrece en este servicio. La lectura de tickets usa la IA del producto.'
GMAIL = ('Esta conexión no se ofrece en este servicio. Conecta tu correo en Ajustes con tu dirección y una '
         'contraseña de aplicación.')
# Both authorizations return to http://127.0.0.1 and cannot work for someone who enters from elsewhere.
NOT_OFFERED = {('POST', '/api/chatgpt/connect'): CHATGPT, ('GET', '/auth/chatgpt/callback'): '/?conexion=chatgpt-error#ajustes',
               ('POST', '/api/gmail/client'): GMAIL, ('POST', '/api/gmail/connect'): GMAIL,
               ('GET', '/auth/gmail/callback'): '/?conexion=gmail-error&motivo=no-disponible#ajustes'}


class ConfigError(ValueError):
    """The configuration asks for something this program refuses to start with."""


class Refusal(Exception):
    def __init__(self, status: int, detail: str):
        super().__init__(detail)
        self.status, self.detail = status, detail


@dataclass(frozen=True)
class Settings:
    public_origin: str
    data_root: Path
    port: int = 8765
    max_browsers: int = 3
    idle_seconds: float = 30 * 60


def clean_origin(value: str) -> str:
    """``https://host[:port]`` and nothing else; plain http only for localhost while testing."""
    parts = urlsplit((value or '').strip())
    host = (parts.hostname or '').lower()
    try:
        port = parts.port
    except ValueError:
        raise ConfigError(NEEDS_ORIGIN) from None
    local = host in {'localhost', '127.0.0.1'}
    if (parts.scheme not in {'https', 'http'} or (parts.scheme == 'http' and not local) or not HOST.fullmatch(host)
            or parts.username or parts.password or parts.path not in {'', '/'} or parts.query or parts.fragment):
        raise ConfigError(NEEDS_ORIGIN)
    default = 443 if parts.scheme == 'https' else 80
    return f'{parts.scheme}://{host}' + (f':{port}' if port and port != default else '')


def _number(env, name: str, default: int, lowest: int, highest: int) -> int:
    text = (env.get(name) or '').strip()
    if not text:
        return default
    if not text.isdigit() or not lowest <= int(text) <= highest:
        raise ConfigError(f'{name} debe ser un número de {lowest} a {highest}.')
    return int(text)


def data_root(env=None) -> Path:
    """The data folder, read exactly as the application reads it."""
    env = os.environ if env is None else env
    return Path(env.get('FACTURADOR_DATA', str(BASE / 'data'))).resolve()


def settings_from_env(env=None, *, port: int = 8765) -> Settings | None:
    """The settings of the entrance, or None when the configuration keeps the local single-person mode."""
    env = os.environ if env is None else env
    mode = (env.get(MODE_VARIABLE) or 'local').strip().lower()
    if mode == 'local':
        return None
    if mode != 'usuarios':
        raise ConfigError(f'{MODE_VARIABLE} solo admite «local» o «usuarios».')
    if not (env.get(ORIGIN_VARIABLE) or '').strip():
        raise ConfigError(NEEDS_ORIGIN)
    return Settings(public_origin=clean_origin(env[ORIGIN_VARIABLE]), data_root=data_root(env), port=port,
                    max_browsers=_number(env, 'FACTURADOR_NAVEGADORES_MAX', 3, 1, 50),
                    idle_seconds=60 * _number(env, 'FACTURADOR_INSTANCIA_INACTIVA_MIN', 30, 1, 1440))


def open_accounts(root: Path) -> Accounts:
    return Accounts(Path(root) / 'acceso', pepper=secretos.derive(secretos.PASSWORDS))


def open_entrance(env=None, *, port: int = 8765) -> Entrance | None:
    """The entrance when the configuration asks for several customers; None for the local mode."""
    env = os.environ if env is None else env
    settings = settings_from_env(env, port=port)
    if settings is None:
        return None
    try:
        if not secretos.load_from_env(settings.data_root, env):
            raise ConfigError(NEEDS_KEY)
        secretos.verify(settings.data_root)
    except (secretos.MasterKeyError, secretos.SealedSecrets) as exc:
        raise ConfigError(str(exc)) from None
    return Entrance(settings, open_accounts(settings.data_root))


class Entrance:
    def __init__(self, settings: Settings, accounts: Accounts, *, factory=None, clock=time.monotonic,
                 instances: Instances | None = None):
        if factory is None:
            from .app import create_app as factory
        self.settings = settings
        self.accounts = accounts
        origin = urlsplit(settings.public_origin)
        self.host = origin.netloc
        self.secure = origin.scheme == 'https'
        # The __Host- prefix makes the browser refuse this cookie from any other host or path.
        self.cookie = ('__Host-' if self.secure else '') + 'facturai_sesion'
        self.instances = instances or Instances(
            settings.data_root, factory, port=settings.port, public_origin=settings.public_origin,
            max_browsers=settings.max_browsers, idle_seconds=settings.idle_seconds, clock=clock)
        self._hashing = ThreadPoolExecutor(max_workers=2, thread_name_prefix='acceso')
        self._waiting = 0
        self._upkeep: asyncio.Task | None = None
        self._stopping = asyncio.Event()
        self._files = {}
        for name in {*PAGES.values(), *(name for name, _ in FILES.values())}:
            body = (BASE / 'web' / name).read_bytes()
            self._files[name] = (body, '"' + hashlib.sha256(body).hexdigest()[:16] + '"')
        # (method, path) -> (who may call it, handler)
        self._routes = {
            ('GET', '/entrar'): ('anyone', self._login_page),
            ('GET', '/invitacion'): ('anyone', self._invitation_page),
            ('GET', '/cuenta'): ('anyone', self._account_page),
            ('POST', '/acceso/entrar'): ('anyone', self._login),
            ('POST', '/acceso/salir'): ('anyone', self._logout),
            ('POST', '/acceso/invitacion/consultar'): ('anyone', self._invitation_check),
            ('POST', '/acceso/invitacion'): ('anyone', self._invitation_accept),
            ('GET', '/acceso/yo'): ('session', self._me),
            ('POST', '/acceso/contrasena'): ('session', self._password),
            ('POST', '/acceso/sesiones/cerrar-otras'): ('session', self._close_others),
            ('GET', '/acceso/admin/usuarios'): ('admin', self._admin_list),
            ('POST', '/acceso/admin/invitaciones'): ('admin', self._admin_invite),
            ('POST', '/acceso/admin/invitaciones/revocar'): ('admin', self._admin_revoke),
            ('POST', '/acceso/admin/usuarios/estado'): ('admin', self._admin_state),
        }

    # ------------------------------------------------------------ ASGI

    async def __call__(self, scope, receive, send):
        if scope['type'] == 'lifespan':
            await self._lifespan(receive, send)
        elif scope['type'] == 'http':
            await self._http(scope, receive, send)
        elif scope['type'] == 'websocket':
            await send({'type': 'websocket.close', 'code': 1008})

    async def _lifespan(self, receive, send):
        await receive()
        try:
            await self.startup()
        except Exception:
            await send({'type': 'lifespan.startup.failed', 'message': 'No se pudo iniciar la entrada.'})
            raise
        await send({'type': 'lifespan.startup.complete'})
        await receive()
        try:
            await self.shutdown()
        finally:
            await send({'type': 'lifespan.shutdown.complete'})

    async def startup(self) -> None:
        # Slow reads of many customers at once must not leave the others without a worker thread.
        asyncio.get_running_loop().set_default_executor(
            ThreadPoolExecutor(max_workers=WORKER_THREADS, thread_name_prefix='facturai'))
        self._upkeep = asyncio.create_task(self._keep())

    async def shutdown(self) -> None:
        self._stopping.set()
        if self._upkeep:
            # An instance that is being closed right now finishes its own shutdown first.
            await asyncio.wait({self._upkeep})
            self._upkeep = None
        await self.instances.close()
        self._hashing.shutdown(wait=False)

    async def _keep(self) -> None:
        pause = max(1.0, min(30.0, self.settings.idle_seconds / 4))
        purge_at = 0.0
        while not self._stopping.is_set():
            try:
                await asyncio.wait_for(self._stopping.wait(), pause)
                return
            except asyncio.TimeoutError:
                pass
            try:
                await self.instances.sweep()
                if time.monotonic() >= purge_at:
                    purge_at = time.monotonic() + 600
                    self.accounts.purge()
            except Exception as exc:
                log.error('Falló el mantenimiento de la entrada (%s).', type(exc).__name__)

    async def _http(self, scope, receive, send):
        method, path = scope['method'], scope['path']
        if path == '/healthz':
            # Answers the container's own check: no session, no host rule and nothing to reveal.
            reply = JSONResponse({'status': 'ok'}) if method in {'GET', 'HEAD'} else PlainTextResponse('', 405)
            return await self._own(reply)(scope, receive, send)
        request = Request(scope, receive)
        if request.headers.get('host', '').lower() != self.host:
            return await PlainTextResponse('Dirección no admitida', 400)(scope, receive, send)
        send = self._transport_security(send)
        token = self._token(request)
        try:
            session = self.accounts.session(token) if token else None
        except sqlite3.Error:
            # Without knowing who asks, nothing is served.
            return await self._own(PlainTextResponse(BUSY, 503, headers={'Retry-After': '5'}))(scope, receive, send)
        route = self._routes.get(('GET' if method == 'HEAD' else method, path))
        if route:
            return await (await self._answer(route, request, session, token))(scope, receive, send)
        if path in FILES and method in {'GET', 'HEAD'}:
            return await self._file(request, *FILES[path])(scope, receive, send)
        if path in PAGES or path == '/acceso' or path.startswith('/acceso/'):
            # This part of the address space belongs to the entrance and never reaches an instance.
            return await self._own(PlainTextResponse('No disponible', 404))(scope, receive, send)
        if session is None:
            return await self._signed_out(request, token)(scope, receive, send)
        await self._forward(scope, receive, send, session)

    async def _forward(self, scope, receive, send, session):
        blocked = NOT_OFFERED.get((scope['method'], scope['path']))
        if blocked:
            reply = RedirectResponse(blocked, 303) if blocked.startswith('/') else PlainTextResponse(blocked, 422)
            return await self._own(reply)(scope, receive, send)
        try:
            # The session, and nothing else in the request, chooses the instance.
            instance = await self.instances.enter(session['usuario'], scope['path'])
        except Unavailable as exc:
            return await self._own(PlainTextResponse(str(exc), 503, headers={'Retry-After': '5'}))(scope, receive, send)
        try:
            inner = dict(scope)
            # The application has no use for the session cookie, so it never sees it.
            inner['headers'] = [(name, value) for name, value in scope['headers'] if name != b'cookie']
            inner['state'] = dict(instance.state)
            await instance.app(inner, receive, send)
        finally:
            self.instances.leave(instance)

    # ------------------------------------------------------------ plumbing

    def _transport_security(self, send):
        if not self.secure:
            return send

        async def wrapped(message):
            if message['type'] == 'http.response.start':
                message = {**message, 'headers': [*message.get('headers', []),
                                                  (b'strict-transport-security', b'max-age=31536000')]}
            await send(message)
        return wrapped

    @staticmethod
    def _own(response: Response, cache: str = 'no-store') -> Response:
        response.headers['X-Content-Type-Options'] = 'nosniff'
        response.headers['Referrer-Policy'] = 'no-referrer'
        response.headers['Cache-Control'] = cache
        response.headers['Cross-Origin-Resource-Policy'] = 'same-origin'
        response.headers['Content-Security-Policy'] = CSP
        return response

    def _token(self, request: Request) -> str:
        for part in request.headers.get('cookie', '').split(';'):
            name, _, value = part.strip().partition('=')
            if name == self.cookie and TOKEN.fullmatch(value):
                return value
        return ''

    def _set_cookie(self, response: Response, token: str) -> Response:
        age = LIFETIME_SECONDS if token else 0
        response.raw_headers.append((b'set-cookie', (
            f'{self.cookie}={token}; Max-Age={age}; Path=/; HttpOnly; Secure; SameSite=Strict').encode('ascii')))
        return response

    def _file(self, request: Request, name: str, kind: str) -> Response:
        body, tag = self._files[name]
        if tag in request.headers.get('if-none-match', ''):
            return self._own(Response(status_code=304, headers={'ETag': tag}), 'no-cache')
        return self._own(Response(body, media_type=kind, headers={'ETag': tag}), 'no-cache')

    def _page(self, name: str) -> Response:
        return self._own(Response(self._files[name][0], media_type='text/html; charset=utf-8'))

    def _signed_out(self, request: Request, token: str) -> Response:
        navigation = (request.method in {'GET', 'HEAD'} and not request.scope['path'].startswith('/api/')
                      and 'text/html' in request.headers.get('accept', ''))
        reply = self._own(RedirectResponse('/entrar', 303) if navigation else PlainTextResponse(SESSION_OVER, 401))
        return self._set_cookie(reply, '') if token else reply

    def _same_origin(self, request: Request) -> bool:
        site = request.headers.get('sec-fetch-site')
        return request.headers.get('origin') == self.settings.public_origin and site in (None, 'same-origin')

    @staticmethod
    def _address(request: Request) -> str:
        return request.client.host if request.client else ''

    @staticmethod
    async def _body(request: Request) -> dict:
        """A small JSON object. Read by hand so a refused body, which may carry a password, is never echoed."""
        if request.headers.get('content-type', '').split(';')[0].strip().lower() != 'application/json':
            raise Refusal(415, 'Solicitud no admitida.')
        raw = b''
        async for chunk in request.stream():
            raw += chunk
            if len(raw) > BODY_LIMIT:
                raise Refusal(413, 'Solicitud demasiado grande.')
        try:
            data = json.loads(raw)
        except ValueError:
            data = None
        if not isinstance(data, dict):
            raise Refusal(422, 'Revisa los datos e intenta de nuevo.')
        return data

    async def _slow(self, function, *args):
        """Password hashing, two at a time and away from the threads the instances use."""
        if self._waiting >= WAITING_LOGINS:
            raise Refusal(503, 'Hay muchas personas entrando. Intenta de nuevo en un momento.')
        self._waiting += 1
        try:
            return await asyncio.get_running_loop().run_in_executor(self._hashing, function, *args)
        finally:
            self._waiting -= 1

    async def _answer(self, route, request: Request, session, token: str) -> Response:
        allowed, handler = route
        try:
            if request.method == 'POST' and not self._same_origin(request):
                raise Refusal(403, 'Origen no admitido')
            if allowed != 'anyone' and session is None:
                return self._signed_out(request, token)
            if allowed == 'admin' and session['rol'] != 'admin':
                raise Forbidden('Solo un administrador puede hacer esto.')
            return self._own(await handler(request, session, token))
        except Refusal as exc:
            return self._own(JSONResponse({'detail': exc.detail}, exc.status))
        except AccessError as exc:
            headers = {'Retry-After': str(exc.retry_after)} if isinstance(exc, Throttled) else None
            return self._own(JSONResponse({'detail': str(exc)}, exc.status, headers=headers))
        except Exception as exc:
            log.error('Falló una petición de la entrada (%s).', type(exc).__name__)
            return self._own(JSONResponse({'detail': 'No se pudo completar. Intenta de nuevo.'}, 500))

    def _signed_in(self, account: dict) -> Response:
        return self._set_cookie(JSONResponse({'ok': True}), self.accounts.open_session(account['id']))

    def _not_blocked(self, address: str) -> None:
        wait = self.accounts.blocked(address)
        if wait:
            raise Throttled(wait)

    # ------------------------------------------------------------ pages

    async def _login_page(self, request, session, token):
        return RedirectResponse('/', 303) if session else self._page('entrar.html')

    async def _invitation_page(self, request, session, token):
        return self._page('entrar.html')

    async def _account_page(self, request, session, token):
        return self._page('cuenta.html') if session else self._signed_out(request, token)

    # ------------------------------------------------------------ sessions

    async def _login(self, request, session, token):
        data = await self._body(request)
        account = await self._slow(self.accounts.authenticate, data.get('usuario'), data.get('contrasena'),
                                   self._address(request))
        # Whatever identifier the browser held before signing in stops being valid here.
        self.accounts.close_session(token)
        return self._signed_in(account)

    async def _logout(self, request, session, token):
        self.accounts.close_session(token)
        return self._set_cookie(JSONResponse({'ok': True}), '')

    async def _me(self, request, session, token):
        return JSONResponse({'usuario': session['nombre'], 'rol': session['rol'],
                             'sesiones': self.accounts.session_count(session['usuario'])})

    async def _password(self, request, session, token):
        data = await self._body(request)
        try:
            fresh = await self._slow(self.accounts.change_password, session['usuario'], data.get('actual'),
                                     data.get('nueva'), self._address(request))
        except BadCredentials:
            raise Refusal(422, 'La contraseña actual no es correcta.') from None
        return self._set_cookie(JSONResponse({'ok': True}), fresh)

    async def _close_others(self, request, session, token):
        return JSONResponse({'cerradas': self.accounts.close_other_sessions(session['usuario'], token)})

    # ------------------------------------------------------------ invitations

    async def _invitation_check(self, request, session, token):
        data = await self._body(request)
        address = self._address(request)
        self._not_blocked(address)
        try:
            return JSONResponse({'usuario': self.accounts.invitation(data.get('codigo'))['nombre']})
        except InvalidInvitation:
            self.accounts.note_failure(address)
            raise

    async def _invitation_accept(self, request, session, token):
        data = await self._body(request)
        address = self._address(request)
        self._not_blocked(address)
        try:
            account = await self._slow(self.accounts.accept_invitation, data.get('codigo'), data.get('usuario'),
                                       data.get('contrasena'))
        except InvalidInvitation:
            self.accounts.note_failure(address)
            raise
        self.accounts.close_session(token)
        return self._signed_in(account)

    # ------------------------------------------------------------ administration

    async def _admin_list(self, request, session, token):
        # Account names and dates only. Nothing here reads what a customer keeps.
        return JSONResponse({'usuarios': self.accounts.users(), 'invitaciones': self.accounts.invitations()})

    async def _admin_invite(self, request, session, token):
        data = await self._body(request)
        made = self.accounts.create_invitation(session['usuario'], name=data.get('usuario') or '',
                                               hours=data.get('horas', INVITATION_HOURS))
        return JSONResponse({'codigo': made['codigo'], 'usuario': made['nombre'], 'vence': made['vence'],
                             'enlace': self.settings.public_origin + '/invitacion#' + made['codigo']})

    async def _admin_revoke(self, request, session, token):
        data = await self._body(request)
        return JSONResponse({'cancelada': self.accounts.revoke_invitation(str(data.get('id') or ''))})

    async def _admin_state(self, request, session, token):
        data = await self._body(request)
        key, active = data.get('id'), data.get('activo')
        if not isinstance(key, str) or not isinstance(active, bool):
            raise Refusal(422, 'Revisa los datos e intenta de nuevo.')
        account = self.accounts.set_active(key, active, by=session['usuario'])
        if not active:
            await self.instances.retire(key)
        return JSONResponse({'usuario': account})
