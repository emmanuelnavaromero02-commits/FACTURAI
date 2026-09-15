from decimal import Decimal
import logging
import re
import xml.etree.ElementTree as ET
from typing import Any, Dict, List, Optional

logger = logging.getLogger(__name__)

# Espacios de nombres estándar de CFDI 3.3 y 4.0 del SAT
CFDI_NAMESPACES = {
    "cfdi": "http://www.sat.gob.mx/cfd/4",
    "cfdi33": "http://www.sat.gob.mx/cfd/3",
    "implocal": "http://www.sat.gob.mx/implocal",
    "aerolineas": "http://www.sat.gob.mx/aerolineas",
}

# Claves de Producto o Servicio del SAT por categoría
SAT_CLAVES_CATEGORIA = {
    "combustible": [
        "15101505",  # Combustible diésel
        "15101514",  # Gasolina regular (Magna, menor a 91 octanos)
        "15101515",  # Gasolina premium (mayor o igual a 91 octanos)
        "15101500",  # Combustibles
    ],
    "hospedaje": [
        "90111500",  # Hoteles y moteles y pensiones
        "90111501",  # Hoteles
        "90111502",  # Moteles
        "90111800",  # Alojamiento en campamentos y casas de huéspedes
        "90111801",  # Alquiler de habitaciones
        "90111802",  # Alojamiento temporal
    ],
    "restaurante": [
        "90101500",  # Restaurantes
        "90101501",  # Restaurantes de autoservicio / buffet
        "90101503",  # Bares o pubs
        "90101700",  # Cafeterías
        "90101800",  # Comida rápida
    ],
    "casetas_peaje": [
        "95111602",  # Servicios de peaje de autopista
        "95111600",  # Peajes de transporte
    ],
    "vuelos_transporte": [
        "78111500",  # Transporte aéreo de pasajeros
        "78111800",  # Transporte de pasajeros por carretera / taxis / ridesharing
        "78111802",  # Servicios de autobuses
        "78111804",  # Servicios de taxi
    ],
}

# Palabras clave y RFCs para detección por nombre o texto
KEYWORD_PATTERNS = {
    "combustible": [
        "gasolina", "combustible", "magna", "premium", "diesel", "pemex", "oxxo gas",
        "petro seven", "g500", "hidrosina", "bp", "shell", "mobil", "repsol", "total gas",
        "gasolinera", "estacion de servicio", "litros", "despachador"
    ],
    "hospedaje": [
        "hotel", "motel", "posada", "inn", "suites", "resort", "fiesta americana",
        "city express", "holiday inn", "marriott", "hilton", "habitacion", "hospedaje",
        "check in", "check out", "posadas", "airbnb", "estancia"
    ],
    "restaurante": [
        "restaurante", "cafeteria", "bistro", "taqueria", "pizzeria", "domino", "starbucks",
        "vips", "toks", "sanborns", "chilis", "italiannis", "burger king", "mcdonald",
        "alimentos", "consumo", "mesa", "propina", "mesero", "comida", "comedor", "bar"
    ],
    "supermercado": [
        "walmart", "bodega aurrera", "soriana", "chedraui", "superama", "la comer",
        "heb", "costco", "sam's", "sams club", "tiendas 3b", "oxxo", "7-eleven", "supermercado",
        "abarrotes", "despensa", "farmacia guadalajara", "farmacias del ahorro"
    ],
    "casetas_peaje": [
        "capufe", "peaje", "caseta", "autopista", "via corta", "pinfra", "ideal", "rco",
        "viapass", "pase", "iave", "televia", "tramo carretero"
    ],
    "vuelos_transporte": [
        "aeromexico", "volaris", "vivaaerobus", "aerolinea", "vuelo", "avion", "embarque",
        "tua", "uber", "didi", "cabify", "taxi", "ado", "primera plus", "etn", "autobus"
    ],
}


def to_decimal(val: Any) -> Decimal:
    if val is None:
        return Decimal("0.00")
    try:
        cleaned = str(val).strip().replace(",", "").replace("$", "")
        return Decimal(cleaned)
    except Exception:
        return Decimal("0.00")


