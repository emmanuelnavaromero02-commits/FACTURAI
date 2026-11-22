"""Keep Chrome's Linux profile exclusion effective across container replacements."""
import argparse
import asyncio
import errno
import os
import re
import socket
import sys
import weakref
from pathlib import Path
from playwright.async_api import Error as BrowserError


LINKS = ('SingletonSocket', 'SingletonCookie', 'SingletonLock')
PROFILES = ('browser_profile_chrome', 'browser_personal_chrome')
_context_leases = weakref.WeakKeyDictionary()


def lock_profile(directory):
    import fcntl
    directory.mkdir(mode=0o700, parents=True, exist_ok=True)
    handle = os.fdopen(os.open(directory/'.facturai-owner', os.O_RDWR | os.O_CREAT | os.O_NOFOLLOW, 0o600), 'r+')
    try:
        fcntl.flock(handle, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except BlockingIOError as exc:
        handle.close()
        raise ValueError('Otra instancia de FACTURAI está usando este perfil de Chrome.') from exc
    return handle


def socket_unavailable(path):
    with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as probe:
        probe.settimeout(0.5)
        try:
            probe.connect(str(path))
        except OSError as exc:
            return exc.errno in (errno.ENOENT, errno.ECONNREFUSED)
    return False


class ChromeLease:
    def __init__(self, handle, directory):
        self.handle = handle
        self.closed = asyncio.Event()
        self.pid = None
        self.socket = None
        lock, endpoint = directory/'SingletonLock', directory/'SingletonSocket'
        if lock.is_symlink() and endpoint.is_symlink():
            owner = re.fullmatch(re.escape(socket.gethostname()) + r'-([1-9]\d*)', os.readlink(lock))
            if owner:
                self.pid = int(owner[1])
                self.socket = Path(os.readlink(endpoint))

    def release(self, *_):
        self.handle.close()
        self.closed.set()

    def process_gone(self):
        if self.pid is None:
            return False
        try:
            os.kill(self.pid, 0)
        except ProcessLookupError:
            return socket_unavailable(self.socket)
        except PermissionError:
            pass
        return False


async def close_context(context):
    lease = _context_leases.get(context)
    if lease is None:
        try:
            await context.close()
        except BrowserError:
            if context.is_closed() is not True:
                raise
        return
    if lease.closed.is_set():
        return
    # Playwright's is_closed() is also true while a concurrent close is in
    # progress. The profile remains owned until Chrome has finished closing.
    try:
        async with asyncio.timeout(10):
            if context.is_closed() is not True:
                try:
                    await context.close()
                except BrowserError:
                    if context.is_closed() is not True and not lease.process_gone():
                        raise
            while not lease.closed.is_set():
                try:
                    await asyncio.wait_for(lease.closed.wait(), .25)
                except TimeoutError:
                    if lease.process_gone():
                        lease.release()
    except TimeoutError as exc:
        if lease.closed.is_set() or lease.process_gone():
            lease.release()
            return
        raise ValueError('Google Chrome sigue cerrando el perfil. Espera a que termine para reabrirlo.') from exc


def release_stale_links(directory, handle, shared_tmp, retired_host=None):
    links = [directory/name for name in LINKS]
    lock = links[-1]
    if not lock.is_symlink():
        return False
    owner = os.readlink(lock)
    match = re.fullmatch(r'([A-Za-z0-9._-]+)-([1-9]\d*)', owner)
    if not match or any(p.exists() and not p.is_symlink() for p in links):
        return False
    socket_link = links[0]
    if not socket_link.is_symlink():
        return False
    path = Path(os.readlink(socket_link))
    managed = (path.name == 'SingletonSocket' and path.parent.parent == shared_tmp
               and not path.parent.is_symlink() and not shared_tmp.is_symlink())
    if retired_host is None:
        handle.seek(0)
        if not managed or handle.read(256) != owner:
            return False
    elif match[1] != retired_host or match[1] == socket.gethostname():
        return False
    # For an old /tmp socket this check alone cannot exclude a live foreign
    # container. Legacy cleanup is gated by the operator's verified retired hostname.
    if not socket_unavailable(path):
        return False
    if os.readlink(lock) != owner:
        return False
    for link in links:
        if link.is_symlink():
            link.unlink()
    return True


async def launch_chrome(launch, directory, **options):
    if not sys.platform.startswith('linux'):
        return await launch(directory, **options)
    directory = Path(directory).resolve()
    handle = lock_profile(directory)
    tmp = directory/'.runtime'
    try:
        tmp.mkdir(mode=0o700, exist_ok=True)
        if tmp.is_symlink():
            raise ValueError('Directorio de sesión de Chrome inválido.')
        release_stale_links(directory, handle, tmp)
        # Linux sockaddr_un has a 108-byte path limit. Chrome can use its normal
        # temp location on unusually long paths; keep its own exclusion there.
        if len(os.fsencode(tmp)) + len('/com.google.Chrome.XXXXXX/SingletonSocket') < 108:
            options['env'] = {**os.environ, **options.get('env', {}), 'TMPDIR': str(tmp)}
        context = await launch(directory, **options)
        lease = ChromeLease(handle, directory)
        _context_leases[context] = lease
        context.on('close', lease.release)
        lock = directory/'SingletonLock'
        if lock.is_symlink():
            handle.seek(0)
            handle.truncate()
            handle.write(os.readlink(lock))
            handle.flush()
            os.fsync(handle.fileno())
        return context
    except BaseException:
        handle.close()
        raise


def main():
    parser = argparse.ArgumentParser(description='Migra locks de Chrome después de retirar su contenedor propietario.')
    parser.add_argument('--data', required=True, type=Path)
    parser.add_argument('--retired-host', required=True)
    args = parser.parse_args()
    if args.retired_host == socket.gethostname() or not re.fullmatch(r'[A-Za-z0-9][A-Za-z0-9.-]{0,252}', args.retired_host):
        parser.error('Se requiere el hostname verificado del contenedor retirado.')
    changed = 0
    for name in PROFILES:
        directory = args.data.resolve()/name
        if directory.is_symlink() or not directory.is_dir():
            continue
        with lock_profile(directory) as handle:
            changed += release_stale_links(directory, handle, directory/'.runtime', args.retired_host)
    print('Perfiles de Chrome liberados:', changed)


if __name__ == '__main__':
    main()
