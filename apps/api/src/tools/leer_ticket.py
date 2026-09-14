"""
Script de línea de comandos:
    uv run python -m src.tools.leer_ticket <ruta_a_la_foto>

Ejecuta la extracción real con Anthropic Vision sobre una fotografía de
ticket de compra tomada con celular, e imprime:
- JSON completo extraído
- Confianza del modelo
- Comercio identificado (vía BD o catálogo JSON opcional)
- Tokens usados (entrada, salida, total)
- Costo estimado de la llamada en USD (o 'costo no disponible')

Sin base de datos requerida, sin cola, sin tenant.
"""

import argparse
import asyncio
import json
import logging
import os
import sys
import uuid
from typing import List, Optional

# Silenciar logging verboso de SQLAlchemy para el CLI
logging.getLogger("sqlalchemy.engine").setLevel(logging.WARNING)
logging.getLogger("sqlalchemy.engine.Engine").setLevel(logging.WARNING)

from ..config import get_model_token_pricing, get_settings
from ..models import Merchant, TipoMotor
from ..vision.extractor import (
    AnthropicVisionExtractor,
    FakeVisionExtractor,
    extract_qr_code,
)
from ..vision.merchant_matcher import match_merchant_cascade


async def load_merchants(catalogo_path: Optional[str] = None) -> List[Merchant]:
    """
    Carga el catálogo de comercios:
    1. Si se pasó --catalogo, lee desde el archivo JSON proporcionado.
    2. Si no, intenta consultar los comercios activos en PostgreSQL.
    3. Si no hay base de datos disponible ni archivo, devuelve lista vacía.
    """
    if catalogo_path:
        if not os.path.exists(catalogo_path):
            print(f"Advertencia: El archivo de catálogo '{catalogo_path}' no existe.", file=sys.stderr)
            return []
        try:
            with open(catalogo_path, "r", encoding="utf-8") as f:
                data = json.load(f)
            merchants: List[Merchant] = []
            for item in data:
                m = Merchant(
                    id=uuid.UUID(item["id"]) if "id" in item else uuid.uuid4(),
                    slug=item["slug"],
                    nombre=item["nombre"],
                    patrones=item.get("patrones", []),
                    tipo_motor=TipoMotor(item.get("tipo_motor", "web")),
                    engine_slug=item.get("engine_slug", item["slug"]),
                    activo=item.get("activo", True),
                )
                merchants.append(m)
            return merchants
        except Exception as exc:
            print(f"Advertencia: Error al cargar catálogo JSON '{catalogo_path}': {exc}", file=sys.stderr)
            return []

    # Intentar leer desde PostgreSQL si la conexión está disponible
    try:
        from sqlalchemy import select
        from ..db import engine, AsyncSessionLocal

        orig_echo = engine.echo
        engine.echo = False
        try:
            async with AsyncSessionLocal() as session:
                stmt = select(Merchant).where(Merchant.activo == True)
                result = await session.execute(stmt)
                return list(result.scalars().all())
        finally:
            engine.echo = orig_echo
    except Exception:
        # Base de datos no disponible: continuar sin catálogo propio
        return []


