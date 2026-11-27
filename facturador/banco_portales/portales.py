"""The invented billing portals of the bank, one shape each.

Every brand and domain here is made up (``.example`` is reserved and never resolves).
Each class draws its pages, checks what is captured like a real portal would, and says
in ``guion`` what a competent model would answer for each of its screens.
"""
from __future__ import annotations

import html
import json
import re

from .base import (CORREO, CP, FECHA, IMPORTE, PAGOS, REGIMENES, REGIMENES_MORAL, RFC, USOS, Pedido, PortalSimulado,
                   Respuesta, Vista, campo, campos_fiscales, campos_ticket, descarga, lista)


def aviso(texto: str) -> str:
    return f'<p class="error" role="alert">{html.escape(texto)}</p>' if texto else ''


def ocultos(valores: dict) -> str:
    return ''.join(f'<input type="hidden" name="{html.escape(name, quote=True)}" value="{html.escape(value, quote=True)}">'
                   for name, value in valores.items())


def resumen(ticket, datos: dict) -> str:
    return (f'<p>Ticket: {html.escape(ticket.ticket_number)}</p><p>Total: ${html.escape(ticket.amount)}</p>'
            f'<p>RFC receptor: {html.escape(datos.get("rfc", "").upper())}</p>'
            f'<p>Nombre: {html.escape(datos.get("nombre", "").upper())}</p>')


class PortalComun(PortalSimulado):
    """Lookup, fiscal data and emission on separate pages: what most of the bank builds on."""
    boton_buscar = 'Buscar ticket'

    def consulta(self, error: str = '') -> Respuesta:
        return self.pagina('<h1>Factura tu compra</h1>' + aviso(error) +
                           '<form method="post" action="/datos" onsubmit="return revisa(this)">' + campos_ticket() +
                           f'<button>{self.boton_buscar}</button></form>')

    def ticket_de(self, pedido: Pedido):
        return self.buscar(pedido.valor('folio'), pedido.valor('fecha'), pedido.valor('total'))

    def descargas(self, pedido: Pedido) -> Respuesta | None:
        if pedido.ruta.startswith('/descarga/'):
            token = pedido.ruta.rsplit('/', 1)[1].split('.')[0]
            emision = self.por_token(token)
            if emision:
                return descarga(f'cfdi-{token[:8]}.zip', emision.zip())
        return None


# ------------------------------------------------------------------ 1) one page

class UnaPagina(PortalComun):
    slug = 'una_pagina'
    marca = 'Café Lumbre'
    forma = 'formulario de una sola página'
    origen = 'https://facturacion.cafelumbre.example'
    folio_formato = 'CL{n}'

    def formulario(self, error=''):
        return self.pagina('<h1>Factura tu consumo</h1>' + aviso(error) +
                           '<form method="post" action="/facturar" onsubmit="return revisa(this)"><h2>Tu ticket</h2>' +
                           campos_ticket() + '<h2>Tus datos fiscales</h2>' + campos_fiscales() +
                           '<button>Facturar</button></form>')

    def responder(self, p):
        if p.ruta == '/':
            return self.formulario()
        if p.ruta == '/facturar' and p.metodo == 'POST':
            ticket = self.ticket_de(p)
            if not ticket:
                return self.formulario('No encontramos un ticket con esos datos.')
            error = self.error_fiscal(p.datos)
            if error:
                return self.formulario(error)
            emision = self.emitir(ticket, p.datos)
            return self.pagina(f'<h1>Factura emitida</h1><p>Folio fiscal: {emision.folio_fiscal}</p>'
                               f'<a href="/descarga/{self.token(ticket.ticket_number)}.zip">Descargar factura (ZIP)</a>')
        return self.descargas(p)

    def guion(self, v: Vista):
        if v.ve('Factura emitida'):
            return v.descargar('Descargar factura (ZIP)')
        return v.formulario(v.datos_ticket() + v.datos_fiscales(), 'Facturar', riesgo='issue')


# ------------------------------------------------------------------ 2) three-step wizard

class Asistente(PortalComun):
    slug = 'asistente_3_pasos'
    marca = 'Ferretería Tornillo Feliz'
    forma = 'asistente de 3 pasos en una misma página'
    origen = 'https://facturas.tornillofeliz.example'
    folio_formato = 'TF-{n}'
    SCRIPT = r"""<script>
let ticket = null;
const datos = f => Object.fromEntries(new FormData(f));
async function enviar(ruta, cuerpo) {
  const r = await fetch(ruta, {method: 'POST', headers: {'Content-Type': 'application/json'}, body: JSON.stringify(cuerpo)});
  return r.json();
}
async function paso1(ev) {
  ev.preventDefault();
  if (!revisa(document.getElementById('f1'))) return false;
  const r = await enviar('/api/ticket', datos(document.getElementById('f1')));
  if (!r.ok) { document.getElementById('aviso1').textContent = r.error; return false; }
  ticket = r;
  document.getElementById('paso1').hidden = true;
  document.getElementById('paso2').hidden = false;
  return false;
}
function paso2(ev) {
  ev.preventDefault();
  const f = document.getElementById('f2');
  if (!revisa(f) || !f.checkValidity()) { document.getElementById('aviso2').textContent = 'Revisa los datos marcados.'; return false; }
  const d = datos(f);
  document.getElementById('resumen').innerHTML = '<p>Ticket: ' + ticket.folio + '</p><p>Total: $' + ticket.total +
    '</p><p>RFC receptor: ' + d.rfc.toUpperCase() + '</p><p>Nombre: ' + d.nombre.toUpperCase() + '</p>';
  document.getElementById('paso2').hidden = true;
  document.getElementById('paso3').hidden = false;
  return false;
}
async function generar() {
  const r = await enviar('/api/emitir', Object.assign(datos(document.getElementById('f1')), datos(document.getElementById('f2'))));
  if (!r.ok) { document.getElementById('aviso3').textContent = r.error; return; }
  document.getElementById('paso3').hidden = true;
  document.getElementById('listo').hidden = false;
  document.getElementById('uuid').textContent = 'Folio fiscal: ' + r.uuid;
  document.getElementById('baja').href = r.liga;
}
</script>"""

    def responder(self, p):
        if p.ruta == '/':
            return self.pagina(
                '<h1>Facturación en línea</h1>'
                '<section id="paso1"><h2>Paso 1 de 3: tu ticket</h2><form id="f1" onsubmit="return paso1(event)">' +
                campos_ticket() + '<p class="error" id="aviso1"></p><button>Siguiente</button></form></section>'
                '<section id="paso2" hidden><h2>Paso 2 de 3: tus datos fiscales</h2><form id="f2" onsubmit="return paso2(event)">' +
                campos_fiscales() + '<p class="error" id="aviso2"></p><button>Siguiente</button></form></section>'
                '<section id="paso3" hidden><h2>Paso 3 de 3: revisa y confirma</h2><div id="resumen"></div>'
                '<button type="button" onclick="generar()">Generar factura</button><p class="error" id="aviso3"></p></section>'
                '<section id="listo" hidden><h2>¡Listo! Tu factura fue generada</h2><p id="uuid"></p>'
                '<a id="baja" href="#">Descargar PDF y XML</a></section>', self.SCRIPT)
        if p.ruta == '/api/ticket' and p.metodo == 'POST':
            ticket = self.ticket_de(p)
            body = ({'ok': True, 'folio': ticket.ticket_number, 'total': ticket.amount} if ticket
                    else {'ok': False, 'error': 'No encontramos ese ticket. Revisa folio, fecha y total.'})
            return Respuesta(json.dumps(body), tipo='application/json')
        if p.ruta == '/api/emitir' and p.metodo == 'POST':
            ticket = self.ticket_de(p)
            error = 'No encontramos ese ticket.' if not ticket else self.error_fiscal(p.datos)
            if error:
                return Respuesta(json.dumps({'ok': False, 'error': error}), tipo='application/json')
            emision = self.emitir(ticket, p.datos)
            liga = f'/descarga/{self.token(ticket.ticket_number)}.zip'
            return Respuesta(json.dumps({'ok': True, 'uuid': emision.folio_fiscal, 'liga': liga}), tipo='application/json')
        return self.descargas(p)

    def guion(self, v):
        if v.ve('fue generada'):
            return v.descargar('Descargar PDF y XML')
        if v.ve('Paso 3 de 3'):
            return v.plan(v.clic('Generar factura', riesgo='issue'))
        if v.campo('^RFC'):
            return v.formulario(v.datos_fiscales(), 'Siguiente')
        return v.formulario(v.datos_ticket(), 'Siguiente')


