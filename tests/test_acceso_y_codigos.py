"""Sign-in in two steps, codes sent to the phone, the longer wait for the registration e-mail,
and the proof that no secret reaches the model, an event or a message.

Same made-up portal and stand-in model as ``test_registro_asistido``; the mailbox and the chat
are functions of the test. Nothing leaves this machine.
"""
import json
import tempfile
import time
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch
from urllib.parse import parse_qs, urlsplit

from facturador.account_flow import (HOLD, MAIL_WAIT, PHONE_CODES, PHONE_PAUSE, PHONE_QUESTION, PortalAccountFlow, phone_code,
                                     waited_text)
from facturador.auth import write_private
from facturador.models import Receipt
from facturador.permisos import guardar as permitir
from facturador.portal_accounts import site_password
from facturador.registro_asistido import MSG_ENVIADO
from facturador.registro_datos import guardar_datos
from simulated_portal import PAGE as PORTAL_PAGE, Brain, Portal, ticket
from test_core import PROFILE
from test_registro_asistido import COMPLETO, DATOS, ENVIADO, ORIGIN, Modelo, PortalCase
from test_screen_plans import PortalCase as EngineCase, kinds, metrics

SECRET_PAUSE = ('login', 'El portal muestra una verificación de acceso. Completa el paso en el sitio y pulsa Continuar.')
STEP_ONE = ('<h1>Inicia sesión</h1><form method="post" action="/acceso/clave"><label>Correo electrónico'
            '<input name="correo" type="email" required></label><button>Siguiente</button></form>'
            '<button type="button" onclick="location.href=\'/google\'">Continuar con Google</button>'
            '<a href="/registro">Regístrate</a>')
STEP_TWO = ('<h1>Inicia sesión</h1><p>Escribe tu contraseña</p><form method="post" action="/sesion"><label>Contraseña'
            '<input name="clave" type="password" required></label><button>%s</button></form>')
INSIDE = '<h1>Facturación en línea</h1><p>Elige el ticket que quieres facturar.</p>'


