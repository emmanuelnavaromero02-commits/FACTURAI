"""Photos and PDF the owner sends to their own WhatsApp chat become tickets.

Only with the standing permission `leer_mis_fotos_de_whatsapp` and the owner's own
number from the profile. Only that self chat is opened; no other chat is read.
"""
from __future__ import annotations

import asyncio
import re
import time
from pathlib import Path

from . import whatsapp_selectores as sel
from .db import now
from .ingest import IMAGE_EXT, MAX_UPLOAD, save_upload
from .permisos import permitido
from .whatsapp_selectores import SinVincular

MEDIA = IMAGE_EXT | {'.pdf'}
BASELINE = '__baseline__'


def crear_tabla(db):
    db.execute("""CREATE TABLE IF NOT EXISTS whatsapp_own_media (
        item TEXT PRIMARY KEY, at TEXT NOT NULL, state TEXT NOT NULL, job_ids TEXT NOT NULL DEFAULT '')""")


class MisFotos:
    def __init__(self, store, web, profile, busy, *, interval=30, clock=time.monotonic):
        self.store = store
        self.web = web
        self.profile = profile
        self.busy = busy
        self.interval = interval
        self.clock = clock
        self.last = None
        self.pause_until = 0.0
        self.message = ''
        with store.connect() as db:
            crear_tabla(db)

    def own_phone(self) -> str:
        try:
            phone = getattr(self.profile(), 'whatsapp_phone', '') or ''
        except (ValueError, OSError):
            return ''
        return re.sub(r'\D', '', phone)

    def seen(self) -> set[str]:
        with self.store.connect() as db:
            return {row['item'] for row in db.execute('SELECT item FROM whatsapp_own_media')}

    def mark(self, item, state, job_ids=()):
        with self.store.connect() as db:
            db.execute('INSERT OR IGNORE INTO whatsapp_own_media(item,at,state,job_ids) VALUES(?,?,?,?)',
                       (item, now(), state, ','.join(job_ids)))

    def status(self):
        return dict(enabled=permitido(self.store.root, 'leer_mis_fotos_de_whatsapp') and bool(self.own_phone()),
                    message=self.message)

    def due(self) -> bool:
        """Cheap check, so the shared WhatsApp lock is only taken when a scan may run."""
        moment = self.clock()
        if (self.last is not None and moment - self.last < self.interval) or moment < self.pause_until:
            return False
        return (permitido(self.store.root, 'leer_mis_fotos_de_whatsapp') and bool(self.own_phone())
                and self.web.runner.context is not None)

    async def tick(self, *, force=False):
        moment = self.clock()
        if not force and ((self.last is not None and moment - self.last < self.interval) or moment < self.pause_until):
            return []
        if not permitido(self.store.root, 'leer_mis_fotos_de_whatsapp'):
            return []
        phone = self.own_phone()
        if not phone or self.busy() or self.web.runner.context is None:
            return []
        self.last = moment
        seen = self.seen()
        baseline = BASELINE not in seen
        imported = []

        def keep(item, name, data):
            # Runs in a worker thread: validation and storage of one photo or PDF.
            if Path(name).suffix.lower() not in MEDIA or len(data) > MAX_UPLOAD:
                self.mark(item, 'rejected')
                return
            try:
                rows = save_upload(self.store, name, data, 'WhatsApp')
            except ValueError:
                self.mark(item, 'rejected')
                return
            self.mark(item, 'imported', [row['id'] for row in rows])
            imported.extend(rows)

        try:
            await self.web.own_media(phone, seen, keep, baseline=baseline,
                                     remember=lambda item: self.mark(item, 'baseline'))
        except SinVincular as exc:
            self.message = str(exc)
            self.pause_until = moment + 300
            return imported
        except ValueError as exc:
            self.message = str(exc)
            self.pause_until = moment + 120
            return imported
        if baseline:
            self.mark(BASELINE, 'baseline')
        self.message = f'Importé {len(imported)} ticket(s) de tu chat de WhatsApp.' if imported else ''
        return imported


async def media_items(page, seen, keep, *, baseline, remember, limit=10):
    """Download the photos and PDF of the open self chat that were not imported yet."""
    count = 0
    names: dict[str, int] = {}
    for bubble in await sel.burbujas(page):
        # Without a WhatsApp id, the text plus its repetition number stays stable between scans.
        names[bubble.texto] = names.get(bubble.texto, 0) + 1
        item = bubble.ident or f'{bubble.texto}\0{names[bubble.texto]}'
        has_photo = await sel.buscar(bubble.locator, 'imagen') is not None
        pdfs = [n for n in bubble.archivos if n.lower().endswith('.pdf')]
        if not (has_photo or pdfs) or item in seen:
            continue
        if baseline:
            remember(item)
            continue
        if has_photo:
            download = await sel.descargar_foto(page, bubble)
        else:
            control = await sel.buscar(bubble.locator, 'descargar')
            download = await sel.descargar(page, control) if control is not None else None
        if download is None:
            continue
        path = await download.path()
        data = path.read_bytes() if path and path.stat().st_size <= MAX_UPLOAD else b''
        await asyncio.to_thread(keep, item, Path(download.suggested_filename).name or 'foto.jpg', data)
        count += 1
        if count >= limit:
            break
    return count
