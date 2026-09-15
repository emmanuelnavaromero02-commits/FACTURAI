import re
from datetime import date
from decimal import Decimal
from typing import Any, Dict, List, Optional

from ..models import FiscalProfile, Ticket


# Estados de México y variantes comunes utilizadas en portales de facturación
ESTADOS_MEXICO: Dict[str, List[str]] = {
    "AGUASCALIENTES": ["Aguascalientes", "AGS"],
    "BAJA CALIFORNIA": ["Baja California", "BC", "B.C."],
    "BAJA CALIFORNIA SUR": ["Baja California Sur", "BCS", "B.C.S."],
    "CAMPECHE": ["Campeche", "CAMP"],
    "CHIAPAS": ["Chiapas", "CHIS"],
    "CHIHUAHUA": ["Chihuahua", "CHIH"],
    "CIUDAD DE MEXICO": ["Ciudad de México", "CDMX", "Distrito Federal", "DF", "D.F."],
    "COAHUILA": ["Coahuila", "Coahuila de Zaragoza", "COAH"],
    "COLIMA": ["Colima", "COL"],
    "DURANGO": ["Durango", "DGO"],
    "ESTADO DE MEXICO": ["Estado de México", "Edomex", "Edo. de México", "MEX", "México"],
    "GUANAJUATO": ["Guanajuato", "GTO"],
    "GUERRERO": ["Guerrero", "GRO"],
    "HIDALGO": ["Hidalgo", "HGO"],
    "JALISCO": ["Jalisco", "JAL"],
    "MICHOACAN": ["Michoacán", "Michoacán de Ocampo", "MICH"],
    "MORELOS": ["Morelos", "MOR"],
    "NAYARIT": ["Nayarit", "NAY"],
    "NUEVO LEON": ["Nuevo León", "NL", "N.L."],
    "OAXACA": ["Oaxaca", "OAX"],
    "PUEBLA": ["Puebla", "PUE"],
    "QUERETARO": ["Querétaro", "QRO"],
    "QUINTANA ROO": ["Quintana Roo", "Q. ROO", "QROO"],
    "SAN LUIS POTOSI": ["San Luis Potosí", "SLP", "S.L.P."],
    "SINALOA": ["Sinaloa", "SIN"],
    "SONORA": ["Sonora", "SON"],
    "TABASCO": ["Tabasco", "TAB"],
    "TAMAULIPAS": ["Tamaulipas", "TAMPS"],
    "TLAXCALA": ["Tlaxcala", "TLAX"],
    "VERACRUZ": ["Veracruz", "Veracruz de Ignacio de la Llave", "VER"],
    "YUCATAN": ["Yucatán", "YUC"],
    "ZACATECAS": ["Zacatecas", "ZAC"],
}


def generate_ocr_permutations(text: str) -> List[str]:
    """
    Genera permutaciones probables de confusión OCR para tickets térmicos y borrosos:
    - 0 <-> O
    - 1 <-> I / l
    - 5 <-> S
    - 8 <-> B
    - 2 <-> Z
    Retorna variantes únicas (máximo 6 para evitar combinatorias excesivas).
    """
    if not text or len(text) < 3 or len(text) > 30:
        return []

    confusion_map = {
        "0": ["O"],
        "O": ["0"],
        "1": ["I"],
        "I": ["1"],
        "5": ["S"],
        "S": ["5"],
        "8": ["B"],
        "B": ["8"],
        "2": ["Z"],
        "Z": ["2"],
    }

    variants: List[str] = []
    for i, char in enumerate(text):
        if char in confusion_map:
            for replacement in confusion_map[char]:
                new_str = text[:i] + replacement + text[i + 1 :]
                if new_str != text and new_str not in variants:
                    variants.append(new_str)
                    if len(variants) >= 6:
                        return variants
    return variants