class TwoStepSignInTests(PortalCase):
    """First the e-mail, then the password, with the account saved for this merchant."""

    async def asyncSetUp(self):
        await super().asyncSetUp()
        self.password = site_password(self.record_path(), True)
        write_private(self.record_path(), {**self.record(), 'submission_attempted': True, 'registered_with_password': True})
        self.pages.update({'/acceso': STEP_ONE, '/acceso/clave': STEP_TWO % 'Iniciar sesión', '/sesion': INSIDE,
                           '/api/sesion': '', '/google': '<h1>Otra cuenta</h1>'})
        self.requests = []
        self.page.on('request', lambda request: self.requests.append(urlsplit(request.url).path))

    async def test_email_first_then_password_with_the_saved_account(self):
        await self.open('/acceso')
        flow = self.flow()                    # no model and no permission: signing in needs neither
        self.assertEqual(await self.step(flow), ('advanced', 'Se escribió el correo de la cuenta guardada de este comercio.'))
        await self.page.wait_for_url(ORIGIN + '/acceso/clave')
        self.assertEqual(await self.step(flow), ('advanced', 'Se usó la cuenta guardada de este comercio.'))
        self.assertEqual(self.posted, [('/acceso/clave', {'correo': PROFILE.email}), ('/sesion', {'clave': self.password})])
        self.assertEqual(self.record()['login_job'], 'ticket-1')
        self.assertIn(self.password, flow.credential_values)
        self.assertNotIn('/google', self.requests)
        self.assertNotIn('/registro', self.requests)
        # One sign-in per ticket: back on the first screen it is left to the person.
        await self.open('/acceso')
        self.assertIsNone(await self.step(flow))
        self.assertEqual(len(self.posted), 2)

    async def test_password_step_behind_a_plain_continue_button(self):
        self.pages['/acceso/clave'] = STEP_TWO % 'Continuar'
        await self.open('/acceso')
        flow = self.flow()
        self.assertEqual((await self.step(flow))[0], 'advanced')
        await self.page.wait_for_url(ORIGIN + '/acceso/clave')
        self.assertEqual(await self.step(flow), ('advanced', 'Se usó la cuenta guardada de este comercio.'))
        self.assertEqual(self.posted[-1], ('/sesion', {'clave': self.password}))
        # A second attempt of the same ticket does not sign in again.
        await self.open('/acceso/clave')
        again = self.flow()
        again.identifier_sent = True
        self.assertEqual(await self.step(again), ('login', 'Se intentó el acceso una vez. Revisa la respuesta del comercio en el navegador.'))
        self.assertEqual(len(self.posted), 2)

    async def test_password_alone_is_not_typed_when_this_attempt_did_not_give_the_email(self):
        await self.open('/acceso/clave', STEP_TWO % 'Continuar')
        self.assertEqual(await self.step(self.flow()), ('login', 'Completa la verificación o el acceso en el portal y vuelve al trámite.'))
        await self.open('/acceso/clave', STEP_TWO % 'Iniciar sesión')
        self.assertEqual(await self.step(self.flow()), (HOLD, 'Comprueba el correo y el campo de acceso del comercio.'))
        self.assertEqual(self.posted, [])

    async def test_scripted_sign_in_without_a_form_uses_the_saved_account(self):
        scripted = ('<h2>Acceso a clientes</h2><div class="acceso"><div><label for="u">Correo</label><input id="u" type="email"></div>'
                    '<div><label for="p">Contraseña</label><input id="p" type="password"></div>'
                    '<div role="button" tabindex="0" id="entrar">Entrar</div>'
                    '<div role="button" tabindex="0" id="g">Entrar con Google</div></div><script>'
                    'document.getElementById("g").addEventListener("click", () => fetch("/google", {method: "POST"}));'
                    'document.getElementById("entrar").addEventListener("click", () => fetch("/api/sesion", {method: "POST", body: '
                    'JSON.stringify({correo: document.getElementById("u").value, clave: document.getElementById("p").value})})'
                    '.then(() => { document.body.innerHTML = "<h1>Facturación en línea</h1>"; }));</script>')
        await self.open('/acceso', scripted)
        self.assertEqual(await self.step(self.flow()), ('advanced', 'Se usó la cuenta guardada de este comercio.'))
        await self.page.wait_for_function('document.body.innerText.includes("Facturación")')
        self.assertEqual(self.posted, [('/api/sesion', {'correo': PROFILE.email, 'clave': self.password})])
        self.assertNotIn('/google', self.requests)

    async def test_without_a_saved_account_the_first_screen_is_left_as_before(self):
        self.record_path().unlink()
        await self.open('/acceso')
        self.assertIsNone(await self.step(self.flow()))
        self.assertEqual(self.posted, [])
        self.assertEqual(await self.page.locator('input[name=correo]').input_value(), '')

    async def test_an_email_field_that_is_not_a_sign_in_is_never_filled(self):
        # The page offers «Iniciar sesión» in its menu, but this form sends the invoice somewhere.
        delivery = ('<nav><a href="/acceso">Iniciar sesión</a></nav><h1>Recibe tu factura</h1><form method="post" action="/enviar">'
                    '<label>Correo electrónico<input name="correo" type="email" required></label><button>Continuar</button></form>')
        await self.open('/entrega', delivery)
        self.assertIsNone(await self.step(self.flow()))
        # Nor a sign-in form on a page that already shows the ticket being billed.
        receipt = Receipt(merchant='Tienda Ejemplo', ticket_number='TKT-88231-Q', amount='312.40')
        await self.open('/acceso', STEP_ONE + '<p>Tu ticket TKT-88231-Q por $312.40 se facturará al continuar.</p>')
        self.assertIsNone(await self.step(self.flow(ticket=receipt)))
        self.assertEqual(self.posted, [])
        self.assertEqual(await self.page.locator('input[name=correo]').input_value(), '')


