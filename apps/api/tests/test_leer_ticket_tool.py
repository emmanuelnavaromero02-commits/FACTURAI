import io
import json
from unittest.mock import AsyncMock, patch
from PIL import Image
import pytest

from src.config import get_settings
from src.models import Merchant, TipoMotor
from src.vision.extractor import (
    AnthropicVisionExtractor,
    VisionExtractionSchema,
    prepare_image_for_anthropic,
)
from src.tools.leer_ticket import (
    build_arg_parser,
    load_merchants,
    run_ticket_extraction,
)
from src.vision.merchant_matcher import match_merchant_cascade


@pytest.fixture
def sample_image_path(tmp_path):
    img_path = tmp_path / "ticket_sample.jpg"
    img = Image.new("RGB", (200, 400), color="white")
    img.save(img_path, format="JPEG")
    return str(img_path)


@pytest.mark.asyncio
async def test_leer_ticket_mock_execution(sample_image_path, capsys):
    ret = await run_ticket_extraction(
        ruta_imagen=sample_image_path,
        use_mock=True,
        json_only=False,
    )
    assert ret == 0

    captured = capsys.readouterr()
    assert "FACTURAI · EXTRACCIÓN DE TICKET CON VISIÓN" in captured.out
    assert "4821-993-0077" in captured.out
    assert "Tokens de entrada" in captured.out
    assert "costo no disponible" in captured.out


@pytest.mark.asyncio
async def test_leer_ticket_mock_json_only(sample_image_path, capsys):
    ret = await run_ticket_extraction(
        ruta_imagen=sample_image_path,
        use_mock=True,
        json_only=True,
    )
    assert ret == 0

    captured = capsys.readouterr()
    parsed = json.loads(captured.out.strip())
    assert parsed["comercio"] == "Oxxo Comercial"
    assert parsed["folio"] == "4821-993-0077"
    assert parsed["confianza"] == 0.95


@pytest.mark.asyncio
async def test_leer_ticket_file_not_found(capsys):
    ret = await run_ticket_extraction(
        ruta_imagen="/ruta/inexistente/no_existe.jpg",
        use_mock=True,
    )
    assert ret == 1
    captured = capsys.readouterr()
    assert "no existe" in captured.err.lower()


@pytest.mark.asyncio
async def test_leer_ticket_missing_api_key(sample_image_path, capsys, monkeypatch):
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    with patch("src.vision.extractor.get_settings") as mock_settings:
        mock_settings.return_value.ANTHROPIC_API_KEY = None
        mock_settings.return_value.ANTHROPIC_MODEL_VISION = "claude-opus-5"
        ret = await run_ticket_extraction(
            ruta_imagen=sample_image_path,
            api_key=None,
            use_mock=False,
        )
    assert ret == 1
    captured = capsys.readouterr()
    assert "ERROR DE CONFIGURACIÓN" in captured.err
    assert "ANTHROPIC_API_KEY" in captured.err


def test_prepare_image_for_anthropic_scaling_to_1568():
    # Imagen de 3000 x 4000 px -> debe reescalarse a max 1568px de lado mayor
    huge_img = Image.new("RGBA", (3000, 4000), color=(255, 0, 0, 255))
    buf = io.BytesIO()
    huge_img.save(buf, format="PNG")
    raw_bytes = buf.getvalue()

    b64_str, media_type = prepare_image_for_anthropic(raw_bytes)
    assert media_type == "image/jpeg"
    assert len(b64_str) > 0

    # Decodificar imagen resultante para verificar dimensiones máximas
    import base64
    decoded_bytes = base64.b64decode(b64_str)
    result_img = Image.open(io.BytesIO(decoded_bytes))
    assert max(result_img.size) == 1568


@pytest.mark.asyncio
async def test_anthropic_extractor_with_env_pricing(monkeypatch):
    import httpx

    # Precios definidos por variables de entorno según requerimiento del producto
    monkeypatch.setenv("ANTHROPIC_PRECIO_IN_CLAUDE_OPUS_5", "15.00")
    monkeypatch.setenv("ANTHROPIC_PRECIO_OUT_CLAUDE_OPUS_5", "75.00")

    settings = get_settings()
    extractor = AnthropicVisionExtractor(
        api_key="sk-ant-test-key",
        model=settings.ANTHROPIC_MODEL_VISION,
    )

    mock_response_data = {
        "content": [
            {
                "type": "text",
                "text": json.dumps(
                    {
                        "comercio": "Tienda Ejemplo",
                        "url_facturacion": "https://factura.ejemplo.mx",
                        "folio": "F-001",
                        "fecha": "14/09/2026",
                        "hora": "12:30",
                        "total": "100.00",
                        "confianza": 0.98,
                        "otros": [],
                    }
                ),
            }
        ],
        "usage": {
            "input_tokens": 1000,
            "output_tokens": 200,
        },
    }

    dummy_img = Image.new("RGB", (100, 100), color="white")
    buf = io.BytesIO()
    dummy_img.save(buf, format="JPEG")
    img_bytes = buf.getvalue()

    mock_resp = httpx.Response(
        200,
        json=mock_response_data,
        request=httpx.Request("POST", "https://api.anthropic.com/v1/messages"),
    )

    with patch("httpx.AsyncClient.post", new_callable=AsyncMock) as mock_post:
        mock_post.return_value = mock_resp
        schema, meta = await extractor.extract_with_usage(img_bytes)

        assert schema.comercio == "Tienda Ejemplo"
        assert meta["input_tokens"] == 1000
        assert meta["output_tokens"] == 200
        assert meta["cost_usd"] is not None
        # 1000 * 15 / 1,000,000 + 200 * 75 / 1,000,000 = 0.015 + 0.015 = 0.03 USD
        assert abs(meta["cost_usd"] - 0.03) < 1e-6


