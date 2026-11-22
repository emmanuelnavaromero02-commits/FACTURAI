from urllib.parse import urlsplit

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


def allowed_url(url: str, provider: str) -> bool:
    try:
        parts = urlsplit(url)
        if parts.scheme != "https" or parts.username or parts.password:
            return False
        return parts.netloc.lower() in CATALOG[provider]["hosts"]
    except (ValueError, KeyError):
        return False


def provider_for_url(url: str) -> str | None:
    return next((key for key in CATALOG if allowed_url(url, key)), None)
