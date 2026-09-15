from decimal import Decimal
import pytest
from httpx import ASGITransport, AsyncClient

from src.main import app
from src.models import Ticket, TicketEstado
from src.services.fiscal_classifier import (
    analyze_fiscal_classification,
    classify_expense_by_text_and_rfc,
    estimate_tax_breakdown_from_ticket,
    evaluate_sat_deducibility,
    parse_cfdi_tax_breakdown,
)


def test_classify_expense_by_text_and_rfc():
    # 1. Combustibles
    assert classify_expense_by_text_and_rfc(comercio="Estación de Servicio PEMEX 4512", rfc_emisor="GME980101AA1") == "combustible"
    assert classify_expense_by_text_and_rfc(comercio="Oxxo Gas", rfc_emisor="OXG010101AB2") == "combustible"
    assert classify_expense_by_text_and_rfc(claves_prod_serv=["15101514"]) == "combustible"
    assert classify_expense_by_text_and_rfc(claves_prod_serv=["15101505"]) == "combustible"

    # 2. Hoteles y Hospedaje
    assert classify_expense_by_text_and_rfc(comercio="Hotel Fiesta Americana Reforma") == "hospedaje"
    assert classify_expense_by_text_and_rfc(comercio="City Express Suites") == "hospedaje"
    assert classify_expense_by_text_and_rfc(claves_prod_serv=["90111500"]) == "hospedaje"

    # 3. Restaurantes
    assert classify_expense_by_text_and_rfc(comercio="Domino's Pizza Alsea") == "restaurante"
    assert classify_expense_by_text_and_rfc(comercio="Starbucks Coffee") == "restaurante"
    assert classify_expense_by_text_and_rfc(comercio="KFC (Premium Restaurant Brands SDE RL DE CV)") == "restaurante"
    assert classify_expense_by_text_and_rfc(comercio="RESTAURANTES TOKS, S.A. DE C.V.") == "restaurante"
    assert classify_expense_by_text_and_rfc(claves_prod_serv=["90101500"]) == "restaurante"

    # 4. Supermercado
    assert classify_expense_by_text_and_rfc(comercio="Nueva Walmart de México") == "supermercado"
    assert classify_expense_by_text_and_rfc(comercio="Tiendas Chedraui") == "supermercado"

    # 5. Casetas
    assert classify_expense_by_text_and_rfc(comercio="CAPUFE Autopista México Cuernavaca") == "casetas_peaje"
    assert classify_expense_by_text_and_rfc(claves_prod_serv=["95111602"]) == "casetas_peaje"

    # 6. Vuelos / Transporte
    assert classify_expense_by_text_and_rfc(comercio="Aeroméxico Vuelo AM102") == "vuelos_transporte"
    assert classify_expense_by_text_and_rfc(comercio="Uber Technologies") == "vuelos_transporte"
    assert classify_expense_by_text_and_rfc(comercio="Autobuses de Oriente ADO") == "vuelos_transporte"

    # 7. Servicios Generales y Entretenimiento (evitando que 'operadora' detone 'ado')
    assert classify_expense_by_text_and_rfc(comercio="Cinemex (Operadora de Cinemas SA de CV)") == "servicios_generales"
    assert classify_expense_by_text_and_rfc(comercio="CFE Suministrador de Servicios Basicos") == "servicios_generales"


def test_sat_deducibility_rules():
    # 1. Combustible: Efectivo (01) es NO DEDUCIBLE (Art. 27 Fracc. III LISR)
    res_efectivo = evaluate_sat_deducibility(categoria="combustible", forma_pago="01", total=Decimal("800.00"))
    assert res_efectivo["estatus"] == "no_deducible"
    assert res_efectivo["color"] == "rojo"
    assert "Art. 27" in res_efectivo["motivo"]

    # 2. Combustible: Tarjeta de crédito (04) o débito (28) es DEDUCIBLE 100%
    res_tarjeta = evaluate_sat_deducibility(categoria="combustible", forma_pago="04", total=Decimal("800.00"))
    assert res_tarjeta["estatus"] == "deducible_100"
    assert res_tarjeta["color"] == "verde"

    # 3. Restaurante: Consumo local deducible al 8.5% (Art. 28 Fracc. XX LISR)
    res_rest_local = evaluate_sat_deducibility(categoria="restaurante", forma_pago="04", total=Decimal("500.00"), es_viatico_foraneo=False)
    assert res_rest_local["estatus"] == "deducible_parcial"
    assert res_rest_local["porcentaje_deducible"] == Decimal("8.50")

    # 4. Restaurante: Viático foráneo deducible al 100%
    res_rest_viatico = evaluate_sat_deducibility(categoria="restaurante", forma_pago="04", total=Decimal("500.00"), es_viatico_foraneo=True)
    assert res_rest_viatico["estatus"] == "deducible_100"

    # 5. Efectivo mayor a $2,000 MXN en compra general
    res_general_efectivo = evaluate_sat_deducibility(categoria="supermercado", forma_pago="01", total=Decimal("3500.00"))
    assert res_general_efectivo["estatus"] == "no_deducible"


