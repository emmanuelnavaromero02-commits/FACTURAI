"""Assisted sign-up on the registration form of any portal: what the model is asked, how its plan
is checked before anything is touched, and how the form is filled, read back and sent.

Chrome runs against pages served from memory and the «model» is a function that only reads the
description it is given: no merchant portal, mailbox, chat or AI service is contacted. Every
person, phone and address here is made up.
"""
import asyncio
import json
import os
import re
import tempfile
import unicodedata
import unittest
from pathlib import Path
from types import SimpleNamespace
from typing import get_args
from unittest.mock import Mock, patch
from urllib.parse import parse_qs, urlsplit

from playwright.async_api import async_playwright

from facturador.account_flow import HOLD, PortalAccountFlow
from facturador.ai import AI
from facturador.auth import read_json, strict_schema
from facturador.models import Receipt, RegistrationField, RegistrationPlan, RegistrationSource
from facturador.permisos import guardar as permitir
from facturador.portal_accounts import credential_file, site_password
from facturador.registro_asistido import (FUENTES, MSG_TERMINOS, PROGRAMA, REHUSA, Pausa, PlanInvalido, datos_del_titular,
                                          resolver, sin_datos, vista_para_ia)
from facturador.registro_datos import guardar_datos
from facturador.runner import visible_challenge
from simulated_portal import CHROME
from test_core import PROFILE

ORIGIN = 'https://cuentas.tienda-ejemplo.mx'
PAGE = '<!doctype html><html lang="es"><head><meta charset="utf-8"><title>Tienda Ejemplo</title></head><body>%s</body></html>'
DATOS = {'nombres': 'Ana María', 'apellido_paterno': 'Pruebas', 'apellido_materno': 'Ficticia', 'telefono': '+525500001111',
         'fecha_nacimiento': '1990-05-17', 'calle': 'Avenida Siempre Viva', 'numero_exterior': '742', 'numero_interior': '',
         'colonia': 'Centro', 'municipio': 'Guadalajara', 'estado': 'Jalisco', 'pais': 'México'}
TODAY_PROTECTED = ('login', 'Completa la verificación o el acceso en el portal y vuelve al trámite.')
COMPLETO = ('<h1>Crea tu cuenta</h1><form method="post" action="/alta">'
            '<label>Nombre(s)<input name="nombres" required></label>'
            '<label>Apellido paterno<input name="paterno" required></label>'
            '<label>Apellido materno<input name="materno"></label>'
            '<label>Correo electrónico<input name="correo" type="email" required></label>'
            '<label>Teléfono celular (10 dígitos)<input name="telefono" type="tel" maxlength="10" required></label>'
            '<label>Fecha de nacimiento<input name="nacimiento" placeholder="dd/mm/aaaa" required></label>'
            '<label>Calle<input name="calle" required></label>'
            '<label>Número exterior<input name="exterior" required></label>'
            '<label>Número interior<input name="interior"></label>'
            '<label>Colonia<input name="colonia" required></label>'
            '<label>Municipio o ciudad<input name="municipio" required></label>'
            '<label>Estado<select name="estado" required><option value="">Elige</option><option value="9">Ciudad de México</option>'
            '<option value="14">Jalisco</option><option value="15">México</option></select></label>'
            '<label>Tipo de persona<select name="persona" required><option value="">Elige</option>'
            '<option value="F">Persona física</option><option value="M">Persona moral</option></select></label>'
            '<label>Contraseña<input name="clave" type="password" required></label>'
            '<label>Confirmar contraseña<input name="clave2" type="password" required></label>'
            '<label><input type="checkbox" name="terminos" required> Acepto los términos y condiciones y el aviso de privacidad</label>'
            '<label><input type="checkbox" name="promos"> Quiero recibir promociones</label>'
            '<button>Registrarme</button></form>')
ENVIADO = {'nombres': 'Ana María', 'paterno': 'Pruebas', 'materno': 'Ficticia', 'correo': PROFILE.email,
           'telefono': '5500001111', 'nacimiento': '17/05/1990', 'calle': 'Avenida Siempre Viva', 'exterior': '742',
           'interior': '', 'colonia': 'Centro', 'municipio': 'Guadalajara', 'estado': '14', 'persona': 'F', 'terminos': 'on'}
BY_LABEL = (('nombre completo', 'nombre_completo'), ('nombre', 'nombres'), ('apellido paterno', 'apellido_paterno'),
            ('apellido materno', 'apellido_materno'), ('apellidos', 'apellidos'), ('correo', 'correo'),
            ('telefono', 'telefono'), ('celular', 'telefono'), ('dia', 'dia_nacimiento'), ('mes', 'mes_nacimiento'),
            ('ano', 'anio_nacimiento'), ('nacimiento', 'fecha_nacimiento'), ('calle y numero', 'calle_y_numero'),
            ('calle', 'calle'), ('exterior', 'numero_exterior'), ('interior', 'numero_interior'), ('colonia', 'colonia'),
            ('municipio', 'municipio'), ('estado', 'estado'), ('pais', 'pais'), ('rfc', 'rfc'), ('razon social', 'nombre'),
            ('codigo postal', 'codigo_postal'), ('regimen', 'regimen'), ('uso', 'uso_cfdi'))


def plain(text):
    return ''.join(c for c in unicodedata.normalize('NFD', text.casefold()) if not unicodedata.combining(c))


def entry(field, source, option=None, missing=None):
    return RegistrationField(field=field, source=source, option=option, missing=missing)


