"""Seguimiento voluntario y persistente de respuestas de WhatsApp, con prioridad al visor."""
from __future__ import annotations

import asyncio
import logging
import random
from datetime import datetime, timedelta, timezone

_LOG = logging.getLogger(__name__)


def time_utc():
    return datetime.now(timezone.utc)


def create_schedule(db):
    db.execute("""CREATE TABLE IF NOT EXISTS whatsapp_followups (
        job_id TEXT PRIMARY KEY, enabled INTEGER NOT NULL, next_check TEXT, last_check TEXT,
        misses INTEGER NOT NULL DEFAULT 0, detail TEXT NOT NULL DEFAULT '')""")
    db.execute("CREATE INDEX IF NOT EXISTS wa_due ON whatsapp_followups(enabled, next_check)")


def configure(db, key, enabled, when, first=timedelta(minutes=5)):
    due = (when + first).isoformat() if enabled else None
    db.execute("""INSERT INTO whatsapp_followups(job_id,enabled,next_check,detail) VALUES(?,?,?,?)
        ON CONFLICT(job_id) DO UPDATE SET enabled=excluded.enabled, next_check=excluded.next_check,
        misses=0, detail=excluded.detail""", (key, int(enabled), due, 'Seguimiento activo.' if enabled else 'Seguimiento desactivado.'))


def schedule_status(db, key):
    row = db.execute("SELECT * FROM whatsapp_followups WHERE job_id=?", (key,)).fetchone()
    if not row:
        return dict(watch=False, next_check=None, last_check=None, watch_message='')
    return dict(watch=bool(row['enabled']), next_check=row['next_check'], last_check=row['last_check'], watch_message=row['detail'])


class WhatsAppWatcher:
    def __init__(self, store, recover, busy, clock=time_utc, extras=(), pause=5):
        self.store = store
        self.recover = recover
        self.busy = busy
        self.clock = clock
        self.task = None
        # Other periodic WhatsApp duties (own photos) share this loop and its yielding rules.
        self.extras = list(extras)
        self.pause = pause

    def start(self):
        if self.task is None or self.task.done():
            self.task = asyncio.create_task(self.run())

    async def close(self):
        if self.task and not self.task.done():
            self.task.cancel()
            try:
                await self.task
            except asyncio.CancelledError:
                pass
        self.task = None

    async def run(self):
        while True:
            try:
                await self.tick()
            except Exception:
                _LOG.exception('Error en seguimiento de respuestas de WhatsApp')
            for extra in self.extras:
                try:
                    await extra()
                except Exception:
                    _LOG.exception('Error en una tarea periódica de WhatsApp')
            # Short pauses keep a live merchant conversation at a 20-30 s rhythm.
            await asyncio.sleep(self.pause)

    def claim_due(self):
        when = self.clock()
        with self.store.connect() as db:
            db.execute('BEGIN IMMEDIATE')
            db.execute("""UPDATE whatsapp_followups SET enabled=0,next_check=NULL,detail='PDF y XML cotejados.'
                WHERE enabled=1 AND job_id IN (SELECT id FROM jobs WHERE status='verified')""")
            row = db.execute("""SELECT f.job_id,f.misses FROM whatsapp_followups f
                JOIN whatsapp_requests w ON w.job_id=f.job_id JOIN jobs j ON j.id=f.job_id
                WHERE f.enabled=1 AND f.next_check<=? AND w.state IN ('sent','uncertain')
                AND j.status='external' ORDER BY f.next_check,f.job_id LIMIT 1""", (when.isoformat(),)).fetchone()
            if row:
                # A short lease also bounds retries after a process crash.
                db.execute("UPDATE whatsapp_followups SET next_check=? WHERE job_id=?",
                           ((when + timedelta(minutes=5)).isoformat(), row['job_id']))
            return row

    def reschedule(self, key, misses, detail, verified=False, interrupted=False, live=False):
        when = self.clock()
        if live and not interrupted:
            # The merchant answered in the last ten minutes: keep up with the conversation.
            delay = timedelta(seconds=random.randint(20, 30))
        else:
            delay = timedelta(minutes=1 if interrupted else min(120, 5 * 2**min(misses, 5)))
        next_check = None if verified else (when + delay).isoformat()
        with self.store.connect() as db:
            db.execute("""UPDATE whatsapp_followups SET next_check=?,enabled=?,last_check=CASE WHEN ? THEN last_check ELSE ? END,
                misses=?, detail=? WHERE job_id=? AND enabled=1""",
                (next_check, int(not verified), int(interrupted), when.isoformat(), misses, detail[:500], key))

    async def tick(self):
        if self.busy():
            return
        row = self.claim_due()
        if not row:
            return
        key = row['job_id']
        work = asyncio.create_task(self.recover(key))
        try:
            while not work.done():
                await asyncio.wait([work], timeout=.35)
                if work.done():
                    break
                with self.store.connect() as db:
                    enabled = schedule_status(db, key)['watch']
                if not enabled or self.busy():
                    self.reschedule(key, row['misses'], 'Esperando a que se libere el navegador.', interrupted=True)
                    return
            result = await work
            verified = result['status'] == 'verified'
            live = bool(result.get('live')) and not verified
            self.reschedule(key, 0 if verified or live else row['misses']+1,
                            result['message'], verified=verified, live=live)
        except Exception as exc:
            if isinstance(exc, ValueError):
                detail = str(exc)
            else:
                detail = 'No se completó la consulta. Se volverá a intentar.'
            self.reschedule(key, row['misses']+1, detail)
        finally:
            if not work.done():
                work.cancel()
                await asyncio.gather(work, return_exceptions=True)
