import json
import logging
import re
import uuid
from typing import Any, Dict, List, Optional
from urllib.parse import urlparse
from sqlalchemy import text
from sqlalchemy.ext.asyncio import create_async_engine

from ..config import get_settings
from ..models import Ticket

import unicodedata

logger = logging.getLogger(__name__)


def slugify(text: str) -> str:
    """Convierte texto en un slug alfanumérico limpio sin acentos ni caracteres especiales."""
    text = unicodedata.normalize("NFKD", text).encode("ascii", "ignore").decode("utf-8")
    text = text.lower().strip()
    text = re.sub(r"[^\w\s-]", "", text)
    text = re.sub(r"[\s_-]+", "-", text)
    return text.strip("-") or "comercio"


async def extract_portal_recipe_from_page(page: Any, ticket: Ticket) -> Dict[str, Any]:
    """
    Inspecciona activamente el DOM del portal de facturación cargado para
    aprender la estructura del formulario, campos y selectores.
    """
    recipe: Dict[str, Any] = {
        "url_facturacion": page.url,
        "campos_detectados": [],
        "selectores": {},
        "reglas": {},
    }

    try:
        inputs = await page.eval_on_selector_all(
            "input:visible, select:visible",
            """els => els.map(e => ({
                tag: e.tagName.toLowerCase(),
                id: e.id || '',
                name: e.name || '',
                type: e.type || '',
                placeholder: e.placeholder || '',
                maxlength: e.maxLength > 0 ? e.maxLength : null
            }))"""
        )

        for inp in inputs:
            desc = f"#{inp['id']}" if inp['id'] else f"[name='{inp['name']}']"
            ph_lower = (inp['placeholder'] or "").lower()
            name_lower = (inp['name'] or inp['id'] or "").lower()

            if any(k in name_lower or k in ph_lower for k in ("ticket", "folio", "orden", "transaccion")):
                if "ticket" not in recipe["campos_detectados"]:
                    recipe["campos_detectados"].append("ticket")
                recipe["selectores"]["ticket"] = desc
                if inp.get("maxlength"):
                    recipe["reglas"]["ticket_maxlength"] = inp["maxlength"]
            elif any(k in name_lower or k in ph_lower for k in ("tienda", "sucursal")):
                if "tienda" not in recipe["campos_detectados"]:
                    recipe["campos_detectados"].append("tienda")
                recipe["selectores"]["tienda"] = desc
                if inp.get("maxlength"):
                    recipe["reglas"]["tienda_maxlength"] = inp["maxlength"]
            elif any(k in name_lower or k in ph_lower for k in ("fecha", "consumo", "date")):
                if "fecha" not in recipe["campos_detectados"]:
                    recipe["campos_detectados"].append("fecha")
                recipe["selectores"]["fecha"] = desc
                if "dd/mm/aaaa" in ph_lower or "dd-mm-yyyy" in ph_lower:
                    recipe["reglas"]["fecha_format"] = "dd/mm/aaaa"
                elif "yyyy-mm-dd" in ph_lower or "aaaa-mm-dd" in ph_lower:
                    recipe["reglas"]["fecha_format"] = "aaaa-mm-dd"
            elif any(k in name_lower or k in ph_lower for k in ("total", "importe", "monto")):
                if "total" not in recipe["campos_detectados"]:
                    recipe["campos_detectados"].append("total")
                recipe["selectores"]["total"] = desc
            elif any(k in name_lower or k in ph_lower for k in ("rfc",)):
                if "rfc" not in recipe["campos_detectados"]:
                    recipe["campos_detectados"].append("rfc")
                recipe["selectores"]["rfc"] = desc

        # Portales multimarca (ej. Alsea / PRB)
        if "alsea.interfactura.com" in page.url.lower():
            recipe["reglas"]["es_alsea"] = True
            recipe["reglas"]["ticket_digits"] = 9
            recipe["reglas"]["tienda_digits"] = 5
            comercio = ((ticket.extracted.get("comercio") if ticket.extracted else "") or ticket.sucursal or "").lower()
            if "domino" in comercio or ticket.rfc_emisor == "OPP010927SA5":
                recipe["selectores"]["brand"] = "img[src*='logo_dominos']"
                recipe["reglas"]["marca"] = "Domino's Pizza"

    except Exception as exc:
        logger.debug("No se pudo inspeccionar completamente el DOM del portal: %s", exc)

    return recipe