@pytest.mark.asyncio
async def test_anthropic_extractor_with_unconfigured_pricing_cost_unavailable(monkeypatch):
    import httpx

    # Garantizar que no hay variables de entorno para este modelo
    monkeypatch.delenv("ANTHROPIC_PRECIO_IN_CLAUDE_OPUS_5", raising=False)
    monkeypatch.delenv("ANTHROPIC_PRECIO_OUT_CLAUDE_OPUS_5", raising=False)

    extractor = AnthropicVisionExtractor(
        api_key="sk-ant-test-key",
        model="claude-opus-5",
    )

    mock_response_data = {
        "content": [
            {
                "type": "text",
                "text": json.dumps(
                    {
                        "comercio": "Tienda Test",
                        "folio": "F-002",
                        "confianza": 0.90,
                        "otros": [],
                    }
                ),
            }
        ],
        "usage": {
            "input_tokens": 500,
            "output_tokens": 100,
        },
    }

    dummy_img = Image.new("RGB", (100, 100), color="white")
    buf = io.BytesIO()
    dummy_img.save(buf, format="JPEG")
    img_bytes = buf.getvalue()

    mock_resp = httpx.Response(
        200,
        json=mock_response_data,
        request=httpx.Request("POST", "https://api.anthropic.com/v1/messages"),
    )

    with patch("httpx.AsyncClient.post", new_callable=AsyncMock) as mock_post:
        mock_post.return_value = mock_resp
        schema, meta = await extractor.extract_with_usage(img_bytes)

        assert meta["cost_usd"] is None


@pytest.mark.asyncio
async def test_load_merchants_from_json_catalog(tmp_path):
    cat_file = tmp_path / "comercios.json"
    catalog_data = [
        {
            "slug": "tienda-real",
            "nombre": "Tienda Real de México",
            "patrones": ["tienda real", "tiendareal.mx"],
            "tipo_motor": "web",
        }
    ]
    cat_file.write_text(json.dumps(catalog_data), encoding="utf-8")

    merchants = await load_merchants(catalogo_path=str(cat_file))
    assert len(merchants) == 1
    assert merchants[0].slug == "tienda-real"
    assert merchants[0].nombre == "Tienda Real de México"

    # Matching en cascada con el catálogo cargado del archivo JSON
    matched = match_merchant_cascade(merchants, comercio_nombre="Tienda Real Sucursal Centro")
    assert matched is not None
    assert matched.slug == "tienda-real"

    # Comercio no contemplado en el catálogo
    matched_unknown = match_merchant_cascade(merchants, comercio_nombre="Ferretería García")
    assert matched_unknown is None


def test_cli_arg_parser_defaults_and_options():
    parser = build_arg_parser()
    # Sin especificar --model, debe ser None para que tome ANTHROPIC_MODEL_VISION de config.py
    args = parser.parse_args(["/tmp/foto.heic"])
    assert args.ruta_a_la_foto == "/tmp/foto.heic"
    assert args.model is None
    assert args.api_key is None
    assert args.catalogo is None
    assert args.mock is False
    assert args.json_only is False

    # Con opciones explícitas
    args_custom = parser.parse_args([
        "/tmp/foto.png",
        "--model", "claude-sonnet-5",
        "--catalogo", "/tmp/cat.json",
        "--api-key", "sk-custom",
        "--json-only",
    ])
    assert args_custom.model == "claude-sonnet-5"
    assert args_custom.catalogo == "/tmp/cat.json"
    assert args_custom.api_key == "sk-custom"
    assert args_custom.json_only is True


def test_leer_ticket_subprocess_with_exported_pricing_env(tmp_path):
    import os
    import subprocess
    import sys

    # Crear imagen temporal para la prueba
    img_path = tmp_path / "ticket_subprocess.jpg"
    img = Image.new("RGB", (100, 100), color="white")
    img.save(img_path, format="JPEG")

    # Entorno con variables de precio exportadas
    env = os.environ.copy()
    env["ANTHROPIC_PRECIO_IN_CLAUDE_OPUS_5"] = "15.00"
    env["ANTHROPIC_PRECIO_OUT_CLAUDE_OPUS_5"] = "75.00"

    res = subprocess.run(
        [sys.executable, "-m", "src.tools.leer_ticket", str(img_path), "--mock"],
        env=env,
        capture_output=True,
        text=True,
    )
    assert res.returncode == 0
    assert "$0.03975 USD" in res.stdout
    assert "claude-opus-5" in res.stdout


def test_leer_ticket_subprocess_without_pricing_env_cost_unavailable(tmp_path):
    import os
    import subprocess
    import sys

    img_path = tmp_path / "ticket_subprocess.jpg"
    img = Image.new("RGB", (100, 100), color="white")
    img.save(img_path, format="JPEG")

    env = os.environ.copy()
    env.pop("ANTHROPIC_PRECIO_IN_CLAUDE_OPUS_5", None)
    env.pop("ANTHROPIC_PRECIO_OUT_CLAUDE_OPUS_5", None)

    res = subprocess.run(
        [sys.executable, "-m", "src.tools.leer_ticket", str(img_path), "--mock"],
        env=env,
        capture_output=True,
        text=True,
    )
    assert res.returncode == 0
    assert "costo no disponible" in res.stdout

