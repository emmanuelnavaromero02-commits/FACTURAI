"""Assisted sign-up for the registration form of any billing portal.

The fixed rules of ``portal_form.js`` only send minimal forms. When the owner allowed it
(``registrarme_en_portales``) a form they cannot send is described WITHOUT values, the model says
where the value of each field comes from by picking sources of a closed list, and this module
checks that plan, writes the values itself, reads them back and sends the form.

The password is generated and typed here and the model never sees it, nor any value of the
person: it only reads labels, types and the text of the page with those values blanked out.
Every screen is recorded before it is sent and is never sent twice.
"""
from __future__ import annotations

import asyncio
import json
import re
import time
import unicodedata
from pathlib import Path
from typing import get_args

from playwright.async_api import Error as BrowserError

from .auth import read_json, write_private
from .catalog import public_portal_origin
from .fiscal_choices import matches as sat_option
from .models import RegistrationSource
from .permisos import permitido
from .portal_accounts import FORM_SCRIPT, site_password
from .registro_datos import CAMPOS, datos_de_registro
from .uso_ia import en_tarea

# Two ways of stopping for the person. HOLD waits for something they fix outside the portal (a
# datum or a permission in Ajustes) and then goes on by itself. PORTAL waits for them to finish
# the step in the portal: the ticket goes on once the portal no longer asks for the access.
HOLD = 'unknown'
PORTAL = 'login'
# A registration the model planned out of bounds: nothing is touched and the person takes it.
# A caller that already had a pause for that page answers with its own.
REHUSA = (PORTAL, 'Revisa el registro y complétalo en el portal. Puede requerir verificación, condiciones u otros campos.')
# The closed list lives in the answer type; nothing outside it is ever written.
FUENTES = frozenset(get_args(RegistrationSource))
PERFIL = {'rfc': 'rfc', 'nombre': 'name', 'codigo_postal': 'postal_code', 'correo': 'email', 'regimen': 'regime',
          'uso_cfdi': 'cfdi_use'}
# Saved data each derived source needs.
REQUIERE = {'nombre_completo': ('nombres', 'apellido_paterno'), 'apellidos': ('apellido_paterno',),
            'dia_nacimiento': ('fecha_nacimiento',), 'mes_nacimiento': ('fecha_nacimiento',),
            'anio_nacimiento': ('fecha_nacimiento',), 'calle_y_numero': ('calle', 'numero_exterior')}
PROGRAMA = frozenset({'contrasena_nueva', 'confirmar_contrasena', 'aceptar_terminos', 'opcion', 'omitir', 'falta_dato'})
MAX_PANTALLAS = 6
MAX_RELECTURAS = 3
MESES = (('enero', 'ene', 'january', 'jan'), ('febrero', 'feb', 'february'), ('marzo', 'mar', 'march'),
         ('abril', 'abr', 'april', 'apr'), ('mayo', 'may'), ('junio', 'jun', 'june'), ('julio', 'jul', 'july'),
         ('agosto', 'ago', 'august', 'aug'), ('septiembre', 'setiembre', 'sep', 'sept', 'september'),
         ('octubre', 'oct', 'october'), ('noviembre', 'nov', 'november'), ('diciembre', 'dic', 'december', 'dec'))
# The same place under the names a list may use for it.
ALIAS = {'pais': (('mexico', 'mx', 'mex', 'estados unidos mexicanos'),),
         'estado': (('ciudad de mexico', 'cdmx', 'distrito federal', 'df'),
                    ('mexico', 'estado de mexico', 'edomex', 'edo de mexico', 'edo mex'))}
# A button the sign-up never presses, whatever the plan says: payments, purchases, emission, other accounts.
BOTON_AJENO = re.compile(r'\b(?:pag(?:ar|o|ue)|compr(?:ar|a)|factur\w*|emit\w*|timbr\w*|cfdi|donar|suscrib\w*|tarjeta|'
                         r'google|facebook|apple|microsoft|twitter|linkedin|github|outlook|yahoo|amazon)\b')

