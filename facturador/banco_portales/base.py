"""What every simulated portal of the bank shares: requests, pages, fiscal forms and a test CFDI.

Each portal is an invented brand on a reserved ``.example`` domain. It is served from
memory through route interception, validates in the page and on its «server» what is
captured, and hands out a test CFDI (XML and PDF) built from the data it received.
Nothing here touches the network or emits a real invoice.
"""
from __future__ import annotations

import html
import io
import json
import re
import uuid
import zipfile
from dataclasses import dataclass, field
from datetime import date
from email.parser import BytesParser
from email.policy import default as email_policy
from urllib.parse import parse_qs, urlsplit

from ..models import Action, Plan, Receipt, Target

# The issuer of every test CFDI: the RFC the SAT publishes for its own test certificates.
EMISOR = 'EKU9003173C9'
RFC = r'[A-ZÑ&]{3,4}[0-9]{6}[A-Z0-9]{3}'
CP = r'[0-9]{5}'
CORREO = r'[^@\s]+@[^@\s]+\.[^@\s]+'
FECHA = r'[0-9]{2}/[0-9]{2}/[0-9]{4}'
IMPORTE = r'[0-9]+(?:\.[0-9]{2})?'
REGIMENES = [('605', '605 - Sueldos y Salarios e Ingresos Asimilados a Salarios'),
             ('612', '612 - Personas Físicas con Actividades Empresariales y Profesionales'),
             ('626', '626 - Régimen Simplificado de Confianza')]
REGIMENES_MORAL = [('601', '601 - General de Ley Personas Morales'),
                   ('603', '603 - Personas Morales con Fines no Lucrativos')]
USOS = [('G01', 'G01 - Adquisición de mercancías'), ('G03', 'G03 - Gastos en general'),
        ('S01', 'S01 - Sin efectos fiscales')]
PAGOS = [('01', '01 - Efectivo'), ('04', '04 - Tarjeta de crédito'), ('28', '28 - Tarjeta de débito')]
ESTILO = ('<style>body{font-family:Arial,sans-serif;max-width:760px;margin:24px auto;padding:0 14px;color:#222}'
          'label{display:block;margin-top:10px}input,select{display:block;margin-top:4px;padding:6px;width:320px}'
          'input[type=checkbox]{display:inline;width:auto}button{margin-top:14px;padding:8px 18px}'
          '.error{color:#b00020;margin:2px 0}.modal{position:fixed;inset:0;background:rgba(0,0,0,.55);display:flex;'
          'align-items:center;justify-content:center}.modal>div{background:#fff;padding:22px;max-width:440px}'
          '.lista{margin-top:10px;width:330px}.actual{border:1px solid #888;padding:6px;cursor:pointer}'
          '.opcion{padding:6px;cursor:pointer;border-bottom:1px solid #eee}</style>')
# Validation that runs in every page: each field with data-regla must match it before sending.
VALIDA = r"""<script>
function revisa(form) {
  let bien = true;
  form.querySelectorAll('[data-regla]').forEach(e => {
    const valor = (e.value || '').trim();
    const malo = (e.required && !valor) || (valor && !new RegExp('^(?:' + e.dataset.regla + ')$').test(valor));
    const aviso = document.getElementById('error-' + e.id);
    if (aviso) aviso.textContent = malo ? e.dataset.mensaje : '';
    if (malo) bien = false;
  });
  return bien;
}
</script>"""


def pagina(marca: str, cuerpo: str, script: str = '') -> str:
    return ('<!doctype html><html lang="es"><head><meta charset="utf-8"><title>' + html.escape(marca) +
            ' · Facturación</title>' + ESTILO + VALIDA + '</head><body>' + cuerpo + script + '</body></html>')