def classify_expense_by_text_and_rfc(
    comercio: Optional[str] = None,
    rfc_emisor: Optional[str] = None,
    conceptos_text: Optional[str] = None,
    claves_prod_serv: Optional[List[str]] = None,
) -> str:
    """
    Clasifica automáticamente el tipo de gasto a partir de claves SAT, nombres y RFC.
    Retorna una categoría: 'combustible', 'hospedaje', 'restaurante', 'supermercado',
    'casetas_peaje', 'vuelos_transporte', 'servicios_generales' u 'otros'.
    """
    # 1. Prioridad Máxima: Clave de Producto o Servicio del SAT (si está disponible del XML)
    if claves_prod_serv:
        for cps in claves_prod_serv:
            clean_cps = cps.strip()
            for cat, codes in SAT_CLAVES_CATEGORIA.items():
                if any(clean_cps.startswith(code[:6]) or clean_cps == code for code in codes):
                    return cat

    # 2. Análisis de texto conjunto (Comercio, RFC, conceptos)
    text_corpus = " ".join([
        (comercio or "").lower(),
        (rfc_emisor or "").lower(),
        (conceptos_text or "").lower(),
    ])

    for cat in ("combustible", "casetas_peaje", "hospedaje", "vuelos_transporte", "restaurante", "supermercado"):
        keywords = KEYWORD_PATTERNS.get(cat, [])
        if any(kw in text_corpus for kw in keywords):
            return cat

    if any(k in text_corpus for k in ("cfe", "telmex", "telcel", "at&t", "izzi", "totalplay", "office depot", "officemax")):
        return "servicios_generales"

    return "otros"


def parse_cfdi_tax_breakdown(xml_content: str | bytes) -> Dict[str, Any]:
    """
    Inspecciona y calcula minuciosamente el desglose de impuestos de un comprobante CFDI 3.3 o 4.0:
    - Base e importe a Tasa 16%
    - Base e importe a Tasa 0%
    - Base Exenta
    - IEPS (cuota o porcentaje)
    - ISH (Impuesto Sobre Hospedaje en nodo ImpuestosLocales)
    - TUA (Tarifa de Uso de Aeropuerto)
    - Retenciones de IVA e ISR
    - Claves ProdServ extraídas
    - Forma de Pago SAT (01 efectivo, 03 transferencia, 04 crédito, 28 débito, etc.)
    """
    if isinstance(xml_content, str):
        xml_bytes = xml_content.encode("utf-8")
    else:
        xml_bytes = xml_content

    result: Dict[str, Any] = {
        "forma_pago": None,
        "metodo_pago": None,
        "uso_cfdi": None,
        "subtotal": Decimal("0.00"),
        "total": Decimal("0.00"),
        "descuento": Decimal("0.00"),
        "base_16": Decimal("0.00"),
        "iva_16": Decimal("0.00"),
        "base_0": Decimal("0.00"),
        "iva_0": Decimal("0.00"),
        "base_exenta": Decimal("0.00"),
        "ieps": Decimal("0.00"),
        "ish": Decimal("0.00"),
        "tua": Decimal("0.00"),
        "retencion_iva": Decimal("0.00"),
        "retencion_isr": Decimal("0.00"),
        "claves_prod_serv": [],
        "conceptos_resumen": [],
    }

    try:
        root = ET.fromstring(xml_bytes)
    except Exception as exc:
        logger.warning("Error al parsear XML de CFDI: %s", exc)
        return result

    # Atributos raíz del comprobante
    result["forma_pago"] = root.attrib.get("FormaPago")
    result["metodo_pago"] = root.attrib.get("MetodoPago")
    result["subtotal"] = to_decimal(root.attrib.get("SubTotal"))
    result["total"] = to_decimal(root.attrib.get("Total"))
    result["descuento"] = to_decimal(root.attrib.get("Descuento"))

    # Uso de CFDI en Receptor
    for elem in root.iter():
        tag = elem.tag.split("}")[-1]
        if tag == "Receptor":
            result["uso_cfdi"] = elem.attrib.get("UsoCFDI")
        elif tag == "Concepto":
            cps = elem.attrib.get("ClaveProdServ")
            if cps and cps not in result["claves_prod_serv"]:
                result["claves_prod_serv"].append(cps)
            desc = elem.attrib.get("Descripcion")
            if desc:
                result["conceptos_resumen"].append(desc[:60])
                if "tua" in desc.lower():
                    result["tua"] += to_decimal(elem.attrib.get("Importe"))

    # 1. Localizar nodo global <cfdi:Impuestos> (hijo directo de Comprobante)
    global_impuestos = None
    for child in root:
        if child.tag.split("}")[-1] == "Impuestos":
            global_impuestos = child
            break

    # Si hay nodo global de impuestos, usamos sus Traslados y Retenciones (evitando duplicar conceptos)
    traslados_to_process = []
    retenciones_to_process = []

    if global_impuestos is not None:
        for elem in global_impuestos.iter():
            tag = elem.tag.split("}")[-1]
            if tag == "Traslado":
                traslados_to_process.append(elem)
            elif tag == "Retencion":
                retenciones_to_process.append(elem)
    else:
        # Si no hay nodo global, extraer de los conceptos individuales
        for elem in root.iter():
            tag = elem.tag.split("}")[-1]
            if tag == "Traslado":
                traslados_to_process.append(elem)
            elif tag == "Retencion":
                retenciones_to_process.append(elem)

    for elem in traslados_to_process:
        impuesto = elem.attrib.get("Impuesto")
        tipo_factor = elem.attrib.get("TipoFactor", "").upper()
        tasa_o_cuota = to_decimal(elem.attrib.get("TasaOCuota"))
        base = to_decimal(elem.attrib.get("Base"))
        importe = to_decimal(elem.attrib.get("Importe"))

        # Si no tiene Base explícita pero tiene tasa e importe (ej. CFDI 3.3 global)
        if base == Decimal("0.00") and importe > Decimal("0.00") and tasa_o_cuota > Decimal("0.00"):
            base = round(importe / tasa_o_cuota, 2)
        elif base == Decimal("0.00") and importe > Decimal("0.00") and result["subtotal"] > Decimal("0.00"):
            base = result["subtotal"]

        # 002 = IVA
        if impuesto == "002" or impuesto is None:
            if tipo_factor == "EXENTO":
                result["base_exenta"] += base
            elif tasa_o_cuota == Decimal("0.160000") or (tasa_o_cuota == Decimal("0") and importe > Decimal("0")):
                result["base_16"] += base
                result["iva_16"] += importe
            elif tasa_o_cuota == Decimal("0.000000") or (tipo_factor == "TASA" and importe == Decimal("0")):
                result["base_0"] += base
                result["iva_0"] += importe
            else:
                result["base_16"] += base
                result["iva_16"] += importe

        # 003 = IEPS
        elif impuesto == "003":
            result["ieps"] += importe

    for elem in retenciones_to_process:
        impuesto = elem.attrib.get("Impuesto")
        importe = to_decimal(elem.attrib.get("Importe"))
        if impuesto == "002":
            result["retencion_iva"] += importe
        elif impuesto == "001":
            result["retencion_isr"] += importe

    # 2. Complemento de Impuestos Locales (ej. ISH en Hoteles)
    for elem in root.iter():
        tag = elem.tag.split("}")[-1]
        if tag in ("TrasladosLocales", "RetencionesLocales"):
            nom_imp = (elem.attrib.get("ImpLocTrasladado") or elem.attrib.get("ImpLocRetenido") or "").lower()
            importe = to_decimal(elem.attrib.get("Importe"))
            if any(k in nom_imp for k in ("hospedaje", "ish", "hotel")):
                result["ish"] += importe

    return result