def generate_fiscal_address_variants(perfil: FiscalProfile) -> Dict[str, Any]:
    """
    Construye las variantes de formato de dirección fiscal que los portales
    mexicanos suelen solicitar en sus formularios.
    """
    calle = (perfil.calle or "").strip()
    num_ext = (perfil.numero_exterior or "").strip()
    num_int = (perfil.numero_interior or "").strip()
    colonia = (perfil.colonia or "").strip()
    municipio = (perfil.municipio_alcaldia or "").strip()
    estado_raw = (perfil.estado or "").strip()
    cp = (perfil.cp or "").strip()

    # Calle y número
    calle_y_num_parts = [calle] if calle else []
    if num_ext:
        calle_y_num_parts.append(num_ext)
    if num_int:
        calle_y_num_parts.append(f"Int {num_int}")
    calle_y_numero = " ".join(calle_y_num_parts).strip()

    # Domicilio completo
    domicilio_parts = []
    if calle_y_numero:
        domicilio_parts.append(calle_y_numero)
    if colonia:
        domicilio_parts.append(f"Col. {colonia}")
    if municipio:
        domicilio_parts.append(municipio)
    if estado_raw:
        domicilio_parts.append(estado_raw)
    if cp:
        domicilio_parts.append(f"C.P. {cp}")
    domicilio_completo = ", ".join(domicilio_parts)

    # Variantes de estado
    estado_variants = [estado_raw] if estado_raw else []
    if estado_raw:
        clean_state_key = re.sub(
            r"[^A-Z ]",
            "",
            estado_raw.upper()
            .replace("Á", "A")
            .replace("É", "E")
            .replace("Í", "I")
            .replace("Ó", "O")
            .replace("Ú", "U"),
        )
        for key, syns in ESTADOS_MEXICO.items():
            if key in clean_state_key or clean_state_key in key:
                for s in syns:
                    if s not in estado_variants:
                        estado_variants.append(s)

    return {
        "domicilio_completo": domicilio_completo or None,
        "calle_y_numero": calle_y_numero or None,
        "calle": calle or None,
        "numero_exterior": num_ext or None,
        "numero_interior": num_int or None,
        "colonia": colonia or None,
        "municipio_alcaldia": municipio or None,
        "cp": cp or None,
        "estado": estado_raw or None,
        "estado_variantes": estado_variants,
        "pais": perfil.pais or "MEX",
        "telefono": perfil.telefono or None,
    }


