#!/usr/bin/env python3
"""Puerta para el panel de FACTURAI de una sola persona.

El panel local no tiene cuentas: quien llega a él lo maneja. Esta puerta va entre un proxy con
TLS y el panel, y solo deja pasar a los aparatos que su dueño aprobó. Un aparato nuevo ve un
número; su dueño lo dicta a quien administra el servidor y este lo aprueba desde la terminal.
No hay contraseñas: cada aparato guarda una llave larga en una cookie y aquí solo queda su huella.

    python3 puerta.py servir                      la puerta (la arranca systemd)
    python3 puerta.py pendientes                  aparatos que esperan permiso
    python3 puerta.py aprobar 12345678 "celular"  da permiso al aparato que muestra ese número
    python3 puerta.py lista                       aparatos con permiso
    python3 puerta.py quitar a1b2c3               quita el permiso a un aparato

El proxy toma como permiso cualquier respuesta 2xx de la puerta. Por eso la puerta contesta 2xx
en un solo caso: cuando la petición trae la llave de un aparato aprobado. La página del número,
los errores y todo lo demás llevan otro código.

Solo usa la biblioteca estándar (Python 3.10 o posterior).
"""
from __future__ import annotations

import argparse
import hashlib
import html
import ipaddress
import json
import os
import re
import secrets
import signal
import socket
import socketserver
import sys
import threading
import time
from dataclasses import asdict, dataclass
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

COOKIE = '__Host-facturai_puerta'
PAGE = '/_puerta/'
CHECK = '/verificar'
KEY = re.compile(r'[A-Za-z0-9_-]{43}\Z')
CODE_DIGITS = 8
PENDING_SECONDS = 600
PENDING_MAX = 40
PENDING_PER_ADDRESS = 4
DEVICES_MAX = 20
IDLE_SECONDS = 180 * 86400
COOKIE_SECONDS = 400 * 86400
NAME_MAX = 60
SEEN_STEP = 60
FLUSH_SECONDS = 600
DATA_VARIABLE = 'FACTURAI_PUERTA_DATOS'
ADMIN_VARIABLE = 'FACTURAI_PUERTA_ADMIN'
ADMIN_DEFAULT = '/run/facturai-puerta/admin.sock'

UNKNOWN_CODE = 'Ese número no está esperando permiso. Los números valen 10 minutos: pide el que se ve ahora.'
TOO_MANY_DEVICES = f'Ya hay {DEVICES_MAX} aparatos con permiso. Quita uno antes de aprobar otro.'
UNKNOWN_DEVICE = 'No hay un aparato con esa clave. Revisa la lista.'
DAMAGED_FILE = 'El archivo de aparatos aprobados está dañado; la puerta no arranca para no dejar pasar a nadie por error.'
NOT_SAVED = 'No se pudo guardar el cambio en el disco del servidor, así que no se hizo. Revisa el disco y repite la orden.'
NO_ANSWER = 'La puerta no contesta. ¿Está arrancada? (systemctl status facturai-puerta)'
NEEDS_ROOT = 'Esta orden necesita permiso de administrador: repítela con sudo.'
ODD_ANSWER = 'La puerta contestó algo que no se entiende; la orden pudo no hacerse. Revisa con «lista».'


def fingerprint(key: str) -> str:
    return hashlib.sha256(key.encode('ascii')).hexdigest()


def key_from(cookie_header: str | None) -> str | None:
    """The key in a Cookie header; None when it is missing, repeated or not one this door issues."""
    found = []
    for part in (cookie_header or '').split(';'):
        name, separator, value = part.strip().partition('=')
        if separator and name.strip() == COOKIE:
            found.append(value.strip())
    if len(found) != 1 or not KEY.match(found[0]):
        return None
    return found[0]


def clean_name(name: str) -> str:
    text = ' '.join(''.join(char if char.isprintable() else ' ' for char in str(name or '')).split())
    return text[:NAME_MAX] or 'aparato'


def origin_group(address: str) -> str:
    """What counts as one visitor for the limit of numbers: an IPv4 address, or the /64 of an IPv6 one."""
    try:
        parsed = ipaddress.ip_address(address)
    except ValueError:
        return 'desconocido'
    if parsed.version == 6:
        return str(ipaddress.ip_network(f'{parsed}/64', strict=False))
    return str(parsed)