def estimate_tax_breakdown_from_ticket(
    total: Optional[Decimal],
    subtotal: Optional[Decimal],
    iva: Optional[Decimal],
    categoria: str,
) -> Dict[str, Any]:
    """
    Estima el desglose de impuestos a partir de los datos leídos del ticket físico
    cuando el XML formal aún no ha sido descargado.
    """
    tot = to_decimal(total)
    sub = to_decimal(subtotal)
    iva_m = to_decimal(iva)

    if tot == Decimal("0.00") and sub > Decimal("0.00"):
        tot = sub + iva_m

    base_16 = Decimal("0.00")
    iva_16 = Decimal("0.00")
    base_0 = Decimal("0.00")
    base_exenta = Decimal("0.00")
    ieps = Decimal("0.00")
    ish = Decimal("0.00")

    if categoria == "combustible":
        # En gasolinas mexicanas, el IVA es 16% sobre la base sin IEPS cuota
        if iva_m > Decimal("0.00"):
            iva_16 = iva_m
            base_16 = round(iva_m / Decimal("0.16"), 2)
            ieps = max(Decimal("0.00"), tot - base_16 - iva_16)
        else:
            base_16 = round(tot / Decimal("1.16"), 2)
            iva_16 = tot - base_16

    elif categoria == "hospedaje":
        # En hoteles, típicamente hay IVA 16% e ISH 3% o 3.5%
        if iva_m > Decimal("0.00"):
            iva_16 = iva_m
            base_16 = round(iva_m / Decimal("0.16"), 2)
            ish = max(Decimal("0.00"), tot - base_16 - iva_16)
        else:
            # Aproximación estándar: 16% IVA + 3% ISH = 1.19
            base_16 = round(tot / Decimal("1.19"), 2)
            iva_16 = round(base_16 * Decimal("0.16"), 2)
            ish = tot - base_16 - iva_16

    elif categoria in ("restaurante", "casetas_peaje", "servicios_generales"):
        # Casi 100% gravado a tasa 16%
        if iva_m > Decimal("0.00"):
            iva_16 = iva_m
            base_16 = sub if sub > Decimal("0.00") else round(iva_m / Decimal("0.16"), 2)
        else:
            base_16 = round(tot / Decimal("1.16"), 2)
            iva_16 = tot - base_16

    elif categoria == "supermercado":
        # En compras mixtas de supermercado suele haber tasa 0% (comida) y tasa 16%
        if iva_m > Decimal("0.00"):
            iva_16 = iva_m
            base_16 = round(iva_m / Decimal("0.16"), 2)
            base_0 = max(Decimal("0.00"), tot - base_16 - iva_16)
        else:
            # Si no hay IVA desglosado en el ticket de despensa, es mayoritariamente tasa 0%
            base_0 = tot

    else:
        if iva_m > Decimal("0.00"):
            iva_16 = iva_m
            base_16 = sub if sub > Decimal("0.00") else round(iva_m / Decimal("0.16"), 2)
        else:
            base_16 = round(tot / Decimal("1.16"), 2)
            iva_16 = tot - base_16

    return {
        "base_16": base_16,
        "iva_16": iva_16,
        "base_0": base_0,
        "iva_0": Decimal("0.00"),
        "base_exenta": base_exenta,
        "ieps": ieps,
        "ish": ish,
        "tua": Decimal("0.00"),
        "retencion_iva": Decimal("0.00"),
        "retencion_isr": Decimal("0.00"),
    }


