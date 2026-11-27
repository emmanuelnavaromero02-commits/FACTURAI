from __future__ import annotations

import asyncio
import base64
import csv
import hashlib
import io
import json
import mimetypes
import os
import re
import secrets
import sqlite3
import zipfile
from contextlib import asynccontextmanager
from pathlib import Path
from tempfile import TemporaryDirectory
from typing import Literal

from dotenv import load_dotenv
from fastapi import Body, FastAPI, File, Form, HTTPException, Query, Request, UploadFile
from fastapi.responses import FileResponse, PlainTextResponse, RedirectResponse, Response
from pydantic import BaseModel, Field, ValidationError
from PIL import UnidentifiedImageError
from pypdf.errors import PyPdfError
from starlette.middleware.gzip import DEFAULT_EXCLUDED_CONTENT_TYPES, GZipMiddleware
from starlette.middleware.trustedhost import TrustedHostMiddleware

from .auth import CALLBACK, ChatGPTPlan, PlanError, read_json, write_private
from .automation import TicketAutomation
from .catalog import CATALOG, CUSTOM_PORTAL, portal_suggestions
from .chat import chat_router
from .correo_ajustes import correo_router
from .db import Store
from .diagnostics import check_tools
from .invoice_mail import InvoiceMailRecovery
from .documents import browser_file, browser_files, reconcile, save_candidate
from .gmail import GmailAuthorizationError, GmailConnection, cfdi_parts, import_eml, import_tickets, parse_eml, recover_attachments, search_tickets, ticket_parts
from .ia_ajustes import ia_router
from .ingest import IMAGE_EXT, MAX_UPLOAD, save_input, save_upload, validate_document
from .local_reader import read_local_page
from .medicion import medicion_router
from .models import Profile, Receipt
from .proveedores_ia import SelectorIA
from .runner import Runner
from .sat import sat_router
from .portal_accounts import portal_accounts_router
from .portal_discovery import review_links
from .portal_mail import portal_mail_router
from .viewer import ViewerBatch, ViewerInput
from .whatsapp import whatsapp_router
from .avisos import Avisos
from .avisos_rutas import avisos_router, piezas_router
from .avisos_vigilante import VigilanteAvisos
from .permisos_rutas import permisos_router
from .registro_datos import registro_router

BASE = Path(__file__).resolve().parent.parent
STATIC_FILES = ('app.js', 'chat.js', 'style.css', 'visor.js', 'navegacion.js', 'cuentas-portal.js', 'correo-portal.js',
                'herramientas.js', 'seguimiento.js', 'avance.js', 'subida.js', 'correo.js', 'ia.js', 'medicion.js')
IMMUTABLE = 'public, max-age=31536000, immutable'
RECENT_JOBS = 60
HISTORY_PAGE = 100


def entity_tag(body: bytes) -> str:
    return '"' + hashlib.blake2b(body, digest_size=16).hexdigest() + '"'


def matches_tag(request: Request, tag: str) -> bool:
    values = [value.strip().removeprefix('W/') for value in request.headers.get('if-none-match', '').split(',')]
    return tag in values or '*' in values


def tagged_json(request: Request, payload) -> Response:
    """JSON with a strong ETag; answers 304 when the client already holds the same bytes."""
    body = json.dumps(payload, ensure_ascii=False, separators=(',', ':'), default=str).encode()
    tag = entity_tag(body)
    if matches_tag(request, tag):
        return Response(status_code=304, headers={'ETag': tag})
    return Response(body, media_type='application/json', headers={'ETag': tag})


