"""The queue asks a merchant without a portal by mail or WhatsApp, under the standing permissions only."""
import asyncio
import hashlib
import io
import json
import tempfile
import time
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock, patch

from PIL import Image

from facturador import buzon, permisos
from facturador.auth import write_private
from facturador.automation import TicketAutomation
from facturador.correo_envio import MailOutbox
from facturador.db import Store
from facturador.ingest import save_input, save_upload
from facturador.models import Profile
from facturador.runner import Runner
from facturador.whatsapp import WhatsAppWeb, whatsapp_router
from facturador.whatsapp_fotos import BASELINE, MisFotos

PROFILE = Profile(rfc='XAXX010101000', name='PERSONA DE PRUEBA', postal_code='12345', regime='612',
                  email='persona@example.com', whatsapp_phone='+525555550100')
MAIL = 'facturas@cafe-luna.example.com'
PHONE = '+52 55 5555 0188'
TEXT = 'CAFE LUNA\nFOLIO PRUEBA-A\nTOTAL 40.50\nSolicita tu factura por correo o WhatsApp'


def extracted(**changes):
    fields = dict(provider='unknown', merchant='Café Luna', is_receipt=True, amount='40.50',
                  date='2026-06-15', ticket_number='PRUEBA-A', billing_codes=[], payment_code='28',
                  payment_evidence='Pago con tarjeta de débito', details='', uncertainties=[], billing_url=None,
                  issuer_rfc=None, merchant_whatsapp=None, merchant_email=None)
    return SimpleNamespace(**{**fields, **changes})


def png(name):
    output = io.BytesIO()
    Image.new('RGB', (12, 12), tuple(hashlib.sha256(name.encode()).digest()[:3])).save(output, 'PNG')
    return output.getvalue()


class Sender:
    """Mail sender double: records what would leave, optionally refusing before anything is sent."""
    channel = 'smtp'
    address = 'persona@example.com'

    def __init__(self):
        self.refuse = ''
        self.prepared, self.sent = [], []

    def prepare(self, recipient):
        self.prepared.append(recipient)
        if self.refuse:
            raise ValueError(self.refuse)

    def deliver(self, data):
        self.sent.append(data)
        return ''

    def abort(self):
        pass

    def close(self):
        pass


class DirectChannelTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.root = Path(directory.name)
        self.store = Store(self.root)
        self.plan = Mock()
        self.plan.status.return_value = {'status': 'ready'}
        self.runner = Runner(self.store, self.plan)
        self.sender = Sender()
        self.mail = MailOutbox(self.store, senders=lambda: self.sender)
        self.routes = whatsapp_router(self.store, self.runner, lambda: PROFILE)
        self.link = {'ready': True, 'state': 'listo'}
        self.routes.link_state = AsyncMock(side_effect=lambda: dict(self.link))
        self.worker = self.automation()
        self.worker.configure(True)
        self.sends = []

    def automation(self):
        return TicketAutomation(self.store, self.runner, self.plan, lambda: PROFILE,
                                whatsapp=lambda: self.routes, mail=self.mail)

    def connect_mail(self):
        buzon.save_account(self.root, {'imap_host': '127.0.0.1', 'imap_port': 1, 'user': 'persona@example.com',
                                       'address': 'persona@example.com', 'smtp_host': '127.0.0.1',
                                       'smtp_port': 465, 'smtp_tls': 'ssl', 'authority': ''}, 'clave-ficticia')

    def allow(self, *names):
        for name in names:
            permisos.guardar(self.root, name, True)

    def ticket(self, name='ticket'):
        return save_input(self.store, name + '.png', png(name))['id']

    async def whatsapp_send(self, phone, reference, message, docs, arm, progress):
        self.sends.append(phone)
        arm()  # Recorded before the message would leave.

    async def settle(self, reading, *, text=TEXT, rounds=8, search=('', '')):
        page = {'pages': 1, 'method': 'texto', 'codes': [], 'text': text}
        with patch('facturador.automation.AI.extract', return_value=reading), \
             patch('facturador.automation.AI.discover_portal', return_value=search), \
             patch('facturador.automation.read_local_page', return_value=page), \
             patch('facturador.automation.public_page_links_to', new_callable=AsyncMock, return_value=False), \
             patch.object(WhatsAppWeb, 'send', side_effect=self.whatsapp_send), \
             patch.object(self.runner, 'run', new_callable=AsyncMock) as run:
            for _ in range(rounds):
                await self.worker.step()
            if self.runner.job_tasks:
                await asyncio.gather(*self.runner.job_tasks.values())
        return run

    def requests(self, table, key):
        with self.store.connect() as db:
            return [dict(row) for row in db.execute(f'SELECT * FROM {table} WHERE job_id=?', (key,))]

    async def test_mail_with_permission_is_requested_once(self):
        self.connect_mail()
        self.allow('pedir_por_correo')
        key = self.ticket()
        await self.settle(extracted(merchant_email=MAIL), rounds=12)
        row = self.store.get(key)
        self.assertEqual(row['receipt']['provider'], 'solicitud_directa')
        self.assertEqual((row['status'], row['issue_attempted']), ('external', 1))
        self.assertEqual((self.sender.prepared, len(self.sender.sent)), ([MAIL], 1))
        self.assertEqual(self.requests('mail_requests', key)[0]['state'], 'sent')
        self.assertIn('pedido_directo_auto', [event['kind'] for event in self.store.events(key)])
        self.assertNotIn('Indica el portal', row['message'])

    async def test_whatsapp_with_permission_goes_through_its_own_guards(self):
        self.allow('pedir_por_whatsapp')
        key = self.ticket()
        await self.settle(extracted(merchant_whatsapp=PHONE))
        self.assertEqual(self.sends, ['525555550188'])
        self.assertEqual(self.requests('whatsapp_requests', key)[0]['state'], 'sent')
        self.assertEqual(self.store.get(key)['issue_attempted'], 1)
        self.assertEqual(self.sender.prepared, [])

    async def test_both_channels_mail_first_then_whatsapp_only_if_mail_did_not_leave(self):
        self.connect_mail()
        self.allow('pedir_por_correo', 'pedir_por_whatsapp')
        first = self.ticket('primero')
        await self.settle(extracted(merchant_email=MAIL, merchant_whatsapp=PHONE))
        self.assertEqual(len(self.sender.sent), 1)
        self.assertEqual(self.sends, [])
        self.sender.refuse = 'El servidor de correo rechazó la dirección del comercio. Revisa el correo escrito.'
        second = self.ticket('segundo')
        await self.settle(extracted(merchant_email=MAIL, merchant_whatsapp=PHONE, ticket_number='PRUEBA-B'))
        self.assertEqual(len(self.sender.sent), 1)
        self.assertEqual(self.sends, ['525555550188'])
        self.assertEqual(self.requests('mail_requests', second), [])
        self.assertEqual(self.requests('whatsapp_requests', second)[0]['state'], 'sent')
        self.assertEqual(self.requests('whatsapp_requests', first), [])

    async def test_without_permission_waits_with_what_to_do(self):
        self.connect_mail()
        key = self.ticket()
        await self.settle(extracted(merchant_email=MAIL))
        row = self.store.get(key)
        self.assertEqual((row['status'], row['issue_attempted'], row['receipt']['provider']),
                         ('external', 0, 'solicitud_directa'))
        self.assertIn('Este comercio factura por correo.', row['message'])
        self.assertIn('Pídela desde el ticket, o activa el permiso en Ajustes', row['message'])
        self.assertEqual(self.sender.prepared, [])

    async def test_unavailable_channels_say_what_is_missing_and_wait(self):
        self.allow('pedir_por_correo', 'pedir_por_whatsapp')
        self.link = {'ready': False, 'state': 'sin_vincular'}
        key = self.ticket()
        await self.settle(extracted(merchant_email=MAIL, merchant_whatsapp=PHONE))
        message = self.store.get(key)['message']
        self.assertIn('Conecta un correo con envío', message)
        self.assertIn('Vincula WhatsApp', message)
        self.assertEqual((self.sender.prepared, self.sends), ([], []))
        self.link = {'ready': True, 'state': 'listo'}  # The person links WhatsApp; the queue goes on.
        await self.settle(extracted(merchant_email=MAIL, merchant_whatsapp=PHONE), rounds=3)
        self.assertEqual(self.sends, ['525555550188'])

    async def test_a_portal_always_wins_over_the_direct_channel(self):
        self.connect_mail()
        self.allow('pedir_por_correo')
        link = 'https://facturas.cafe-luna.example.org/emitir'
        key = self.ticket()
        await self.settle(extracted(merchant_email=MAIL, billing_url=link), text=TEXT + '\nFactura en ' + link)
        row = self.store.get(key)
        # The printed portal stays the destination (here it waits for its domain check).
        self.assertEqual((row['status'], row['receipt']['provider'], row['receipt']['portal_url']),
                         ('ready', 'portal_ticket', link))
        self.assertNotIn(key, self.worker._direct)
        self.assertEqual(self.sender.prepared, [])

    async def test_catalog_portal_is_prepared_and_never_asked_directly(self):
        self.connect_mail()
        self.allow('pedir_por_correo')
        key = self.ticket()
        run = await self.settle(extracted(provider='capufe', merchant='CAPUFE', merchant_email=MAIL),
                                text='CAPUFE\nFOLIO PRUEBA-A')
        run.assert_awaited_once_with(key, PROFILE, True, False)
        self.assertEqual(self.store.get(key)['receipt']['provider'], 'capufe')
        self.assertEqual(self.sender.prepared, [])

    async def test_two_printed_fiscal_links_are_still_a_portal(self):
        self.connect_mail()
        self.allow('pedir_por_correo')
        key = self.ticket()
        await self.settle(extracted(merchant_email=MAIL),
                          text=TEXT + '\nFactura en https://facturas.uno.example.org/a'
                                      '\nFactura en https://facturas.dos.example.org/b')
        row = self.store.get(key)
        self.assertEqual((row['status'], row['receipt']['provider']), ('review', 'unknown'))
        self.assertEqual(self.sender.prepared, [])

    async def test_unverified_web_suggestion_is_not_a_portal(self):
        self.connect_mail()
        self.allow('pedir_por_correo')
        key = self.ticket()
        await self.settle(extracted(merchant_email=MAIL),
                          search=('https://facturas.example.org/emitir', 'https://directorio.example.org/otro'))
        row = self.store.get(key)
        self.assertEqual((row['receipt']['provider'], row['receipt']['portal_url']), ('solicitud_directa', ''))
        self.assertEqual(len(self.sender.sent), 1)

    async def test_ticket_uploaded_before_the_permission_is_not_sent(self):
        self.connect_mail()
        # The permission was turned on after this ticket arrived.
        write_private(self.root / 'permisos.json',
                      {'pedir_por_correo': {'activo': True, 'desde': int(time.time()) + 3600}})
        key = self.ticket()
        await self.settle(extracted(merchant_email=MAIL))
        row = self.store.get(key)
        self.assertEqual((row['status'], row['issue_attempted']), ('external', 0))
        self.assertIn('Pídela desde el ticket.', row['message'])
        self.assertEqual(self.sender.prepared, [])

    async def test_restart_sends_nothing_for_tickets_that_already_existed(self):
        self.allow('pedir_por_correo')
        key = self.ticket()
        await self.settle(extracted(merchant_email=MAIL))
        self.assertIn('Conecta un correo con envío', self.store.get(key)['message'])
        self.store.recover_after_restart()
        self.worker = self.automation()  # A new version starts.
        self.worker.configure(True)
        self.connect_mail()
        await self.settle(extracted(merchant_email=MAIL))
        self.assertEqual(self.sender.prepared, [])
        self.assertEqual(self.store.get(key)['issue_attempted'], 0)

    async def test_send_error_is_recorded_once_and_left_to_the_person(self):
        self.connect_mail()
        self.allow('pedir_por_correo')
        self.sender.refuse = 'El servidor de correo rechazó la dirección del comercio. Revisa el correo escrito.'
        key = self.ticket()
        await self.settle(extracted(merchant_email=MAIL), rounds=15)
        self.assertEqual(self.sender.prepared, [MAIL])
        row = self.store.get(key)
        self.assertEqual((row['status'], row['issue_attempted']), ('external', 0))
        self.assertIn('No se pudo pedir por correo', row['message'])
        self.assertEqual([e['kind'] for e in self.store.events(key)].count('pedido_directo_error'), 1)
        self.assertFalse(self.worker._direct)

    async def test_attempt_by_the_person_stops_the_queue(self):
        self.allow('pedir_por_whatsapp')
        self.link = {'ready': False, 'state': 'sin_vincular'}
        key = self.ticket()
        await self.settle(extracted(merchant_whatsapp=PHONE))
        with self.store.connect() as db:
            db.execute('UPDATE jobs SET issue_attempted=1 WHERE id=?', (key,))
        self.link = {'ready': True, 'state': 'listo'}
        await self.settle(extracted(merchant_whatsapp=PHONE), rounds=3)
        self.assertEqual(self.sends, [])

    async def test_example_ticket_is_never_requested(self):
        self.connect_mail()
        self.allow('pedir_por_correo')
        key = save_input(self.store, 'ejemplo.png', png('ejemplo'), 'Ejemplo')['id']
        await self.settle(extracted(merchant_email=MAIL))
        self.assertEqual(self.store.get(key)['status'], 'new')
        self.assertEqual(self.sender.prepared, [])

    async def test_chat_upload_is_requested_without_the_inbox_toggle(self):
        self.connect_mail()
        self.allow('pedir_por_correo')
        self.worker.configure(False)
        data = png('chat')
        path = self.root / 'originales' / (hashlib.sha256(data).hexdigest() + '.png')
        path.parent.mkdir(exist_ok=True)
        path.write_bytes(data)
        key, _ = self.store.add_chat_bundle([(str(path), hashlib.sha256(data).hexdigest(), 'foto.png')])
        self.store.queue_chat_job(key)
        await self.settle(extracted(merchant_email=MAIL))
        self.assertEqual(len(self.sender.sent), 1)
        self.assertEqual(self.store.get(key)['status'], 'external')
        self.assertFalse(self.store.has_chat_jobs())


class OwnWhatsAppPhotosTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.root = Path(directory.name)
        self.store = Store(self.root)
        self.plan = Mock()
        self.plan.status.return_value = {'status': 'ready'}
        self.runner = Runner(self.store, self.plan)
        self.worker = TicketAutomation(self.store, self.runner, self.plan, lambda: PROFILE)
        self.worker.configure(False)  # «Procesar al subir» stays off.

    async def test_own_photos_are_read_and_prepared_but_manual_imports_are_not(self):
        permisos.guardar(self.root, 'leer_mis_fotos_de_whatsapp', True)

        async def own_media(phone, seen, keep, *, baseline, remember):
            self.assertEqual(phone, '525555550100')
            await asyncio.to_thread(keep, 'foto-1', 'ticket.png', png('propia'))

        web = SimpleNamespace(runner=SimpleNamespace(context=object()), own_media=own_media)
        photos = MisFotos(self.store, web, lambda: PROFILE, lambda: False)
        photos.mark(BASELINE, 'baseline')
        own = (await photos.tick(force=True))[0]['id']
        manual = save_upload(self.store, 'exportado.png', png('manual'), 'WhatsApp')[0]['id']
        self.assertEqual(self.store.get(manual)['receipt']['source'], 'WhatsApp: exportado.png')
        page = {'pages': 1, 'method': 'texto', 'codes': [], 'text': 'CAPUFE\nFOLIO PRUEBA-A'}
        with patch('facturador.automation.AI.extract', return_value=extracted(
                provider='capufe', merchant='CAPUFE')) as extract, \
             patch('facturador.automation.read_local_page', return_value=page), \
             patch.object(self.runner, 'run', new_callable=AsyncMock) as run:
            for _ in range(5):
                await self.worker.step()
            await asyncio.gather(*self.runner.job_tasks.values())
        extract.assert_called_once()
        run.assert_awaited_once_with(own, PROFILE, True, False)
        self.assertEqual(self.store.get(own)['status'], 'queued')
        self.assertEqual(self.store.get(manual)['status'], 'new')

    async def test_without_the_permission_own_photos_wait_for_the_toggle(self):
        permisos.guardar(self.root, 'leer_mis_fotos_de_whatsapp', True)

        async def own_media(phone, seen, keep, *, baseline, remember):
            await asyncio.to_thread(keep, 'foto-1', 'ticket.png', png('propia'))

        photos = MisFotos(self.store, SimpleNamespace(runner=SimpleNamespace(context=object()), own_media=own_media),
                          lambda: PROFILE, lambda: False)
        photos.mark(BASELINE, 'baseline')
        own = (await photos.tick(force=True))[0]['id']
        permisos.guardar(self.root, 'leer_mis_fotos_de_whatsapp', False)
        with patch('facturador.automation.AI.extract') as extract:
            await self.worker.step()
        extract.assert_not_called()
        self.assertEqual(self.store.get(own)['status'], 'new')


if __name__ == '__main__':
    unittest.main()