def evaluate_sat_deducibility(
    categoria: str,
    forma_pago: Optional[str] = None,
    total: Optional[Decimal] = None,
    es_viatico_foraneo: bool = False,
) -> Dict[str, Any]:
    """
    Evalúa las reglas de deducibilidad del SAT según el Artículo 27 y 28 de la LISR:
    - Combustible: Efectivo (01) es NO DEDUCIBLE.
    - Restaurantes: 8.5% en consumo local; 100% en viáticos foráneos con comprobación.
    - Gastos > $2,000 MXN en efectivo: NO DEDUCIBLES.
    - Hospedaje, peajes, vuelos: Deducibles al 100% como viáticos.
    """
    tot = to_decimal(total)
    fp = (forma_pago or "").strip()

    # 1. Regla Crítica: Combustibles (Art. 27 Fracc. III LISR)
    if categoria == "combustible":
        if fp == "01":
            return {
                "estatus": "no_deducible",
                "porcentaje_deducible": Decimal("0.00"),
                "motivo": "Art. 27 Fracc. III LISR: El combustible pagado en efectivo no es deducible. Requiere tarjeta, transferencia o monedero electrónico.",
                "color": "rojo",
            }
        elif fp in ("03", "04", "28", "29", "05"):
            return {
                "estatus": "deducible_100",
                "porcentaje_deducible": Decimal("100.00"),
                "motivo": "Combustible deducible al 100% pagado mediante medio electrónico bancario autorizado.",
                "color": "verde",
            }
        else:
            return {
                "estatus": "condicionado",
                "porcentaje_deducible": Decimal("100.00"),
                "motivo": "Deducible al 100% condicionado a que el medio de pago sea electrónico (no efectivo).",
                "color": "amarillo",
            }

    # 2. Regla de Restaurantes y Alimentos (Art. 28 Fracc. XX LISR)
    if categoria == "restaurante":
        if es_viatico_foraneo:
            return {
                "estatus": "deducible_100",
                "porcentaje_deducible": Decimal("100.00"),
                "motivo": "Art. 28 Fracc. XX LISR: Consumo de alimentos en viaje foráneo deducible al 100% (hasta $750 MXN diarios en territorio nacional).",
                "color": "verde",
            }
        else:
            return {
                "estatus": "deducible_parcial",
                "porcentaje_deducible": Decimal("8.50"),
                "motivo": "Art. 28 Fracc. XX LISR: Los consumos en restaurantes dentro de la zona del contribuyente solo son deducibles al 8.5% pagados con tarjeta o transferencia.",
                "color": "amarillo",
            }

    # 3. Regla General de Efectivo > $2,000 MXN (Art. 27 Fracc. III LISR)
    if fp == "01" and tot > Decimal("2000.00"):
        return {
            "estatus": "no_deducible",
            "porcentaje_deducible": Decimal("0.00"),
            "motivo": f"Art. 27 Fracc. III LISR: Gastos mayores a $2,000 MXN (${tot}) pagados en efectivo no son deducibles.",
            "color": "rojo",
        }

    # 4. Hospedaje, Casetas y Transporte
    if categoria in ("hospedaje", "casetas_peaje", "vuelos_transporte"):
        return {
            "estatus": "deducible_100",
            "porcentaje_deducible": Decimal("100.00"),
            "motivo": "Gasto de transporte, peaje o viático de hospedaje deducible al 100%.",
            "color": "verde",
        }

    # 5. Resto de compras comerciales y servicios
    return {
        "estatus": "deducible_100",
        "porcentaje_deducible": Decimal("100.00"),
        "motivo": "Gasto operativo deducible al 100% cumpliendo requisitos de CFDI.",
        "color": "verde",
    }