SMS = ('<h1>Verifica tu teléfono</h1><p>Te enviamos un código por SMS al número terminado en 11.</p>'
       '<form method="post" action="/verificado"><label>Código<input name="codigo" autocomplete="one-time-code" '
       'inputmode="numeric" maxlength="6" required></label><button>Verificar</button></form><a href="#">Reenviar código</a>'
       '<p>Atención a clientes: correo ayuda@tienda-ejemplo.mx</p>')
TWO_FACTOR = SMS.replace('<h1>Verifica tu teléfono</h1>', '<h1>Verificación en dos pasos</h1>').replace(
    'Te enviamos un código por SMS', 'Escribe el código de seguridad que te enviamos por WhatsApp')
CODE = '482913'


class Chat:
    """Stands in for the person in the chat: it answers the question it is asked, or nothing."""

    def __init__(self, *answers):
        self.asked: list[str] = []
        self.answers = list(answers)

    async def __call__(self, text):
        self.asked.append(text)
        return self.answers.pop(0) if self.answers else ''


class PhoneCodeTests(PortalCase):
    async def asyncSetUp(self):
        await super().asyncSetUp()
        self.pages['/verificado'] = INSIDE
        # This ticket is opening its account on the portal: that is when a code may be asked for.
        site_password(self.record_path(), True)
        write_private(self.record_path(), {**self.record(), 'submission_attempted': True, 'registration_job': 'ticket-1',
                                           'registration_started': time.time()})

    async def code_field(self):
        return await self.page.locator('input[name=codigo]').input_value()

    async def test_code_is_asked_in_the_chat_typed_and_sent(self):
        chat = Chat(f' {CODE[:3]} {CODE[3:]} ')
        await self.open('/verifica', SMS)
        flow = self.flow(pedir_codigo=chat)
        result = await self.step(flow)
        self.assertEqual(result, ('advanced', 'Se escribió en el portal el código que llegó a tu teléfono.'))
        self.assertEqual(self.posted, [('/verificado', {'codigo': CODE})])
        # A short question that names the portal that asks, so its message can be told from another.
        self.assertEqual(chat.asked, ['El portal cuentas.tienda-ejemplo.mx dice que mandó un código a tu teléfono por SMS o '
                                      'WhatsApp. Si el mensaje que te llegó es de ese comercio, escríbeme aquí solo el código.'])
        self.assertEqual(chat.asked, [PHONE_QUESTION % 'cuentas.tienda-ejemplo.mx'])
        # From here on the code is guarded like a password, and it is in nothing that was said.
        self.assertIn(CODE, flow.credential_values)
        self.assertTrue(flow.private_observation('Tu código fue ' + CODE))
        self.assertNotIn(CODE, json.dumps([result, self.said, chat.asked], ensure_ascii=False))

    async def test_a_ticket_that_is_not_opening_or_using_its_account_is_never_asked(self):
        for body in (SMS, TWO_FACTOR):
            chat = Chat(CODE)
            await self.open('/verifica', body)
            result = await self.step(self.flow(job='otro-ticket', pedir_codigo=chat))
            self.assertIn(result, (None, SECRET_PAUSE))
            self.assertEqual((chat.asked, self.posted, await self.code_field()), ([], [], ''))
        # A sign-in of this ticket counts the same as its registration.
        write_private(self.record_path(), {**self.record(), 'login_job': 'otro-ticket'})
        chat = Chat(CODE)
        self.assertEqual((await self.step(self.flow(job='otro-ticket', pedir_codigo=chat)))[0], 'advanced')
        self.assertEqual(len(chat.asked), 1)

    async def test_a_plain_function_and_the_two_factor_screen_work_the_same(self):
        asked = []
        await self.open('/verifica', TWO_FACTOR)
        flow = self.flow(pedir_codigo=lambda text: asked.append(text) or CODE)
        self.assertEqual((await self.step(flow))[0], 'advanced')
        self.assertEqual((self.posted, len(asked)), ([('/verificado', {'codigo': CODE})], 1))

    async def test_without_an_answer_or_without_the_function_the_pause_is_the_same_as_before(self):
        await self.open('/verifica', SMS)
        self.assertIsNone(await self.step(self.flow()))                 # the engine then pauses for the access
        chat = Chat()
        flow = self.flow(pedir_codigo=chat)
        self.assertIsNone(await self.step(flow))
        self.assertIsNone(await self.step(flow))
        self.assertIsNone(await self.step(flow))
        self.assertEqual(len(chat.asked), PHONE_CODES)                   # asked twice at most, never a third time
        await self.open('/verifica', TWO_FACTOR)
        self.assertEqual(await self.step(self.flow()), SECRET_PAUSE)
        self.assertEqual(await self.step(self.flow(pedir_codigo=Chat())), SECRET_PAUSE)

        def broken(_text):
            raise RuntimeError('chat fuera de línea')
        self.assertEqual(await self.step(self.flow(pedir_codigo=broken)), SECRET_PAUSE)
        self.assertEqual((self.posted, await self.code_field()), ([], ''))

    async def test_an_answer_that_is_not_one_clear_code_is_not_typed(self):
        await self.open('/verifica', SMS)
        chat = Chat('no sé, me llegaron 1234 y 987654', 'todavía nada')
        flow = self.flow(pedir_codigo=chat)
        self.assertIsNone(await self.step(flow))
        self.assertIsNone(await self.step(flow))
        self.assertEqual((self.posted, await self.code_field()), ([], ''))
        # Whatever looked like a code in the answer is still kept away from the model.
        self.assertTrue({'1234', '987654'} <= flow.credential_values)

    async def test_row_of_digit_boxes_that_sends_itself(self):
        boxes = ('<h1>Verifica tu número</h1><p>Ingresa el código de 6 dígitos que llegó a tu celular por mensaje de texto.</p>'
                 '<div id="cajas">' + '<input inputmode="numeric" maxlength="1" aria-label="Dígito">' * 6 + '</div><script>'
                 'const cajas = [...document.querySelectorAll("#cajas input")];'
                 'cajas.forEach(caja => caja.addEventListener("input", () => { if (cajas.every(c => c.value)) '
                 'fetch("/verificado", {method: "POST", body: JSON.stringify({codigo: cajas.map(c => c.value).join("")})})'
                 '.then(() => { document.body.innerHTML = "<h1>Facturación en línea</h1>"; }); }));</script>')
        await self.open('/verifica', boxes)
        self.assertEqual((await self.step(self.flow(pedir_codigo=Chat(CODE))))[0], 'advanced')
        self.assertEqual(self.posted, [('/verificado', {'codigo': CODE})])

    async def test_screens_that_are_not_a_code_sent_to_the_phone_are_never_asked_about(self):
        screens = {
            'aplicación de autenticación': TWO_FACTOR.replace('que te enviamos por WhatsApp', 'de tu aplicación de autenticación'),
            'código enviado al correo': SMS.replace('por SMS al número terminado en 11', 'a tu correo'),
            'código postal y teléfono': ('<h1>Tus datos de contacto para tu registro</h1><p>Usamos tu código postal y tu teléfono para '
                                         'ubicar tu sucursal.</p><form method="post" action="/verificado"><label>Código postal'
                                         '<input name="codigo" required></label><button>Continuar</button></form>'),
            'código de facturación del ticket': ('<h1>Registro de ticket</h1><p>Escribe el código de facturación; si falla llama al '
                                                 'teléfono de la tienda.</p><form method="post" action="/verificado"><label>Código de '
                                                 'facturación<input name="codigo" required></label><button>Continuar</button></form>'),
            'verificación de robot': SMS.replace('<button>', '<div data-sitekey="demo"></div><button>'),
        }
        for name, body in screens.items():
            with self.subTest(name):
                chat = Chat(CODE)
                await self.open('/verifica', body)
                result = await self.step(self.flow(pedir_codigo=chat))
                self.assertNotEqual((result or ('',))[0], 'advanced')
                self.assertEqual((chat.asked, self.posted, await self.code_field()), ([], [], ''))

    async def test_activation_code_sent_by_sms_is_asked_instead_of_awaited_in_the_mailbox(self):
        activation = ('<h1>Activa tu cuenta</h1><p>Ingresa el código de activación que enviamos por SMS a tu celular.</p>'
                      '<form action="/verificado" method="post"><label>Código de activación<input name="codigo" required></label>'
                      '<button>%s</button></form>')
        # Under either button the fixed rules take the screen for a wait on the mailbox.
        for button in ('Verificar cuenta', 'Validar'):
            with self.subTest(button):
                self.posted.clear()
                await self.open('/verifica', activation % button)
                with patch('facturador.account_flow.registration_email') as mailbox:
                    # Nobody to ask: it stops at once and says where the code went, without ten minutes of mailbox.
                    self.assertEqual(await self.step(self.flow()), ('login', PHONE_PAUSE))
                    chat = Chat(CODE)
                    self.assertEqual((await self.step(self.flow(pedir_codigo=chat)))[0], 'advanced')
                mailbox.assert_not_called()
                self.assertEqual((self.posted, len(chat.asked)), ([('/verificado', {'codigo': CODE})], 1))

    async def test_activation_code_sent_by_mail_still_comes_from_the_mailbox(self):
        activation = ('<h1>Activa tu cuenta</h1><p>Ingresa el código de activación que enviamos a tu correo.</p>'
                      '<form action="/verificado" method="post"><label>Código de activación<input name="codigo" required></label>'
                      '<button>Verificar cuenta</button></form>')
        (self.root / 'gmail_token.json').write_text('{}')
        chat = Chat(CODE)
        await self.open('/verifica', activation)
        with patch('facturador.account_flow.registration_email', return_value='815270'):
            self.assertEqual(await self.step(self.flow(pedir_codigo=chat)),
                             ('advanced', 'Se envió el código de alta al mismo comercio.'))
        self.assertEqual((self.posted, chat.asked), ([('/verificado', {'codigo': '815270'})], []))


