"""Accounts, sessions and one-use invitations of the entrance for several customers.

Passwords are hashed with scrypt and a salt per account. A session is a random 256-bit token of
which only a digest is stored. Nothing here knows about tickets or reads a customer's data.
"""
from __future__ import annotations

import hashlib
import hmac
import math
import re
import secrets
import sqlite3
import time
import unicodedata
from contextlib import contextmanager
from pathlib import Path

SCRYPT = {'n': 2 ** 17, 'r': 8, 'p': 1}
MIN_PASSWORD = 12
MAX_PASSWORD = 256
IDLE_SECONDS = 12 * 3600
LIFETIME_SECONDS = 7 * 86400
INVITATION_HOURS = 72
MAX_INVITATION_HOURS = 14 * 24
MAX_SESSIONS = 20
SEEN_STEP = 60
NAME = re.compile(r'[a-z0-9][a-z0-9._@+-]{1,62}[a-z0-9]')
ROLES = ('admin', 'usuario')
# Failures that cost nothing, and the seconds one failure takes to be forgotten.
ALLOWANCE = {'usuario': (5, 15 * 60), 'direccion': (20, 60)}
FIRST_WAIT = 5
LONGEST_WAIT = 15 * 60

WRONG = 'Usuario o contraseña incorrectos.'
INVITATION = 'Esta invitación ya no es válida. Pide una nueva a quien administra el servicio.'


class AccessError(ValueError):
    """A refusal the person can read; ``status`` is the HTTP code that goes with it."""
    status = 422


class BadCredentials(AccessError):
    status = 401

    def __init__(self):
        super().__init__(WRONG)


class Throttled(AccessError):
    status = 429

    def __init__(self, seconds: float):
        self.retry_after = max(1, math.ceil(seconds))
        wait = (f'{self.retry_after} segundos' if self.retry_after < 90
                else f'{math.ceil(self.retry_after / 60)} minutos')
        super().__init__(f'Demasiados intentos. Espera {wait} y vuelve a intentar.')


class InvalidInvitation(AccessError):
    status = 404

    def __init__(self):
        super().__init__(INVITATION)


class Forbidden(AccessError):
    status = 403


def clean_name(value) -> str:
    """The canonical form of a user name, or '' when it cannot be one."""
    if not isinstance(value, str):
        return ''
    name = unicodedata.normalize('NFKC', value).strip().casefold()
    return name if NAME.fullmatch(name) else ''


def valid_name(value) -> str:
    name = clean_name(value)
    if not name:
        raise AccessError('El usuario debe tener de 3 a 64 letras o números. También admite punto, guion, @ y +.')
    return name


def valid_password(value, name: str = '') -> str:
    if not isinstance(value, str):
        raise AccessError('Escribe una contraseña.')
    password = unicodedata.normalize('NFKC', value)
    if len(password) < MIN_PASSWORD:
        raise AccessError(f'La contraseña debe tener al menos {MIN_PASSWORD} caracteres.')
    if len(password) > MAX_PASSWORD:
        raise AccessError(f'La contraseña admite hasta {MAX_PASSWORD} caracteres.')
    if name and password.casefold() == name:
        raise AccessError('La contraseña no puede ser igual al usuario.')
    return password


def token_digest(token: str) -> bytes:
    return hashlib.sha256(token.encode('ascii', 'replace')).digest()