@dataclass
class Device:
    id: str
    huella: str
    nombre: str
    aprobado: float
    visto: float


@dataclass
class Pending:
    huella: str
    codigo: str
    creado: float  # On the door's own timer, which a change of the system clock does not move.
    origen: str
    grupo: str


@dataclass
class Visit:
    estado: str  # 'pasa', 'espera' or 'lleno'
    llave: str = ''
    pendiente: Pending | None = None


class Door:
    """The approved devices, kept on disk by fingerprint, and those waiting for approval, kept in memory."""

    def __init__(self, folder: Path | str, *, clock=time.time, timer=time.monotonic):
        self.file = Path(folder) / 'aparatos.json'
        self.clock = clock
        self.timer = timer
        self.lock = threading.Lock()
        self.devices: dict[str, Device] = {}
        self.pending: dict[str, Pending] = {}
        self.unsaved = False
        self._load()

    def _load(self) -> None:
        if not self.file.exists():
            return
        try:
            rows = json.loads(self.file.read_text(encoding='utf-8'))['aparatos']
            for row in rows:
                device = Device(str(row['id']), str(row['huella']), clean_name(row['nombre']),
                                float(row['aprobado']), float(row['visto']))
                if not re.fullmatch(r'[0-9a-f]{64}', device.huella) or not re.fullmatch(r'[0-9a-f]{6}', device.id):
                    raise ValueError(device.id)
                self.devices[device.huella] = device
        except (OSError, ValueError, KeyError, TypeError) as error:
            raise SystemExit(DAMAGED_FILE) from error

    def _save(self) -> None:
        # Called with the lock held. The file never holds a key, only fingerprints. Until the new file
        # has replaced the old one the change counts as not saved, so a failed write is tried again.
        self.unsaved = True
        self.file.parent.mkdir(parents=True, exist_ok=True)
        text = json.dumps({'aparatos': [asdict(device) for device in self.devices.values()]}, ensure_ascii=False, indent=1)
        temporary = self.file.with_name(self.file.name + '.nuevo')
        with os.fdopen(os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600), 'wb') as handle:
            handle.write(text.encode('utf-8'))
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, self.file)
        if os.name == 'posix':
            folder = os.open(self.file.parent, os.O_RDONLY)
            try:
                os.fsync(folder)
            finally:
                os.close(folder)
        self.unsaved = False

    def _expire(self) -> None:
        now = self.timer()
        for huella in [huella for huella, item in self.pending.items() if now - item.creado > PENDING_SECONDS]:
            del self.pending[huella]

    def _drop_idle(self) -> None:
        # Called with the lock held: a device nobody has used for months loses its permission.
        now = self.clock()
        for huella in [huella for huella, device in self.devices.items() if now - device.visto > IDLE_SECONDS]:
            del self.devices[huella]
            self.unsaved = True

    def _approved(self, key: str | None) -> bool:
        # Called with the lock held.
        if not isinstance(key, str) or not KEY.match(key):
            return False
        device = self.devices.get(fingerprint(key))
        if device is None:
            return False
        now = self.clock()
        if now - device.visto > IDLE_SECONDS:
            del self.devices[device.huella]
            try:
                self._save()
            except OSError:
                pass  # It stays out; the periodic save writes it down later.
            return False
        if now - device.visto > SEEN_STEP:
            device.visto = now
            self.unsaved = True
        return True

    def _waiting(self, key: str | None, origin: str) -> tuple[str, Pending | None]:
        # Called with the lock held.
        if not isinstance(key, str) or not KEY.match(key):
            key = None
        self._expire()
        item = self.pending.get(fingerprint(key)) if key else None
        if item is not None:
            return key, item
        group = origin_group(origin)
        from_group = sum(1 for other in self.pending.values() if other.grupo == group)
        if len(self.pending) >= PENDING_MAX or from_group >= PENDING_PER_ADDRESS:
            return '', None
        key = secrets.token_urlsafe(32)
        taken = {other.codigo for other in self.pending.values()}
        while True:
            code = f'{secrets.randbelow(10 ** CODE_DIGITS):0{CODE_DIGITS}d}'
            if code not in taken:
                break
        item = Pending(fingerprint(key), code, self.timer(), origin, group)
        self.pending[item.huella] = item
        return key, item

    def allowed(self, key: str | None) -> bool:
        """Whether this key belongs to an approved device that has been used in the last months."""
        with self.lock:
            return self._approved(key)

    def waiting(self, key: str | None, origin: str) -> tuple[str, Pending | None]:
        """The number a device without permission must dictate, with the key that identifies the device.

        A key that is not waiting here is never adopted: the device gets a new one. When too many
        devices are waiting nobody new is admitted, so a flood cannot push out a number already shown.
        """
        with self.lock:
            return self._waiting(key, origin)

    def visit(self, key: str | None, origin: str) -> Visit:
        """What the page shows to a device, decided in one step so an approval cannot fall in between."""
        with self.lock:
            if self._approved(key):
                return Visit('pasa')
            issued, item = self._waiting(key, origin)
            return Visit('espera', issued, item) if item is not None else Visit('lleno')

    def approve(self, code: str, name: str = '') -> Device:
        digits = re.sub(r'[^0-9]', '', str(code or ''))
        with self.lock:
            self._expire()
            item = next((other for other in self.pending.values() if other.codigo == digits), None)
            if item is None:
                raise ValueError(UNKNOWN_CODE)
            self._drop_idle()
            if len(self.devices) >= DEVICES_MAX:
                raise ValueError(TOO_MANY_DEVICES)
            taken = {device.id for device in self.devices.values()}
            while True:
                identifier = secrets.token_hex(3)
                if identifier not in taken:
                    break
            now = self.clock()
            device = Device(identifier, item.huella, clean_name(name), now, now)
            del self.pending[item.huella]
            self.devices[device.huella] = device
            try:
                self._save()
            except OSError as error:  # Nothing changes: the number still waits and nobody new passes.
                del self.devices[device.huella]
                self.pending[item.huella] = item
                raise ValueError(NOT_SAVED) from error
            return device

    def remove(self, identifier: str) -> Device:
        with self.lock:
            device = next((device for device in self.devices.values() if device.id == str(identifier or '').strip().lower()), None)
            if device is None:
                raise ValueError(UNKNOWN_DEVICE)
            del self.devices[device.huella]
            try:
                self._save()
            except OSError as error:  # Said as it is: the device keeps its permission, here and on disk.
                self.devices[device.huella] = device
                raise ValueError(NOT_SAVED) from error
            return device

    def flush(self) -> None:
        with self.lock:
            if self.unsaved:
                self._save()

    def order(self, request: dict) -> dict:
        """What the terminal asks for. Answers never carry a key or a fingerprint."""
        name = request.get('orden') if isinstance(request, dict) else None
        if name == 'pendientes':
            with self.lock:
                self._expire()
                now = self.timer()
                return {'pendientes': [{'codigo': item.codigo, 'origen': item.origen,
                                        'segundos': max(0, int(PENDING_SECONDS - (now - item.creado)))}
                                       for item in sorted(self.pending.values(), key=lambda item: item.creado)]}
        if name == 'aprobar':
            device = self.approve(request.get('codigo', ''), request.get('nombre', ''))
            return {'aprobado': {'id': device.id, 'nombre': device.nombre}}
        if name == 'lista':
            with self.lock:
                self._drop_idle()
                return {'aparatos': [{'id': device.id, 'nombre': device.nombre, 'aprobado': device.aprobado, 'visto': device.visto}
                                     for device in sorted(self.devices.values(), key=lambda device: device.aprobado)]}
        if name == 'quitar':
            device = self.remove(request.get('id', ''))
            return {'quitado': {'id': device.id, 'nombre': device.nombre}}
        raise ValueError('Orden desconocida.')


