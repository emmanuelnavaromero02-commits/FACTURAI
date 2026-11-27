import asyncio
import base64
import email
import hashlib
import json
import logging
import os
import shutil
import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from email import policy

from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import ec
from fastapi import FastAPI
from fastapi.responses import PlainTextResponse
from fastapi.testclient import TestClient
from PIL import Image

from facturador import avisos as avisos_module
from facturador.auth import write_private
from facturador.avisos import Avisos, CorreoPropio, guardar_ajustes, limpiar
from facturador.avisos_push import PushDevices, device_id, public_key
from facturador.avisos_rutas import PIEZAS, avisos_router, piezas_router
from facturador.avisos_vigilante import VigilanteAvisos
from facturador.db import Store
from facturador.invoice_mail import AMBIGUOUS
from facturador.models import Receipt
from facturador.permisos_rutas import permisos_router
from facturador.registro_datos import datos_de_registro, registro_router

WEB = Path(__file__).resolve().parent.parent / 'web'
# Fictitious personal data that must never appear in a notice.
RFC = 'XAXX010101000'
OWN_EMAIL = 'persona@example.com'
ACCOUNT_EMAIL = 'cuenta@example.com'
MERCHANT_EMAIL = 'ventas@comercio.example.com'
PHONE = '5512345678'
FOLIO = 'FOLIO-998877'
AMOUNT = '1234.56'
SECRETS = [RFC, OWN_EMAIL, ACCOUNT_EMAIL, MERCHANT_EMAIL, PHONE, '998877', AMOUNT, '1,234.56', 'COD-77441']
PROFILE = {'rfc': RFC, 'name': 'PERSONA FICTICIA', 'postal_code': '76146', 'regime': '612', 'cfdi_use': 'G03',
           'email': OWN_EMAIL}


def b64(data):
    return base64.urlsafe_b64encode(data).decode().rstrip('=')


def client_keys():
    private = ec.generate_private_key(ec.SECP256R1())
    point = private.public_key().public_bytes(serialization.Encoding.X962, serialization.PublicFormat.UncompressedPoint)
    return private, b64(point), b64(os.urandom(16))


class FakePush:
    """Double of pywebpush.webpush: records each call and can answer like the push service."""

    def __init__(self, status=201, error=None):
        self.calls = []
        self.status = status
        self.error = error

    def __call__(self, **kwargs):
        self.calls.append(kwargs)
        if self.error:
            raise self.error
        if self.status > 202:
            from pywebpush import WebPushException

            class Response:
                status_code = self.status
                reason = 'Gone'
                text = ''
            raise WebPushException('Push failed', response=Response())
        return None

    def payloads(self):
        return [json.loads(call['data']) for call in self.calls]


class FakeSender:
    channel = 'smtp'

    def __init__(self, address=ACCOUNT_EMAIL, fail=False):
        self.address = address
        self.fail = fail
        self.sent = []
        self.recipient = None

    def prepare(self, recipient):
        if self.fail:
            raise ValueError('sin conexión')
        self.recipient = recipient

    def deliver(self, data):
        message = email.message_from_bytes(data, policy=policy.default)
        self.sent.append((self.recipient, message['Subject'] + '\n' + message.get_content()))
        return ''

    def close(self):
        pass


class Base(unittest.TestCase):
    def setUp(self):
        folder = tempfile.TemporaryDirectory()
        self.addCleanup(folder.cleanup)
        self.root = Path(folder.name)
        self.store = Store(self.root)
        self.push = FakePush()
        self.sender = FakeSender()
        self.devices = PushDevices(self.store, send=self.push)
        self.mail = CorreoPropio(self.root, senders=lambda: self.sender)
        self.avisos = Avisos(self.store, push=self.devices, correo=self.mail, panel_url='http://127.0.0.1:8765')
        write_private(self.root / 'profile.json', PROFILE)
        self.count = 0

    def ticket(self, merchant='Cafetería Luna', status=None):
        self.count += 1
        source = self.root / 'originales' / f'ticket-{self.count}.png'
        source.parent.mkdir(parents=True, exist_ok=True)
        Image.new('RGB', (8, 8), tuple(hashlib.sha256(str(self.count).encode()).digest()[:3])).save(source)
        row, _ = self.store.add(str(source), hashlib.sha256(source.read_bytes()).hexdigest())
        receipt = Receipt(provider='samara', merchant=merchant, amount=AMOUNT, date='2026-11-13',
                          ticket_number=FOLIO + str(self.count), billing_codes=['COD-77441'], payment_code='04')
        self.store.save_receipt(row['id'], receipt, ready=True)
        if status:
            self.store.update(row['id'], status=status)
        return row['id']

    def subscribe(self, name='Chrome en Android'):
        _, point, auth = client_keys()
        endpoint = 'https://fcm.googleapis.com/fcm/send/' + b64(os.urandom(12))
        return self.devices.subscribe(endpoint, point, auth, name), endpoint

    def all_avisos(self):
        with self.store.connect() as db:
            return [dict(r) for r in db.execute('SELECT * FROM avisos ORDER BY id')]