MSG_TERMINOS = ('El portal pide aceptar sus términos para crear tu cuenta. Dame ese permiso en Ajustes, en Permisos, '
                'y pulsa Continuar; o toma el control y termina tú el registro.')
MSG_REPETIDO = 'El portal volvió a mostrar un paso del registro que ya se envió. Revisa su respuesta en el navegador.'
MSG_TOPE = 'El registro de este portal tiene más pasos de los que puedo completar. Termínalo en el portal.'
MSG_CAMBIO = 'El formulario del registro cambió mientras lo preparaba. Revísalo en el portal y pulsa Continuar.'
MSG_ENVIADO = 'Se envió el registro del comercio con los datos que dejaste.'


class PlanInvalido(ValueError):
    """The plan of the model cannot be used as it came. Nothing was touched."""


class Pausa(Exception):
    """The form cannot be sent without the person. Carries the fixed message for them."""


def fold(text) -> str:
    plain = ''.join(char for char in unicodedata.normalize('NFD', str(text or '').casefold())
                    if not unicodedata.combining(char))
    return ' '.join(re.findall(r'[a-z0-9]+', plain))


def _plain(text) -> str:
    """Lower case without accents, keeping the punctuation a date format is written with."""
    return ''.join(char for char in unicodedata.normalize('NFD', str(text or '').casefold())
                   if not unicodedata.combining(char))


def _listado(names: list[str]) -> str:
    names = list(dict.fromkeys(names))
    return names[0] if len(names) == 1 else ', '.join(names[:-1]) + ' y ' + names[-1]


def _etiqueta(text) -> str:
    """A label of the page made safe to repeat in a message: short, one line, no markup."""
    text = re.sub(r'[\x00-\x1f\x7f<>«»"*:]', ' ', str(text or ''))
    return ' '.join(text.split())[:40]


def datos_del_titular(root, profile) -> tuple[dict[str, str], dict[str, str]]:
    """Every textual source with its value ('' when the person did not leave it), and the saved data as they are."""
    registro = datos_de_registro(root)
    # Same rule as the simple sign-up: the phone of the profile only when it is still this profile.
    telefono = getattr(profile, 'whatsapp_phone', '') or ''
    current = read_json(Path(root) / 'profile.json', {}) or {}
    if (current.get('rfc') and current['rfc'] == getattr(profile, 'rfc', None)
            and str(current.get('email', '')).casefold() == str(getattr(profile, 'email', '')).casefold()):
        telefono = current.get('whatsapp_phone', '') or ''
    valores = {fuente: str(getattr(profile, attribute, '') or '') for fuente, attribute in PERFIL.items()}
    valores.update(registro)
    valores['telefono'] = registro['telefono'] or telefono
    nombres, paterno, materno = registro['nombres'], registro['apellido_paterno'], registro['apellido_materno']
    valores['nombre_completo'] = ' '.join(filter(None, (nombres, paterno, materno))) if nombres and paterno else ''
    if not valores['nombre_completo'] and len(valores['rfc']) == 13:
        # A person's fiscal name already is their full name; a company's is not anybody's.
        valores['nombre_completo'] = valores['nombre']
    valores['apellidos'] = ' '.join(filter(None, (paterno, materno))) if paterno else ''
    interior = registro['numero_interior'] and 'Int. ' + registro['numero_interior']
    valores['calle_y_numero'] = (' '.join(filter(None, (registro['calle'], registro['numero_exterior'], interior)))
                                 if registro['calle'] and registro['numero_exterior'] else '')
    year, month, day = (registro['fecha_nacimiento'].split('-') + ['', '', ''])[:3]
    valores.update(dia_nacimiento=day, mes_nacimiento=month, anio_nacimiento=year)
    return valores, registro


def _faltantes(fuente: str, valores: dict, registro: dict) -> list[str]:
    """Which saved data are missing for this source, by their key in «Datos para registros»."""
    if fuente == 'telefono':
        return [] if valores['telefono'] else ['telefono']
    if fuente == 'nombre_completo' and valores['nombre_completo']:
        return []
    return [key for key in REQUIERE.get(fuente, (fuente,)) if key in registro and not registro[key]]