# ------------------------------------------------------------------ 3) form inside a same-origin iframe

class Iframe(PortalComun):
    slug = 'iframe'
    marca = 'Panadería La Espiga'
    forma = 'formulario dentro de un iframe del mismo origen'
    origen = 'https://facturas.laespiga.example'
    folio_formato = 'E{n}'

    def marco(self, cuerpo):
        return self.pagina(cuerpo)

    def responder(self, p):
        if p.ruta == '/':
            return self.pagina('<h1>Panadería La Espiga</h1><p>Factura tu compra en el formulario de abajo.</p>'
                               '<iframe src="/marco" title="Formulario de facturación" '
                               'style="width:100%;height:1300px;border:1px solid #ccc"></iframe>')
        if p.ruta == '/marco':
            return self.marco(self.lookup(''))
        if p.ruta == '/marco/datos' and p.metodo == 'POST':
            ticket = self.ticket_de(p)
            if not ticket:
                return self.marco(self.lookup('No encontramos ese ticket.'))
            return self.marco(self.fiscal(ticket))
        if p.ruta == '/marco/emitir' and p.metodo == 'POST':
            ticket = self.ticket_de(p)
            error = 'No encontramos ese ticket.' if not ticket else self.error_fiscal(p.datos)
            if error:
                return self.marco(self.fiscal(ticket, error) if ticket else self.lookup(error))
            emision = self.emitir(ticket, p.datos)
            return self.marco(f'<h2>Factura emitida</h2><p>Folio fiscal: {emision.folio_fiscal}</p>'
                              f'<a href="/descarga/{self.token(ticket.ticket_number)}.zip">Descargar comprobantes</a>')
        return self.descargas(p)

    def lookup(self, error):
        return ('<h2>Datos del ticket</h2>' + aviso(error) +
                '<form method="post" action="/marco/datos" onsubmit="return revisa(this)">' + campos_ticket() +
                '<button>Buscar ticket</button></form>')

    def fiscal(self, ticket, error=''):
        return (f'<h2>Datos fiscales</h2><p>Ticket {ticket.ticket_number} por ${ticket.amount}</p>' + aviso(error) +
                '<form method="post" action="/marco/emitir" onsubmit="return revisa(this)">' +
                ocultos({'folio': ticket.ticket_number, 'fecha': '/'.join(reversed(ticket.date.split('-'))),
                         'total': ticket.amount}) + campos_fiscales() + '<button>Emitir factura</button></form>')

    def guion(self, v):
        if v.emitida:
            if not v.hizo('Descargar comprobantes'):
                return v.plan(v.clic('Descargar comprobantes', 'link'))
            return v.plan(v.accion('done', reason='Archivos descargados'))
        if v.campo('^RFC'):
            return v.formulario(v.datos_fiscales(), 'Emitir factura', riesgo='issue')
        if v.campo('^Folio'):
            return v.formulario(v.datos_ticket(), 'Buscar ticket')
        return v.plan(v.accion('wait', reason='El formulario aún carga'))


# ------------------------------------------------------------------ 4) lists made of divs

def lista_div(ident, etiqueta, indicacion, opciones):
    items = ''.join(f'<div class="opcion" data-valor="{value}" onclick="elegir(\'{ident}\', this)">{html.escape(text)}</div>'
                    for value, text in opciones)
    return (f'<div class="lista"><div class="titulo">{etiqueta}</div>'
            f'<div class="actual" id="actual-{ident}" onclick="abrir(\'{ident}\')">{indicacion}</div>'
            f'<div class="opciones" id="opciones-{ident}" hidden>{items}</div>'
            f'<input type="hidden" name="{ident}" id="{ident}"><p class="error" id="error-{ident}"></p></div>')


