from __future__ import annotations

import asyncio
import csv
import io
import json
import mimetypes
import os
import secrets
import sqlite3
import zipfile
from contextlib import asynccontextmanager
from pathlib import Path
from tempfile import TemporaryDirectory
from typing import Literal

from dotenv import load_dotenv
from fastapi import Body, FastAPI, File, Form, HTTPException, Request, UploadFile
from fastapi.responses import FileResponse, HTMLResponse, PlainTextResponse, RedirectResponse, Response
from pydantic import BaseModel, Field, ValidationError
from PIL import UnidentifiedImageError
from pypdf.errors import PyPdfError
from starlette.middleware.trustedhost import TrustedHostMiddleware

from .ai import AI
from .auth import CALLBACK, ChatGPTPlan, PlanError, read_json, write_private
from .catalog import CATALOG, provider_for_url
from .db import EDITABLE, Store
from .diagnostics import check_tools
from .documents import browser_file, browser_files, reconcile, save_candidate
from .gmail import GmailConnection, cfdi_parts, import_eml, import_tickets, parse_eml, recover_attachments, search_tickets, ticket_parts
from .ingest import IMAGE_EXT, MAX_UPLOAD, save_input, save_upload, validate_document
from .local_reader import read_local_page
from .models import Profile, Receipt
from .runner import Runner
from .viewer import ViewerInput

BASE = Path(__file__).resolve().parent.parent


class RunRequest(BaseModel):
    consent: bool
    auto_issue: bool = False
    mode: Literal['agent', 'manual'] = 'agent'


class HistoryItem(BaseModel):
    key: str = Field(pattern=r'^[\w-]{1,128}$')
    receipt: Receipt
    note: str = Field(max_length=600)
    uuid: str | None = Field(default=None, max_length=36)


