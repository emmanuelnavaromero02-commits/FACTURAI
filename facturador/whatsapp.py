"""Solicitudes de CFDI en la sesión de WhatsApp Web del contenedor."""
from __future__ import annotations

import asyncio
import re
import secrets
from contextlib import asynccontextmanager
from pathlib import Path
from urllib.parse import urlencode, urlsplit

from fastapi import APIRouter, Body, File, Form, UploadFile
from playwright.async_api import Error as BrowserError

from .cfdi import MissingPair
from .db import now
from .documents import job_candidates, reconcile, save_candidate
from .ingest import MAX_UPLOAD, validate_document
from .models import Profile, Receipt
from .whatsapp_followup import WhatsAppWatcher, configure, create_schedule, schedule_status, time_utc


def destination(value):
    if not re.fullmatch(r'\+?[0-9 ()-]+', value.strip()):
        raise ValueError('Escribe el número internacional del comercio con código de país.')
    digits = re.sub(r'[^0-9]', '', value)
    if not re.fullmatch(r'[1-9][0-9]{7,14}', digits):
        raise ValueError('El número debe incluir el código del país y tener de 8 a 15 dígitos.')
    return digits


class Outbox:
    def __init__(self, store):
        self.store = store
        with store.connect() as db:
            db.execute("""CREATE TABLE IF NOT EXISTS whatsapp_requests (
                job_id TEXT PRIMARY KEY, phone TEXT NOT NULL, reference TEXT NOT NULL UNIQUE,
                original_status TEXT NOT NULL, original_message TEXT NOT NULL, state TEXT NOT NULL,
                detail TEXT NOT NULL, updated TEXT NOT NULL)""")
            create_schedule(db)
            # A crash before arming the first send has no externally visible message.
            db.execute("""UPDATE jobs SET status=(SELECT original_status FROM whatsapp_requests WHERE job_id=jobs.id),
                message=(SELECT original_message FROM whatsapp_requests WHERE job_id=jobs.id)
                WHERE status='whatsapp_preparing' AND id IN
                (SELECT job_id FROM whatsapp_requests WHERE state='preparing')""")
            db.execute("DELETE FROM whatsapp_requests WHERE state='preparing'")
            db.execute("""UPDATE whatsapp_requests SET state='uncertain',detail=detail || ?,updated=?
                WHERE state='sending'""", (' Se interrumpió el envío. Revisa el chat antes de continuar.', now()))

    def status(self, key):
        job = self.store.get(key)
        with self.store.connect() as db:
            req = db.execute("SELECT * FROM whatsapp_requests WHERE job_id=?", (key,)).fetchone()
            followup = schedule_status(db, key)
        if req is None:
            return dict(status='new', phone='', reference='', message='', updated='', count=0, **followup)
        verified = job['status'] == 'verified'
        if verified:
            followup.update(watch=False, next_check=None, watch_message='PDF y XML cotejados.')
        directory = self.store.root / 'downloads' / key / 'recibidos'
        count = sum(1 for f in directory.glob('*') if f.is_file() and f.suffix in {'.pdf', '.xml', '.zip'})
        return dict(status='verified' if verified else 'sending' if req['state']=='preparing' else req['state'],
                    phone=req['phone'], reference=req['reference'], updated=req['updated'], count=count,
                    message='PDF y XML cotejados.' if verified else req['detail'], **followup)

    def reserve(self, key, phone):
        with self.store.connect() as db:
            db.execute('BEGIN IMMEDIATE')
            job = self.store.decode(db.execute('SELECT * FROM jobs WHERE id=?', (key,)).fetchone())
            if db.execute('SELECT 1 FROM whatsapp_requests WHERE job_id=?', (key,)).fetchone():
                raise ValueError('Este expediente ya tiene una solicitud. Revisa el chat o recupera su respuesta.')
            receipt = Receipt(**job['receipt'])
            if (job['status'] not in {'ready', 'external'} or job['historical'] or job['uuid'] or
                    job['issue_attempted'] or job['human_exposed'] or job['issuance_seen'] or
                    receipt.source.startswith('Ejemplo:') or receipt.ready_errors()):
                raise ValueError('Solo puedes enviar un ticket real revisado sin un intento previo de emisión.')
            reference = 'FACTURAI-' + secrets.token_hex(8).upper()
            db.execute('INSERT INTO whatsapp_requests VALUES(?,?,?,?,?,?,?,?)',
                       (key, phone, reference, job['status'], job['message'], 'preparing', 'Preparando los adjuntos.', now()))
            db.execute("UPDATE jobs SET status='whatsapp_preparing',message=?,updated=? WHERE id=?",
                       ('Preparando la solicitud por WhatsApp.', now(), key))
            return reference

    def arm(self, key, profile, watch=True):
        with self.store.connect() as db:
            db.execute('BEGIN IMMEDIATE')
            if db.execute("""UPDATE whatsapp_requests SET state='sending',detail=?,updated=?
                        WHERE job_id=? AND state='preparing'""",
                        ('Envío en curso.', now(), key)).rowcount != 1:
                raise ValueError('La solicitud cambió. Revisa el expediente.')
            if db.execute("""UPDATE jobs SET issue_attempted=1,status='external',billing_profile=?,message=?,updated=?
                       WHERE id=? AND status='whatsapp_preparing' AND issue_attempted=0 AND uuid IS NULL""",
                       (profile.model_dump_json(), 'Envío por WhatsApp iniciado. Revisa la conversación.', now(), key)).rowcount != 1:
                raise ValueError('El expediente cambió antes de enviar.')
            db.execute("INSERT INTO events(job_id,at,kind,detail) VALUES(?,?,?,?)", (key, now(), 'whatsapp_attempt', 'Solicitud al comercio'))
            configure(db, key, watch, time_utc())

    def finish(self, key, success, detail):
        with self.store.connect() as db:
            db.execute('BEGIN IMMEDIATE')
            record = db.execute('SELECT * FROM whatsapp_requests WHERE job_id=?', (key,)).fetchone()
            if record['state'] == 'preparing':
                db.execute("UPDATE jobs SET status=?,message=?,updated=? WHERE id=? AND status='whatsapp_preparing'",
                           (record['original_status'], record['original_message'], now(), key))
                db.execute('DELETE FROM whatsapp_requests WHERE job_id=?', (key,))
                return
            state = 'sent' if success else 'uncertain'
            if not success:
                detail = record['detail'] + ' ' + detail
            db.execute('UPDATE whatsapp_requests SET state=?, detail=?,updated=? WHERE job_id=?', (state, detail, now(), key))
            db.execute("UPDATE jobs SET message=?,updated=? WHERE id=? AND status='external'", (detail, now(), key))
            db.execute('INSERT INTO events(job_id,at,kind,detail) VALUES(?,?,?,?)', (key, now(), 'whatsapp_'+state, detail))

    def progress(self, key, detail):
        with self.store.connect() as db:
            db.execute("UPDATE whatsapp_requests SET detail=?,updated=? WHERE job_id=? AND state='sending'",
                       (detail, now(), key))

    def overlapping(self, key, phone):
        with self.store.connect() as db:
            return bool(db.execute("""SELECT 1 FROM whatsapp_requests WHERE job_id<>? AND phone=?
                      AND state<>'preparing' LIMIT 1""", (key, phone)).fetchone())


