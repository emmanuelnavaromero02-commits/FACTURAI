import asyncio
import ipaddress
import re
import socket
from urllib.parse import urlsplit

CUSTOM_PORTAL = 'portal_ticket'


def public_portal_origin(url: str) -> str | None:
    """HTTPS origin displayed to the operator; never accept credentials or browser URL quirks."""
    if not isinstance(url, str) or len(url) > 2048 or re.search(r'[\x00-\x20\x7f\\]', url):
        return None
    try:
        parts = urlsplit(url)
        host = parts.hostname or ''
        port = parts.port
    except ValueError:
        return None
    if parts.scheme != 'https' or parts.username is not None or parts.password is not None or port == 0:
        return None
    labels = host.split('.')
    if len(labels) < 2 or len(host) > 253 or not re.fullmatch(r'[a-z]{2,63}|xn--[a-z0-9-]+', labels[-1]):
        return None
    if labels[-1] in {'local', 'localhost', 'internal', 'lan', 'home', 'test', 'invalid', 'onion', 'arpa'}:
        return None
    if not all(re.fullmatch(r'[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?', label) for label in labels):
        return None
    return 'https://' + host + (f':{port}' if port not in (None, 443) else '')


async def public_addresses(url: str) -> list[str]:
    if not public_portal_origin(url):
        return []
    parts = urlsplit(url)
    try:
        results = await asyncio.wait_for(asyncio.get_running_loop().getaddrinfo(
            parts.hostname, parts.port or 443, type=socket.SOCK_STREAM), timeout=5)
    except (OSError, asyncio.TimeoutError):
        return []
    addresses = []
    for _, _, _, _, address in results:
        ip = ipaddress.ip_address(address[0])
        if not ip.is_global or ip.is_multicast or str(ip) == '168.63.129.16':
            return []
        addresses.append(str(ip))
    return list(dict.fromkeys(addresses))


async def resolves_publicly(url: str) -> bool:
    return bool(await public_addresses(url))


BILLING_WORD = re.compile(r'factur|\bcfdi\b|recibo\s+fiscal|billing|invoice', re.I)
UNRELATED = re.compile(r'encuesta|survey|promoci[oó]n|satisfacci[oó]n|feedback|privacidad|privacy|'
                       r'calif[ií]canos|opini[oó]n|verificacfdi|paga\s+aqu[ií]', re.I)
LINK = re.compile(r'''(?<![\w@./-])(?:https?://[^\s<>"']+|www\.[^\s<>"']+|(?:[a-z0-9-]+\.)+(?:com\.mx|org\.mx|gob\.mx|com|mx|org|net|io|coop|shop|store|biz|app)(?=[:/?#\s,;!)\]]|\.(?![a-z0-9-])|$)(?:[:/?#][^\s<>"']*)?)''', re.I)


def _printed_link(raw):
    value = raw.strip().rstrip('.,;!)]')
    if not value or not LINK.fullmatch(value):
        return ''
    if '://' not in value:
        value = 'https://' + value
    if not public_portal_origin(value):
        return ''
    host = urlsplit(value).hostname
    excluded = ('sat.gob.mx', 'facebook.com', 'instagram.com', 'whatsapp.com', 'wa.me', 'x.com',
                'tiktok.com', 'youtube.com', 'forms.gle', 'docs.google.com', 'maps.app.goo.gl')
    if any(host == domain or host.endswith('.' + domain) for domain in excluded) or UNRELATED.search(value):
        return ''
    return value


def printed_portals(codes: list[str], text: str) -> tuple[list[str], list[str]]:
    """Prioritize fiscal links. Other printed sites are review suggestions only."""
    primary, other, ignored = [], [], set()
    for raw in codes:
        link = _printed_link(raw)
        if link:
            (primary if BILLING_WORD.search(urlsplit(link).netloc + urlsplit(link).path) else other).append(link)
    previous = ''
    for line in text.splitlines():
        previous_link_end = 0
        for match in LINK.finditer(line):
            prefix = line[previous_link_end:match.start()].strip()
            previous_link_end = match.end()
            link = _printed_link(match[0])
            if not link:
                continue
            label = prefix[-75:] if prefix else previous[-75:]
            parts = urlsplit(link)
            if UNRELATED.search(label) and not BILLING_WORD.search(parts.netloc + parts.path):
                ignored.add(link)
            elif BILLING_WORD.search(label) or BILLING_WORD.search(parts.netloc + parts.path) or provider_for_url(link):
                primary.append(link)
            else:
                other.append(link)
        previous = line.strip()
    primary = list(dict.fromkeys(link for link in primary if link not in ignored))
    return primary, list(dict.fromkeys(link for link in other if link not in ignored and link not in primary))


