from __future__ import annotations

import json
import hashlib
import sqlite3
import time
import uuid
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path

from .models import Receipt


def now() -> str:
    return datetime.now(timezone.utc).isoformat()


EDITABLE = {"new", "review", "ready", "blocked", "external"}
# Delay after each failed automatic read. The final attempt falls back to review.
READ_DELAYS = (30, 120, 600)
AUTO_ELIGIBLE = """historical=0 AND source_path IS NOT NULL AND billing_profile IS NULL
    AND issue_attempted=0 AND human_exposed=0 AND issuance_seen=0 AND uuid IS NULL
    AND COALESCE(json_extract(receipt,'$.source'),'') NOT LIKE 'Ejemplo:%'"""
ARCHIVED_TOLL = 'Este código de peaje figura en un CFDI guardado. Recupera sus comprobantes para cotejar el ticket.'


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
            CREATE TABLE IF NOT EXISTS invoice_matches(
              job_id TEXT PRIMARY KEY, invoice_uuid TEXT NOT NULL, invoice_total TEXT NOT NULL,
              ticket_total TEXT NOT NULL, match_type TEXT NOT NULL, concept_count INTEGER NOT NULL,
              xml_sha256 TEXT NOT NULL);
            CREATE TABLE IF NOT EXISTS observed_shared_invoices(job_id TEXT PRIMARY KEY, invoice_uuid TEXT NOT NULL);
            CREATE INDEX IF NOT EXISTS invoice_match_uuid ON invoice_matches(invoice_uuid);
            CREATE TABLE IF NOT EXISTS invoice_items(
              invoice_uuid TEXT NOT NULL, concept INTEGER NOT NULL, code TEXT NOT NULL, job_id TEXT,
              PRIMARY KEY(invoice_uuid,concept), UNIQUE(invoice_uuid,code));
            CREATE INDEX IF NOT EXISTS invoice_item_code ON invoice_items(code);
            CREATE UNIQUE INDEX IF NOT EXISTS invoice_item_claim ON invoice_items(code) WHERE job_id IS NOT NULL;
            CREATE TABLE IF NOT EXISTS events(
              id INTEGER PRIMARY KEY, job_id TEXT, at TEXT NOT NULL, kind TEXT NOT NULL, detail TEXT NOT NULL
            );
            CREATE TABLE IF NOT EXISTS recovery_requests(job_id TEXT PRIMARY KEY, requested_at INTEGER NOT NULL);
            CREATE TABLE IF NOT EXISTS recovery_schedule(
              job_id TEXT PRIMARY KEY, attempts INTEGER NOT NULL, next_check INTEGER NOT NULL, paused INTEGER NOT NULL);
            CREATE TABLE IF NOT EXISTS negative_billing_checks(
              job_id TEXT PRIMARY KEY, fingerprint TEXT NOT NULL, checked_at INTEGER NOT NULL, preparing INTEGER NOT NULL);
            """)
            columns = {row['name'] for row in db.execute('PRAGMA table_info(jobs)')}
            if 'billing_profile' not in columns:
                db.execute('ALTER TABLE jobs ADD COLUMN billing_profile TEXT')
            if 'issue_attempted' not in columns:
                db.execute('ALTER TABLE jobs ADD COLUMN issue_attempted INTEGER NOT NULL DEFAULT 0')
                db.execute("UPDATE jobs SET issue_attempted=1 WHERE status IN ('issuing','uncertain','issued_waiting_files')")
            if 'human_exposed' not in columns:
                db.execute('ALTER TABLE jobs ADD COLUMN human_exposed INTEGER NOT NULL DEFAULT 0')
                db.execute("UPDATE jobs SET human_exposed=1 WHERE status='needs_user' AND "
                           "COALESCE((SELECT detail FROM events WHERE job_id=jobs.id AND kind='needs_user' ORDER BY id DESC LIMIT 1),'') NOT IN ('issue','plan')")
            if 'issuance_seen' not in columns:
                db.execute('ALTER TABLE jobs ADD COLUMN issuance_seen INTEGER NOT NULL DEFAULT 0')
                db.execute("UPDATE jobs SET issuance_seen=1 WHERE uuid IS NOT NULL OR status IN ('verified','issued_waiting_files')")
            db.execute("UPDATE jobs SET issue_attempted=1 WHERE issue_attempted=0 AND id IN "
                       "(SELECT job_id FROM events WHERE kind='issue_attempt')")
            if 'auto_stage' not in columns:
                db.execute("ALTER TABLE jobs ADD COLUMN auto_stage TEXT NOT NULL DEFAULT ''")
            if 'read_attempts' not in columns:
                db.execute("ALTER TABLE jobs ADD COLUMN read_attempts INTEGER NOT NULL DEFAULT 0")
            if 'read_after' not in columns:
                db.execute("ALTER TABLE jobs ADD COLUMN read_after REAL NOT NULL DEFAULT 0")
            if 'confirmed_payment' not in columns:
                db.execute("ALTER TABLE jobs ADD COLUMN confirmed_payment TEXT NOT NULL DEFAULT ''")
                db.execute("""UPDATE jobs SET confirmed_payment=COALESCE(json_extract(receipt, '$.payment_code'),'')
                              WHERE (status='ready' AND message='Revisado') OR status='external'""")
            db.execute("CREATE INDEX IF NOT EXISTS jobs_auto_inbox ON jobs(status,auto_stage,created,id) "
                       "WHERE historical=0 AND billing_profile IS NULL")
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
        observed_uuid = result.pop('observed_uuid', None)
        invoice_uuid = result.pop('matched_uuid', None) or observed_uuid
        if invoice_uuid:
            result['uuid'] = invoice_uuid
        return result

    _job_select = """SELECT j.*, m.invoice_uuid AS matched_uuid, s.invoice_uuid AS observed_uuid, m.invoice_total, m.ticket_total,
                     m.match_type, m.concept_count FROM jobs j LEFT JOIN invoice_matches m ON m.job_id=j.id
                     LEFT JOIN observed_shared_invoices s ON s.job_id=j.id"""

    def get(self, job_id: str):
        with self.connect() as db:
            return self.decode(db.execute(self._job_select + " WHERE j.id=?", (job_id,)).fetchone())

    def list(self):
        with self.connect() as db:
            return [self.decode(x) for x in db.execute(self._job_select + " ORDER BY j.created DESC")]

    @staticmethod
    def invoice_candidates(db, receipt):
        if receipt.provider != 'capufe' or not receipt.billing_codes:
            return []
        from .cfdi import normalize_capufe_code
        codes = set()
        for value in receipt.billing_codes:
            try:
                codes.add(normalize_capufe_code(value))
            except ValueError:
                continue
        if not codes:
            return []
        return [r[0] for r in db.execute("""SELECT DISTINCT invoice_uuid FROM invoice_items
                   WHERE code IN (SELECT value FROM json_each(?)) ORDER BY invoice_uuid""", (json.dumps(sorted(codes)),))]

    def hold_archived_ticket(self, db, key):
        row = db.execute("""SELECT receipt,status FROM jobs WHERE id=? AND status IN ('ready','review','blocked','queued','running')
                         AND historical=0 AND json_extract(receipt,'$.provider')='capufe'""", (key,)).fetchone()
        if not row:
            return False
        receipt = Receipt.model_validate_json(row['receipt'])
        if (row['status'] == 'review' and receipt.ready_errors()) or not self.invoice_candidates(db, receipt):
            return False
        db.execute("UPDATE jobs SET status='issued_waiting_files',issuance_seen=1,message=?,updated=? WHERE id=?", (ARCHIVED_TOLL, now(), key))
        return True

    def add(self, path: str, sha: str, source: str = "archivo"):
        with self.connect() as db:
            db.execute('BEGIN IMMEDIATE')
            existing = db.execute(self._job_select + " WHERE j.source_hash=?", (sha,)).fetchone()
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

    def running_message(self, key: str, message: str):
        with self.connect() as db:
            db.execute("UPDATE jobs SET message=?,updated=? WHERE id=? AND status='running'",
                       (message, now(), key))

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
            if fields.get('uuid'):
                db.execute('BEGIN IMMEDIATE')
                row = self.decode(db.execute(self._job_select + ' WHERE j.id=?', (key,)).fetchone())
                if row['uuid'] and row['uuid'] != fields['uuid']:
                    if row['status'] == 'verified' or row['match_type']:
                        raise ValueError('El UUID observado difiere del que ya está cotejado para este ticket.')
                    db.execute('DELETE FROM observed_shared_invoices WHERE job_id=?', (key,))
                owner = db.execute("SELECT id,receipt FROM jobs WHERE uuid=? AND id<>?", (fields['uuid'],key)).fetchone()
                if owner and row['receipt']['provider'] == 'capufe' and json.loads(owner['receipt'])['provider'] == 'capufe':
                    observed, fields['uuid'] = fields['uuid'], None
                    db.execute('INSERT OR IGNORE INTO observed_shared_invoices VALUES(?,?)', (key, observed))
            db.execute("UPDATE jobs SET " + ",".join(f"{x}=?" for x in fields) + " WHERE id=?", (*fields.values(), key))

    def auto_candidates(self, status: str, *, stage: str = '', due_only: bool = False):
        with self.connect() as db:
            row = db.execute(f"SELECT * FROM jobs WHERE {AUTO_ELIGIBLE} AND status=? AND auto_stage=? "
                             "AND (?=0 OR read_after<=?) ORDER BY read_after,created,id LIMIT 1",
                             (status, stage, int(due_only), time.time())).fetchone()
            return self.decode(row) if row else None

    def claim_read(self, key: str, automatic: bool):
        claim = ('reading:auto:' if automatic else 'reading:') + uuid.uuid4().hex
        with self.connect() as db:
            db.execute('BEGIN IMMEDIATE')
            row = self.decode(db.execute('SELECT * FROM jobs WHERE id=?', (key,)).fetchone())
            if row['auto_stage'].startswith('reading:'):
                raise ValueError('Ya hay una lectura en curso para este ticket.')
            if (row['status'] not in EDITABLE or row['historical'] or row['issue_attempted']
                    or row['human_exposed'] or row['issuance_seen'] or row['uuid']):
                raise ValueError('Este expediente ya está en proceso o emitido.')
            if automatic:
                eligible = db.execute(f"SELECT 1 FROM jobs WHERE id=? AND {AUTO_ELIGIBLE} "
                                      "AND ((status='new' AND auto_stage='') OR "
                                      "(status='review' AND (auto_stage='offline' OR "
                                      "(auto_stage='retry_read' AND read_after<=?))))", (key, time.time())).fetchone()
                if not eligible:
                    return None
            if automatic or row['auto_stage'] == 'retry_read':
                row['read_attempts'] += 1
            db.execute('UPDATE jobs SET auto_stage=?,read_attempts=? WHERE id=?', (claim, row['read_attempts'], key))
        return row, claim

    def finish_read(self, key: str, claim: str, status: str, message: str, stage: str = ''):
        with self.connect() as db:
            db.execute("UPDATE jobs SET auto_stage=?,status=?,message=?,updated=?,read_attempts=0,read_after=0 WHERE id=? AND auto_stage=? "
                       "AND status IN ('new','review','ready','external','blocked') AND issue_attempted=0 "
                       "AND human_exposed=0 AND issuance_seen=0 AND uuid IS NULL",
                       (stage, status, message, now(), key, claim))
        return self.get(key)

    def note_review(self, row: dict, message: str, *, keep_ready: bool = False):
        with self.connect() as db:
            db.execute(f"UPDATE jobs SET auto_stage='review',status=?,message=?,updated=? WHERE id=? "
                       f"AND {AUTO_ELIGIBLE} AND auto_stage='' AND status=? AND updated=?",
                       ('ready' if keep_ready else 'review', message, now(), row['id'], row['status'], row['updated']))

    def save_receipt(self, key: str, receipt: Receipt, ready: bool = False, *, claim: str = '', note: str = '',
                     read_stage: str = 'review', read_after: float = 0):
        identity = receipt.identity() if (receipt.ticket_number or receipt.billing_codes) else None
        with self.connect() as db:
            db.execute('BEGIN IMMEDIATE')
            row = self.decode(db.execute('SELECT * FROM jobs WHERE id=?', (key,)).fetchone())
            if claim and claim != row['auto_stage']:
                return row  # A human or newer operation superseded this reading.
            if row['status'] not in EDITABLE or row['issue_attempted'] or row['human_exposed'] or row['issuance_seen'] or row['uuid']:
                raise ValueError('Este ticket ya está en proceso o tiene un intento de emisión.')
            status, message = ('ready', 'Revisado') if ready else ('review', 'Revisar datos extraídos')
            if note:
                message = note
            if ready and receipt.provider == 'solicitud_directa':
                status, message = 'external', 'Solicita el CFDI al comercio y adjunta sus comprobantes aquí.'
            for other in db.execute('SELECT id, identity, receipt FROM jobs WHERE id<>?', (key,)):
                old = Receipt.model_validate_json(other['receipt'])
                same_codes = old.merchant_key() == receipt.merchant_key() and set(old.billing_codes).intersection(receipt.billing_codes)
                if (identity and other['identity'] == identity) or same_codes:
                    status, message, identity = 'duplicate', f"Ya registrado en {other['id']}", None
                    break
            if status in {'ready', 'review'} and not receipt.ready_errors() and self.invoice_candidates(db, receipt):
                status = 'issued_waiting_files'
                message = ARCHIVED_TOLL
            stage = read_stage if status == 'review' and claim else ''
            confirmed_payment = receipt.payment_code if ready and not claim else row['confirmed_payment']
            db.execute('UPDATE jobs SET receipt=?, identity=?, status=?, message=?, auto_stage=?, confirmed_payment=?, '
                       "updated=?,read_attempts=?,read_after=?,issuance_seen=? WHERE id=?",
                       (receipt.model_dump_json(), identity, status, message, stage, confirmed_payment, now(),
                        row['read_attempts'] if stage == 'retry_read' else 0,
                        read_after if stage == 'retry_read' else 0, int(status == 'issued_waiting_files'), key))
        return self.get(key)

    def claim(self, key: str) -> bool:
        with self.connect() as db:
            db.execute('BEGIN IMMEDIATE')
            if self.hold_archived_ticket(db, key):
                return False
            count = db.execute("UPDATE jobs SET status='running',updated=? WHERE id=? AND status IN ('ready','queued') "
                               "AND issue_attempted=0 AND issuance_seen=0 AND uuid IS NULL AND (human_exposed=0 OR EXISTS("
                               "SELECT 1 FROM negative_billing_checks WHERE job_id=jobs.id AND preparing=1 AND checked_at>=unixepoch()-300))",
                               (now(), key)).rowcount
            if not count:
                db.execute("""UPDATE jobs SET status='uncertain',message='Venció la consulta. Busca de nuevo el resultado del peaje.',
                    updated=? WHERE id=? AND status='queued' AND human_exposed=1 AND issue_attempted=0
                    AND issuance_seen=0 AND uuid IS NULL AND EXISTS(SELECT 1 FROM negative_billing_checks
                    WHERE job_id=jobs.id AND preparing=1 AND checked_at<unixepoch()-300)""", (now(), key))
            return count == 1

    @staticmethod
    def billing_fingerprint(receipt, profile):
        scope = json.dumps([receipt.model_dump(mode='json'), profile.model_dump(mode='json', exclude={'whatsapp_phone'})],
                           ensure_ascii=False, sort_keys=True, separators=(',', ':')).encode()
        return hashlib.sha256(scope).hexdigest()

    def record_not_billed(self, key, provider, receipt, profile):
        if provider != 'capufe' or receipt.provider != provider or not receipt.billing_codes:
            return False
        with self.connect() as db:
            db.execute('BEGIN IMMEDIATE')
            row = self.decode(db.execute('SELECT * FROM jobs WHERE id=?', (key,)).fetchone())
            if not self._eligible_negative(row, receipt, profile):
                return False
            db.execute("""INSERT INTO negative_billing_checks VALUES(?,?,unixepoch(),0)
                ON CONFLICT(job_id) DO UPDATE SET fingerprint=excluded.fingerprint,
                checked_at=excluded.checked_at, preparing=0""", (key, self.billing_fingerprint(receipt, profile)))
            db.execute('INSERT INTO events(job_id,at,kind,detail) VALUES(?,?,?,?)',
                       (key, now(), 'not_billed_confirmed', 'CAPUFE confirmó que todos los códigos del expediente siguen sin facturar.'))
            return True

    @staticmethod
    def _eligible_negative(row, receipt, profile):
        from .models import Profile
        return (Receipt.model_validate(row['receipt']) == receipt and row['billing_profile']
                and Profile.model_validate_json(row['billing_profile']).model_dump(exclude={'whatsapp_phone'})
                == profile.model_dump(exclude={'whatsapp_phone'})
                and row['human_exposed'] and not (row['issue_attempted'] or row['issuance_seen'] or row['uuid'] or row['historical'])
                and row['status'] in {'needs_user', 'uncertain', 'blocked'})

    def queue_after_negative_check(self, key, receipt, profile):
        with self.connect() as db:
            db.execute('BEGIN IMMEDIATE')
            row = self.decode(db.execute('SELECT * FROM jobs WHERE id=?', (key,)).fetchone())
            if not self._eligible_negative(row, receipt, profile):
                return False
            count = db.execute("""UPDATE negative_billing_checks SET preparing=1 WHERE job_id=? AND preparing=0
                AND checked_at>=unixepoch()-300 AND fingerprint=?""", (key, self.billing_fingerprint(receipt, profile))).rowcount
            if not count:
                return False
            db.execute("UPDATE jobs SET status='queued',auto_stage='scheduled',message=?,updated=? WHERE id=?",
                       ('CAPUFE confirmó que sigue sin facturar. Preparando el ticket para tu aprobación.', now(), key))
            return True

    def queue(self, key: str, profile=None, confirmed_url: str = '') -> bool:
        with self.connect() as db:
            db.execute('BEGIN IMMEDIATE')
            if self.hold_archived_ticket(db, key):
                return False
            return db.execute("UPDATE jobs SET status='queued',auto_stage='scheduled',message='En cola',updated=?,billing_profile=? WHERE id=? "
                              "AND (status='ready' OR (status='blocked' AND billing_profile IS NOT NULL)) "
                              "AND auto_stage NOT LIKE 'reading:%' "
                              "AND issue_attempted=0 AND human_exposed=0 AND issuance_seen=0 AND uuid IS NULL "
                              "AND COALESCE(json_extract(receipt,'$.portal_url'),'')=?",
                              (now(), profile.model_dump_json() if profile else None, key, confirmed_url)).rowcount == 1

    def confirm_issued(self, key: str):
        with self.connect() as db:
            db.execute("UPDATE jobs SET issuance_seen=1,status='issued_waiting_files',message='Emisión confirmada. Recuperando PDF y XML.',updated=? "
                       "WHERE id=? AND status NOT IN ('verified','duplicate','historical')", (now(), key))

    def pause_automatic_recovery(self, key, paused=True):
        with self.connect() as db:
            if paused:
                db.execute('''INSERT INTO recovery_schedule VALUES(?,0,0,1)
                    ON CONFLICT(job_id) DO UPDATE SET paused=1''', (key,))
            else:
                db.execute('DELETE FROM recovery_schedule WHERE job_id=? AND paused=1', (key,))

    def next_recovery(self, current_time):
        # A confirmed UUID already has its own Gmail follow-up. Reserve each remaining
        # check before opening the portal so a restart cannot create a polling loop.
        delays = (90, 600, 3600)
        with self.connect() as db:
            db.execute('BEGIN IMMEDIATE')
            row = db.execute('''SELECT j.id,COALESCE(s.attempts,0) AS attempts FROM jobs j
                LEFT JOIN recovery_schedule s ON s.job_id=j.id LEFT JOIN recovery_requests r ON r.job_id=j.id
                WHERE j.status IN ('uncertain','issued_waiting_files') AND j.historical=0
                  AND j.billing_profile IS NOT NULL AND COALESCE(j.uuid,'')=''
                  AND COALESCE(s.paused,0)=0 AND COALESCE(s.attempts,0)<? AND COALESCE(s.next_check,0)<=?
                  AND COALESCE(r.requested_at,0)<=? AND COALESCE(json_extract(j.receipt,'$.source'),'') NOT LIKE 'Ejemplo:%'
                ORDER BY s.next_check,j.updated,j.id LIMIT 1''', (len(delays)+1, current_time, current_time-30)).fetchone()
            if row is None:
                return None
            attempts = row['attempts']
            after = current_time + delays[min(attempts, len(delays)-1)]
            db.execute('''INSERT INTO recovery_schedule VALUES(?,?,?,0) ON CONFLICT(job_id)
                DO UPDATE SET attempts=excluded.attempts,next_check=excluded.next_check''',
                (row['id'], attempts+1, after))
            return row['id']

    def defer_recovery_until(self, key, when):
        with self.connect() as db:
            db.execute('''INSERT INTO recovery_schedule VALUES(?,0,?,0) ON CONFLICT(job_id)
                DO UPDATE SET next_check=MAX(next_check,excluded.next_check)''', (key, when))

    def claim_recovery(self, key: str) -> bool:
        with self.connect() as db:
            changed = db.execute("""INSERT INTO recovery_requests(job_id,requested_at) VALUES(?,unixepoch())
                ON CONFLICT(job_id) DO UPDATE SET requested_at=excluded.requested_at
                WHERE requested_at<=excluded.requested_at-30""", (key,)).rowcount == 1
            if changed:
                db.execute("DELETE FROM negative_billing_checks WHERE job_id=?", (key,))
            return changed

    def checked_preparation(self, key):
        with self.connect() as db:
            return bool(db.execute("SELECT 1 FROM negative_billing_checks WHERE job_id=? AND preparing=1", (key,)).fetchone())

    def finish_recovery(self, key: str, message: str):
        with self.connect() as db:
            db.execute("""UPDATE jobs SET status=CASE WHEN issuance_seen=1 OR uuid IS NOT NULL
                THEN 'issued_waiting_files' ELSE 'uncertain' END, message=?,updated=? WHERE id=?
                AND status NOT IN ('verified','duplicate','historical')""", (message, now(), key))

    def begin_emission(self, key: str, action: str) -> None:
        with self.connect() as db:
            db.execute('BEGIN IMMEDIATE')
            expired = db.execute("""SELECT 1 FROM negative_billing_checks WHERE job_id=? AND preparing=1
                AND checked_at<unixepoch()-300""", (key,)).fetchone()
            if expired:
                raise ValueError('Venció la consulta de CAPUFE. Vuelve a buscar comprobantes para preparar una nueva aprobación.')
            result = db.execute("UPDATE jobs SET issue_attempted=1,status='issuing',message='Emisión enviada a revisión del portal',updated=? WHERE id=? AND issue_attempted=0 AND issuance_seen=0 AND uuid IS NULL AND status='running'",
                                (now(), key))
            if result.rowcount != 1:
                raise ValueError('Recupera el resultado del intento anterior antes de emitir.')
            db.execute('INSERT INTO events(job_id,at,kind,detail) VALUES(?,?,?,?)', (key, now(), 'issue_attempt', action[:250]))

    def enter_intervention(self, key: str, kind: str, message: str, manual: bool):
        with self.connect() as db:
            db.execute("UPDATE jobs SET status='needs_user',human_exposed=MAX(human_exposed,?),message=?,updated=? WHERE id=?",
                       (int(manual), message, now(), key))
            db.execute('INSERT INTO events(job_id,at,kind,detail) VALUES(?,?,?,?)', (key, now(), 'needs_user', kind))

    def resume_intervention(self, key: str):
        with self.connect() as db:
            db.execute("UPDATE jobs SET status='running',message='Continuando en el portal…',updated=? WHERE id=? AND status='needs_user'",
                       (now(), key))

    def recover_after_restart(self):
        with self.connect() as db:
            db.execute(f"UPDATE jobs SET status='review',auto_stage='retry_read',read_after=?,message=?,updated=? "
                       f"WHERE {AUTO_ELIGIBLE} AND auto_stage LIKE 'reading:auto:%' AND read_attempts<=? "
                       "AND status IN ('new','review')",
                       (time.time() + READ_DELAYS[0], 'Se interrumpió la lectura. El reintento automático está programado.',
                        now(), len(READ_DELAYS)))
            db.execute("UPDATE jobs SET status='review',auto_stage='review',message=?,updated=? "
                       "WHERE auto_stage LIKE 'reading:%' AND status IN ('new','review','ready','external','blocked') "
                       "AND issue_attempted=0 AND human_exposed=0 AND issuance_seen=0 AND uuid IS NULL",
                       ('La lectura se interrumpió. Revisa los datos o pulsa Leer con mi plan para repetirla.', now()))
            db.execute("UPDATE jobs SET status='issued_waiting_files',message='El portal confirmó la emisión. Recupera el PDF y el XML.',updated=? "
                       "WHERE status IN ('queued','running','issuing','needs_user') AND (issuance_seen=1 OR uuid IS NOT NULL)", (now(),))
            db.execute("UPDATE jobs SET status='uncertain',issue_attempted=MAX(issue_attempted,status='issuing'),"
                       "message='El resultado del portal sigue pendiente. Puedes buscar los comprobantes del ticket.',updated=? "
                       "WHERE status IN ('queued','running','issuing','needs_user') AND "
                       "(issue_attempted=1 OR uuid IS NOT NULL OR status='issuing' OR human_exposed=1)", (now(),))
            db.execute("UPDATE jobs SET status='ready',message='Trámite interrumpido antes de enviar. Puedes prepararlo de nuevo.',updated=? "
                       "WHERE status IN ('queued','running','needs_user') AND issue_attempted=0 AND uuid IS NULL", (now(),))

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