async def learn_merchant_recipe(
    ticket: Ticket,
    portal_url: str,
    form_recipe: Optional[Dict[str, Any]] = None,
) -> bool:
    """
    Persiste o actualiza la receta del comercio en el catálogo global 'merchants'.
    Permite que futuros tickets del mismo RFC o nombre utilicen la receta aprendida
    en un solo paso directo.
    """
    settings = get_settings()
    migration_url = settings.DATABASE_MIGRATION_URL or settings.DATABASE_URL
    if not migration_url:
        return False

    comercio_nom = None
    if ticket.extracted and isinstance(ticket.extracted, dict):
        comercio_nom = ticket.extracted.get("comercio")
    comercio_nom = comercio_nom or ticket.sucursal or "Comercio"

    rfc_emisor = (ticket.rfc_emisor or "").strip().upper()
    parsed = urlparse(portal_url)
    domain_host = (parsed.netloc or "").split(":")[0].lower()

    slug = slugify(comercio_nom)[:60]

    config_data = form_recipe or {}
    config_data["url_facturacion"] = portal_url
    config_data["aprendido_automaticamente"] = True

    patrones = [p for p in [rfc_emisor, comercio_nom.lower(), domain_host] if p]

    engine = create_async_engine(migration_url)
    try:
        async with engine.begin() as conn:
            # 1. Buscar si ya existe un comercio con este RFC o slug
            existing = None
            if rfc_emisor:
                res = await conn.execute(
                    text("SELECT id, slug, config, patrones FROM merchants WHERE patrones::text ILIKE :rfc LIMIT 1"),
                    {"rfc": f"%{rfc_emisor}%"}
                )
                existing = res.mappings().first()

            if not existing:
                res = await conn.execute(
                    text("SELECT id, slug, config, patrones FROM merchants WHERE slug = :slug LIMIT 1"),
                    {"slug": slug}
                )
                existing = res.mappings().first()

            if existing:
                # Merge config y patrones
                m_id = existing["id"]
                current_config = existing["config"] if isinstance(existing["config"], dict) else {}
                current_patrones = existing["patrones"] if isinstance(existing["patrones"], list) else []

                current_config.update(config_data)
                for p in patrones:
                    if p not in current_patrones:
                        current_patrones.append(p)

                await conn.execute(
                    text("""
                        UPDATE merchants 
                        SET config = CAST(:config AS jsonb),
                            patrones = CAST(:patrones AS jsonb),
                            updated_at = NOW()
                        WHERE id = :id
                    """),
                    {
                        "id": m_id,
                        "config": json.dumps(current_config, ensure_ascii=False),
                        "patrones": json.dumps(current_patrones, ensure_ascii=False),
                    }
                )
                logger.info("Receta de facturación actualizada para comercio %s (id: %s)", existing["slug"], m_id)
            else:
                # Crear nuevo comercio auto-aprendido
                new_id = str(uuid.uuid4())
                await conn.execute(
                    text("""
                        INSERT INTO merchants (
                            id, slug, nombre, patrones, tipo_motor, engine_slug,
                            config, requiere_captcha, activo, entrega_esperada, created_at, updated_at
                        ) VALUES (
                            :id, :slug, :nombre, CAST(:patrones AS jsonb), 'web', 'generico-web',
                            CAST(:config AS jsonb), false, true, 'emisor', NOW(), NOW()
                        )
                    """),
                    {
                        "id": new_id,
                        "slug": slug,
                        "nombre": comercio_nom,
                        "patrones": json.dumps(patrones, ensure_ascii=False),
                        "config": json.dumps(config_data, ensure_ascii=False),
                    }
                )
                logger.info("Nuevo comercio auto-aprendido registrado en catálogo: %s (%s)", comercio_nom, slug)

        return True
    except Exception as exc:
        logger.warning("Error al persistir receta auto-aprendida del comercio %s: %s", comercio_nom, exc)
        return False
    finally:
        await engine.dispose()
