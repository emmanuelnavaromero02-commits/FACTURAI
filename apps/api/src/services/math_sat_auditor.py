"""
Motor Matemático Auditor SAT Anexo 20.
Valida con precisión de punto fijo (Decimal) el cumplimiento estricto de las reglas
aritméticas del Anexo 20 del SAT (CFDI 4.0 / 3.3).

Fórmula Fundamental Anexo 20:
    Total = Subtotal - Descuentos + Sum(Traslados) - Sum(Retenciones)
    Tolerancia máxima por redondeo: ±0.01 * max(1, num_conceptos)
"""

from decimal import Decimal, ROUND_HALF_UP
from typing import Any, Dict, List, Optional


def to_decimal(val: Any) -> Decimal:
    """Convierte cualquier valor a Decimal de 2 decimales redondeado."""
    if val is None:
        return Decimal("0.00")
    if isinstance(val, Decimal):
        return val
    try:
        return Decimal(str(val))
    except Exception:
        return Decimal("0.00")


def audit_sat_anexo_20_arithmetic(
    total: Optional[Decimal],
    subtotal: Optional[Decimal],
    descuento: Optional[Decimal] = None,
    traslados: Optional[Decimal] = None,
    retenciones: Optional[Decimal] = None,
    iva_16: Optional[Decimal] = None,
    ieps: Optional[Decimal] = None,
    ish: Optional[Decimal] = None,
    retencion_iva: Optional[Decimal] = None,
    retencion_isr: Optional[Decimal] = None,
    base_16: Optional[Decimal] = None,
    base_0: Optional[Decimal] = None,
    base_exenta: Optional[Decimal] = None,
    num_conceptos: int = 1,
) -> Dict[str, Any]:
    """
    Ejecuta la auditoría aritmética del Anexo 20 SAT.

    Retorna un diccionario con:
    - es_valido_anexo_20: bool
    - score_matematico: int (0 a 100)
    - total_calculado: float
    - total_declarado: float
    - discrepancia: float
    - tolerancia_permitida: float
    - tasa_efectiva_iva: Optional[float]
    - alertas: List[str]
    - desglose_resumen: Dict[str, float]
    """
    d_total = to_decimal(total)
    d_subtotal = to_decimal(subtotal)
    d_descuento = to_decimal(descuento)

    # Si se proporcionaron los componentes individuales, calcular sumas
    d_iva_16 = to_decimal(iva_16)
    d_ieps = to_decimal(ieps)
    d_ish = to_decimal(ish)
    d_ret_iva = to_decimal(retencion_iva)
    d_ret_isr = to_decimal(retencion_isr)

    sum_traslados = to_decimal(traslados) if traslados is not None else (d_iva_16 + d_ieps + d_ish)
    sum_retenciones = to_decimal(retenciones) if retenciones is not None else (d_ret_iva + d_ret_isr)

    # Fórmula Anexo 20: Total = Subtotal - Descuento + Traslados - Retenciones
    total_esperado = (d_subtotal - d_descuento + sum_traslados - sum_retenciones).quantize(
        Decimal("0.01"), rounding=ROUND_HALF_UP
    )
    discrepancia = (d_total - total_esperado).quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)
    abs_discrepancia = abs(discrepancia)

    # Tolerancia SAT: 0.01 por cada concepto reportado
    n_conceptos = max(1, int(num_conceptos))
    tolerancia_max = (Decimal("0.01") * Decimal(n_conceptos)).quantize(
        Decimal("0.01"), rounding=ROUND_HALF_UP
    )

    alertas: List[str] = []
    score_puntos = 0

    # 1. Validación Aritmética Anexo 20 (40 puntos máx)
    es_valido_aritmetica = abs_discrepancia <= tolerancia_max
    if es_valido_aritmetica:
        score_puntos += 40
    elif abs_discrepancia <= Decimal("0.05"):
        score_puntos += 25
        alertas.append(f"Discrepancia menor (${discrepancia:.2f}) excede tolerancia Anexo 20 (±${tolerancia_max:.2f}).")
    elif abs_discrepancia <= Decimal("1.00"):
        score_puntos += 10
        alertas.append(f"Discrepancia aritmética notable (${discrepancia:.2f}) vs total declarado.")
    else:
        alertas.append(
            f"Falla crítica Anexo 20: Total calculado ${total_esperado:.2f} difiere por ${discrepancia:.2f} de ${d_total:.2f}."
        )

    # 2. Coherencia de Tasas Impositivas (30 puntos máx)
    tasa_efectiva: Optional[float] = None
    d_base_16 = to_decimal(base_16) if base_16 is not None else Decimal("0.00")
    d_base_0 = to_decimal(base_0) if base_0 is not None else Decimal("0.00")
    d_base_exenta = to_decimal(base_exenta) if base_exenta is not None else Decimal("0.00")

    # Si hay base gravable al 16% explícita
    if d_base_16 > Decimal("0.00"):
        tasa_calculada = (d_iva_16 / d_base_16).quantize(Decimal("0.0001"), rounding=ROUND_HALF_UP)
        tasa_efectiva = float(tasa_calculada)
        if abs(tasa_calculada - Decimal("0.1600")) <= Decimal("0.0010"):
            score_puntos += 30
        else:
            score_puntos += 15
            alertas.append(f"Tasa efectiva de IVA al 16% ({tasa_calculada * 100:.2f}%) se desvía del 16.00% legal.")
    elif d_iva_16 > Decimal("0.00") and d_subtotal > Decimal("0.00"):
        # Estimada contra subtotal si no hay desglose por concepto
        tasa_calculada = (d_iva_16 / (d_subtotal - d_descuento)).quantize(
            Decimal("0.0001"), rounding=ROUND_HALF_UP
        ) if (d_subtotal - d_descuento) > Decimal("0.00") else Decimal("0.00")
        tasa_efectiva = float(tasa_calculada)
        if abs(tasa_calculada - Decimal("0.1600")) <= Decimal("0.0050"):
            score_puntos += 30
        elif tasa_calculada < Decimal("0.1600"):
            # Puede ser mixto (tasa 0% o exento)
            score_puntos += 25
        else:
            score_puntos += 15
            alertas.append(f"Tasa aparente de IVA ({tasa_calculada * 100:.2f}%) supera la tasa máxima de IVA del 16%.")
    elif d_iva_16 == Decimal("0.00") and (d_base_0 > Decimal("0.00") or d_base_exenta > Decimal("0.00")):
        # Correctamente tasa 0 o exento
        tasa_efectiva = 0.0
        score_puntos += 30
    else:
        # Sin IVA reportado
        score_puntos += 20

    # 3. Sanidad Estructural y Positividad (30 puntos máx)
    if d_total > Decimal("0.00") and d_subtotal > Decimal("0.00"):
        score_puntos += 20
    elif d_total <= Decimal("0.00"):
        alertas.append("El monto total debe ser estrictamente positivo.")

    if d_descuento >= Decimal("0.00") and d_descuento < d_subtotal:
        score_puntos += 10
    elif d_descuento >= d_subtotal and d_subtotal > Decimal("0.00"):
        alertas.append("El descuento no puede ser mayor o igual al subtotal.")

    # Penalización estricta si la discrepancia aritmética es severa
    if abs_discrepancia > Decimal("10.00"):
        score_puntos = min(score_puntos, 25)
    elif abs_discrepancia > Decimal("1.00"):
        score_puntos = min(score_puntos, 40)

    # Asegurar rango 0 a 100
    score_final = max(0, min(100, score_puntos))

    return {
        "es_valido_anexo_20": es_valido_aritmetica,
        "score_matematico": score_final,
        "total_calculado": float(total_esperado),
        "total_declarado": float(d_total),
        "discrepancia": float(discrepancia),
        "tolerancia_permitida": float(tolerancia_max),
        "tasa_efectiva_iva": tasa_efectiva,
        "alertas": alertas,
        "desglose_resumen": {
            "subtotal": float(d_subtotal),
            "descuento": float(d_descuento),
            "traslados": float(sum_traslados),
            "retenciones": float(sum_retenciones),
            "iva_16": float(d_iva_16),
            "ieps": float(d_ieps),
            "ish": float(d_ish),
            "retencion_iva": float(d_ret_iva),
            "retencion_isr": float(d_ret_isr),
        },
    }