class PhoneAnswerTests(unittest.TestCase):
    def test_code_is_taken_out_of_what_the_person_wrote(self):
        for answer, code in {'482913': '482913', ' 482 913 ': '482913', '48-29-13': '482913', 'es 482913': '482913',
                             'El código es: 482913.': '482913', 'A1B2C3': 'A1B2C3', 'mi código es a1b2c3 gracias': 'a1b2c3',
                             '': '', None: '', 'no me llegó': '', 'llegaron 1234 y 5678': '', '12': '', 'código': '',
                             '123456789012345': ''}.items():
            with self.subTest(answer=answer):
                self.assertEqual(phone_code(answer), code)


class LongMailWaitTests(unittest.IsolatedAsyncioTestCase):
    """The registration e-mail is awaited up to ten minutes, on a clock the test moves."""

    async def asyncSetUp(self):
        folder = tempfile.TemporaryDirectory()
        self.addCleanup(folder.cleanup)
        root = Path(folder.name)
        (root / 'gmail_token.json').write_text('{}')
        self.said = []
        self.meter = SimpleNamespace(ai_calls=0, waited=0.0)
        self.flow = PortalAccountFlow(root, ORIGIN, SimpleNamespace(email=PROFILE.email), 'actual', self.said.append,
                                      medidor=self.meter)
        self.record = {'registration_job': 'actual', 'registration_started': time.time()}

    async def wait(self, arrives_at=None, lookup_takes=0):
        clock, sleeps, looked = [1000.0], [], []

        async def sleep(seconds):
            sleeps.append(seconds)
            clock[0] += seconds

        def lookup(*_args):
            looked.append(clock[0] - 1000)
            clock[0] += lookup_takes
            return 'validado' if arrives_at is not None and clock[0] - 1000 >= arrives_at else None

        with patch('facturador.account_flow.registration_email', side_effect=lookup), \
                patch('facturador.account_flow.asyncio.sleep', new=sleep), \
                patch('facturador.account_flow.monotonic', new=lambda: clock[0]):
            try:
                value = await self.flow._message(self.record, 'code')
            except ValueError as exc:
                value = exc
        return value, sleeps, looked

    async def test_mail_that_arrives_after_seven_minutes_is_still_used(self):
        value, sleeps, looked = await self.wait(arrives_at=7 * 60)
        self.assertEqual(value, 'validado')
        self.assertTrue(all(10 <= pause <= 20 for pause in sleeps), sleeps)
        self.assertEqual((looked[0], looked[-1], sum(sleeps)), (0, 420, 420))
        # The wait says how long it has been, and it is not charged to the attempt as work.
        self.assertEqual(self.said[0], 'Esperando el correo de alta del comercio…')
        self.assertIn('Esperando el correo de alta del comercio… Llevo 1 min de hasta 10 min.', self.said)
        self.assertIn('Esperando el correo de alta del comercio… Llevo 6 min 40 s de hasta 10 min.', self.said)
        self.assertEqual(self.said[-1], 'Se encontró el correo de alta. Continuando en el portal…')
        self.assertEqual(self.meter.waited, 420)

    async def test_wait_ends_at_ten_minutes_with_the_same_pause_as_before(self):
        value, sleeps, looked = await self.wait()
        self.assertIsInstance(value, ValueError)
        self.assertEqual(str(value), 'Todavía no llegó un correo de alta verificable. Pulsa Continuar para volver a consultarlo.')
        self.assertEqual((MAIL_WAIT, sum(sleeps), looked[-1]), (600, 600, 600))
        self.assertTrue(all(10 <= pause <= 20 for pause in sleeps), sleeps)
        self.assertEqual(len(looked), len(sleeps) + 1)
        self.assertEqual(self.meter.waited, 600)
        # Past the 165 seconds it waited before, it keeps looking.
        self.assertGreater(sum(moment > 165 for moment in looked), 20)

    async def test_slow_mailbox_does_not_stretch_the_wait(self):
        value, sleeps, looked = await self.wait(lookup_takes=45)
        self.assertIsInstance(value, ValueError)
        self.assertLessEqual(looked[-1], 600)
        self.assertLess(sum(sleeps), 600)
        self.assertLess(self.meter.waited, 600 + 45 + 1)

    def test_elapsed_time_is_said_the_way_a_person_says_it(self):
        self.assertEqual([waited_text(seconds) for seconds in (0, 10, 59.9, 60, 95, 600)],
                         ['0 s', '10 s', '59 s', '1 min', '1 min 35 s', '10 min'])


