"""
Catálogo de comercios oficiales de México para FacturAI.
Pobla y actualiza la tabla global 'merchants' con las cadenas comerciales más comunes,
sus portales oficiales de facturación y sus identificadores fiscales / patrones.
"""

import asyncio
import json
import logging
import uuid
from typing import Any, Dict, List
from sqlalchemy import text
from sqlalchemy.ext.asyncio import create_async_engine

from .config import get_settings

logger = logging.getLogger(__name__)

OFFICIAL_MERCHANTS: List[Dict[str, Any]] = [
    {
        "slug": "alsea",
        "nombre": "Grupo Alsea (Domino's Pizza, Starbucks, Vips, Burger King)",
        "patrones": [
            "dominos",
            "domino's",
            "domino's pizza",
            "starbucks",
            "starbucks coffee",
            "vips",
            "burger king",
            "italiannis",
            "italianni's",
            "chilis",
            "chili's",
            "p.f. chang",
            "cheesecake factory",
            "alsea",
            "operadora de franquicias alsea",
            "cafe sirena",
            "operadora vips",
            "OFA9210138U1",
            "CGI930623RH5",
            "OVI961128795",
            "BKM911204853",
            "ITI930607994",
            "RCH981105FA7",
            "dominos.com.mx",
            "starbucks.com.mx",
            "vips.com.mx",
        ],
        "tipo_motor": "web",
        "engine_slug": "generico-web",
        "config": {
            "url_facturacion": "https://alsea.interfactura.com",
            "portal_nombre": "Portal Unificado Alsea",
            "campos_requeridos": ["ticket", "tienda", "fecha", "total", "rfc"],
            "marcas": ["Domino's Pizza", "Starbucks", "Vips", "Burger King", "Italianni's", "Chili's"],
        },
        "requiere_captcha": False,
        "activo": True,
        "entrega_esperada": "emisor",
    },
    {
        "slug": "kfc-prb",
        "nombre": "KFC / Pizza Hut (Premium Restaurant Brands)",
        "patrones": [
            "kfc",
            "kentucky fried chicken",
            "pizza hut",
            "premium restaurant brands",
            "prb",
            "PRB100802H27",
            "kfc.com.mx",
            "pizzahut.com.mx",
        ],
        "tipo_motor": "web",
        "engine_slug": "generico-web",
        "config": {
            "url_facturacion": "https://facturacion.prb.com.mx",
            "portal_nombre": "Portal de Facturación PRB (KFC / Pizza Hut)",
            "campos_requeridos": ["folio", "tienda", "fecha", "total", "rfc"],
            "marcas": ["KFC", "Pizza Hut"],
        },
        "requiere_captcha": False,
        "activo": True,
        "entrega_esperada": "emisor",
    },
    {
        "slug": "toks-grg",
        "nombre": "Restaurantes Toks (Grupo Restaurantero Gigante)",
        "patrones": [
            "toks",
            "restaurantes toks",
            "panda express",
            "beer factory",
            "shake shack",
            "el farolito",
            "farolito",
            "grupo restaurantero gigante",
            "RTO840921RE4",
            "PEH101019688",
            "toks.com.mx",
        ],
        "tipo_motor": "web",
        "engine_slug": "generico-web",
        "config": {
            "url_facturacion": "https://facturacion.toks.com.mx",
            "portal_nombre": "Facturación Electrónica Toks / GRG",
            "campos_requeridos": ["folio", "fecha", "total", "rfc"],
            "marcas": ["Toks", "Panda Express", "Beer Factory", "Shake Shack", "El Farolito"],
        },
        "requiere_captcha": False,
        "activo": True,
        "entrega_esperada": "emisor",
    },
    {
        "slug": "cinemex",
        "nombre": "Cinemex (Operadora de Cinemas)",
        "patrones": [
            "cinemex",
            "operadora de cinemas",
            "cinemas",
            "OCI9312061T0",
            "cinemex.com",
        ],
        "tipo_motor": "web",
        "engine_slug": "generico-web",
        "config": {
            "url_facturacion": "https://facturacion.cinemex.com",
            "portal_nombre": "Portal de Facturación Cinemex",
            "campos_requeridos": ["transaccion", "complejo", "fecha", "total", "rfc"],
        },
        "requiere_captcha": False,
        "activo": True,
        "entrega_esperada": "emisor",
    },
    {
        "slug": "cinepolis",
        "nombre": "Cinépolis (Exhibidora Mexicana de Cinépolis)",
        "patrones": [
            "cinepolis",
            "cinépolis",
            "exhibidora mexicana de cinepolis",
            "EMC8407269J7",
            "cinepolis.com",
        ],
        "tipo_motor": "web",
        "engine_slug": "generico-web",
        "config": {
            "url_facturacion": "https://cinepolis.com/facturacion-electronica",
            "portal_nombre": "Portal de Facturación Cinépolis",
            "campos_requeridos": ["transaccion", "cine", "fecha", "total", "rfc"],
        },
        "requiere_captcha": False,
        "activo": True,
        "entrega_esperada": "emisor",
    },
    {
        "slug": "oxxo",
        "nombre": "Cadena Comercial OXXO",
        "patrones": [
            "oxxo",
            "cadena comercial oxxo",
            "tiendas oxxo",
            "CCO8605231N4",
            "oxxo.com",
        ],
        "tipo_motor": "web",
        "engine_slug": "generico-web",
        "config": {
            "url_facturacion": "https://www.oxxo.com/facturacion-electronica",
            "portal_nombre": "Portal Oficial OXXO Facturación",
            "campos_requeridos": ["folio_venta", "id_venta", "total", "rfc"],
        },
        "requiere_captcha": False,
        "activo": True,
        "entrega_esperada": "emisor",
    },
    {
        "slug": "walmart-mexico",
        "nombre": "Walmart / Bodega Aurrera / Sam's Club",
        "patrones": [
            "walmart",
            "bodega aurrera",
            "aurrera",
            "sams",
            "sam's club",
            "sams club",
            "superama",
            "walmart express",
            "nueva walmart de mexico",
            "NWM9709244W4",
            "walmart.com.mx",
        ],
        "tipo_motor": "web",
        "engine_slug": "generico-web",
        "config": {
            "url_facturacion": "https://facturacion.walmartmexico.com.mx",
            "portal_nombre": "Facturación Electrónica Walmart México",
            "campos_requeridos": ["tc", "tr", "tienda", "caja", "rfc"],
        },
        "requiere_captcha": False,
        "activo": True,
        "entrega_esperada": "emisor",
    },
    {
        "slug": "costco-mexico",
        "nombre": "Costco Wholesale México",
        "patrones": [
            "costco",
            "costco wholesale",
            "CWM910607H53",
            "costco.com.mx",
        ],
        "tipo_motor": "web",
        "engine_slug": "generico-web",
        "config": {
            "url_facturacion": "https://www.costco.com.mx/facturacion",
            "portal_nombre": "Facturación Costco México",
            "campos_requeridos": ["ticket", "sucursal", "rfc"],
        },
        "requiere_captcha": False,
        "activo": True,
        "entrega_esperada": "emisor",
    },
    {
        "slug": "soriana",
        "nombre": "Tiendas Soriana / City Club",
        "patrones": [
            "soriana",
            "tiendas soriana",
            "city club",
            "hipermart",
            "TSO991022PB6",
            "soriana.com",
        ],
        "tipo_motor": "web",
        "engine_slug": "generico-web",
        "config": {
            "url_facturacion": "https://www.soriana.com/facturacion.html",
            "portal_nombre": "Facturación Soriana",
            "campos_requeridos": ["folio", "tienda", "caja", "total", "rfc"],
        },
        "requiere_captcha": False,
        "activo": True,
        "entrega_esperada": "emisor",
    },
    {
        "slug": "chedraui",
        "nombre": "Tiendas Chedraui",
        "patrones": [
            "chedraui",
            "tiendas chedraui",
            "super chedraui",
            "selecto chedraui",
            "TCH850730TA0",
            "chedraui.com.mx",
        ],
        "tipo_motor": "web",
        "engine_slug": "generico-web",
        "config": {
            "url_facturacion": "https://facturacion.chedraui.com.mx",
            "portal_nombre": "Portal de Facturación Chedraui",
            "campos_requeridos": ["folio", "tienda", "total", "rfc"],
        },
        "requiere_captcha": False,
        "activo": True,
        "entrega_esperada": "emisor",
    },
    {
        "slug": "farmacias-guadalajara",
        "nombre": "Farmacias Guadalajara (Fragua)",
        "patrones": [
            "farmacias guadalajara",
            "guadalajara",
            "fragua",
            "corporativo fragua",
            "FGU830930PD3",
            "farmaciasguadalajara.com",
        ],
        "tipo_motor": "web",
        "engine_slug": "generico-web",
        "config": {
            "url_facturacion": "https://www.farmaciasguadalajara.com/facturacion",
            "portal_nombre": "Facturación Farmacias Guadalajara",
            "campos_requeridos": ["ticket", "tienda", "total", "rfc"],
        },
        "requiere_captcha": False,
        "activo": True,
        "entrega_esperada": "emisor",
    },
    {
        "slug": "farmacias-del-ahorro",
        "nombre": "Farmacias del Ahorro",
        "patrones": [
            "farmacias del ahorro",
            "del ahorro",
            "comercializadora farmaceutica de chiapas",
            "CFC9103076G7",
            "fahorro.com.mx",
        ],
        "tipo_motor": "web",
        "engine_slug": "generico-web",
        "config": {
            "url_facturacion": "https://facturacion.fahorro.com.mx",
            "portal_nombre": "Portal de Facturación Farmacias del Ahorro",
            "campos_requeridos": ["ticket", "rfc"],
        },
        "requiere_captcha": False,
        "activo": True,
        "entrega_esperada": "emisor",
    },
    {
        "slug": "sanborns-sears",
        "nombre": "Sanborns / Sears (Grupo Carso)",
        "patrones": [
            "sanborns",
            "sanborns hermanos",
            "sears",
            "sears operadora",
            "SHE190630V37",
            "SOP0011246J1",
            "sanborns.com.mx",
            "sears.com.mx",
        ],
        "tipo_motor": "web",
        "engine_slug": "generico-web",
        "config": {
            "url_facturacion": "https://facturacion.sanborns.com.mx",
            "portal_nombre": "Portal de Facturación Sanborns y Sears",
            "campos_requeridos": ["transaccion", "terminal", "tienda", "total", "rfc"],
        },
        "requiere_captcha": False,
        "activo": True,
        "entrega_esperada": "emisor",
    },
    {
        "slug": "liverpool-suburbia",
        "nombre": "Liverpool / Suburbia",
        "patrones": [
            "liverpool",
            "el puerto de liverpool",
            "suburbia",
            "EPL820616G69",
            "SUB9106045E6",
            "liverpool.com.mx",
            "suburbia.com.mx",
        ],
        "tipo_motor": "web",
        "engine_slug": "generico-web",
        "config": {
            "url_facturacion": "https://facturacion.liverpool.com.mx",
            "portal_nombre": "Facturación Liverpool y Suburbia",
            "campos_requeridos": ["codigo_facturacion", "rfc"],
        },
        "requiere_captcha": False,
        "activo": True,
        "entrega_esperada": "emisor",
    },
    {
        "slug": "oxxo-gas",
        "nombre": "OXXO Gas (Servicios Gasolineros de México)",
        "patrones": [
            "oxxo gas",
            "oxxogas",
            "servicios gasolineros de mexico",
            "SGM950714DC2",
            "oxxogas.com",
        ],
        "tipo_motor": "web",
        "engine_slug": "generico-web",
        "config": {
            "url_facturacion": "https://facturacion.oxxogas.com",
            "portal_nombre": "Portal de Facturación OXXO Gas",
            "campos_requeridos": ["folio", "estacion", "web_id", "total", "rfc"],
        },
        "requiere_captcha": False,
        "activo": True,
        "entrega_esperada": "emisor",
    },
]