def generate_ticket_candidate_variants(
    ticket: Ticket,
    perfil: FiscalProfile,
    merchant_config: Optional[Dict[str, Any]] = None,
) -> Dict[str, Any]:
    """
    Genera candidatos estructurados y variantes alternativas para que el agente
    pruebe de forma autónoma ante errores de validación de los portales.
    Prioriza longitudes y formatos si se provee una receta aprendida del comercio.
    """
    reglas = merchant_config.get("reglas", {}) if isinstance(merchant_config, dict) else {}
    target_ticket_digits = reglas.get("ticket_digits")
    target_tienda_digits = reglas.get("tienda_digits")
    target_fecha_format = reglas.get("fecha_format")

    # 1. Candidatos de Folio e Identificadores del Ticket
    folios_candidatos: List[str] = []
    seen_folios = set()

    def add_folio(val: Optional[str]):
        if not val:
            return
        cleaned = str(val).strip()
        if cleaned and cleaned not in seen_folios and len(cleaned) >= 2:
            folios_candidatos.append(cleaned)
            seen_folios.add(cleaned)

    # a. Folio principal y variantes
    if ticket.folio:
        raw_folio = ticket.folio.strip()
        add_folio(raw_folio)
        # Sin espacios ni guiones
        no_spaces = raw_folio.replace(" ", "").replace("-", "")
        add_folio(no_spaces)
        # Solo números (ej. 'B 181103' -> '181103')
        digits_only = re.sub(r"\D", "", raw_folio)
        if len(digits_only) >= 3:
            add_folio(digits_only)
            # Sin ceros a la izquierda
            no_leading_zeros = digits_only.lstrip("0")
            if no_leading_zeros:
                add_folio(no_leading_zeros)
            # Con relleno a 16 dígitos (formato común en portales como PRB/KFC)
            if len(digits_only) < 16:
                add_folio(digits_only.zfill(16))

            # Con formato según regla aprendida del comercio (ej. 9 dígitos Alsea)
            if target_ticket_digits:
                td = int(target_ticket_digits)
                if len(digits_only) < td:
                    add_folio(digits_only.zfill(td))
                elif len(digits_only) > td:
                    add_folio(digits_only[:td])
                    add_folio(digits_only[-td:])

        # Permutaciones de confusión OCR (térmicos/borrosos: 0/O, 1/I, 5/S, 8/B)
        for perm in generate_ocr_permutations(raw_folio):
            add_folio(perm)

    # b. Web ID / Referencia
    if ticket.web_id:
        raw_web_id = ticket.web_id.strip()
        add_folio(raw_web_id)
        digits_web = re.sub(r"\D", "", raw_web_id)
        if digits_web:
            add_folio(digits_web)
        for perm in generate_ocr_permutations(raw_web_id):
            add_folio(perm)

    # c. Transacción / Orden
    if ticket.transaccion:
        raw_tx = ticket.transaccion.strip()
        add_folio(raw_tx)
        digits_tx = re.sub(r"\D", "", raw_tx)
        if digits_tx:
            add_folio(digits_tx)
        for perm in generate_ocr_permutations(raw_tx):
            add_folio(perm)

    # d. Combinaciones comunes de cadenas mexicanas (Tienda/Sucursal + Ticket/Folio como en Alsea / Domino's / Walmart)
    sucursal_digits = re.sub(r"\D", "", ticket.sucursal or "")
    folio_digits = re.sub(r"\D", "", ticket.folio or "")
    if sucursal_digits and folio_digits:
        add_folio(f"{sucursal_digits}-{folio_digits}")
        add_folio(f"{sucursal_digits}{folio_digits}")
        add_folio(f"{folio_digits}-{sucursal_digits}")

    # e. Campos secundarios de extracción ('otros')
    if ticket.extracted and isinstance(ticket.extracted, dict):
        for item in ticket.extracted.get("otros", []):
            if isinstance(item, dict):
                v = str(item.get("valor", "")).strip()
                k = str(item.get("etiqueta", "")).lower()
                # Excluir montos, mesas o datos no identificadores
                if not any(ign in k for ign in ("mesa", "personas", "monto", "total", "propina", "gratificacion")):
                    if len(v) >= 3:
                        add_folio(v)

    # 2. Candidatos de Total e Importes
    totales_candidatos: List[str] = []
    seen_totales = set()

    def add_total(val: Optional[str]):
        if not val:
            return
        cleaned = str(val).strip()
        if cleaned and cleaned not in seen_totales:
            totales_candidatos.append(cleaned)
            seen_totales.add(cleaned)

    if ticket.total is not None:
        # Formato con 2 decimales
        fmt_2dec = f"{Decimal(str(ticket.total)):.2f}"
        add_total(fmt_2dec)
        # Formato entero si termina en .00
        if fmt_2dec.endswith(".00"):
            add_total(fmt_2dec[:-3])
        # Formato con coma decimal
        add_total(fmt_2dec.replace(".", ","))

    if ticket.subtotal is not None:
        sub_2dec = f"{Decimal(str(ticket.subtotal)):.2f}"
        add_total(sub_2dec)
        if sub_2dec.endswith(".00"):
            add_total(sub_2dec[:-3])

    # 3. Candidatos de Fecha
    fechas_candidatos: List[str] = []
    seen_fechas = set()

    def add_fecha(val: Optional[str]):
        if not val:
            return
        cleaned = str(val).strip()
        if cleaned and cleaned not in seen_fechas:
            fechas_candidatos.append(cleaned)
            seen_fechas.add(cleaned)

    if ticket.fecha_ticket:
        d = ticket.fecha_ticket
        if isinstance(d, str):
            add_fecha(d)
        elif isinstance(d, date):
            add_fecha(d.strftime("%d/%m/%Y"))
            add_fecha(d.strftime("%Y-%m-%d"))
            add_fecha(d.strftime("%d-%m-%Y"))
            add_fecha(d.strftime("%d%m%Y"))
            add_fecha(d.strftime("%Y/%m/%d"))
    else:
        # Fallback con fecha de creación del ticket o fecha actual para portales con fecha obligatoria
        ref_date = getattr(ticket, "created_at", None)
        if ref_date and hasattr(ref_date, "date"):
            d = ref_date.date()
        else:
            d = date.today()
        add_fecha(d.strftime("%d/%m/%Y"))
        add_fecha(d.strftime("%Y-%m-%d"))
        add_fecha(d.strftime("%d-%m-%Y"))

    # 4. Candidatos de Razón Social
    razones_sociales: List[str] = []
    seen_razones = set()

    def add_razon(val: Optional[str]):
        if not val:
            return
        cleaned = str(val).strip()
        if cleaned and cleaned not in seen_razones:
            razones_sociales.append(cleaned)
            seen_razones.add(cleaned)

    if perfil.razon_social:
        raw_rs = perfil.razon_social.strip()
        add_razon(raw_rs)
        # Quitar régimen societario común (S.A. de C.V., S. de R.L. de C.V., S.A.P.I., etc.)
        cleaned_rs = re.sub(
            r"(?i)\s+(S\.?A\.?\s+DE\s+C\.?V\.?|S\.?A\.?P\.?I\.?\s+DE\s+C\.?V\.?|S\.?DE\s+R\.?L\.?\s+DE\s+C\.?V\.?|S\.?A\.?|S\.?C\.?|I\.?A\.?P\.?|A\.?C\.?)\.?$",
            "",
            raw_rs,
        ).strip()
        if cleaned_rs:
            add_razon(cleaned_rs)

    # 5. Candidatos de Uso CFDI
    usos_cfdi = [perfil.uso_cfdi]
    for u in ("G03", "CP01", "S01", "G01"):
        if u not in usos_cfdi:
            usos_cfdi.append(u)

    # 6. Variantes de Dirección Fiscal
    direccion_candidatos = generate_fiscal_address_variants(perfil)

    # 7. Candidatos de Tienda / Sucursal Numérica (ej. Alsea / Domino's / Walmart)
    tienda_candidatos: List[str] = []
    if ticket.sucursal:
        m = re.search(r"(?:tienda|sucursal|tienda\s*no\.?)\s*[:#\-]?\s*(\d+)", ticket.sucursal, re.IGNORECASE)
        if m and m.group(1) not in tienda_candidatos:
            tienda_candidatos.append(m.group(1))
        for num in re.findall(r"\b\d{4,6}\b", ticket.sucursal):
            if num not in tienda_candidatos:
                tienda_candidatos.append(num)

    # Si hay regla aprendida de longitud de tienda (ej. 5 dígitos para Alsea)
    if target_tienda_digits and ticket.sucursal:
        td_digits = int(target_tienda_digits)
        for num in re.findall(rf"\b\d{{{td_digits}}}\b", ticket.sucursal):
            if num not in tienda_candidatos:
                tienda_candidatos.insert(0, num)

    # Priorización según reglas aprendidas del comercio
    if target_ticket_digits:
        td = int(target_ticket_digits)
        folios_candidatos.sort(
            key=lambda f: 0 if len(re.sub(r"\D", "", f)) == td else 1
        )

    if target_tienda_digits:
        tienda_d = int(target_tienda_digits)
        tienda_candidatos.sort(
            key=lambda t: 0 if len(re.sub(r"\D", "", t)) == tienda_d else 1
        )

    if target_fecha_format:
        if target_fecha_format == "dd/mm/aaaa":
            fechas_candidatos.sort(
                key=lambda f: 0 if "/" in f and len(f.split("/")[0]) == 2 and len(f.split("/")[-1]) == 4 else 1
            )
        elif target_fecha_format in ("aaaa-mm-dd", "yyyy-mm-dd"):
            fechas_candidatos.sort(
                key=lambda f: 0 if "-" in f and len(f.split("-")[0]) == 4 else 1
            )

    return {
        "folios": folios_candidatos,
        "totales": totales_candidatos,
        "fechas": fechas_candidatos,
        "razones_sociales": razones_sociales,
        "usos_cfdi": usos_cfdi,
        "direccion": direccion_candidatos,
        "sucursal": ticket.sucursal,
        "tienda": tienda_candidatos[0] if tienda_candidatos else None,
        "tiendas": tienda_candidatos,
        "caja": ticket.caja,
        "hora": ticket.hora_ticket.strftime("%H:%M") if ticket.hora_ticket else None,
    }
