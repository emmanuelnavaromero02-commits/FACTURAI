"""Solicitudes de CFDI en la sesión de WhatsApp Web del contenedor."""
from __future__ import annotations

import asyncio
import hashlib
import json
import re
import secrets
import time
from contextlib import asynccontextmanager
from datetime import timedelta
from pathlib import Path

from fastapi import APIRouter, Body, File, Form, UploadFile
from playwright.async_api import Error as BrowserError

from . import whatsapp_selectores as sel
from .cfdi import MissingPair
from .db import now
from .documents import job_candidates, reconcile, save_candidate
from .ingest import MAX_UPLOAD, validate_document
from .models import Profile, Receipt
from .permisos import permitido
from .whatsapp_dialogo import Conversaciones
from .whatsapp_followup import WhatsAppWatcher, configure, create_schedule, schedule_status, time_utc
from .whatsapp_fotos import MisFotos, media_items

REFERENCE = re.compile(r'FACTURAI-[A-F0-9]{16}\b')
LABELS = {'ticket': 'el ticket', 'constancia': 'la constancia fiscal', 'comprobante': 'el comprobante de pago'}
CONVERSATION_FIRST_CHECK = timedelta(seconds=30)


def destination(value):
    if not re.fullmatch(r'\+?[0-9 ()-]+', value.strip()):
        raise ValueError('Escribe el número internacional del comercio con código de país.')
    digits = re.sub(r'[^0-9]', '', value)
    if not re.fullmatch(r'[1-9][0-9]{7,14}', digits):
        raise ValueError('El número debe incluir el código del país y tener de 8 a 15 dígitos.')
    return digits


def merchant_phone(row) -> str:
    """The merchant's WhatsApp saved in the case, when the ticket model carries it."""
    receipt = Receipt(**row['receipt'])
    value = getattr(receipt, 'merchant_whatsapp', '') or ''
    return value if isinstance(value, str) else ''