STYLE = ('body{margin:0;font:18px/1.5 system-ui,-apple-system,"Segoe UI",Roboto,sans-serif;background:#f4f5f7;color:#16202c}'
         'main{max-width:26rem;margin:12vh auto 0;padding:1.5rem}h1{font-size:1.4rem;margin:0 0 1.2rem}'
         '.codigo{font:700 2.6rem/1.2 ui-monospace,Menlo,Consolas,monospace;letter-spacing:.08em;margin:1rem 0;'
         'padding:1rem;background:#fff;border:1px solid #cfd6df;border-radius:.6rem;text-align:center}'
         '.nota{font-size:.95rem;color:#4a5868}'
         '@media (prefers-color-scheme:dark){body{background:#12171d;color:#e8edf2}'
         '.codigo{background:#1c232c;border-color:#34404d}.nota{color:#a9b6c4}}')


def document(title: str, body: str) -> str:
    return ('<!doctype html><html lang="es"><head><meta charset="utf-8">'
            '<meta name="viewport" content="width=device-width, initial-scale=1">'
            '<meta http-equiv="refresh" content="4"><meta name="robots" content="noindex">'
            f'<title>{html.escape(title)}</title><style>{STYLE}</style></head><body><main><h1>FACTURAI</h1>{body}</main></body></html>')


