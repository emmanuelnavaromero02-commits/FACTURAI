"""Registration over several screens, sign-up buttons under other names and buttons that are not a <button>.

Same made-up portal and stand-in model as ``test_registro_asistido``: nothing leaves this machine.
"""
import json
import unittest
from urllib.parse import urlsplit

from facturador.account_flow import HOLD
from facturador.permisos import guardar as permitir
from facturador.portal_accounts import site_password
from facturador.registro_asistido import MAX_PANTALLAS, MSG_REPETIDO, MSG_TOPE
from facturador.registro_datos import guardar_datos
from test_core import PROFILE
from test_registro_asistido import COMPLETO, DATOS, ORIGIN, Modelo, PortalCase

MAIL_SENT = '<h1>Revisa tu correo</h1><p>Te enviamos un mensaje para activar tu cuenta.</p>'


def wizard(step, body, button, action):
    return f'<h1>{step}</h1><form method="post" action="{action}">{body}<button>{button}</button></form>'


class SeveralStepsTests(PortalCase):
    """A registration spread over screens: each one is planned, recorded and sent once."""

    async def asyncSetUp(self):
        await super().asyncSetUp()
        guardar_datos(self.root, DATOS)
        self.allow()
        self.pages.update({
            '/registro': wizard('Regístrate', '<label>Nombre(s)<input name="nombres" required></label>'
                                '<label>Apellidos<input name="apellidos" required></label>'
                                '<label>Correo<input name="correo" type="email" required></label>', 'Continuar', '/registro/paso2'),
            # Nothing on this screen says «registro»: only the address of the page and what came before.
            '/registro/paso2': wizard('Tu domicilio', '<p>Paso 2 de 3</p><label>Calle y número<input name="calle" required></label>'
                                      '<label>Colonia<input name="colonia" required></label>'
                                      '<label>Código postal<input name="cp" required></label>'
                                      '<label>País<select name="pais" required><option value="">Elige</option>'
                                      '<option value="US">Estados Unidos</option><option value="MX">México</option></select></label>',
                                      'Siguiente', '/registro/paso3'),
            '/registro/paso3': wizard('Protege tu cuenta', '<label>Contraseña<input name="clave" type="password" required></label>'
                                      '<label>Confirmar contraseña<input name="clave2" type="password" required></label>'
                                      '<label><input type="checkbox" name="acepto" required> Acepto los términos y condiciones</label>',
                                      'Finalizar registro', '/alta'),
            '/alta': MAIL_SENT})

    async def test_three_screens_are_sent_one_after_another(self):
        model = Modelo()
        await self.open('/registro')
        flow = self.flow(model)
        for _ in range(3):
            self.assertEqual((await self.step(flow))[0], 'advanced')
        await self.page.wait_for_url(ORIGIN + '/alta')
        password = site_password(self.record_path(), False)
        self.assertEqual(self.posted, [
            ('/registro/paso2', {'nombres': 'Ana María', 'apellidos': 'Pruebas Ficticia', 'correo': PROFILE.email}),
            ('/registro/paso3', {'calle': 'Avenida Siempre Viva 742', 'colonia': 'Centro', 'cp': PROFILE.postal_code, 'pais': 'MX'}),
            ('/alta', {'clave': password, 'clave2': password, 'acepto': 'on'})])
        # Every send was on file before it left, the last one like the first.
        self.assertEqual(self.recorded, [True, True, True])
        record = self.record()
        self.assertEqual((len(record['assisted_screens']), len(set(record['assisted_screens'])), record['registered_with_password'],
                          record['registration_job']), (3, 3, True, 'ticket-1'))
        self.assertEqual((len(model.seen), self.meter.ai_calls), (3, 3))
        self.assertEqual([json.loads(text)['pantalla'] for text in model.seen], [1, 2, 3])

    async def test_first_screen_without_a_password_keeps_the_temporary_password_rule(self):
        model = Modelo()
        await self.open('/registro')
        flow = self.flow(model)
        self.assertEqual((await self.step(flow))[0], 'advanced')
        record = self.record()
        # No password was asked yet: until one is, the merchant's temporary one would be the way in.
        self.assertEqual((record['submission_attempted'], record['registered_with_password']), (True, False))
        started = record['registration_started']
        self.assertEqual((await self.step(flow))[0], 'advanced')
        self.assertEqual((await self.step(flow))[0], 'advanced')
        record = self.record()
        # The mail of this enrollment is looked for since its first screen.
        self.assertEqual((record['registered_with_password'], record['registration_started']), (True, started))

    async def test_a_screen_shown_again_is_never_sent_twice(self):
        model = Modelo()
        self.pages['/registro/paso2'] = self.pages['/registro'].replace('<h1>Regístrate</h1>',
                                                                       '<h1>Regístrate</h1><p>El correo ya existe.</p>')
        await self.open('/registro')
        flow = self.flow(model)
        self.assertEqual((await self.step(flow))[0], 'advanced')
        await self.page.wait_for_url(ORIGIN + '/registro/paso2')
        # Same fields under another address and with an error on top: it is the screen already sent.
        self.assertEqual(await self.step(flow), ('login', MSG_REPETIDO))
        self.assertEqual((len(self.posted), len(model.seen)), (1, 1))
        # Another attempt of the same ticket does not start over either, nor does another ticket.
        await self.open('/registro')
        self.assertEqual(await self.step(self.flow(model)), ('login', MSG_REPETIDO))
        result = await self.step(self.flow(model, job='ticket-2'))
        self.assertEqual((len(self.posted), len(model.seen)), (1, 1))
        self.assertNotEqual((result or ('',))[0], 'advanced')

    async def test_registration_stops_at_six_screens(self):
        for number in range(1, 10):
            self.pages[f'/registro/{number}'] = wizard(
                'Regístrate', f'<label>Nombre(s)<input name="dato{number}" required></label>', 'Continuar',
                f'/registro/{number + 1}')
        model = Modelo()
        await self.open('/registro/1')
        flow = self.flow(model)
        for number in range(1, MAX_PANTALLAS + 1):
            self.assertEqual((await self.step(flow))[0], 'advanced', number)
            await self.page.wait_for_url(ORIGIN + f'/registro/{number + 1}')
        self.assertEqual(await self.step(flow), ('login', MSG_TOPE))
        self.assertEqual((len(self.posted), len(model.seen), self.meter.ai_calls), (MAX_PANTALLAS, MAX_PANTALLAS, MAX_PANTALLAS))

    async def test_fields_that_appear_while_filling_are_planned_too(self):
        grows = ('<h1>Crea tu cuenta</h1><form method="post" action="/alta"><label>Correo<input name="correo" type="email" required></label>'
                 '<label>Tipo de persona<select name="persona" required onchange="document.getElementById(\'mas\').innerHTML='
                 '\'<label>Nombre(s)<input name=nombres required></label>\'"><option value="">Elige</option>'
                 '<option value="F">Persona física</option><option value="M">Persona moral</option></select></label>'
                 '<div id="mas"></div><label>Contraseña<input name="clave" type="password" required></label>'
                 '<button>Crear cuenta</button></form>')
        model = Modelo()
        await self.open('/registro', grows)
        flow = self.flow(model)
        self.assertEqual(await self.step(flow), ('advanced', 'Se llenó una parte del registro y el portal pidió más datos.'))
        self.assertEqual(self.posted, [])
        self.assertNotIn('assisted_screens', self.record())
        self.assertEqual((await self.step(flow))[0], 'advanced')
        password = site_password(self.record_path(), False)
        self.assertEqual(self.posted, [('/alta', {'correo': PROFILE.email, 'persona': 'F', 'nombres': 'Ana María', 'clave': password})])
        self.assertEqual(len(model.seen), 2)