def test_estimate_tax_breakdown_from_ticket():
    # Restaurante con total $1160.00 y subtotal $1000.00
    est = estimate_tax_breakdown_from_ticket(
        total=Decimal("1160.00"),
        subtotal=Decimal("1000.00"),
        iva=Decimal("160.00"),
        categoria="restaurante",
    )
    assert est["base_16"] == Decimal("1000.00")
    assert est["iva_16"] == Decimal("160.00")

    # Hotel con desglose estimado de ISH
    hotel_est = estimate_tax_breakdown_from_ticket(
        total=Decimal("1190.00"),
        subtotal=Decimal("1000.00"),
        iva=Decimal("160.00"),
        categoria="hospedaje",
    )
    assert hotel_est["base_16"] == Decimal("1000.00")
    assert hotel_est["iva_16"] == Decimal("160.00")
    assert hotel_est["ish"] == Decimal("30.00")


def test_parse_cfdi_xml_breakdown():
    xml_sample = """<?xml version="1.0" encoding="UTF-8"?>
    <cfdi:Comprobante xmlns:cfdi="http://www.sat.gob.mx/cfd/4"
        xmlns:implocal="http://www.sat.gob.mx/implocal"
        Version="4.0" Serie="H" Folio="9981" Fecha="2026-09-14T20:00:00"
        FormaPago="04" MetodoPago="PUE" SubTotal="2000.00" Total="2390.00">
        <cfdi:Emisor Rfc="HFA900101AB1" Nombre="HOTELES FIESTA AMERICANA SA DE CV" RegimenFiscal="601"/>
        <cfdi:Receptor Rfc="XAXX010101000" Nombre="EMPRESA DEMO" UsoCFDI="G03"/>
        <cfdi:Conceptos>
            <cfdi:Concepto ClaveProdServ="90111501" Cantidad="1" ClaveUnidad="E48" Descripcion="Hospedaje 1 noche habitacion deluxe" ValorUnitario="2000.00" Importe="2000.00">
                <cfdi:Impuestos>
                    <cfdi:Traslados>
                        <cfdi:Traslado Base="2000.00" Impuesto="002" TipoFactor="Tasa" TasaOCuota="0.160000" Importe="320.00"/>
                    </cfdi:Traslados>
                </cfdi:Impuestos>
            </cfdi:Concepto>
        </cfdi:Conceptos>
        <cfdi:Impuestos TotalImpuestosTrasladados="320.00">
            <cfdi:Traslados>
                <cfdi:Traslado Base="2000.00" Impuesto="002" TipoFactor="Tasa" TasaOCuota="0.160000" Importe="320.00"/>
            </cfdi:Traslados>
        </cfdi:Impuestos>
        <cfdi:Complemento>
            <implocal:ImpuestosLocales version="1.0" TotaldeRetenciones="0.00" TotaldeTraslados="70.00">
                <implocal:TrasladosLocales ImpLocTrasladado="ISH" TasadeTraslado="3.50" Importe="70.00"/>
            </implocal:ImpuestosLocales>
        </cfdi:Complemento>
    </cfdi:Comprobante>
    """

    res = parse_cfdi_tax_breakdown(xml_sample)
    assert res["forma_pago"] == "04"
    assert res["base_16"] == Decimal("2000.00")
    assert res["iva_16"] == Decimal("320.00")
    assert res["ish"] == Decimal("70.00")
    assert "90111501" in res["claves_prod_serv"]

    analysis = analyze_fiscal_classification(
        comercio="HOTELES FIESTA AMERICANA",
        rfc_emisor="HFA900101AB1",
        xml_bytes=xml_sample.encode("utf-8"),
    )
    assert analysis["categoria"] == "hospedaje"
    assert analysis["desglose_impuestos"]["ish"] == 70.0
    assert analysis["desglose_impuestos"]["iva_16"] == 320.0
    assert analysis["estatus_deducibilidad"] == "deducible_100"