class KeysAndDevices(Base):
    def test_keys_are_created_once_and_stay_private(self):
        first = public_key(self.root)
        second = public_key(self.root)
        self.assertEqual(first, second)
        self.assertEqual(len(base64.urlsafe_b64decode(first + '=' * (-len(first) % 4))), 65)
        stored = json.loads((self.root / 'auth' / 'avisos' / 'vapid.json').read_text(encoding='utf-8'))
        self.assertIn('PRIVATE KEY', stored['private_pem'])
        self.assertEqual(stored['public'], first)
        # Every route answer lacks the private key, including the settings and device list.
        client = routes_client(self)
        self.subscribe()
        bodies = [client.get(path).text for path in ('/api/avisos/llave', '/api/avisos/ajustes', '/api/avisos',
                                                      '/api/avisos/dispositivos')]
        secret = stored['private_pem'].splitlines()[1]
        for body in bodies:
            self.assertNotIn(secret, body)
            self.assertNotIn('private', body)
        # A new process on the same folder reads the same pair instead of creating another.
        self.assertEqual(PushDevices(self.store).root, self.root)
        self.assertEqual(public_key(self.root), first)

    def test_private_key_never_reaches_the_logs(self):
        self.subscribe()
        public_key(self.root)
        self.push.error = RuntimeError('caída')
        pem = json.loads((self.root / 'auth' / 'avisos' / 'vapid.json').read_text(encoding='utf-8'))['private_pem']
        with self.assertLogs('facturador', level='DEBUG') as logs:
            logging.getLogger('facturador').debug('inicio')
            self.devices.send({'titulo': 'x'})
        self.assertNotIn(pem.splitlines()[1], '\n'.join(logs.output))

    def test_subscribe_validates_endpoint_and_keys(self):
        _, point, auth = client_keys()
        for endpoint in ('http://fcm.googleapis.com/x', 'https://ejemplo.com/push', 'https://fcm.googleapis.com.evil.com/x',
                         'https://user:pw@fcm.googleapis.com/x', 'https://fcm.googleapis.com:8443/x'):
            with self.assertRaises(ValueError):
                self.devices.subscribe(endpoint, point, auth)
        with self.assertRaises(ValueError):
            self.devices.subscribe('https://fcm.googleapis.com/x', b64(b'\x04' + b'1' * 10), auth)
        with self.assertRaises(ValueError):
            self.devices.subscribe('https://fcm.googleapis.com/x', point, b64(b'corto'))
        for endpoint in ('https://updates.push.services.mozilla.com/wpush/v2/a', 'https://web.push.apple.com/b',
                         'https://wns2-par02p.notify.windows.com/w/?token=c'):
            self.devices.subscribe(endpoint, point, auth, 'Equipo <b>')
        self.assertEqual(self.devices.count(), 3)
        self.assertTrue(all(d['nombre'] == '' for d in self.devices.devices()))

    def test_subscribe_again_updates_and_remove(self):
        device, endpoint = self.subscribe()
        _, point, auth = client_keys()
        again = self.devices.subscribe(endpoint, point, auth, '')
        self.assertEqual(again['id'], device['id'])
        self.assertEqual(self.devices.count(), 1)
        self.assertEqual(self.devices.devices()[0]['nombre'], 'Chrome en Android')
        self.assertNotIn('endpoint', self.devices.devices()[0])
        self.assertTrue(self.devices.remove(endpoint=endpoint))
        self.assertFalse(self.devices.remove(device['id']))
        self.assertFalse(self.devices.remove('../../x'))

    def test_expired_subscriptions_are_removed_and_other_failures_counted(self):
        gone, _ = self.subscribe('A')
        self.push.status = 410
        result = self.devices.send({'titulo': 'x'})
        self.assertEqual(result, {'enviados': 0, 'fallidos': 0, 'quitados': 1})
        self.assertEqual(self.devices.count(), 0)
        self.subscribe('B')
        self.push.status = 404
        self.assertEqual(self.devices.send({'titulo': 'x'})['quitados'], 1)
        kept, _ = self.subscribe('C')
        self.push.status = 500
        self.assertEqual(self.devices.send({'titulo': 'x'})['fallidos'], 1)
        self.assertEqual(self.devices.devices()[0]['fallos'], 1)
        self.push.status = 201
        self.assertEqual(self.devices.send({'titulo': 'x'})['enviados'], 1)
        self.assertEqual(self.devices.devices()[0]['fallos'], 0)
        self.assertNotEqual(gone['id'], kept['id'])

    def test_device_limit_keeps_newest(self):
        for index in range(23):
            self.subscribe(f'Equipo {index}')
        self.assertEqual(self.devices.count(), 20)

    def test_real_web_push_encryption_with_a_simulated_push_service(self):
        """pywebpush itself encrypts and signs; only the HTTP post is simulated."""
        import http_ece
        import requests
        private, point, auth = client_keys()
        endpoint = 'https://fcm.googleapis.com/fcm/send/prueba'
        devices = PushDevices(self.store)  # the real pywebpush.webpush
        devices.subscribe(endpoint, point, auth, 'Chrome en Windows')
        seen = []

        class Answer:
            status_code = 201
            reason = 'Created'
            text = ''
            headers = {}

        def fake_post(session, url, data=None, headers=None, timeout=None, **_):
            seen.append({'url': url, 'data': data, 'headers': headers, 'redirects': session.max_redirects})
            return Answer()

        with patch.object(requests.Session, 'post', fake_post):
            result = devices.send({'titulo': 'Te necesito', 'cuerpo': 'Cafetería Luna: Inicia sesión', 'liga': '/#bandeja'},
                                  urgent=True)
        self.assertEqual(result['enviados'], 1)
        self.assertEqual(seen[0]['url'], endpoint)
        self.assertEqual(seen[0]['redirects'], 0)
        headers = {k.lower(): v for k, v in seen[0]['headers'].items()}
        self.assertTrue(headers['authorization'].startswith('vapid t='))
        self.assertIn('k=' + public_key(self.root), headers['authorization'])
        self.assertEqual(headers['urgency'], 'high')
        clear = http_ece.decrypt(seen[0]['data'], private_key=private,
                                 auth_secret=base64.urlsafe_b64decode(auth + '=='), version='aes128gcm')
        self.assertEqual(json.loads(clear)['titulo'], 'Te necesito')