TOKEN = 'TOKEN-DE-LA-LIGA-7Q9Z'
ECHO = 'Hola Ana María Pruebas Ficticia. Confirmamos tu correo prueba@example.com y tu teléfono 5500001111.'


class NothingSecretLeavesTests(PortalCase):
    """A whole enrollment on a talkative portal: steps, password, SMS code and activation link."""

    async def asyncSetUp(self):
        await super().asyncSetUp()
        guardar_datos(self.root, DATOS)
        self.allow()
        (self.root / 'gmail_token.json').write_text('{}')
        form = '<h1>%s</h1><p>%s</p><form method="post" action="%s">%s<button>%s</button></form>'
        self.pages.update({
            '/registro': form % ('Regístrate', '', '/registro/clave', '<label>Nombre(s)<input name="nombres" required></label>'
                                 '<label>Apellidos<input name="apellidos" required></label>'
                                 '<label>Correo<input name="correo" type="email" required></label>'
                                 '<label>Celular<input name="celular" type="tel" required></label>'
                                 '<label>Fecha de nacimiento<input name="nacimiento" type="date" required></label>', 'Continuar'),
            '/registro/clave': form % ('Protege tu cuenta', ECHO, '/registro/sms',
                                       '<label>Contraseña<input name="clave" type="password" required></label>'
                                       '<label>Confirmar contraseña<input name="clave2" type="password" required></label>'
                                       '<label><input type="checkbox" name="acepto" required> Acepto los términos y condiciones</label>',
                                       'Crear cuenta'),
            '/registro/sms': form % ('Activa tu cuenta', 'Ingresa el código de activación que enviamos por SMS a tu celular.',
                                     '/registro/correo', '<label>Código de activación<input name="codigo" required></label>',
                                     'Verificar cuenta'),
            '/registro/correo': '<h1>Activa tu cuenta</h1><p>Te enviamos un correo. Revisa tu bandeja de entrada.</p>',
            f'/registro/activar/{TOKEN}': form % ('Completa tu registro', ECHO + f' Tu liga fue {ORIGIN}/registro/activar/{TOKEN}',
                                                  '/bienvenida', '<label>Régimen fiscal<select name="regimen" required>'
                                                  '<option value="">Elige</option><option value="a">605 - Sueldos y salarios</option>'
                                                  '<option value="b">612 - Actividades empresariales</option></select></label>'
                                                  '<label>Colonia<input name="colonia" required></label>', 'Finalizar registro'),
            '/bienvenida': INSIDE + '<p>' + ECHO + '</p>'})

    async def test_model_events_and_messages_never_carry_a_secret(self):
        model, chat = Modelo(), Chat(CODE)
        link = f'{ORIGIN}/registro/activar/{TOKEN}?k=clave-de-activacion'
        await self.open('/registro')
        flow = self.flow(model, pedir_codigo=chat)
        results = []
        with patch('facturador.account_flow.registration_email', return_value=link):
            for _ in range(5):
                results.append(await self.step(flow))
            await self.page.wait_for_url(ORIGIN + '/bienvenida')
            results.append(await self.step(flow))
        self.assertEqual([result and result[0] for result in results], ['advanced'] * 5 + [None])
        password = site_password(self.record_path(), False)
        self.assertEqual([path for path, _ in self.posted], ['/registro/clave', '/registro/sms', '/registro/correo', '/bienvenida'])
        self.assertEqual(self.posted[0][1], {'nombres': 'Ana María', 'apellidos': 'Pruebas Ficticia', 'correo': PROFILE.email,
                                             'celular': '5500001111', 'nacimiento': '1990-05-17'})
        self.assertEqual(self.posted[1][1], {'clave': password, 'clave2': password, 'acepto': 'on'})
        self.assertEqual(self.posted[2][1], {'codigo': CODE})
        self.assertEqual(self.posted[3][1], {'regimen': 'b', 'colonia': 'Centro'})
        # Three screens were described to the model; the code screen and the mail never were.
        self.assertEqual(len(model.seen), 3)
        sent_to_model = '\n'.join(model.seen)
        outside = json.dumps([results, self.said, chat.asked], ensure_ascii=False)
        secrets = {'contraseña': password, 'código': CODE, 'liga': TOKEN, 'clave de la liga': 'clave-de-activacion'}
        personal = {'nombre': 'Ana', 'apellido': 'Pruebas', 'segundo apellido': 'Ficticia', 'teléfono': '5500001111',
                    'año de nacimiento': '1990', 'calle': 'Siempre Viva', 'municipio': 'Guadalajara', 'correo': PROFILE.email,
                    'RFC': PROFILE.rfc, 'nombre fiscal': PROFILE.name}
        for name, value in {**secrets, **personal}.items():
            with self.subTest(name):
                self.assertNotIn(value.casefold(), sent_to_model.casefold())
                self.assertNotIn(value, outside)
        for text in model.seen:
            self.assertFalse(flow.private_observation(text))
        # What stays on disk about the enrollment has neither the password nor the code in the clear.
        stored = self.record_path().read_text(encoding='utf-8')
        for value in (password, CODE, TOKEN, 'Ana', 'Pruebas', '5500001111'):
            self.assertNotIn(value, stored)
        self.assertTrue({password, CODE, link} <= flow.credential_values)

    async def test_page_that_shows_the_password_is_never_described(self):
        self.pages['/registro/clave'] = self.pages['/registro/clave'].replace(ECHO, 'Tu acceso quedó como %s')
        model = Modelo()
        await self.open('/registro')
        flow = self.flow(model)
        self.assertEqual((await self.step(flow))[0], 'advanced')
        password = site_password(self.record_path(), False)
        self.pages['/registro/clave'] = self.pages['/registro/clave'] % password
        await self.open('/registro/clave')
        # The screen stays with the person, with the pause it always had, and nobody else reads it.
        self.assertEqual(await self.step(flow), ('login', 'Completa la verificación o el acceso en el portal y vuelve al trámite.'))
        self.assertEqual((len(model.seen), len(self.posted)), (1, 1))