SCRIPTED = """<h2>Crea tu cuenta</h2><div class="tarjeta-alta">
<div><label for="n">Nombre completo</label><input id="n"></div>
<div><label for="c">Correo</label><input id="c" type="email"></div>
<div><label for="p">Contraseña</label><input id="p" type="password"></div>
<div>%s</div></div>
<script>
function alta() {
  const value = id => document.getElementById(id).value;
  fetch('/api/alta', {method: 'POST', body: JSON.stringify({nombre: value('n'), correo: value('c'), clave: value('p')})})
    .then(() => { document.body.innerHTML = '<h1>Revisa tu correo</h1><p>Te enviamos un mensaje para activar tu cuenta.</p>'; });
}
document.querySelectorAll('[data-alta]').forEach(node => node.addEventListener('click', alta));
</script>"""


class OtherButtonsTests(PortalCase):
    """Sign-up buttons under other names and buttons that are not a <button>."""

    async def asyncSetUp(self):
        await super().asyncSetUp()
        guardar_datos(self.root, DATOS)
        self.allow()
        self.pages['/api/alta'] = ''
        self.pages['/alta'] = MAIL_SENT

    def fresh(self):
        self.posted.clear()
        self.record_path().unlink(missing_ok=True)

    async def test_scripted_controls_outside_any_form_are_pressed(self):
        controls = {'liga con otro nombre': '<a href="#" class="btn" data-alta>Regístrate</a>',
                    'bloque con rol de botón': '<div role="button" tabindex="0" data-alta>Crear cuenta nueva</div>',
                    'botón que no envía': '<input type="button" value="Registrarse" data-alta>',
                    'texto vestido de botón': '<span class="boton-principal" style="cursor:pointer" data-alta>Regístrate gratis</span>'}
        for number, (name, control) in enumerate(controls.items()):
            with self.subTest(name):
                self.fresh()
                model = Modelo()
                await self.open('/registro', SCRIPTED % control)
                self.assertEqual((await self.step(self.flow(model, job=f'ticket-{number}')))[0], 'advanced')
                password = site_password(self.record_path(), False)
                self.assertEqual(self.posted, [('/api/alta', {'nombre': 'Ana María Pruebas Ficticia', 'correo': PROFILE.email,
                                                              'clave': password})])
                self.assertEqual((self.recorded[-1], len(model.seen)), (True, 1))

    async def test_other_names_on_a_real_form_and_weak_names_only_where_it_is_a_registration(self):
        form = ('<h1>%s</h1><form method="post" action="/alta"><label>Nombre(s)<input name="nombres" required></label>'
                '<label>Correo<input name="correo" type="email" required></label>%s<button>%s</button></form>')
        secret = '<label>Contraseña<input name="clave" type="password" autocomplete="new-password" required></label>'
        for number, (title, button) in enumerate([('Bienvenido', 'Regístrate'), ('Bienvenido', 'Registrarse'),
                                                  ('Bienvenido', 'Crear cuenta nueva'), ('Bienvenido', '¡Regístrate ahora!'),
                                                  ('Nuevo usuario', 'Continuar'), ('Registro', 'Enviar')]):
            with self.subTest(button):
                self.fresh()
                model = Modelo(button='regist|crear cuenta|continuar|enviar')
                await self.open('/registro', form % (title, secret, button))
                self.assertEqual((await self.step(self.flow(model, job=f'ticket-{number}')))[0], 'advanced')
                self.assertEqual(self.posted[0][1]['nombres'], 'Ana María')
        # «Continuar» under a heading that does not speak of an account is not a registration.
        self.fresh()
        model = Modelo()
        await self.open('/datos', form % ('Datos de contacto para tu registro', '', 'Continuar'))
        self.assertIsNone(await self.step(self.flow(model, job='ticket-x')))
        self.assertEqual((model.seen, self.posted), ([], []))

    async def test_password_never_travels_in_the_address(self):
        # A form without POST: only a script of the page may take it.
        form = ('<h1>Crea tu cuenta</h1><form action="/alta" %s><label>Correo<input name="correo" type="email" required></label>'
                '<label>Contraseña<input name="clave" type="password" required></label><button>Regístrate</button></form>')
        seen = []
        self.page.on('request', lambda request: seen.append(request.url))
        model = Modelo()
        await self.open('/registro', form % '')
        kind, message = await self.step(self.flow(model))
        self.assertEqual((kind, self.posted), ('login', []))
        self.assertIn('no tomó el envío', message)
        password = site_password(self.record_path(), False)
        self.assertFalse(any('clave=' in address or password in address for address in seen), seen)
        self.assertEqual(self.page.url, ORIGIN + '/registro')
        # The same form taken by a script is sent, by the script.
        scripted = form % ('onsubmit="event.preventDefault(); fetch(\'/api/alta\', {method: \'POST\', body: JSON.stringify('
                           '{correo: this.correo.value, clave: this.clave.value})})'
                           '.then(() => { document.body.innerHTML = \'<h1>Revisa tu correo</h1>\'; })"')
        # The script may listen on the form or on its button: either way it is the one that sends.
        on_button = (form % '') + ('<script>document.querySelector("button").addEventListener("click", event => { '
                                   'event.preventDefault(); const f = document.forms[0]; fetch("/api/alta", {method: "POST", '
                                   'body: JSON.stringify({correo: f.correo.value, clave: f.clave.value})})'
                                   '.then(() => { document.body.innerHTML = "<h1>Revisa tu correo</h1>"; }); });</script>')
        for number, body in enumerate((scripted, on_button)):
            self.fresh()
            await self.open('/registro', body)
            self.assertEqual((await self.step(self.flow(model, job=f'ticket-{number + 2}')))[0], 'advanced')
            self.assertEqual(self.posted, [('/api/alta', {'correo': PROFILE.email,
                                                          'clave': site_password(self.record_path(), False)})])
        self.assertFalse(any('clave=' in address for address in seen), seen)

    async def test_values_somebody_typed_are_kept_and_a_foreign_password_stops_the_send(self):
        form = ('<h1>Crea tu cuenta</h1><form method="post" action="/alta"><label>Nombre(s)<input name="nombres" required value="%s"></label>'
                '<label>Correo<input name="correo" type="email" required></label>'
                '<label>Contraseña<input name="clave" type="password" required value="%s"></label><button>Regístrate</button></form>')
        model = Modelo()
        await self.open('/registro', form % ('Escrito A Mano', ''))
        self.assertEqual((await self.step(self.flow(model)))[0], 'advanced')
        self.assertEqual(self.posted[0][1]['nombres'], 'Escrito A Mano')
        self.fresh()
        await self.open('/registro', form % ('', 'otra-que-no-es-la-nuestra'))
        kind, message = await self.step(self.flow(model, job='ticket-2'))
        self.assertEqual((kind, self.posted), ('login', []))
        self.assertIn('«Contraseña»', message)
        self.assertFalse(self.record().get('submission_attempted'))
        # What was already written in the fields never travels to the model: it is told labels, not values.
        self.assertEqual(len(model.seen), 2)
        for typed in ('Escrito A Mano', 'otra-que-no-es-la-nuestra'):
            self.assertNotIn(typed, '\n'.join(model.seen))

    async def test_lists_option_groups_and_masked_fields_are_filled_and_read_back(self):
        days = ''.join(f'<option value="{n}">{n:02d}</option>' for n in range(1, 32))
        months = ''.join(f'<option value="{n}">{name}</option>' for n, name in enumerate(
            ['Enero', 'Febrero', 'Marzo', 'Abril', 'Mayo', 'Junio', 'Julio', 'Agosto', 'Septiembre', 'Octubre', 'Noviembre',
             'Diciembre']))
        years = ''.join(f'<option>{n}</option>' for n in range(1985, 1996))
        form = ('<h1>Crea tu cuenta</h1><form method="post" action="/alta">'
                '<label>Correo<input name="correo" type="email" required></label>'
                '<fieldset><legend>Tipo de persona</legend><label><input type="radio" name="persona" value="F" required> Persona física'
                '</label><label><input type="radio" name="persona" value="M"> Persona moral</label></fieldset>'
                f'<label>Día de nacimiento<select name="dia" required><option value="">Día</option>{days}</select></label>'
                f'<label>Mes de nacimiento<select name="mes" required><option value="">Mes</option>{months}</select></label>'
                f'<label>Año de nacimiento<select name="ano" required><option value="">Año</option>{years}</select></label>'
                '<label>Celular<input name="celular" type="tel" required oninput="this.value=this.value.replace(/\\D/g, \'\')'
                '.replace(/(\\d{2})(\\d{4})(\\d{1,4})/, \'$1 $2 $3\')"></label>'
                '<label>Régimen fiscal<select name="regimen" required><option value="">Elige</option>'
                '<option value="r1">605 - Sueldos y salarios</option><option value="r2">612 - Actividades empresariales</option>'
                '</select></label><label>Comentarios<textarea name="notas"></textarea></label>%s'
                '<label>Contraseña<input name="clave" type="password" required></label><button>Regístrate</button></form>')
        model = Modelo()
        await self.open('/registro', form % '')
        self.assertEqual((await self.step(self.flow(model)))[0], 'advanced')
        password = site_password(self.record_path(), False)
        # The month list counts from zero and the phone field rewrites what is typed: both are read back.
        self.assertEqual(self.posted, [('/alta', {'correo': PROFILE.email, 'persona': 'F', 'dia': '17', 'mes': '4', 'ano': '1990',
                                                  'celular': '55 0000 1111', 'regimen': 'r2', 'notas': '', 'clave': password})])
        # A field that takes none of the ways the datum can be written stays empty and nothing is sent.
        self.fresh()
        await self.open('/registro', form % '<label>Teléfono fijo<input name="fijo" type="tel" pattern="[0-9]{8}" required></label>')
        kind, message = await self.step(self.flow(model, job='ticket-2'))
        self.assertEqual((kind, self.posted), ('login', []))
        self.assertEqual(message, 'Llené tu registro en el portal, pero falta revisar «Teléfono fijo». '
                                  'Complétalo ahí, envía el registro y pulsa Continuar.')
        self.assertEqual(await self.page.locator('input[name=fijo]').input_value(), '')
        self.assertEqual(await self.page.locator('select[name=mes]').input_value(), '4')

    async def test_signup_link_under_another_name_is_opened_once_and_other_accounts_never(self):
        requests = []
        self.page.on('request', lambda request: requests.append(urlsplit(request.url).path))
        self.pages['/google'] = '<h1>Otra cuenta</h1>'
        self.pages['/registro'] = COMPLETO
        wall = ('<h1>Inicia sesión</h1><form method="post" action="/session"><label>Correo<input name="correo" type="email" required>'
                '</label><label>Contraseña<input name="clave" type="password" required></label><button>Iniciar sesión</button></form>'
                '<a href="/google" class="btn">Registrarse con Google</a>'
                '<button type="button" onclick="location.href=\'/google\'">Entrar con Google</button>'
                '<p>¿No tienes cuenta? <a href="/registro">Regístrate aquí</a></p>')
        model = Modelo()
        await self.open('/acceso', wall)
        flow = self.flow(model)
        self.assertEqual(await self.step(flow), ('advanced', 'Se abrió el registro del comercio.'))
        await self.page.wait_for_url(ORIGIN + '/registro')
        self.assertEqual((await self.step(flow))[0], 'advanced')
        self.assertEqual((len(self.posted), self.posted[0][0]), (1, '/alta'))
        self.assertNotIn('/google', requests)
        # Without the permission the same wall is left to the person, as before.
        permitir(self.root, 'registrarme_en_portales', False)
        self.fresh()
        await self.open('/acceso')
        self.assertEqual(await self.step(self.flow(model, job='ticket-2')),
                         (HOLD, 'Inicia sesión en esta cuenta del comercio o abre su registro y pulsa Continuar.'))
        self.assertEqual(self.page.url, ORIGIN + '/acceso')


if __name__ == '__main__':
    unittest.main()
