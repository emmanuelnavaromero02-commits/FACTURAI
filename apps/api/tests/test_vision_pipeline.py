"""
Pruebas de la preparación de imágenes para el modelo de visión:
- sin filtros de contraste (borran la tinta térmica),
- recorte del ticket y tamaño según el modelo,
- elección de la URL de facturación entre QR e impresa.
"""
import base64
import io
import json
from unittest.mock import AsyncMock, patch

import httpx
import pytest
import zxingcpp
from PIL import Image, ImageDraw

from src.vision import extractor as extractor_mod
from src.vision.extractor import (
    AnthropicVisionExtractor,
    expand_and_validate_bbox,
    extract_qr_code,
    locate_ticket_bbox,
    max_image_edge_for_model,
    prepare_image_for_anthropic,
    scale_for_model,
)
from src.vision.url_sanitizer import choose_billing_url, looks_like_billing_url


def _jpeg_bytes(img: Image.Image) -> bytes:
    buf = io.BytesIO()
    img.save(buf, format="JPEG", quality=95)
    return buf.getvalue()


def _decode_b64_image(b64: str) -> Image.Image:
    return Image.open(io.BytesIO(base64.b64decode(b64)))


def _qr_image(text: str, scale: int = 6) -> Image.Image:
    zi = zxingcpp.create_barcode(text, zxingcpp.BarcodeFormat.QRCode).to_image(scale=scale)
    mv = memoryview(zi)
    return Image.frombytes("L", (mv.shape[1], mv.shape[0]), mv.tobytes()).convert("RGB")


def _mock_api_response(payload: dict, usage=(1000, 200)) -> httpx.Response:
    return httpx.Response(
        200,
        json={
            "content": [{"type": "text", "text": json.dumps(payload)}],
            "stop_reason": "end_turn",
            "usage": {"input_tokens": usage[0], "output_tokens": usage[1]},
        },
        request=httpx.Request("POST", "https://api.anthropic.com/v1/messages"),
    )


TICKET_JSON = {"comercio": "Tienda", "folio": "F-1", "total": "10.00", "confianza": 0.9, "otros": []}


def test_prepare_image_keeps_faint_thermal_ink():
    # Fondo de papel (215) con una franja de tinta tenue (185): el contraste debe conservarse tal cual
    img = Image.new("RGB", (600, 400), (215, 215, 215))
    ImageDraw.Draw(img).rectangle((100, 150, 500, 250), fill=(185, 185, 185))

    out = _decode_b64_image(prepare_image_for_anthropic(_jpeg_bytes(img))[0]).convert("L")
    papel = out.getpixel((50, 50))
    tinta = out.getpixel((300, 200))

    assert abs(papel - 215) <= 3
    assert abs(tinta - 185) <= 3


def test_max_image_edge_depends_on_model():
    assert max_image_edge_for_model("claude-sonnet-5") == 2576
    assert max_image_edge_for_model("claude-opus-5") == 2576
    assert max_image_edge_for_model("claude-fable-5-1") == 2576
    assert max_image_edge_for_model("claude-haiku-4-5") == 1568
    assert max_image_edge_for_model(None) == 1568


def test_scale_for_model_downscales_and_caps_upscale():
    assert scale_for_model(Image.new("RGB", (4000, 3000)), 2576).size == (2576, 1932)
    # Recorte de 300x900 ampliado hasta 2576 de alto
    assert max(scale_for_model(Image.new("RGB", (300, 900)), 2576, 3.0).size) == 2576
    # Recorte diminuto: la ampliación se detiene en 3x
    assert scale_for_model(Image.new("RGB", (100, 200)), 2576, 3.0).size == (300, 600)
    # Sin ampliación permitida, una imagen chica se queda igual
    assert scale_for_model(Image.new("RGB", (800, 600)), 2576).size == (800, 600)


def test_expand_and_validate_bbox():
    size = (1000, 1000)
    # Caja válida: se le agrega margen y se recorta a los bordes
    assert expand_and_validate_bbox((100, 100, 400, 900), size) == (76, 76, 424, 924)
    # Caja diminuta o que ocupa casi toda la imagen: se usa la imagen completa
    assert expand_and_validate_bbox((10, 10, 25, 25), size) is None
    assert expand_and_validate_bbox((0, 0, 990, 990), size) is None
    # Coordenadas invertidas o fuera de la imagen se normalizan
    assert expand_and_validate_bbox((400, 900, 100, 100), size) == (76, 76, 424, 924)


def test_choose_billing_url_prefers_billing_over_marketing_qr():
    marketing = "http://mansiondelcuate.com"
    impresa = "https://facturacion.parrot.rest/la-mansion-del-cuate-cdmx/C661609262048C"

    assert looks_like_billing_url(impresa)
    assert not looks_like_billing_url(marketing)
    # El QR publicitario no le gana a la URL de facturación impresa
    assert choose_billing_url(marketing, impresa) == impresa
    # Si el QR sí es de facturación, gana porque se lee sin errores de OCR
    assert choose_billing_url("https://factura.oxxo.com/abc", impresa) == "https://factura.oxxo.com/abc"
    # Sin pistas en ninguna, se conserva el comportamiento anterior (QR primero)
    assert choose_billing_url(marketing, "https://ejemplo.mx/x") == marketing
    assert choose_billing_url(None, impresa) == impresa
    assert choose_billing_url(None, None) is None