async def run_ticket_extraction(
    ruta_imagen: str,
    api_key: Optional[str] = None,
    model: Optional[str] = None,
    catalogo_path: Optional[str] = None,
    use_mock: bool = False,
    json_only: bool = False,
) -> int:
    if not os.path.exists(ruta_imagen):
        print(f"Error: El archivo '{ruta_imagen}' no existe.", file=sys.stderr)
        return 1

    file_size = os.path.getsize(ruta_imagen)
    with open(ruta_imagen, "rb") as f:
        image_bytes = f.read()

    settings = get_settings()
    active_model = model or os.environ.get("ANTHROPIC_MODEL_VISION") or settings.ANTHROPIC_MODEL_VISION

    # 1. Intentar decodificar código QR
    qr_url = extract_qr_code(image_bytes)

    # 2. Extractor (Real Anthropic o Mock)
    if use_mock:
        extractor = FakeVisionExtractor()
        schema = await extractor.extract(image_bytes, model=active_model)
        rates = get_model_token_pricing(active_model)
        input_tokens = 1250
        output_tokens = 280
        cost_usd = (
            (input_tokens * rates["input"] / 1_000_000) + (output_tokens * rates["output"] / 1_000_000)
            if rates is not None
            else None
        )
        meta = {
            "model": active_model,
            "input_tokens": input_tokens,
            "output_tokens": output_tokens,
            "total_tokens": input_tokens + output_tokens,
            "cost_usd": cost_usd,
            "elapsed_seconds": 0.12,
            "media_type": "image/jpeg",
            "image_size_bytes": file_size,
        }
    else:
        try:
            extractor = AnthropicVisionExtractor(api_key=api_key, model=active_model)
            schema, meta = await extractor.extract_with_usage(image_bytes, model=active_model)
        except ValueError as val_err:
            print(f"\n[ERROR DE CONFIGURACIÓN] {val_err}\n", file=sys.stderr)
            print(
                "Para ejecutar la llamada real a Anthropic Claude, define tu clave:\n"
                "  export ANTHROPIC_API_KEY='sk-ant-api03-...'\n"
                "O pásala con --api-key:\n"
                "  uv run python -m src.tools.leer_ticket <ruta> --api-key 'sk-ant-api03-...'\n",
                file=sys.stderr,
            )
            return 1
        except Exception as exc:
            print(f"\n[ERROR DE EXTRACCIÓN CON ANTHROPIC] {exc}\n", file=sys.stderr)
            return 2

    # 3. Identificación del comercio: vía BD o catálogo JSON opcional
    merchants = await load_merchants(catalogo_path)
    matched_merchant = (
        match_merchant_cascade(
            merchants=merchants,
            qr_url=qr_url,
            printed_url=schema.url_facturacion,
            rfc_emisor=schema.rfc_emisor,
            comercio_nombre=schema.comercio,
        )
        if merchants
        else None
    )

    schema_dict = schema.model_dump()

    # Si se solicitó solo JSON
    if json_only:
        print(json.dumps(schema_dict, indent=2, ensure_ascii=False))
        return 0

    # 4. Formatear salida en consola
    conf_pct = schema.confianza * 100.0
    if conf_pct >= 80:
        conf_badge = f"{conf_pct:.1f}% (ALTA - Apto para facturación automática)"
    elif conf_pct >= 60:
        conf_badge = f"{conf_pct:.1f}% (MEDIA - Requiere validación de folio/total)"
    else:
        conf_badge = f"{conf_pct:.1f}% (BAJA - Ticket borroso o datos faltantes)"

    if matched_merchant:
        comercio_str = f"{matched_merchant.nombre} (slug: '{matched_merchant.slug}')"
    else:
        comercio_str = "Comercio no identificado"

    cost_usd = meta.get("cost_usd")
    if cost_usd is not None:
        cost_mxn = cost_usd * 20.0
        cost_str = f"${cost_usd:.5f} USD (~${cost_mxn:.3f} MXN)"
    else:
        cost_str = "costo no disponible"

    print("\n" + "=" * 64)
    print("       FACTURAI · EXTRACCIÓN DE TICKET CON VISIÓN (ANTHROPIC)")
    print("=" * 64)
    print(f"Archivo analizado     : {ruta_imagen}")
    print(f"Tamaño de imagen      : {file_size / 1024:.1f} KB")
    print(f"Código QR detectado   : {qr_url or 'Ninguno'}")
    print(f"Comercio identificado : {comercio_str}")
    print(f"Confianza global      : {conf_badge}")
    print("-" * 64)
    print("METADATOS DE LA LLAMADA:")
    print(f"  Modelo Anthropic    : {meta['model']}")
    print(f"  Tokens de entrada   : {meta['input_tokens']:,}")
    print(f"  Tokens de salida    : {meta['output_tokens']:,}")
    print(f"  Tokens totales      : {meta['total_tokens']:,}")
    print(f"  Costo estimado      : {cost_str}")
    print(f"  Tiempo de respuesta : {meta['elapsed_seconds']} s")
    print("-" * 64)
    print("DATOS EXTRAÍDOS (JSON COMPLETO):")
    print(json.dumps(schema_dict, indent=2, ensure_ascii=False))
    print("-" * 64)
    print("RESUMEN DE CAMPOS CLAVE:")
    print(f"  Comercio impreso    : {schema.comercio or '(no detectado)'}")
    print(f"  RFC Emisor          : {schema.rfc_emisor or '(no detectado)'}")
    print(f"  Folio del ticket    : {schema.folio or '(no detectado)'}")
    print(f"  Web ID / Referencia : {schema.web_id or '(no detectado)'}")
    print(f"  Fecha de compra     : {schema.fecha or '(no detectada)'}")
    print(f"  Hora de compra      : {schema.hora or '(no detectada)'}")
    print(f"  Total pagado        : {schema.total or '(no detectado)'}")
    print(f"  Subtotal            : {schema.subtotal or '(no detectado)'}")
    print(f"  IVA                 : {schema.iva or '(no detectado)'}")
    print(f"  Sucursal / Caja     : {schema.sucursal or '-'} / {schema.caja or '-'}")
    print(f"  URL Facturación     : {schema.url_facturacion or '(no detectada)'}")
    print("=" * 64 + "\n")

    return 0


def build_arg_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="uv run python -m src.tools.leer_ticket",
        description="Extrae datos de un ticket de compra usando Anthropic Claude Vision sin BD ni cola.",
    )
    parser.add_argument("ruta_a_la_foto", help="Ruta local al archivo de imagen (JPEG, PNG, HEIC, etc.)")
    parser.add_argument("--api-key", default=None, help="Clave de API de Anthropic (opcional, fallback a ANTHROPIC_API_KEY)")
    parser.add_argument("--model", default=None, help="Modelo de Anthropic a utilizar (opcional, fallback a ANTHROPIC_MODEL_VISION en config.py)")
    parser.add_argument("--catalogo", default=None, help="Ruta a archivo JSON con catálogo de comercios si no hay conexión a base de datos")
    parser.add_argument("--mock", action="store_true", help="Usar extractor simulado sin llamar a la API real")
    parser.add_argument("--json-only", action="store_true", help="Imprimir únicamente el JSON extraído")
    return parser


def main():
    parser = build_arg_parser()
    args = parser.parse_args()

    exit_code = asyncio.run(
        run_ticket_extraction(
            ruta_imagen=args.ruta_a_la_foto,
            api_key=args.api_key,
            model=args.model,
            catalogo_path=args.catalogo,
            use_mock=args.mock,
            json_only=args.json_only,
        )
    )
    sys.exit(exit_code)


if __name__ == "__main__":
    main()
