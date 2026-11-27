"""Background watcher: turns ticket, chat, mailbox and WhatsApp changes into one notice each.

It only reads what the rest of the program already records (the jobs table, the events and the WhatsApp
attention list); it never changes a ticket. Its own marks live in the database, so a restart neither repeats
an old notice nor reports the whole history. The first run takes the current state as already notified.
"""
from __future__ import annotations

import asyncio
import json
import logging
from datetime import datetime, timedelta

from .invoice_mail import AMBIGUOUS

_LOG = logging.getLogger(__name__)
EVENT_BATCH = 500
TICKET_OVERLAP = timedelta(seconds=60)
LINK_EVERY = 60

# Kind of intervention (``needs_user`` event detail) -> short reason. Fixed texts only.
MOTIVOS = {
    'captcha': 'Resuelve la verificación «no soy un robot»',
    'login': 'Inicia sesión en el portal',
    'issue': 'Aprueba la emisión de la factura',
    'confirm': 'Confirma el paso que muestra el portal',
    'attachment': 'Adjunta el archivo que pide el portal',
    'terms': 'Revisa los términos que pide el portal',
    'plan': 'Revisa la conexión del servicio de lectura',
    'blocked': 'Revisa un paso en el que el portal no avanza',
    'unknown': 'Revisa un paso pendiente en el portal',
}
# The person chose to operate the portal: they are already there.
SIN_AVISO = {'operator'}
PREGUNTA = 'Contesta una pregunta en el chat'
CORREO_AMBIGUO = 'Llegaron varias facturas por correo: elige la de este ticket'
WHATSAPP_PIDE = 'El comercio pide algo por WhatsApp'
WHATSAPP_VINCULO = 'Vuelve a vincular WhatsApp con el código QR'
ESTADOS_TICKET = {
    'verified': ('factura_lista', 'La factura quedó comprobada con su PDF y XML'),
    'blocked': ('ticket_detenido', 'El trámite se detuvo y necesita tu revisión'),
    'uncertain': ('ticket_detenido', 'No se sabe si se emitió: revísalo antes de repetir'),
}
LINK_STATES = {'listo', 'qr', 'sesion_cerrada'}


def create_tables(db):
    db.execute('CREATE TABLE IF NOT EXISTS avisos_marcas (clave TEXT PRIMARY KEY, valor TEXT NOT NULL)')
    db.execute('CREATE TABLE IF NOT EXISTS avisos_tickets (job_id TEXT PRIMARY KEY, status TEXT NOT NULL)')
    db.execute('CREATE TABLE IF NOT EXISTS avisos_whatsapp (job_id TEXT PRIMARY KEY, firma TEXT NOT NULL)')


def _mark(db, key, default=None):
    row = db.execute('SELECT valor FROM avisos_marcas WHERE clave=?', (key,)).fetchone()
    return row['valor'] if row else default


def _set_mark(db, key, value):
    db.execute('INSERT INTO avisos_marcas(clave,valor) VALUES(?,?) ON CONFLICT(clave) DO UPDATE SET valor=excluded.valor',
               (key, str(value)))


def _merchant(raw) -> str:
    try:
        return str((json.loads(raw) or {}).get('merchant') or '')
    except (TypeError, ValueError):
        return ''


