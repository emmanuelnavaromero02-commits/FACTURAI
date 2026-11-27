"""A made-up invoicing portal and a stand-in for the model, shared by the engine tests.

Nothing here reaches the network: every page is served from memory through route
interception and the «model» is a function that reads the observation.
"""
import io
import json
import os
import uuid
import zipfile
from urllib.parse import parse_qs, urlsplit

from facturador.catalog import CUSTOM_PORTAL
from facturador.models import Action, Plan, Receipt, Target
from test_core import PROFILE, pdf_bytes, xml_bytes

CHROME = os.getenv('FACTURADOR_CHROMIUM_PATH',
                   'C:/Program Files/Google/Chrome/Application/chrome.exe' if os.name == 'nt'
                   else '/usr/bin/google-chrome-stable')
ORIGIN = 'https://facturas.tienda-ejemplo.mx'
PAGE = '<!doctype html><html lang="es"><head><meta charset="utf-8"><title>Facturas</title></head><body>%s</body></html>'


def ticket(folio: str, day: str, amount: str, origin: str = ORIGIN, payment: str = '28') -> Receipt:
    return Receipt(provider=CUSTOM_PORTAL, portal_url=origin + '/', merchant='Tienda Ejemplo', amount=amount,
                   date=day, ticket_number=folio, payment_code=payment)


def step(kind, role, name, value=None, risk='normal'):
    return Action(kind=kind, target=Target(by='role', role=role, name=name, exact=True), value=value,
                  reason='Paso del portal de prueba', risk=risk)


def numbered(kind, number, value=None):
    return Action(kind=kind, target=Target(by='field', role=None, name=str(number), exact=True), value=value,
                  reason='Campo sin etiqueta', risk='normal')


WAIT = Action(kind='wait', target=None, value=None, reason='Nada que hacer', risk='normal')


class Portal:
    """Lookup, fiscal data, confirmation and emission, like many small billing portals."""

    def __init__(self, origin: str = ORIGIN, *, phone=False, show_amount=True, stuck=False, download=True,
                 emit='Emitir factura', choices=False, reveal=''):
        self.origin, self.phone, self.show_amount, self.stuck = origin, phone, show_amount, stuck
        self.download, self.emit = download, emit
        # ``choices`` adds two lists the fiscal rules do not recognize; ``reveal`` names the
        # field ('folio' or 'fecha') whose typing makes an optional field appear.
        self.choices, self.reveal = choices, reveal
        self.tickets: dict[str, Receipt] = {}
        self.lookups, self.fiscal, self.emitted = [], [], []

    def add(self, receipt: Receipt) -> Receipt:
        self.tickets[receipt.ticket_number] = receipt
        return receipt

    def lookup(self, error='') -> str:
        submit = ('<button type="button" onclick="document.getElementById(\'aviso\').textContent='
                  '\'No encontramos ese ticket\'">Buscar ticket</button>' if self.stuck else '<button>Buscar ticket</button>')
        choices = ('<label>¿Cómo pagaste?<select name="como"><option value="">Elige</option>'
                   '<option value="1">Efectivo</option><option value="2">Tarjeta de débito</option>'
                   '<option value="3">Tarjeta de crédito</option></select></label>'
                   '<label>Tipo de comprobante<select name="tipo"><option value="">Elige</option>'
                   '<option value="T">Ticket de caja</option><option value="N">Nota de venta</option></select></label>'
                   if self.choices else '')
        show = ("document.getElementById('extra').innerHTML="
                "'<label>Sucursal (opcional)<input name=sucursal></label>'")
        typed = {name: f' oninput="{show}"' if self.reveal == name else '' for name in ('folio', 'fecha')}
        return PAGE % (f'<h1>Facturación en línea</h1><p id="aviso">{error}</p><form action="/datos" method="get">'
                       + choices +
                       f'<label>Folio del ticket<input name="folio" required{typed["folio"]}></label>'
                       f'<label>Fecha de compra<input name="fecha" placeholder="dd/mm/aaaa" required{typed["fecha"]}></label>'
                       '<label>Total pagado<input name="total" required></label><div id="extra"></div>'
                       + submit + '</form>')

    def data(self, folio, total) -> str:
        phone = '<label>Teléfono de contacto<input name="telefono"></label>' if self.phone else ''
        return PAGE % (f'<h1>Datos fiscales</h1><p>Ticket {folio} por ${total}</p><form action="/confirmar" method="get">'
                       f'<input type="hidden" name="folio" value="{folio}"><input type="hidden" name="total" value="{total}">'
                       '<label>RFC<input name="rfc" required></label>'
                       '<label>Nombre o razón social<input name="nombre" required></label>'
                       '<label>Código postal<input name="cp" required></label>'
                       '<label>Correo electrónico<input name="correo" type="email" required></label>' + phone +
                       '<label>Régimen fiscal<select name="regimen" required><option value="">Elige</option>'
                       '<option value="605">605 - Sueldos y salarios</option>'
                       '<option value="612">612 - Personas físicas con actividades empresariales</option></select></label>'
                       '<label>Uso del CFDI<select name="uso" required><option value="">Elige</option>'
                       '<option value="G03">Gastos en general</option><option value="S01">Sin efectos fiscales</option></select></label>'
                       '<label>Forma de pago<select name="pago" required><option value="">Elige</option>'
                       '<option value="01">Efectivo</option><option value="28">Tarjeta de débito</option>'
                       '<option value="04">Tarjeta de crédito</option></select></label>'
                       '<button>Continuar</button></form>')

    def confirm(self, query) -> str:
        hidden = ''.join(f'<input type="hidden" name="{name}" value="{values[0]}">' for name, values in query.items())
        amount = f'<p>Total: ${query["total"][0]}</p>' if self.show_amount else ''
        return PAGE % (f'<h1>Confirma tu factura</h1><p>RFC receptor: {query["rfc"][0]}</p>'
                       f'<p>Ticket {query["folio"][0]}</p>{amount}'
                       f'<form action="/emitir" method="post">{hidden}<button>{self.emit}</button></form>')

    def invoice(self, folio: str) -> tuple[str, bytes]:
        receipt = self.tickets[folio]
        fiscal = str(uuid.uuid5(uuid.NAMESPACE_URL, self.origin + '/' + folio)).upper()
        archive = io.BytesIO()
        with zipfile.ZipFile(archive, 'w') as pair:
            pair.writestr('factura.xml', xml_bytes(Total=receipt.amount, FormaPago=receipt.payment_code, uuid=fiscal))
            pair.writestr('factura.pdf', pdf_bytes(fiscal))
        return fiscal, archive.getvalue()

    async def handle(self, route):
        request = route.request
        parts = urlsplit(request.url)
        query = parse_qs(parts.query)
        html = 'text/html; charset=utf-8'
        if parts.path == '/':
            await route.fulfill(content_type=html, body=self.lookup())
        elif parts.path == '/datos':
            self.lookups.append({name: values[0] for name, values in query.items()})
            found = self.tickets.get(query.get('folio', [''])[0])
            day = found and '/'.join(reversed(found.date.split('-')))
            if not found or query.get('fecha') != [day] or query.get('total') != [found.amount]:
                await route.fulfill(content_type=html, body=self.lookup('No encontramos ese ticket'))
            else:
                await route.fulfill(content_type=html, body=self.data(found.ticket_number, found.amount))
        elif parts.path == '/confirmar':
            self.fiscal.append({name: values[0] for name, values in query.items()})
            await route.fulfill(content_type=html, body=self.confirm(query))
        elif parts.path == '/emitir' and request.method == 'POST':
            sent = {name: values[0] for name, values in parse_qs(request.post_data or '').items()}
            self.emitted.append(sent)
            fiscal, _ = self.invoice(sent['folio'])
            link = (f'<a href="/archivos?folio={sent["folio"]}">Descargar comprobantes</a>' if self.download
                    else '<p>Enviamos el PDF y el XML a tu correo.</p>')
            await route.fulfill(content_type=html, body=PAGE % (
                f'<h1>Factura emitida</h1><p>Folio fiscal {fiscal}</p>' + link))
        elif parts.path == '/archivos':
            _, archive = self.invoice(query['folio'][0])
            await route.fulfill(content_type='application/zip', body=archive,
                                headers={'Content-Disposition': 'attachment; filename="comprobantes.zip"'})
        else:
            await route.fulfill(status=404, body='')


