"""Secrets at rest sealed with a master key that is kept away from the data folder.

Without a master key every helper behaves exactly like ``read_json``/``write_private``, so the
local single-person mode needs nothing new. With one, vault keys and token files are stored as
authenticated envelopes, each under its own key derived for the owner of the folder and the purpose.
A missing or different master key is an error: nothing sealed is ever replaced or removed.
"""
from __future__ import annotations

import base64
import binascii
import json
import logging
import os
import secrets
import threading
from pathlib import Path

from cryptography.exceptions import InvalidTag
from cryptography.hazmat.primitives import hashes
from cryptography.hazmat.primitives.ciphers.aead import AESGCM
from cryptography.hazmat.primitives.kdf.hkdf import HKDF

log = logging.getLogger(__name__)

BASE = Path(__file__).resolve().parent.parent
KEY_VARIABLE = 'FACTURADOR_LLAVE_MAESTRA'
FILE_VARIABLE = 'FACTURADOR_ARCHIVO_LLAVE'
MARK = 'facturai_secreto'
CHECK_FILE = 'llave-maestra.comprobacion'
# Purposes. Each one has its own derived key, so one kind of file never opens another.
PORTAL_VAULT = 'boveda-portales'
MAIL_VAULT = 'boveda-correo'
CHATGPT_SESSION = 'sesion-chatgpt'
GMAIL_TOKEN = 'token-gmail'
GMAIL_CLIENT = 'cliente-gmail'
IA_VAULT = 'boveda-ia'
PUSH_KEYS = 'llaves-avisos'
PASSWORDS = 'contrasenas'
CHECK = 'comprobacion'
LOCAL = 'local'

SEALED = 'No se pueden abrir los secretos guardados. '
MISSING = SEALED + 'Falta la llave maestra con la que se protegieron.'
DIFFERENT = SEALED + 'La llave maestra configurada no es la que los protegió.'
FORMAT = ('La llave maestra debe tener 32 bytes al azar escritos en base64. '
          'Créala con: python -m facturador crear-llave <archivo>')
INSIDE = 'El archivo de la llave maestra debe estar fuera de la carpeta de datos.'


class SealedSecrets(ValueError):
    """Stored secrets exist but this installation cannot open them."""


class MasterKeyError(ValueError):
    """The master key is configured in a way that cannot be used."""


_lock = threading.RLock()
_master: bytes | None = None
_loaded = False
_roots: dict[Path, str] = {}
_owners: dict[str, str] = {}
_derived: dict[tuple[str, str], bytes] = {}
_ABSENT = object()


def generate_key() -> str:
    return base64.urlsafe_b64encode(secrets.token_bytes(32)).decode('ascii')


def parse_key(text: str) -> bytes:
    """Exactly 32 bytes, written in base64 (either alphabet) or in hexadecimal."""
    value = ''.join(str(text).split())
    raw = b''
    if len(value) == 64:
        try:
            raw = bytes.fromhex(value)
        except ValueError:
            raw = b''
    if not raw:
        try:
            raw = base64.b64decode(value.replace('-', '+').replace('_', '/') + '=' * (-len(value) % 4), validate=True)
        except (binascii.Error, ValueError):
            raw = b''
    # A typed phrase or a repeated pattern is not a key: random bytes are almost all different.
    if len(raw) != 32 or len(set(raw)) < 16:
        raise MasterKeyError(FORMAT)
    return raw


def _inside(path: Path, folder: Path) -> bool:
    return path == folder or folder in path.parents


def configure(key: bytes | str | None) -> None:
    """Set or clear the master key explicitly; the environment is not consulted afterwards."""
    global _master, _loaded
    if isinstance(key, str):
        key = parse_key(key)
    if key is not None and len(key) != 32:
        raise MasterKeyError(FORMAT)
    with _lock:
        _master, _loaded = key, True
        _derived.clear()


def reset() -> None:
    """Forget the key and every registered folder; the next use reads the environment again."""
    global _master, _loaded
    with _lock:
        _master, _loaded = None, False
        _derived.clear()
        _roots.clear()
        _owners.clear()


def load_from_env(data_root: Path | None = None, env=None) -> bool:
    """Read the master key from the environment. True when one is now in use."""
    env = os.environ if env is None else env
    inline = (env.get(KEY_VARIABLE) or '').strip()
    file_name = (env.get(FILE_VARIABLE) or '').strip()
    if inline and file_name:
        raise MasterKeyError(f'Configura la llave maestra de una sola forma: {KEY_VARIABLE} o {FILE_VARIABLE}.')
    key = None
    if file_name:
        path = Path(file_name).expanduser().resolve()
        folder = Path(data_root or env.get('FACTURADOR_DATA', str(BASE / 'data'))).resolve()
        if _inside(path, folder):
            raise MasterKeyError(INSIDE)
        try:
            text = path.read_text(encoding='ascii')
        except (OSError, UnicodeError):
            raise MasterKeyError('No se pudo leer el archivo de la llave maestra.') from None
        key = parse_key(text)
    elif inline:
        key = parse_key(inline)
    configure(key)
    return key is not None


def create_key_file(path: Path, data_root: Path) -> Path:
    """Write a new master key to a file that does not exist yet, outside the data folder."""
    path = Path(path).expanduser().resolve()
    if _inside(path, Path(data_root).resolve()):
        raise MasterKeyError(INSIDE)
    path.parent.mkdir(parents=True, exist_ok=True)
    try:
        descriptor = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    except FileExistsError:
        raise MasterKeyError('Ese archivo ya existe. Una llave maestra nunca se reemplaza.') from None
    with os.fdopen(descriptor, 'w', encoding='ascii') as handle:
        handle.write(generate_key() + '\n')
    return path