class WhatsAppWeb:
    def __init__(self, runner):
        self.runner = runner

    @staticmethod
    async def verify_recipient(page, phone):
        def numbers(text):
            result = set()
            for line in text.splitlines():
                for item in re.findall(r'(?<!\w)\+\d[\d ().-]{6,}\d', line):
                    digits = re.sub(r'\D', '', item)
                    if 8 <= len(digits) <= 15:
                        result.add(digits)
            return result

        header = page.locator('#main header')
        shown = numbers(await header.inner_text())
        if not shown:
            # Saved contacts normally show a name. Verify the contact card itself,
            # never infer the peer of a fiscal document from a display name.
            title = header.locator('span[title]').first
            if not await title.count():
                title = header
            await title.click(timeout=4500)
            try:
                drawer = page.locator(
                    '[data-testid="contact-info-drawer"]:visible, [data-testid="drawer-right"]:visible, '
                    '[data-testid="drawer-right-inner"]:visible, [aria-label="Contact info"]:visible, '
                    '[aria-label="Info. del contacto"]:visible, [role="dialog"]:visible'
                ).last
                await drawer.wait_for(timeout=4000)
                shown = numbers(await drawer.inner_text())
            except BrowserError as exc:
                raise ValueError('No se pudo comprobar el número en la ficha del contacto. Revisa el destinatario en WhatsApp.') from exc
            finally:
                await page.keyboard.press('Escape')
        if shown != {phone}:
            raise ValueError('El número que muestra el chat difiere del destinatario o no se puede comprobar.')

    async def navigate(self, page, phone):
        await page.goto('https://web.whatsapp.com/send?' + urlencode({'phone': phone}),
                        wait_until='domcontentloaded', timeout=30_000)
        editor = page.locator('#main footer [contenteditable=true]').first
        await editor.wait_for(timeout=18_000)
        if (urlsplit(page.url).scheme, urlsplit(page.url).netloc) != ('https', 'web.whatsapp.com'):
            raise ValueError('El chat salió del dominio de WhatsApp.')
        await self.verify_recipient(page, phone)
        return editor

    @asynccontextmanager
    async def chat(self, phone, *, background=False):
        runner = self.runner
        if runner.has_active_jobs():
            raise ValueError('Espera a que termine el trámite del navegador.')
        async with runner.lock:
            if runner.has_active_jobs():
                raise ValueError('El navegador está ocupado.')
            await runner.start_browser()
            if background:
                page = await runner.context.new_page()
            else:
                if runner.whatsapp is None or runner.whatsapp.is_closed():
                    runner.whatsapp = await runner.context.new_page()
                page = runner.whatsapp
                await runner.viewer.show(page, 'WhatsApp Web', manual=False)
            try:
                if background:
                    editor = await self.navigate(page, phone)
                    yield page, editor
                else:
                    async with runner.viewer.commands:
                        editor = await self.navigate(page, phone)
                        yield page, editor
            except BrowserError as exc:
                raise ValueError('WhatsApp no confirmó el paso. Comprueba la sesión vinculada y el chat en el navegador.') from exc
            finally:
                if background:
                    if not page.is_closed():
                        await page.close()
                else:
                    await runner.viewer.set_manual(True)

    async def open(self, phone):
        async with self.chat(phone):
            pass

    async def send(self, phone, reference, message, documents, on_attempt, on_progress=None):
        async with self.chat(phone) as (page, editor):
            if (await editor.inner_text()).strip():
                raise ValueError('Este chat tiene un borrador. Consérvalo o envíalo en WhatsApp antes de preparar la factura.')
            await editor.fill(message)
            on_attempt()
            await editor.press('Enter')
            outgoing = page.locator('#main .message-out')
            await outgoing.filter(has_text=reference).first.wait_for(timeout=12_000)
            if on_progress:
                on_progress('El texto aparece en el chat. Todavía faltan adjuntos por confirmar.')
            pending = list(documents)
            while pending:
                selected = await self.attach_documents(page, pending)
                for payload in selected:
                    await outgoing.filter(has_text=payload['name']).first.wait_for(timeout=15_000)
                    pending.remove(payload)
                    if on_progress:
                        on_progress(f'Confirmados en el chat: texto y {len(documents)-len(pending)} de {len(documents)} adjuntos.')

    @staticmethod
    async def attach_documents(page, pending):
        attach = page.get_by_role('button', name=re.compile('^(Adjuntar|Attach)( archivos?| files?)?$', re.I))
        if await attach.count() != 1:
            attach = page.locator('#main footer [data-icon="plus-rounded"], #main footer [data-icon="plus"]')
        await attach.click()
        doc = page.get_by_text(re.compile(r'^(Documento|Document)$', re.I))
        async with page.expect_file_chooser(timeout=6000) as waiting:
            await doc.click()
        chooser = await waiting.value
        selected = pending[:] if chooser.is_multiple() else pending[:1]
        await chooser.set_files(selected)
        send = page.get_by_role('button', name=re.compile(r'^(Enviar|Send)( \(Enter\))?$', re.I))
        if await send.count() != 1:
            send = page.locator('[data-icon="send"]:visible')
        await send.click()
        return selected

    async def recover(self, phone, reference, collect, *, ambiguous=False, background=False):
        async with self.chat(phone, background=background) as (page, _):
            thread = page.locator('#main')
            await thread.locator('.message-out').filter(has_text=reference).first.wait_for(timeout=8500)
            messages = thread.locator('.message-in, .message-out')
            found = False
            other_request = False
            count = 0
            for bubble in await messages.all():
                body = await bubble.inner_text()
                inbound = 'message-in' in (await bubble.get_attribute('class') or '').split()
                if not inbound:
                    if reference in body:
                        found = True
                    elif found and re.search(r'FACTURAI-[A-F0-9]{16}\b', body):
                        other_request = True
                    continue
                if not found or ((ambiguous or other_request) and reference not in body):
                    continue
                if not re.search(r'\.(pdf|xml|zip)\b', body, re.I):
                    continue
                button = bubble.get_by_role('button', name=re.compile('Descargar|Download', re.I))
                if await button.count() != 1:
                    button = bubble.locator('a[download], [data-icon="download"], [data-icon="document-download"]')
                if await button.count() != 1:
                    continue
                async with page.expect_download(timeout=12_000) as waiting:
                    await button.click()
                download = await waiting.value
                name = Path(download.suggested_filename).name
                if Path(name).suffix.lower() not in {'.pdf', '.xml', '.zip'}:
                    continue
                path = await download.path()
                if not path or path.stat().st_size > 50*1024*1024:
                    raise ValueError('Uno de los comprobantes supera 50 MB o no terminó de descargarse.')
                await asyncio.to_thread(collect, name, path.read_bytes())
                count += 1
                if count >= 30:
                    break
            return count

