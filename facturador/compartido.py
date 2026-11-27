"""The folder the accounts of one installation share.

A single person keeps everything in the data folder and shares nothing. With several accounts
each one has its own folder, and what the program learns about the portals of the merchants
also goes to one folder of the installation, so the next account does not start from zero.
Nothing of a person may reach that folder: whoever writes there decides, value by value, what
is public knowledge of a merchant. This module only says where the folder is, how many
accounts must agree before one acts for the others, and which paths are fit to leave an account.
"""
from __future__ import annotations

import logging
import re
import threading
import unicodedata
from pathlib import Path

log = logging.getLogger(__name__)

FOLDER = 'compartido'
# Accounts whose verified results must agree before what one of them learned acts for the
# others without asking. They are counted, never named: the shared files do not say who.
CORROBORATION = 2

_lock = threading.Lock()
_shared: dict[Path, Path] = {}   # folder that holds the accounts -> folder they share
_known: dict[str, Path] = {}     # data folder as it was given -> where it shares


def enable(accounts, shared) -> Path:
    """Every data folder directly inside ``accounts`` shares what it learns in ``shared``."""
    accounts, shared = Path(accounts).resolve(), Path(shared).resolve()
    try:
        shared.mkdir(parents=True, exist_ok=True, mode=0o700)
        shared.chmod(0o700)
    except OSError:
        # Without the folder nothing is shared; every account still works with what it knows.
        log.warning('No se pudo preparar la carpeta compartida.')
    with _lock:
        _shared[accounts] = shared
        _known.clear()
    return shared


def disable(accounts) -> None:
    """The data folders inside ``accounts`` share nothing; each one works with what it knows."""
    with _lock:
        _shared.pop(Path(accounts).resolve(), None)
        _known.clear()


def reset() -> None:
    """Forget every installation: no data folder shares anything."""
    with _lock:
        _shared.clear()
        _known.clear()


def shared_root(root) -> Path:
    """Where the installation of the data folder ``root`` keeps what its accounts share.

    It is ``root`` itself when that folder shares nothing: the single-person mode, and any
    folder that is not an account of an installation set up with ``enable``.
    """
    root = Path(root)
    name = str(root)
    with _lock:
        found = _known.get(name)
        if found is None:
            found = _known[name] = _shared.get(root.resolve().parent, root)
        return found


def shares(root) -> bool:
    """Whether the data folder ``root`` is an account that shares with the others of its installation."""
    return shared_root(root) != Path(root)


def personal_words(root) -> set[str] | None:
    """Words of four letters or more of what the person of a data folder saved about themselves.

    Their name in the profile and what they left for registrations (names, street,
    neighbourhood, town, state). Whoever shares a text checks it against these besides its own
    rules. None when they cannot be read: then nothing is certain and nothing should be shared.
    """
    try:
        from .auth import read_json
        from .registro_datos import datos_de_registro
        profile = read_json(Path(root) / 'profile.json', {})
        values = [profile.get('name', '') if isinstance(profile, dict) else '',
                  *(value for name, value in datos_de_registro(root).items() if name != 'pais')]
    except Exception:
        return None
    return {word for value in values for word in re.findall(r'[a-z]{4,}', _folded(str(value or '')))}


def _folded(text: str) -> str:
    return ''.join(char for char in unicodedata.normalize('NFKD', text.casefold()) if not unicodedata.combining(char))


PLACEHOLDER = re.compile(r'\{[a-z_.\[\]0-9]{1,40}\}')


def _coded(piece: str) -> bool:
    """A piece of a path that reads as an identifier or as encoded data rather than as a word."""
    if len(piece) > 40:
        return True
    if '#' in piece or any(char.isdigit() for char in piece):
        # A number alone, or a word that ends in a short one («paso2», «cfdi40»), is still a place.
        return not re.fullmatch(r'#|\d{1,3}|[^\W\d_]+(?:#|\d{1,3})', piece)
    humps = re.findall(r'[A-Z]+(?![a-z])|[A-Z]?[^\WA-Z\d_]+', piece)
    if len(humps) >= 3 and len(piece) < 3 * len(humps):
        return True  # The case changes every other letter: that is base64, not «nuevaFactura».
    if len(piece) < 12:
        return False
    plain = _folded(piece)
    vowels = sum(char in 'aeiouy' for char in plain)
    return vowels * 5 < len(plain) or max(map(len, re.findall(r'[^aeiouy]+', plain)), default=0) > 5


def plain_path(path: str) -> bool:
    """True when a path is made of words: it names a place of a site, not a record or a session.

    ``{name}`` marks left where a known value was taken out and ``#`` for a long number are
    accepted. A path with parameters (``;jsessionid=``), with a session written into it
    (``/(S(…))/``) or with a piece that looks like a token or like base64 is not plain: it may
    carry what identifies a visit, a ticket or a person, in clear or encoded.
    """
    if not isinstance(path, str) or len(path) > 300:
        return False
    text = PLACEHOLDER.sub('', path)
    if not all(char.isalnum() or char in ' _.~/+,-#' for char in text):
        return False
    return not any(_coded(piece) for piece in re.findall(r'(?:[^\W_]|#)+', text))
