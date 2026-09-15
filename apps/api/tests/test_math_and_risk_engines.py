"""
Tests exhaustivos para los motores matemáticos y de riesgo fiscal SAT:
1. Auditor Aritmético CFDI Anexo 20 (math_sat_auditor.py)
2. Motor de Riesgo Fiscal Art. 69-B CFF / EFOS / EDOS (fiscal_risk_engine.py)
3. Corrector Probabilístico OCR y Dígito Verificador Módulo 11 SAT (ocr_fuzzy_corrector.py)
4. Integración en el clasificador fiscal integral (analyze_fiscal_classification)
"""

from decimal import Decimal
import pytest

from src.services.math_sat_auditor import audit_sat_anexo_20_arithmetic
from src.services.fiscal_risk_engine import (
    check_efos_art_69b,
    calculate_integrity_hash,
    analyze_amount_anomaly,
    evaluate_fiscal_risk_score,
)
from src.services.ocr_fuzzy_corrector import (
    damerau_levenshtein_distance,
    string_similarity,
    calculate_sat_rfc_check_digit,
    validate_sat_rfc,
    correct_noisy_ocr_rfc,
    match_fuzzy_merchant,
)
from src.services.fiscal_classifier import analyze_fiscal_classification


# ---------------------------------------------------------------------------
# 1. Pruebas Motor Matemático Auditor SAT Anexo 20
# ---------------------------------------------------------------------------
def test_math_sat_anexo_20_exact_balance():
    """Valida un comprobante con balance aritmético perfecto y tasa 16% exacta."""
    # Subtotal 1000.00, IVA 160.00, Total 1160.00
    res = audit_sat_anexo_20_arithmetic(
        total=Decimal("1160.00"),
        subtotal=Decimal("1000.00"),
        descuento=Decimal("0.00"),
        iva_16=Decimal("160.00"),
        base_16=Decimal("1000.00"),
        num_conceptos=1,
    )
    assert res["es_valido_anexo_20"] is True
    assert res["score_matematico"] == 100
    assert res["discrepancia"] == 0.0
    assert res["tasa_efectiva_iva"] == 0.16


def test_math_sat_anexo_20_rounding_tolerance():
    """Valida que una variación de centavos dentro de ±0.01 * N conceptos sea admitida."""
    # Subtotal 100.00, IVA 16.01 (por redondeo de 3 conceptos), Total 116.01
    res = audit_sat_anexo_20_arithmetic(
        total=Decimal("116.02"),  # 1 centavo de diferencia
        subtotal=Decimal("100.00"),
        descuento=Decimal("0.00"),
        iva_16=Decimal("16.00"),
        base_16=Decimal("100.00"),
        num_conceptos=3,  # Tolerancia ±0.03
    )
    assert res["es_valido_anexo_20"] is True
    assert abs(res["discrepancia"]) == 0.02
    assert res["tolerancia_permitida"] == 0.03


def test_math_sat_anexo_20_discrepancy_failure():
    """Detecta falla cuando la discrepancia excede la tolerancia legal."""
    res = audit_sat_anexo_20_arithmetic(
        total=Decimal("1500.00"),
        subtotal=Decimal("1000.00"),
        descuento=Decimal("0.00"),
        iva_16=Decimal("160.00"),  # Total esperado 1160.00, diferencia 340.00
        num_conceptos=1,
    )
    assert res["es_valido_anexo_20"] is False
    assert res["score_matematico"] < 60
    assert any("Falla crítica Anexo 20" in a for a in res["alertas"])


def test_math_sat_anexo_20_retentions():
    """Valida cálculo con retenciones de IVA e ISR."""
    # Subtotal 10,000, IVA 1,600, Ret IVA (10.6667%) 1,066.67, Ret ISR (10%) 1,000 -> Total 9,533.33
    subt = Decimal("10000.00")
    iva = Decimal("1600.00")
    ret_iva = Decimal("1066.67")
    ret_isr = Decimal("1000.00")
    tot = Decimal("9533.33")
    res = audit_sat_anexo_20_arithmetic(
        total=tot,
        subtotal=subt,
        iva_16=iva,
        retencion_iva=ret_iva,
        retencion_isr=ret_isr,
        num_conceptos=1,
    )
    assert res["es_valido_anexo_20"] is True
    assert res["score_matematico"] >= 90