def campo(ident: str, etiqueta: str, regla: str = '', mensaje: str = 'Revisa este dato', *, tipo: str = 'text',
          extra: str = '', valor: str = '') -> str:
    """A labelled input with its rule and the place where the page writes its error."""
    regla_html = f' data-regla="{html.escape(regla, quote=True)}" data-mensaje="{html.escape(mensaje, quote=True)}"' if regla else ''
    return (f'<label for="{ident}">{etiqueta}</label><input id="{ident}" name="{ident}" type="{tipo}" required'
            f' value="{html.escape(valor, quote=True)}"{regla_html}{extra}><p class="error" id="error-{ident}"></p>')


def lista(ident: str, etiqueta: str, opciones, *, primera: str = 'Elige una opción', extra: str = '') -> str:
    choices = ''.join(f'<option value="{value}">{html.escape(text)}</option>' for value, text in opciones)
    return (f'<label for="{ident}">{etiqueta}</label><select id="{ident}" name="{ident}" required{extra}>'
            f'<option value="">{primera}</option>{choices}</select>')


def campos_ticket(fecha_etiqueta: str = 'Fecha de compra', total_etiqueta: str = 'Total pagado') -> str:
    return (campo('folio', 'Folio del ticket', r'[A-Za-z0-9-]{4,20}', 'Escribe el folio como aparece en el ticket') +
            campo('fecha', fecha_etiqueta, FECHA, 'Usa el formato dd/mm/aaaa', extra=' placeholder="dd/mm/aaaa"') +
            campo('total', total_etiqueta, IMPORTE, 'Escribe el total con punto decimal'))


def campos_fiscales(*, regimen=True, uso=True, pago=True) -> str:
    return (campo('rfc', 'RFC', RFC, 'El RFC no tiene un formato válido') +
            campo('nombre', 'Nombre o razón social', r'.{3,}', 'Escribe tu nombre completo') +
            campo('cp', 'Código postal fiscal', CP, 'El código postal tiene cinco dígitos') +
            campo('correo', 'Correo electrónico', CORREO, 'Escribe un correo válido', tipo='email') +
            (lista('regimen', 'Régimen fiscal', REGIMENES) if regimen else '') +
            (lista('uso', 'Uso del CFDI', USOS) if uso else '') +
            (lista('pago', 'Forma de pago', PAGOS) if pago else ''))


def a_fecha(iso: str) -> str:
    return '/'.join(reversed(iso.split('-')))


@dataclass
class Pedido:
    metodo: str
    ruta: str
    consulta: dict
    datos: dict
    archivos: dict
    cookies: dict
    url: str

    def valor(self, nombre: str, defecto: str = '') -> str:
        return (self.datos.get(nombre) or self.consulta.get(nombre) or defecto).strip()


@dataclass
class Respuesta:
    cuerpo: str | bytes = ''
    estado: int = 200
    tipo: str = 'text/html; charset=utf-8'
    cabeceras: dict = field(default_factory=dict)


def descarga(nombre: str, contenido: bytes) -> Respuesta:
    kind = {'.zip': 'application/zip', '.xml': 'application/xml', '.pdf': 'application/pdf'}[nombre[nombre.rfind('.'):]]
    return Respuesta(contenido, tipo=kind, cabeceras={'Content-Disposition': f'attachment; filename="{nombre}"'})


def _multipart(headers: dict, body: bytes) -> tuple[dict, dict]:
    kind = headers.get('content-type', '')
    message = BytesParser(policy=email_policy).parsebytes(b'Content-Type: ' + kind.encode() + b'\r\n\r\n' + body)
    datos, archivos = {}, {}
    for part in message.iter_parts():
        name = part.get_param('name', header='content-disposition')
        if not name:
            continue
        if part.get_filename() is not None:
            archivos[name] = part.get_payload(decode=True) or b''
        else:
            datos[name] = part.get_content().strip() if part.get_content_type() == 'text/plain' else ''
    return datos, archivos