class Outbox:
    def __init__(self, store):
        self.store = store
        with store.connect() as db:
            db.execute("""CREATE TABLE IF NOT EXISTS whatsapp_requests (
                job_id TEXT PRIMARY KEY, phone TEXT NOT NULL, reference TEXT NOT NULL UNIQUE,
                original_status TEXT NOT NULL, original_message TEXT NOT NULL, state TEXT NOT NULL,
                detail TEXT NOT NULL, updated TEXT NOT NULL)""")
            # File names expected in the chat for each request, and whether it was completed once.
            db.execute("""CREATE TABLE IF NOT EXISTS whatsapp_parts (
                job_id TEXT PRIMARY KEY, names TEXT NOT NULL, completions INTEGER NOT NULL DEFAULT 0)""")
            create_schedule(db)
            # A crash before arming the first send has no externally visible message.
            db.execute("""UPDATE jobs SET status=(SELECT original_status FROM whatsapp_requests WHERE job_id=jobs.id),
                message=(SELECT original_message FROM whatsapp_requests WHERE job_id=jobs.id)
                WHERE status='whatsapp_preparing' AND id IN
                (SELECT job_id FROM whatsapp_requests WHERE state='preparing')""")
            db.execute("DELETE FROM whatsapp_parts WHERE job_id IN (SELECT job_id FROM whatsapp_requests WHERE state='preparing')")
            db.execute("DELETE FROM whatsapp_requests WHERE state='preparing'")
            db.execute("""UPDATE whatsapp_requests SET state='uncertain',detail=detail || ?,updated=?
                WHERE state='sending'""", (' Se interrumpió el envío. Revisa el chat antes de continuar.', now()))

    def status(self, key):
        job = self.store.get(key)
        with self.store.connect() as db:
            req = db.execute("SELECT * FROM whatsapp_requests WHERE job_id=?", (key,)).fetchone()
            followup = schedule_status(db, key)
            parts = db.execute("SELECT completions FROM whatsapp_parts WHERE job_id=?", (key,)).fetchone()
        if req is None:
            return dict(status='new', phone='', reference='', message='', updated='', count=0, completable=False, **followup)
        verified = job['status'] == 'verified'
        if verified:
            followup.update(watch=False, next_check=None, watch_message='PDF y XML cotejados.')
        directory = self.store.root / 'downloads' / key / 'recibidos'
        count = sum(1 for f in directory.glob('*') if f.is_file() and f.suffix in {'.pdf', '.xml', '.zip'})
        state = 'verified' if verified else 'sending' if req['state']=='preparing' else req['state']
        return dict(status=state, phone=req['phone'], reference=req['reference'], updated=req['updated'], count=count,
                    message='PDF y XML cotejados.' if verified else req['detail'],
                    completable=state == 'uncertain' and not (parts and parts['completions']), **followup)

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

    def expect(self, key, names):
        with self.store.connect() as db:
            db.execute('INSERT OR REPLACE INTO whatsapp_parts(job_id,names,completions) VALUES(?,?,0)',
                       (key, json.dumps(list(names))))

    def parts(self, key):
        with self.store.connect() as db:
            row = db.execute('SELECT * FROM whatsapp_parts WHERE job_id=?', (key,)).fetchone()
        return dict(names=json.loads(row['names']), completions=row['completions']) if row else dict(names=[], completions=0)

    def claim_completion(self, key):
        """Allow completing an uncertain send exactly once, recorded before anything is sent."""
        with self.store.connect() as db:
            db.execute('BEGIN IMMEDIATE')
            db.execute('INSERT OR IGNORE INTO whatsapp_parts(job_id,names,completions) VALUES(?,?,0)', (key, '[]'))
            if db.execute("""UPDATE whatsapp_parts SET completions=completions+1 WHERE job_id=? AND completions=0
                    AND job_id IN (SELECT job_id FROM whatsapp_requests WHERE state='uncertain')""", (key,)).rowcount != 1:
                raise ValueError('Este envío ya se completó una vez. Revisa el chat en WhatsApp.')
            db.execute("INSERT INTO events(job_id,at,kind,detail) VALUES(?,?,?,?)",
                       (key, now(), 'whatsapp_complete_attempt', 'Completar el envío incierto'))

    def arm(self, key, profile, watch=True, first=timedelta(minutes=5)):
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
            configure(db, key, watch, time_utc(), first)

    def finish(self, key, success, detail):
        with self.store.connect() as db:
            db.execute('BEGIN IMMEDIATE')
            record = db.execute('SELECT * FROM whatsapp_requests WHERE job_id=?', (key,)).fetchone()
            if record['state'] == 'preparing':
                db.execute("UPDATE jobs SET status=?,message=?,updated=? WHERE id=? AND status='whatsapp_preparing'",
                           (record['original_status'], record['original_message'], now(), key))
                db.execute('DELETE FROM whatsapp_requests WHERE job_id=?', (key,))
                db.execute('DELETE FROM whatsapp_parts WHERE job_id=?', (key,))
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

    def live_overlapping(self, key, phone):
        """Another request to this number still waits for its invoice."""
        with self.store.connect() as db:
            return bool(db.execute("""SELECT 1 FROM whatsapp_requests w JOIN jobs j ON j.id=w.job_id
                      WHERE w.job_id<>? AND w.phone=? AND w.state IN ('sending','sent','uncertain')
                      AND j.status<>'verified' LIMIT 1""", (key, phone)).fetchone())


def part_label(name: str) -> str:
    return LABELS.get(name.split('-', 1)[0], name)


