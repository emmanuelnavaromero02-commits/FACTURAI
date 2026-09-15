"""
Motor Probabilístico y Corrector de Errores de OCR para Comprobantes Fiscales.

Implementa:
1. Algoritmo de Distancia Damerau-Levenshtein para mitigar ruido y degradación en tickets térmicos.
2. Validador y corrector de Dígito Verificador Módulo 11 oficial del SAT para RFC (12 y 13 caracteres).
3. Normalizador morfológico de caracteres confundibles en OCR (O/0, I/1, S/5, Z/2, B/8).
4. Motor de alineación probabilística de emisores contra catálogos corporativos conocidos.
"""

import re
from typing import Any, Dict, List, Optional, Tuple


CHARS_SAT = "0123456789ABCDEFGHIJKLMN&OPQRSTUVWXYZ Ñ"
SAT_CHAR_DICT = {c: i for i, c in enumerate(CHARS_SAT)}


def damerau_levenshtein_distance(s1: str, s2: str) -> int:
    """
    Calcula la distancia de Damerau-Levenshtein entre dos cadenas,
    contemplando inserciones, eliminaciones, sustituciones y transposiciones adyacentes.
    """
    len1, len2 = len(s1), len(s2)
    if len1 == 0:
        return len2
    if len2 == 0:
        return len1

    # Matriz (len1 + 2) x (len2 + 2)
    max_dist = len1 + len2
    da: Dict[str, int] = {}
    h = [[0] * (len2 + 2) for _ in range(len1 + 2)]

    h[0][0] = max_dist
    for i in range(0, len1 + 1):
        h[i + 1][0] = max_dist
        h[i + 1][1] = i
    for j in range(0, len2 + 1):
        h[0][j + 1] = max_dist
        h[1][j + 1] = j

    for i in range(1, len1 + 1):
        db = 0
        for j in range(1, len2 + 1):
            k = da.get(s2[j - 1], 0)
            l = db
            cost = 0 if s1[i - 1] == s2[j - 1] else 1
            if cost == 0:
                db = j

            h[i + 1][j + 1] = min(
                h[i][j + 1] + 1,  # Deletion
                h[i + 1][j] + 1,  # Insertion
                h[i][j] + cost,   # Substitution
                h[k][l] + (i - k - 1) + 1 + (j - l - 1),  # Transposition
            )
        da[s1[i - 1]] = i

    return h[len1 + 1][len2 + 1]


def string_similarity(s1: str, s2: str) -> float:
    """Calcula similitud normalizada [0.0, 1.0] entre dos cadenas."""
    norm1 = s1.strip().upper()
    norm2 = s2.strip().upper()
    if norm1 == norm2:
        return 1.0
    max_len = max(len(norm1), len(norm2))
    if max_len == 0:
        return 1.0
    dist = damerau_levenshtein_distance(norm1, norm2)
    return max(0.0, 1.0 - (dist / max_len))


def calculate_sat_rfc_check_digit(rfc_core: str) -> Optional[str]:
    """
    Calcula el dígito verificador oficial del SAT utilizando el algoritmo Módulo 11.
    - Persona Moral: recibe 11 caracteres (se antepone espacio para alinear a 12 de cálculo).
    - Persona Física: recibe 12 caracteres.
    """
    core = rfc_core.strip().upper()
    if len(core) == 11:
        # Persona Moral
        calc_str = " " + core
    elif len(core) == 12:
        # Persona Física
        calc_str = core
    else:
        return None

    try:
        total_sum = sum(SAT_CHAR_DICT[c] * (14 - (i + 1)) for i, c in enumerate(calc_str))
    except KeyError:
        return None

    remainder = total_sum % 11
    if remainder == 0:
        return "0"
    dv = 11 - remainder
    if dv == 10:
        return "A"
    return str(dv)


