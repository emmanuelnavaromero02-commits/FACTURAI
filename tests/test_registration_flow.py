import asyncio
import tempfile
import time
import unittest
from email.message import EmailMessage
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock, patch

from facturador.account_flow import PortalAccountFlow, registration_email


ORIGIN = 'https://factura.comercio.com'
EMAIL = 'cliente@example.org'


def enrollment_message(subject='Completa tu registro', body='Código de activación: 815270'):
    mail = EmailMessage()
    mail['To'] = EMAIL
    mail['From'] = 'registro@factura.comercio.com'
    mail['Subject'] = subject
    mail['Authentication-Results'] = 'mx.google.com; dmarc=pass header.from=factura.comercio.com'
    mail.set_content(body)
    return mail


class RegistrationEmailTests(unittest.TestCase):
    def lookup(self, mails, ages, kind='code'):
        now = time.time()
        items = [dict(sender=str(m['From']), received_ms=int((now-age)*1000), id=str(i)) for i,(m,age) in enumerate(zip(mails, ages))]
        with tempfile.TemporaryDirectory() as folder, patch('facturador.account_flow.search_tickets', return_value=items) as search, \
                patch('facturador.account_flow.read_selected_message', side_effect=lambda _, k: mails[int(k)]) as read:
            value = registration_email(Path(folder), ORIGIN, EMAIL, now-25, kind)
            self.assertIn('to:'+EMAIL, search.call_args.args[1])
            return value, read.call_count

    def test_ingestion_time_and_authenticated_sender_recipient_match(self):
        good = enrollment_message()
        other = enrollment_message()
        other.replace_header('To', 'otro@example.org')
        spoof = enrollment_message()
        spoof.replace_header('Authentication-Results', 'attacker.example; dmarc=pass header.from=factura.comercio.com')
        spoof['Authentication-Results'] = 'mx.google.com; dmarc=pass header.from=factura.comercio.com'
        self.assertEqual(self.lookup([good, good, other, spoof], [60, 5, 4, 3]), ('815270', 3))

    def test_ambiguous_or_login_or_recovery_mail_is_not_applied(self):
        with self.assertRaises(ValueError):
            self.lookup([enrollment_message(), enrollment_message()], [5,4])
        for subject in ('Tu código de acceso', 'Completa tu registro · inicio de sesión', 'Restablece la contraseña'):
            with self.subTest(subject=subject):
                self.assertEqual(self.lookup([enrollment_message(subject)], [2])[0], None)

    def test_unrelated_links_and_non_registration_notifications(self):
        body = 'Activa tu cuenta en https://otro.example.org/cuenta/activar'
        self.assertIsNone(self.lookup([enrollment_message(body=body)], [5], 'activate')[0])
        self.assertEqual(self.lookup([enrollment_message(body='Contraseña temporal: SINTETICA!7')], [5], 'temporary')[0], 'SINTETICA!7')


class RegistrationWaitTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        folder = tempfile.TemporaryDirectory()
        self.addCleanup(folder.cleanup)
        root = Path(folder.name)
        (root/'gmail_token.json').write_text('{}')
        self.progress = Mock()
        self.flow = PortalAccountFlow(root, ORIGIN, SimpleNamespace(email=EMAIL), 'actual', self.progress)
        self.record = {'registration_job': 'actual', 'registration_started': time.time()}

    async def test_late_delivery_reports_progress_without_resubmitting(self):
        with patch('facturador.account_flow.registration_email', side_effect=[None]*6+['validado']) as lookup, \
                patch('facturador.account_flow.asyncio.sleep', new_callable=AsyncMock) as sleeper:
            self.assertEqual(await self.flow._message(self.record, 'code'), 'validado')
        self.assertEqual(lookup.call_count, 7)
        self.assertEqual([call.args[0] for call in sleeper.await_args_list], [3, 7, 15, 20, 30, 35])
        self.assertIn('Esperando el correo', self.progress.call_args_list[0].args[0])
        self.assertIn('encontró el correo', self.progress.call_args_list[-1].args[0])

    async def test_wait_expires_and_can_resume_the_same_enrollment(self):
        with patch('facturador.account_flow.registration_email', return_value=None), \
                patch('facturador.account_flow.asyncio.sleep', new_callable=AsyncMock) as sleeper:
            with self.assertRaisesRegex(ValueError, 'Continuar'):
                await self.flow._message(self.record, 'code')
        self.assertEqual(sum(call.args[0] for call in sleeper.await_args_list), 145)
        with patch('facturador.account_flow.registration_email', return_value='validado'):
            self.assertEqual(await self.flow._message(self.record, 'code'), 'validado')

    async def test_cancel_during_poll_does_not_trigger_a_second_lookup(self):
        entered = asyncio.Event()
        async def wait(_):
            entered.set()
            await asyncio.Event().wait()
        with patch('facturador.account_flow.registration_email', return_value=None) as lookup, \
                patch('facturador.account_flow.asyncio.sleep', new=wait):
            task = asyncio.create_task(self.flow._message(self.record, 'code'))
            await asyncio.wait_for(entered.wait(), 2)
            task.cancel()
            with self.assertRaises(asyncio.CancelledError):
                await task
        self.assertEqual(lookup.call_count, 1)