class Modelo:
    """Stands in for the model. It reads the description it is given and answers with sources."""

    def __init__(self, decide=None, button=r'crear cuenta|registr|continuar|siguiente|finalizar'):
        self.seen: list[str] = []
        self.decide, self.button = decide, button

    def registration_plan(self, form):
        self.seen.append(form)
        view = json.loads(form)
        return self.decide(view) if self.decide else self.by_label(view)

    def by_label(self, view, **changes):
        fields, passwords = [], 0
        for campo in view['campos']:
            label = plain(campo['etiqueta'])
            if campo['tipo'] == 'contrasena':
                passwords += 1
                fields.append(entry(campo['id'], 'contrasena_nueva' if passwords == 1 else 'confirmar_contrasena'))
            elif campo['tipo'] == 'casilla':
                fields.append(entry(campo['id'], 'aceptar_terminos' if 'terminos' in label else 'omitir'))
            elif 'tipo de persona' in label:
                fields.append(entry(campo['id'], 'opcion', 'Persona ' + view['tipo_de_persona']))
            else:
                source = next((source for word, source in BY_LABEL if word in label), None)
                fields.append(entry(campo['id'], source) if source else
                              entry(campo['id'], 'falta_dato' if campo['obligatorio'] else 'omitir', missing=campo['etiqueta']))
        submit = next((b['id'] for b in view['botones'] if b['en_formulario'] and re.search(self.button, plain(b['texto']))), None)
        return RegistrationPlan(**{'is_registration': True, 'fields': fields, 'submit': submit, **changes})


class ContractTests(unittest.TestCase):
    """What the model is asked and what it may answer."""

    def test_answer_only_names_sources_of_the_closed_list(self):
        schema = strict_schema(RegistrationPlan)
        self.assertEqual(schema['required'], ['is_registration', 'fields', 'submit'])
        item = schema['$defs']['RegistrationField']
        self.assertEqual(item['required'], ['field', 'source', 'option', 'missing'])
        self.assertEqual(set(item['properties']['source']['enum']), set(get_args(RegistrationSource)))
        self.assertEqual(FUENTES, set(get_args(RegistrationSource)))
        with self.assertRaises(ValueError):
            entry('c1', 'curp')

    def test_one_consultation_at_medium_effort_with_every_source_explained(self):
        service = Mock()
        service.infer.return_value = RegistrationPlan(is_registration=False, fields=[], submit=None)
        answer = AI(service).registration_plan('{"campos": []}')
        self.assertIs(answer, service.infer.return_value)
        instruction, content, output = service.infer.call_args.args
        self.assertIs(output, RegistrationPlan)
        self.assertEqual(service.infer.call_args.kwargs, {'effort': 'medium'})
        self.assertEqual(content, [{'type': 'input_text', 'text': '{"campos": []}'}])
        for source in FUENTES:
            self.assertRegex(instruction, r'\b' + source + r'\b')

    def test_every_data_source_has_a_value_the_program_can_build(self):
        with tempfile.TemporaryDirectory() as folder:
            guardar_datos(Path(folder), {**DATOS, 'numero_interior': '4B'})
            values, saved = datos_del_titular(Path(folder), PROFILE)
        self.assertEqual(set(values), FUENTES - PROGRAMA)
        self.assertTrue(all(values.values()), values)
        self.assertEqual(values['nombre_completo'], 'Ana María Pruebas Ficticia')
        self.assertEqual(values['apellidos'], 'Pruebas Ficticia')
        self.assertEqual(values['calle_y_numero'], 'Avenida Siempre Viva 742 Int. 4B')
        self.assertEqual((values['dia_nacimiento'], values['mes_nacimiento'], values['anio_nacimiento']), ('17', '05', '1990'))
        self.assertEqual(saved['numero_interior'], '4B')

    def test_nothing_is_invented_when_the_person_left_no_data(self):
        with tempfile.TemporaryDirectory() as folder:
            values, _ = datos_del_titular(Path(folder), PROFILE)
            company = PROFILE.model_copy(update={'rfc': 'AAA010101AAA'})
            self.assertEqual(datos_del_titular(Path(folder), company)[0]['nombre_completo'], '')
        # A person's fiscal name is their own full name; nothing else is filled in.
        self.assertEqual(values['nombre_completo'], PROFILE.name)
        self.assertEqual({source for source in FUENTES - PROGRAMA if not values[source]},
                         {'nombres', 'apellido_paterno', 'apellido_materno', 'telefono', 'fecha_nacimiento', 'calle',
                          'numero_exterior', 'numero_interior', 'colonia', 'municipio', 'estado', 'pais', 'apellidos',
                          'dia_nacimiento', 'mes_nacimiento', 'anio_nacimiento', 'calle_y_numero'})


def field(number, kind, label, required=True, **more):
    return {'id': f'c{number}', 'tipo': kind, 'etiqueta': label, 'obligatorio': required, 'pista': '', 'lleno': False,
            'nombre': '', **more}


def view(fields, buttons=None, **more):
    buttons = buttons or [{'id': 'b1', 'texto': 'Crear cuenta', 'en_formulario': True, 'externo': False, 'prohibido': False}]
    return {'estado': 'formulario', 'campos': fields, 'botones': buttons, 'terminos_implicitos': False, **more}


