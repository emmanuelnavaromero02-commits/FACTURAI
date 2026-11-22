"""Recover a confirmed invoice by its exact UUID, without opening its merchant portal."""
from __future__ import annotations

import asyncio
import logging
import time

from .cfdi import UUID_RE
from .documents import reconcile
from .gmail import recover_attachments
from .models import Profile
from .read_http import ServiceWait


class InvoiceMailRecovery:
    delays = (120, 600, 3600, 14400)

    def __init__(self, store, enabled, active_job):
        self.store = store
        self.enabled = enabled
        self.active_job = active_job
        self.task = None
        self.stop = asyncio.Event()
        with store.connect() as db:
            db.execute('''CREATE TABLE IF NOT EXISTS invoice_mail_checks (
                job_id TEXT PRIMARY KEY, uuid TEXT NOT NULL, attempts INTEGER NOT NULL, next_check INTEGER NOT NULL)''')

    def start(self):
        self.stop.clear()
        self.task = asyncio.create_task(self.run())

    async def close(self):
        self.stop.set()
        if self.task:
            await self.task

    def claim(self, now):
        with self.store.connect() as db:
            db.execute('BEGIN IMMEDIATE')
            rows = db.execute('''SELECT j.id,j.uuid,j.billing_profile,COALESCE(c.attempts,0) AS attempts
                FROM jobs j LEFT JOIN invoice_mail_checks c ON c.job_id=j.id AND c.uuid=j.uuid
                WHERE j.status='issued_waiting_files' AND j.historical=0 AND j.issuance_seen=1
                  AND j.uuid IS NOT NULL AND j.billing_profile IS NOT NULL AND j.id<>?
                  AND COALESCE(c.next_check,0)<=? AND COALESCE(c.attempts,0)<=?
                ORDER BY j.updated,j.id''', (self.active_job() or '', now, len(self.delays)))
            for row in rows:
                if not UUID_RE.fullmatch(row['uuid']):
                    continue
                attempt = row['attempts']
                delay = self.delays[min(attempt, len(self.delays)-1)]
                db.execute('''INSERT INTO invoice_mail_checks(job_id,uuid,attempts,next_check) VALUES(?,?,?,?)
                    ON CONFLICT(job_id) DO UPDATE SET uuid=excluded.uuid,attempts=excluded.attempts,
                    next_check=excluded.next_check''', (row['id'], row['uuid'], attempt+1, now+delay))
                return dict(row)

    def recover(self, row):
        key = row['id']
        if self.store.get(key)['status'] != 'issued_waiting_files':
            return
        buyer = Profile.model_validate_json(row['billing_profile'])
        try:
            recover_attachments(self.store.root, key, f'has:attachment "{row["uuid"]}"')
            reconcile(self.store, key, buyer)
        except ServiceWait as exc:
            with self.store.connect() as db:
                db.execute('UPDATE invoice_mail_checks SET next_check=MAX(next_check,?) WHERE job_id=? AND uuid=?',
                           (int(time.time())+exc.seconds+1, key, row['uuid']))
            self.store.event(key, 'gmail_recovery', str(exc))
        except ValueError:
            if self.store.get(key)['status'] != 'issued_waiting_files':
                return
            detail = ('La búsqueda automática terminó. Puedes recuperar los archivos desde el expediente.'
                      if row['attempts'] >= len(self.delays) else
                      'Aún falta la pareja PDF/XML del UUID confirmado. Se volverá a consultar Gmail.')
            self.store.event(key, 'gmail_recovery', detail)

    async def run(self):
        while not self.stop.is_set():
            try:
                if self.enabled():
                    row = self.claim(int(time.time()))
                    if row:
                        await asyncio.to_thread(self.recover, row)
            except Exception:
                logging.getLogger(__name__).warning('No se completó el seguimiento de comprobantes por Gmail.')
            try:
                await asyncio.wait_for(self.stop.wait(), timeout=60)
            except asyncio.TimeoutError:
                pass