class Content(Base):
    def test_notice_has_no_personal_data(self):
        dirty = f'OXXO {RFC} tel {PHONE} {MERCHANT_EMAIL} ${AMOUNT} folio 998877 https://oxxo.example.com/f'
        key = self.ticket(dirty)
        guardar_ajustes(self.root, {'canales': {'correo': True}})
        self.subscribe()
        with patch('facturador.correo_envio.can_send', return_value=True):
            aviso = self.avisos.avisar('te_necesito', clave='x1', job_id=key, comercio=dirty,
                                       motivo=f'Inicia sesión con {OWN_EMAIL} y el código 123456')
        self.assertTrue(aviso['comercio'].startswith('OXXO'))
        texts = [json.dumps(self.all_avisos(), ensure_ascii=False), json.dumps(self.push.payloads(), ensure_ascii=False)]
        texts += [body for _, body in self.sender.sent]
        self.assertEqual(len(self.sender.sent), 1)
        for text in texts:
            for value in SECRETS + ['123456', 'oxxo.example.com']:
                self.assertNotIn(value, text)
        self.assertEqual(aviso['liga'], f'/?ticket={key}#bandeja')
        self.assertEqual(self.push.payloads()[0]['liga'], f'/?ticket={key}#bandeja')

    def test_limpiar_keeps_ordinary_names(self):
        self.assertEqual(limpiar('  7-Eleven   Centro ', 40), '7-Eleven Centro')
        self.assertEqual(limpiar('Café "La Luna" <script>', 40), 'Café La Luna script')
        self.assertEqual(limpiar(f'{RFC}', 40), '')

    def test_once_per_key(self):
        key = self.ticket()
        self.assertIsNotNone(self.avisos.avisar('factura_lista', clave='k', job_id=key, motivo='Lista'))
        self.assertIsNone(self.avisos.avisar('factura_lista', clave='k', job_id=key, motivo='Lista'))
        self.assertEqual(len(self.all_avisos()), 1)