# ---------------------------------------------------------------------------
# 2. Pruebas Motor de Riesgo Fiscal Art. 69-B CFF / EFOS / EDOS
# ---------------------------------------------------------------------------
def test_efos_art_69b_detection():
    """Detecta RFCs en lista negra de EFOS definitivos y presuntos."""
    # RFC Definitivo en lista negra
    efos_def = check_efos_art_69b("XEF900101AB1")
    assert efos_def["es_efos"] is True
    assert efos_def["situacion"] == "DEFINITIVO"

    # RFC Presunto
    efos_pres = check_efos_art_69b("FAC150320HQ2")
    assert efos_pres["es_efos"] is True
    assert efos_pres["situacion"] == "PRESUNTO"

    # RFC Limpio
    limpio = check_efos_art_69b("CCO8605231N4")
    assert limpio["es_efos"] is False
    assert limpio["situacion"] == "LIMPIO"


def test_integrity_hash_uniqueness_and_determinism():
    """Valida que el hash SHA-256 de integridad sea determinista y sensible a alteraciones."""
    h1 = calculate_integrity_hash("CCO8605231N4", "XAXX010101000", Decimal("450.50"), "2026-09-14", "F-12345")
    h2 = calculate_integrity_hash("CCO8605231N4", "XAXX010101000", Decimal("450.50"), "2026-09-14", "F-12345")
    # Mismos datos deben dar exactamente el mismo hash
    assert h1 == h2
    assert len(h1) == 64

    # Cambio en 1 centavo debe alterar completamente el hash
    h3 = calculate_integrity_hash("CCO8605231N4", "XAXX010101000", Decimal("450.51"), "2026-09-14", "F-12345")
    assert h1 != h3


def test_fiscal_risk_score_matrix():
    """Evalúa los diferentes niveles de la matriz de riesgo fiscal SAT (0 a 100)."""
    # 1. Bajo Riesgo: Gasto operativo normal pagado con tarjeta
    bajo = evaluate_fiscal_risk_score(
        rfc_emisor="CCO8605231N4",
        total=Decimal("850.00"),
        categoria_gasto="Supermercados",
        forma_pago="04",
        es_valido_anexo_20=True,
        score_matematico=100,
    )
    assert bajo["score_riesgo"] <= 25
    assert bajo["nivel"] == "BAJO"

    # 2. Medio Riesgo: Pago en efectivo de $3,500 (> $2,000 no deducible LISR Art. 27 Fracc III)
    medio = evaluate_fiscal_risk_score(
        rfc_emisor="CCO8605231N4",
        total=Decimal("3500.00"),
        categoria_gasto="Servicios",
        forma_pago="01",  # Efectivo
        es_valido_anexo_20=True,
    )
    assert 26 <= medio["score_riesgo"] <= 65
    assert medio["nivel"] == "MEDIO"
    assert any("Art. 27 Fracc. III LISR" in f for f in medio["factores_riesgo"])

    # 3. Alto Riesgo: Proveedor EFOS Definitivo + Combustible en efectivo
    alto = evaluate_fiscal_risk_score(
        rfc_emisor="XEF900101AB1",  # EFOS
        total=Decimal("1200.00"),
        categoria_gasto="Gasolinas",
        forma_pago="01",  # Efectivo en gasolina es prohibido por LISR
        es_valido_anexo_20=False,
    )
    assert alto["score_riesgo"] >= 66
    assert alto["nivel"] == "ALTO"
    assert any("EFOS Definitivo" in f for f in alto["factores_riesgo"])


# ---------------------------------------------------------------------------
# 3. Pruebas Motor Probabilístico y Corrector de Errores OCR
# ---------------------------------------------------------------------------
def test_damerau_levenshtein_distance():
    """Valida cálculo de distancia incluyendo transposiciones y sustituciones."""
    # Transposición adyacente (costo 1 en Damerau-Levenshtein)
    assert damerau_levenshtein_distance("OXXO", "OXOX") == 1
    # Sustitución simple
    assert damerau_levenshtein_distance("PEMEX", "PEMEZ") == 1
    # Cadenas idénticas
    assert damerau_levenshtein_distance("WALMART", "WALMART") == 0


