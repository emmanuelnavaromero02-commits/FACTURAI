"""La puerta del panel de una sola persona: solo pasan los aparatos aprobados."""
import contextlib
import http.client
import importlib.util
import io
import json
import os
import re
import socket
import socketserver
import stat
import sys
import tempfile
import threading
import unittest
from pathlib import Path
from unittest.mock import patch

# The door is a single file outside the package: the server runs it alone, with the system's Python.
SOURCE = Path(__file__).resolve().parents[1] / 'puerta' / 'puerta.py'
spec = importlib.util.spec_from_file_location('puerta_de_prueba', SOURCE)
puerta = importlib.util.module_from_spec(spec)
sys.modules[spec.name] = puerta
spec.loader.exec_module(puerta)

GOOD = 'A' * 43
OTHER = 'B' * 43


class Clock:
    def __init__(self, now=1_800_000_000.0):
        self.now = now

    def __call__(self):
        return self.now


class Folder(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.folder = Path(temporary.name)
        self.clock = Clock()
        self.timer = Clock(5_000.0)

    def door(self):
        return puerta.Door(self.folder, clock=self.clock, timer=self.timer)

    def approved(self, door, origin='203.0.113.7', name='celular'):
        key, item = door.waiting(None, origin)
        return key, door.approve(item.codigo, name)


class KeyTests(unittest.TestCase):
    def test_only_one_well_formed_key_of_this_door_counts(self):
        name = puerta.COOKIE
        self.assertEqual(puerta.key_from(f'{name}={GOOD}'), GOOD)
        self.assertEqual(puerta.key_from(f'otra=1; {name}={GOOD} ; sesion=x=y'), GOOD)
        for header in (None, '', 'otra=1', f'{name}=', f'{name}={GOOD[:-1]}', f'{name}={GOOD}x', f'{name}={GOOD[:-1]}!',
                       f'{name}={GOOD}; {name}={OTHER}', f'{name}={GOOD}; {name}={GOOD}', f'x{name}={GOOD}',
                       f'{name.lower()}={GOOD}', f'{name}="{GOOD}"', f'{name}={GOOD} x', f'{name}={GOOD},{OTHER}'):
            self.assertIsNone(puerta.key_from(header), header)

    def test_one_visitor_is_an_ipv4_address_or_the_64_of_an_ipv6_one(self):
        self.assertEqual(puerta.origin_group('203.0.113.7'), '203.0.113.7')
        self.assertEqual(puerta.origin_group('2001:db8:1:2:aaaa::1'), puerta.origin_group('2001:db8:1:2:bbbb::9'))
        self.assertNotEqual(puerta.origin_group('2001:db8:1:2::1'), puerta.origin_group('2001:db8:1:3::1'))
        for odd in ('', 'desconocido', '<script>', '999.1.1.1'):
            self.assertEqual(puerta.origin_group(odd), 'desconocido')


class DoorTests(Folder):
    def test_nobody_passes_until_the_number_is_approved(self):
        door = self.door()
        self.assertFalse(door.allowed(None))
        self.assertFalse(door.allowed(GOOD))
        key, item = door.waiting(None, '203.0.113.7')
        self.assertRegex(key, puerta.KEY)
        self.assertRegex(item.codigo, r'^\d{8}$')
        self.assertFalse(door.allowed(key))
        self.assertEqual(door.waiting(key, '198.51.100.9'), (key, item))  # The same device keeps its number.
        with self.assertRaisesRegex(ValueError, 'no está esperando'):
            door.approve('00000000' if item.codigo != '00000000' else '11111111', 'ajeno')
        self.assertFalse(door.allowed(key))
        device = door.approve(f'{item.codigo[:4]} {item.codigo[4:]}', '  celular\nde   Ana ')
        self.assertEqual(device.nombre, 'celular de Ana')
        self.assertTrue(door.allowed(key))
        self.assertFalse(door.allowed(OTHER))
        with self.assertRaisesRegex(ValueError, 'no está esperando'):
            door.approve(item.codigo, 'otra vez')  # A number works once.

    def test_only_plain_digits_make_a_number(self):
        door = self.door()
        key, item = door.waiting(None, '203.0.113.7')
        wide = ''.join(chr(ord('０') + int(digit)) for digit in item.codigo)
        for odd in (wide, '', None, '١٢٣٤٥٦٧٨', item.codigo[:-1], item.codigo + '0'):
            with self.assertRaisesRegex(ValueError, 'no está esperando'):
                door.approve(odd, 'ajeno')
        self.assertFalse(door.allowed(key))

    def test_the_file_keeps_fingerprints_and_never_the_key(self):
        door = self.door()
        key, item = door.waiting(None, '203.0.113.7')
        door.approve(item.codigo, 'celular')
        text = door.file.read_text(encoding='utf-8')
        self.assertNotIn(key, text)
        self.assertIn(puerta.fingerprint(key), text)
        self.assertNotIn(item.codigo, text)
        if os.name == 'posix':
            self.assertEqual(stat.S_IMODE(door.file.stat().st_mode), 0o600)
        again = self.door()  # After a restart the device keeps its permission and nobody is left waiting.
        self.assertTrue(again.allowed(key))
        self.assertEqual(again.pending, {})

    def test_a_planted_key_is_not_adopted(self):
        door = self.door()
        key, item = door.waiting(GOOD, '203.0.113.7')
        self.assertNotEqual(key, GOOD)
        door.approve(item.codigo, 'celular')
        self.assertFalse(door.allowed(GOOD))
        self.assertTrue(door.allowed(key))

    def test_a_number_expires_and_the_device_gets_another(self):
        door = self.door()
        key, item = door.waiting(None, '203.0.113.7')
        self.timer.now += puerta.PENDING_SECONDS + 1
        with self.assertRaisesRegex(ValueError, 'no está esperando'):
            door.approve(item.codigo, 'tarde')
        newer_key, newer = door.waiting(key, '203.0.113.7')
        self.assertNotEqual(newer_key, key)
        self.assertFalse(door.allowed(key))
        door.approve(newer.codigo, 'celular')
        self.assertTrue(door.allowed(newer_key))

    def test_setting_the_system_clock_back_does_not_keep_a_number_alive(self):
        door = self.door()
        _, item = door.waiting(None, '203.0.113.7')
        self.clock.now -= 3600
        self.timer.now += puerta.PENDING_SECONDS + 1
        self.assertEqual(door.order({'orden': 'pendientes'}), {'pendientes': []})
        with self.assertRaisesRegex(ValueError, 'no está esperando'):
            door.approve(item.codigo, 'tarde')

    def test_a_flood_cannot_push_out_a_number_already_shown(self):
        door = self.door()
        key, item = door.waiting(None, '203.0.113.7')
        for _ in range(puerta.PENDING_PER_ADDRESS + 3):
            door.waiting(None, '198.51.100.1')
        self.assertEqual(sum(1 for other in door.pending.values() if other.origen == '198.51.100.1'), puerta.PENDING_PER_ADDRESS)
        for number in range(puerta.PENDING_PER_ADDRESS + 3):  # One machine with many IPv6 addresses is one visitor.
            door.waiting(None, f'2001:db8:1:2::{number + 1:x}')
        self.assertEqual(sum(1 for other in door.pending.values() if other.grupo == '2001:db8:1:2::/64'), puerta.PENDING_PER_ADDRESS)
        for number in range(puerta.PENDING_MAX * 2):
            door.waiting(None, f'198.51.{number // 250}.{number % 250 + 2}')
        self.assertEqual(len(door.pending), puerta.PENDING_MAX)
        self.assertEqual(door.waiting(None, '192.0.2.200'), ('', None))
        self.assertEqual(door.visit(None, '192.0.2.200').estado, 'lleno')
        self.assertEqual(len({other.codigo for other in door.pending.values()}), puerta.PENDING_MAX)
        self.assertEqual(door.waiting(key, '203.0.113.7'), (key, item))
        door.approve(item.codigo, 'celular')
        self.assertTrue(door.allowed(key))

    def test_the_page_decides_in_one_step_whether_a_device_passes_or_waits(self):
        door = self.door()
        first = door.visit(None, '203.0.113.7')
        self.assertEqual((first.estado, first.pendiente.codigo), ('espera', door.pending[puerta.fingerprint(first.llave)].codigo))
        again = door.visit(first.llave, '203.0.113.7')
        self.assertEqual((again.estado, again.llave, again.pendiente), ('espera', first.llave, first.pendiente))
        door.approve(first.pendiente.codigo, 'celular')
        passed = door.visit(first.llave, '203.0.113.7')
        self.assertEqual((passed.estado, passed.llave, passed.pendiente), ('pasa', '', None))
        self.assertEqual(door.pending, {})  # An approved device is never given another number or another key.

    def test_permission_ends_when_removed_or_after_months_without_use(self):
        door = self.door()
        key, device = self.approved(door)
        other_key, _ = self.approved(door, '203.0.113.8', 'tableta')
        with self.assertRaisesRegex(ValueError, 'No hay un aparato'):
            door.remove('zzzzzz')
        door.remove(device.id.upper())
        self.assertFalse(door.allowed(key))
        self.assertTrue(door.allowed(other_key))
        self.assertFalse(self.door().allowed(key))
        self.clock.now += puerta.IDLE_SECONDS - 5
        self.assertTrue(door.allowed(other_key))  # Using it keeps it alive.
        self.clock.now += puerta.IDLE_SECONDS - 5
        self.assertTrue(door.allowed(other_key))
        self.clock.now += puerta.IDLE_SECONDS + 5
        self.assertFalse(door.allowed(other_key))
        self.assertEqual(self.door().devices, {})

    def test_the_last_use_reaches_the_file_without_writing_on_every_request(self):
        door = self.door()
        key, _ = self.approved(door)
        with patch.object(door, '_save', wraps=door._save) as saving:
            for _ in range(50):
                self.assertTrue(door.allowed(key))
            self.clock.now += 3600
            self.assertTrue(door.allowed(key))
            self.assertEqual(saving.call_count, 0)
            door.flush()
            door.flush()
            self.assertEqual(saving.call_count, 1)
        self.assertEqual(self.door().devices[puerta.fingerprint(key)].visto, self.clock.now)

    def test_there_is_a_limit_of_devices_and_forgotten_ones_do_not_count(self):
        door = self.door()
        for number in range(puerta.DEVICES_MAX):
            self.approved(door, f'203.0.113.{number}', f'aparato {number}')
        key, item = door.waiting(None, '198.51.100.1')
        with self.assertRaisesRegex(ValueError, 'Quita uno'):
            door.approve(item.codigo, 'de más')
        self.assertFalse(door.allowed(key))
        self.assertEqual(door.waiting(key, '198.51.100.1'), (key, item))  # The number is still good.
        self.clock.now += puerta.IDLE_SECONDS + 5  # Nobody used the twenty in months: they are gone.
        door.approve(item.codigo, 'el nuevo')
        self.assertTrue(door.allowed(key))
        self.assertEqual([device['nombre'] for device in door.order({'orden': 'lista'})['aparatos']], ['el nuevo'])
        self.assertEqual(len(self.door().devices), 1)

    def test_a_change_that_cannot_be_written_is_not_made(self):
        door = self.door()
        kept_key, kept = self.approved(door, '203.0.113.8', 'tableta')
        key, item = door.waiting(None, '203.0.113.7')
        with patch.object(puerta.os, 'replace', side_effect=OSError('disco lleno')):
            with self.assertRaisesRegex(ValueError, 'No se pudo guardar'):
                door.approve(item.codigo, 'celular')
            self.assertFalse(door.allowed(key))
            self.assertEqual(door.waiting(key, '203.0.113.7'), (key, item))  # The same number can be approved again.
            with self.assertRaisesRegex(ValueError, 'No se pudo guardar'):
                door.remove(kept.id)
            self.assertTrue(door.allowed(kept_key))
            self.assertEqual([device['id'] for device in door.order({'orden': 'lista'})['aparatos']], [kept.id])
            self.assertTrue(door.unsaved)
        self.assertTrue(self.door().allowed(kept_key))  # Disk and memory say the same.
        self.assertFalse(self.door().allowed(key))
        door.approve(item.codigo, 'celular')
        door.remove(kept.id)
        self.assertFalse(door.unsaved)
        again = self.door()
        self.assertEqual((again.allowed(key), again.allowed(kept_key)), (True, False))

    def test_a_damaged_file_stops_the_door_instead_of_opening_it(self):
        for text in ('{', '[]', '{"aparatos": [{"id": "abc"}]}', '{"aparatos": [{"id": "a1b2c3", "huella": "corta", "nombre": "x", "aprobado": 1, "visto": 1}]}'):
            (self.folder / 'aparatos.json').write_text(text, encoding='utf-8')
            with self.assertRaises(SystemExit) as stopped:
                self.door()
            self.assertIn('dañado', str(stopped.exception))

    def test_orders_from_the_terminal_never_show_keys_or_fingerprints(self):
        door = self.door()
        key, item = door.waiting(None, '203.0.113.7')
        waiting = door.order({'orden': 'pendientes'})
        self.assertEqual(waiting, {'pendientes': [{'codigo': item.codigo, 'origen': '203.0.113.7', 'segundos': puerta.PENDING_SECONDS}]})
        approved = door.order({'orden': 'aprobar', 'codigo': item.codigo, 'nombre': 'celular'})
        listing = door.order({'orden': 'lista'})
        self.assertEqual([device['nombre'] for device in listing['aparatos']], ['celular'])
        for reply in (waiting, approved, listing):
            text = json.dumps(reply)
            self.assertNotIn(key, text)
            self.assertNotIn(puerta.fingerprint(key), text)
        self.assertEqual(door.order({'orden': 'quitar', 'id': approved['aprobado']['id']})['quitado']['nombre'], 'celular')
        self.assertFalse(door.allowed(key))
        for request in ({'orden': 'abrir'}, {}, None, 'lista', {'orden': 'aprobar'}, {'orden': 'quitar'}):
            with self.assertRaises(ValueError):
                door.order(request)

    def test_many_threads_at_once_leave_the_door_consistent(self):
        door = self.door()
        key, _ = self.approved(door)
        failures = []

        def work(number):
            try:
                for turn in range(60):
                    if not door.allowed(key) or door.allowed(OTHER):
                        failures.append('permiso')
                    visit = door.visit(None, f'198.51.{number}.{turn % 200 + 1}')
                    if visit.estado == 'espera' and turn % 3 == 0:
                        door.approve(visit.pendiente.codigo, f'hilo {number}')
                        if door.visit(visit.llave, 'x').estado != 'pasa':
                            failures.append('aprobado sin pasar')
                        door.remove(next(device.id for device in door.devices.values() if device.huella == puerta.fingerprint(visit.llave)))
            except Exception as error:  # noqa: BLE001
                failures.append(repr(error))

        threads = [threading.Thread(target=work, args=(number,)) for number in range(8)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join()
        self.assertEqual(failures, [])
        self.assertEqual(len(door.devices), 1)
        self.assertEqual(len(self.door().devices), 1)


class Served(Folder):
    def setUp(self):
        super().setUp()
        self.gate = self.door()
        self.server = puerta.DoorServer(('127.0.0.1', 0), self.gate)
        threading.Thread(target=self.server.serve_forever, daemon=True).start()
        self.addCleanup(self.server.server_close)
        self.addCleanup(self.server.shutdown)

    def ask(self, path, *, method='GET', key=None, headers=None, cookie=None):
        connection = http.client.HTTPConnection('127.0.0.1', self.server.server_address[1], timeout=10)
        self.addCleanup(connection.close)
        sent = dict(headers or {})
        if key:
            sent['Cookie'] = f'{puerta.COOKIE}={key}'
        if cookie is not None:
            sent['Cookie'] = cookie
        connection.request(method, path, headers=sent)
        response = connection.getresponse()
        return response, response.read().decode('utf-8')

    def raw(self, data):
        with socket.create_connection(('127.0.0.1', self.server.server_address[1]), timeout=10) as client:
            client.sendall(data)
            received = b''
            while True:
                try:
                    chunk = client.recv(65536)
                except ConnectionResetError:  # The door hung up without reading what was left.
                    chunk = b''
                if not chunk:
                    return received.decode('utf-8')
                received += chunk

    def status(self, answer):
        return int(answer.split(' ', 2)[1]) if answer.startswith('HTTP/1.') else 0

    def pair(self):
        response, body = self.ask(puerta.PAGE, headers={'X-Forwarded-For': '203.0.113.7'})
        self.assertEqual(response.status, 403)
        cookie = response.getheader('Set-Cookie')
        key = re.match(rf'{re.escape(puerta.COOKIE)}=([^;]+);', cookie).group(1)
        code = re.search(r'class="codigo">(\d{4}) (\d{4})<', body)
        return key, code.group(1) + code.group(2), cookie


class ServedTests(Served):
    def test_without_permission_requests_stop_and_pages_go_to_the_number(self):
        for headers in ({}, {'X-Forwarded-Method': 'POST', 'Sec-Fetch-Mode': 'navigate'}, {'Sec-Fetch-Mode': 'cors', 'Accept': 'text/html'},
                        {'Accept': 'application/json'}, {'X-Forwarded-Method': 'DELETE', 'Accept': 'text/html'}):
            for key in (None, GOOD):
                response, body = self.ask(puerta.CHECK, key=key, headers=headers)
                self.assertEqual(response.status, 401, headers)
                self.assertIn('no tiene permiso', body)
        for headers in ({'Sec-Fetch-Mode': 'navigate'}, {'Accept': 'text/html,application/xhtml+xml'},
                        {'X-Forwarded-Method': 'get', 'Sec-Fetch-Mode': 'navigate', 'Accept': '*/*'}):
            response, _ = self.ask(puerta.CHECK, headers=headers)
            self.assertEqual((response.status, response.getheader('Location')), (302, puerta.PAGE), headers)

    def test_a_device_pairs_with_its_number_and_then_passes(self):
        key, code, cookie = self.pair()
        for attribute in ('; Path=/', '; Secure', '; HttpOnly', '; SameSite=Lax', f'; Max-Age={puerta.COOKIE_SECONDS}'):
            self.assertIn(attribute, cookie)
        self.assertNotIn('Domain', cookie)
        self.assertTrue(cookie.startswith('__Host-'))
        self.assertEqual(self.ask(puerta.CHECK, key=key)[0].status, 401)
        response, body = self.ask(puerta.PAGE, key=key)  # The page refreshes itself and keeps the number.
        self.assertEqual(response.status, 403)
        self.assertIsNone(response.getheader('Set-Cookie'))
        self.assertIn(f'{code[:4]} {code[4:]}', body)
        self.assertIn('http-equiv="refresh"', body)
        self.assertEqual(response.getheader('Cache-Control'), 'no-store')
        self.assertIn("default-src 'none'", response.getheader('Content-Security-Policy'))
        self.gate.approve(code, 'celular')
        response, body = self.ask(puerta.CHECK, key=key, headers={'X-Forwarded-Method': 'POST'})
        self.assertEqual((response.status, body), (200, ''))
        for page in (puerta.PAGE, puerta.PAGE.rstrip('/'), puerta.PAGE + '?de=inicio'):
            response, _ = self.ask(page, key=key)
            self.assertEqual((response.status, response.getheader('Location'), response.getheader('Set-Cookie')), (302, '/', None))
        self.assertEqual(self.ask(puerta.CHECK, key=OTHER)[0].status, 401)
        doubled = f'{puerta.COOKIE}={key}; {puerta.COOKIE}={OTHER}'
        self.assertEqual(self.ask(puerta.CHECK, cookie=doubled)[0].status, 401)
        self.gate.remove(next(iter(self.gate.devices.values())).id)
        self.assertEqual(self.ask(puerta.CHECK, key=key)[0].status, 401)

    def test_an_unknown_cookie_gets_a_fresh_key(self):
        response, _ = self.ask(puerta.PAGE, key=GOOD)
        issued = re.match(rf'{re.escape(puerta.COOKIE)}=([^;]+);', response.getheader('Set-Cookie')).group(1)
        self.assertNotEqual(issued, GOOD)
        self.assertRegex(issued, puerta.KEY)

    def test_the_only_2xx_is_the_check_of_an_approved_device(self):
        # The proxy takes any 2xx of the door for a permission: the page of the number, the mistakes and
        # every target that is not a plain path must answer something else, whoever asks.
        key, code, _ = self.pair()
        waiting = f'Cookie: {puerta.COOKIE}={key}\r\n'
        targets = ('/', puerta.PAGE, puerta.PAGE.rstrip('/'), puerta.CHECK + '/', '/_puerta/verificar', '/_PUERTA/', '/api/state',
                   'y:/_puerta/', 'x:y:/_puerta/', 'x:y:/_puerta', 'x:/verificar', 'x:y:/verificar', 'http://x/_puerta/',
                   'http://x/verificar', 'https://x:8443/_puerta/?a=1', '*', '_puerta/', 'verificar', '?/_puerta/', '#/_puerta/',
                   '/_puerta/#x', '/verificar#x', '/_puerta/%2e%2e/verificar', '/%5fpuerta/', '\\_puerta\\')
        for method in ('GET', 'HEAD', 'POST', 'PUT', 'DELETE', 'OPTIONS', 'PATCH', 'TRACE', 'CONNECT', 'PROPFIND'):
            for target in targets + (puerta.CHECK, puerta.CHECK + '?x=1'):
                for cookie in ('', waiting, f'Cookie: {puerta.COOKIE}={OTHER}\r\n'):
                    answer = self.raw(f'{method} {target} HTTP/1.1\r\nHost: puerta\r\n{cookie}\r\n'.encode())
                    self.assertFalse(200 <= self.status(answer) <= 299, (method, target, cookie, answer[:40]))
        self.gate.approve(code, 'celular')
        for method in ('GET', 'HEAD', 'POST', 'PUT', 'DELETE', 'OPTIONS', 'PATCH', 'TRACE', 'CONNECT', 'PROPFIND'):
            for target in targets:
                answer = self.raw(f'{method} {target} HTTP/1.1\r\nHost: puerta\r\n{waiting}\r\n'.encode())
                self.assertFalse(200 <= self.status(answer) <= 299, (method, target, answer[:40]))
        for target in ('y:/_puerta/', 'x:y:/_puerta/', 'x:/verificar', 'http://x/verificar', '*', 'verificar'):
            self.assertEqual(self.status(self.raw(f'GET {target} HTTP/1.1\r\nHost: puerta\r\n{waiting}\r\n'.encode())), 400, target)
        for target in (puerta.CHECK, puerta.CHECK + '?x=1'):
            self.assertEqual(self.status(self.raw(f'GET {target} HTTP/1.1\r\nHost: puerta\r\n{waiting}\r\n'.encode())), 200, target)

    def test_other_paths_and_methods_are_refused(self):
        key, code, _ = self.pair()
        self.gate.approve(code, 'celular')
        for path in ('/', '/verificar/', '/verificar/x', '/_puerta/verificar', '/_puerta/x', '/_PUERTA/', '/api/state'):
            self.assertEqual(self.ask(path, key=key)[0].status, 404, path)
        for method in ('POST', 'PUT', 'PATCH', 'DELETE', 'OPTIONS'):
            for path in (puerta.CHECK, puerta.PAGE):
                response, _ = self.ask(path, method=method, key=key)
                self.assertEqual(response.status, 405, (method, path))
        response, body = self.ask(puerta.CHECK, method='HEAD', key=key)
        self.assertEqual((response.status, body), (200, ''))
        self.assertEqual(self.ask(puerta.CHECK, method='HEAD')[0].status, 401)
        response, body = self.ask(puerta.PAGE, method='HEAD')
        self.assertEqual((response.status, body), (403, ''))

    def test_when_too_many_wait_the_page_says_so(self):
        for number in range(puerta.PENDING_MAX):
            self.gate.waiting(None, f'198.51.100.{number + 1}')
        response, body = self.ask(puerta.PAGE, headers={'X-Forwarded-For': '203.0.113.7'})
        self.assertEqual((response.status, response.getheader('Retry-After')), (429, '60'))
        self.assertIsNone(response.getheader('Set-Cookie'))
        self.assertIn('demasiados aparatos', body)

    def test_the_origin_shown_to_the_terminal_is_the_last_address_the_proxy_wrote(self):
        self.ask(puerta.PAGE, headers={'X-Forwarded-For': '1.2.3.4, 203.0.113.7'})
        self.ask(puerta.PAGE, headers={'X-Forwarded-For': '<script>'})
        self.ask(puerta.PAGE)
        self.assertEqual(sorted(item.origen for item in self.gate.pending.values()), ['203.0.113.7', 'desconocido', 'desconocido'])

    def test_each_connection_serves_one_request_and_no_request_may_carry_data(self):
        key, code, _ = self.pair()
        self.gate.approve(code, 'celular')
        cookie = f'Cookie: {puerta.COOKIE}={key}\r\n'
        check = 'GET /verificar HTTP/1.1\r\nHost: puerta\r\n'
        # What a body carries is never read as a second request, with or without permission.
        hidden = (check + cookie + '\r\n').encode()
        for first in (f'GET /_puerta/ HTTP/1.1\r\nHost: puerta\r\nContent-Length: {len(hidden)}\r\n\r\n',
                      f'{check}Content-Length: {len(hidden)}\r\n\r\n',
                      f'{check}{cookie}Content-Length: {len(hidden)}\r\n\r\n',
                      f'{check}{cookie}Content-Length: 0\r\nContent-Length: {len(hidden)}\r\n\r\n',
                      f'{check}{cookie}Content-Length: \r\nContent-Length: {len(hidden)}\r\n\r\n',
                      f'{check}{cookie}Content-Length: {len(hidden)}\r\nContent-Length: 0\r\n\r\n',
                      f'{check}Transfer-Encoding: chunked\r\n\r\n{len(hidden):x}\r\n'):
            answer = self.raw(first.encode() + hidden)
            self.assertEqual(answer.count('HTTP/1.'), 1, first)
            self.assertEqual(self.status(answer), 400, first)
            self.assertIn('Connection: close', answer)
        # Two requests sent together: only the first is answered and the connection ends.
        answer = self.raw((check + '\r\n').encode() + hidden)
        self.assertEqual((answer.count('HTTP/1.'), self.status(answer)), (1, 401))
        answer = self.raw(hidden + (check + '\r\n').encode())
        self.assertEqual((answer.count('HTTP/1.'), self.status(answer)), (1, 200))
        for path, expected in ((puerta.CHECK, 200), (puerta.PAGE, 302), ('/nada', 404)):
            response, _ = self.ask(path, key=key, headers={'Content-Length': '0'})
            self.assertEqual((response.status, response.getheader('Connection')), (expected, 'close'), path)

    def test_a_page_that_asks_for_many_things_at_once_is_not_kept_waiting(self):
        self.assertGreaterEqual(puerta.DoorServer.request_queue_size, 128)
        key, code, _ = self.pair()
        self.gate.approve(code, 'celular')
        answers = []

        def visit(approved):
            try:
                answers.append((approved, self.ask(puerta.CHECK, key=key if approved else OTHER)[0].status))
            except OSError as error:
                answers.append((approved, repr(error)))

        threads = [threading.Thread(target=visit, args=(number % 2 == 0,)) for number in range(60)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join()
        self.assertEqual(sorted(set(answers)), [(False, 401), (True, 200)])
        self.assertEqual(len(answers), 60)

    def test_a_failure_inside_closes_the_door(self):
        key, code, _ = self.pair()
        self.gate.approve(code, 'celular')
        for answer in (RuntimeError('roto'), 1, 'sí', None):
            effect = {'side_effect': answer} if isinstance(answer, Exception) else {'return_value': answer}
            with patch.object(self.gate, 'allowed', **effect):
                response, _ = self.ask(puerta.CHECK, key=key)
                self.assertIn(response.status, (401, 500), answer)
        for answer in (RuntimeError('roto'), None, 'pasa', puerta.Visit('otra'), puerta.Visit('espera')):
            effect = {'side_effect': answer} if isinstance(answer, Exception) else {'return_value': answer}
            with patch.object(self.gate, 'visit', **effect):
                response, _ = self.ask(puerta.PAGE, key=key)
                self.assertIn(response.status, (429, 500), answer)
        self.assertEqual(self.ask(puerta.CHECK, key=key)[0].status, 200)


class TerminalWordsTests(unittest.TestCase):
    def test_the_number_may_be_typed_as_it_is_shown(self):
        self.assertEqual(puerta.code_and_name(['12345678']), ('12345678', ''))
        self.assertEqual(puerta.code_and_name(['12345678', 'mi', 'celular']), ('12345678', 'mi celular'))
        self.assertEqual(puerta.code_and_name(['1234', '5678']), ('12345678', ''))
        self.assertEqual(puerta.code_and_name(['1234', '5678', 'mi', 'celular']), ('12345678', 'mi celular'))
        self.assertEqual(puerta.code_and_name(['1234', 'cel']), ('1234', 'cel'))

    def test_an_answer_that_is_not_understood_is_never_shown_as_done(self):
        for reply in ({}, {'otra': 1}):
            with contextlib.redirect_stdout(io.StringIO()) as shown:
                self.assertEqual(puerta.show(reply), 1)
            self.assertIn('no se entiende', shown.getvalue())
        with contextlib.redirect_stdout(io.StringIO()) as shown:
            self.assertEqual(puerta.show({'error': 'Ese número no está esperando permiso.'}), 1)
            self.assertEqual(puerta.show({'aprobado': {'id': 'a1b2c3', 'nombre': 'celular'}}), 0)
        self.assertIn('Permiso dado: celular', shown.getvalue())


@unittest.skipUnless(hasattr(socketserver, 'ThreadingUnixStreamServer'), 'la terminal de la puerta usa sockets de archivo')
class TerminalTests(Folder):
    def test_the_terminal_approves_lists_and_removes_through_a_private_socket(self):
        door = self.door()
        path = str(self.folder / 'admin.sock')
        server = puerta.admin_server(path, door)
        threading.Thread(target=server.serve_forever, daemon=True).start()
        self.addCleanup(server.server_close)
        self.addCleanup(server.shutdown)
        self.assertEqual(stat.S_IMODE(os.stat(path).st_mode), 0o600)
        key, item = door.waiting(None, '203.0.113.7')
        self.assertEqual(puerta.ask(path, {'orden': 'pendientes'})['pendientes'][0]['codigo'], item.codigo)
        self.assertIn('no está esperando', puerta.ask(path, {'orden': 'aprobar', 'codigo': '1'})['error'])
        with contextlib.redirect_stdout(io.StringIO()) as shown:
            self.assertEqual(puerta.main(['--admin', path, 'aprobar', item.codigo[:4], item.codigo[4:], 'mi', 'celular']), 0)
        self.assertIn('Permiso dado: mi celular', shown.getvalue())
        self.assertTrue(door.allowed(key))
        listed = puerta.ask(path, {'orden': 'lista'})['aparatos']
        self.assertEqual([device['nombre'] for device in listed], ['mi celular'])
        self.assertEqual(puerta.ask(path, {'orden': 'quitar', 'id': listed[0]['id']})['quitado']['nombre'], 'mi celular')
        self.assertFalse(door.allowed(key))
        self.assertIn('error', puerta.ask(path, {'orden': 'abrir'}))
        with contextlib.redirect_stdout(io.StringIO()) as shown:
            self.assertEqual(puerta.main(['--admin', path, 'lista']), 0)
            self.assertEqual(puerta.main(['--admin', path, 'quitar', 'zzzzzz']), 1)
            self.assertEqual(puerta.main(['--admin', str(self.folder / 'no-hay.sock'), 'lista']), 1)
        self.assertIn('no contesta', shown.getvalue())

    def test_a_terminal_cut_short_or_without_permission_says_so(self):
        path = str(self.folder / 'mudo.sock')

        class Mute(socketserver.StreamRequestHandler):
            def handle(self):
                self.rfile.readline()
                self.wfile.write(b'{"aprobado": {"id"')  # Hangs up in the middle of the answer.

        server = socketserver.ThreadingUnixStreamServer(path, Mute)
        threading.Thread(target=server.serve_forever, daemon=True).start()
        self.addCleanup(server.server_close)
        self.addCleanup(server.shutdown)
        with contextlib.redirect_stdout(io.StringIO()) as shown:
            self.assertEqual(puerta.main(['--admin', path, 'aprobar', '12345678']), 1)
        self.assertIn('no se entiende', shown.getvalue())
        with patch.object(puerta, 'ask', side_effect=PermissionError('sin permiso')):
            with contextlib.redirect_stdout(io.StringIO()) as shown:
                self.assertEqual(puerta.main(['--admin', path, 'lista']), 1)
        self.assertIn('sudo', shown.getvalue())


if __name__ == '__main__':
    unittest.main()