class SettingsAndChannels(Base):
    def test_type_off_records_nothing(self):
        guardar_ajustes(self.root, {'tipos': {'factura_lista': False}})
        self.subscribe()
        self.assertIsNone(self.avisos.avisar('factura_lista', clave='a', motivo='Lista'))
        self.assertEqual(self.all_avisos(), [])
        self.assertEqual(self.push.calls, [])

    def test_push_channel_off(self):
        self.subscribe()
        guardar_ajustes(self.root, {'canales': {'navegador': False}})
        self.assertIsNotNone(self.avisos.avisar('te_necesito', clave='a', motivo='Hola'))
        self.assertEqual(self.push.calls, [])
        self.assertEqual(self.avisos.sin_leer(), 1)

    def test_panel_channel_off_hides_but_still_dedupes(self):
        self.subscribe()
        guardar_ajustes(self.root, {'canales': {'panel': False}})
        self.assertIsNotNone(self.avisos.avisar('te_necesito', clave='a', motivo='Hola'))
        self.assertEqual(self.avisos.lista()['avisos'], [])
        self.assertEqual(len(self.push.calls), 1)
        self.assertIsNone(self.avisos.avisar('te_necesito', clave='a', motivo='Hola'))

    def test_email_is_off_by_default_and_only_to_own_address(self):
        with patch('facturador.correo_envio.can_send', return_value=True):
            self.avisos.avisar('te_necesito', clave='a', motivo='Hola')
            self.assertEqual(self.sender.sent, [])
            guardar_ajustes(self.root, {'canales': {'correo': True}})
            key = self.ticket()
            self.avisos.avisar('te_necesito', clave='b', job_id=key, motivo='Hola')
            # No mailbox account saved: the profile address is the only own address available.
            self.assertEqual(self.sender.sent[-1][0], OWN_EMAIL)
            from facturador import buzon
            buzon.save_account(self.root, {'imap_host': 'imap.example.com', 'imap_port': 993, 'user': ACCOUNT_EMAIL,
                                           'address': ACCOUNT_EMAIL, 'smtp_host': 'smtp.example.com', 'smtp_port': 465},
                               'clave-ficticia')
            self.avisos.avisar('te_necesito', clave='c', motivo='Hola')
            self.assertEqual(self.sender.sent[-1][0], ACCOUNT_EMAIL)
            guardar_ajustes(self.root, {'correo_destino': 'perfil'})
            self.avisos.avisar('te_necesito', clave='d', motivo='Hola')
            self.assertEqual(self.sender.sent[-1][0], OWN_EMAIL)
        self.assertEqual({to for to, _ in self.sender.sent}, {OWN_EMAIL, ACCOUNT_EMAIL})
        self.assertIn('http://127.0.0.1:8765/?ticket=' + key + '#bandeja', self.sender.sent[0][1])
        with self.assertRaises(ValueError):
            guardar_ajustes(self.root, {'correo_destino': MERCHANT_EMAIL})

    def test_no_email_without_a_sending_account(self):
        guardar_ajustes(self.root, {'canales': {'correo': True}})
        self.avisos.avisar('te_necesito', clave='a', motivo='Hola')
        self.assertEqual(self.sender.sent, [])

    def test_email_hourly_cap(self):
        guardar_ajustes(self.root, {'canales': {'correo': True}})
        with patch('facturador.correo_envio.can_send', return_value=True):
            for index in range(avisos_module.EMAILS_PER_HOUR + 3):
                self.avisos.avisar('te_necesito', clave=f'k{index}', motivo='Hola')
        self.assertEqual(len(self.sender.sent), avisos_module.EMAILS_PER_HOUR)

    def test_channel_failures_never_raise(self):
        self.subscribe()
        guardar_ajustes(self.root, {'canales': {'correo': True}})
        self.push.error = RuntimeError('servicio caído')
        self.sender.fail = True
        with patch('facturador.correo_envio.can_send', return_value=True):
            aviso = self.avisos.avisar('te_necesito', clave='a', motivo='Hola')
        self.assertIsNotNone(aviso)
        self.assertEqual(self.avisos.sin_leer(), 1)
        with patch.object(self.devices, 'send', side_effect=RuntimeError('roto')), \
                patch.object(self.mail, 'enviar', side_effect=RuntimeError('roto')):
            self.assertIsNotNone(self.avisos.avisar('te_necesito', clave='b', motivo='Hola'))
        with patch('facturador.avisos.ajustes', side_effect=RuntimeError('disco')):
            self.assertIsNone(self.avisos.avisar('te_necesito', clave='c', motivo='Hola'))

    def test_settings_validation(self):
        with self.assertRaises(ValueError):
            guardar_ajustes(self.root, {'tipos': {'nuevo': True}})
        with self.assertRaises(ValueError):
            guardar_ajustes(self.root, {'canales': {'correo': 'si'}})
        (self.root / 'avisos.json').write_text('{roto', encoding='utf-8')
        self.assertTrue(avisos_module.ajustes(self.root)['tipos']['te_necesito'])