class VigilanteAvisos:
    def __init__(self, store, avisos, whatsapp=lambda: None, tick: float = 5):
        self.store = store
        self.avisos = avisos
        self.whatsapp = whatsapp
        self.tick = tick
        self.task = None
        self.stop = asyncio.Event()
        self._last_link = 0.0
        with store.connect() as db:
            create_tables(db)

    def start(self):
        self.stop.clear()
        if self.task is None or self.task.done():
            self.task = asyncio.create_task(self.run())

    async def close(self):
        self.stop.set()
        if self.task and not self.task.done():
            self.task.cancel()
            try:
                await self.task
            except asyncio.CancelledError:
                pass
        self.task = None

    async def run(self):
        await asyncio.to_thread(self.avisos.pendientes_de_entrega)
        while not self.stop.is_set():
            try:
                await asyncio.to_thread(self.revisar)
            except Exception:
                _LOG.warning('No se completó la revisión de avisos.')
            try:
                await self.revisar_whatsapp()
            except Exception:
                _LOG.warning('No se completó la revisión de avisos de WhatsApp.')
            try:
                await asyncio.wait_for(self.stop.wait(), timeout=self.tick)
            except asyncio.TimeoutError:
                pass

    # ------------------------------------------------------------ tickets and events

    def instalar(self, db) -> bool:
        """First run: everything that already happened counts as notified."""
        if _mark(db, 'eventos') is not None:
            return False
        _set_mark(db, 'eventos', db.execute('SELECT COALESCE(MAX(id),0) FROM events').fetchone()[0])
        db.execute('''INSERT OR REPLACE INTO avisos_tickets(job_id,status)
            SELECT id,status FROM jobs WHERE historical=0''')
        _set_mark(db, 'tickets_hasta', db.execute('SELECT COALESCE(MAX(updated),\'\') FROM jobs').fetchone()[0])
        return True

    def revisar(self) -> list[dict]:
        """One pass: record every new change as a notice in one transaction, then deliver them."""
        recorded = []
        with self.store.connect() as db:
            db.execute('BEGIN IMMEDIATE')
            if self.instalar(db):
                return []
            recorded += self._events(db)
            recorded += self._tickets(db)
        for aviso in recorded:
            self.avisos.entregar(aviso)
        return recorded

    def _job(self, db, key):
        return db.execute('SELECT id,receipt,historical FROM jobs WHERE id=?', (key,)).fetchone()

    def _events(self, db) -> list[dict]:
        after = int(_mark(db, 'eventos', 0))
        rows = db.execute('''SELECT id,job_id,kind,detail FROM events WHERE id>?
            AND kind IN ('needs_user','question','gmail_recovery') ORDER BY id LIMIT ?''',
                          (after, EVENT_BATCH)).fetchall()
        if len(rows) == EVENT_BATCH:
            last = rows[-1]['id']
        else:
            # The write lock is held: every event up to the newest one has been read.
            last = db.execute('SELECT COALESCE(MAX(id),0) FROM events').fetchone()[0]
        result = []
        for row in rows:
            job = self._job(db, row['job_id']) if row['job_id'] else None
            if job is None or job['historical']:
                continue
            tipo = motivo = None
            pantalla = 'bandeja'
            if row['kind'] == 'needs_user' and row['detail'] not in SIN_AVISO:
                tipo, motivo = 'te_necesito', MOTIVOS.get(row['detail'], MOTIVOS['unknown'])
            elif row['kind'] == 'question':
                tipo, motivo, pantalla = 'te_necesito', PREGUNTA, 'chat'
            elif row['kind'] == 'gmail_recovery' and row['detail'] == AMBIGUOUS:
                tipo, motivo = 'correo_ambiguo', CORREO_AMBIGUO
            if tipo:
                aviso = self.avisos.registrar(db, tipo, clave=f"evento:{row['id']}", job_id=job['id'],
                                              comercio=_merchant(job['receipt']), motivo=motivo, pantalla=pantalla)
                if aviso:
                    result.append(aviso)
        if last > after:
            _set_mark(db, 'eventos', last)
        return result

    def _tickets(self, db) -> list[dict]:
        since = _mark(db, 'tickets_hasta', '')
        try:
            cutoff = (datetime.fromisoformat(since) - TICKET_OVERLAP).isoformat() if since else ''
        except ValueError:
            cutoff = ''
        rows = db.execute('''SELECT j.id,j.status,j.receipt,j.updated,t.status AS seen FROM jobs j
            LEFT JOIN avisos_tickets t ON t.job_id=j.id
            WHERE j.historical=0 AND j.updated>? ORDER BY j.updated,j.id''', (cutoff,)).fetchall()
        result, newest = [], since
        for row in rows:
            newest = max(newest, row['updated'])
            if row['seen'] == row['status']:
                continue
            db.execute('INSERT INTO avisos_tickets(job_id,status) VALUES(?,?) ON CONFLICT(job_id) '
                       'DO UPDATE SET status=excluded.status', (row['id'], row['status']))
            if row['status'] not in ESTADOS_TICKET:
                continue
            tipo, motivo = ESTADOS_TICKET[row['status']]
            # A verified invoice is announced once per ticket; a stop, once per stop.
            clave = (f"ticket:{row['id']}:verified" if row['status'] == 'verified'
                     else f"ticket:{row['id']}:{row['status']}:{row['updated']}")
            aviso = self.avisos.registrar(db, tipo, clave=clave, job_id=row['id'],
                                          comercio=_merchant(row['receipt']), motivo=motivo)
            if aviso:
                result.append(aviso)
        if newest and newest != since:
            _set_mark(db, 'tickets_hasta', newest)
        return result

    # ------------------------------------------------------------ WhatsApp

    def revisar_conversaciones(self, attention: list[dict]) -> list[dict]:
        """Merchant conversations that stopped waiting for the person, once per situation."""
        current = {}
        for item in attention or []:
            key = str(item.get('job_id') or '')
            if key:
                current[key] = f"{item.get('state', '')}|{item.get('detail', '')}"
        recorded = []
        with self.store.connect() as db:
            db.execute('BEGIN IMMEDIATE')
            first = _mark(db, 'whatsapp_instalado') is None
            seen = {r['job_id']: r['firma'] for r in db.execute('SELECT job_id,firma FROM avisos_whatsapp')}
            for key in set(seen) - set(current):
                db.execute('DELETE FROM avisos_whatsapp WHERE job_id=?', (key,))
            for key, firma in current.items():
                if seen.get(key) == firma:
                    continue
                db.execute('INSERT INTO avisos_whatsapp(job_id,firma) VALUES(?,?) ON CONFLICT(job_id) '
                           'DO UPDATE SET firma=excluded.firma', (key, firma))
                if first:
                    continue
                job = self._job(db, key)
                if job is None or job['historical']:
                    continue
                count = db.execute("SELECT COUNT(*) FROM avisos WHERE clave LIKE ?", (f'whatsapp:{key}:%',)).fetchone()[0]
                aviso = self.avisos.registrar(db, 'te_necesito', clave=f'whatsapp:{key}:{count + 1}', job_id=key,
                                              comercio=_merchant(job['receipt']), motivo=WHATSAPP_PIDE)
                if aviso:
                    recorded.append(aviso)
            if first:
                _set_mark(db, 'whatsapp_instalado', 1)
        for aviso in recorded:
            self.avisos.entregar(aviso)
        return recorded

    def revisar_vinculo(self, state: str) -> dict | None:
        """Notify once when a linked WhatsApp session is lost."""
        if state not in LINK_STATES:
            return None
        aviso = None
        with self.store.connect() as db:
            db.execute('BEGIN IMMEDIATE')
            previous = _mark(db, 'whatsapp_vinculo')
            if previous == state:
                return None
            _set_mark(db, 'whatsapp_vinculo', state)
            if previous == 'listo' and state in {'qr', 'sesion_cerrada'}:
                count = db.execute("SELECT COUNT(*) FROM avisos WHERE clave LIKE 'whatsapp:vinculo:%'").fetchone()[0]
                aviso = self.avisos.registrar(db, 'whatsapp', clave=f'whatsapp:vinculo:{count + 1}',
                                              motivo=WHATSAPP_VINCULO, pantalla='ajustes')
        if aviso:
            self.avisos.entregar(aviso)
        return aviso

    async def revisar_whatsapp(self):
        routes = self.whatsapp()
        if routes is None:
            return
        attention = getattr(routes, 'attention', None)
        if attention is not None:
            await asyncio.to_thread(self.revisar_conversaciones, await asyncio.to_thread(attention))
        link_state = getattr(routes, 'link_state', None)
        loop = asyncio.get_running_loop()
        if link_state is not None and loop.time() - self._last_link >= LINK_EVERY:
            self._last_link = loop.time()
            try:
                async with asyncio.timeout(5):
                    link = await link_state()
            except Exception:
                return  # Busy, closed or slow page: the next round reads it again.
            await asyncio.to_thread(self.revisar_vinculo, (link or {}).get('state', ''))