def waiting_page(code: str) -> str:
    shown = f'{code[:4]} {code[4:]}'
    return document('FACTURAI · Permiso para este aparato',
                    '<p>Este aparato todavía no tiene permiso para entrar.</p>'
                    '<p>Dicta este número a quien cuida tu FACTURAI:</p>'
                    f'<p class="codigo">{html.escape(shown)}</p>'
                    '<p class="nota">El número vale 10 minutos. Esta pantalla entra sola en cuanto den el permiso.</p>')


def busy_page() -> str:
    return document('FACTURAI · Espera un momento',
                    '<p>Hay demasiados aparatos pidiendo permiso en este momento.</p>'
                    '<p class="nota">Espera unos minutos. Esta pantalla se actualiza sola.</p>')


class Handler(BaseHTTPRequestHandler):
    """Answers the proxy: whether a request may pass, and the page a device without permission sees."""

    protocol_version = 'HTTP/1.1'
    server_version = 'puerta'
    sys_version = ''
    timeout = 30

    def log_message(self, *args) -> None:  # The proxy keeps the access log.
        pass

    def handle(self) -> None:
        # One request per connection. The door never reads a request body: on a connection kept open,
        # whatever a body carried would be taken for the next request and every later answer would
        # reach the wrong visitor.
        self.close_connection = True
        self.handle_one_request()

    def do_GET(self) -> None:
        self._answer(with_body=True)

    def do_HEAD(self) -> None:
        self._answer(with_body=False)

    def _refuse_method(self) -> None:
        self.close_connection = True
        self._send(405, 'Método no admitido.', extra={'Allow': 'GET, HEAD'})

    do_POST = do_PUT = do_PATCH = do_DELETE = do_OPTIONS = _refuse_method

    def _answer(self, *, with_body: bool) -> None:
        # Any failure closes the door: nothing here may end in a 2xx by accident.
        self.close_connection = True
        try:
            lengths = self.headers.get_all('Content-Length') or []
            if any(value.strip() != '0' for value in lengths) or self.headers.get_all('Transfer-Encoding'):
                self._send(400, 'La puerta no recibe datos.', with_body=with_body)
                return
            # The target is compared as it came. A target that is not a plain path («x:y:/_puerta/»,
            # «http://…», «*») is refused, so no other way of reading it can turn it into a known route.
            if not self.path.startswith('/'):
                self._send(400, 'Petición no admitida.', with_body=with_body)
                return
            path = self.path.partition('?')[0]
            key = key_from('; '.join(self.headers.get_all('Cookie') or []))
            if path == CHECK:
                self._check(key, with_body)
            elif path in (PAGE, PAGE.rstrip('/')):
                self._page(key, with_body)
            else:
                self._send(404, 'No existe.', with_body=with_body)
        except Exception:
            try:
                self._send(500, 'La puerta falló y no deja pasar a nadie.', with_body=with_body)
            except Exception:
                pass

    def _check(self, key: str | None, with_body: bool) -> None:
        # The only 2xx this door ever sends.
        if self.server.door.allowed(key) is True:
            self._send(200, '', with_body=False)
            return
        method = (self.headers.get('X-Forwarded-Method') or 'GET').upper()
        mode = self.headers.get('Sec-Fetch-Mode')
        navigation = method == 'GET' and (mode == 'navigate' or (mode is None and 'text/html' in (self.headers.get('Accept') or '')))
        if navigation:
            self._send(302, '', extra={'Location': PAGE}, with_body=False)
        else:
            self._send(401, 'Este aparato no tiene permiso para entrar.', with_body=with_body)

    def _page(self, key: str | None, with_body: bool) -> None:
        visit = self.server.door.visit(key, self._origin())
        if visit.estado == 'pasa':
            self._send(302, '', extra={'Location': '/'}, with_body=False)
        elif visit.estado == 'espera' and visit.pendiente is not None:
            extra = {}
            if visit.llave != key:
                extra['Set-Cookie'] = f'{COOKIE}={visit.llave}; Max-Age={COOKIE_SECONDS}; Path=/; Secure; HttpOnly; SameSite=Lax'
            # 403 and not 200: the page is shown just the same, and the proxy can never take it for a permission.
            self._send(403, waiting_page(visit.pendiente.codigo), kind='text/html; charset=utf-8', extra=extra, with_body=with_body)
        else:
            self._send(429, busy_page(), kind='text/html; charset=utf-8', extra={'Retry-After': '60'}, with_body=with_body)

    def _origin(self) -> str:
        address = (self.headers.get('X-Forwarded-For') or '').rpartition(',')[2].strip()
        return address if re.fullmatch(r'[0-9A-Fa-f:.]{2,45}', address) else 'desconocido'

    def _send(self, status: int, text: str, *, kind: str = 'text/plain; charset=utf-8', extra: dict | None = None,
              with_body: bool = True) -> None:
        body = text.encode('utf-8')
        self.send_response(status)
        self.send_header('Content-Type', kind)
        self.send_header('Content-Length', str(len(body)))
        self.send_header('Cache-Control', 'no-store')
        self.send_header('Content-Security-Policy',
                         "default-src 'none'; style-src 'unsafe-inline'; base-uri 'none'; form-action 'none'; frame-ancestors 'none'")
        self.send_header('X-Content-Type-Options', 'nosniff')
        self.send_header('Referrer-Policy', 'no-referrer')
        self.send_header('X-Robots-Tag', 'noindex, nofollow')
        for name, value in (extra or {}).items():
            self.send_header(name, value)
        self.send_header('Connection', 'close')
        self.end_headers()
        if with_body and body:
            self.wfile.write(body)