def analyze_fiscal_classification(
    comercio: Optional[str] = None,
    rfc_emisor: Optional[str] = None,
    total: Optional[Decimal] = None,
    subtotal: Optional[Decimal] = None,
    iva: Optional[Decimal] = None,
    xml_bytes: Optional[bytes] = None,
    forma_pago_raw: Optional[str] = None,
    es_viatico_foraneo: bool = False,
) -> Dict[str, Any]:
    """
    Función integral que clasifica la factura, extrae o estima el desglose de impuestos
    (Tasa 16%, 0%, Exento, IEPS, ISH, TUA, Retenciones) y evalúa el estatus de deducibilidad SAT.
    """
    claves_prod_serv = []
    forma_pago = forma_pago_raw
    conceptos_resumen = []

    # 1. Si hay XML, hacer análisis formal con precisión SAT
    if xml_bytes:
        xml_data = parse_cfdi_tax_breakdown(xml_bytes)
        claves_prod_serv = xml_data.get("claves_prod_serv", [])
        conceptos_resumen = xml_data.get("conceptos_resumen", [])
        if xml_data.get("forma_pago"):
            forma_pago = xml_data["forma_pago"]

        categoria = classify_expense_by_text_and_rfc(
            comercio=comercio,
            rfc_emisor=rfc_emisor,
            conceptos_text=" ".join(conceptos_resumen),
            claves_prod_serv=claves_prod_serv,
        )

        tax_breakdown = {
            "base_16": float(xml_data["base_16"]),
            "iva_16": float(xml_data["iva_16"]),
            "base_0": float(xml_data["base_0"]),
            "iva_0": float(xml_data["iva_0"]),
            "base_exenta": float(xml_data["base_exenta"]),
            "ieps": float(xml_data["ieps"]),
            "ish": float(xml_data["ish"]),
            "tua": float(xml_data["tua"]),
            "retencion_iva": float(xml_data["retencion_iva"]),
            "retencion_isr": float(xml_data["retencion_isr"]),
            "fuente": "cfdi_xml",
        }
        tot_eval = xml_data["total"] or total

    else:
        # 2. Estimación a partir de ticket impreso / visión
        categoria = classify_expense_by_text_and_rfc(
            comercio=comercio,
            rfc_emisor=rfc_emisor,
            conceptos_text="",
            claves_prod_serv=None,
        )
        est = estimate_tax_breakdown_from_ticket(total, subtotal, iva, categoria)
        tax_breakdown = {
            "base_16": float(est["base_16"]),
            "iva_16": float(est["iva_16"]),
            "base_0": float(est["base_0"]),
            "iva_0": float(est["iva_0"]),
            "base_exenta": float(est["base_exenta"]),
            "ieps": float(est["ieps"]),
            "ish": float(est["ish"]),
            "tua": float(est["tua"]),
            "retencion_iva": float(est["retencion_iva"]),
            "retencion_isr": float(est["retencion_isr"]),
            "fuente": "estimacion_ticket",
        }
        tot_eval = total

    deducibilidad = evaluate_sat_deducibility(
        categoria=categoria,
        forma_pago=forma_pago,
        total=tot_eval,
        es_viatico_foraneo=es_viatico_foraneo,
    )

    return {
        "categoria": categoria,
        "desglose_impuestos": tax_breakdown,
        "estatus_deducibilidad": deducibilidad["estatus"],
        "porcentaje_deducible": float(deducibilidad["porcentaje_deducible"]),
        "motivo_deducibilidad": deducibilidad["motivo"],
        "color_deducibilidad": deducibilidad["color"],
        "forma_pago": forma_pago,
        "claves_prod_serv": claves_prod_serv,
    }