class Watcher(Base):
    def setUp(self):
        super().setUp()
        self.subscribe()
        self.watcher = VigilanteAvisos(self.store, self.avisos)

    def kinds(self):
        return [(a['tipo'], a['motivo']) for a in self.all_avisos()]

    def test_install_takes_history_as_notified(self):
        old = self.ticket(status='verified')
        self.ticket(status='blocked')
        self.store.enter_intervention(old, 'captcha', 'x', True)
        self.store.event(old, 'question', '¿Cuál?')
        self.assertEqual(self.watcher.revisar(), [])
        self.assertEqual(self.watcher.revisar(), [])
        self.assertEqual(self.all_avisos(), [])
        self.assertEqual(self.push.calls, [])

    def test_each_change_once_and_not_after_restart(self):
        self.watcher.revisar()
        key = self.ticket()
        self.store.enter_intervention(key, 'captcha', 'Completa la verificación.', False)
        first = self.watcher.revisar()
        self.assertEqual([(a['tipo'], a['titulo']) for a in first], [('te_necesito', 'Te necesito')])
        self.assertIn('no soy un robot', first[0]['motivo'])
        self.assertEqual(self.watcher.revisar(), [])
        restarted = VigilanteAvisos(self.store, Avisos(self.store, push=self.devices, correo=self.mail))
        self.assertEqual(restarted.revisar(), [])
        self.assertEqual(len(self.push.calls), 1)

    def test_every_kind_of_change(self):
        self.watcher.revisar()
        a, b, c, d, e, f = (self.ticket(name) for name in ('Uno', 'Dos', 'Tres', 'Cuatro', 'Cinco', 'Seis'))
        self.store.enter_intervention(a, 'login', 'x', True)
        self.store.enter_intervention(b, 'issue', 'x', False)
        self.store.enter_intervention(c, 'operator', 'x', True)  # the person is already operating it
        self.store.event(c, 'question', '¿Qué número de tienda?')
        self.store.event(d, 'gmail_recovery', AMBIGUOUS)
        self.store.event(d, 'gmail_recovery', 'Aún no llega.')
        self.store.update(e, status='verified')
        self.store.update(f, status='blocked')
        self.store.enter_intervention(a, 'attachment', 'x', True)
        found = {(x['comercio'], x['tipo'], x['motivo']) for x in self.watcher.revisar()}
        self.assertEqual(found, {
            ('Uno', 'te_necesito', 'Inicia sesión en el portal'),
            ('Dos', 'te_necesito', 'Aprueba la emisión de la factura'),
            ('Tres', 'te_necesito', 'Contesta una pregunta en el chat'),
            ('Cuatro', 'correo_ambiguo', 'Llegaron varias facturas por correo: elige la de este ticket'),
            ('Cinco', 'factura_lista', 'La factura quedó comprobada con su PDF y XML'),
            ('Seis', 'ticket_detenido', 'El trámite se detuvo y necesita tu revisión'),
            ('Uno', 'te_necesito', 'Adjunta el archivo que pide el portal'),
        })
        # Status needs_user is announced by its event, not again by the status change.
        self.assertEqual(self.watcher.revisar(), [])
        question = [x for x in self.all_avisos() if x['comercio'] == 'Tres'][0]
        self.assertEqual(question['liga'], f'/?ticket={c}#chat')
        self.store.update(f, status='ready')
        self.store.update(f, status='uncertain')
        stopped = self.watcher.revisar()
        self.assertEqual([x['motivo'] for x in stopped], ['No se sabe si se emitió: revísalo antes de repetir'])
        self.store.update(f, status='blocked')
        self.assertEqual(len(self.watcher.revisar()), 1)
        self.store.update(e, status='issued_waiting_files')
        self.store.update(e, status='verified')
        self.assertEqual(self.watcher.revisar(), [])  # a verified invoice is announced only once

    def test_historical_rows_are_ignored(self):
        self.watcher.revisar()
        self.store.import_history([{'key': 'h1', 'receipt': {'merchant': 'Viejo', 'ticket_number': 'A1'},
                                    'note': 'n', 'uuid': ''}])
        self.store.event('history_h1', 'needs_user', 'captcha')
        self.assertEqual(self.watcher.revisar(), [])

    def test_whatsapp_conversations_and_link(self):
        self.watcher.revisar()
        key = self.ticket('Tienda Sol')
        waiting = [{'job_id': key, 'state': 'needs_person', 'detail': 'Pide una foto', 'updated': 'x'}]
        self.assertEqual(self.watcher.revisar_conversaciones(waiting), [])  # installs the baseline
        self.assertEqual(self.watcher.revisar_conversaciones([]), [])
        found = self.watcher.revisar_conversaciones(waiting)
        self.assertEqual([(x['tipo'], x['motivo']) for x in found], [('te_necesito', 'El comercio pide algo por WhatsApp')])
        self.assertEqual(self.watcher.revisar_conversaciones(waiting), [])
        restarted = VigilanteAvisos(self.store, self.avisos)
        self.assertEqual(restarted.revisar_conversaciones(waiting), [])
        limit = [{**waiting[0], 'state': 'limit', 'detail': 'Tope'}]
        self.assertEqual(len(restarted.revisar_conversaciones(limit)), 1)

        self.assertIsNone(self.watcher.revisar_vinculo('qr'))      # first state seen: baseline
        self.assertIsNone(self.watcher.revisar_vinculo('listo'))
        self.assertIsNone(self.watcher.revisar_vinculo('sin_abrir'))  # unknown states change nothing
        lost = self.watcher.revisar_vinculo('sesion_cerrada')
        self.assertEqual((lost['tipo'], lost['titulo'], lost['liga']), ('whatsapp', 'WhatsApp se desvinculó', '/#ajustes'))
        self.assertIsNone(self.watcher.revisar_vinculo('qr'))
        self.assertIsNone(VigilanteAvisos(self.store, self.avisos).revisar_vinculo('qr'))
        self.assertIsNone(self.watcher.revisar_vinculo('listo'))
        self.assertIsNotNone(self.watcher.revisar_vinculo('qr'))

    def test_whatsapp_through_the_routes_object(self):
        key = self.ticket()

        class Routes:
            state = 'listo'
            items = []

            def attention(self):
                return list(self.items)

            async def link_state(self):
                return {'state': self.state, 'message': '', 'ready': self.state == 'listo', 'checked': None}

        routes = Routes()
        watcher = VigilanteAvisos(self.store, self.avisos, whatsapp=lambda: routes)

        async def scenario():
            await watcher.revisar_whatsapp()
            routes.items = [{'job_id': key, 'state': 'needs_person', 'detail': 'd', 'updated': 'u'}]
            routes.state = 'qr'
            watcher._last_link = -10_000
            await watcher.revisar_whatsapp()
            await watcher.revisar_whatsapp()

        asyncio.run(scenario())
        self.assertEqual(sorted(a['tipo'] for a in self.all_avisos()), ['te_necesito', 'whatsapp'])

    def test_disabled_type_is_not_repeated_later(self):
        self.watcher.revisar()
        guardar_ajustes(self.root, {'tipos': {'factura_lista': False}})
        key = self.ticket()
        self.store.update(key, status='verified')
        self.assertEqual(self.watcher.revisar(), [])
        guardar_ajustes(self.root, {'tipos': {'factura_lista': True}})
        self.assertEqual(self.watcher.revisar(), [])

    def test_failure_in_delivery_keeps_watching(self):
        self.watcher.revisar()
        self.push.error = RuntimeError('caído')
        key = self.ticket()
        self.store.enter_intervention(key, 'captcha', 'x', False)
        self.assertEqual(len(self.watcher.revisar()), 1)
        self.store.update(key, status='blocked')
        self.assertEqual(len(self.watcher.revisar()), 1)

    def test_background_task_runs_and_stops(self):
        async def scenario():
            watcher = VigilanteAvisos(self.store, self.avisos, tick=0.05)
            watcher.start()
            await asyncio.sleep(0.2)
            key = self.ticket()
            self.store.enter_intervention(key, 'login', 'x', True)
            for _ in range(60):
                await asyncio.sleep(0.05)
                if self.all_avisos():
                    break
            await watcher.close()
            return watcher.task

        self.assertIsNone(asyncio.run(scenario()))
        self.assertEqual(len(self.all_avisos()), 1)

    def test_undelivered_recent_notices_are_sent_after_restart(self):
        key = self.ticket()
        with self.store.connect() as db:
            fresh = self.avisos.registrar(db, 'te_necesito', clave='r1', job_id=key, motivo='Hola')
            old = self.avisos.registrar(db, 'te_necesito', clave='r2', job_id=key, motivo='Viejo')
            db.execute("UPDATE avisos SET creado='2020-01-01T00:00:00+00:00' WHERE id=?", (old['id'],))
        self.avisos.pendientes_de_entrega()
        self.assertEqual([p['id'] for p in self.push.payloads()], [fresh['id']])
        self.avisos.pendientes_de_entrega()
        self.assertEqual(len(self.push.calls), 1)