class ListasDiv(PortalComun):
    slug = 'listas_div'
    marca = 'Óptica Mirada Clara'
    forma = 'listas hechas con div (no select) para régimen y uso'
    origen = 'https://facturacion.miradaclara.example'
    folio_formato = 'MC{n}'
    LISTAS = (('regimen', 'Selecciona tu régimen', REGIMENES), ('uso', 'Selecciona el uso del CFDI', USOS))
    SCRIPT = r"""<script>
function abrir(id) { const o = document.getElementById('opciones-' + id); o.hidden = !o.hidden; }
function elegir(id, el) {
  document.getElementById(id).value = el.dataset.valor;
  document.getElementById('actual-' + id).textContent = el.textContent;
  document.getElementById('opciones-' + id).hidden = true;
  document.getElementById('error-' + id).textContent = '';
}
function listas() {
  let bien = true;
  for (const [id, texto] of [['regimen', 'Elige tu régimen fiscal'], ['uso', 'Elige el uso del CFDI']]) {
    if (!document.getElementById(id).value) { document.getElementById('error-' + id).textContent = texto; bien = false; }
  }
  return bien;
}
</script>"""

    def fiscal(self, ticket, error=''):
        return self.pagina(
            f'<h1>Datos de facturación</h1><p>Ticket {ticket.ticket_number} por ${ticket.amount}</p>' + aviso(error) +
            '<form method="post" action="/confirmar" onsubmit="return revisa(this) &amp;&amp; listas()">' +
            ocultos({'folio': ticket.ticket_number}) + campos_fiscales(regimen=False, uso=False, pago=False) +
            lista_div('regimen', 'Régimen fiscal', 'Selecciona tu régimen', REGIMENES) +
            lista_div('uso', 'Uso del CFDI', 'Selecciona el uso del CFDI', USOS) + lista('pago', 'Forma de pago', PAGOS) +
            '<button>Continuar</button></form>', self.SCRIPT)

    def responder(self, p):
        if p.ruta == '/':
            return self.consulta()
        if p.ruta == '/datos' and p.metodo == 'POST':
            ticket = self.ticket_de(p)
            return self.fiscal(ticket) if ticket else self.consulta('No encontramos ese ticket.')
        ticket = self.tickets.get(p.valor('folio'))
        if p.ruta == '/confirmar' and p.metodo == 'POST' and ticket:
            error = self.error_fiscal(p.datos)
            if error:
                return self.fiscal(ticket, error)
            return self.pagina('<h1>Confirma tu factura</h1>' + resumen(ticket, p.datos) +
                               '<form method="post" action="/emitir">' + ocultos(p.datos) +
                               '<button>Confirmar y facturar</button></form>')
        if p.ruta == '/emitir' and p.metodo == 'POST' and ticket and not self.error_fiscal(p.datos):
            emision = self.emitir(ticket, p.datos)
            return self.pagina(f'<h1>Factura emitida</h1><p>Folio fiscal: {emision.folio_fiscal}</p>'
                               f'<a href="/descarga/{self.token(ticket.ticket_number)}.zip">Descargar factura</a>')
        return self.descargas(p)

    def guion(self, v):
        if v.ve('Factura emitida'):
            return v.descargar('Descargar factura')
        if v.ve('Confirma tu factura'):
            return v.plan(v.clic('Confirmar y facturar', riesgo='issue'))
        if v.campo('^RFC'):
            fills = [action for action in v.datos_fiscales() if action]
            if fills:
                return v.plan(*fills)
            for ident, indicacion, opciones in self.LISTAS:
                texto = dict(opciones)[v.valores['regimen' if ident == 'regimen' else 'uso']]
                if v.ve(indicacion):
                    return v.plan(v.clic_texto(texto if v.ve(texto) else indicacion))
            return v.plan(v.clic('Continuar'))
        return v.formulario(v.datos_ticket(), 'Buscar ticket')


# ------------------------------------------------------------------ 5) masked fields that refuse pasting

class Mascaras(PortalComun):
    slug = 'mascaras'
    marca = 'Lavandería Burbuja'
    forma = 'campos con máscara (fecha dd/mm/aaaa, importe con coma) que rechazan el pegado'
    origen = 'https://facturas.lavanderiaburbuja.example'
    folio_formato = 'LB/{n}'
    SCRIPT = r"""<script>
function mascara(id, formato) {
  const e = document.getElementById(id);
  const rechaza = ev => { ev.preventDefault(); document.getElementById('error-' + id).textContent = 'No se permite pegar: escribe el dato.'; };
  e.addEventListener('paste', rechaza);
  e.addEventListener('drop', rechaza);
  e.addEventListener('beforeinput', ev => {
    if (ev.inputType === 'insertFromPaste' || ev.inputType === 'insertReplacementText' || (ev.data && ev.data.length > 1)) rechaza(ev);
  });
  e.addEventListener('input', () => { e.value = formato(e.value); });
}
mascara('fecha', v => { const d = v.replace(/\D/g, '').slice(0, 8); return [d.slice(0, 2), d.slice(2, 4), d.slice(4)].filter(Boolean).join('/'); });
mascara('total', v => { let t = v.replace(/[^0-9,]/g, ''); const i = t.indexOf(','); return i < 0 ? t : t.slice(0, i + 1) + t.slice(i + 1).replace(/,/g, '').slice(0, 2); });
</script>"""

    def consulta(self, error=''):
        return self.pagina('<h1>Factura tu servicio</h1>' + aviso(error) +
                           '<form method="post" action="/datos" onsubmit="return revisa(this)">' +
                           campo('folio', 'Folio del ticket', r'[A-Za-z0-9/-]{4,20}', 'Escribe el folio del ticket') +
                           campo('fecha', 'Fecha del servicio (dd/mm/aaaa)', FECHA, 'Escribe la fecha como dd/mm/aaaa',
                                 extra=' inputmode="numeric" autocomplete="off"') +
                           campo('total', 'Importe (usa coma para los centavos)', r'[0-9]+(?:,[0-9]{2})?',
                                 'Escribe el importe con coma, por ejemplo 125,50', extra=' inputmode="decimal" autocomplete="off"') +
                           '<button>Buscar ticket</button></form>', self.SCRIPT)

    def fiscal(self, ticket, error=''):
        return self.pagina(f'<h1>Datos para tu factura</h1><p>Ticket {ticket.ticket_number} por ${ticket.amount}</p>' + aviso(error) +
                           '<form method="post" action="/emitir" onsubmit="return revisa(this)">' +
                           ocultos({'folio': ticket.ticket_number}) + campos_fiscales() + '<button>Facturar</button></form>')

    def responder(self, p):
        if p.ruta == '/':
            return self.consulta()
        if p.ruta == '/datos' and p.metodo == 'POST':
            ticket = self.buscar(p.valor('folio'), p.valor('fecha'), p.valor('total'), decimal=',')
            if ',' not in p.valor('total') and '.' in p.valor('total'):
                ticket = None
            return self.fiscal(ticket) if ticket else self.consulta('No encontramos ese ticket. Revisa la fecha y el importe.')
        ticket = self.tickets.get(p.valor('folio'))
        if p.ruta == '/emitir' and p.metodo == 'POST' and ticket:
            error = self.error_fiscal(p.datos)
            if error:
                return self.fiscal(ticket, error)
            emision = self.emitir(ticket, p.datos)
            return self.pagina(f'<h1>Factura generada</h1><p>Folio fiscal: {emision.folio_fiscal}</p>'
                               f'<a href="/descarga/{self.token(ticket.ticket_number)}.zip">Descargar ZIP</a>')
        return self.descargas(p)

    def guion(self, v):
        if v.ve('Factura generada'):
            return v.descargar('Descargar ZIP')
        if v.campo('^RFC'):
            return v.formulario(v.datos_fiscales(), 'Facturar', riesgo='issue')
        return v.formulario([v.llenar(r'^Folio', v.valores['folio']), v.llenar(r'^Fecha', v.valores['fecha']),
                             v.llenar(r'^Importe', v.valores['total_coma'])], 'Buscar ticket')


# ------------------------------------------------------------------ 6) fields without labels, only examples