class Brain:
    """Stands in for the model. It plans the whole screen it is shown, with the ticket it was given."""

    def __init__(self, emit='Emitir factura'):
        self.calls: list[dict] = []
        self.emit = emit

    def seen(self, text: str) -> int:
        return sum(text in call['estado_visible'] for call in self.calls)

    def __call__(self, instructions, observation, *_rest):
        facts = json.loads(instructions.rsplit('\n', 1)[1])
        view = json.loads(observation)
        self.calls.append(view)
        return self.plan(facts['perfil'], facts['ticket'], view)

    def plan(self, profile, receipt, view):
        visible = view['estado_visible']
        if 'Factura emitida' in visible:
            if 'Descargar comprobantes' not in visible:
                return Plan(steps=[Action(kind='done', target=None, value=None, reason='Llega por correo', risk='normal')])
            return Plan(steps=[step('click', 'link', 'Descargar comprobantes')])
        if 'Confirma tu factura' in visible:
            return Plan(steps=[step('click', 'button', self.emit, risk='issue')])
        if 'Datos fiscales' in visible:
            return Plan(steps=[step('fill', 'textbox', 'RFC', profile['rfc']),
                               step('fill', 'textbox', 'Nombre o razón social', profile['name']),
                               step('fill', 'textbox', 'Código postal', profile['postal_code']),
                               step('fill', 'textbox', 'Correo electrónico', profile['email']),
                               step('click', 'button', 'Continuar')])
        if 'Folio del ticket' in visible:
            day = '/'.join(reversed(receipt['date'].split('-')))
            paid = {'01': 'Efectivo', '28': 'Tarjeta de débito', '04': 'Tarjeta de crédito'}[receipt['payment_code']]
            choices = [step('select', 'combobox', '¿Cómo pagaste?', paid),
                       step('select', 'combobox', 'Tipo de comprobante', 'Ticket de caja')] if '¿Cómo pagaste?' in visible else []
            return Plan(steps=[*choices,
                               step('fill', 'textbox', 'Folio del ticket', receipt['ticket_number']),
                               step('fill', 'textbox', 'Fecha de compra', day),
                               step('fill', 'textbox', 'Total pagado', receipt['amount']),
                               step('click', 'button', 'Buscar ticket')])
        return Plan(steps=[WAIT])


__all__ = ['Brain', 'CHROME', 'ORIGIN', 'PAGE', 'PROFILE', 'Portal', 'WAIT', 'numbered', 'step', 'ticket']
