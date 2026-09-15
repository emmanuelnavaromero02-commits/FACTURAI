"""
Motor Estadístico y de Inteligencia contra Riesgo Fiscal SAT (Art. 69-B CFF / EFOS / EDOS).

Evalúa el riesgo de auditoría del SAT combinando:
1. Verificación contra listas de EFOS (Empresas que Facturan Operaciones Simuladas - Art. 69-B CFF).
2. Hash criptográfico SHA-256 de integridad y antifraude de doble timbrado.
3. Análisis estadístico de anomalías de montos (Heurística de Benford y montos redondos atípicos).
4. Score integral de riesgo fiscal de 0 a 100 (Bajo, Medio, Alto).
"""

import hashlib
import math
import re
from decimal import Decimal
from typing import Any, Dict, List, Optional, Set

# Muestra representativa y extensible de RFCs listados en Art. 69-B (EFOS Definitivos y Presuntos)
# En ambiente empresarial se sincroniza periódicamente con el CSV del DOF / SAT
EFOS_BLACKLIST_SAMPLE: Dict[str, Dict[str, str]] = {
    "XEF900101AB1": {"situacion": "DEFINITIVO", "publicacion": "DOF 2023-04-12"},
    "SIM890214XX9": {"situacion": "DEFINITIVO", "publicacion": "DOF 2022-11-05"},
    "FAC150320HQ2": {"situacion": "PRESUNTO", "publicacion": "DOF 2024-01-18"},
    "FAN1108097G3": {"situacion": "DEFINITIVO", "publicacion": "DOF 2021-08-30"},
    "OPF160415KZ8": {"situacion": "PRESUNTO", "publicacion": "DOF 2024-06-10"},
}


def calculate_integrity_hash(
    rfc_emisor: Optional[str],
    rfc_receptor: Optional[str],
    total: Optional[Decimal],
    fecha: Optional[str],
    folio: Optional[str] = None,
) -> str:
    """
    Genera un hash criptográfico SHA-256 único e inmutable para el comprobante.
    Permite detectar intentos de doble facturación o colisiones en la bóveda fiscal.
    """
    norm_emisor = (rfc_emisor or "").strip().upper()
    norm_receptor = (rfc_receptor or "").strip().upper()
    norm_total = f"{float(total or 0):.2f}"
    norm_fecha = (fecha or "").strip()
    norm_folio = (folio or "").strip().upper()

    raw_payload = f"{norm_emisor}|{norm_receptor}|{norm_total}|{norm_fecha}|{norm_folio}"
    return hashlib.sha256(raw_payload.encode("utf-8")).hexdigest()


def check_efos_art_69b(rfc_emisor: Optional[str]) -> Dict[str, Any]:
    """
    Verifica si el RFC emisor se encuentra en el listado negro del Art. 69-B del CFF.
    """
    if not rfc_emisor:
        return {"es_efos": False, "situacion": "NO_EMISOR", "detalles": "Sin RFC emisor registrado."}

    norm_rfc = rfc_emisor.strip().upper()

    # Chequeo contra RFC genérico nacional usado indebidamente como emisor comercial
    if norm_rfc == "XAXX010101000":
        return {
            "es_efos": True,
            "situacion": "RFC_GENERICO_PUBLICO_GENERAL",
            "detalles": "RFC genérico público en general utilizado indebidamente como emisor deducible.",
        }

    if norm_rfc in EFOS_BLACKLIST_SAMPLE:
        efo_info = EFOS_BLACKLIST_SAMPLE[norm_rfc]
        return {
            "es_efos": True,
            "situacion": efo_info["situacion"],
            "detalles": f"RFC emisor listado en Art. 69-B CFF como {efo_info['situacion']} ({efo_info['publicacion']}).",
        }

    return {"es_efos": False, "situacion": "LIMPIO", "detalles": "Sin coincidencias en lista negra Art. 69-B CFF."}


def analyze_amount_anomaly(total: Optional[Decimal]) -> Dict[str, Any]:
    """
    Evalúa la probabilidad estadística del monto.
    En contabilidad mexicana (IVA 16%), montos cerrados y redondos en miles exactos
    (ej. $10,000.00, $50,000.00) tienen mayor probabilidad de ser simulados o atípicos.
    """
    if not total or total <= Decimal("0.00"):
        return {"es_anomalo": False, "puntuacion_anomalia": 0, "motivo": "Monto nulo o no disponible."}

    f_total = float(total)
    primer_digito = int(str(int(f_total))[0]) if int(f_total) > 0 else 0

    # Probabilidad esperada según Ley de Benford: P(d) = log10(1 + 1/d)
    benford_prob = math.log10(1 + 1 / primer_digito) if 1 <= primer_digito <= 9 else 0.0

    # Detección de cifras sospechosamente cerradas (múltiplos exactos de 1,000 mayores a 5,000)
    centavos = f_total - int(f_total)
    es_redondo_miles = (centavos == 0.0) and (int(f_total) % 1000 == 0) and (f_total >= 5000.0)

    puntos = 0
    motivos = []

    if es_redondo_miles:
        puntos += 20
        motivos.append(f"Monto redondo exacto en millares (${f_total:,.2f}), atípico frente a cálculos de IVA 16%.")

    if primer_digito >= 8 and f_total > 50000.0:
        # Frecuencia natural en Benford para 8 o 9 es < 5%
        puntos += 10
        motivos.append(f"Monto elevado con primer dígito de baja frecuencia estadística ({primer_digito}).")

    return {
        "es_anomalo": puntos > 0,
        "puntuacion_anomalia": puntos,
        "primer_digito": primer_digito,
        "probabilidad_benford": round(benford_prob, 4),
        "motivo": "; ".join(motivos) if motivos else "Monto dentro de rangos estadísticos normales.",
    }