class SinEtiquetas(PortalComun):
    slug = 'sin_etiquetas'
    marca = 'Taquería El Fogón'
    forma = 'campos sin etiqueta, solo con texto de ejemplo'
    origen = 'https://factura.elfogon.example'
    folio_formato = 'A-{n}'

    @staticmethod
    def sin_etiqueta(nombre, ejemplo, regla, tipo='text'):
        return (f'<div><input name="{nombre}" id="{nombre}" type="{tipo}" required placeholder="{html.escape(ejemplo, quote=True)}"'
                f' data-regla="{html.escape(regla, quote=True)}" data-mensaje="Revisa este dato"><p class="error" id="error-{nombre}"></p></div>')

    @staticmethod
    def lista_sin_etiqueta(nombre, primera, opciones):
        choices = ''.join(f'<option value="{value}">{html.escape(text)}</option>' for value, text in opciones)
        return f'<div><select name="{nombre}" required><option value="">{primera}</option>{choices}</select></div>'

    def consulta(self, error=''):
        return self.pagina('<h1>Factura tu ticket</h1>' + aviso(error) +
                           '<form method="post" action="/datos" onsubmit="return revisa(this)">' +
                           self.sin_etiqueta('c1', 'A-000123', r'A-[0-9]{4,8}') + self.sin_etiqueta('c2', 'dd/mm/aaaa', FECHA) +
                           self.sin_etiqueta('c3', '0.00', IMPORTE) + '<button>Consultar</button></form>')

    def fiscal(self, ticket, error=''):
        return self.pagina(f'<h1>Datos para tu factura</h1><p>Ticket {ticket.ticket_number} · Total ${ticket.amount}</p>' +
                           aviso(error) + '<form method="post" action="/emitir" onsubmit="return revisa(this)">' +
                           ocultos({'c1': ticket.ticket_number}) +
                           self.sin_etiqueta('c4', 'AAAA000000XXX', RFC) + self.sin_etiqueta('c5', 'JUAN PÉREZ LÓPEZ', r'.{3,}') +
                           self.sin_etiqueta('c6', '00000', CP) + self.sin_etiqueta('c7', 'nombre@correo.com', CORREO, 'email') +
                           self.lista_sin_etiqueta('s1', 'Régimen fiscal', REGIMENES) +
                           self.lista_sin_etiqueta('s2', 'Uso de CFDI', USOS) + self.lista_sin_etiqueta('s3', 'Forma de pago', PAGOS) +
                           '<button>Generar factura</button></form>')

    def responder(self, p):
        if p.ruta == '/':
            return self.consulta()
        if p.ruta == '/datos' and p.metodo == 'POST':
            ticket = self.buscar(p.valor('c1'), p.valor('c2'), p.valor('c3'))
            return self.fiscal(ticket) if ticket else self.consulta('No encontramos ese ticket.')
        ticket = self.tickets.get(p.valor('c1'))
        if p.ruta == '/emitir' and p.metodo == 'POST' and ticket:
            datos = {'rfc': p.valor('c4'), 'nombre': p.valor('c5'), 'cp': p.valor('c6'), 'correo': p.valor('c7'),
                     'regimen': p.valor('s1'), 'uso': p.valor('s2'), 'pago': p.valor('s3')}
            error = self.error_fiscal(datos)
            if error:
                return self.fiscal(ticket, error)
            emision = self.emitir(ticket, datos)
            token = self.token(ticket.ticket_number)
            return self.pagina(f'<h1>Factura generada</h1><p>Folio fiscal: {emision.folio_fiscal}</p>'
                               f'<p><a href="/xml/{token}">Descargar XML</a></p><p><a href="/pdf/{token}">Descargar PDF</a></p>')
        for kind in ('xml', 'pdf'):
            if p.ruta.startswith(f'/{kind}/'):
                emision = self.por_token(p.ruta.rsplit('/', 1)[1])
                if emision:
                    return descarga(f'factura.{kind}', emision.xml() if kind == 'xml' else emision.pdf())
        return None

    def guion(self, v):
        if v.ve('Factura generada'):
            return v.descargar('Descargar XML', 'Descargar PDF')
        if v.campo(r'^AAAA0'):
            listas = v.del_tipo('lista')
            choices = [v.elegir(item, dict(options)[v.valores[key]])
                       for item, (key, options) in zip(listas, (('regimen', REGIMENES), ('uso', USOS), ('pago', PAGOS)))]
            fills = [v.llenar(r'^AAAA0', v.valores['rfc'], por_numero=True),
                     v.llenar(r'^JUAN', v.valores['nombre'], por_numero=True),
                     v.llenar(r'^00000$', v.valores['cp'], por_numero=True),
                     v.llenar(r'^nombre@', v.valores['correo'], por_numero=True), *choices]
            return v.formulario(fills, 'Generar factura', riesgo='issue')
        return v.formulario([v.llenar(r'^A-0', v.valores['folio'], por_numero=True),
                             v.llenar(r'^dd/mm', v.valores['fecha'], por_numero=True),
                             v.llenar(r'^0\.00$', v.valores['total'], por_numero=True)], 'Consultar')


# ------------------------------------------------------------------ 7) redirect to an invoicing provider

class Redireccion(PortalComun):
    slug = 'redireccion_proveedor'
    marca = 'Librería Hoja Suelta'
    forma = 'redirección a un proveedor de facturación en otro dominio'
    origen = 'https://www.hojasuelta.example'
    otros_origenes = ('https://portal.timbrado-nube.example',)
    folio_formato = 'HS{n}'
    BASE = '/emisor/hojasuelta'
    PROVEEDOR = 'Timbrado Nube'

    def portada(self):
        return self.pagina(f'<h1>{self.marca}</h1><p>Nuestras facturas las emite {self.PROVEEDOR}, '
                           'nuestro proveedor de facturación.</p>'
                           f'<a href="{self.otros_origenes[0]}{self.BASE}">Ir a facturar</a>')

    def consulta(self, error=''):
        return self.pagina(f'<h1>{self.PROVEEDOR}</h1><p>Emisor: {self.marca}</p>' + aviso(error) +
                           f'<form method="post" action="{self.BASE}/datos" onsubmit="return revisa(this)">' +
                           campos_ticket() + '<button>Buscar ticket</button></form>')

    def fiscal(self, ticket, error=''):
        return self.pagina(f'<h1>{self.PROVEEDOR}</h1><h2>Datos del receptor</h2><p>Ticket {ticket.ticket_number} '
                           f'por ${ticket.amount}</p>' + aviso(error) +
                           f'<form method="post" action="{self.BASE}/timbrar" onsubmit="return revisa(this)">' +
                           ocultos({'folio': ticket.ticket_number}) + campos_fiscales() +
                           '<button>Timbrar factura</button></form>')

    def responder(self, p):
        if p.url.startswith(self.origen):
            return self.portada() if p.ruta == '/' else None
        if p.ruta == self.BASE:
            return self.consulta()
        if p.ruta == self.BASE + '/datos' and p.metodo == 'POST':
            ticket = self.ticket_de(p)
            return self.fiscal(ticket) if ticket else self.consulta('No encontramos ese ticket.')
        ticket = self.tickets.get(p.valor('folio'))
        if p.ruta == self.BASE + '/timbrar' and p.metodo == 'POST' and ticket:
            error = self.error_fiscal(p.datos)
            if error:
                return self.fiscal(ticket, error)
            emision = self.emitir(ticket, p.datos)
            return self.pagina(f'<h1>CFDI generado correctamente</h1><p>UUID: {emision.folio_fiscal}</p>'
                               f'<a href="/descarga/{self.token(ticket.ticket_number)}.zip">Descargar ZIP</a>')
        return self.descargas(p)

    def guion(self, v):
        if v.ve('Ir a facturar'):
            return v.plan(v.clic('Ir a facturar', 'link'))
        if v.ve('CFDI generado'):
            return v.descargar('Descargar ZIP')
        if v.campo('^RFC'):
            return v.formulario(v.datos_fiscales(), 'Timbrar factura', riesgo='issue')
        if v.ve('Te llevamos'):
            return v.plan(v.accion('wait', reason='La página lleva sola al proveedor'))
        return v.formulario(v.datos_ticket(), 'Buscar ticket')