def test_extract_qr_code_picks_billing_qr_among_several():
    marketing = _qr_image("http://mansiondelcuate.com")
    billing = _qr_image("https://facturacion.parrot.rest/la-mansion/C661609262048C")
    lienzo = Image.new("RGB", (billing.width + 80, marketing.height + billing.height + 120), "white")
    # El QR publicitario va arriba: antes el lector se quedaba solo con el primero
    lienzo.paste(marketing, (40, 40))
    lienzo.paste(billing, (40, marketing.height + 80))

    assert extract_qr_code(_jpeg_bytes(lienzo)) == "https://facturacion.parrot.rest/la-mansion/C661609262048C"


@pytest.mark.asyncio
async def test_locate_ticket_bbox_scales_preview_coordinates_to_original():
    img = Image.new("RGB", (2048, 1024), (40, 90, 40))
    # La vista previa mide 1024x512: el modelo responde en esas coordenadas
    respuesta = _mock_api_response({"encontrado": True, "x0": 100, "y0": 50, "x1": 300, "y1": 450}, (900, 40))

    with patch("httpx.AsyncClient.post", new_callable=AsyncMock) as mock_post:
        mock_post.return_value = respuesta
        bbox, usage = await locate_ticket_bbox(img, "sk-ant-test", "claude-haiku-4-5")

    enviado = mock_post.call_args.kwargs["json"]
    vista = _decode_b64_image(enviado["messages"][0]["content"][0]["source"]["data"])
    assert vista.size == (1024, 512)
    assert enviado["temperature"] == 0.0
    # (200, 100, 600, 900) en la imagen original, más un margen de 24 px
    assert bbox == (176, 76, 624, 924)
    assert usage == {"input_tokens": 900, "output_tokens": 40}


@pytest.mark.asyncio
async def test_locate_ticket_bbox_falls_back_on_bad_answer():
    img = Image.new("RGB", (1200, 900), "gray")
    with patch("httpx.AsyncClient.post", new_callable=AsyncMock) as mock_post:
        mock_post.return_value = _mock_api_response({"encontrado": False})
        bbox, _ = await locate_ticket_bbox(img, "sk-ant-test", "claude-haiku-4-5")
    assert bbox is None

    with patch("httpx.AsyncClient.post", new_callable=AsyncMock) as mock_post:
        mock_post.side_effect = httpx.ConnectError("sin red")
        bbox, _ = await locate_ticket_bbox(img, "sk-ant-test", "claude-haiku-4-5")
    assert bbox is None


@pytest.mark.asyncio
async def test_extractor_sends_cropped_ticket_at_model_resolution(monkeypatch):
    foto = Image.new("RGB", (1000, 1000), (40, 90, 40))
    ImageDraw.Draw(foto).rectangle((100, 50, 400, 950), fill="white")

    async def fake_locate(img, api_key, model):
        return (100, 50, 400, 950), {"input_tokens": 800, "output_tokens": 30}

    monkeypatch.setattr(extractor_mod, "locate_ticket_bbox", fake_locate)
    extractor = AnthropicVisionExtractor(api_key="sk-ant-test", model="claude-sonnet-5", recortar_ticket=True)

    with patch("httpx.AsyncClient.post", new_callable=AsyncMock) as mock_post:
        mock_post.return_value = _mock_api_response(TICKET_JSON)
        schema, meta = await extractor.extract_with_usage(_jpeg_bytes(foto))

    enviado = mock_post.call_args.kwargs["json"]
    imagen = _decode_b64_image(enviado["messages"][0]["content"][0]["source"]["data"])
    # Solo el ticket (300x900), ampliado hasta 2576 px de alto para Sonnet 5
    assert max(imagen.size) == 2576
    assert abs(imagen.size[0] / imagen.size[1] - 300 / 900) < 0.01
    assert enviado["max_tokens"] >= 4096
    assert meta["recorte_ticket"] is True
    assert meta["recorte_bbox"] == [100, 50, 400, 950]
    assert schema.folio == "F-1"


@pytest.mark.asyncio
async def test_extractor_uses_full_image_when_ticket_not_located(monkeypatch):
    async def fake_locate(img, api_key, model):
        return None, {"input_tokens": 0, "output_tokens": 0}

    monkeypatch.setattr(extractor_mod, "locate_ticket_bbox", fake_locate)
    extractor = AnthropicVisionExtractor(api_key="sk-ant-test", model="claude-sonnet-5", recortar_ticket=True)

    with patch("httpx.AsyncClient.post", new_callable=AsyncMock) as mock_post:
        mock_post.return_value = _mock_api_response(TICKET_JSON)
        _, meta = await extractor.extract_with_usage(_jpeg_bytes(Image.new("RGB", (1000, 800), "white")))

    imagen = _decode_b64_image(mock_post.call_args.kwargs["json"]["messages"][0]["content"][0]["source"]["data"])
    # Sin recorte no se amplía: la imagen completa va a su tamaño original
    assert imagen.size == (1000, 800)
    assert meta["recorte_ticket"] is False


@pytest.mark.asyncio
async def test_extractor_reports_truncated_response():
    extractor = AnthropicVisionExtractor(api_key="sk-ant-test", model="claude-sonnet-5", recortar_ticket=False)
    truncada = httpx.Response(
        200,
        json={"content": [{"type": "thinking", "thinking": ""}], "stop_reason": "max_tokens",
              "usage": {"input_tokens": 10, "output_tokens": 4096}},
        request=httpx.Request("POST", "https://api.anthropic.com/v1/messages"),
    )
    with patch("httpx.AsyncClient.post", new_callable=AsyncMock) as mock_post:
        mock_post.return_value = truncada
        with pytest.raises(ValueError, match="se cortó"):
            await extractor.extract_with_usage(_jpeg_bytes(Image.new("RGB", (400, 400), "white")))