def routes_client(case):
    app = FastAPI()

    @app.exception_handler(ValueError)
    async def invalid(request, exc):
        return PlainTextResponse(str(exc), status_code=422)

    app.include_router(avisos_router(case.store, case.avisos))
    app.include_router(permisos_router(case.store))
    app.include_router(registro_router(case.root))
    app.include_router(piezas_router(WEB))
    return TestClient(app)


class Routes(Base):
    def setUp(self):
        super().setUp()
        self.client = routes_client(self)

    def test_list_unread_and_mark_read(self):
        for index in range(3):
            self.avisos.avisar('te_necesito', clave=str(index), motivo='Hola')
        data = self.client.get('/api/avisos').json()
        self.assertEqual(data['sin_leer'], 3)
        self.assertEqual(set(data['avisos'][0]), {'id', 'tipo', 'titulo', 'comercio', 'motivo', 'liga', 'job_id',
                                                  'creado', 'leido'})
        first = data['avisos'][0]['id']
        self.assertEqual(self.client.post('/api/avisos/leidos', json={'ids': [first]}).json(), {'sin_leer': 2})
        self.assertEqual(self.client.get('/api/avisos/sin-leer').json(), {'sin_leer': 2})
        self.assertEqual(self.client.post('/api/avisos/leidos', json={'todos': True}).json(), {'sin_leer': 0})
        self.assertEqual(self.client.post('/api/avisos/leidos', json={'todos': 'si'}).status_code, 422)

    def test_subscribe_remove_test_and_settings(self):
        self.assertEqual(self.client.post('/api/avisos/prueba').status_code, 422)
        _, point, auth = client_keys()
        endpoint = 'https://fcm.googleapis.com/fcm/send/abc'
        answer = self.client.post('/api/avisos/dispositivos',
                                  json={'endpoint': endpoint, 'expirationTime': None,
                                        'keys': {'p256dh': point, 'auth': auth}, 'nombre': 'Chrome en Android'})
        self.assertEqual(answer.status_code, 200)
        self.assertEqual(answer.json()['id'], device_id(endpoint))
        bad = self.client.post('/api/avisos/dispositivos',
                               json={'endpoint': 'https://intranet.example.com/x', 'keys': {'p256dh': point, 'auth': auth}})
        self.assertEqual(bad.status_code, 422)
        test = self.client.post('/api/avisos/prueba').json()
        self.assertEqual(test['enviados'], 1)
        self.assertEqual(self.push.payloads()[0]['titulo'], 'Aviso de prueba')
        self.assertEqual(self.avisos.sin_leer(), 0)  # the test is not listed
        settings = self.client.get('/api/avisos/ajustes').json()
        self.assertEqual(settings['canales'], {'navegador': True, 'correo': False, 'panel': True})
        self.assertFalse(settings['correo_disponible'])
        self.assertNotIn(endpoint, json.dumps(settings))
        changed = self.client.put('/api/avisos/ajustes', json={'tipos': {'factura_lista': False},
                                                              'canales': {'correo': True}}).json()
        self.assertFalse(changed['tipos']['factura_lista'])
        self.assertTrue(changed['canales']['correo'])
        self.assertEqual(self.client.put('/api/avisos/ajustes', json={'tipos': {'factura_lista': 1}}).status_code, 422)
        self.assertEqual(self.client.put('/api/avisos/ajustes', json={'correo_destino': 'otro@x.com'}).status_code, 422)
        removed = self.client.post('/api/avisos/dispositivos/quitar', json={'endpoint': endpoint}).json()
        self.assertEqual((removed['quitado'], removed['dispositivos']), (True, []))
        self.client.post('/api/avisos/dispositivos', json={'endpoint': endpoint, 'keys': {'p256dh': point, 'auth': auth}})
        self.assertTrue(self.client.delete(f'/api/avisos/dispositivos/{device_id(endpoint)}').json()['quitado'])
        key = self.client.get('/api/avisos/llave').json()['llave']
        self.assertEqual(key, public_key(self.root))

    def test_permission_routes_leave_an_audit_event(self):
        listed = self.client.get('/api/permisos').json()
        self.assertTrue(all(item['activo'] is False for item in listed))
        result = self.client.put('/api/permisos/pedir_por_correo', json={'activo': True}).json()
        self.assertTrue(next(i for i in result if i['nombre'] == 'pedir_por_correo')['activo'])
        from facturador.permisos import permitido
        self.assertTrue(permitido(self.root, 'pedir_por_correo'))
        self.client.put('/api/permisos/pedir_por_correo', json={'activo': False})
        self.assertFalse(permitido(self.root, 'pedir_por_correo'))
        with self.store.connect() as db:
            events = [dict(r) for r in db.execute("SELECT job_id,at,kind,detail FROM events WHERE kind='permiso' ORDER BY id")]
        self.assertEqual(len(events), 2)
        self.assertIsNone(events[0]['job_id'])
        self.assertTrue(events[0]['at'])
        self.assertRegex(events[0]['detail'], r'^Permiso pedir_por_correo activado el \d{4}-\d{2}-\d{2} \d{2}:\d{2} UTC\.$')
        self.assertIn('desactivado', events[1]['detail'])
        self.assertEqual(self.client.put('/api/permisos/borrar_todo', json={'activo': True}).status_code, 422)
        self.assertEqual(self.client.put('/api/permisos/pedir_por_correo', json={'activo': 'true'}).status_code, 422)
        # Permission events never reach the notice watcher.
        watcher = VigilanteAvisos(self.store, self.avisos)
        watcher.revisar()
        self.client.put('/api/permisos/pedir_por_whatsapp', json={'activo': True})
        self.assertEqual(watcher.revisar(), [])

    def test_registration_data(self):
        empty = self.client.get('/api/registro/datos').json()
        self.assertEqual(set(empty['datos'].values()), {''})
        values = {'nombres': 'María José', 'apellido_paterno': "O'Neill", 'apellido_materno': 'Pérez-Núñez',
                  'telefono': '+52 (55) 1234-5678', 'fecha_nacimiento': '1990-05-17', 'calle': 'Av. Juárez',
                  'numero_exterior': '120-B', 'numero_interior': 'Depto. 4', 'colonia': 'Centro',
                  'municipio': 'Querétaro', 'estado': 'Querétaro', 'pais': 'México'}
        saved = self.client.put('/api/registro/datos', json=values).json()['datos']
        self.assertEqual(saved['telefono'], '+525512345678')
        self.assertEqual(saved['nombres'], 'María José')
        self.assertEqual(datos_de_registro(self.root), saved)
        self.assertEqual(self.client.put('/api/registro/datos', json={'colonia': 'Roma'}).json()['datos']['nombres'], '')
        for field, value in (('telefono', '12345'), ('fecha_nacimiento', '2999-01-01'), ('fecha_nacimiento', '1990-02-30'),
                             ('nombres', 'Ana3'), ('calle', 'x' * 121), ('colonia', 'Roma\n<script>'),
                             ('numero_exterior', '#$%'), ('pais', 123)):
            answer = self.client.put('/api/registro/datos', json={field: value})
            self.assertEqual(answer.status_code, 422, (field, value))
            self.assertNotIn(str(value), answer.text)
        self.assertEqual(self.client.put('/api/registro/datos', json={'rfc': RFC}).status_code, 422)
        self.assertEqual(self.client.put('/api/registro/datos', content=b'{roto').status_code, 422)
        with self.store.connect() as db:
            self.assertEqual(db.execute('SELECT COUNT(*) FROM events').fetchone()[0], 0)
        (self.root / 'registro.json').write_text('{"telefono": "malo"}', encoding='utf-8')
        self.assertEqual(datos_de_registro(self.root)['telefono'], '')

    def test_panel_pieces(self):
        kinds = {'/sw.js': 'application/javascript', '/avisos.js': 'application/javascript', '/avisos.css': 'text/css',
                 '/manifest.webmanifest': 'application/manifest+json', '/icono-192.png': 'image/png'}
        for path, kind in kinds.items():
            answer = self.client.get(path)
            self.assertEqual(answer.status_code, 200, path)
            self.assertTrue(answer.headers['content-type'].startswith(kind), path)
        manifest = self.client.get('/manifest.webmanifest').json()
        self.assertEqual(manifest['display'], 'standalone')
        for icon in manifest['icons']:
            self.assertEqual(self.client.get(icon['src']).status_code, 200)
            size = int(icon['sizes'].split('x')[0])
            with Image.open(WEB / icon['src'].lstrip('/')) as image:
                self.assertEqual(image.size, (size, size))
        self.assertEqual(set(PIEZAS), {p for p in PIEZAS if (WEB / PIEZAS[p][0]).is_file()})
        self.assertEqual(self.client.get('/sw.js?body=x').content, (WEB / 'sw.js').read_bytes())