class WhatsAppWeb:
    def __init__(self, runner):
        self.runner = runner
        self.link = dict(state='sin_abrir', message=sel.ESTADOS['sin_abrir'], ready=False, checked=None)

    def note_link(self, state):
        self.link = dict(state=state, message=sel.ESTADOS[state], ready=state == 'listo', checked=now())
        return dict(self.link)

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

        shown = numbers(await sel.leer_encabezado(page))
        if not shown:
            # Saved contacts normally show a name. Verify the contact card itself,
            # never infer the peer of a fiscal document from a display name.
            try:
                shown = numbers(await sel.leer_ficha(page))
            except (BrowserError, sel.ElementoNoEncontrado) as exc:
                raise ValueError('No se pudo comprobar el número en la ficha del contacto. Revisa el destinatario en WhatsApp.') from exc
        if shown != {phone}:
            raise ValueError('El número que muestra el chat difiere del destinatario o no se puede comprobar.')

    async def navigate(self, page, phone):
        try:
            editor = await sel.abrir_chat(page, phone)
        except sel.SinVincular as exc:
            self.note_link(exc.estado)
            raise
        self.note_link('listo')
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
                runner.personal_at_revision = None
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

    async def check_link(self, *, timeout=15):
        """Read the link state without opening any chat."""
        runner = self.runner
        page = runner.whatsapp
        if page is not None and not page.is_closed():
            return self.note_link(await sel.estado_vinculo(page))
        if runner.has_active_jobs():
            raise ValueError('Espera a que termine el trámite del navegador.')
        async with runner.lock:
            await runner.start_browser()
            page = await runner.context.new_page()
            try:
                await page.goto('https://web.whatsapp.com/', wait_until='domcontentloaded', timeout=30_000)
                limit = time.monotonic() + timeout
                state = await sel.estado_vinculo(page)
                while state == 'cargando' and time.monotonic() < limit:
                    await asyncio.sleep(.3)
                    state = await sel.estado_vinculo(page)
            except BrowserError:
                state = 'sin_conexion'
            finally:
                if not page.is_closed():
                    await page.close()
        return self.note_link(state)

    async def open(self, phone):
        async with self.chat(phone):
            pass

    @staticmethod
    async def outgoing(page, text, timeout):
        if not await sel.esperar_saliente(page, text, 0, timeout=timeout):
            raise ValueError('WhatsApp no mostró en el chat lo que se envió. Revisa la conversación.')

    async def send(self, phone, reference, message, documents, on_attempt, on_progress=None):
        async with self.chat(phone) as (page, editor):
            if await sel.borrador(editor):
                raise ValueError('Este chat tiene un borrador. Consérvalo o envíalo en WhatsApp antes de preparar la factura.')
            await sel.escribir(editor, message)
            on_attempt()
            await sel.enviar_texto(editor)
            await self.outgoing(page, reference, 12_000)
            if on_progress:
                on_progress('El texto aparece en el chat. Todavía faltan adjuntos por confirmar.')
            await self.send_documents(page, documents, on_progress)

    async def send_documents(self, page, documents, on_progress=None):
        pending = list(documents)
        while pending:
            selected = await self.attach_documents(page, pending)
            for payload in selected:
                await self.outgoing(page, payload['name'], 15_000)
                pending.remove(payload)
                if on_progress:
                    on_progress(f'Confirmados en el chat: texto y {len(documents)-len(pending)} de {len(documents)} adjuntos.')

    @staticmethod
    async def attach_documents(page, pending):
        return await sel.adjuntar(page, pending)

    @staticmethod
    async def find_reference(page, reference):
        try:
            await sel.esperar(page, 'burbuja', timeout=8500)
        except sel.ElementoNoEncontrado as exc:
            raise ValueError('El chat no muestra mensajes todavía. Revisa la conversación en WhatsApp.') from exc
        if not await sel.mas_historial(page, reference):
            raise ValueError('No encuentro en el chat la solicitud de este expediente. Revisa la conversación en WhatsApp.')

    async def collect_files(self, page, reference, collect, ambiguous):
        await self.find_reference(page, reference)
        found = False
        other_request = False
        count = 0
        for bubble in await sel.burbujas(page):
            if not bubble.entrante:
                if reference in bubble.texto:
                    found = True
                elif found and REFERENCE.search(bubble.texto):
                    other_request = True
                continue
            if not found or ((ambiguous or other_request) and reference not in bubble.texto):
                continue
            if not bubble.archivos:
                continue
            # A bubble can carry several documents; every control is read once.
            for control in await sel.todos(bubble.locator, 'descargar'):
                download = await sel.descargar(page, control)
                name = Path(download.suggested_filename).name
                if Path(name).suffix.lower() not in {'.pdf', '.xml', '.zip'}:
                    continue
                path = await download.path()
                if not path or path.stat().st_size > 50*1024*1024:
                    raise ValueError('Uno de los comprobantes supera 50 MB o no terminó de descargarse.')
                await asyncio.to_thread(collect, name, path.read_bytes())
                count += 1
                if count >= 30:
                    return count
        return count

    async def recover(self, phone, reference, collect, *, ambiguous=False, background=False):
        async with self.chat(phone, background=background) as (page, _):
            return await self.collect_files(page, reference, collect, ambiguous)

    async def converse(self, phone, reference, collect, respond, confirm, *, ambiguous=False, background=False):
        """Download what arrived and answer the merchant's newest messages once, in the verified chat."""
        async with self.chat(phone, background=background) as (page, editor):
            count = await self.collect_files(page, reference, collect, ambiguous)
            rows = await sel.burbujas(page)
            start = next((i for i, b in enumerate(rows) if not b.entrante and reference in b.texto), None)
            if start is None:
                return dict(count=count, inbound=False, decision='none')
            after = rows[start + 1:]
            if any(not b.entrante and REFERENCE.search(b.texto) and reference not in b.texto for b in after):
                return dict(count=count, inbound=False, decision='none')  # A newer request owns this chat.
            ours = [i for i, b in enumerate(after) if not b.entrante and reference not in b.texto]
            first = ours[-1] + 1 if ours else 0
            new = [(first + i, b) for i, b in enumerate(after[first:]) if b.entrante]
            if not new:
                return dict(count=count, inbound=False, decision='none')
            position, last = new[-1]
            key = last.ident or hashlib.sha256(f'{position}\0{last.texto}'.encode()).hexdigest()[:24]
            decision = await respond([b.texto for _, b in new], key, any(b.archivos for _, b in new))
            if decision.kind == 'reply':
                if await sel.borrador(editor):
                    confirm(decision.reply_id, False, 'Hay un borrador en el chat; no contesté al comercio. Revísalo en WhatsApp.')
                    return dict(count=count, inbound=True, decision='stop')
                before = sum(1 for b in rows if not b.entrante)
                await sel.escribir(editor, decision.text)
                await sel.enviar_texto(editor)
                confirm(decision.reply_id, await sel.esperar_saliente(page, decision.text, before))
            return dict(count=count, inbound=True, decision=decision.kind)

    async def complete(self, phone, reference, message, expected, available, on_attempt, on_progress=None):
        """Send only what is really missing from an uncertain request, checked in the chat itself."""
        async with self.chat(phone) as (page, editor):
            if await sel.buscar(page, 'burbuja') is not None:
                await sel.mas_historial(page, reference)
            out = [b for b in await sel.burbujas(page) if not b.entrante]
            names = {n for n in expected if any(n in b.texto for b in out)}
            text_sent = any(reference in b.texto and not any(n in b.texto for n in expected) for b in out)
            missing = [n for n in expected if n not in names]
            lacking = [n for n in missing if n not in available]
            if lacking:
                raise ValueError('Para completar el envío vuelve a elegir ' + ', '.join(part_label(n) for n in lacking) + '.')
            if not text_sent and await sel.borrador(editor):
                raise ValueError('Este chat tiene un borrador. Consérvalo o envíalo en WhatsApp antes de completar la solicitud.')
            on_attempt()
            if not text_sent:
                await sel.escribir(editor, message)
                await sel.enviar_texto(editor)
                await self.outgoing(page, 'Referencia ' + reference, 12_000)
            await self.send_documents(page, [available[n] for n in missing], on_progress)
            return dict(text=not text_sent, files=len(missing))

    async def own_media(self, phone, seen, keep, *, baseline, remember):
        """Read only the owner's chat with themselves and hand over new photos and PDF."""
        async with self.chat(phone, background=True) as (page, _):
            try:
                await sel.esperar(page, 'burbuja', timeout=3000)
            except sel.ElementoNoEncontrado:
                return 0
            return await media_items(page, seen, keep, baseline=baseline, remember=remember)


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