def _master_key() -> bytes | None:
    if not _loaded:
        load_from_env()
    return _master


def active() -> bool:
    with _lock:
        return _master_key() is not None


def register(root: Path, owner: str) -> None:
    """Everything stored under ``root`` belongs to ``owner`` and is sealed with that owner's keys."""
    with _lock:
        _roots[Path(root).resolve()] = owner
        _owners.clear()


def _owner(path: Path) -> str:
    name = str(path)
    owner = _owners.get(name)
    if owner is None:
        resolved = Path(path).resolve()
        match = max((root for root in _roots if _inside(resolved, root)), key=lambda root: len(root.parts), default=None)
        owner = _owners[name] = _roots[match] if match is not None else LOCAL
    return owner


def derive(purpose: str, owner: str = '') -> bytes:
    """A 32-byte key for one purpose and owner, from the master key (HKDF-SHA256)."""
    with _lock:
        master = _master_key()
        if master is None:
            raise SealedSecrets(MISSING)
        key = _derived.get((purpose, owner))
        if key is None:
            info = b'facturai/secretos/v1\0' + purpose.encode('utf-8') + b'\0' + owner.encode('utf-8')
            key = _derived[(purpose, owner)] = HKDF(algorithm=hashes.SHA256(), length=32, salt=None, info=info).derive(master)
        return key


def _bound(purpose: str, owner: str) -> bytes:
    return ('facturai/secretos/v1|' + purpose + '|' + owner).encode('utf-8')


def _is_envelope(value) -> bool:
    return isinstance(value, dict) and MARK in value


def _seal(value, purpose: str, owner: str) -> dict:
    nonce = secrets.token_bytes(12)
    data = json.dumps(value, ensure_ascii=False, separators=(',', ':')).encode('utf-8')
    sealed = AESGCM(derive(purpose, owner)).encrypt(nonce, data, _bound(purpose, owner))
    return {MARK: 1, 'uso': purpose, 'nonce': base64.b64encode(nonce).decode('ascii'),
            'datos': base64.b64encode(sealed).decode('ascii')}


def _open(envelope: dict, purpose: str, owner: str):
    try:
        if envelope.get(MARK) != 1 or envelope.get('uso') != purpose:
            raise ValueError
        nonce = base64.b64decode(envelope['nonce'], validate=True)
        sealed = base64.b64decode(envelope['datos'], validate=True)
        return json.loads(AESGCM(derive(purpose, owner)).decrypt(nonce, sealed, _bound(purpose, owner)))
    except SealedSecrets:
        raise
    except (InvalidTag, KeyError, TypeError, ValueError):
        raise SealedSecrets(DIFFERENT) from None


def _stored(path: Path):
    """What the file holds today, or None when it is absent or is not JSON."""
    try:
        return json.loads(path.read_text(encoding='utf-8'))
    except (OSError, ValueError):
        return None


def check(path: Path, *, purpose: str) -> None:
    """Raise unless the file can be replaced or removed without losing a secret nobody can open."""
    path = Path(path)
    with _lock:
        current = _stored(path)
        if _is_envelope(current):
            if _master_key() is None:
                raise SealedSecrets(MISSING)
            _open(current, purpose, _owner(path))


def read_json(path: Path, fallback=None, *, purpose: str):
    """``auth.read_json`` for a file of secrets. A plain file is sealed the first time it is read with a key."""
    from .auth import read_json as read_plain, write_private

    path = Path(path)
    with _lock:
        if _master_key() is None:
            value = read_plain(path, fallback)
            if _is_envelope(value):
                raise SealedSecrets(MISSING)
            return value
        value = read_plain(path, _ABSENT)
        if value is _ABSENT:
            return fallback
        owner = _owner(path)
        if _is_envelope(value):
            return _open(value, purpose, owner)
        try:
            write_private(path, _seal(value, purpose, owner))
        except OSError:
            # The content is still readable; sealing is tried again on the next read.
            log.warning('No se pudo cifrar %s; se intentará en la siguiente lectura.', path.name)
        return value


def write_json(path: Path, value, *, purpose: str) -> None:
    """``auth.write_private`` for a file of secrets."""
    from .auth import write_private

    path = Path(path)
    with _lock:
        check(path, purpose=purpose)
        if _master_key() is None:
            write_private(path, value)
        else:
            write_private(path, _seal(value, purpose, _owner(path)))


def remove(path: Path, *, purpose: str) -> None:
    path = Path(path)
    with _lock:
        check(path, purpose=purpose)
        path.unlink(missing_ok=True)


def verify(data_root: Path) -> None:
    """Refuse to continue when the configured master key is not the one this data folder was sealed with."""
    from .auth import write_private

    path = Path(data_root) / CHECK_FILE
    proof = {'facturai': 'llave-maestra'}
    with _lock:
        current = _stored(path)
        if _master_key() is None:
            if _is_envelope(current):
                raise SealedSecrets(MISSING)
            return
        if _is_envelope(current):
            if _open(current, CHECK, '') != proof:
                raise SealedSecrets(DIFFERENT)
            return
        if path.exists():
            raise SealedSecrets(SEALED + 'El archivo de comprobación de la llave maestra está dañado.')
        write_private(path, _seal(proof, CHECK, ''))