class PlanCheckTests(unittest.TestCase):
    """The plan of the model is checked against the described form before anything is touched."""

    def setUp(self):
        folder = tempfile.TemporaryDirectory()
        self.addCleanup(folder.cleanup)
        guardar_datos(Path(folder.name), DATOS)
        self.values, self.saved = datos_del_titular(Path(folder.name), PROFILE)

    def solve(self, form, fields, submit='b1', terms=True, values=None, saved=None):
        plan = SimpleNamespace(is_registration=True, submit=submit,
                               fields=[SimpleNamespace(field=f, source=s, option=o, missing=m) for f, s, o, m in fields])
        return resolver(form, plan, values or self.values, saved or self.saved, terminos=terms)

    def test_sources_become_what_the_program_writes(self):
        form = view([field(1, 'texto', 'Nombre'), field(2, 'correo', 'Correo'), field(3, 'contrasena', 'Contraseña'),
                     field(4, 'contrasena', 'Repite la contraseña'), field(5, 'casilla', 'Acepto términos', terminos=True),
                     field(6, 'lista', 'Tipo', opciones=['Persona física', 'Persona moral'], valores=['F', 'M']),
                     field(7, 'texto', 'Cupón', required=False), field(8, 'telefono', 'Teléfono', maximo=10),
                     field(9, 'fecha', 'Nacimiento'), field(10, 'texto', 'Nacimiento (aaaa-mm-dd)')])
        ready = self.solve(form, [('c1', 'nombres', None, None), ('c2', 'correo', None, None),
                                  ('c3', 'contrasena_nueva', None, None), ('c4', 'confirmar_contrasena', None, None),
                                  ('c5', 'aceptar_terminos', None, None), ('c6', 'opcion', 'persona fisica', None),
                                  ('c7', 'omitir', None, None), ('c8', 'telefono', None, None),
                                  ('c9', 'fecha_nacimiento', None, None), ('c10', 'fecha_nacimiento', None, None)])
        written = {item['id']: item for item in ready['campos']}
        self.assertEqual((ready['boton'], ready['clave']), ('b1', True))
        self.assertEqual(ready['incompletos'], [])
        self.assertEqual(written['c1'], {'id': 'c1', 'tipo': 'texto', 'modo': 'texto', 'candidatos': ['Ana María'], 'exacto': False})
        self.assertEqual((written['c2']['candidatos'], written['c2']['exacto']), ([PROFILE.email], True))
        # The password is not in the plan: only the place where the program will type it.
        self.assertEqual((written['c3'], written['c4']), ({'id': 'c3', 'tipo': 'contrasena', 'modo': 'clave'},
                                                          {'id': 'c4', 'tipo': 'contrasena', 'modo': 'clave'}))
        self.assertEqual(written['c5']['modo'], 'marcar')
        self.assertEqual((written['c6']['modo'], written['c6']['indice']), ('opcion', 0))
        self.assertNotIn('c7', written)
        self.assertEqual(written['c8']['candidatos'], ['5500001111', '+525500001111'])
        self.assertEqual(written['c9']['candidatos'], ['1990-05-17'])
        self.assertEqual(written['c10']['candidatos'][0], '1990-05-17')

    def test_dates_and_lists_take_the_format_of_the_field(self):
        days = [str(n) for n in range(1, 32)]
        months = ['Enero', 'Febrero', 'Marzo', 'Abril', 'Mayo', 'Junio']
        form = view([field(1, 'lista', 'Día', opciones=days, valores=days),
                     field(2, 'lista', 'Mes', opciones=months, valores=[str(n) for n in range(6)]),
                     field(3, 'lista', 'Año', opciones=['1989', '1990', '1991'], valores=['1989', '1990', '1991']),
                     field(4, 'texto', 'Nacimiento', pista='ejemplo o formato: MM-DD-AAAA'),
                     field(5, 'lista', 'Estado', opciones=['Ciudad de México', 'Jalisco', 'México'], valores=['9', '14', '15']),
                     field(6, 'lista', 'País', opciones=['Estados Unidos', 'MX - México'], valores=['US', 'MX']),
                     field(7, 'lista', 'Régimen', opciones=['605 - Sueldos', '612 - Actividad empresarial'], valores=['a', 'b']),
                     field(8, 'opciones', 'Uso', opciones=['Gastos en general', 'Sin efectos fiscales'], valores=['1', '2'])])
        ready = self.solve(form, [('c1', 'dia_nacimiento', None, None), ('c2', 'mes_nacimiento', None, None),
                                  ('c3', 'anio_nacimiento', None, None), ('c4', 'fecha_nacimiento', None, None),
                                  ('c5', 'estado', None, None), ('c6', 'pais', None, None), ('c7', 'regimen', None, None),
                                  ('c8', 'uso_cfdi', None, None)])
        chosen = {item['id']: item.get('indice', item.get('candidatos')) for item in ready['campos']}
        # The month list counts its values from zero: the visible name decides.
        self.assertEqual(chosen, {'c1': 16, 'c2': 4, 'c3': 1, 'c4': chosen['c4'], 'c5': 1, 'c6': 1, 'c7': 1, 'c8': 0})
        self.assertEqual(chosen['c4'][0], '05-17-1990')

    def test_plan_out_of_bounds_is_rejected(self):
        text = [field(1, 'texto', 'Nombre'), field(2, 'contrasena', 'Contraseña')]
        fine = [('c1', 'nombres', None, None), ('c2', 'contrasena_nueva', None, None)]
        pay = {'id': 'b2', 'texto': 'Pagar ahora', 'en_formulario': True, 'externo': False, 'prohibido': False}
        outside = {'id': 'b3', 'texto': 'Crear cuenta', 'en_formulario': False, 'externo': False, 'prohibido': False}
        google = {'id': 'b4', 'texto': 'Continuar con Google', 'en_formulario': True, 'externo': False, 'prohibido': False}
        flagged = {'id': 'b5', 'texto': 'Siguiente', 'en_formulario': True, 'externo': False, 'prohibido': True}
        ok = {'id': 'b1', 'texto': 'Crear cuenta', 'en_formulario': True, 'externo': False, 'prohibido': False}
        cases = {
            'fuente fuera de la lista': (view(text), [('c1', 'curp', None, None), fine[1]], 'b1'),
            'valor libre en vez de fuente': (view(text), [('c1', 'Ana María', None, None), fine[1]], 'b1'),
            'campo que no existe': (view(text), [*fine, ('c9', 'nombres', None, None)], 'b1'),
            'campo repetido': (view(text), [*fine, ('c1', 'apellidos', None, None)], 'b1'),
            'botón de pago': (view(text, [ok, pay]), fine, 'b2'),
            'botón fuera del formulario': (view(text, [ok, outside]), fine, 'b3'),
            'botón de otra cuenta': (view(text, [ok, google]), fine, 'b4'),
            'botón marcado por la página': (view(text, [ok, flagged]), fine, 'b5'),
            'botón que no se mostró': (view(text), fine, 'b7'),
            'sin botón': (view(text), fine, None),
            'contraseña en un campo de texto': (view(text), [('c1', 'contrasena_nueva', None, None), fine[1]], 'b1'),
            'dato en el campo de contraseña': (view(text), [fine[0], ('c2', 'nombres', None, None)], 'b1'),
            'contraseña sin fuente': (view(text), [fine[0]], 'b1'),
            'confirmación sin contraseña': (view(text), [fine[0], ('c2', 'confirmar_contrasena', None, None)], 'b1'),
            'tres contraseñas': (view([*text, field(3, 'contrasena', 'Otra'), field(4, 'contrasena', 'Otra más')]),
                                 [*fine, ('c3', 'confirmar_contrasena', None, None), ('c4', 'confirmar_contrasena', None, None)], 'b1'),
            'campo de tarjeta': (view([*text, field(3, 'texto', 'Número de tarjeta', prohibido='tarjeta')]),
                                 [*fine, ('c3', 'telefono', None, None)], 'b1'),
            'opción que no se mostró': (view([*text, field(3, 'lista', 'Tipo', opciones=['Física', 'Moral'], valores=['F', 'M'])]),
                                        [*fine, ('c3', 'opcion', 'Extranjero', None)], 'b1'),
            'opción en un campo de texto': (view(text), [('c1', 'opcion', 'Ana', None), fine[1]], 'b1'),
            'términos en una casilla de publicidad': (view([*text, field(3, 'casilla', 'Recibir promociones', False, terminos=False)]),
                                                      [*fine, ('c3', 'aceptar_terminos', None, None)], 'b1'),
            'términos en un campo de texto': (view(text), [('c1', 'aceptar_terminos', None, None), fine[1]], 'b1'),
            'dato en una casilla': (view([*text, field(3, 'casilla', 'Acepto términos', terminos=True)]),
                                    [*fine, ('c3', 'nombres', None, None)], 'b1'),
            'correo con otro dato': (view([*text, field(3, 'correo', 'Correo')]), [*fine, ('c3', 'telefono', None, None)], 'b1'),
        }
        for name, (form, fields, submit) in cases.items():
            with self.subTest(name), self.assertRaises(PlanInvalido):
                self.solve(form, fields, submit)

    def test_missing_data_is_named_and_never_invented(self):
        form = view([field(1, 'texto', 'Nombre'), field(2, 'texto', 'Fecha de nacimiento'),
                     field(3, 'telefono', 'Teléfono'), field(4, 'texto', 'CURP'), field(5, 'texto', 'Apellido materno', False),
                     field(6, 'lista', 'Estado', opciones=['Jalisco', 'Nayarit'], valores=['1', '2']),
                     field(7, 'texto', 'Empresa donde trabajas'), field(8, 'texto', 'Puesto', False)])
        empty = {**self.saved, 'fecha_nacimiento': '', 'telefono': '', 'apellido_materno': '', 'estado': ''}
        values = {**self.values, 'fecha_nacimiento': '', 'telefono': '', 'apellido_materno': '', 'estado': '',
                  'dia_nacimiento': '', 'mes_nacimiento': '', 'anio_nacimiento': ''}
        plan = [('c1', 'nombres', None, None), ('c2', 'fecha_nacimiento', None, None), ('c3', 'telefono', None, None),
                ('c4', 'falta_dato', None, 'CURP'), ('c5', 'apellido_materno', None, None), ('c6', 'estado', None, None),
                ('c7', 'omitir', None, None), ('c8', 'falta_dato', None, 'puesto')]
        with self.assertRaises(Pausa) as paused:
            self.solve(form, plan, values=values, saved=empty)
        message = str(paused.exception)
        # Exactly the data to add in Ajustes, by their names there; optional ones are left alone
        # and no value of the person is repeated.
        self.assertEqual(message, 'Para crear tu cuenta en el portal me falta la fecha de nacimiento, el teléfono y el estado. '
                                  'Agrégalos en Ajustes, en «Datos para registros», y pulsa Continuar.')
        for value in DATOS.values():
            if value:
                self.assertNotIn(value, message)
        # One datum alone, named by the model as a key that is kept in «Datos para registros».
        with self.assertRaises(Pausa) as paused:
            self.solve(view([field(1, 'texto', 'Fecha en que naciste')]), [('c1', 'falta_dato', None, 'fecha_nacimiento')],
                       values=values, saved=empty)
        self.assertEqual(str(paused.exception), 'Para crear tu cuenta en el portal me falta la fecha de nacimiento. '
                                                'Agrégalo en Ajustes, en «Datos para registros», y pulsa Continuar.')
        # What only the portal asks (it is not kept in Ajustes) does not stop the rest from being
        # filled: those fields stay empty, are listed, and with any of them the form is never sent.
        ready = self.solve(form, plan)
        self.assertEqual(ready['incompletos'], ['CURP', 'Empresa donde trabajas'])
        self.assertEqual([item['id'] for item in ready['campos']], ['c1', 'c2', 'c3', 'c5', 'c6'])
        # A field that already has something typed by the person is not asked again.
        typed = view([field(1, 'texto', 'CURP', lleno=True), field(2, 'texto', 'Nombre')])
        ready = self.solve(typed, [('c1', 'falta_dato', None, 'CURP'), ('c2', 'nombres', None, None)])
        self.assertEqual(([item['id'] for item in ready['campos']], ready['incompletos']), (['c2'], []))

    def test_terms_need_their_own_permission(self):
        box = view([field(1, 'texto', 'Nombre'), field(2, 'casilla', 'Acepto los términos', terminos=True)])
        plan = [('c1', 'nombres', None, None), ('c2', 'aceptar_terminos', None, None)]
        with self.assertRaises(Pausa) as paused:
            self.solve(box, plan, terms=False)
        self.assertEqual(str(paused.exception), MSG_TERMINOS)
        self.assertEqual(self.solve(box, plan)['campos'][-1], {'id': 'c2', 'tipo': 'casilla', 'modo': 'marcar'})
        # A required terms box the plan skipped is still a decision of the owner, not of the model.
        skipped = [('c1', 'nombres', None, None), ('c2', 'omitir', None, None)]
        with self.assertRaises(Pausa):
            self.solve(box, skipped, terms=False)
        self.assertEqual(self.solve(box, skipped)['campos'][-1]['modo'], 'marcar')
        # Sending a form that says «al registrarte aceptas los términos» accepts them too.
        implied = view([field(1, 'texto', 'Nombre')], terminos_implicitos=True)
        with self.assertRaises(Pausa) as paused:
            self.solve(implied, plan[:1], terms=False)
        self.assertEqual(str(paused.exception), MSG_TERMINOS)
        self.assertEqual(len(self.solve(implied, plan[:1])['campos']), 1)
        # Missing data are asked first: both are fixed in Ajustes.
        with self.assertRaises(Pausa) as paused:
            self.solve(box, plan, terms=False, values={**self.values, 'nombres': ''}, saved={**self.saved, 'nombres': ''})
        self.assertIn('me falta el nombre', str(paused.exception))