async def seed_official_merchants(migration_url: str) -> int:
    """Inserta o actualiza los comercios oficiales en la tabla global 'merchants'."""
    engine = create_async_engine(migration_url)
    updated_count = 0
    try:
        async with engine.begin() as conn:
            for m in OFFICIAL_MERCHANTS:
                patrones_json = json.dumps(m["patrones"], ensure_ascii=False)
                config_json = json.dumps(m["config"], ensure_ascii=False)

                # Upsert basado en slug
                stmt = text("""
                    INSERT INTO merchants (
                        id, slug, nombre, patrones, tipo_motor, engine_slug,
                        config, requiere_captcha, activo, entrega_esperada, created_at, updated_at
                    ) VALUES (
                        :id, :slug, :nombre, CAST(:patrones AS jsonb), :tipo_motor, :engine_slug,
                        CAST(:config AS jsonb), :requiere_captcha, :activo, :entrega_esperada, NOW(), NOW()
                    )
                    ON CONFLICT (slug) DO UPDATE SET
                        nombre = EXCLUDED.nombre,
                        patrones = EXCLUDED.patrones,
                        config = EXCLUDED.config,
                        engine_slug = EXCLUDED.engine_slug,
                        entrega_esperada = EXCLUDED.entrega_esperada,
                        activo = EXCLUDED.activo,
                        updated_at = NOW();
                """)
                await conn.execute(
                    stmt,
                    {
                        "id": str(uuid.uuid4()),
                        "slug": m["slug"],
                        "nombre": m["nombre"],
                        "patrones": patrones_json,
                        "tipo_motor": m["tipo_motor"],
                        "engine_slug": m["engine_slug"],
                        "config": config_json,
                        "requiere_captcha": m["requiere_captcha"],
                        "activo": m["activo"],
                        "entrega_esperada": m["entrega_esperada"],
                    },
                )
                updated_count += 1
        logger.info("Catálogo oficial de comercios sembrado con éxito (%d cadenas registradas)", updated_count)
    finally:
        await engine.dispose()
    return updated_count


if __name__ == "__main__":
    settings = get_settings()
    url = settings.DATABASE_MIGRATION_URL
    asyncio.run(seed_official_merchants(url))