def _candidatos(fuente: str, campo: dict, valores: dict) -> list[str]:
    """What may be typed for this source, in order of preference; the page keeps the first it accepts."""
    valor = valores.get(fuente, '')
    if not valor:
        return []
    hint = _plain(campo.get('etiqueta', '') + ' ' + campo.get('pista', ''))
    if fuente == 'telefono':
        digits = re.sub(r'\D', '', valor)
        national = digits[-10:] if re.fullmatch(r'(?:52)?\d{10}', digits) else ''
        return list(dict.fromkeys(filter(None, (national, valor))))
    if fuente == 'fecha_nacimiento':
        year, month, day = valor.split('-')
        if campo['tipo'] == 'fecha':
            return [valor]
        parts = {'dd': day, 'mm': month, 'aaaa': year, 'yyyy': year, 'aa': year[2:], 'yy': year[2:]}
        asked = re.search(r'\b(dd|mm|aaaa|yyyy|aa|yy)([/.\- ]?)(dd|mm|aaaa|yyyy)\2(dd|mm|aaaa|yyyy|aa|yy)\b', hint)
        first = asked.group(2).join(parts[asked.group(index)] for index in (1, 3, 4)) if asked else ''
        return list(dict.fromkeys(filter(None, (first, f'{day}/{month}/{year}', valor, f'{day}-{month}-{year}',
                                                f'{day}{month}{year}'))))
    if fuente in {'dia_nacimiento', 'mes_nacimiento'}:
        return [valor, str(int(valor))]
    if fuente == 'anio_nacimiento' and campo.get('maximo') == 2:
        return [valor[2:]]
    return [valor]


def _opcion(fuente: str, campo: dict, valores: dict) -> int | None:
    """The one option of a list that stands for this source, or None when it is not there or not clear."""
    valor = valores.get(fuente, '')
    textos = [fold(text) for text in campo.get('opciones', [])]
    nativos = [fold(value) for value in campo.get('valores', [])]
    if not valor or not textos:
        return None

    def only(hits):
        return hits[0] if len(hits) == 1 else None

    if fuente in {'regimen', 'uso_cfdi'}:
        kind = 'regime' if fuente == 'regimen' else 'use'
        return only([index for index, text in enumerate(campo['opciones'])
                     if sat_option(str(campo.get('valores', [''] * len(textos))[index]), text, kind, valor)])
    if fuente in {'dia_nacimiento', 'mes_nacimiento', 'anio_nacimiento'}:
        number = int(valor)
        names = MESES[number - 1] if fuente == 'mes_nacimiento' else ()
        # The visible text decides first: some lists count their values from zero.
        for pool in (textos, nativos):
            found = only([index for index, text in enumerate(pool)
                          if text and ((text.split()[0].isdigit() and int(text.split()[0]) == number)
                                       or any(word in names for word in text.split()))])
            if found is not None:
                return found
        return None
    wanted = [fold(valor)]
    for names in ALIAS.get(fuente, ()):
        if wanted[0] in names:
            wanted.extend(names)
    for name in wanted:
        for pool in (textos, nativos):
            found = only([index for index, text in enumerate(pool) if text == name])
            if found is not None:
                return found
    name = wanted[0]
    if len(name) >= 4:
        # «Coahuila» for «Coahuila de Zaragoza», or the other way round, when only one option fits.
        return only([index for index, text in enumerate(textos)
                     if re.search(rf'\b{re.escape(name)}\b', text) or (len(text) >= 4 and re.search(rf'\b{re.escape(text)}\b', name))])
    return None


def mensaje_faltantes(claves: list[str]) -> str:
    """Names exactly what to add in «Datos para registros»; never a value, only which datum."""
    claves = list(dict.fromkeys(claves))
    return ('Para crear tu cuenta en el portal me falta ' + _listado([CAMPOS[key] for key in claves]) + '. ' +
            ('Agrégalo' if len(claves) == 1 else 'Agrégalos') + ' en Ajustes, en «Datos para registros», y pulsa Continuar.')