class ModelViewTests(unittest.TestCase):
    """What travels to the model: labels and options, never a value, a link or a number."""

    def test_page_text_loses_values_links_addresses_and_numbers(self):
        private = {'Ana María', 'Pruebas', PROFILE.email, '+525500001111', 'Xy!_7segura-clave-99', 'Guadalajara', 'ab'}
        text = ('Hola ANA MARIA pruebas, te escribimos a prueba@example.com y al +525500001111. Tu clave es '
                'Xy!_7segura-clave-99. Activa en https://cuentas.tienda-ejemplo.mx/activar?t=SECRETO-1 '
                'Código 482913. Vives en guadalajara. Usa de 8 a 16 caracteres. Tabla ab.')
        clean = sin_datos(text, private)
        for secret in ('ana', 'maria', 'pruebas', 'example.com', '5500001111', 'segura', 'SECRETO', 'activar', '482913',
                       'guadalajara'):
            self.assertNotIn(secret, clean.casefold())
        self.assertIn('Usa de 8 a 16 caracteres', clean)
        self.assertIn('Tabla ab.', clean)

    def test_view_keeps_structure_and_drops_everything_internal(self):
        with tempfile.TemporaryDirectory() as folder:
            guardar_datos(Path(folder), {**DATOS, 'pais': ''})
            values, _ = datos_del_titular(Path(folder), PROFILE)
        form = view([field(1, 'texto', 'Nombre', pista='máximo 40 caracteres', lleno=True, nombre='nombre_interno'),
                     field(2, 'lista', 'Estado', opciones=['Jalisco', 'Nayarit'], valores=['VALOR-14', 'VALOR-18'], recortada=True),
                     field(3, 'contrasena', 'Contraseña')],
                    ruta='/registro/activar/TOKEN-DE-LA-LIGA', titulo='Registro de Ana María', firma='3:abc', huella='def',
                    encabezados=['Crea tu cuenta'], texto='Bienvenida Ana María Pruebas. Folio 123456.')
        sent = vista_para_ia(form, values, {*values.values(), 'clave-Generada_1!'}, 2)
        shown = json.loads(sent)
        self.assertEqual(set(shown), {'pantalla', 'titulo', 'encabezados', 'campos', 'botones', 'fuentes', 'tipo_de_persona', 'texto'})
        self.assertEqual(shown['campos'][0], {'id': 'c1', 'tipo': 'texto', 'etiqueta': 'Nombre', 'obligatorio': True,
                                              'pista': 'máximo 40 caracteres'})
        self.assertEqual(shown['campos'][1]['opciones'], ['Jalisco', 'Nayarit'])
        self.assertTrue(shown['campos'][1]['hay_mas_opciones'])
        self.assertEqual(shown['botones'], [{'id': 'b1', 'texto': 'Crear cuenta', 'en_formulario': True}])
        self.assertEqual((shown['pantalla'], shown['tipo_de_persona']), (2, 'física'))
        self.assertIn('pais', shown['fuentes']['vacias'])
        self.assertIn('nombres', shown['fuentes']['disponibles'])
        self.assertEqual(set(shown['fuentes']['disponibles']) | set(shown['fuentes']['vacias']), FUENTES - PROGRAMA)
        for hidden in ('TOKEN', 'activar', 'VALOR-14', 'nombre_interno', 'Ana', 'Pruebas', '123456', 'clave-Generada',
                       PROFILE.rfc, PROFILE.email, '5500001111', '1990', 'Siempre Viva', 'lleno', 'firma', 'huella'):
            self.assertNotIn(hidden, sent)