def evaluate_fiscal_risk_score(
    rfc_emisor: Optional[str],
    total: Optional[Decimal],
    categoria_gasto: Optional[str],
    forma_pago: Optional[str],
    es_valido_anexo_20: bool = True,
    score_matematico: int = 100,
    es_viatico_foraneo: bool = False,
) -> Dict[str, Any]:
    """
    Matriz de Riesgo Fiscal SAT (Score de 0 a 100).
    - 0 a 25: RIESGO BAJO (Auditoría verde, deducibilidad sólida)
    - 26 a 65: RIESGO MEDIO (Precaución fiscal, requiere soporte documental)
    - 66 a 100: RIESGO ALTO (Peligro de rechazo por el SAT, Art. 69-B o infracción legal)
    """
    riesgo_acumulado = 0
    factores_riesgo: List[str] = []

    # 1. Art. 69-B CFF (Lista Negra EFOS)
    efos_check = check_efos_art_69b(rfc_emisor)
    if efos_check["es_efos"]:
        if efos_check["situacion"] == "DEFINITIVO":
            riesgo_acumulado += 85
            factores_riesgo.append("CRÍTICO: Proveedor listado como EFOS Definitivo en Art. 69-B CFF.")
        elif efos_check["situacion"] == "PRESUNTO":
            riesgo_acumulado += 55
            factores_riesgo.append("ALERTA: Proveedor con presunción de operaciones simuladas (Art. 69-B CFF).")
        elif efos_check["situacion"] == "RFC_GENERICO_PUBLICO_GENERAL":
            riesgo_acumulado += 40
            factores_riesgo.append("RFC genérico XAXX010101000 no permite deducción corporativa.")

    # 2. Art. 27 Fracción III LISR (Efectivo mayor a $2,000 MXN)
    f_total = float(total or 0)
    es_efectivo = forma_pago in ["01", "efectivo", "Efectivo"]

    if es_efectivo and f_total > 2000.0:
        riesgo_acumulado += 40
        factores_riesgo.append("Pago en efectivo mayor a $2,000 MXN no deducible (Art. 27 Fracc. III LISR).")

    # 3. Gasolina en Efectivo (Sin importar el monto)
    if categoria_gasto == "Gasolinas" and es_efectivo and f_total > 0:
        riesgo_acumulado += 50
        factores_riesgo.append("Combustible pagado en efectivo no deducible (Art. 27 Fracc. III LISR exige tarjeta/monedero).")

    # 4. Auditoría Aritmética Anexo 20
    if not es_valido_anexo_20:
        riesgo_acumulado += 30
        factores_riesgo.append("Comprobante con discrepancia aritmética superior a tolerancia SAT Anexo 20.")
    elif score_matematico < 70:
        riesgo_acumulado += 15
        factores_riesgo.append("Inconsistencia en desglose de tasas de impuestos (Score matemático < 70).")

    # 5. Anomalía Estadística de Montos
    anomalia = analyze_amount_anomaly(total)
    if anomalia["es_anomalo"]:
        riesgo_acumulado += anomalia["puntuacion_anomalia"]
        factores_riesgo.append(f"Anomalía de importe: {anomalia['motivo']}")

    # 6. Viáticos de restaurantes locales
    if categoria_gasto == "Restaurantes" and not es_viatico_foraneo:
        # En la misma plaza solo el 8.5% es deducible
        riesgo_acumulado += 10
        factores_riesgo.append("Consumo en restaurante dentro de la circunscripción local deducible sólo al 8.5% (Art. 28 Fracc. XX LISR).")

    # Delimitar score entre 0 y 100
    score_riesgo = max(0, min(100, riesgo_acumulado))

    # Nivel cualitativo
    if score_riesgo <= 25:
        nivel = "BAJO"
        color = "verde"
        recomendacion = "Comprobante fiscalmente sólido y plenamente auditable ante el SAT."
    elif score_riesgo <= 65:
        nivel = "MEDIO"
        color = "amarillo"
        recomendacion = "Conservar evidencia documental del entregable o póliza para desvirtuar observaciones SAT."
    else:
        nivel = "ALTO"
        color = "rojo"
        recomendacion = "Riesgo crítico de no deducibilidad o auditoría SAT. Revisar situación con el emisor."

    return {
        "score_riesgo": score_riesgo,
        "nivel": nivel,
        "color": color,
        "recomendacion": recomendacion,
        "factores_riesgo": factores_riesgo,
        "efos_info": efos_check,
        "anomalia_monto": anomalia,
    }