class RedireccionAutomatica(Redireccion):
    """Same provider shape, but the merchant page forwards by itself: the redirect alone is measured."""
    slug = 'redireccion_automatica'
    marca = 'Cafetería Órbita'
    forma = 'la página del comercio manda sola al proveedor en otro dominio'
    origen = 'https://www.cafeteriaorbita.example'
    otros_origenes = ('https://cfdi.emisores-sur.example',)
    folio_formato = 'CO{n}'
    BASE = '/e/cafeteriaorbita'
    PROVEEDOR = 'Emisores del Sur'

    def portada(self):
        destino = self.otros_origenes[0] + self.BASE
        return self.pagina(f'<h1>{self.marca}</h1><p>Te llevamos al portal de nuestro proveedor de facturación…</p>',
                           f'<script>setTimeout(() => location.replace({json.dumps(destino)}), 300);</script>')


# ------------------------------------------------------------------ 8) already invoiced: recover the files

class YaFacturado(PortalComun):
    slug = 'ya_facturado'
    marca = 'Gasolinera Ruta Norte'
    forma = '«este ticket ya fue facturado» con consulta para recuperar los archivos'
    origen = 'https://facturacion.rutanorte.example'
    folio_formato = 'RN{n}'

    def registrar(self, ticket, perfil):
        super().registrar(ticket, perfil)
        # The purchase was invoiced before, to the same person: only its files are missing.
        self.emitir(ticket, {'rfc': perfil.rfc, 'nombre': perfil.name, 'cp': perfil.postal_code, 'correo': perfil.email,
                             'regimen': perfil.regime, 'uso': perfil.cfdi_use, 'pago': ticket.payment_code})
        self.capturas.clear()

    def recuperacion(self, error=''):
        return self.pagina('<h1>Consulta tu factura</h1>' + aviso(error) +
                           '<form method="post" action="/consulta" onsubmit="return revisa(this)">' +
                           campo('folio', 'Folio del ticket', r'[A-Za-z0-9-]{4,20}') +
                           campo('rfc', 'RFC del receptor', RFC, 'El RFC no tiene un formato válido') +
                           '<button>Recuperar factura</button></form>')

    def responder(self, p):
        if p.ruta == '/':
            return self.consulta()
        if p.ruta == '/datos' and p.metodo == 'POST':
            ticket = self.ticket_de(p)
            if not ticket:
                return self.consulta('No encontramos ese ticket.')
            return self.pagina('<h1>Resultado de tu ticket</h1><p>Este ticket ya fue facturado.</p>'
                               '<p>Si necesitas tus archivos, consulta la factura existente.</p>'
                               '<a href="/consulta">Consultar mi factura</a>')
        if p.ruta == '/consulta' and p.metodo == 'GET':
            return self.recuperacion()
        if p.ruta == '/consulta' and p.metodo == 'POST':
            self.consultas.append(dict(p.datos))
            emision = self.emisiones.get(p.valor('folio'))
            if not emision or emision.receptor['rfc'] != p.valor('rfc').upper():
                return self.recuperacion('No hay una factura con ese folio y RFC.')
            return self.pagina(f'<h1>Factura encontrada</h1><p>Folio fiscal: {emision.folio_fiscal}</p>'
                               f'<a href="/descarga/{self.token(emision.folio)}.zip">Descargar XML y PDF</a>')
        return self.descargas(p)

    def guion(self, v):
        if v.ve('Factura encontrada'):
            return v.descargar('Descargar XML y PDF')
        if v.ve('ya fue facturado'):
            return v.plan(v.clic('Consultar mi factura', 'link'))
        if v.campo('RFC del receptor'):
            return v.formulario([v.llenar('^Folio', v.valores['folio']), v.llenar('^RFC', v.valores['rfc'])],
                                'Recuperar factura')
        return v.formulario(v.datos_ticket(), 'Buscar ticket')


# ------------------------------------------------------------------ 9) an account is required first