@unittest.skipUnless(os.path.isfile(CHROME), 'Requiere Google Chrome oficial')
class PortalCase(unittest.IsolatedAsyncioTestCase):
    """A made-up portal in a real Chrome and an account flow like the one the engine builds."""

    async def asyncSetUp(self):
        asyncio.get_running_loop().set_debug(False)
        folder = tempfile.TemporaryDirectory()
        self.addCleanup(folder.cleanup)
        self.root = Path(folder.name)
        self.playwright = await async_playwright().start()
        self.addAsyncCleanup(self.playwright.stop)
        self.browser = await self.playwright.chromium.launch(executable_path=CHROME, headless=True)
        self.addAsyncCleanup(self.browser.close)
        self.page = await self.browser.new_page()
        self.pages: dict[str, str] = {}
        self.posted: list[tuple[str, dict]] = []
        self.recorded: list[bool] = []
        self.said: list[str] = []
        self.meter = SimpleNamespace(ai_calls=0, waited=0.0)
        await self.page.route('**/*', self.portal)

    async def portal(self, route):
        request = route.request
        parts = urlsplit(request.url)
        if request.method == 'POST':
            raw = request.post_data or ''
            sent = json.loads(raw) if raw.startswith('{') else {k: v[0] for k, v in parse_qs(raw, keep_blank_values=True).items()}
            self.posted.append((parts.path, sent))
            # Whether the attempt was already on file when the request reached the merchant.
            self.recorded.append(bool(read_json(self.record_path(), {}).get('submission_attempted')))
        if parts.path not in self.pages:
            return await route.fulfill(status=404, body='')
        await route.fulfill(content_type='text/html; charset=utf-8', body=PAGE % self.pages[parts.path])

    def record_path(self):
        return credential_file(self.root, ORIGIN, PROFILE.email)

    def record(self):
        return read_json(self.record_path(), {})

    def allow(self, *names):
        for name in names or ('registrarme_en_portales', 'aceptar_terminos_de_registro'):
            permitir(self.root, name, True)

    def flow(self, model=None, job='ticket-1', **more):
        return PortalAccountFlow(self.root, ORIGIN + '/facturar', PROFILE, job, self.said.append, ai=model,
                                 medidor=self.meter, **more)

    async def open(self, path, body=None):
        if body is not None:
            self.pages[path] = body
        await self.page.goto(ORIGIN + path)

    async def step(self, flow):
        """One pass of the engine over the account flow, with the challenge the engine computes."""
        snapshot = await self.page.locator('body').aria_snapshot()
        challenge = await visible_challenge(self.page, snapshot)
        if challenge in {'captcha', 'attachment'}:
            return 'engine', challenge
        return await flow.step(self.page, snapshot, challenge)

    async def typed(self):
        """What every visible field holds, to prove a refused form was left alone."""
        return await self.page.evaluate(
            "() => Array.from(document.querySelectorAll('input, select')).map(e => "
            "e.type === 'checkbox' || e.type === 'radio' ? e.checked : e.value)")