async def pedido(route) -> Pedido:
    request = route.request
    parts = urlsplit(request.url)
    headers = await request.all_headers()
    consulta = {name: values[0] for name, values in parse_qs(parts.query, keep_blank_values=True).items()}
    datos, archivos = {}, {}
    if request.method == 'POST':
        kind = headers.get('content-type', '')
        if kind.startswith('multipart/form-data'):
            datos, archivos = _multipart(headers, request.post_data_buffer or b'')
        elif kind.startswith('application/json'):
            loaded = json.loads(request.post_data or '{}')
            datos = {key: str(value) for key, value in loaded.items()} if isinstance(loaded, dict) else {}
        else:
            datos = {name: values[0] for name, values in parse_qs(request.post_data or '', keep_blank_values=True).items()}
    cookies = {}
    for piece in headers.get('cookie', '').split(';'):
        name, _, value = piece.strip().partition('=')
        if name:
            cookies[name] = value
    return Pedido(request.method, parts.path or '/', consulta, datos, archivos, cookies, request.url)


# ------------------------------------------------------------------ test CFDI

def cfdi_xml(receptor: dict, total: str, forma_pago: str, folio_fiscal: str, dia: str) -> bytes:
    def attrs(values):
        return ' '.join(f'{key}="{html.escape(str(value), quote=True)}"' for key, value in values.items())
    root = {'Version': '4.0', 'Serie': 'BAN', 'Folio': folio_fiscal[:8], 'Fecha': dia + 'T12:00:00',
            'SubTotal': total, 'Total': total, 'Moneda': 'MXN', 'FormaPago': forma_pago, 'MetodoPago': 'PUE',
            'TipoDeComprobante': 'I', 'Exportacion': '01', 'LugarExpedicion': '06000'}
    persona = {'Rfc': receptor['rfc'], 'Nombre': receptor['nombre'], 'DomicilioFiscalReceptor': receptor['cp'],
               'RegimenFiscalReceptor': receptor['regimen'], 'UsoCFDI': receptor['uso']}
    concepto = {'ClaveProdServ': '01010101', 'Cantidad': '1', 'ClaveUnidad': 'ACT', 'Descripcion': 'Venta del ticket',
                'ValorUnitario': total, 'Importe': total, 'ObjetoImp': '01'}
    return ('<?xml version="1.0" encoding="UTF-8"?><cfdi:Comprobante xmlns:cfdi="http://www.sat.gob.mx/cfd/4" '
            'xmlns:tfd="http://www.sat.gob.mx/TimbreFiscalDigital" ' + attrs(root) + '>'
            '<cfdi:Emisor Rfc="' + EMISOR + '" Nombre="EMISOR DE PRUEBA DEL BANCO" RegimenFiscal="601"/>'
            '<cfdi:Receptor ' + attrs(persona) + '/><cfdi:Conceptos><cfdi:Concepto ' + attrs(concepto) + '/></cfdi:Conceptos>'
            '<cfdi:Complemento><tfd:TimbreFiscalDigital Version="1.1" UUID="' + folio_fiscal + '" '
            'SelloSAT="SELLO_DE_PRUEBA_SIN_VALOR_FISCAL"/></cfdi:Complemento></cfdi:Comprobante>').encode('utf-8')


def cfdi_pdf(folio_fiscal: str, rfc: str, total: str) -> bytes:
    lines = ['FACTURA DE PRUEBA SIN VALOR FISCAL', f'RFC receptor {rfc}', f'UUID {folio_fiscal}', f'Total {total}']
    text = 'BT /F1 11 Tf 30 150 Td 14 TL ' + ' '.join(f'({line}) Tj T*' for line in lines) + ' ET'
    objects = ['<< /Type /Catalog /Pages 2 0 R >>', '<< /Type /Pages /Kids [3 0 R] /Count 1 >>',
               '<< /Type /Page /Parent 2 0 R /MediaBox [0 0 600 200] /Resources << /Font << /F1 4 0 R >> >> /Contents 5 0 R >>',
               '<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica >>',
               f'<< /Length {len(text)} >>\nstream\n{text}\nendstream']
    result = bytearray(b'%PDF-1.4\n')
    offsets = []
    for number, body in enumerate(objects, 1):
        offsets.append(len(result))
        result.extend(f'{number} 0 obj\n{body}\nendobj\n'.encode('latin-1'))
    start = len(result)
    result.extend(('xref\n0 6\n0000000000 65535 f \n' + ''.join(f'{offset:010d} 00000 n \n' for offset in offsets) +
                   f'trailer\n<< /Size 6 /Root 1 0 R >>\nstartxref\n{start}\n%%EOF').encode('latin-1'))
    return bytes(result)