def mensaje_pendientes(etiquetas: list[str]) -> str:
    """What the portal still asks after everything known was filled, by the labels of its own fields."""
    names = []
    for name in filter(None, map(_etiqueta, etiquetas)):
        if fold(name) not in map(fold, names):
            names.append(name)
    names = names[:3]
    if not names:
        return REHUSA[1]
    return ('Llené tu registro en el portal, pero falta revisar ' + _listado(['«' + name + '»' for name in names]) +
            '. Complétalo ahí, envía el registro y pulsa Continuar.')


def resolver(vista: dict, plan, valores: dict, registro: dict, *, terminos: bool) -> dict:
    """Check the plan against the described form before anything is touched.

    Returns what to write in each field, the button to press and, in ``incompletos``, the required
    fields nothing covers: with any of those the form is filled but never sent. Raises
    ``PlanInvalido`` when the plan steps outside what is allowed and ``Pausa`` when the person has
    to add a datum or a permission in Ajustes first.
    """
    campos = {campo['id']: campo for campo in vista['campos']}
    elegidas = {}
    for entry in getattr(plan, 'fields', None) or []:
        field, fuente = getattr(entry, 'field', None), getattr(entry, 'source', None)
        if field not in campos or field in elegidas or not isinstance(fuente, str) or fuente not in FUENTES:
            raise PlanInvalido('fuente o campo fuera de la lista')
        elegidas[field] = entry
    fuentes = [getattr(entry, 'source') for entry in elegidas.values()]
    claves = [campo for campo in vista['campos'] if campo['tipo'] == 'contrasena']
    if (len(claves) > 2 or fuentes.count('contrasena_nueva') > 1 or fuentes.count('confirmar_contrasena') > 1
            or ('confirmar_contrasena' in fuentes and 'contrasena_nueva' not in fuentes)):
        raise PlanInvalido('contraseñas')
    boton = next((item for item in vista['botones'] if item['id'] == getattr(plan, 'submit', None)), None)
    if (boton is None or not boton.get('en_formulario') or boton.get('externo') or boton.get('prohibido')
            or BOTON_AJENO.search(fold(boton.get('texto')))):
        raise PlanInvalido('botón')
    escribir, faltan, otros, aceptar = [], [], [], False
    for campo in vista['campos']:
        entry = elegidas.get(campo['id'])
        fuente = getattr(entry, 'source', 'omitir')
        tipo, base = campo['tipo'], {'id': campo['id'], 'tipo': campo['tipo']}
        pendiente = bool(campo.get('obligatorio')) and not campo.get('lleno')
        if campo.get('prohibido'):
            raise PlanInvalido('campo prohibido')
        if tipo == 'contrasena' or fuente in {'contrasena_nueva', 'confirmar_contrasena'}:
            # Only the generated password goes in a password field, and nowhere else.
            if tipo != 'contrasena' or fuente not in {'contrasena_nueva', 'confirmar_contrasena'}:
                raise PlanInvalido('contraseña fuera de su campo')
            escribir.append({**base, 'modo': 'clave'})
        elif tipo == 'casilla':
            if fuente not in {'aceptar_terminos', 'omitir', 'falta_dato'} or (
                    fuente == 'aceptar_terminos' and not campo.get('terminos')):
                raise PlanInvalido('casilla')
            # A terms box the form requires is accepted by the owner's permission even if the plan skipped it.
            if not campo.get('lleno') and (fuente == 'aceptar_terminos' or (pendiente and campo.get('terminos'))):
                aceptar = True
                escribir.append({**base, 'modo': 'marcar'})
            elif pendiente:
                otros.append(_etiqueta(campo.get('etiqueta')))
        elif fuente == 'aceptar_terminos':
            raise PlanInvalido('términos fuera de su casilla')
        elif fuente == 'opcion':
            wanted = fold(getattr(entry, 'option', None))
            hits = [index for index, text in enumerate(campo.get('opciones', [])) if fold(text) == wanted]
            if tipo not in {'lista', 'opciones'} or not wanted or len(hits) != 1:
                raise PlanInvalido('opción que no se mostró')
            escribir.append({**base, 'modo': 'opcion', 'indice': hits[0]})
        elif fuente in {'omitir', 'falta_dato'} or tipo == 'otro':
            if pendiente:
                named = fold(getattr(entry, 'missing', None)).replace(' ', '_')
                if named in registro and not registro[named]:
                    faltan.append(named)
                else:
                    otros.append(_etiqueta(campo.get('etiqueta')) or _etiqueta(getattr(entry, 'missing', None)))
        elif tipo == 'correo' and fuente != 'correo':
            raise PlanInvalido('correo con otro dato')
        else:
            index = _opcion(fuente, campo, valores) if tipo in {'lista', 'opciones'} else None
            candidatos = [] if tipo in {'lista', 'opciones'} else _candidatos(fuente, campo, valores)
            if index is not None:
                escribir.append({**base, 'modo': 'opcion', 'indice': index})
            elif candidatos:
                escribir.append({**base, 'modo': 'texto', 'candidatos': candidatos, 'exacto': fuente == 'correo'})
            elif pendiente:
                # Nothing to put there: a datum the person can add, or something only the portal knows.
                missing = _faltantes(fuente, valores, registro)
                faltan.extend(missing)
                if not missing:
                    otros.append(_etiqueta(campo.get('etiqueta')))
    if faltan:
        raise Pausa(mensaje_faltantes(faltan))
    if (aceptar or vista.get('terminos_implicitos')) and not terminos:
        raise Pausa(MSG_TERMINOS)
    return {'campos': escribir, 'boton': boton['id'], 'clave': any(item['modo'] == 'clave' for item in escribir),
            'incompletos': list(dict.fromkeys(name or 'un campo obligatorio' for name in otros))}