def test_sat_rfc_modulo_11_check_digit():
    """Valida el cálculo del dígito verificador SAT oficial para Persona Moral y Física."""
    # CCO8605231N4 (OXXO - Persona Moral 12 chars) -> DV esperado '4'
    dv_oxxo = calculate_sat_rfc_check_digit("CCO8605231N")
    assert dv_oxxo == "4"

    # NWM9709244W4 (Walmart - Persona Moral 12 chars) -> DV esperado '4'
    dv_walmart = calculate_sat_rfc_check_digit("NWM9709244W")
    assert dv_walmart == "4"

    # Validaciones completas
    val_oxxo = validate_sat_rfc("CCO8605231N4")
    assert val_oxxo["es_valido"] is True
    assert val_oxxo["tipo"] == "PERSONA_MORAL"

    val_falsa = validate_sat_rfc("CCO8605231N9")
    assert val_falsa["es_valido"] is False
    assert "Dígito verificador SAT discordante" in val_falsa["error"]


def test_correct_noisy_ocr_rfc():
    """Corrige errores comunes de OCR como '0' en vez de 'O' y 'O' en vez de '0'."""
    # Caso 1: '0' en lugar de 'O' al inicio de OXXO ("CC08605231N4")
    noisy_1 = "CC08605231N4"
    corregido_1, conf_1 = correct_noisy_ocr_rfc(noisy_1)
    assert corregido_1 == "CCO8605231N4"
    assert conf_1 >= 0.85

    # Caso 2: 'O' en lugar de '0' en la fecha ("CCO86O5231N4")
    noisy_2 = "CCO86O5231N4"
    corregido_2, conf_2 = correct_noisy_ocr_rfc(noisy_2)
    assert corregido_2 == "CCO8605231N4"
    assert conf_2 >= 0.85


def test_match_fuzzy_merchant():
    """Encuentra coincidencia difusa en catálogo SAT ante ruido de ticket."""
    match = match_fuzzy_merchant("CADENA COMERCAL OXO")
    assert match is not None
    assert match["merchant_slug"] == "oxxo"
    assert match["rfc_sugerido"] == "CCO8605231N4"


# ---------------------------------------------------------------------------
# 4. Pruebas de Integración con analyze_fiscal_classification
# ---------------------------------------------------------------------------
def test_analyze_fiscal_classification_integrated():
    """Verifica que analyze_fiscal_classification devuelva todos los componentes nuevos."""
    res = analyze_fiscal_classification(
        comercio="CADENA COMERCIAL OXXO",
        rfc_emisor="CCO8605231N4",
        total=Decimal("232.00"),
        subtotal=Decimal("200.00"),
        iva=Decimal("32.00"),
        forma_pago_raw="04",
        conceptos_text="Consumo de bebidas y alimentos",
    )
    # Categoría y deducibilidad estándar (restaurante local es deducible_parcial al 8.5% por Art. 28 LISR)
    assert res["categoria"] in ["supermercado", "restaurante", "servicios_generales", "otros"]
    assert res["estatus_deducibilidad"] in ["deducible_parcial", "deducible_100", "DEDUCIBLE_100"]

    # Nuevos campos del Motor Matemático Anexo 20
    assert "auditoria_aritmetica" in res
    assert res["auditoria_aritmetica"]["es_valido_anexo_20"] is True
    assert res["auditoria_aritmetica"]["score_matematico"] == 100

    # Nuevos campos del Motor de Riesgo Fiscal Art. 69-B
    assert "score_riesgo_fiscal" in res
    assert 0 <= res["score_riesgo_fiscal"] <= 25
    assert "evaluacion_riesgo" in res
    assert res["evaluacion_riesgo"]["nivel"] == "BAJO"

    # Hash criptográfico de integridad
    assert "hash_integridad" in res
    assert len(res["hash_integridad"]) == 64