class StaticAssets:
    """Panel files read once at start-up; scripts and styles are referenced by content version."""
    KINDS = {'.js': 'application/javascript', '.css': 'text/css'}

    def __init__(self, folder: Path):
        self.files: dict[str, tuple[bytes, str]] = {}
        raw = {name: (folder / name).read_text(encoding='utf-8') for name in STATIC_FILES if (folder / name).is_file()}
        versions: dict[str, str] = {}

        def build(name: str, trail=()) -> str:
            if name in versions:
                return versions[name]
            text = raw[name]
            if name.endswith('.js'):
                def versioned(match):
                    target = match.group(3)
                    if target not in raw or target in trail:
                        return match.group(0)
                    return f"{match.group(1)}{match.group(2)}./{target}?v={build(target, (*trail, name))}{match.group(2)}"
                text = re.sub(r"""(\bfrom\s*|\bimport\s*\(?\s*)(['"])\./([\w-]+\.js)\2""", versioned, text)
            body = text.encode()
            versions[name] = hashlib.sha256(body).hexdigest()[:16]
            self.files[name] = (body, versions[name])
            return versions[name]

        for name in raw:
            build(name)
        page = (folder / 'index.html').read_text(encoding='utf-8')
        for name, (_, version) in self.files.items():
            page = page.replace(f'"/{name}"', f'"/{name}?v={version}"')
        preload = ''.join(f'\n  <link rel="modulepreload" href="/{name}?v={version}">'
                          for name, (_, version) in self.files.items() if name.endswith('.js') and name != 'app.js')
        page = page.replace('</head>', preload + '\n</head>', 1)
        self.index = page.encode()
        self.index_tag = entity_tag(self.index)

    def add_script(self, source: str) -> None:
        """One more module for the page, served by whoever stands in front of this application."""
        self.index = self.index.replace(b'</head>', f'  <script type="module" src="{source}"></script>\n</head>'.encode(), 1)
        self.index_tag = entity_tag(self.index)

    def response(self, request: Request, name: str) -> Response:
        body, version = self.files[name]
        kind = self.KINDS[Path(name).suffix]
        if request.query_params.get('v') == version:
            return Response(body, media_type=kind, headers={'Cache-Control': IMMUTABLE})
        tag = f'"{version}"'
        headers = {'Cache-Control': 'no-cache', 'ETag': tag}
        if matches_tag(request, tag):
            return Response(status_code=304, headers=headers)
        return Response(body, media_type=kind, headers=headers)

    def page(self, request: Request) -> Response:
        headers = {'Cache-Control': 'no-cache', 'ETag': self.index_tag}
        if matches_tag(request, self.index_tag):
            return Response(status_code=304, headers=headers)
        return Response(self.index, media_type='text/html; charset=utf-8', headers=headers)


def page_cursor(value: str) -> tuple[str, str] | None:
    if not value:
        return None
    try:
        created, key = base64.urlsafe_b64decode(value.encode() + b'=' * (-len(value) % 4)).decode().split('|', 1)
    except (ValueError, UnicodeError) as exc:
        raise ValueError('Página del historial inválida.') from exc
    if not re.fullmatch(r'[0-9T:.+\-]{10,40}', created) or not re.fullmatch(r'[\w-]{1,160}', key):
        raise ValueError('Página del historial inválida.')
    return created, key


def encode_cursor(value: str) -> str:
    return base64.urlsafe_b64encode(value.encode()).decode().rstrip('=') if value else ''


class RunRequest(BaseModel):
    consent: bool
    confirmed_url: str = Field(default='', max_length=2048)
    auto_issue: bool = False
    mode: Literal['agent', 'manual'] = 'agent'


class HistoryItem(BaseModel):
    key: str = Field(pattern=r'^[\w-]{1,128}$')
    receipt: Receipt
    note: str = Field(max_length=600)
    uuid: str | None = Field(default=None, max_length=36)