@dataclass
class Emision:
    folio: str
    folio_fiscal: str
    receptor: dict
    total: str
    forma_pago: str
    dia: str

    def xml(self) -> bytes:
        return cfdi_xml(self.receptor, self.total, self.forma_pago, self.folio_fiscal, self.dia)

    def pdf(self) -> bytes:
        return cfdi_pdf(self.folio_fiscal, self.receptor['rfc'], self.total)

    def zip(self) -> bytes:
        archive = io.BytesIO()
        with zipfile.ZipFile(archive, 'w') as pair:
            pair.writestr(f'factura-{self.folio_fiscal[:8]}.xml', self.xml())
            pair.writestr(f'factura-{self.folio_fiscal[:8]}.pdf', self.pdf())
        return archive.getvalue()


# ------------------------------------------------------------------ the portal

class PortalSimulado:
    """One invented billing portal. Subclasses draw its pages and say how to solve them."""
    slug = ''
    marca = ''
    forma = ''
    origen = ''
    otros_origenes: tuple[str, ...] = ()
    # 'verificada' (invoice checked), 'emitida_espera' (issued, files on their way) or 'persona'.
    esperado = 'verificada'
    folio_formato = 'T-{n}'
    # What the engine cannot do yet on this portal, by pass, once measured. Empty: nothing.
    limitacion = ''
    limitacion_memoria = ''

    def __init__(self):
        self.tickets: dict[str, Receipt] = {}
        self.emisiones: dict[str, Emision] = {}
        self.consultas: list[dict] = []
        self.capturas: list[dict] = []
        self.peticiones: list[tuple[str, str]] = []

    @property
    def origenes(self) -> tuple[str, ...]:
        return (self.origen, *self.otros_origenes)

    @property
    def dominios(self) -> set[str]:
        return {urlsplit(origin).hostname for origin in self.origenes}

    @property
    def portal_url(self) -> str:
        return self.origen + '/'

    def registrar(self, ticket: Receipt, perfil) -> None:
        """The purchase exists in the merchant's records (the portal will find it)."""
        self.tickets[ticket.ticket_number] = ticket

    def buscar(self, folio: str, fecha: str, total: str, *, decimal: str = '.') -> Receipt | None:
        found = self.tickets.get(folio.strip())
        if not found:
            return None
        if fecha and fecha.strip() != a_fecha(found.date):
            return None
        if total and total.strip().replace(decimal, '.') != found.amount:
            return None
        return found

    @staticmethod
    def error_fiscal(datos: dict, *, regimenes=REGIMENES, pago=True) -> str:
        checks = [('rfc', RFC, 'El RFC no tiene un formato válido.'), ('nombre', r'.{3,}', 'Falta el nombre.'),
                  ('cp', CP, 'El código postal no es válido.'), ('correo', CORREO, 'El correo no es válido.')]
        for name, rule, message in checks:
            if not re.fullmatch(rule, (datos.get(name) or '').strip()):
                return message
        if datos.get('regimen') not in {code for code, _ in regimenes}:
            return 'Elige tu régimen fiscal.'
        if datos.get('uso') not in {code for code, _ in USOS}:
            return 'Elige el uso del CFDI.'
        if pago and datos.get('pago') not in {code for code, _ in PAGOS}:
            return 'Elige la forma de pago.'
        return ''

    def emitir(self, ticket: Receipt, datos: dict) -> Emision:
        """Stamp the test CFDI with what the portal received. A second request returns the same one."""
        if ticket.ticket_number in self.emisiones:
            return self.emisiones[ticket.ticket_number]
        receptor = {name: (datos.get(name) or '').strip() for name in ('rfc', 'nombre', 'cp', 'regimen', 'uso')}
        receptor['rfc'] = receptor['rfc'].upper()
        receptor['nombre'] = ' '.join(receptor['nombre'].upper().split())
        emision = Emision(ticket.ticket_number, str(uuid.uuid5(uuid.NAMESPACE_URL, self.origen + '/' + ticket.ticket_number)).upper(),
                          receptor, ticket.amount, (datos.get('pago') or ticket.payment_code).strip(), ticket.date)
        self.emisiones[ticket.ticket_number] = emision
        self.capturas.append({'folio': ticket.ticket_number, **receptor, 'pago': emision.forma_pago})
        return emision

    def correo(self, folio: str) -> bytes:
        """The files the portal would have sent by mail for that ticket."""
        return self.emisiones[folio].zip()

    def token(self, folio: str) -> str:
        return self.emisiones[folio].folio_fiscal.lower()

    def por_token(self, token: str) -> Emision | None:
        return next((item for item in self.emisiones.values() if item.folio_fiscal.lower() == token), None)

    def pagina(self, cuerpo: str, script: str = '') -> Respuesta:
        return Respuesta(pagina(self.marca, cuerpo, script))

    async def atender(self, route) -> None:
        request = await pedido(route)
        self.peticiones.append((request.metodo, request.url))
        if request.ruta == '/favicon.ico':
            answer = Respuesta('', 404, 'text/plain')
        else:
            answer = self.responder(request) or Respuesta('<h1>Página no encontrada</h1>', 404)
        await route.fulfill(status=answer.estado, content_type=answer.tipo, body=answer.cuerpo, headers=answer.cabeceras)

    def responder(self, pedido: Pedido) -> Respuesta | None:
        raise NotImplementedError

    def guion(self, vista: 'Vista') -> Plan:
        """The plan a competent model would give for what is on screen (the scripted AI)."""
        raise NotImplementedError