def _patron(valor: str) -> str:
    """A pattern that finds the value with or without its accents and in any case."""
    accents = {'a': 'aáàäâ', 'e': 'eéèëê', 'i': 'iíìïî', 'o': 'oóòöô', 'u': 'uúùüû', 'n': 'nñ'}
    pieces = []
    for char in _plain(valor):
        pieces.append(r'\s+' if char.isspace() else '[' + accents[char] + ']' if char in accents else re.escape(char))
    return ''.join(pieces)


def sin_datos(texto, privados) -> str:
    """Page text made safe for the model: no value of the person, no link, address or number."""
    texto = str(texto or '')
    for valor in sorted({str(value) for value in privados if value and len(str(value)) >= 3}, key=len, reverse=True):
        texto = re.sub(_patron(valor), '[dato]', texto, flags=re.I)
    texto = re.sub(r'https?://\S+', '[liga]', texto)
    texto = re.sub(r'[\w.+%-]+@[\w-]+(?:\.[\w-]+)+', '[correo]', texto)
    return re.sub(r'\d{4,}', '[número]', texto)


def vista_para_ia(vista: dict, valores: dict, privados, pantalla: int) -> str:
    """The form as the model may see it: labels, types and options, never a value nor an address."""
    def clean(text):
        return sin_datos(text, privados)

    campos = []
    for campo in vista['campos']:
        item = {'id': campo['id'], 'tipo': campo['tipo'], 'etiqueta': clean(campo.get('etiqueta')),
                'obligatorio': bool(campo.get('obligatorio')), 'pista': clean(campo.get('pista'))}
        if 'opciones' in campo:
            # Lists belong to the page and are the same for anybody; they are shown as they are.
            item['opciones'] = [str(text) for text in campo['opciones']]
            if campo.get('recortada'):
                item['hay_mas_opciones'] = True
        campos.append(item)
    disponibles = sorted(fuente for fuente in FUENTES - PROGRAMA if valores.get(fuente))
    return json.dumps({
        'pantalla': pantalla, 'titulo': clean(vista.get('titulo')),
        'encabezados': [clean(text) for text in vista.get('encabezados', [])], 'campos': campos,
        'botones': [{'id': boton['id'], 'texto': clean(boton.get('texto')), 'en_formulario': bool(boton.get('en_formulario'))}
                    for boton in vista['botones']],
        'fuentes': {'disponibles': disponibles, 'vacias': sorted(FUENTES - PROGRAMA - set(disponibles))},
        'tipo_de_persona': 'moral' if len(valores.get('rfc', '')) == 12 else 'física',
        'texto': clean(vista.get('texto'))}, ensure_ascii=False)