class Accounts:
    def __init__(self, folder: Path, *, pepper: bytes, clock=time.time, cost: dict | None = None):
        self.folder = Path(folder)
        self.folder.mkdir(parents=True, exist_ok=True, mode=0o700)
        self.path = self.folder / 'acceso.sqlite'
        self.pepper = pepper
        self.clock = clock
        self.cost = dict(cost or SCRYPT)
        # Unknown names are checked against this salt so that every attempt costs one hash.
        self._decoy_salt = secrets.token_bytes(16)
        self._decoy_hash = secrets.token_bytes(32)
        with self.connect() as db:
            db.executescript("""
            PRAGMA journal_mode=WAL;
            CREATE TABLE IF NOT EXISTS usuarios(
              id TEXT PRIMARY KEY, nombre TEXT NOT NULL UNIQUE, rol TEXT NOT NULL,
              sal BLOB NOT NULL, clave BLOB NOT NULL, costo TEXT NOT NULL,
              activo INTEGER NOT NULL DEFAULT 1, creado REAL NOT NULL, ultimo_acceso REAL);
            CREATE TABLE IF NOT EXISTS sesiones(
              huella BLOB PRIMARY KEY, usuario TEXT NOT NULL, creada REAL NOT NULL, vista REAL NOT NULL);
            CREATE INDEX IF NOT EXISTS sesiones_usuario ON sesiones(usuario);
            CREATE TABLE IF NOT EXISTS invitaciones(
              id TEXT PRIMARY KEY, huella BLOB NOT NULL UNIQUE, nombre TEXT NOT NULL DEFAULT '',
              creador TEXT NOT NULL, creada REAL NOT NULL, vence REAL NOT NULL, usada REAL, usuario TEXT);
            CREATE TABLE IF NOT EXISTS intentos(
              clave TEXT PRIMARY KEY, fallos REAL NOT NULL, actualizado REAL NOT NULL, hasta REAL NOT NULL);
            """)
        try:
            self.path.chmod(0o600)
        except OSError:
            pass

    @contextmanager
    def connect(self):
        db = sqlite3.connect(self.path, timeout=20)
        db.row_factory = sqlite3.Row
        try:
            with db:
                yield db
        finally:
            db.close()

    # ------------------------------------------------------------ passwords

    def _hash(self, password: str, salt: bytes, cost: dict) -> bytes:
        # The pepper comes from the master key, which is not stored with the data.
        secret = hmac.new(self.pepper, unicodedata.normalize('NFKC', password).encode('utf-8'), 'sha256').digest()
        return hashlib.scrypt(secret, salt=salt, n=cost['n'], r=cost['r'], p=cost['p'], dklen=32,
                              maxmem=max(2 ** 25, 256 * cost['n'] * cost['r'] * cost['p']))

    def _new_hash(self, password: str) -> tuple[bytes, bytes, str]:
        salt = secrets.token_bytes(16)
        return salt, self._hash(password, salt, self.cost), self._cost_text(self.cost)

    @staticmethod
    def _cost_text(cost: dict) -> str:
        return f"{cost['n']}:{cost['r']}:{cost['p']}"

    @staticmethod
    def _cost_of(row) -> dict:
        n, r, p = (int(part) for part in row['costo'].split(':'))
        return {'n': n, 'r': r, 'p': p}

    # ------------------------------------------------------------ attempts

    def _key(self, kind: str, value: str) -> str:
        # Typed names may hold a password written in the wrong box: only a keyed digest is kept.
        return kind + ':' + hmac.new(self.pepper, (kind + '\0' + value).encode('utf-8', 'replace'), 'sha256').hexdigest()[:40]

    def _keys(self, name: str, address: str) -> list[tuple[str, str]]:
        return [('usuario', self._key('usuario', name)), ('direccion', self._key('direccion', address or '?'))]

    @staticmethod
    def _wait(db, keys, now: float) -> float:
        marks = ','.join('?' * len(keys))
        row = db.execute(f'SELECT MAX(hasta) AS hasta FROM intentos WHERE clave IN ({marks})',
                         [key for _, key in keys]).fetchone()
        return max(0.0, (row['hasta'] or 0) - now)

    @staticmethod
    def _fail(db, keys, now: float) -> None:
        for kind, key in keys:
            free, forget = ALLOWANCE[kind]
            row = db.execute('SELECT fallos,actualizado FROM intentos WHERE clave=?', (key,)).fetchone()
            failures = max(0.0, row['fallos'] - (now - row['actualizado']) / forget) + 1 if row else 1.0
            failures = min(failures, free + 12)
            # A failure counts whole until it is entirely forgotten.
            extra = math.ceil(failures - 1e-6) - free
            until = now + min(LONGEST_WAIT, FIRST_WAIT * 2 ** extra) if extra >= 0 else 0
            db.execute('''INSERT INTO intentos(clave,fallos,actualizado,hasta) VALUES(?,?,?,?)
                ON CONFLICT(clave) DO UPDATE SET fallos=excluded.fallos,actualizado=excluded.actualizado,
                hasta=excluded.hasta''', (key, failures, now, until))

    def _check(self, name: str, password, address: str) -> sqlite3.Row:
        """The account behind a correct name and password. One hash is computed in every case."""
        name = clean_name(name)
        password = password if isinstance(password, str) else ''
        keys = self._keys(name or '?', address)
        with self.connect() as db:
            wait = self._wait(db, keys, self.clock())
            row = db.execute('SELECT * FROM usuarios WHERE nombre=?', (name,)).fetchone() if name else None
        if wait:
            raise Throttled(wait)
        usable = row is not None and bool(row['activo'])
        digest = self._hash(password, row['sal'] if usable else self._decoy_salt,
                            self._cost_of(row) if usable else self.cost)
        correct = hmac.compare_digest(digest, row['clave'] if usable else self._decoy_hash) and usable
        now = self.clock()
        with self.connect() as db:
            db.execute('BEGIN IMMEDIATE')
            # Guesses sent at the same time are stopped here, once the first failures are counted.
            wait = self._wait(db, keys, now)
            if not wait and not correct:
                self._fail(db, keys, now)
            elif not wait:
                db.execute('DELETE FROM intentos WHERE clave=?', (keys[0][1],))
        if wait:
            raise Throttled(wait)
        if not correct:
            raise BadCredentials()
        return row

    @staticmethod
    def _public(row) -> dict:
        return {'id': row['id'], 'nombre': row['nombre'], 'rol': row['rol'], 'activo': bool(row['activo']),
                'creado': row['creado'], 'ultimo_acceso': row['ultimo_acceso']}

    # ------------------------------------------------------------ accounts

    def create_user(self, name: str, password: str, *, role: str = 'usuario') -> dict:
        name = valid_name(name)
        password = valid_password(password, name)
        if role not in ROLES:
            raise AccessError('Tipo de cuenta desconocido.')
        salt, digest, cost = self._new_hash(password)
        key = secrets.token_hex(8)
        try:
            with self.connect() as db:
                db.execute('INSERT INTO usuarios(id,nombre,rol,sal,clave,costo,creado) VALUES(?,?,?,?,?,?,?)',
                           (key, name, role, salt, digest, cost, self.clock()))
        except sqlite3.IntegrityError:
            raise AccessError('Ese usuario ya existe. Elige otro.') from None
        return self.user(key)

    def user(self, key: str) -> dict:
        with self.connect() as db:
            row = db.execute('SELECT * FROM usuarios WHERE id=?', (key,)).fetchone()
        if row is None:
            raise AccessError('Esa cuenta no existe.')
        return self._public(row)

    def users(self) -> list[dict]:
        with self.connect() as db:
            return [self._public(row) for row in db.execute('SELECT * FROM usuarios ORDER BY creado,id')]

    def authenticate(self, name: str, password: str, address: str) -> dict:
        row = self._check(name, password, address)
        fresh = None
        if self._cost_of(row) != self.cost:
            fresh = self._new_hash(password)  # Stored with older parameters: bring it up to date.
        with self.connect() as db:
            db.execute('UPDATE usuarios SET ultimo_acceso=? WHERE id=?', (self.clock(), row['id']))
            if fresh:
                db.execute('UPDATE usuarios SET sal=?,clave=?,costo=? WHERE id=? AND clave=?', (*fresh, row['id'], row['clave']))
        return self.user(row['id'])

    def change_password(self, key: str, current: str, new: str, address: str) -> str:
        """Replace the password after checking the current one. Every session ends; a new token is returned."""
        account = self.user(key)
        new = valid_password(new, account['nombre'])
        self._check(account['nombre'], current, address)
        if unicodedata.normalize('NFKC', current) == new:
            raise AccessError('La contraseña nueva debe ser distinta de la actual.')
        salt, digest, cost = self._new_hash(new)
        with self.connect() as db:
            db.execute('UPDATE usuarios SET sal=?,clave=?,costo=? WHERE id=?', (salt, digest, cost, key))
            db.execute('DELETE FROM sesiones WHERE usuario=?', (key,))
        return self.open_session(key)

    def set_active(self, key: str, active: bool, *, by: str = '') -> dict:
        """Turn an account on or off. Turning it off ends its sessions; its data is not touched."""
        with self.connect() as db:
            db.execute('BEGIN IMMEDIATE')
            row = db.execute('SELECT * FROM usuarios WHERE id=?', (key,)).fetchone()
            if row is None:
                raise AccessError('Esa cuenta no existe.')
            if not active:
                if key == by:
                    raise AccessError('No puedes desactivar tu propia cuenta.')
                others = db.execute("SELECT COUNT(*) FROM usuarios WHERE rol='admin' AND activo=1 AND id<>?", (key,)).fetchone()[0]
                if row['rol'] == 'admin' and not others:
                    raise AccessError('Debe quedar al menos un administrador activo.')
                db.execute('DELETE FROM sesiones WHERE usuario=?', (key,))
            db.execute('UPDATE usuarios SET activo=? WHERE id=?', (int(bool(active)), key))
        return self.user(key)

    # ------------------------------------------------------------ sessions

    def open_session(self, key: str) -> str:
        token = secrets.token_urlsafe(32)
        now = self.clock()
        with self.connect() as db:
            db.execute('INSERT INTO sesiones(huella,usuario,creada,vista) VALUES(?,?,?,?)', (token_digest(token), key, now, now))
            db.execute('''DELETE FROM sesiones WHERE usuario=? AND huella NOT IN
                (SELECT huella FROM sesiones WHERE usuario=? ORDER BY creada DESC LIMIT ?)''', (key, key, MAX_SESSIONS))
        return token

    def session(self, token: str) -> dict | None:
        """The account and times of a live session, or None. Using it counts as activity."""
        if not token:
            return None
        digest, now = token_digest(token), self.clock()
        with self.connect() as db:
            row = db.execute('''SELECT s.creada,s.vista,u.id,u.nombre,u.rol,u.activo FROM sesiones s
                JOIN usuarios u ON u.id=s.usuario WHERE s.huella=?''', (digest,)).fetchone()
            if row is None:
                return None
            if not row['activo'] or now - row['creada'] > LIFETIME_SECONDS or now - row['vista'] > IDLE_SECONDS:
                db.execute('DELETE FROM sesiones WHERE huella=?', (digest,))
                return None
            if now - row['vista'] >= SEEN_STEP:
                db.execute('UPDATE sesiones SET vista=? WHERE huella=?', (now, digest))
        return {'usuario': row['id'], 'nombre': row['nombre'], 'rol': row['rol'], 'creada': row['creada']}

    def close_session(self, token: str) -> None:
        if token:
            with self.connect() as db:
                db.execute('DELETE FROM sesiones WHERE huella=?', (token_digest(token),))

    def close_other_sessions(self, key: str, keep: str) -> int:
        with self.connect() as db:
            return db.execute('DELETE FROM sesiones WHERE usuario=? AND huella<>?', (key, token_digest(keep))).rowcount

    def session_count(self, key: str) -> int:
        now = self.clock()
        with self.connect() as db:
            return db.execute('SELECT COUNT(*) FROM sesiones WHERE usuario=? AND creada>=? AND vista>=?',
                              (key, now - LIFETIME_SECONDS, now - IDLE_SECONDS)).fetchone()[0]

    # ------------------------------------------------------------ invitations

    def create_invitation(self, creator: str, *, name: str = '', hours: float = INVITATION_HOURS) -> dict:
        """A code that creates one account, once, before it expires. The code is returned only here."""
        if isinstance(hours, bool) or not isinstance(hours, (int, float)) or not 1 <= hours <= MAX_INVITATION_HOURS:
            raise AccessError(f'La invitación puede durar de 1 a {MAX_INVITATION_HOURS} horas.')
        reserved = valid_name(name) if name else ''
        code, key, now = secrets.token_urlsafe(32), secrets.token_hex(8), self.clock()
        with self.connect() as db:
            if reserved and db.execute('SELECT 1 FROM usuarios WHERE nombre=?', (reserved,)).fetchone():
                raise AccessError('Ese usuario ya existe. Elige otro.')
            db.execute('INSERT INTO invitaciones(id,huella,nombre,creador,creada,vence) VALUES(?,?,?,?,?,?)',
                       (key, token_digest(code), reserved, creator, now, now + hours * 3600))
        return {'id': key, 'codigo': code, 'nombre': reserved, 'vence': now + hours * 3600}

    def _open_invitation(self, db, code) -> sqlite3.Row:
        row = None
        if isinstance(code, str) and 20 <= len(code) <= 128:
            row = db.execute('SELECT * FROM invitaciones WHERE huella=?', (token_digest(code),)).fetchone()
        if row is None or row['usada'] is not None or row['vence'] <= self.clock():
            raise InvalidInvitation()
        return row

    def invitation(self, code: str) -> dict:
        with self.connect() as db:
            row = self._open_invitation(db, code)
        return {'nombre': row['nombre'], 'vence': row['vence']}

    def invitations(self) -> list[dict]:
        """Invitations that can still be used. Their codes are not stored, so they are never listed."""
        with self.connect() as db:
            return [{'id': row['id'], 'nombre': row['nombre'], 'creada': row['creada'], 'vence': row['vence']}
                    for row in db.execute('SELECT * FROM invitaciones WHERE usada IS NULL AND vence>? ORDER BY creada',
                                          (self.clock(),))]

    def revoke_invitation(self, key: str) -> bool:
        with self.connect() as db:
            return db.execute('DELETE FROM invitaciones WHERE id=? AND usada IS NULL', (key,)).rowcount == 1

    def accept_invitation(self, code: str, name: str, password: str) -> dict:
        with self.connect() as db:
            reserved = self._open_invitation(db, code)['nombre']
        name = reserved or valid_name(name)
        password = valid_password(password, name)
        salt, digest, cost = self._new_hash(password)  # Slow on purpose: kept out of the write lock.
        key = secrets.token_hex(8)
        with self.connect() as db:
            db.execute('BEGIN IMMEDIATE')
            invitation = self._open_invitation(db, code)
            if db.execute('SELECT 1 FROM usuarios WHERE nombre=?', (name,)).fetchone():
                raise AccessError('Ese usuario ya existe. Elige otro.')
            now = self.clock()
            db.execute('INSERT INTO usuarios(id,nombre,rol,sal,clave,costo,creado) VALUES(?,?,?,?,?,?,?)',
                       (key, name, 'usuario', salt, digest, cost, now))
            db.execute('UPDATE invitaciones SET usada=?,usuario=? WHERE id=?', (now, key, invitation['id']))
        return self.user(key)

    # ------------------------------------------------------------ upkeep

    def note_failure(self, address: str) -> None:
        """Count a wrong invitation code against the address that sent it."""
        keys = self._keys('?', address)[1:]
        with self.connect() as db:
            db.execute('BEGIN IMMEDIATE')
            self._fail(db, keys, self.clock())

    def blocked(self, address: str) -> float:
        with self.connect() as db:
            return self._wait(db, self._keys('?', address)[1:], self.clock())

    def purge(self) -> None:
        now = self.clock()
        with self.connect() as db:
            db.execute('DELETE FROM sesiones WHERE creada<? OR vista<?', (now - LIFETIME_SECONDS, now - IDLE_SECONDS))
            db.execute('DELETE FROM intentos WHERE hasta<? AND actualizado<?', (now, now - 86400))
            db.execute('DELETE FROM invitaciones WHERE vence<?', (now - 30 * 86400,))