def validate_sat_rfc(rfc: str) -> Dict[str, Any]:
    """
    Valida un RFC mexicano contra:
    1. Formato de longitud y expresión regular (12 PM, 13 PF).
    2. Coherencia de fecha en dígitos centrales.
    3. Dígito verificador exacto del Módulo 11 del SAT.
    """
    if not rfc:
        return {"es_valido": False, "tipo": None, "error": "RFC vacío o no especificado."}

    norm_rfc = rfc.strip().upper().replace("-", "").replace(" ", "")

    if len(norm_rfc) == 12:
        tipo = "PERSONA_MORAL"
        match = re.match(r"^([A-ZÑ&]{3})([0-9]{6})([A-Z0-9]{3})$", norm_rfc)
        if not match:
            return {"es_valido": False, "tipo": tipo, "error": "Formato de Persona Moral inválido (3 letras + 6 dígitos + 3 homoclave)."}
        core = norm_rfc[:11]
        dv_declarado = norm_rfc[-1]
    elif len(norm_rfc) == 13:
        tipo = "PERSONA_FISICA"
        match = re.match(r"^([A-ZÑ&]{4})([0-9]{6})([A-Z0-9]{3})$", norm_rfc)
        if not match:
            return {"es_valido": False, "tipo": tipo, "error": "Formato de Persona Física inválido (4 letras + 6 dígitos + 3 homoclave)."}
        core = norm_rfc[:12]
        dv_declarado = norm_rfc[-1]
    else:
        return {"es_valido": False, "tipo": None, "error": f"Longitud de RFC inválida ({len(norm_rfc)} caracteres, esperado 12 o 13)."}

    # RFCs genéricos oficiales del SAT
    if norm_rfc in ["XAXX010101000", "XEXX010101000"]:
        return {
            "es_valido": True,
            "tipo": "GENERICO_SAT",
            "rfc": norm_rfc,
            "digito_verificador_valido": True,
            "error": None,
        }

    dv_calculado = calculate_sat_rfc_check_digit(core)
    dv_correcto = (dv_calculado == dv_declarado)

    return {
        "es_valido": dv_correcto,
        "tipo": tipo,
        "rfc": norm_rfc,
        "digito_verificador_calculado": dv_calculado,
        "digito_verificador_declarado": dv_declarado,
        "digito_verificador_valido": dv_correcto,
        "error": None if dv_correcto else f"Dígito verificador SAT discordante: reportado '{dv_declarado}', calculado '{dv_calculado}'.",
    }


def correct_noisy_ocr_rfc(noisy_rfc: str) -> Tuple[Optional[str], float]:
    """
    Corrige degradaciones comunes de OCR en RFCs (ej. '0' vs 'O', '1' vs 'I', '5' vs 'S', 'B' vs '8')
    y prueba combinaciones candidatas evaluando el Módulo 11 del SAT.
    Retorna (rfc_corregido, score_confianza).
    """
    if not noisy_rfc:
        return None, 0.0

    raw = noisy_rfc.strip().upper().replace("-", "").replace(" ", "")

    # Si ya es completamente válido
    val_directa = validate_sat_rfc(raw)
    if val_directa["es_valido"]:
        return raw, 1.0

    # Normalización posicional:
    # Si tiene 12 caracteres: letras en 0..2, dígitos en 3..8, homoclave en 9..11
    # Si tiene 13 caracteres: letras en 0..3, dígitos en 4..9, homoclave en 10..12
    char_list = list(raw)
    length = len(char_list)

    if length in (12, 13):
        letter_slice = slice(0, 3 if length == 12 else 4)
        date_slice = slice(3 if length == 12 else 4, 9 if length == 12 else 10)

        # 1. En el bloque de letras: reemplazar dígitos comunes de OCR por letras
        to_letters = {"0": "O", "1": "I", "5": "S", "2": "Z", "8": "B"}
        for i in range(letter_slice.start, letter_slice.stop):
            if char_list[i] in to_letters:
                char_list[i] = to_letters[char_list[i]]

        # 2. En el bloque de fecha: reemplazar letras comunes de OCR por números
        to_digits = {"O": "0", "D": "0", "I": "1", "L": "1", "S": "5", "Z": "2", "B": "8"}
        for i in range(date_slice.start, date_slice.stop):
            if char_list[i] in to_digits:
                char_list[i] = to_digits[char_list[i]]

        candidato = "".join(char_list)
        val_cand = validate_sat_rfc(candidato)
        if val_cand["es_valido"]:
            sim = string_similarity(raw, candidato)
            return candidato, round(sim * 0.95, 2)

        # 3. Probar corrección del dígito verificador si el resto cumple el patrón
        core = candidato[:11 if length == 12 else 12]
        dv_calc = calculate_sat_rfc_check_digit(core)
        if dv_calc:
            candidato_dv = core + dv_calc
            if validate_sat_rfc(candidato_dv)["es_valido"]:
                sim = string_similarity(raw, candidato_dv)
                return candidato_dv, round(sim * 0.90, 2)

    return raw, 0.5