def compose(receipt: Receipt, profile: Profile, reference: str) -> str:
    number = receipt.ticket_number or ', '.join(receipt.billing_codes)
    return (f'Buen día. Solicito la factura de mi compra en {receipt.merchant} del {receipt.date}, '
            f'folio {number}, por ${receipt.amount} MXN. Adjunto la documentación.\n\n'
            f'Receptor: {profile.name}\nRFC: {profile.rfc}\nCódigo postal: {profile.postal_code}\n'
            f'Régimen fiscal: {profile.regime}\nUso de CFDI: {profile.cfdi_use}\nForma de pago: {receipt.payment_code}\n\n'
            f'Agradeceré que envíen el PDF y el XML a {profile.email} o como respuesta a este mensaje. '
            f'Referencia {reference}. Gracias.')


async def selected_file(upload: UploadFile, label: str, reference: str):
    name = upload.filename or ''
    data = await upload.read(MAX_UPLOAD + 1)
    mime = await asyncio.to_thread(validate_document, name, data)
    return dict(name=f'{label}-{reference}{Path(name).suffix.lower()}', mimeType=mime, buffer=data)


def whatsapp_router(store, runner, profile):
    routes = APIRouter(prefix='/api/jobs/{key}/whatsapp')
    outbox = Outbox(store)
    web = WhatsAppWeb(runner)
    operations = asyncio.Lock()

    @routes.get('')
    def status(key: str):
        return outbox.status(key)

    @routes.post('/send')
    async def send(key: str, phone: str = Form(..., max_length=40), consent: bool = Form(...), watch: bool = Form(True),
                   csf: UploadFile | None = File(None), voucher: UploadFile | None = File(None)):
        if not consent:
            raise ValueError('Confirma el envío del ticket y los datos fiscales al número indicado.')
        phone = destination(phone)
        buyer = profile()
        if operations.locked():
            raise ValueError('Hay una operación de WhatsApp en curso.')
        async with operations:
            row = store.get(key)
            reference = outbox.reserve(key, phone)
            try:
                source = Path(row['source_path'] or '').resolve()
                if not source.is_relative_to(store.root / 'originales') or not source.is_file() or source.stat().st_size > MAX_UPLOAD:
                    raise ValueError('El archivo original del ticket no está disponible.')
                data = await asyncio.to_thread(source.read_bytes)
                mime = await asyncio.to_thread(validate_document, source.name, data)
                docs = [dict(name=f'ticket-{reference}{source.suffix.lower()}', mimeType=mime, buffer=data)]
                for upload, label in [(csf, 'constancia'), (voucher, 'comprobante')]:
                    if upload and upload.filename:
                        docs.append(await selected_file(upload, label, reference))
                receipt = Receipt(**row['receipt'])
                await web.send(phone, reference, compose(receipt, buyer, reference), docs,
                               lambda: outbox.arm(key, buyer, watch), lambda detail: outbox.progress(key, detail))
            except (Exception, asyncio.CancelledError) as exc:
                message = ('El envío pudo haber llegado: comprueba el texto y los adjuntos en el chat. '
                           'Recupera el CFDI aquí sin repetir la solicitud.')
                outbox.finish(key, False, message)
                if isinstance(exc, asyncio.CancelledError):
                    raise
                if outbox.status(key)['status'] == 'uncertain':
                    return outbox.status(key)
                if isinstance(exc, ValueError):
                    raise
                raise ValueError('No se inició el envío. Revisa sesión, ticket y archivos seleccionados.') from exc
            outbox.finish(key, True, f'Solicitud enviada con {len(docs)} documento(s). Esperando PDF y XML.')
            return outbox.status(key)

    async def check_reply(key, background=False):
        req = outbox.status(key)
        if req['status'] == 'verified':
            return req
        if req['status'] not in {'sent', 'uncertain'}:
            raise ValueError('No hay una solicitud enviada para este expediente.')
        row = store.get(key)
        buyer = Profile.model_validate_json(row['billing_profile'])
        paths = []
        def collect(filename, data):
            paths.append(save_candidate(store, key, filename, data))
        try:
            count = await web.recover(req['phone'], req['reference'], collect,
                                      ambiguous=outbox.overlapping(key, req['phone']), background=background)
        except ValueError as exc:
            if not paths:
                raise
            count = len(paths)
            store.event(key, 'whatsapp_partial_download', str(exc))
        if count or job_candidates(store, key):
            try:
                await asyncio.to_thread(reconcile, store, key, buyer)
                return outbox.status(key)
            except MissingPair:
                detail = 'Se guardaron los archivos disponibles. Falta el PDF o XML que complete el cotejo.'
        else:
            detail = ('No se encontró una respuesta con documentos para esta referencia. '
                      'También puedes adjuntarlos al expediente o recuperarlos desde Gmail.')
        return {**outbox.status(key), 'message': detail}

    @routes.post('/recover')
    async def recover(key: str):
        if operations.locked():
            raise ValueError('Hay una operación de WhatsApp en curso.')
        async with operations:
            return await check_reply(key)

    @routes.put('/watch')
    def watch_request(key: str, watch: bool = Body(embed=True)):
        if outbox.status(key)['status'] not in {'sent','uncertain'}:
            raise ValueError('Selecciona una solicitud de WhatsApp pendiente.')
        with store.connect() as db:
            configure(db, key, watch, time_utc())
        return outbox.status(key)

    def foreground_busy():
        viewer = runner.viewer
        return bool(operations.locked() or runner.has_active_jobs() or viewer.channel or viewer.dialog or viewer.file_request)

    async def background_check(key):
        async with asyncio.timeout(50):
            return await check_reply(key, background=True)

    routes.watcher = WhatsAppWatcher(store, background_check, foreground_busy)

    @routes.post('/open')
    async def open_chat(key: str):
        req = outbox.status(key)
        if not req['phone']:
            raise ValueError('Este expediente aún no tiene un número de WhatsApp asociado.')
        if operations.locked():
            raise ValueError('Hay una operación de WhatsApp en curso.')
        async with operations:
            await web.open(req['phone'])
        return req

    return routes