class Cuenta(PortalComun):
    slug = 'crear_cuenta'
    marca = 'Gimnasio Fuerza Viva'
    forma = 'exige crear cuenta (formulario sencillo) antes de facturar'
    origen = 'https://facturas.fuerzaviva.example'
    folio_formato = 'FV{n}'

    def __init__(self):
        super().__init__()
        self.cuentas: dict[str, str] = {}
        self.sesiones: dict[str, str] = {}

    def entrada(self, error=''):
        return self.pagina('<h1>Facturación de Gimnasio Fuerza Viva</h1>'
                           '<p>Para facturar necesitas una cuenta. Inicia sesión o crea una cuenta.</p>' + aviso(error) +
                           '<form method="post" action="/entrar"><label for="correo">Correo</label>'
                           '<input id="correo" name="correo" type="email" required><label for="clave">Contraseña</label>'
                           '<input id="clave" name="clave" type="password" required><button>Iniciar sesión</button></form>'
                           '<a href="/registro">Crear cuenta</a>')

    def con_sesion(self, respuesta: Respuesta, correo: str) -> Respuesta:
        token = f'{len(self.sesiones) + 1:04d}{abs(hash(correo)) % 10 ** 8:08d}'
        self.sesiones[token] = correo
        respuesta.cabeceras['Set-Cookie'] = f'sesion={token}; Path=/; Secure; HttpOnly; SameSite=Lax'
        return respuesta

    def responder(self, p):
        logged = p.cookies.get('sesion') in self.sesiones
        if p.ruta == '/registro' and p.metodo == 'GET':
            return self.pagina('<h1>Crea tu cuenta</h1><form method="post" action="/alta">'
                               '<label for="correo">Correo</label><input id="correo" name="correo" type="email" required>'
                               '<label for="clave">Contraseña</label><input id="clave" name="clave" type="password" '
                               'minlength="8" required><button>Crear cuenta</button></form>')
        if p.ruta == '/alta' and p.metodo == 'POST':
            correo, clave = p.valor('correo').casefold(), p.datos.get('clave', '')
            if not correo or len(clave) < 8 or correo in self.cuentas:
                return self.entrada('No se pudo crear la cuenta.')
            self.cuentas[correo] = clave
            return self.con_sesion(self.consulta(), correo)
        if p.ruta == '/entrar' and p.metodo == 'POST':
            correo = p.valor('correo').casefold()
            if self.cuentas.get(correo) != p.datos.get('clave'):
                return self.entrada('Correo o contraseña incorrectos.')
            return self.con_sesion(self.consulta(), correo)
        if not logged:
            return self.entrada() if p.ruta == '/' else self.entrada('Tu sesión terminó. Vuelve a entrar.')
        if p.ruta == '/':
            return self.consulta()
        if p.ruta == '/datos' and p.metodo == 'POST':
            ticket = self.ticket_de(p)
            if not ticket:
                return self.consulta('No encontramos ese ticket.')
            return self.pagina(f'<h1>Datos fiscales</h1><p>Ticket {ticket.ticket_number} por ${ticket.amount}</p>'
                               '<form method="post" action="/emitir" onsubmit="return revisa(this)">' +
                               ocultos({'folio': ticket.ticket_number}) + campos_fiscales() +
                               '<button>Emitir factura</button></form>')
        ticket = self.tickets.get(p.valor('folio'))
        if p.ruta == '/emitir' and p.metodo == 'POST' and ticket and not self.error_fiscal(p.datos):
            emision = self.emitir(ticket, p.datos)
            return self.pagina(f'<h1>Factura emitida</h1><p>Folio fiscal: {emision.folio_fiscal}</p>'
                               f'<a href="/descarga/{self.token(ticket.ticket_number)}.zip">Descargar factura</a>')
        return self.descargas(p)

    def guion(self, v):
        if v.ve('Factura emitida'):
            return v.descargar('Descargar factura')
        if v.campo('^RFC'):
            return v.formulario(v.datos_fiscales(), 'Emitir factura', riesgo='issue')
        if v.campo('^Folio'):
            return v.formulario(v.datos_ticket(), 'Buscar ticket')
        # The sign-up and its password belong to the program, never to the model.
        return v.plan(v.accion('wait', reason='El programa gestiona la cuenta del portal'))


# ------------------------------------------------------------------ 10) notices and terms to close

class Avisos(PortalComun):
    slug = 'avisos_terminos'
    marca = 'Pastelería Nube Dulce'
    forma = 'ventanas de aviso y de términos que hay que cerrar'
    origen = 'https://facturacion.nubedulce.example'
    folio_formato = 'ND{n}'
    MODAL = r"""<script>
function cerrar(id) { document.getElementById(id).remove(); }
function acepta(caja) { document.getElementById('aceptar-terminos').disabled = !caja.checked; }
function terminos() { document.getElementById('terminos').value = '1'; cerrar('ventana-terminos'); }
</script>"""

    def consulta(self, error=''):
        return self.pagina('<div class="modal" id="ventana-aviso" role="dialog" aria-label="Aviso"><div><h2>Aviso importante</h2>'
                           '<p>Desde este mes las facturas se emiten en CFDI 4.0. Ten a la mano tu constancia fiscal.</p>'
                           '<button type="button" onclick="cerrar(\'ventana-aviso\')">Aceptar</button></div></div>'
                           '<h1>Factura tu pedido</h1>' + aviso(error) +
                           '<form method="post" action="/datos" onsubmit="return revisa(this)">' + campos_ticket() +
                           '<button>Buscar ticket</button></form>', self.MODAL)

    def fiscal(self, ticket, error=''):
        return self.pagina('<div class="modal" id="ventana-terminos" role="dialog" aria-label="Términos"><div>'
                           '<h2>Términos y condiciones</h2><p>Al usar este servicio aceptas que tus datos fiscales se '
                           'usen solo para emitir tu factura.</p><input type="checkbox" id="acepto" onchange="acepta(this)">'
                           '<label for="acepto">Acepto los términos y condiciones</label>'
                           '<button type="button" id="aceptar-terminos" disabled onclick="terminos()">Aceptar</button></div></div>'
                           f'<h1>Datos fiscales</h1><p>Ticket {ticket.ticket_number} por ${ticket.amount}</p>' + aviso(error) +
                           '<form method="post" action="/confirmar" onsubmit="return revisa(this)">' +
                           ocultos({'folio': ticket.ticket_number}) + '<input type="hidden" name="terminos" id="terminos" value="">' +
                           campos_fiscales() + '<button>Continuar</button></form>', self.MODAL)

    def responder(self, p):
        if p.ruta == '/':
            return self.consulta()
        if p.ruta == '/datos' and p.metodo == 'POST':
            ticket = self.ticket_de(p)
            return self.fiscal(ticket) if ticket else self.consulta('No encontramos ese ticket.')
        ticket = self.tickets.get(p.valor('folio'))
        if p.ruta == '/confirmar' and p.metodo == 'POST' and ticket:
            error = self.error_fiscal(p.datos) or ('' if p.valor('terminos') == '1' else 'Acepta los términos para continuar.')
            if error:
                return self.fiscal(ticket, error)
            return self.pagina('<h1>Confirma tu factura</h1>' + resumen(ticket, p.datos) +
                               '<form method="post" action="/emitir">' + ocultos(p.datos) + '<button>Emitir factura</button></form>')
        if p.ruta == '/emitir' and p.metodo == 'POST' and ticket and not self.error_fiscal(p.datos):
            emision = self.emitir(ticket, p.datos)
            return self.pagina(f'<h1>Factura emitida</h1><p>Folio fiscal: {emision.folio_fiscal}</p>'
                               f'<a href="/descarga/{self.token(ticket.ticket_number)}.zip">Descargar factura</a>')
        return self.descargas(p)

    def guion(self, v):
        if v.ve('Aviso importante'):
            return v.plan(v.clic('Aceptar'))
        if v.ve('Términos y condiciones'):
            caja = v.campo('Acepto los términos', 'checkbox')
            if caja and caja['valor'] != 'marcado':
                return v.plan(v.clic('Acepto los términos y condiciones', 'checkbox'))
            return v.plan(v.clic('Aceptar'))
        if v.ve('Factura emitida'):
            return v.descargar('Descargar factura')
        if v.ve('Confirma tu factura'):
            return v.plan(v.clic('Emitir factura', riesgo='issue'))
        if v.campo('^RFC'):
            return v.formulario(v.datos_fiscales(), 'Continuar')
        return v.formulario(v.datos_ticket(), 'Buscar ticket')


# ------------------------------------------------------------------ 11) the photo of the ticket is required