class AssistedSignupTests(PortalCase):
    async def asyncSetUp(self):
        await super().asyncSetUp()
        guardar_datos(self.root, DATOS)
        self.pages['/alta'] = '<h1>Revisa tu correo</h1><p>Te enviamos un mensaje para activar tu cuenta.</p>'

    async def test_whole_form_is_registered_with_the_data_the_person_left(self):
        self.allow()
        model = Modelo()
        await self.open('/registro', COMPLETO)
        flow = self.flow(model)
        self.assertEqual(await self.step(flow), ('advanced', 'Se envió el registro del comercio con los datos que dejaste.'))
        self.assertEqual(len(self.posted), 1)
        path, sent = self.posted[0]
        password = site_password(self.record_path(), False)
        self.assertEqual(path, '/alta')
        self.assertEqual(sent, {**ENVIADO, 'clave': password, 'clave2': password})
        # The attempt was on file before the request left, and the screen is remembered as sent.
        self.assertEqual(self.recorded, [True])
        record = self.record()
        self.assertEqual((record['submission_attempted'], record['registered_with_password'], record['registration_job'],
                          len(record['assisted_screens'])), (True, True, 'ticket-1', 1))
        self.assertNotIn(password, self.record_path().read_text(encoding='utf-8'))
        self.assertEqual((len(model.seen), self.meter.ai_calls), (1, 1))
        self.assertIn(password, flow.credential_values)
        await self.page.wait_for_url(ORIGIN + '/alta')

    async def test_without_the_registration_permission_everything_stays_as_today(self):
        model = Modelo()
        await self.open('/registro', COMPLETO)
        before = await self.typed()
        self.assertEqual(await self.step(self.flow(model)), TODAY_PROTECTED)
        self.allow('aceptar_terminos_de_registro')          # the other permission alone changes nothing
        self.assertEqual(await self.step(self.flow(model)), TODAY_PROTECTED)
        self.assertEqual((model.seen, self.posted, await self.typed()), ([], [], before))
        self.assertFalse(self.record_path().exists())
        # And with the permission but nobody to plan the form (the engine gave no model).
        self.allow()
        self.assertEqual(await self.step(self.flow(None)), TODAY_PROTECTED)
        self.assertEqual((self.posted, await self.typed()), ([], before))

    async def test_without_the_terms_permission_it_waits_and_says_so(self):
        self.allow('registrarme_en_portales')
        model = Modelo()
        await self.open('/registro', COMPLETO)
        before = await self.typed()
        flow = self.flow(model)
        self.assertEqual(await self.step(flow), (HOLD, MSG_TERMINOS))
        self.assertIn('términos', MSG_TERMINOS)
        self.assertEqual((self.posted, await self.typed()), ([], before))
        self.assertFalse(self.record().get('submission_attempted'))
        # The owner grants it and resumes: the same consultation is used, nothing is asked twice.
        self.allow('aceptar_terminos_de_registro')
        self.assertEqual((await self.step(flow))[0], 'advanced')
        self.assertEqual((len(model.seen), len(self.posted), self.posted[0][1]['terminos']), (1, 1, 'on'))

    async def test_missing_datum_is_named_and_then_used_once_the_person_adds_it(self):
        self.allow()
        guardar_datos(self.root, {**DATOS, 'fecha_nacimiento': '', 'colonia': ''})
        model = Modelo()
        await self.open('/registro', COMPLETO)
        before = await self.typed()
        flow = self.flow(model)
        kind, message = await self.step(flow)
        self.assertEqual(kind, HOLD)
        self.assertEqual(message, 'Para crear tu cuenta en el portal me falta la fecha de nacimiento y la colonia. '
                                  'Agrégalos en Ajustes, en «Datos para registros», y pulsa Continuar.')
        self.assertEqual((self.posted, await self.typed()), ([], before))
        guardar_datos(self.root, DATOS)
        self.assertEqual((await self.step(flow))[0], 'advanced')
        self.assertEqual((len(model.seen), self.posted[0][1]['nacimiento'], self.posted[0][1]['colonia']),
                         (1, '17/05/1990', 'Centro'))

    async def test_what_only_the_portal_asks_is_left_to_the_person_with_the_rest_already_filled(self):
        self.allow()
        (self.root / 'gmail_token.json').write_text('{}')
        model = Modelo()
        await self.open('/registro', COMPLETO.replace('<button>Registrarme',
                                                      '<label>CURP<input name="curp" required></label><button>Registrarme'))
        flow = self.flow(model)
        self.assertEqual(await self.step(flow), ('login', 'Llené tu registro en el portal, pero falta revisar «CURP». '
                                                          'Complétalo ahí, envía el registro y pulsa Continuar.'))
        self.assertEqual(self.posted, [])
        record = self.record()
        self.assertNotIn('submission_attempted', record)
        self.assertEqual((record['registration_job'], record['registered_with_password']), ('ticket-1', True))
        self.assertEqual(await self.page.locator('input[name=curp]').input_value(), '')
        # The person types the one thing that was missing and sends the form themselves.
        await self.page.fill('input[name=curp]', 'CURP-DE-PRUEBA-01')
        await self.page.click('button')
        await self.page.wait_for_url(ORIGIN + '/alta')
        password = site_password(self.record_path(), False)
        self.assertEqual(self.posted, [('/alta', {**ENVIADO, 'clave': password, 'clave2': password, 'curp': 'CURP-DE-PRUEBA-01'})])
        # The enrollment is still this ticket's: its activation e-mail is awaited and used.
        self.pages['/activar'] = '<h1>Cuenta activada</h1>'
        with patch('facturador.account_flow.registration_email', return_value=ORIGIN + '/activar') as mailbox:
            self.assertEqual(await self.step(flow), ('advanced', 'Se abrió la activación de registro en el mismo comercio.'))
        mailbox.assert_called_once()
        self.assertEqual((len(model.seen), self.page.url), (1, ORIGIN + '/activar'))

    async def test_rules_go_first_and_the_assisted_path_takes_what_they_cannot_send(self):
        self.allow()
        model = Modelo()
        simple = ('<form method="post" action="/alta"><label>Correo<input name="email" type="email" required></label>'
                  '<label>Contraseña<input type="password" name="pass" required></label>%s<button>Crear cuenta</button></form>')
        # The minimal form is sent by the rules, exactly as before: the model is never consulted.
        await self.open('/registro', simple % '')
        self.assertEqual(await self.step(self.flow(model)),
                         ('advanced', 'Solicitud de registro enviada. Revisa la respuesta del comercio y tu correo.'))
        password = site_password(self.record_path(), False)
        self.assertEqual((self.posted, model.seen, self.recorded), ([('/alta', {'email': PROFILE.email, 'pass': password})], [], [True]))
        self.assertNotIn('assisted_screens', self.record())
        # One more required field and the rules stop; the same step then takes the assisted path.
        self.record_path().unlink()
        self.posted.clear()
        await self.open('/registro', simple % '<label>Nombre(s)<input name="nombre" required></label>')
        self.assertEqual((await self.step(self.flow(model, job='ticket-2')))[0], 'advanced')
        password = site_password(self.record_path(), False)
        self.assertEqual(self.posted, [('/alta', {'email': PROFILE.email, 'pass': password, 'nombre': 'Ana María'})])
        self.assertEqual(len(model.seen), 1)

    async def test_rules_alone_still_pause_on_the_form_they_cannot_send(self):
        form = ('<form method="post" action="/alta"><label>Correo<input name="email" type="email" required></label>'
                '<label>Contraseña<input type="password" name="pass" required></label>'
                '<label>Nombre(s)<input name="nombre" required></label><button>Crear cuenta</button></form>')
        model = Modelo()
        await self.open('/registro', form)
        self.assertEqual(await self.step(self.flow(model)), (HOLD, REHUSA[1]))
        self.assertEqual((self.posted, model.seen), ([], []))

    async def test_invalid_plans_are_rejected_and_nothing_is_touched(self):
        self.allow()
        await self.open('/registro', COMPLETO + '<form method="post" action="/pago"><button>Pagar ahora</button></form>'
                        '<button type="button" onclick="fetch(\'/google\', {method: \'POST\'})">Continuar con Google</button>')
        before = await self.typed()

        def out_of_list(shown):
            plan = Modelo().by_label(shown)
            plan.fields[0] = RegistrationField.model_construct(field='c1', source='curp', option=None, missing=None)
            return plan

        def pay_button(shown):
            return Modelo().by_label(shown, submit=next(b['id'] for b in shown['botones'] if b['texto'] == 'Pagar ahora'))

        def other_account(shown):
            return Modelo().by_label(shown, submit=next(b['id'] for b in shown['botones'] if 'Google' in b['texto']))

        def password_in_text(shown):
            plan = Modelo().by_label(shown)
            plan.fields[0] = entry('c1', 'contrasena_nueva')
            return plan

        def free_value(shown):
            plan = Modelo().by_label(shown)
            plan.fields[0] = RegistrationField.model_construct(field='c1', source='Ana María', option=None, missing=None)
            return plan

        for decide in (out_of_list, pay_button, other_account, password_in_text, free_value):
            with self.subTest(decide.__name__):
                model = Modelo(decide)
                self.assertEqual(await self.step(self.flow(model)), TODAY_PROTECTED)
                self.assertEqual((len(model.seen), self.posted, await self.typed()), (1, [], before))
                self.assertFalse(self.record().get('submission_attempted'))

    async def test_forms_that_are_never_touched_are_not_even_described_to_the_model(self):
        self.allow()
        card = COMPLETO.replace('<button>Registrarme', '<label>Número de plástico<input name="plastico" autocomplete="cc-number">'
                                '</label><button>Registrarme')
        cases = {
            'tarjeta': card,
            'tarjeta por su etiqueta': COMPLETO.replace('Número interior', 'Número de tarjeta de puntos'),
            'archivo': COMPLETO.replace('<button>', '<input type="file" name="ine"><button>'),
            'código de un solo uso': COMPLETO.replace('<button>', '<input name="otp" autocomplete="one-time-code"><button>'),
            'pago': COMPLETO.replace('<button>Registrarme', '<button>Registrarme y pagar'),
            'pago en un botón sin texto visible': COMPLETO.replace('<button>', '<input type="submit" value="Pagar membresía"><button>'),
            'emisión': COMPLETO.replace('</form>', '<p>Al continuar vamos a timbrar tu factura.</p></form>'),
            'otro dominio': COMPLETO.replace('action="/alta"', 'action="https://cobros.otro-ejemplo.mx/alta"'),
            'captcha': COMPLETO.replace('<button>', '<div data-sitekey="demo"></div><button>'),
        }
        for name, body in cases.items():
            with self.subTest(name):
                model = Modelo()
                await self.open('/registro', body)
                before = await self.typed()
                self.assertEqual(await self.step(self.flow(model)), TODAY_PROTECTED)
                self.assertEqual((model.seen, self.posted, await self.typed()), ([], [], before))

    async def test_the_form_that_bills_the_ticket_is_never_taken_for_a_registration(self):
        self.allow()
        model = Modelo()
        receipt = Receipt(merchant='Tienda Ejemplo', ticket_number='TKT-88231-Q', billing_codes=['COD-771-ZX9'], amount='312.40')
        lookup = ('<h1>Crea tu cuenta</h1><form method="post" action="/siguiente"><label>Folio del ticket<input name="folio" required>'
                  '</label><label>Correo<input name="correo" type="email" required></label><button>Continuar</button></form>')
        fiscal = ('<h1>Crea tu cuenta</h1><p>Ticket %s</p><form method="post" action="/siguiente">'
                  '<label>RFC<input name="rfc" required></label><label>Correo<input name="correo" type="email" required></label>'
                  '<button>Continuar</button></form>')
        hidden = ('<h1>Crea tu cuenta</h1><form method="post" action="/siguiente"><input type="hidden" name="t" value="%s">'
                  '<label>RFC<input name="rfc" required></label><label>Correo<input name="correo" type="email" required></label>'
                  '<button>Continuar</button></form>')
        self.pages['/siguiente'] = '<h1>Gracias</h1>'
        for body in (lookup, fiscal % 'TKT-88231-Q', fiscal % 'cod-771-zx9', hidden % 'TKT-88231-Q',
                     fiscal % 'por un total de $312.40'):
            await self.open('/registro', body)
            before = await self.typed()
            self.assertIsNone(await self.step(self.flow(model, ticket=receipt)))
            self.assertEqual((model.seen, self.posted, await self.typed()), ([], [], before))
        # The same page without this ticket on it is a registration like any other.
        await self.open('/registro', fiscal % 'de regalo en tu primera compra')
        self.assertEqual((await self.step(self.flow(model, ticket=receipt)))[0], 'advanced')
        self.assertEqual(self.posted, [('/siguiente', {'rfc': PROFILE.rfc, 'correo': PROFILE.email})])


if __name__ == '__main__':
    unittest.main()