# ------------------------------------------------------------------ the scripted AI's view

CAMPO = re.compile(r'^#(\d+) (\S+) «(.*?)»: (.*)$')
NOTA = re.compile(r' \((?:obligatorio|deshabilitado|error: [^()]*)(?:, (?:obligatorio|deshabilitado|error: [^()]*))*\)$')


class Vista:
    """One observation the runner sends to the model, with the profile and ticket it was given."""

    def __init__(self, vista: dict, perfil: dict, ticket: dict):
        self.datos = vista
        self.texto = vista.get('estado_visible', '')
        self.recientes = vista.get('acciones_recientes', [])
        self.error = vista.get('ultimo_error', '')
        self.emitida = bool(vista.get('emision_intentada') or vista.get('emision_visible'))
        self.campos = []
        for line in vista.get('campos', []):
            found = CAMPO.match(line)
            if found:
                value = NOTA.sub('', found[4])
                self.campos.append({'n': int(found[1]), 'tipo': found[2], 'etiqueta': found[3], 'valor': value,
                                    'linea': line})
        amount = ticket.get('amount', '')
        self.valores = {'folio': ticket.get('ticket_number', ''), 'fecha': a_fecha(ticket.get('date', '')),
                        'fecha_iso': ticket.get('date', ''), 'total': amount, 'total_coma': amount.replace('.', ','),
                        'pago': ticket.get('payment_code', ''), 'rfc': perfil.get('rfc', ''),
                        'nombre': perfil.get('name', ''), 'cp': perfil.get('postal_code', ''),
                        'correo': perfil.get('email', ''), 'regimen': perfil.get('regime', ''),
                        'uso': perfil.get('cfdi_use', '')}

    def ve(self, *textos: str) -> bool:
        folded = self.texto.casefold()
        return all(texto.casefold() in folded for texto in textos)

    def campo(self, patron: str, tipo: str | None = None, ordinal: int = 0) -> dict | None:
        found = [item for item in self.campos if re.search(patron, item['etiqueta'], re.I)
                 and (tipo is None or item['tipo'] == tipo)]
        return found[ordinal] if len(found) > ordinal else None

    def del_tipo(self, tipo: str) -> list[dict]:
        return [item for item in self.campos if item['tipo'] == tipo]

    def llenar(self, patron: str, valor: str, *, por_numero: bool = False, tipo: str | None = None,
               ordinal: int = 0) -> Action | None:
        """Fill the field when it does not show the value yet; by its label, or by number when it has none."""
        item = self.campo(patron, tipo, ordinal)
        if item is None or item['valor'] == valor:
            return None
        return self.escribir(item, valor, por_numero=por_numero)

    def escribir(self, item: dict, valor: str, *, por_numero: bool = False) -> Action:
        same = [other for other in self.campos if other['etiqueta'] == item['etiqueta']]
        if por_numero or len(same) > 1 or item['tipo'] not in {'text', 'email', 'tel', 'number', 'search'}:
            target = Target(by='field', name=str(item['n']), role=None, exact=True)
        else:
            target = Target(by='role', role='textbox', name=item['etiqueta'], exact=True)
        return Action(kind='fill', target=target, value=valor, reason='Capturar ' + item['etiqueta'], risk='normal')

    def elegir(self, item: dict | None, opcion: str) -> Action | None:
        if item is None or item['valor'] == opcion:
            return None
        return Action(kind='select', target=Target(by='field', name=str(item['n']), role=None, exact=True),
                      value=opcion, reason='Elegir la opción del expediente', risk='normal')

    @staticmethod
    def clic(nombre: str, rol: str = 'button', riesgo: str = 'normal') -> Action:
        return Action(kind='click', target=Target(by='role', role=rol, name=nombre, exact=True), value=None,
                      reason='Avanzar en el portal', risk=riesgo)

    @staticmethod
    def clic_texto(texto: str) -> Action:
        return Action(kind='click', target=Target(by='text', role=None, name=texto, exact=True), value=None,
                      reason='Abrir o elegir en la lista', risk='normal')

    @staticmethod
    def accion(kind: str, value: str | None = None, reason: str = 'Paso del portal') -> Action:
        return Action(kind=kind, target=None, value=value, reason=reason, risk='normal')

    def hizo(self, control: str, tipo: str = 'click') -> bool:
        return any(item.get('tipo') == tipo and item.get('control') == control for item in self.recientes)

    @staticmethod
    def plan(*acciones: Action | None) -> Plan:
        steps = [action for action in acciones if action is not None]
        return Plan(steps=steps or [Vista.accion('wait', reason='Esperar a que cargue la página')])

    def formulario(self, llenados: list[Action | None], boton: str, *, riesgo: str = 'normal',
                   rol: str = 'button') -> Plan:
        """Fill what is missing and press the button; an emission button travels alone at the end."""
        fills = [action for action in llenados if action is not None]
        if fills and riesgo != 'normal':
            return Plan(steps=fills)
        return Plan(steps=[*fills, self.clic(boton, rol, riesgo)])

    def datos_ticket(self, *, fecha: str = 'fecha', total: str = 'total') -> list[Action | None]:
        return [self.llenar(r'^Folio', self.valores['folio']), self.llenar(r'^Fecha', self.valores[fecha]),
                self.llenar(r'^(Total|Importe)', self.valores[total])]

    def datos_fiscales(self) -> list[Action | None]:
        return [self.llenar(r'^RFC', self.valores['rfc']), self.llenar(r'^Nombre', self.valores['nombre']),
                self.llenar(r'^C[oó]digo postal', self.valores['cp']), self.llenar(r'^Correo', self.valores['correo'])]

    def descargar(self, *enlaces: str) -> Plan:
        """Download each file once; then say it is done."""
        for name in enlaces:
            if self.ve(name) and not self.hizo(name):
                return self.plan(self.clic(name, 'link'))
        return self.plan(self.accion('done', reason='Archivos descargados'))