class FotoTicket(PortalComun):
    slug = 'foto_ticket'
    marca = 'Papelería Trazo'
    forma = 'pide subir la foto del ticket'
    origen = 'https://facturas.papeleriatrazo.example'
    folio_formato = 'PT{n}'
    # The page reads the first bytes of the chosen file: a simulated portal never receives the
    # bytes of an attachment, only its name, so the «server» checks what the page read.
    SCRIPT = r"""<script>
document.getElementById('foto').addEventListener('change', async ev => {
  const archivo = ev.target.files[0];
  const inicio = archivo ? new Uint8Array(await archivo.slice(0, 4).arrayBuffer()) : [];
  document.getElementById('foto_inicio').value = [...inicio].map(b => b.toString(16).padStart(2, '0')).join('');
});
</script>"""

    def __init__(self):
        super().__init__()
        self.fotos: list[str] = []  # Name of each photo the portal accepted.

    def consulta(self, error=''):
        return self.pagina('<h1>Factura tu compra</h1><p>Sube la foto de tu ticket para validarlo.</p>' + aviso(error) +
                           '<form method="post" action="/datos" enctype="multipart/form-data" onsubmit="return revisa(this)">' +
                           campo('folio', 'Folio del ticket', r'[A-Za-z0-9-]{4,20}') +
                           campo('total', 'Total pagado', IMPORTE, 'Escribe el total con punto decimal') +
                           '<label for="foto">Foto del ticket</label><input id="foto" name="foto" type="file" accept="image/*" required>'
                           '<input type="hidden" name="foto_inicio" id="foto_inicio"><button>Continuar</button></form>',
                           self.SCRIPT)

    def responder(self, p):
        if p.ruta == '/':
            return self.consulta()
        if p.ruta == '/datos' and p.metodo == 'POST':
            # PNG or JPEG by its first bytes, under a file name: a real photo was chosen.
            if not p.nombres.get('foto') or not p.valor('foto_inicio').startswith(('89504e47', 'ffd8')):
                return self.consulta('Sube una foto del ticket (JPG o PNG).')
            self.fotos.append(p.nombres['foto'])
            ticket = self.buscar(p.valor('folio'), '', p.valor('total'))
            if not ticket:
                return self.consulta('No encontramos ese ticket.')
            return self.pagina(f'<h1>Datos fiscales</h1><p>Ticket {ticket.ticket_number} por ${ticket.amount}</p>'
                               '<form method="post" action="/emitir" onsubmit="return revisa(this)">' +
                               ocultos({'folio': ticket.ticket_number}) + campos_fiscales() + '<button>Facturar</button></form>')
        ticket = self.tickets.get(p.valor('folio'))
        if p.ruta == '/emitir' and p.metodo == 'POST' and ticket and not self.error_fiscal(p.datos):
            emision = self.emitir(ticket, p.datos)
            return self.pagina(f'<h1>Factura emitida</h1><p>Folio fiscal: {emision.folio_fiscal}</p>'
                               f'<a href="/descarga/{self.token(ticket.ticket_number)}.zip">Descargar factura</a>')
        return self.descargas(p)

    def guion(self, v):
        if v.ve('Factura emitida'):
            return v.descargar('Descargar factura')
        if v.campo('^RFC'):
            return v.formulario(v.datos_fiscales(), 'Facturar', riesgo='issue')
        foto = v.campo('^Foto', 'file')
        if foto is not None and not foto['valor']:
            return v.plan(v.accion('upload', 'ticket', 'El portal pide la foto del ticket'))
        return v.formulario([v.llenar('^Folio', v.valores['folio']), v.llenar('^Total', v.valores['total'])], 'Continuar')


# ------------------------------------------------------------------ 12) options that show up after the RFC

class OpcionesTardias(PortalComun):
    slug = 'opciones_tardias'
    marca = 'Farmacia Alivio'
    forma = 'régimen y uso aparecen tarde (1.5 s cada lista), después de capturar el RFC'
    origen = 'https://facturacion.farmaciaalivio.example'
    folio_formato = 'FA{n}'
    SCRIPT = r"""<script>
function opciones(id, lista, aviso) {
  const s = document.getElementById(id);
  s.innerHTML = '<option value="">' + aviso + '</option>' + lista.map(([v, t]) => '<option value="' + v + '">' + t + '</option>').join('');
  s.disabled = !lista.length;
}
document.getElementById('rfc').addEventListener('change', ev => {
  opciones('regimen', [], 'Cargando regímenes…'); opciones('uso', [], 'Primero elige tu régimen');
  setTimeout(async () => {
    const r = await fetch('/api/regimenes?rfc=' + encodeURIComponent(ev.target.value.trim().toUpperCase()));
    opciones('regimen', await r.json(), 'Elige tu régimen');
  }, DEMORA);
});
document.getElementById('regimen').addEventListener('change', ev => {
  opciones('uso', [], 'Cargando usos…');
  setTimeout(async () => {
    const r = await fetch('/api/usos?regimen=' + encodeURIComponent(ev.target.value));
    opciones('uso', await r.json(), 'Elige el uso del CFDI');
  }, DEMORA);
});
</script>"""
    # Milliseconds the portal takes to bring each list after the RFC or the régimen changes.
    demora_ms = 1500

    def fiscal(self, ticket, error=''):
        return self.pagina(f'<h1>Datos fiscales</h1><p>Ticket {ticket.ticket_number} por ${ticket.amount}</p>' + aviso(error) +
                           '<form method="post" action="/emitir" onsubmit="return revisa(this)">' + ocultos({'folio': ticket.ticket_number}) +
                           campos_fiscales(regimen=False, uso=False, pago=False) +
                           lista('regimen', 'Régimen fiscal', [], primera='Primero captura tu RFC', extra=' disabled') +
                           lista('uso', 'Uso del CFDI', [], primera='Primero elige tu régimen', extra=' disabled') +
                           lista('pago', 'Forma de pago', PAGOS) + '<button>Facturar</button></form>',
                           self.SCRIPT.replace('DEMORA', str(int(self.demora_ms))))

    def responder(self, p):
        if p.ruta == '/':
            return self.consulta()
        if p.ruta == '/datos' and p.metodo == 'POST':
            ticket = self.ticket_de(p)
            return self.fiscal(ticket) if ticket else self.consulta('No encontramos ese ticket.')
        if p.ruta == '/api/regimenes':
            rfc = p.valor('rfc')
            options = (REGIMENES if re.fullmatch(r'[A-ZÑ&]{4}[0-9]{6}[A-Z0-9]{3}', rfc) else
                       REGIMENES_MORAL if re.fullmatch(r'[A-ZÑ&]{3}[0-9]{6}[A-Z0-9]{3}', rfc) else [])
            return Respuesta(json.dumps(options, ensure_ascii=False), tipo='application/json')
        if p.ruta == '/api/usos':
            return Respuesta(json.dumps(USOS if p.valor('regimen') else [], ensure_ascii=False), tipo='application/json')
        ticket = self.tickets.get(p.valor('folio'))
        if p.ruta == '/emitir' and p.metodo == 'POST' and ticket:
            error = self.error_fiscal(p.datos, regimenes=REGIMENES + REGIMENES_MORAL)
            if error:
                return self.fiscal(ticket, error)
            emision = self.emitir(ticket, p.datos)
            return self.pagina(f'<h1>Factura emitida</h1><p>Folio fiscal: {emision.folio_fiscal}</p>'
                               f'<a href="/descarga/{self.token(ticket.ticket_number)}.zip">Descargar factura</a>')
        return self.descargas(p)

    def guion(self, v):
        if v.ve('Factura emitida'):
            return v.descargar('Descargar factura')
        if v.campo('^RFC'):
            fills = [action for action in v.datos_fiscales() if action]
            if fills:
                return v.plan(*fills)
            if v.datos.get('campos_fiscales_pendientes'):
                # The lists are still loading after the RFC: look again in a moment.
                return v.plan(v.accion('wait', reason='Las listas fiscales todavía cargan'))
            return v.plan(v.clic('Facturar', riesgo='issue'))
        return v.formulario(v.datos_ticket(), 'Buscar ticket')