async def ticket_file(store, row, reference):
    source = Path(row['source_path'] or '').resolve()
    if not source.is_relative_to(store.root / 'originales') or not source.is_file() or source.stat().st_size > MAX_UPLOAD:
        raise ValueError('El archivo original del ticket no está disponible.')
    data = await asyncio.to_thread(source.read_bytes)
    mime = await asyncio.to_thread(validate_document, source.name, data)
    return dict(name=f'ticket-{reference}{source.suffix.lower()}', mimeType=mime, buffer=data)


def whatsapp_router(store, runner, profile, plan=None):
    routes = APIRouter()
    base = '/api/jobs/{key}/whatsapp'
    outbox = Outbox(store)
    talks = Conversaciones(store)
    web = WhatsAppWeb(runner)
    operations = asyncio.Lock()

    def full_status(key):
        return {**outbox.status(key), **talks.estado(key)}

    def conversation_allowed(key):
        row = talks.get(key)
        if not talks.activa(key):
            return False
        # Answers started under the standing permission stop as soon as it is withdrawn.
        return row['origin'] != 'permiso' or permitido(store.root, 'pedir_por_whatsapp')

    @routes.get(base)
    def status(key: str):
        return full_status(key)

    async def deliver(key, phone, buyer, uploads, watch, reply_origin=''):
        if operations.locked():
            raise ValueError('Hay una operación de WhatsApp en curso.')
        async with operations:
            row = store.get(key)
            reference = outbox.reserve(key, phone)
            try:
                docs = [await ticket_file(store, row, reference)]
                for upload, label in uploads:
                    if upload and upload.filename:
                        docs.append(await selected_file(upload, label, reference))
                outbox.expect(key, [doc['name'] for doc in docs])
                receipt = Receipt(**row['receipt'])

                def arm():
                    outbox.arm(key, buyer, watch, CONVERSATION_FIRST_CHECK if reply_origin else timedelta(minutes=5))
                    if reply_origin:
                        talks.activar(key, reply_origin)
                await web.send(phone, reference, compose(receipt, buyer, reference), docs,
                               arm, lambda detail: outbox.progress(key, detail))
            except (Exception, asyncio.CancelledError) as exc:
                message = ('El envío pudo haber llegado: comprueba el texto y los adjuntos en el chat. '
                           'Recupera el CFDI aquí sin repetir la solicitud.')
                outbox.finish(key, False, message)
                if isinstance(exc, asyncio.CancelledError):
                    raise
                if outbox.status(key)['status'] == 'uncertain':
                    return full_status(key)
                if isinstance(exc, ValueError):
                    raise
                raise ValueError('No se inició el envío. Revisa sesión, ticket y archivos seleccionados.') from exc
            outbox.finish(key, True, f'Solicitud enviada con {len(docs)} documento(s). Esperando PDF y XML.')
            return full_status(key)

    @routes.post(base + '/send')
    async def send(key: str, phone: str = Form('', max_length=40), consent: bool = Form(...), watch: bool = Form(True),
                   reply: bool = Form(False), csf: UploadFile | None = File(None), voucher: UploadFile | None = File(None)):
        if not consent:
            raise ValueError('Confirma el envío del ticket y los datos fiscales al número indicado.')
        number = phone.strip() or merchant_phone(store.get(key))
        if not number:
            raise ValueError('Escribe el número de WhatsApp del comercio con código de país.')
        return await deliver(key, destination(number), profile(), [(csf, 'constancia'), (voucher, 'comprobante')],
                             watch or reply, 'persona' if reply else '')

    async def request_automatically(key):
        """For the queue: ask for the invoice without a per-ticket confirmation, only under the standing permission."""
        if not permitido(store.root, 'pedir_por_whatsapp'):
            raise ValueError('Activa en Ajustes el permiso para pedir facturas por WhatsApp sin confirmar cada ticket.')
        number = merchant_phone(store.get(key))
        if not number:
            raise ValueError('El expediente no tiene el número de WhatsApp del comercio.')
        return await deliver(key, destination(number), profile(), [], True, 'permiso')

    routes.request_automatically = request_automatically

    async def check_reply(key, background=False):
        req = outbox.status(key)
        if req['status'] == 'verified':
            talks.terminar(key)
            return {**full_status(key), 'live': False}
        if req['status'] not in {'sent', 'uncertain'}:
            raise ValueError('No hay una solicitud enviada para este expediente.')
        row = store.get(key)
        buyer = Profile.model_validate_json(row['billing_profile'])
        receipt = Receipt(**row['receipt'])
        paths = []
        def collect(filename, data):
            paths.append(save_candidate(store, key, filename, data))
        ambiguous = outbox.live_overlapping(key, req['phone'])
        try:
            if conversation_allowed(key):
                async def respond(texts, clave, files):
                    return await asyncio.to_thread(talks.decidir, key, texts, clave, receipt, buyer, plan, files)
                def confirm(reply_id, ok, detail=''):
                    talks.confirmar(key, reply_id, ok, detail)
                result = await web.converse(req['phone'], req['reference'], collect, respond, confirm,
                                            ambiguous=ambiguous, background=background)
                count = result['count']
            else:
                count = await web.recover(req['phone'], req['reference'], collect,
                                          ambiguous=ambiguous, background=background)
        except ValueError as exc:
            if not paths:
                raise
            count = len(paths)
            store.event(key, 'whatsapp_partial_download', str(exc))
        live = talks.viva(key)
        if count or job_candidates(store, key):
            try:
                await asyncio.to_thread(reconcile, store, key, buyer)
                talks.terminar(key)
                return {**full_status(key), 'live': False}
            except MissingPair:
                detail = 'Se guardaron los archivos disponibles. Falta el PDF o XML que complete el cotejo.'
        else:
            detail = ('No se encontró una respuesta con documentos para esta referencia. '
                      'También puedes adjuntarlos al expediente o recuperarlos desde Gmail.')
        conversation = talks.estado(key)
        if conversation['attention'] or (conversation['reply'] and conversation['conversation'] != 'done'):
            detail = conversation['conversation_message'] or detail
        return {**full_status(key), 'message': detail, 'live': live}

    routes.check_reply = check_reply

    @routes.post(base + '/recover')
    async def recover(key: str):
        if operations.locked():
            raise ValueError('Hay una operación de WhatsApp en curso.')
        async with operations:
            return await check_reply(key)

    @routes.put(base + '/watch')
    def watch_request(key: str, watch: bool = Body(embed=True)):
        if outbox.status(key)['status'] not in {'sent','uncertain'}:
            raise ValueError('Selecciona una solicitud de WhatsApp pendiente.')
        with store.connect() as db:
            configure(db, key, watch, time_utc())
        return full_status(key)

    @routes.put(base + '/reply')
    def reply_request(key: str, reply: bool = Body(embed=True)):
        """The person lets the program answer the merchant's bot for this case (or stops it)."""
        if outbox.status(key)['status'] not in {'sent', 'uncertain'}:
            raise ValueError('Selecciona una solicitud de WhatsApp pendiente.')
        talks.activar(key, 'persona', reply)
        if reply:
            with store.connect() as db:
                configure(db, key, True, time_utc(), CONVERSATION_FIRST_CHECK)
        return full_status(key)

    @routes.post(base + '/complete')
    async def complete(key: str, csf: UploadFile | None = File(None), voucher: UploadFile | None = File(None)):
        req = outbox.status(key)
        if req['status'] != 'uncertain':
            raise ValueError('Solo se puede completar un envío que quedó incierto.')
        if not req['completable']:
            raise ValueError('Este envío ya se completó una vez. Revisa el chat en WhatsApp.')
        if operations.locked():
            raise ValueError('Hay una operación de WhatsApp en curso.')
        async with operations:
            row = store.get(key)
            reference = req['reference']
            buyer = Profile.model_validate_json(row['billing_profile'])
            receipt = Receipt(**row['receipt'])
            expected = outbox.parts(key)['names']
            available = {}
            ticket = await ticket_file(store, row, reference)
            available[ticket['name']] = ticket
            for upload, label in [(csf, 'constancia'), (voucher, 'comprobante')]:
                if upload and upload.filename:
                    payload = await selected_file(upload, label, reference)
                    # Keep the name the merchant may already have, whatever the new file is called.
                    previous = next((n for n in expected if n.startswith(label + '-')), payload['name'])
                    payload['name'] = previous
                    available[previous] = payload
            if not expected:
                expected = [ticket['name']]
            claimed = []
            def attempt():
                outbox.claim_completion(key)
                claimed.append(True)
            try:
                result = await web.complete(req['phone'], reference, compose(receipt, buyer, reference), expected,
                                            available, attempt)
            except (Exception, asyncio.CancelledError) as exc:
                if claimed:
                    outbox.finish(key, False, 'No se confirmó el complemento del envío; revisa el chat antes de seguir.')
                if isinstance(exc, (ValueError, asyncio.CancelledError)):
                    raise
                raise ValueError('No se pudo completar el envío. Revisa la sesión y el chat.') from exc
            sent = (['el texto'] if result['text'] else []) + ([f'{result["files"]} adjunto(s)'] if result['files'] else [])
            detail = ('Completé el envío: ' + ' y '.join(sent) + '. Esperando PDF y XML.') if sent else \
                'El chat ya tenía el texto y los adjuntos. Esperando PDF y XML.'
            outbox.finish(key, True, detail)
            return full_status(key)

    def viewer_busy():
        viewer = runner.viewer
        return bool(runner.has_active_jobs() or viewer.channel or viewer.dialog or viewer.file_request)

    def foreground_busy():
        return bool(operations.locked() or viewer_busy())

    async def background_check(key):
        async with asyncio.timeout(50):
            return await check_reply(key, background=True)

    photos = MisFotos(store, web, profile, viewer_busy)

    async def photos_tick(force=False):
        if foreground_busy() or not (force or photos.due()):
            return []
        async with operations:
            async with asyncio.timeout(90):
                return await photos.tick(force=force)

    routes.watcher = WhatsAppWatcher(store, background_check, foreground_busy, extras=[photos_tick])
    routes.photos = photos

    @routes.post(base + '/open')
    async def open_chat(key: str):
        req = outbox.status(key)
        if not req['phone']:
            raise ValueError('Este expediente aún no tiene un número de WhatsApp asociado.')
        if operations.locked():
            raise ValueError('Hay una operación de WhatsApp en curso.')
        async with operations:
            await web.open(req['phone'])
        return full_status(key)

    async def link_state():
        page = runner.whatsapp
        if page is not None and not page.is_closed() and not operations.locked():
            web.note_link(await sel.estado_vinculo(page))
        return dict(web.link)

    async def check_link():
        """Look at the link for real, opening WhatsApp for a moment when nothing has it open."""
        if operations.locked():
            raise ValueError('Hay una operación de WhatsApp en curso.')
        async with operations:
            return await web.check_link()

    def in_use():
        """The person uses WhatsApp here: a standing permission is on, or a request still waits for its invoice."""
        if any(permitido(store.root, name) for name in ('pedir_por_whatsapp', 'leer_mis_fotos_de_whatsapp')):
            return True
        with store.connect() as db:
            return bool(db.execute("""SELECT 1 FROM whatsapp_requests w JOIN jobs j ON j.id=w.job_id
                WHERE w.state IN ('preparing','sending','sent','uncertain') AND j.status<>'verified' LIMIT 1""").fetchone())

    routes.link_state = link_state
    # For the queue: after a restart the state reads «sin_abrir» until something opens WhatsApp.
    routes.check_link = check_link
    routes.in_use = in_use
    routes.attention = talks.atencion

    @routes.get('/api/whatsapp/link')
    async def link():
        return await link_state()

    @routes.post('/api/whatsapp/link/check')
    async def link_check():
        return await check_link()

    @routes.get('/api/whatsapp/attention')
    async def attention():
        return dict(link=await link_state(), conversations=talks.atencion())

    @routes.get('/api/whatsapp/photos')
    def photos_status():
        return photos.status()

    @routes.post('/api/whatsapp/photos/check')
    async def photos_check():
        if foreground_busy():
            raise ValueError('WhatsApp o el navegador están ocupados. Intenta en un momento.')
        imported = await photos_tick(force=True)
        return {**photos.status(), 'imported': imported}

    return routes