def portal_suggestions(codes: list[str], text: str) -> list[str]:
    primary, other = printed_portals(codes, text)
    return primary + other

CATALOG = {
    "walmart": {
        "name": "Walmart / Bodega Aurrera",
        "url": "https://facturacion.walmartmexico.com.mx/",
        "hosts": ["facturacion.walmartmexico.com.mx"],
        "guide": "Usar TC y TR completos del ticket, tienda, fecha e importe. Guardar TR y tienda en detalles si no caben en el número de ticket. No asumir crédito a partir de la marca Mastercard: verificar débito/crédito.",
    },
    "samara": {
        "name": "FUNO / Samara Shops",
        "url": "https://estacionamientos.fibrauno.mx/facturar",
        "hosts": ["estacionamientos.fibrauno.mx"],
        "issuer_rfcs": ["DBM11011043A"],
        "guide": "Plaza SAMARA SHOPS. Fecha debajo de RECIBO NUM.; número sin espacios. RFC requiere Enter; captura manual Sin CSF si corresponde. No sustituir débito por crédito. Verificar CFDI antes de dar por terminado.",
    },
    "enerfuel": {
        "name": "Grupo Uniones / Enerfuel",
        "url": "https://factura.enerfueltech.com/NoUserInvoice",
        "hosts": ["factura.enerfueltech.com"],
        "guide": "Usar referencia exacta del QR/ticket. Premium y litros son corroboración; la referencia es la llave. Recuperar PDF/XML desde Gmail si la descarga no termina.",
    },
    "grupo_oro": {
        "name": "Grupo Oro",
        "url": "https://901bc545.cfdi.cc:8082/crear-factura-invitado",
        "hosts": ["901bc545.cfdi.cc:8082"],
        "guide": "Facturar sin registro. Validar código e importe; no cambiar fecha ni forma de pago. Ante advertencia TLS entregar el navegador al usuario; no desactivar TLS.",
    },
    "capufe": {
        "name": "CAPUFE",
        "url": "https://facturacioncapufe.com.mx/Capufe/facturacionrapida",
        "hosts": ["facturacioncapufe.com.mx"],
        "issuer_rfcs": ["FNI970829JR9"],
        "guide": "Factura rápida. Valida los códigos uno por uno. Corrige cada fecha con el botón editar si difiere del ticket. Conserva un CFDI con los códigos del expediente. 'Facturar conceptos' abre la confirmación. 'Si estoy seguro' es el envío final.",
    },
    "chilis": {
        "name": "Chili’s / Alsea",
        "url": "https://alsea.interfactura.com/RegistroDocumento.aspx?opc=Chilis",
        "hosts": ["alsea.interfactura.com"],
        "guide": "Importe de consumo; no sumar propina al importe facturable sin evidencia del portal. Distinguir total de tarjeta del subtotal de consumo. No reemitir si llegó folio por correo aunque falte descarga.",
    },
    "zorro": {
        "name": "Grupo Zorro",
        "url": "https://www.facturagrupozorro.com.mx/manualBilling",
        "hosts": ["www.facturagrupozorro.com.mx", "facturagrupozorro.com.mx"],
        "guide": "Capturar tienda, ticket, fecha e importe exactos. El error 1201 significa que el portal no encontró los datos; no inventar variantes. Guardar error y dejar pendiente.",
    },
    "pinfra": {
        "name": "Pinfra",
        "url": "https://www.pinfrafacturacion.com.mx/",
        "hosts": ["www.pinfrafacturacion.com.mx", "pinfrafacturacion.com.mx"],
        "guide": "Usar identificador completo del ticket; conservar letras y guiones. En el caso trabajado había letra O y no cero. No modificar a ciegas un código rechazado.",
    },
    "aeromexico": {
        "name": "Aeroméxico",
        "url": "https://www.aeromexico.com/es-mx/facturacion",
        "hosts": ["www.aeromexico.com", "aeromexico.com"],
        "guide": "Usar números de boleto/EMD comprobados en correo. Distinguir boletos, seguros y otros cargos. Una reserva puede producir varios CFDI; el registro de esta versión es por comprobante. Si enlaza otro dominio, solicitar revisión humana del destino.",
    },
    "bp": {
        "name": "BP / Grupo Centra", "url": "https://facturacion.grupocentra.mx/Karmi_FacturacionWeb",
        "hosts": ["facturacion.grupocentra.mx"],
        "guide": "Selecciona la marca y estación del recibo. Busca el RFC. Escribe fecha y hora en sus máscaras, consulta el ticket y agrega la fila. Verifica litros, importe y forma de pago registrados antes de emitir.",
    },
    "tesoro": {
        "name": "Tesoro / Buzón Facturas", "url": "https://buzonfacturas.com/",
        "hosts": ["buzonfacturas.com", "www.buzonfacturas.com"],
        "guide": "Usa el código impreso. Valida el total y la forma de pago. Si hay datos guardados, sustituye los campos editados en vez de agregar texto al final.",
    },
    "vianney": {
        "name": "Vianney", "url": "https://poscfdi.vianney.com.mx/",
        "hosts": ["poscfdi.vianney.com.mx"], "issuer_rfcs": ["VTH981105F90"],
        "guide": "Selecciona la serie del ticket e introduce el folio sin separador de miles. Si coincide el total, abre el editor del receptor. Busca el RFC y selecciona régimen, uso y forma de pago. Si vuelve a Consulta después de emitir, recupera por el mismo ticket.",
    },
    "starbucks": {
        "name": "Starbucks / Alsea", "url": "https://alsea.interfactura.com/",
        "hosts": ["alsea.interfactura.com"], "issuer_rfcs": ["CSI020226MV4"],
        "guide": "Selecciona Starbucks. Introduce tienda, folio, fecha e importe del ticket. Si el portal informa que el consumo aún no está disponible, conserva el pendiente.",
    },
    "oxxo": {
        "name": "OXXO", "url": "https://www4.oxxo.com/facturacionElectronica-web/views/layout/inicio.do",
        "hosts": ["www4.oxxo.com", "www4.oxxo.com:9443"], "issuer_rfcs": ["CCO8605231N4"],
        "guide": "Usa fecha, folio de venta, ID de venta e importe. Si recibes Access Denied o Error 16, conserva el pendiente y muestra el error.",
    },
    "volaris": {
        "name": "Volaris", "url": "https://factura.volaris.com/Facturacion",
        "hosts": ["factura.volaris.com"], "issuer_rfcs": ["CVA041027H80"],
        "guide": "Identifica reservación y fecha de pago. Separa los importes facturados por la aerolínea de los emitidos por agencia o paquetes.",
    },
    "liverpool": {
        "name": "Liverpool", "url": "https://facturacionclientes.liverpool.com.mx/",
        "hosts": ["facturacionclientes.liverpool.com.mx"],
        "guide": "Usa el código de facturación y los datos que muestra el ticket. Si la solicitud queda en proceso, recupérala por correo o consulta antes de volver a emitir.",
    },
    "todomoda": {
        "name": "TodoModa / Isadora", "url": "https://webportal.edicomgroup.com/customers/todomoda-isadora/search.htm",
        "hosts": ["webportal.edicomgroup.com"], "issuer_rfcs": ["BME010405MX7"],
        "guide": "Busca el ticket por sucursal, caja, folio, fecha e importe. Compara la forma de pago registrada con la información del recibo.",
    },
    "mera": {
        "name": "MERA / restaurantes", "url": "https://mera.facturehoy.com/FhAutoFacturacionAdmin/facturacion/nuevaAutoFactura.htm",
        "hosts": ["mera.facturehoy.com", "merafactura.facturehoy.com"],
        "guide": "Usa cheque, ID, fecha e importe del consumo. Si el portal muestra una verificación, pasa el control a la persona.",
    },
    "dominos": {
        "name": "Domino’s / Mi Factura Hoy", "url": "https://www.mifacturahoy.mx/",
        "hosts": ["www.mifacturahoy.mx", "mifacturahoy.mx"],
        "guide": "Captura el código tal como aparece o abre la liga del QR si coincide con el dominio aprobado. Compárala con el ticket y revisa el importe mostrado.",
    },
    "mi_casa": {
        "name": "Mi Casa Supper Club", "url": "https://mefacturo.com/micasasupperclub",
        "hosts": ["mefacturo.com", "www.mefacturo.com"],
        "guide": "Captura el código exacto del ticket. Ante un error del portal, deja el expediente pendiente para recuperar desde su canal oficial.",
    },
}


def allowed_url(url: str, provider: str, portal_url: str = '') -> bool:
    if provider == CUSTOM_PORTAL:
        origin = public_portal_origin(portal_url)
        return origin is not None and public_portal_origin(url) == origin
    try:
        parts = urlsplit(url)
        if parts.scheme != "https" or parts.username or parts.password:
            return False
        return parts.netloc.lower() in CATALOG[provider]["hosts"]
    except (ValueError, KeyError):
        return False


def provider_for_url(url: str) -> str | None:
    return next((key for key in CATALOG if allowed_url(url, key)), None)