# ------------------------------------------------------------------ 13) the final button says «Continuar»

class BotonNeutro(PortalComun):
    slug = 'boton_neutro'
    marca = 'Zapatería Paso Firme'
    forma = 'botón final con nombre neutro («Continuar»)'
    origen = 'https://facturas.pasofirme.example'
    folio_formato = 'PF{n}'

    def responder(self, p):
        if p.ruta == '/':
            return self.consulta()
        if p.ruta == '/datos' and p.metodo == 'POST':
            ticket = self.ticket_de(p)
            if not ticket:
                return self.consulta('No encontramos ese ticket.')
            return self.pagina(f'<h1>Datos fiscales</h1><p>Ticket {ticket.ticket_number} por ${ticket.amount}</p>'
                               '<form method="post" action="/revisar" onsubmit="return revisa(this)">' +
                               ocultos({'folio': ticket.ticket_number}) + campos_fiscales() + '<button>Continuar</button></form>')
        ticket = self.tickets.get(p.valor('folio'))
        if p.ruta == '/revisar' and p.metodo == 'POST' and ticket and not self.error_fiscal(p.datos):
            return self.pagina('<h1>Revisa tus datos</h1>' + resumen(ticket, p.datos) +
                               '<p>Al continuar se genera tu comprobante.</p><form method="post" action="/emitir">' +
                               ocultos(p.datos) + '<button>Continuar</button></form>')
        if p.ruta == '/emitir' and p.metodo == 'POST' and ticket and not self.error_fiscal(p.datos):
            emision = self.emitir(ticket, p.datos)
            return self.pagina(f'<h1>Factura emitida con éxito</h1><p>Folio fiscal: {emision.folio_fiscal}</p>'
                               f'<a href="/descarga/{self.token(ticket.ticket_number)}.zip">Descargar archivos</a>')
        return self.descargas(p)

    def guion(self, v):
        if v.ve('Factura emitida'):
            return v.descargar('Descargar archivos')
        if v.ve('Revisa tus datos'):
            return v.plan(v.clic('Continuar', riesgo='issue'))
        if v.campo('^RFC'):
            return v.formulario(v.datos_fiscales(), 'Continuar')
        return v.formulario(v.datos_ticket(), 'Buscar ticket')


# ------------------------------------------------------------------ 14) no download: it comes by mail

class PorCorreo(PortalComun):
    slug = 'por_correo'
    marca = 'Tintorería Prisma'
    forma = 'no da descarga: la factura llega por correo'
    origen = 'https://facturacion.tintoreriaprisma.example'
    esperado = 'emitida_espera'
    folio_formato = 'TP{n}'

    def responder(self, p):
        if p.ruta == '/':
            return self.consulta()
        if p.ruta == '/datos' and p.metodo == 'POST':
            ticket = self.ticket_de(p)
            if not ticket:
                return self.consulta('No encontramos ese ticket.')
            return self.pagina(f'<h1>Datos fiscales</h1><p>Ticket {ticket.ticket_number} por ${ticket.amount}</p>'
                               '<form method="post" action="/emitir" onsubmit="return revisa(this)">' +
                               ocultos({'folio': ticket.ticket_number}) + campos_fiscales() +
                               '<button>Solicitar factura</button></form>')
        ticket = self.tickets.get(p.valor('folio'))
        if p.ruta == '/emitir' and p.metodo == 'POST' and ticket and not self.error_fiscal(p.datos):
            self.emitir(ticket, p.datos)
            return self.pagina('<h1>Factura generada</h1><p>Te la enviaremos a tu correo en los próximos minutos.</p>')
        return None

    def guion(self, v):
        if v.ve('enviaremos a tu correo'):
            return v.plan(v.accion('done', reason='La factura llega por correo'))
        if v.campo('^RFC'):
            return v.formulario(v.datos_fiscales(), 'Solicitar factura', riesgo='issue')
        return v.formulario(v.datos_ticket(), 'Buscar ticket')


# ------------------------------------------------------------------ 15) «no soy un robot»

class Verificacion(PortalComun):
    slug = 'no_soy_robot'
    marca = 'Estacionamiento Plaza Sol'
    forma = 'verificación «no soy un robot» simulada'
    origen = 'https://facturacion.plazasol.example'
    esperado = 'persona'
    folio_formato = 'PS{n}'

    def consulta(self, error=''):
        return self.pagina('<h1>Factura tu estacionamiento</h1>' + aviso(error) +
                           '<form method="post" action="/datos" onsubmit="return revisa(this)">' + campos_ticket() +
                           '<div class="robot"><input type="checkbox" id="robot" name="robot" value="si">'
                           '<label for="robot">No soy un robot</label></div><button>Buscar ticket</button></form>')

    def responder(self, p):
        if p.ruta == '/':
            return self.consulta()
        if p.ruta == '/datos' and p.metodo == 'POST':
            self.consultas.append(dict(p.datos))
            if p.valor('robot') != 'si':
                return self.consulta('Confirma que no eres un robot.')
            ticket = self.ticket_de(p)
            return self.consulta('Verificación recibida.' if ticket else 'No encontramos ese ticket.')
        return None

    def guion(self, v):
        # The challenge belongs to the person: never solve it.
        return v.plan(Vista.accion('human', reason='El portal pide una verificación «no soy un robot»'))


PORTALES = (UnaPagina, Asistente, Iframe, ListasDiv, Mascaras, SinEtiquetas, Redireccion, RedireccionAutomatica,
            YaFacturado, Cuenta, Avisos, FotoTicket, OpcionesTardias, BotonNeutro, PorCorreo, Verificacion)