class DoorServer(ThreadingHTTPServer):
    daemon_threads = True
    allow_reuse_address = True
    # Every request of the panel opens a connection here: a short queue would stall a page that asks for many things at once.
    request_queue_size = 128

    def __init__(self, address: tuple[str, int], door: Door):
        self.door = door
        super().__init__(address, Handler)

    def handle_error(self, request, client_address) -> None:  # A dropped connection is not news.
        pass


class AdminHandler(socketserver.StreamRequestHandler):
    """One order per connection, as a line of JSON, from the terminal of the server."""

    timeout = 10

    def handle(self) -> None:
        try:
            reply = self.server.door.order(json.loads(self.rfile.readline(4096) or b'null'))
        except ValueError as error:
            reply = {'error': str(error)}
        except Exception:
            reply = {'error': 'La puerta no pudo atender la orden.'}
        self.wfile.write((json.dumps(reply, ensure_ascii=False) + '\n').encode('utf-8'))


def admin_server(path: str, door: Door):
    """The terminal's way in: a socket file that only the door's own user and root can open."""
    if not hasattr(socketserver, 'ThreadingUnixStreamServer'):
        raise SystemExit('La terminal de la puerta necesita un sistema con sockets de archivo (Linux).')
    try:
        os.unlink(path)
    except FileNotFoundError:
        pass
    before = os.umask(0o177)
    try:
        server = socketserver.ThreadingUnixStreamServer(path, AdminHandler)
    finally:
        os.umask(before)
    server.daemon_threads = True
    server.door = door
    return server


def ask(path: str, request: dict) -> dict:
    with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as client:
        client.settimeout(10)
        client.connect(path)
        client.sendall((json.dumps(request) + '\n').encode('utf-8'))
        data = b''
        while not data.endswith(b'\n'):
            chunk = client.recv(65536)
            if not chunk:
                break
            data += chunk
    reply = json.loads(data) if data.endswith(b'\n') else None
    if not isinstance(reply, dict):
        raise ValueError(ODD_ANSWER)
    return reply


def moment(seconds: float) -> str:
    return time.strftime('%Y-%m-%d %H:%M', time.localtime(seconds))


