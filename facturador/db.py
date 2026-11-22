from __future__ import annotations

import json
import sqlite3
import uuid
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path

from .models import Receipt


def now() -> str:
    return datetime.now(timezone.utc).isoformat()


EDITABLE = {"new", "review", "ready", "blocked"}


class Store:
    def __init__(self, root: Path):
        self.root = root.resolve()
        self.root.mkdir(parents=True, exist_ok=True, mode=0o700)
        self.path = self.root / "facturador.sqlite"
        with self.connect() as db:
            db.executescript("""
            PRAGMA journal_mode=WAL;
            CREATE TABLE IF NOT EXISTS jobs(
              id TEXT PRIMARY KEY, receipt TEXT NOT NULL, status TEXT NOT NULL,
              source_path TEXT, source_hash TEXT UNIQUE, identity TEXT,
              message TEXT NOT NULL DEFAULT '', uuid TEXT, pdf TEXT, xml TEXT,
              created TEXT NOT NULL, updated TEXT NOT NULL, historical INTEGER NOT NULL DEFAULT 0
            );
            CREATE UNIQUE INDEX IF NOT EXISTS one_identity ON jobs(identity) WHERE identity IS NOT NULL;
            CREATE UNIQUE INDEX IF NOT EXISTS one_uuid ON jobs(uuid) WHERE uuid IS NOT NULL;
            CREATE TABLE IF NOT EXISTS events(
              id INTEGER PRIMARY KEY, job_id TEXT, at TEXT NOT NULL, kind TEXT NOT NULL, detail TEXT NOT NULL
            );
            """)
            columns = {row['name'] for row in db.execute('PRAGMA table_info(jobs)')}
            if 'billing_profile' not in columns:
                db.execute('ALTER TABLE jobs ADD COLUMN billing_profile TEXT')
            if 'issue_attempted' not in columns:
                db.execute('ALTER TABLE jobs ADD COLUMN issue_attempted INTEGER NOT NULL DEFAULT 0')
                db.execute("UPDATE jobs SET issue_attempted=1 WHERE status IN ('issuing','uncertain','issued_waiting_files')")
        self.path.chmod(0o600)

    @contextmanager
    def connect(self):
        db = sqlite3.connect(self.path, timeout=20)
        db.row_factory = sqlite3.Row
        try:
            with db:
                yield db
        finally:
            db.close()

    @staticmethod
    def decode(row):
        if row is None:
            raise KeyError("Ticket inexistente")
        result = dict(row)
        result["receipt"] = json.loads(result["receipt"])
        return result

    def get(self, job_id: str):
        with self.connect() as db:
            return self.decode(db.execute("SELECT * FROM jobs WHERE id=?", (job_id,)).fetchone())

    def list(self):
        with self.connect() as db:
            return [self.decode(x) for x in db.execute("SELECT * FROM jobs ORDER BY created DESC")]

    def add(self, path: str, sha: str, source: str = "archivo"):
        with self.connect() as db:
            db.execute('BEGIN IMMEDIATE')
            existing = db.execute("SELECT * FROM jobs WHERE source_hash=?", (sha,)).fetchone()
            if existing:
                return self.decode(existing), False
            key = uuid.uuid4().hex
            receipt = Receipt(source=source).model_dump_json()
            db.execute("INSERT INTO jobs(id,receipt,status,source_path,source_hash,created,updated) VALUES(?,?,?,?,?,?,?)",
                       (key, receipt, "new", path, sha, now(), now()))
        return self.get(key), True

    def event(self, key: str, kind: str, detail: str):
        with self.connect() as db:
            db.execute("INSERT INTO events(job_id,at,kind,detail) VALUES(?,?,?,?)", (key, now(), kind, detail[:5000]))

    def update(self, key: str, **fields):
        allowed = {"status", "message", "uuid", "pdf", "xml", "receipt", "identity"}
        if not fields or set(fields) - allowed:
            raise ValueError("Actualización inválida")
        if "receipt" in fields:
            fields["receipt"] = json.dumps(fields["receipt"], ensure_ascii=False)
        if fields.get("uuid"):
            fields["uuid"] = fields["uuid"].upper()
        fields["updated"] = now()
        with self.connect() as db:
            db.execute("UPDATE jobs SET " + ",".join(f"{x}=?" for x in fields) + " WHERE id=?", (*fields.values(), key))

    def save_receipt(self, key: str, receipt: Receipt, ready: bool = False):
        identity = receipt.identity() if (receipt.ticket_number or receipt.billing_codes) else None
        with self.connect() as db:
            db.execute('BEGIN IMMEDIATE')
            row = self.decode(db.execute('SELECT * FROM jobs WHERE id=?', (key,)).fetchone())
            if row['status'] not in EDITABLE or row['issue_attempted']:
                raise ValueError('Este ticket ya está en proceso o tiene un intento de emisión.')
            status, message = ('ready', 'Revisado') if ready else ('review', 'Revisar datos extraídos')
            for other in db.execute('SELECT id, identity, receipt FROM jobs WHERE id<>?', (key,)):
                old = Receipt.model_validate_json(other['receipt'])
                same_codes = old.provider == receipt.provider and set(old.billing_codes).intersection(receipt.billing_codes)
                if (identity and other['identity'] == identity) or same_codes:
                    status, message, identity = 'duplicate', f"Ya registrado en {other['id']}", None
                    break
            db.execute('UPDATE jobs SET receipt=?, identity=?, status=?, message=?, updated=? WHERE id=?',
                       (receipt.model_dump_json(), identity, status, message, now(), key))
        return self.get(key)

    def claim(self, key: str) -> bool:
        with self.connect() as db:
            count = db.execute("UPDATE jobs SET status='running',updated=? WHERE id=? AND status IN ('ready','queued')", (now(), key)).rowcount
            return count == 1

    def queue(self, key: str, profile=None) -> bool:
        with self.connect() as db:
            return db.execute("UPDATE jobs SET status='queued',message='En cola',updated=?,billing_profile=? WHERE id=? AND status='ready' AND issue_attempted=0",
                              (now(), profile.model_dump_json() if profile else None, key)).rowcount == 1

    def begin_emission(self, key: str, action: str) -> None:
        with self.connect() as db:
            result = db.execute("UPDATE jobs SET issue_attempted=1,status='issuing',message='Emisión enviada a revisión del portal',updated=? WHERE id=? AND issue_attempted=0 AND status='running'",
                                (now(), key))
            if result.rowcount != 1:
                raise ValueError('Recupera el resultado del intento anterior antes de emitir.')
            db.execute('INSERT INTO events(job_id,at,kind,detail) VALUES(?,?,?,?)', (key, now(), 'issue_attempt', action[:250]))

    def recover_after_restart(self):
        with self.connect() as db:
            db.execute("UPDATE jobs SET status='uncertain',message='Proceso interrumpido. Recuperar CFDI antes de intentar emitir otra vez.',updated=? WHERE status IN ('running','issuing','needs_user')", (now(),))
            db.execute("UPDATE jobs SET status='ready',message='Cola recuperada; aún no se había abierto el portal.',updated=? WHERE status='queued'", (now(),))

    def import_history(self, rows: list[dict]):
        with self.connect() as db:
            for row in rows:
                receipt = Receipt(**row["receipt"])
                key = "history_" + row["key"]
                db.execute("INSERT OR IGNORE INTO jobs(id,receipt,status,identity,message,uuid,created,updated,historical) VALUES(?,?,?,?,?,?,?,?,1)",
                           (key, receipt.model_dump_json(), "historical", receipt.identity(), row["note"], row.get("uuid", "").upper() if row.get("uuid") else None, now(), now()))

    def events(self, key: str):
        with self.connect() as db:
            return [dict(x) for x in db.execute("SELECT at,kind,detail FROM events WHERE job_id=? ORDER BY id", (key,))]