WALL = COMPLETO.replace('<h1>Crea tu cuenta</h1>', '<h1>Para facturar crea tu cuenta</h1>').replace('action="/alta"',
                                                                                                    'action="/cuenta/alta"')


class WalledPortal(Portal):
    """The billing portal of the engine tests behind an account: it bills once somebody registered."""

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.accounts = []

    async def handle(self, route):
        request = route.request
        parts = urlsplit(request.url)
        if parts.path == '/cuenta/alta' and request.method == 'POST':
            self.accounts.append({name: values[0] for name, values in parse_qs(request.post_data or '',
                                                                                 keep_blank_values=True).items()})
            return await route.fulfill(content_type='text/html; charset=utf-8', body=self.lookup())
        if not self.accounts:
            return await route.fulfill(content_type='text/html; charset=utf-8', body=PORTAL_PAGE % WALL)
        await super().handle(route)


class WholeTicketTests(EngineCase):
    """The engine with its own browser: a portal asks for an account before it bills the ticket."""

    async def test_portal_that_asks_for_an_account_is_registered_and_then_billed(self):
        guardar_datos(self.root, DATOS)
        for name in ('registrarme_en_portales', 'aceptar_terminos_de_registro'):
            permitir(self.root, name, True)
        portal = await self.serve(WalledPortal())
        receipt = portal.add(ticket('TKT-55120-R', '2026-11-20', '312.40'))
        brain, model = Brain(), Modelo()
        with patch('facturador.runner.AI.registration_plan', side_effect=model.registration_plan):
            key = await self.bill(receipt, brain)
        row = self.store.get(key)
        self.assertEqual(row['status'], 'verified', (row['message'], self.store.events(key)[-8:]))
        from facturador.portal_accounts import credential_file
        password = site_password(credential_file(self.root, portal.origin, PROFILE.email), False)
        self.assertEqual(portal.accounts, [{**ENVIADO, 'clave': password, 'clave2': password}])
        self.assertEqual(len(portal.emitted), 1)
        events = self.store.events(key)
        self.assertEqual([event['detail'] for event in events if event['kind'] == 'portal_account'], [MSG_ENVIADO])
        self.assertNotIn('needs_user', kinds(self.store, key))
        self.assertEqual(kinds(self.store, key).count('issue_attempt'), 1)
        # One consultation planned the registration and it is counted with the ones that billed the ticket.
        self.assertEqual((len(model.seen), brain.seen('Folio del ticket'), brain.seen('Crea tu cuenta')), (1, 1, 0))
        self.assertEqual(metrics(self.store, key)[0]['ai_calls'], len(brain.calls) + 1)
        everything = json.dumps([events, self.store.chat_messages(), brain.calls, model.seen], ensure_ascii=False, default=str)
        for value in (password, 'Ana María', 'Pruebas', '5500001111', 'Siempre Viva', '17/05/1990'):
            self.assertNotIn(value, everything)

    async def test_without_the_permission_the_same_portal_waits_for_the_person(self):
        guardar_datos(self.root, DATOS)
        portal = await self.serve(WalledPortal())
        receipt = portal.add(ticket('TKT-55121-S', '2026-11-21', '98.00'))
        brain, model = Brain(), Modelo()
        with patch('facturador.runner.AI.registration_plan', side_effect=model.registration_plan):
            key = await self.bill(receipt, brain, until='login')
            self.assertEqual(self.store.get(key)['message'], 'Completa la verificación o el acceso en el portal y vuelve al trámite.')
            await self.runner.stop(key)
        self.assertEqual((portal.accounts, portal.emitted, model.seen, brain.calls), ([], [], [], []))


if __name__ == '__main__':
    unittest.main()