def show(reply: dict) -> int:
    if 'error' in reply:
        print(reply['error'])
        return 1
    if 'pendientes' in reply:
        if not reply['pendientes']:
            print('Ningún aparato espera permiso.')
        for item in reply['pendientes']:
            print(f"{item['codigo'][:4]} {item['codigo'][4:]}  ·  le quedan {item['segundos'] // 60} min  ·  desde {item['origen']}")
    elif 'aprobado' in reply:
        print(f"Permiso dado: {reply['aprobado']['nombre']} (clave {reply['aprobado']['id']}).")
    elif 'aparatos' in reply:
        if not reply['aparatos']:
            print('Ningún aparato tiene permiso.')
        for device in reply['aparatos']:
            print(f"{device['id']}  ·  {device['nombre']}  ·  aprobado {moment(device['aprobado'])}  ·  visto {moment(device['visto'])}")
    elif 'quitado' in reply:
        print(f"Permiso quitado: {reply['quitado']['nombre']} (clave {reply['quitado']['id']}).")
    else:
        print(ODD_ANSWER)
        return 1
    return 0


def serve(folder: str, port: int, admin_path: str) -> None:
    door = Door(folder)
    public = DoorServer(('127.0.0.1', port), door)
    admin = admin_server(admin_path, door)
    stop = threading.Event()

    def attend(server) -> None:
        # If either server stops on its own the whole door stops, and systemd starts it again.
        try:
            server.serve_forever()
        finally:
            stop.set()

    def keep() -> None:
        while not stop.wait(FLUSH_SECONDS):
            try:
                door.flush()
            except OSError:
                pass  # Tried again on the next round.

    for target, arguments in ((attend, (public,)), (attend, (admin,)), (keep, ())):
        threading.Thread(target=target, args=arguments, daemon=True).start()
    for number in (signal.SIGTERM, signal.SIGINT):
        signal.signal(number, lambda *_: stop.set())
    stop.wait()
    public.shutdown()
    admin.shutdown()
    try:
        door.flush()
    except OSError:
        pass
    try:
        os.unlink(admin_path)
    except OSError:
        pass


def code_and_name(parts: list[str]) -> tuple[str, str]:
    """The number may come as it is shown, in two groups of four: «aprobar 1234 5678 celular»."""
    if len(parts) >= 2 and re.fullmatch(r'[0-9]{4}', parts[0]) and re.fullmatch(r'[0-9]{4}', parts[1]):
        return parts[0] + parts[1], ' '.join(parts[2:])
    return parts[0], ' '.join(parts[1:])


def main(arguments: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog='puerta.py', description='Puerta del panel de FACTURAI: aparatos con permiso.')
    parser.add_argument('--admin', default=os.environ.get(ADMIN_VARIABLE) or ADMIN_DEFAULT, help='socket de la terminal')
    orders = parser.add_subparsers(dest='orden', required=True)
    serving = orders.add_parser('servir', help='atiende al proxy')
    serving.add_argument('--datos', default=os.environ.get(DATA_VARIABLE) or '.', help='carpeta del archivo de aparatos')
    serving.add_argument('--puerto', type=int, default=8766)
    orders.add_parser('pendientes', help='aparatos que esperan permiso')
    approving = orders.add_parser('aprobar', help='da permiso al aparato que muestra ese número')
    approving.add_argument('numero_y_nombre', nargs='+', help='el número (junto o en dos grupos) y, si quieres, un nombre')
    orders.add_parser('lista', help='aparatos con permiso')
    removing = orders.add_parser('quitar', help='quita el permiso a un aparato')
    removing.add_argument('id')
    chosen = parser.parse_args(arguments)
    if chosen.orden == 'servir':
        serve(chosen.datos, chosen.puerto, chosen.admin)
        return 0
    request = {'orden': chosen.orden}
    if chosen.orden == 'aprobar':
        code, name = code_and_name(chosen.numero_y_nombre)
        request.update(codigo=code, nombre=name)
    elif chosen.orden == 'quitar':
        request['id'] = chosen.id
    try:
        return show(ask(chosen.admin, request))
    except PermissionError:
        print(NEEDS_ROOT)
    except OSError:
        print(NO_ANSWER)
    except ValueError:
        print(ODD_ANSWER)
    return 1


if __name__ == '__main__':
    sys.exit(main())