async def describir(page, origin: str, *, marcas=(), siguiente=False, ruta='') -> dict:
    """The registration form of the page without values, or ``{'estado': 'no'}`` when it shows none."""
    try:
        vista = await page.evaluate(FORM_SCRIPT, {'origin': origin, 'action': 'describe', 'profile': {}, 'password': '',
                                                  'plan': {'marks': list(marcas), 'continuing': bool(siguiente),
                                                           'previousPath': ruta}})
    except BrowserError:
        return {'estado': 'no'}
    return vista if isinstance(vista, dict) and vista.get('estado') else {'estado': 'no'}


class AltaAsistida:
    """One ticket's assisted sign-up on one portal: a handful of screens, each consulted and sent once."""

    def __init__(self, flow):
        self.flow = flow
        self.planes: dict[str, object] = {}
        self.consultas = 0
        self.relecturas = 0
        self.ruta = ''

    def permitida(self) -> bool:
        return self.flow.ai is not None and permitido(self.flow.root, 'registrarme_en_portales')

    async def paso(self, page, path, siguiente: bool = False):
        """Fill and send the registration screen on the page.

        Returns ``None`` when the page shows no registration this ticket may take, ``REHUSA`` when
        it shows one that must stay with the person, or the result of the step: ``advanced`` after
        a send, a pause with its message otherwise.
        """
        flow = self.flow
        if not self.permitida():
            return None
        record = read_json(path, {})
        pantallas = list(record.get('assisted_screens') or [])
        propia = record.get('registration_job') == flow.job_id and bool(pantallas)
        if record.get('submission_attempted') and not propia:
            return None  # Somebody already sent a sign-up here: it is never repeated blindly.
        contexto = {'marcas': flow.ticket_marks, 'siguiente': propia and siguiente, 'ruta': self.ruta}
        vista = await describir(page, flow.origin, **contexto)
        if vista['estado'] != 'formulario' or vista['bloqueos']:
            # No registration here, or one with a verification, a payment, a card, a file or a
            # one-time code: it is not touched nor described, and the page is handled as before.
            return None
        if vista['huella'] in pantallas:
            return PORTAL, MSG_REPETIDO
        if len(pantallas) >= MAX_PANTALLAS:
            return PORTAL, MSG_TOPE
        valores, registro = datos_del_titular(flow.root, flow.profile)
        plan = self.planes.get(vista['firma'])
        if plan is None:
            if self.consultas >= MAX_PANTALLAS:
                return PORTAL, MSG_TOPE
            # A page that shows a credential or an access check is never described to the model.
            visible = [vista.get('titulo'), vista.get('texto'), *vista.get('encabezados', []),
                       *(campo.get('etiqueta') for campo in vista['campos']),
                       *(campo.get('pista') for campo in vista['campos']),
                       *(boton.get('texto') for boton in vista['botones'])]
            if flow.private_observation('\n'.join(str(text or '') for text in visible)):
                return None
            privados = {*flow.credential_values, getattr(flow.profile, 'whatsapp_phone', ''),
                        *(value for fuente, value in valores.items() if fuente not in {'regimen', 'uso_cfdi'})}
            texto = vista_para_ia(vista, valores, privados, len(pantallas) + 1)
            self.consultas += 1
            if flow.meter is not None:
                flow.meter.ai_calls += 1
            flow.say('Leyendo el formulario de registro del portal…')
            try:
                with en_tarea('paso_portal'):
                    plan = await asyncio.to_thread(flow.ai.registration_plan, texto)
            except ValueError:
                return None  # No answer from the model: the screen is handled as before.
            self.planes[vista['firma']] = plan
        if getattr(plan, 'is_registration', None) is not True:
            return None
        try:
            listo = resolver(vista, plan, valores, registro,
                             terminos=permitido(flow.root, 'aceptar_terminos_de_registro'))
        except PlanInvalido:
            return REHUSA
        except Pausa as pausa:
            return HOLD, str(pausa)
        # Like the simple sign-up, the password of this account exists (and its vault with it) before
        # anything is written about the attempt, whether or not this screen asks for it.
        password = site_password(path, True)
        flow.credential_values.add(password)
        flow.say('Llenando tu registro en el portal…')
        handle = None
        try:
            handle = await page.evaluate_handle(FORM_SCRIPT, {
                'origin': flow.origin, 'action': 'assist', 'profile': {}, 'password': password if listo['clave'] else '',
                'plan': {'firma': vista['firma'], 'campos': listo['campos'], 'boton': listo['boton'],
                         'email': flow.profile.email, 'marks': list(contexto['marcas']),
                         'continuing': bool(contexto['siguiente']), 'previousPath': contexto['ruta']}})
            resultado = await handle.evaluate('r => ({estado: r.estado, pendientes: r.pendientes || []})')
            if resultado['estado'] not in {'listo', 'pendiente'}:
                # The form is not the one that was planned: it is read again, a few times at most.
                self.relecturas += 1
                if self.relecturas > MAX_RELECTURAS:
                    return PORTAL, MSG_CAMBIO
                if resultado['estado'] == 'ampliado':
                    return 'advanced', 'Se llenó una parte del registro y el portal pidió más datos.'
                return 'advanced', 'El formulario del registro cambió; se vuelve a leer.'
            if resultado['estado'] != 'listo' or listo['incompletos']:
                # Everything known is in the form and the rest is the person's: they finish and send
                # it in the portal. The enrollment is still this ticket's, so its e-mail is awaited.
                record = read_json(path, {})
                if not record.get('submission_attempted'):
                    write_private(path, {**record, 'registration_job': flow.job_id, 'registration_started': time.time(),
                                         'registered_with_password': bool(listo['clave'])})
                return PORTAL, mensaje_pendientes([*listo['incompletos'], *resultado['pendientes']])
            record = read_json(path, {})
            pantallas = list(record.get('assisted_screens') or [])
            if vista['huella'] in pantallas:
                return PORTAL, MSG_REPETIDO
            primera = not record.get('submission_attempted')
            # Persist before contacting the site. An uncertain response must never cause a second submission.
            write_private(path, {**record, 'submission_attempted': True,
                                 'registered_with_password': bool(listo['clave'] or (
                                     not primera and record.get('registered_with_password'))),
                                 'registration_job': flow.job_id,
                                 'registration_started': time.time() if primera else record.get(
                                     'registration_started', time.time()),
                                 'assisted_screens': [*pantallas, vista['huella']]})
            try:
                enviado = await handle.evaluate('r => r.submit()')
            except BrowserError:
                enviado = None
        except BrowserError:
            raise ValueError('La página cambió durante el llenado. Vuelve a revisar el portal.') from None
        finally:
            if handle:
                try:
                    await handle.dispose()
                except BrowserError:
                    pass
        if enviado == 'enviado':
            self.ruta = vista['ruta']
            self.relecturas = 0
            await self._asentar(page, vista['firma'], contexto)
            return 'advanced', MSG_ENVIADO
        if enviado == 'sin_envio':
            return PORTAL, 'El portal no tomó el envío del registro. Termínalo en el portal y pulsa Continuar.'
        if enviado is None:
            return PORTAL, 'La página cambió durante el envío. Revisa el resultado y tu correo antes de repetir.'
        return PORTAL, 'El formulario cambió. Revísalo en el navegador.'

    async def _asentar(self, page, firma: str, contexto: dict) -> None:
        # Sites answer after the click, through a script or with another page; observe before
        # deciding the form is stuck, and let a page that is arriving finish.
        for _ in range(40):
            if page.is_closed() or public_portal_origin(page.url) != self.flow.origin:
                return
            vista = await describir(page, self.flow.origin, **{**contexto, 'siguiente': True})
            if vista['estado'] != 'formulario' or vista.get('firma') != firma:
                break
            await asyncio.sleep(.3)
        for _ in range(30):
            try:
                # Answers once the document that is arriving exists.
                if await page.evaluate('() => document.readyState') != 'loading':
                    return
            except BrowserError:
                if page.is_closed():
                    return
            await asyncio.sleep(.1)