class PanelScripts(unittest.TestCase):
    def test_scripts_have_no_cache_and_no_inline_code(self):
        worker = (WEB / 'sw.js').read_text(encoding='utf-8')
        self.assertNotIn("addEventListener('fetch'", worker)
        self.assertNotIn('caches.', worker)
        script = (WEB / 'avisos.js').read_text(encoding='utf-8')
        for unsafe in ('innerHTML', 'eval(', 'new Function', 'setAttribute(\'style', '.style.'):
            self.assertNotIn(unsafe, script)
        # Permission is requested only from the activation button handler.
        self.assertEqual(script.count('Notification.requestPermission'), 1)
        self.assertIn('pantalla de inicio', script)

    @unittest.skipUnless(shutil.which('node'), 'node no está instalado')
    def test_node_check(self):
        for name in ('sw.js', 'avisos.js'):
            result = subprocess.run(['node', '--check', str(WEB / name)], capture_output=True, text=True)
            self.assertEqual(result.returncode, 0, result.stderr)


class WholeApp(unittest.TestCase):
    """The routers inside the real application: token, origin and CSP rules still apply."""

    def test_routes_under_the_panel_guard(self):
        from facturador.app import create_app, BASE
        with tempfile.TemporaryDirectory() as folder:
            app = create_app(Path(folder))
            store = app.state.store
            avisos = Avisos(store, push=PushDevices(store, send=FakePush()), correo=CorreoPropio(store.root))
            app.include_router(avisos_router(store, avisos))
            app.include_router(permisos_router(store))
            app.include_router(registro_router(store.root))
            app.include_router(piezas_router(BASE / 'web'))
            client = TestClient(app)
            self.assertEqual(client.put('/api/permisos/pedir_por_correo', json={'activo': True}).status_code, 403)
            self.assertEqual(client.put('/api/registro/datos', json={}).status_code, 403)
            token = app.state.csrf_token
            ok = client.put('/api/permisos/pedir_por_correo', json={'activo': True}, headers={'x-app-token': token})
            self.assertEqual(ok.status_code, 200)
            foreign = client.post('/api/avisos/leidos', json={'todos': True},
                                  headers={'x-app-token': token, 'origin': 'https://ajeno.example.com'})
            self.assertEqual(foreign.status_code, 403)
            worker = client.get('/sw.js')
            self.assertEqual(worker.status_code, 200)
            self.assertIn("default-src 'self'", worker.headers['content-security-policy'])
            self.assertEqual(client.get('/api/avisos').json()['sin_leer'], 0)


if __name__ == '__main__':
    unittest.main()