def create_app(data_root: Path | None = None, *, port: int = 8765) -> FastAPI:
    load_dotenv(BASE / ".env")
    root = (data_root or Path(os.getenv("FACTURADOR_DATA", str(BASE / "data")))).resolve()
    store = Store(root)
    plan = ChatGPTPlan(root, port)
    gmail_connection = GmailConnection(root, port)
    runner = Runner(store, plan)
    container_browser = os.getenv("FACTURADOR_CONTAINER") == "1"
    token = secrets.token_urlsafe(32)
    diagnostics_lock = asyncio.Lock()
    permitted_origins = {f"http://127.0.0.1:{port}", f"http://localhost:{port}", "http://testserver"}

    @asynccontextmanager
    async def lifespan(app):
        store.recover_after_restart()
        try:
            if container_browser:
                try:
                    await runner.start_browser()
                except ValueError:
                    pass  # The inbox, reader and recovery tools stay available offline.
            yield
        finally:
            await runner.close()
            plan.close()

    app = FastAPI(title="Facturador", lifespan=lifespan, docs_url=None, redoc_url=None, openapi_url=None)
    app.state.store = store
    app.state.runner = runner
    app.state.plan = plan
    app.state.csrf_token = token
    app.add_middleware(TrustedHostMiddleware, allowed_hosts=["127.0.0.1", "localhost", "testserver"])

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
        response.headers["Cache-Control"] = "no-store"
        response.headers["Cross-Origin-Resource-Policy"] = "same-origin"
        response.headers["Content-Security-Policy"] = "default-src 'self'; script-src 'self'; style-src 'self'; img-src 'self' data: blob:; frame-ancestors 'none'; base-uri 'none'; object-src 'none'; form-action 'self'"
        return response

    def profile() -> Profile:
        path = root / "profile.json"
        if not path.exists():
            raise ValueError("Guarda primero tus datos fiscales en el panel")
        return Profile.model_validate_json(path.read_text(encoding="utf-8"))

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

    @app.get("/", response_class=HTMLResponse)
    def index():
        return (BASE / "web" / "index.html").read_text(encoding="utf-8")

    @app.get("/app.js")
    def javascript():
        return FileResponse(BASE / "web" / "app.js", media_type="application/javascript")

    @app.get("/style.css")
    def stylesheet():
        return FileResponse(BASE / "web" / "style.css", media_type="text/css")

    @app.get('/visor.js')
    def viewer_script():
        return FileResponse(BASE / 'web' / 'visor.js', media_type='application/javascript')

    @app.get('/herramientas.js')
    def receipt_tools_script():
        return FileResponse(BASE / 'web' / 'herramientas.js', media_type='application/javascript')

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
            await runner.start_browser()
        await runner.open_portal()
        return runner.viewer.state()

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
    def gmail_start():
        return {'url': gmail_connection.start()}

    @app.get('/auth/gmail/callback')
    def gmail_callback(state: str = '', code: str = '', error: str = ''):
        try:
            gmail_connection.finish(state, code, error)
        except ValueError:
            return RedirectResponse('/?conexion=gmail-error#ajustes', 303)
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
                           url: str | None = Body(default=None, max_length=2048)):
        await runner.open_portal(provider, url)
        return runner.viewer.state()

    @app.get('/api/browser/frame')
    async def browser_frame(revision: int):
        value = await runner.viewer.frame(revision)
        return Response(content=value, media_type='image/jpeg') if value else Response(status_code=204)

    @app.post('/api/browser/control')
    async def browser_control(revision: int = Body(embed=True)):
        return {'channel': await runner.viewer.take_control(revision)}

    @app.post('/api/browser/release')
    async def browser_release(channel: str = Body(embed=True)):
        await runner.viewer.release(channel)
        return {'released': True}

    @app.post('/api/browser/input')
    async def browser_input(value: ViewerInput):
        await runner.viewer.send(value)
        return {'sent': True}

    @app.post('/api/browser/dialog')
    async def browser_dialog(dialog_id: str = Body(embed=True), accept: bool = Body(embed=True)):
        await runner.viewer.answer(dialog_id, accept)
        return {'answered': True}

    @app.post('/api/browser/text')
    async def browser_text(channel: str = Body(embed=True, max_length=64)):
        return {'text': await runner.viewer.visible_text(channel)}

    @app.post('/api/browser/screenshot')
    async def browser_screenshot(channel: str = Body(embed=True, max_length=64)):
        return Response(content=await runner.viewer.screenshot(channel), media_type='image/png',
                        headers={'Content-Disposition': 'attachment; filename="captura-portal.png"'})

    @app.post('/api/browser/history')
    async def browser_history(channel: str = Body(max_length=64), direction: Literal['back', 'forward'] = Body()):
        if runner.has_active_jobs():
            raise ValueError('El historial está disponible al navegar por cuenta propia.')
        await runner.viewer.history(channel, direction)
        return runner.viewer.state()

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
        await runner.viewer.attach(channel, request_id, payload)
        return {'attached': payload is not None}

    @app.get('/api/browser/downloads')
    def list_browser_files():
        return {'files': browser_files(root), 'message': runner.manual_download_error}

    @app.get('/api/browser/downloads/{file_id}')
    def download_browser_file(file_id: str):
        path = browser_file(root, file_id)
        return FileResponse(path, filename=path.name[17:])

    @app.get("/api/state")
    def state():
        path = root / "profile.json"
        jobs = store.list()
        for row in jobs:
            row['can_retry'] = bool(row['status'] == 'blocked' and row['billing_profile'] and
                                    not (row['issue_attempted'] or row['human_exposed'] or row['issuance_seen'] or row['uuid']))
            row.pop("source_path", None)
            row.pop("source_hash", None)
            row.pop('billing_profile', None)
            row["has_pdf"], row["has_xml"] = bool(row.pop("pdf")), bool(row.pop("xml"))
            row["human_kind"] = runner.pending.get(row["id"], {}).get("kind")
        return {"token": token, "profile": read_json(path), "jobs": jobs,
                'catalog': CATALOG, 'plan': plan.status(), 'gmail': gmail_connection.status(),
                'browser': runner.viewer.state(), 'active_job': runner.active_id}

    @app.put("/api/profile")
    def save_profile(value: Profile):
        if any(x["status"] in {"running", "issuing", "needs_user", "queued"} for x in store.list()):
            raise ValueError("Termina la cola antes de cambiar el perfil fiscal")
        write_private(root / 'profile.json', value.model_dump())
        return {"saved": True}

    @app.post('/api/profile/read-csf')
    async def read_csf(file: UploadFile = File(...), mode: Literal['agent', 'local'] = Form('agent'),
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
            return await asyncio.to_thread(AI(plan).tax_profile, document)

    @app.post("/api/upload")
    async def upload(files: list[UploadFile] = File(...)):
        if len(files) > 100:
            raise ValueError("Límite de 100 archivos por importación")
        result = []
        for file in files:
            data = await file.read(MAX_UPLOAD + 1)
            name = file.filename or 'archivo'
            importer = import_eml if Path(name).suffix.lower() == '.eml' else save_upload
            result += await asyncio.to_thread(importer, store, name, data)
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
    async def analyze(key: str, consent: bool = Body(embed=True)):
        if not consent:
            raise ValueError('Autoriza la lectura de la imagen con tu plan de ChatGPT.')
        row = store.get(key)
        if row["status"] not in EDITABLE:
            raise ValueError("Este expediente ya está en proceso o emitido")
        result = await asyncio.to_thread(AI(plan).extract, Path(row['source_path']), {k: v['name'] for k, v in CATALOG.items()})
        if not result.is_receipt:
            store.update(key, status="blocked", message="La imagen no fue identificada como ticket: " + result.details)
            return store.get(key)
        receipt = Receipt(provider=result.provider, merchant=result.merchant, amount=result.amount or "", date=result.date or "",
                          ticket_number=result.ticket_number or "", billing_codes=result.billing_codes, payment_code=result.payment_code or "",
                          details=result.details, source=row["receipt"]["source"])
        row = store.save_receipt(key, receipt)
        if row["status"] != "duplicate":
            store.update(key, message="; ".join(result.uncertainties) or "Datos extraídos; revisar antes de emitir")
        return store.get(key)

    @app.put("/api/jobs/{key}")
    def edit(key: str, receipt: Receipt):
        errors = receipt.ready_errors()
        row = store.get(key)
        receipt.source = row['receipt']['source']
        if receipt.provider not in CATALOG and receipt.provider != 'solicitud_directa':
            errors.append("Selecciona un portal del catálogo")
        if errors:
            raise ValueError("; ".join(errors))
        return store.save_receipt(key, receipt, ready=True)

    @app.post("/api/jobs/{key}/run")
    async def run(key: str, value: RunRequest):
        if not value.consent:
            raise ValueError("Confirma compartir los datos fiscales con el portal elegido")
        row = store.get(key)
        if row['receipt']['source'].startswith('Ejemplo:'):
            raise ValueError('Este ejemplo sirve para practicar la revisión. Sube un ticket real para abrir el portal.')
        if value.mode == 'agent' and plan.status()['status'] != 'ready':
            raise ValueError('Conecta tu plan de ChatGPT en Ajustes antes de encolar.')
        runner.schedule(key, profile(), value.auto_issue, manual=value.mode == 'manual')
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

    @app.get("/api/jobs/{key}/events")
    def events(key: str):
        store.get(key)
        return store.events(key)

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
        result['portal_links'] = [code for code in result['codes'] if provider_for_url(code)]
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
        writer.writerow(["id", "estado", "comercio", "importe", "fecha", "pago", "ticket", "uuid", "nota"])
        for row in store.list():
            r = row["receipt"]
            values = [row["id"], row["status"], r["merchant"], r["amount"], r["date"], r["payment_code"], r["ticket_number"], row["uuid"], row["message"]]
            # Evitar fórmulas al abrir un campo procedente de OCR en Excel.
            writer.writerow(["'" + str(x) if str(x).lstrip(" \t\r\n").startswith(("=", "+", "-", "@")) else x for x in values])
        return PlainTextResponse("\ufeff" + output.getvalue(), media_type="text/csv", headers={"Content-Disposition": "attachment; filename=registro_facturas.csv"})

    return app