@pytest.mark.asyncio
async def test_upload_cfdi_direct_endpoint():
    import uuid
    from src.config import get_settings
    settings = get_settings()

    xml_sample = """<?xml version="1.0" encoding="UTF-8"?>
    <cfdi:Comprobante xmlns:cfdi="http://www.sat.gob.mx/cfd/4"
        xmlns:tfd="http://www.sat.gob.mx/TimbreFiscalDigital"
        Version="4.0" Serie="GAS" Folio="55201" Fecha="2026-09-14T15:00:00"
        FormaPago="04" MetodoPago="PUE" SubTotal="862.07" Total="1000.00">
        <cfdi:Emisor Rfc="GME980101AA1" Nombre="GASOLINERA MEXICANA SA DE CV" RegimenFiscal="601"/>
        <cfdi:Receptor Rfc="XAXX010101000" Nombre="CLIENTE PRUEBA" UsoCFDI="G03"/>
        <cfdi:Conceptos>
            <cfdi:Concepto ClaveProdServ="15101514" Cantidad="40.0" ClaveUnidad="LTR" Descripcion="Gasolina Magna Regular menor a 91 octanos" ValorUnitario="21.55" Importe="862.07">
                <cfdi:Impuestos>
                    <cfdi:Traslados>
                        <cfdi:Traslado Base="862.07" Impuesto="002" TipoFactor="Tasa" TasaOCuota="0.160000" Importe="137.93"/>
                    </cfdi:Traslados>
                </cfdi:Impuestos>
            </cfdi:Concepto>
        </cfdi:Conceptos>
        <cfdi:Impuestos TotalImpuestosTrasladados="137.93">
            <cfdi:Traslados>
                <cfdi:Traslado Base="862.07" Impuesto="002" TipoFactor="Tasa" TasaOCuota="0.160000" Importe="137.93"/>
            </cfdi:Traslados>
        </cfdi:Impuestos>
        <cfdi:Complemento>
            <tfd:TimbreFiscalDigital UUID="550e8400-e29b-41d4-a716-446655440000" FechaTimbrado="2026-09-14T15:01:00"/>
        </cfdi:Complemento>
    </cfdi:Comprobante>
    """

    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        unique_email = f"fiscal.{uuid.uuid4().hex[:8]}@test.com"
        log_res = await client.post("/v1/auth/google", json={"id_token": f"mock:{unique_email}"})
        cookie = log_res.cookies.get(settings.SESSION_COOKIE_NAME)
        tenant_id = log_res.json()["created_tenant_id"]

    async with AsyncClient(transport=transport, base_url="http://test", cookies={settings.SESSION_COOKIE_NAME: cookie}) as auth_client:
        files = {
            "xml_file": ("factura_gasolina.xml", xml_sample.encode("utf-8"), "application/xml"),
        }

        res = await auth_client.post(
            "/v1/tickets/upload-cfdi",
            files=files,
            headers={"X-Tenant-Id": tenant_id},
        )
        assert res.status_code == 201
        data = res.json()
        assert data["estado"] == "facturado"
        assert data["categoria_gasto"] == "combustible"
        assert data["estatus_deducibilidad"] == "deducible_100"
        assert data["cfdi_uuid"] == "550e8400-e29b-41d4-a716-446655440000"
        assert data["desglose_impuestos"]["iva_16"] == 137.93

        # Verificar filtrado por categoría en list_tickets
        list_res = await auth_client.get(
            "/v1/tickets?categoria=combustible",
            headers={"X-Tenant-Id": tenant_id},
        )
        assert list_res.status_code == 200
        items = list_res.json()["items"]
        assert any(it["id"] == data["id"] for it in items)