def create_app(data_root: Path | None = None, *, port: int = 8765, public_origin: str | None = None,
               lazy_browser: bool = False) -> FastAPI:
    load_dotenv(BASE / ".env")
    root = (data_root or Path(os.getenv("FACTURADOR_DATA", str(BASE / "data")))).resolve()
    store = Store(root)
    # The selector behaves exactly like the ChatGPT plan until the owner configures a pay-per-use key.
    plan = SelectorIA(root, port)
    gmail_connection = GmailConnection(root, port)
    runner = Runner(store, plan)
    container_browser = os.getenv("FACTURADOR_CONTAINER") == "1"
    token = secrets.token_urlsafe(32)
    diagnostics_lock = asyncio.Lock()
    # Behind the entrance for several customers only its public origin talks to this application.
    permitted_origins = ({public_origin} if public_origin else
                         {f"http://127.0.0.1:{port}", f"http://localhost:{port}", "http://testserver"})

    @asynccontextmanager
    async def lifespan(app):
        store.recover_after_restart()
        try:
            if container_browser and not lazy_browser:
                try:
                    await runner.open_personal()
                except ValueError:
                    pass  # The inbox, reader and recovery tools stay available offline.
            wa_routes.watcher.start()
            automation.start()
            mail_recovery.start()
            avisos_watcher.start()
            yield
        finally:
            await avisos_watcher.close()
            await mail_recovery.close()
            await automation.close()
            await wa_routes.watcher.close()
            await runner.close()
            plan.close()

    app = FastAPI(title="Facturador", lifespan=lifespan, docs_url=None, redoc_url=None, openapi_url=None)
    app.state.store = store
    app.state.runner = runner
    app.state.plan = plan
    app.state.csrf_token = token
    app.include_router(sat_router(store))
    app.add_middleware(TrustedHostMiddleware, allowed_hosts=(
        [public_origin.partition("://")[2].partition(":")[0]] if public_origin else ["127.0.0.1", "localhost", "testserver"]))
    assets = StaticAssets(BASE / 'web')
    if public_origin:
        # Behind the entrance: a way to «Mi cuenta» and back to the sign-in page when the session ends.
        assets.add_script('/acceso/panel.js')
    cacheable_paths = {'/', *('/' + name for name in assets.files)}

    @app.middleware("http")
    async def local_guard(request: Request, call_next):
        if request.method not in {"GET", "HEAD", "OPTIONS"} or request.url.path == '/api/browser/frame':
            if not secrets.compare_digest(request.headers.get("x-app-token", ""), token):
                return PlainTextResponse("Solicitud ajena al panel local", status_code=403)
            origin = request.headers.get("origin")
            if origin and origin not in permitted_origins:
                return PlainTextResponse("Origen no admitido", status_code=403)
        response = await call_next(request)
        response.headers["X-Content-Type-Options"] = "nosniff"
        response.headers["Referrer-Policy"] = "no-referrer"
        # Only the panel files and chat previews choose their own caching; everything else stays private.
        if not (request.url.path in cacheable_paths or request.url.path.startswith('/api/chat/files/')) \
                or 'cache-control' not in response.headers:
            response.headers["Cache-Control"] = "no-store"
        response.headers["Cross-Origin-Resource-Policy"] = "same-origin"
        response.headers["Content-Security-Policy"] = "default-src 'self'; script-src 'self'; style-src 'self'; img-src 'self' data: blob:; frame-ancestors 'none'; base-uri 'none'; object-src 'none'; form-action 'self'"
        return response

    # Outermost layer: compresses JSON, scripts, styles and the page; images, PDF and archives pass as they are.
    app.add_middleware(GZipMiddleware, minimum_size=600, compresslevel=6,
                       exclude_content_types=(*DEFAULT_EXCLUDED_CONTENT_TYPES, 'application/pdf',
                                              'application/octet-stream', 'application/xml', 'text/xml'))

    def profile() -> Profile:
        path = root / "profile.json"
        if not path.exists():
            raise ValueError("Guarda primero tus datos fiscales en el panel")
        return Profile.model_validate_json(path.read_text(encoding="utf-8"))

    automation = TicketAutomation(store, runner, plan, profile, whatsapp=lambda: app.state.whatsapp, mail=True)
    # The worker itself checks whether any mailbox (Gmail or IMAP) is connected.
    mail_recovery = InvoiceMailRecovery(store, lambda: automation.enabled, lambda: runner.active_id)
    app.state.automation = automation
    app.include_router(chat_router(store, plan, automation, runner, profile))
    # The plan only picks a category when the rules cannot classify a merchant's question.
    wa_routes = whatsapp_router(store, runner, profile, plan)
    app.state.whatsapp = wa_routes
    app.include_router(wa_routes)
    app.include_router(portal_accounts_router(root, runner.viewer, profile))
    app.include_router(portal_mail_router(root, runner.viewer, profile))
    app.include_router(correo_router(root, store, profile))
    app.include_router(ia_router(root, plan))
    app.include_router(medicion_router(store))
    # Notices to the owner (browser push, own mail, bell) and the watcher that raises them once per change.
    avisos = Avisos(store, panel_url=public_origin or f'http://127.0.0.1:{port}')
    avisos_watcher = VigilanteAvisos(store, avisos, whatsapp=lambda: app.state.whatsapp)
    app.state.avisos = avisos
    app.include_router(avisos_router(store, avisos))
    app.include_router(permisos_router(store))
    app.include_router(registro_router(root))
    app.include_router(piezas_router(BASE / 'web'))

    @app.exception_handler(ValueError)
    async def invalid(request, exc):
        return PlainTextResponse(str(exc), status_code=422)

    @app.exception_handler(KeyError)
    async def missing(request, exc):
        return PlainTextResponse("Registro inexistente", status_code=404)

    @app.exception_handler(sqlite3.IntegrityError)
    async def duplicate(request, exc):
        return PlainTextResponse("Este ticket o UUID ya está registrado", status_code=409)

    @app.exception_handler(zipfile.BadZipFile)
    async def bad_zip(request, exc):
        return PlainTextResponse('El ZIP está dañado o no es válido.', status_code=422)

    @app.exception_handler(PyPdfError)
    @app.exception_handler(UnidentifiedImageError)
    async def bad_document(request, exc):
        return PlainTextResponse('No fue posible leer este documento. Revisa que sea un PDF o una imagen válida.', status_code=422)

    @app.get("/")
    def index(request: Request):
        return assets.page(request)

    def static_route(name: str):
        def serve(request: Request):
            return assets.response(request, name)
        serve.__name__ = 'static_' + re.sub(r'\W', '_', name)
        return serve

    for asset_name in assets.files:
        app.add_api_route('/' + asset_name, static_route(asset_name), methods=['GET'], include_in_schema=False)

    @app.get('/healthz')
    async def health():
        if container_browser and runner.context is None:
            return {'status': 'degraded', 'browser': 'unavailable'}
        return {'status': 'ok'}

    @app.post('/api/tools/check')
    async def tools_check():
        if diagnostics_lock.locked():
            raise ValueError('Ya hay una comprobación en curso.')
        async with diagnostics_lock:
            return await check_tools(runner, gmail_connection)

    @app.post('/api/browser/restart')
    async def restart_browser():
        if runner.has_active_jobs() or runner.lock.locked():
            raise ValueError('Termina el trámite del navegador para poder reiniciarlo.')
        async with runner.lock:
            await runner.viewer.set_manual(False)
            if runner.context:
                await runner.context.close()
            await runner.personal.close()
        return await runner.open_personal()

    @app.post('/api/chatgpt/connect')
    def openai_connect(new_account: bool = Body(default=False, embed=True), consent: bool = Body(default=False, embed=True)):
        if new_account and plan.status()['status'] in {'ready', 'limit'}:
            raise ValueError('Desconecta la cuenta actual antes de vincular otra.')
        return {'url': plan.start(new_account, consent)}

    @app.get(CALLBACK)
    def openai_callback(state: str = '', code: str = '', client_id: str = '', error: str = ''):
        try:
            plan.finish(state, code, client_id, error)
        except PlanError:
            return RedirectResponse('/?conexion=chatgpt-error#ajustes', 303)
        return RedirectResponse('/?conexion=chatgpt-ok#ajustes', 303)

    @app.post('/api/chatgpt/disconnect')
    def openai_disconnect():
        return {'remote_revoked': plan.disconnect()}

    @app.post('/api/chatgpt/retry')
    def openai_retry():
        plan.clear_limit()
        return plan.status()

    @app.get('/api/chatgpt/models')
    def openai_models():
        return plan.models()

    @app.put('/api/chatgpt/model')
    def openai_model(model: str = Body(embed=True)):
        plan.select_model(model)
        return plan.status()

    @app.post('/api/gmail/client')
    async def gmail_client(file: UploadFile = File(...)):
        gmail_connection.configure(await file.read(65_537))
        return gmail_connection.status()

    @app.post('/api/gmail/connect')
    def gmail_start(send: bool = Body(default=False, embed=True)):
        # Sending through Gmail is optional and asked only when the person chooses it.
        return {'url': gmail_connection.start(send)}

    @app.get('/auth/gmail/callback')
    def gmail_callback(state: str = '', code: str = '', error: str = ''):
        try:
            gmail_connection.finish(state, code, error)
        except GmailAuthorizationError as exc:
            return RedirectResponse(f'/?conexion=gmail-error&motivo={exc.reason}#ajustes', 303)
        return RedirectResponse('/?conexion=gmail-ok#ajustes', 303)

    @app.post('/api/gmail/disconnect')
    def gmail_disconnect():
        gmail_connection.disconnect()
        return gmail_connection.status()

    @app.post('/api/gmail/receipts/search')
    def gmail_search(query: str = Body(embed=True, min_length=1, max_length=400)):
        return search_tickets(root, query)

    @app.post('/api/gmail/receipts/{message_id}/parts')
    def gmail_parts(message_id: str):
        return [{'id': part, 'name': 'Texto del correo' if part == 'body' else name, 'size': len(data)}
                for part, (name, data) in ticket_parts(root, message_id).items()]

    @app.post('/api/gmail/receipts/{message_id}/import')
    def gmail_import(message_id: str, selected: list[str] = Body(embed=True, min_length=1, max_length=60)):
        return import_tickets(store, message_id, selected)

    @app.post('/api/browser/open')
    async def open_browser(provider: str | None = Body(default=None, max_length=64),
                           url: str | None = Body(default=None, max_length=2048),
                           confirmed_origin: str = Body(default='', max_length=300)):
        await runner.open_portal(provider, url, confirmed_origin)
        return runner.browser_state()

    @app.post('/api/browser/personal')
    async def personal_browser(url: str | None = Body(default=None, max_length=2048),
                               channel: str = Body(default='', max_length=64)):
        return await runner.open_personal(url, channel)

    @app.post('/api/browser/personal/tabs')
    async def personal_tabs(action: Literal['new', 'select', 'close'] = Body(),
                            tab_id: str | None = Body(default=None, max_length=64),
                            channel: str = Body(default='', max_length=64)):
        return await runner.open_personal(channel=channel, action=action, tab_id=tab_id)

    def check_captcha_ticket(key):
        pending = runner.pending.get(key)
        if (key != runner.active_id or not pending or pending['kind'] != 'captcha' or
                pending['future'].done() or pending['page'] is not runner.viewer.page or
                runner.page_job.get(runner.viewer.page) != key):
            raise ValueError('El ticket ya no solicita esta verificación. Actualiza el panel.')

    @app.post('/api/browser/return')
    async def return_to_job(channel: str = Body(default='', embed=True, max_length=64),
                            job_id: str | None = Body(default=None, max_length=100)):
        if job_id is not None:
            check_captcha_ticket(job_id)
        return await runner.return_to_job(channel)

    @app.get('/api/browser/frame')
    async def browser_frame(revision: int, have: str = Query(default='', max_length=64),
                            w: int | None = Query(default=None, ge=160, le=2560)):
        view = runner.screen
        value, digest = await view.frame_update(revision, have, w)
        if view is not runner.screen:
            value, digest = None, ''
        headers = {'X-Frame-Hash': digest} if digest else {}
        # 204 when there is no frame or it is identical to the one the panel already shows.
        return Response(content=value, media_type='image/jpeg', headers=headers) if value else Response(
            status_code=204, headers=headers)

    @app.post('/api/browser/control')
    async def browser_control(revision: int = Body(embed=True), job_id: str | None = Body(default=None, max_length=100)):
        view = runner.screen
        if job_id is None and view is runner.viewer:
            key = runner.page_job.get(view.page) if view.page is not None else None
            pending = runner.pending.get(key)
            if pending and key == runner.active_id:
                if pending['kind'] == 'captcha':
                    job_id = key
                else:
                    def check_job():
                        if (view is not runner.viewer or runner.screen is not view or
                                runner.active_id != key or runner.pending.get(key) is not pending or
                                pending['future'].done() or pending['page'] is not view.page):
                            raise ValueError('El visor ya no corresponde al ticket pendiente.')
                    channel = await view.take_control(revision, check=check_job)
                    try:
                        store.mark_human_exposed(key)
                    except Exception:
                        await view.release(channel)
                        raise
                    return {'channel': channel}
            elif key:
                raise ValueError('El ticket ya no espera intervención en el navegador.')
        if job_id is None:
            return {'channel': await view.take_control(revision)}
        def check():
            check_captcha_ticket(job_id)
            if view is not runner.viewer or runner.screen is not view:
                raise ValueError('Vuelve al visor de este ticket para resolver la verificación.')
        channel = await view.take_control(revision, check=check)
        try:
            store.mark_human_exposed(job_id)
        except Exception:
            await view.release(channel)
            raise
        return {'channel': channel}

    @app.post('/api/browser/control/alive')
    async def captcha_control_alive(job_id: str = Body(max_length=100), channel: str = Body(max_length=64)):
        check_captcha_ticket(job_id)
        if runner.screen is not runner.viewer:
            raise ValueError('La pantalla del ticket cambió.')
        runner.viewer.require_control(channel)
        return {'active': True}

    @app.post('/api/browser/release')
    async def browser_release(channel: str = Body(embed=True), abandon: bool = Body(default=False)):
        if abandon:
            await runner.viewer.release(channel)
            await runner.personal.view.release(channel)
        else:
            await runner.release_control(channel)
        return {'released': True}

    @app.post('/api/browser/input')
    async def browser_input(value: ViewerInput | ViewerBatch):
        if isinstance(value, ViewerBatch):
            await runner.screen.send_batch(value)
            return {'sent': True, 'count': len(value.events)}
        await runner.screen.send(value)
        return {'sent': True}

    @app.post('/api/browser/dialog')
    async def browser_dialog(dialog_id: str = Body(embed=True), accept: bool = Body(embed=True)):
        await runner.screen.answer(dialog_id, accept)
        return {'answered': True}

    @app.post('/api/browser/text')
    async def browser_text(channel: str = Body(embed=True, max_length=64)):
        return {'text': await runner.screen.visible_text(channel)}

    @app.post('/api/browser/screenshot')
    async def browser_screenshot(channel: str = Body(embed=True, max_length=64)):
        return Response(content=await runner.screen.screenshot(channel), media_type='image/png',
                        headers={'Content-Disposition': 'attachment; filename="captura-portal.png"'})

    @app.post('/api/browser/history')
    async def browser_history(channel: str = Body(default='', max_length=64), direction: Literal['back', 'forward', 'reload'] = Body()):
        view = runner.screen
        personal = view is runner.personal.view
        if runner.has_active_jobs() and not personal:
            raise ValueError('El historial está disponible al navegar por cuenta propia.')
        await view.history(channel, direction, personal=personal)
        return runner.browser_state()

    @app.post('/api/browser/upload')
    async def browser_upload(channel: str = Form(max_length=64), request_id: str = Form(max_length=64),
                             file: UploadFile | None = File(default=None)):
        payload = None
        if file:
            data = await file.read(MAX_UPLOAD + 1)
            name = Path((file.filename or '').replace('\\', '/')).name
            mime = await asyncio.to_thread(validate_document, name, data)
            if mime != mimetypes.guess_type(name)[0]:
                raise ValueError('La extensión no coincide con el contenido del documento.')
            payload = {'name': name, 'buffer': data, 'mimeType': mime}
        await runner.screen.attach(channel, request_id, payload)
        return {'attached': payload is not None}

    @app.get('/api/browser/downloads')
    def list_browser_files():
        return {'files': browser_files(root), 'message': runner.manual_download_error}

    @app.get('/api/browser/downloads/{file_id}')
    def download_browser_file(file_id: str):
        path = browser_file(root, file_id)
        return FileResponse(path, filename=path.name[17:])

    def panel_row(row: dict) -> dict:
        row['retry_action'] = runner.retry_action(row)
        row['can_retry'] = bool(row['status'] == 'blocked' and row['billing_profile'] and
                                not (row['issue_attempted'] or row['human_exposed'] or row['issuance_seen'] or row['uuid']))
        row.pop("source_path", None)
        row.pop("source_hash", None)
        row.pop('billing_profile', None)
        row.pop('auto_stage', None)
        row.pop('confirmed_payment', None)
        row["has_pdf"], row["has_xml"] = bool(row.pop("pdf")), bool(row.pop("xml"))
        row["human_kind"] = runner.pending.get(row["id"], {}).get("kind")
        return row

    @app.get("/api/state")
    def state(request: Request):
        path = root / "profile.json"
        status = automation.status()
        keep = {runner.active_id, status.get('current_id'), *runner.pending, *(status.get('jobs') or {})}
        jobs, cursor = store.panel_jobs(RECENT_JOBS, tuple(key for key in keep if isinstance(key, str)))
        # Recent and open tickets only; the rest of the history is paged from /api/jobs.
        return tagged_json(request, {
            "token": token, "profile": read_json(path), "jobs": [panel_row(row) for row in jobs],
            "jobs_summary": store.job_summary(), "history_cursor": encode_cursor(cursor),
            "supports_ticket_retry": True, "supports_captcha_control": True, 'catalog_url': '/api/catalog',
            'plan': plan.status(), 'gmail': gmail_connection.status(), 'automation': status,
            'browser': runner.browser_state(), 'active_job': runner.active_id})

    @app.get('/api/catalog')
    def catalog(request: Request):
        return tagged_json(request, CATALOG)

    @app.get('/api/jobs')
    def job_history(request: Request, before: str = Query(default='', max_length=400),
                    limit: int = Query(default=HISTORY_PAGE, ge=1, le=200)):
        rows, cursor = store.jobs_page(page_cursor(before), limit)
        return tagged_json(request, {'jobs': [panel_row(row) for row in rows], 'next': encode_cursor(cursor)})

    @app.put('/api/automation')
    async def configure_automation(enabled: bool = Body(embed=True, strict=True)):
        return automation.configure(enabled)

    @app.put("/api/profile")
    def save_profile(value: Profile):
        profile_path = root / 'profile.json'
        data = value.model_dump()
        previous = read_json(profile_path) or {}
        if any(x["status"] in {"running", "issuing", "needs_user", "queued"} for x in store.list()):
            if any(previous.get(k) != v for k, v in data.items() if k != 'whatsapp_phone'):
                raise ValueError("Termina la cola antes de cambiar el perfil fiscal")
        if 'whatsapp_phone' not in value.model_fields_set:
            if previous.get('rfc') == value.rfc:
                data['whatsapp_phone'] = previous.get('whatsapp_phone', '')
        write_private(profile_path, data)
        return {"saved": True}

    @app.post('/api/profile/read-csf')
    async def read_csf(file: UploadFile = File(...), mode: Literal['agent', 'local', 'extract'] = Form('agent'),
                       page: int = Form(1, ge=1, le=20), rotation: int = Form(0), force_ocr: bool = Form(False)):
        suffix = Path(file.filename or '').suffix.lower()
        data = await file.read(MAX_UPLOAD + 1)
        if suffix not in IMAGE_EXT | {'.pdf'} or len(data) > MAX_UPLOAD or not data:
            raise ValueError('Elige una constancia PDF o imagen de hasta 25 MB.')
        if rotation not in {0, 90, 180, 270}:
            raise ValueError('Elige una rotación de 0, 90, 180 o 270 grados.')
        await asyncio.to_thread(validate_document, 'constancia' + suffix, data)
        with TemporaryDirectory(dir=root) as folder:
            document = Path(folder) / ('constancia' + suffix)
            document.write_bytes(data)
            if mode == 'local':
                reading = await asyncio.to_thread(read_local_page, document, page, rotation, force_ocr)
                return {**reading, 'codes': [], 'portal_links': []}
            from .tax_document import constancia_draft
            return await asyncio.to_thread(constancia_draft, document, plan if mode == 'agent' else None)

    @app.post("/api/upload")
    async def upload(files: list[UploadFile] = File(...)):
        if len(files) > 100:
            raise ValueError("Límite de 100 archivos por importación")
        result = []
        batch_id = secrets.token_hex(16)
        for file in files:
            data = await file.read(MAX_UPLOAD + 1)
            name = file.filename or 'archivo'
            if Path(name).suffix.lower() == '.eml':
                result += await asyncio.to_thread(import_eml, store, name, data)
            else:
                result += await asyncio.to_thread(save_upload, store, name, data, batch_id=batch_id)
        if any(item['created'] for item in result):
            automation.wake.set()
        return result

    @app.post('/api/demo')
    async def demo():
        from .sample import sample_ticket

        item = await asyncio.to_thread(save_input, store, 'ticket-de-ejemplo.png', sample_ticket(), 'Ejemplo')
        if item['created']:
            receipt = Receipt(provider='capufe', merchant='CAPUFE · Ejemplo', date='2026-11-21', amount='50.00',
                              payment_code='01', ticket_number='DEMO-000001', source=store.get(item['id'])['receipt']['source'])
            store.save_receipt(item['id'], receipt)
        return item

    @app.post("/api/jobs/{key}/analyze")
    async def analyze(key: str, consent: bool = Body(default=False), mode: Literal['agent', 'local'] = Body(default='agent')):
        if mode == 'agent' and not consent:
            raise ValueError('Autoriza la lectura de la imagen con tu conexión de IA.')
        return await automation.analyze(key, offline=mode == 'local')

    @app.put("/api/jobs/{key}")
    async def edit(key: str, receipt: Receipt):
        errors = receipt.ready_errors()
        row = store.get(key)
        receipt.source = row['receipt']['source']
        # The review form does not carry these readings: a correction keeps what the ticket said.
        for field in ('issuer_rfc', 'merchant_whatsapp', 'merchant_email'):
            if field not in receipt.model_fields_set:
                setattr(receipt, field, row['receipt'].get(field, ''))
        if receipt.provider not in CATALOG and receipt.provider not in {CUSTOM_PORTAL, 'solicitud_directa'}:
            errors.append("Indica el portal de facturación del comercio")
        if errors:
            raise ValueError("; ".join(errors))
        corrected = [field for field in ('provider','portal_url','merchant','amount','date','ticket_number',
                     'billing_codes','payment_code','details')
                     if row['receipt'].get(field) != getattr(receipt, field)]
        saved = store.save_receipt(key, receipt, ready=True)
        if corrected:
            store.event(key, 'fuentes_de_campos', json.dumps(
                {'fields':{field:'correccion_usuario' for field in corrected}, 'tools':['correccion_usuario']},
                ensure_ascii=False))
        automation.wake.set()
        return saved

    @app.post("/api/jobs/{key}/run")
    async def run(key: str, value: RunRequest):
        if not value.consent:
            raise ValueError("Confirma compartir los datos fiscales con el portal elegido")
        row = store.get(key)
        if row['receipt']['source'].startswith('Ejemplo:'):
            raise ValueError('Este ejemplo sirve para practicar la revisión. Sube un ticket real para abrir el portal.')
        if value.mode == 'agent' and plan.status()['status'] != 'ready':
            raise ValueError('Conecta tu IA en Ajustes antes de encolar.')
        if row['receipt']['provider'] == CUSTOM_PORTAL and (not value.confirmed_url or value.confirmed_url != row['receipt'].get('portal_url')):
            raise ValueError('Confirma la dirección actual del comercio antes de iniciar el trámite.')
        runner.schedule(key, profile(), value.auto_issue, manual=value.mode == 'manual', confirmed_url=value.confirmed_url)
        return {"queued": True}

    @app.post("/api/jobs/{key}/decision")
    async def decision(key: str, decision: str = Body(embed=True)):
        if decision not in {"approve", "resume", "cancel", "manual"}:
            raise ValueError("Decisión inválida")
        await runner.continue_job(key, decision)
        return {"continued": True}

    @app.post("/api/jobs/{key}/stop")
    async def stop(key: str):
        await runner.stop(key)
        return {"stopped": True}

    @app.post('/api/jobs/{key}/retry')
    async def retry_ticket(key: str):
        return await runner.retry_job(key, profile)

    @app.get("/api/jobs/{key}/events")
    def events(key: str, after: int | None = Query(default=None, ge=0)):
        store.get(key)
        if after is None:
            return store.events(key)
        # Only the events the panel has not seen yet, each with its id for the next request.
        return store.events_after(key, after)

    def original_path(key: str) -> Path:
        row = store.get(key)
        if not row["source_path"]:
            raise HTTPException(404)
        path = Path(row["source_path"]).resolve()
        if not path.is_relative_to(root / "originales") or not path.is_file():
            raise HTTPException(404)
        return path

    @app.get("/api/jobs/{key}/source")
    def source(key: str):
        return FileResponse(original_path(key))

    @app.post('/api/jobs/{key}/read-local')
    def read_original(key: str, page: int = Body(default=1, ge=1, le=20),
                      rotation: Literal[0, 90, 180, 270] = Body(default=0), force_ocr: bool = Body(default=False)):
        result = read_local_page(original_path(key), page, rotation, force_ocr)
        result['portal_links'] = portal_suggestions(result['codes'], result.get('text', ''))
        result['portal_discoveries'] = review_links(root, key, store.get(key)['receipt'])
        return result

    def recovery_profile(key: str) -> Profile:
        row = store.get(key)
        if key in runner.job_tasks:
            raise ValueError('Espera a que termine el trámite abierto para recuperar el comprobante.')
        return Profile.model_validate_json(row['billing_profile']) if row['billing_profile'] else profile()

    @app.get("/api/jobs/{key}/files/{kind}")
    def document(key: str, kind: str):
        if kind not in {"pdf", "xml"}:
            raise HTTPException(404)
        row = store.get(key)
        path = Path(row[kind] or "").resolve()
        if not row[kind] or not path.is_relative_to(root / "cfdi") or not path.is_file():
            raise HTTPException(404)
        return FileResponse(path, filename=(row["uuid"] or key) + "." + kind)

    @app.post("/api/jobs/{key}/attachments")
    async def attachments(key: str, files: list[UploadFile] = File(...)):
        buyer = recovery_profile(key)
        if len(files) > 30:
            raise ValueError("Límite de 30 comprobantes")
        for file in files:
            suffix = Path(file.filename or "").suffix.lower()
            data = await file.read(MAX_UPLOAD + 1)
            if suffix == '.eml':
                parts = list(cfdi_parts(parse_eml(data)))
                if not parts or len(parts) > 30:
                    raise ValueError('El correo debe contener de 1 a 30 adjuntos PDF, XML o ZIP.')
            elif suffix in {'.pdf', '.xml', '.zip'} and len(data) <= MAX_UPLOAD:
                parts = [(file.filename, data)]
            else:
                raise ValueError("Elige archivos PDF, XML, ZIP o EML de hasta 25 MB.")
            for name, document_data in parts:
                await asyncio.to_thread(save_candidate, store, key, name, document_data)
        return await asyncio.to_thread(reconcile, store, key, buyer)

    @app.post("/api/jobs/{key}/recover-local")
    async def recover_local(key: str):
        buyer = recovery_profile(key)
        return await asyncio.to_thread(reconcile, store, key, buyer)

    @app.post('/api/jobs/{key}/recover-browser')
    def recover_browser(key: str, files: list[str] = Body(embed=True, min_length=1, max_length=30)):
        buyer = recovery_profile(key)
        selected = [browser_file(root, file_id) for file_id in dict.fromkeys(files)]
        copies = [save_candidate(store, key, p.name, p.read_bytes()) for p in selected]
        return reconcile(store, key, buyer, copies, include_partners=True)

    @app.post("/api/jobs/{key}/gmail")
    async def gmail(key: str, query: str = Body(embed=True)):
        buyer = recovery_profile(key)
        if not query.strip():
            raise ValueError("Acota la búsqueda por fecha, remitente o folio")
        await asyncio.to_thread(recover_attachments, root, key, query)
        return await asyncio.to_thread(reconcile, store, key, buyer)

    @app.post("/api/whatsapp/open")
    async def whatsapp_open():
        await runner.open_whatsapp()
        return {"opened": True}

    @app.post("/api/whatsapp/capture")
    async def whatsapp_capture():
        return await runner.capture_whatsapp_media()

    @app.post("/api/history")
    async def import_history(file: UploadFile = File(...)):
        raw = await file.read(2_000_001)
        try:
            rows = json.loads(raw)
            if len(raw) > 2_000_000 or not isinstance(rows, list) or len(rows) > 2000:
                raise ValueError
            values = [HistoryItem.model_validate(row).model_dump() for row in rows]
        except (UnicodeError, ValueError, ValidationError) as exc:
            raise ValueError('Elige un historial JSON del facturador de hasta 2 MB y 2,000 registros.') from exc
        await asyncio.to_thread(store.import_history, values)
        return {'imported': len(values)}

    @app.get("/api/export.csv")
    def export():
        output = io.StringIO()
        writer = csv.writer(output)
        writer.writerow(["id", "estado", "comercio", "importe", "fecha", "pago", "ticket", "uuid", "nota", "importe_imputado_al_ticket", "total_del_cfdi"])
        for row in store.list():
            r = row["receipt"]
            values = [row["id"], row["status"], r["merchant"], r["amount"], r["date"], r["payment_code"], r["ticket_number"], row["uuid"], row["message"], row["ticket_total"], row["invoice_total"]]
            # Evitar fórmulas al abrir un campo procedente de OCR en Excel.
            writer.writerow(["'" + str(x) if str(x).lstrip(" \t\r\n").startswith(("=", "+", "-", "@")) else x for x in values])
        return PlainTextResponse("\ufeff" + output.getvalue(), media_type="text/csv", headers={"Content-Disposition": "attachment; filename=registro_facturas.csv"})

    return app
