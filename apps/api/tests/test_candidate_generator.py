from datetime import date
from decimal import Decimal
import uuid
import pytest

from src.engines.candidate_generator import generate_ticket_candidate_variants
from src.models import FiscalProfile, Ticket


def test_candidate_generator_comprehensive():
    t = Ticket(
        id=uuid.uuid4(),
        tenant_id=uuid.uuid4(),
        created_by=uuid.uuid4(),
        folio="B 181103",
        web_id="46276300701400",
        transaccion="C207646",
        total=Decimal("590.00"),
        subtotal=Decimal("508.62"),
        fecha_ticket=date(2026, 9, 11),
        extracted={
            "otros": [
                {"etiqueta": "CID", "valor": "CID26522TC4"},
                {"etiqueta": "Mesa", "valor": "48 - 4 PERSONAS"},
                {"etiqueta": "Gratificacion", "valor": "59.00"},
            ]
        },
    )

    p = FiscalProfile(
        id=uuid.uuid4(),
        tenant_id=t.tenant_id,
        rfc="XAXX010101000",
        razon_social="RESTAURANTES DE MEXICO S.A. DE C.V.",
        cp="06600",
        regimen_fiscal="601",
        uso_cfdi="G03",
        email_receptor="contacto@empresa.com",
        calle="Av. Insurgentes Sur",
        numero_exterior="1602",
        numero_interior="Piso 4",
        colonia="Crédito Constructor",
        municipio_alcaldia="Benito Juárez",
        estado="Ciudad de México",
        pais="MEX",
    )

    cands = generate_ticket_candidate_variants(t, p)

    # 1. Folios y Permutaciones OCR
    folios = cands["folios"]
    assert "B 181103" in folios
    assert "B181103" in folios
    assert "181103" in folios
    assert "0000000000181103" in folios  # padded 16
    assert "46276300701400" in folios
    assert "C207646" in folios
    assert "207646" in folios
    assert "CID26522TC4" in folios
    # Permutación OCR de 0 a O en C207646
    assert any("C2O7646" == f or "O" in f for f in folios)
    # Non-identifiers like 'Mesa' or 'Gratificacion' should be excluded
    assert "48 - 4 PERSONAS" not in folios

    # 2. Totales
    totales = cands["totales"]
    assert "590.00" in totales
    assert "590" in totales
    assert "590,00" in totales
    assert "508.62" in totales

    # 3. Fechas
    fechas = cands["fechas"]
    assert "11/09/2026" in fechas
    assert "2026-09-11" in fechas
    assert "11-09-2026" in fechas
    assert "11092026" in fechas

    # 4. Razón Social
    razones = cands["razones_sociales"]
    assert "RESTAURANTES DE MEXICO S.A. DE C.V." in razones
    assert "RESTAURANTES DE MEXICO" in razones

    # 5. Usos CFDI
    usos = cands["usos_cfdi"]
    assert "G03" in usos
    assert "CP01" in usos
    assert "S01" in usos

    # 6. Dirección Fiscal
    direccion = cands["direccion"]
    assert direccion["calle"] == "Av. Insurgentes Sur"
    assert direccion["numero_exterior"] == "1602"
    assert direccion["numero_interior"] == "Piso 4"
    assert "1602" in direccion["calle_y_numero"]
    assert "Crédito Constructor" in direccion["domicilio_completo"]
    assert "Benito Juárez" in direccion["domicilio_completo"]
    assert "06600" in direccion["domicilio_completo"]
    assert "CDMX" in direccion["estado_variantes"]
    assert "Distrito Federal" in direccion["estado_variantes"]