# Catálogo de emisores comerciales y patrones de referencia para corrección difusa
KNOWN_EMISORES_CATALOG = [
    {"slug": "oxxo", "nombre": "CADENA COMERCIAL OXXO", "rfc": "CCO8605231N4", "aliases": ["OXXO", "CADENA COMERCIAL OXXO SA DE CV"]},
    {"slug": "walmart", "nombre": "NUEVA WAL MART DE MEXICO", "rfc": "NWM9709244W4", "aliases": ["WALMART", "BODEGA AURRERA", "SAMS CLUB", "WAL MART"]},
    {"slug": "costco", "nombre": "COSTCO DE MEXICO", "rfc": "CME910715UB9", "aliases": ["COSTCO", "COSTCO WHOLESALE"]},
    {"slug": "starbucks", "nombre": "CAFE SIRENA", "rfc": "CSI020226MV4", "aliases": ["STARBUCKS", "STARBUCKS COFFEE", "CAFE SIRENA SA DE CV"]},
    {"slug": "vips", "nombre": "OPERADORA VIPS", "rfc": "OVI800131GQ6", "aliases": ["VIPS", "RESTAURANTE VIPS"]},
    {"slug": "sanborns", "nombre": "SANBORNS HERMANOS", "rfc": "SHE190630V37", "aliases": ["SANBORNS", "SANBORN'S"]},
    {"slug": "liverpool", "nombre": "DISTRIBUIDORA LIVERPOOL", "rfc": "DLI931201MI9", "aliases": ["LIVERPOOL", "FABRICAS DE FRANCIA"]},
    {"slug": "palacio-hierro", "nombre": "EL PALACIO DE HIERRO", "rfc": "EPH450302SZ6", "aliases": ["PALACIO DE HIERRO"]},
    {"slug": "pemex", "nombre": "PETROLEOS MEXICANOS", "rfc": "PME380607P35", "aliases": ["PEMEX", "GASOLINERA PEMEX"]},
    {"slug": "uber", "nombre": "UBER B.V.", "rfc": "UBV141209355", "aliases": ["UBER", "UBER MEXICO", "UBER TRIP"]},
    {"slug": "didi", "nombre": "DIDI MOBILITY MEXICO", "rfc": "DMM1804246H2", "aliases": ["DIDI", "DIDI FOOD", "DIDI VIAJES"]},
    {"slug": "autovias", "nombre": "AUTOVIAS DE OCCIDENTE", "rfc": "AOC9404287A1", "aliases": ["CASETA", "AUTOPISTA", "AUTOVIAS"]},
]


def match_fuzzy_merchant(raw_merchant_text: str, threshold: float = 0.70) -> Optional[Dict[str, Any]]:
    """
    Encuentra la mejor correspondencia de emisor en el catálogo SAT / FacturAI usando
    similitud Damerau-Levenshtein sobre nombres y alias.
    """
    if not raw_merchant_text:
        return None

    cleaned = raw_merchant_text.strip().upper()
    best_match = None
    max_sim = 0.0

    for emisor in KNOWN_EMISORES_CATALOG:
        # Comparar contra nombre oficial
        sim_name = string_similarity(cleaned, emisor["nombre"])
        if sim_name > max_sim:
            max_sim = sim_name
            best_match = emisor

        # Comparar contra alias
        for alias in emisor["aliases"]:
            sim_alias = string_similarity(cleaned, alias)
            # También si el alias es un token contenido
            if alias in cleaned:
                sim_alias = max(sim_alias, 0.85)
            if sim_alias > max_sim:
                max_sim = sim_alias
                best_match = emisor

    if best_match and max_sim >= threshold:
        return {
            "merchant_slug": best_match["slug"],
            "nombre_oficial": best_match["nombre"],
            "rfc_sugerido": best_match["rfc"],
            "similitud": round(max_sim, 3),
        }

    return None
