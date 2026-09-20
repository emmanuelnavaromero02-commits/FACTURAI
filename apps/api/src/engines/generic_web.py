import asyncio
import base64
from decimal import Decimal
import io
import json
import logging
import os
import shutil
import tempfile
import time
import uuid
from typing import Any, Dict, List, Optional

from PIL import Image
from playwright.async_api import async_playwright, BrowserContext, Page, Download

from .. import config
from ..config import get_settings
from ..models import TipoMotor
from ..storage import get_storage_service
from .base import (
    EngineContext,
    EngineResult,
    FacturacionEngine,
    HandoffConcurrenciaExcedidaException,
    register_engine,
)
from .agent_brain import AgentBrain, AnthropicAgentBrain, BrowserAction
from .captcha_solver import detect_interactive_captcha, try_solve_captcha_autonomously
from .stealth_utils import (
    apply_stealth,
    CHROMIUM_STEALTH_ARGS,
    DEFAULT_STEALTH_VIEWPORT,
    DEFAULT_STEALTH_USER_AGENT,
    DEFAULT_STEALTH_EXTRA_HEADERS,
)
from ..services.merchant_learner import extract_portal_recipe_from_page, learn_merchant_recipe
from ..services.portal_searcher import search_candidate_portal_urls, verify_portal_matches_ticket
from ..vision.url_sanitizer import choose_start_url, sanitize_and_classify_billing_url

logger = logging.getLogger(__name__)


def _merchant_config(ctx: EngineContext) -> Dict[str, Any]:
    """Configuración del comercio del catálogo, o un dict vacío si no hay comercio."""
    config_comercio = ctx.merchant.config if ctx.merchant else None
    return config_comercio if isinstance(config_comercio, dict) else {}


@register_engine("generico-web")
class GenericWebEngine(FacturacionEngine):
    """
    Motor Genérico Web de Facturación Automática (Paso B).
    Navega el portal de facturación mediante Playwright y utiliza un agente de visión
    con ventana deslizante, lotes con aborto temprano y envío irreversible único.
    """

    slug = "generico-web"
    tipo = TipoMotor.WEB

    def __init__(self, brain: Optional[AgentBrain] = None):
        self.brain = brain or AnthropicAgentBrain()

    async def resolve_start_url(self, ctx: EngineContext) -> Optional[str]:
        """
        URL con la que arranca el motor.
        La del ticket va primero: ahí quedan el portal alternativo que eligió el worker tras un fallo
        y el portal que el usuario indicó a mano. La del catálogo del comercio es el respaldo.
        Se salta cualquier URL cuyo dominio no existe, para no gastar un intento en un portal muerto.
        """
        merchant_url = _merchant_config(ctx).get("url_facturacion")
        return await choose_start_url(ctx.ticket.url_facturacion, merchant_url)

    async def facturar(self, ctx: EngineContext) -> EngineResult:
        settings = get_settings()
        ticket = ctx.ticket
        perfil = ctx.perfil_fiscal
        storage = get_storage_service()

        start_time = time.time()
        merchant_config = _merchant_config(ctx)
        raw_url = await self.resolve_start_url(ctx)

        # Si la URL es un hub genérico de red o ticket de gasolinera, refinar al portal exacto de la estación
        is_hub = bool(raw_url and any(h in raw_url.lower() for h in ("g500network.com", "efectifactura.com")))
        if is_hub or (ticket.extracted and any(any(w in str(x.get("etiqueta", "")).lower() for w in ("cre", "estacion", "dispensario", "combustible")) for x in ticket.extracted.get("otros", []))):
            from ..services.portal_searcher import deduce_portal_for_ticket
            refined_url = await deduce_portal_for_ticket(
                comercio=ticket.extracted.get("comercio") if ticket.extracted else None,
                rfc_emisor=ticket.rfc_emisor,
                sucursal=ticket.sucursal,
                current_url=raw_url,
                extracted_data=ticket.extracted,
            )
            if refined_url:
                raw_url = refined_url

        if not raw_url:
            return EngineResult(
                ok=False,
                error_code="sin_url_facturacion",
                mensaje="El ticket no cuenta con una URL de facturación válida.",
                reintentable=False,
            )
        url = sanitize_and_classify_billing_url(raw_url) or raw_url
        if hasattr(ticket, "url_facturacion") and ticket.url_facturacion != url:
            ticket.url_facturacion = url

        # 1. Configuración de precios y advertencia si no están definidos
        model_name = settings.ANTHROPIC_MODEL_AGENTE
        pricing = config.get_model_token_pricing(model_name)
        if pricing is None:
            logger.warning(
                "ADVERTENCIA DE CONFIGURACIÓN: No hay variables de precio configuradas "
                "para el modelo %s. El agente operará solo con límite de pasos (%d pasos) "
                "y costo_usd será None.",
                model_name,
                settings.PASOS_MAXIMOS_AGENTE,
            )

        # 2. Directorio temporal propio del proceso para descargas efímeras (Regla 4)
        temp_dir = tempfile.mkdtemp(prefix="facturia_downloads_")
        downloaded_files: Dict[str, bytes] = {}

        # 3. Métricas acumuladas
        total_pasos = 0
        total_tokens_in = 0
        total_tokens_out = 0
        total_costo_usd: Optional[Decimal] = Decimal("0.00000") if pricing else None
        submission_attempted = False
        captcha_auto_intentos = 0
        human_handoff_intentado = False
        historial_resumido: List[str] = []
        learned_recipe: Optional[Dict[str, Any]] = None

        await ctx.log(
            tipo="motor_generico_iniciado",
            mensaje=f"Iniciando motor genérico web en {url} con modelo {model_name}.",
            meta={"url": url, "modelo": model_name},
        )

        try:
            async with async_playwright() as p:
                browser = await p.chromium.launch(
                    headless=True,
                    args=CHROMIUM_STEALTH_ARGS,
                )
                context: BrowserContext = await browser.new_context(
                    viewport=DEFAULT_STEALTH_VIEWPORT,
                    user_agent=DEFAULT_STEALTH_USER_AGENT,
                    extra_http_headers=DEFAULT_STEALTH_EXTRA_HEADERS,
                    locale="es-MX",
                    timezone_id="America/Mexico_City",
                    permissions=["geolocation", "notifications"],
                    color_scheme="light",
                    accept_downloads=True,
                )
                await apply_stealth(context)

                active_pages: List[Page] = []
                download_tasks: List[asyncio.Task] = []
                detected_terminal_error: Optional[str] = None
                detected_terminal_msg: Optional[str] = None

                def setup_page_listeners(p_obj: Page):
                    def on_dialog(dialog):
                        nonlocal detected_terminal_error, detected_terminal_msg
                        msg = dialog.message or ""
                        logger.info("Diálogo del portal web capturado: %s", msg)
                        historial_resumido.append(f"Alerta emergente del portal: {msg}")
                        msg_lower = msg.lower()
                        if any(k in msg_lower for k in ("ya se encuentra facturado", "ya ha sido facturado", "ya fue facturado", "folio ya facturado")):
                            detected_terminal_error = "ticket_ya_facturado"
                            detected_terminal_msg = msg
                        elif any(k in msg_lower for k in ("antigüedad", "fuera de tiempo", "ha expirado", "no puede ser facturado debido a")):
                            detected_terminal_error = "ticket_vencido"
                            detected_terminal_msg = msg
                        elif any(k in msg_lower for k in ("no cuadra", "monto capturado")):
                            detected_terminal_error = "datos_no_coinciden"
                            detected_terminal_msg = msg
                        asyncio.create_task(dialog.accept())

                    p_obj.on("dialog", on_dialog)

                    async def on_response(response):
                        nonlocal detected_terminal_error, detected_terminal_msg
                        if response.status in (400, 404, 409, 422):
                            try:
                                ctype = response.headers.get("content-type", "")
                                if "json" in ctype or "text" in ctype:
                                    text_resp = await response.text()
                                    text_lower = text_resp.lower()
                                    if any(k in text_lower for k in ("ya se encuentra facturado", "facturado a otro", "ya ha sido facturado", "ya fue facturado", "folio ya facturado")):
                                        detected_terminal_error = "ticket_ya_facturado"
                                        try:
                                            parsed = json.loads(text_resp)
                                            detected_terminal_msg = parsed.get("detail") or parsed.get("mensaje") or parsed.get("error") or text_resp
                                        except Exception:
                                            detected_terminal_msg = text_resp[:200]
                                        logger.info("Error terminal capturado desde API del portal: %s (%s)", detected_terminal_msg, detected_terminal_error)
                                        historial_resumido.append(f"Respuesta de error del portal: {detected_terminal_msg}")
                            except Exception:
                                pass

                    p_obj.on("response", lambda r: asyncio.create_task(on_response(r)))

                    def on_download(download: Download):
                        async def _save_and_read(dl: Download):
                            fname = dl.suggested_filename
                            unique_name = f"{uuid.uuid4()}_{fname}"
                            save_path = os.path.join(temp_dir, unique_name)
                            try:
                                await dl.save_as(save_path)
                                with open(save_path, "rb") as f:
                                    content = f.read()
                                    if content:
                                        downloaded_files[fname] = content
                            except Exception as dl_err:
                                logger.warning("Error leyendo archivo descargado %s: %s", fname, dl_err)
                            finally:
                                if os.path.exists(save_path):
                                    try:
                                        os.remove(save_path)
                                    except OSError:
                                        pass

                        task = asyncio.create_task(_save_and_read(download))
                        download_tasks.append(task)

                    p_obj.on("download", on_download)

                def handle_new_page(new_p: Page):
                    if new_p not in active_pages:
                        active_pages.append(new_p)
                        setup_page_listeners(new_p)

                context.on("page", handle_new_page)

                page = await context.new_page()
                handle_new_page(page)

                # Navegar al portal inicial con tolerancia a CDNs lentos o páginas pesadas
                portal_valido = False
                try:
                    is_alsea_portal = "alsea.interfactura.com" in url.lower() or "alsea.com.mx" in url.lower()
                    if is_alsea_portal:
                        # Alsea SPA requiere commit y esperar a que Angular retire el loader .app-loading
                        await page.goto(url, wait_until="commit", timeout=20000)
                        try:
                            await page.wait_for_selector(".app-loading", state="detached", timeout=25000)
                        except Exception:
                            await page.wait_for_timeout(4000)
                    else:
                        try:
                            await page.goto(url, wait_until="domcontentloaded", timeout=12000)
                            await page.wait_for_timeout(1000)
                        except Exception:
                            # Fallback con 'commit' para portales pesados o con CDNs externos que no disparan domcontentloaded a tiempo
                            await page.goto(url, wait_until="commit", timeout=15000)
                            await page.wait_for_timeout(3000)

                    # Si el portal ya disparó un diálogo de error terminal (ej. alert("El ticket ya se encuentra facturado."))
                    if detected_terminal_error:
                        debug_key = await self._save_debug_screenshot(page, ctx, storage)
                        return EngineResult(
                            ok=False,
                            error_code=detected_terminal_error,
                            mensaje=detected_terminal_msg or f"El portal reportó: {detected_terminal_error}",
                            reintentable=False,
                            duracion_segundos=Decimal(str(round(time.time() - start_time, 2))),
                        )

                    body_text = await page.inner_text("body")
                    body_lower = body_text.lower()
                    url_lower = page.url.lower()

                    is_local_test = "127.0.0.1" in url_lower or "localhost" in url_lower or "testserver" in url_lower

                    # Soporte unificado para Alsea Interfactura (Domino's, Starbucks, Burger King, Chili's, etc.)
                    if "alsea.interfactura.com" in url_lower:
                        comercio_str = ((ticket.extracted.get("comercio") if ticket.extracted else "") or ticket.sucursal or "").lower()
                        brand_selectors = [
                            ("domino", "img[src*='logo_dominos']"),
                            ("starbucks", "img[src*='logo_starbucks']"),
                            ("burger", "img[src*='logo_burgerking']"),
                            ("chili", "img[src*='logo_chilis']"),
                            ("italianni", "img[src*='logo_italiannis']"),
                            ("vips", "img[src*='logo_vips']"),
                            ("chang", "img[src*='logo_pfchangs']"),
                            ("cheesecake", "img[src*='logo_cheesecake']"),
                        ]
                        for brand_key, brand_sel in brand_selectors:
                            if brand_key in comercio_str or (brand_key == "domino" and ticket.rfc_emisor == "OPP010927SA5"):
                                brand_loc = page.locator(brand_sel).first
                                try:
                                    if await brand_loc.count() > 0:
                                        logger.info("Portal Alsea detectado: seleccionando marca %s (%s)...", brand_key, brand_sel)
                                        await brand_loc.click(timeout=6000)
                                        # Esperar a que los inputs específicos de la marca aparezcan
                                        await page.locator("input#ticket, input#rfc, input#tienda").first.wait_for(state="visible", timeout=8000)
                                        await page.wait_for_timeout(1000)
                                        break
                                except Exception as brand_err:
                                    logger.warning("No se pudo autoseleccionar marca %s en Alsea: %s", brand_key, brand_err)
                        portal_valido = True
                    elif is_local_test:
                        portal_valido = True
                    elif any(term in body_lower for term in ("pedir en línea", "ordena ahora", "tu carrito", "selecciona tu pizza", "ver cartelera", "horarios y boletos", "agrega al carrito")):
                        # Si es una portada de tienda de comida/cartelera (ej. dominos.com.mx), buscar enlace directo hacia 'Facturación'
                        factura_links = page.locator(
                            "a[href*='factur' i], a:has-text('Facturación'), a:has-text('Facturacion'), a:has-text('Factura tu ticket'), a:has-text('Factura electrónica')"
                        )
                        if await factura_links.count() > 0:
                            try:
                                first_link = factura_links.first
                                href = await first_link.get_attribute("href")
                                logger.info("Enlace de facturación encontrado en tienda de comida: %s. Navegando...", href)
                                await first_link.click(timeout=8000)
                                await page.wait_for_timeout(2500)
                                portal_valido = True
                            except Exception as click_err:
                                logger.debug("No se pudo hacer clic en enlace de facturación de portada: %s", click_err)
                    else:
                        portal_valido = True
                except Exception as exc:
                    logger.warning("No se pudo cargar la URL inicial %s: %s", url, exc)

                # Si la URL inicial falló o está vacía, buscar portal oficial en internet
                is_local_test = "127.0.0.1" in (url or "") or "localhost" in (url or "")
                if not portal_valido and not is_local_test:
                    comercio_nom = ticket.extracted.get("comercio") if ticket.extracted else None
                    logger.info("Buscando portal de facturación en internet para %s...", comercio_nom or ticket.sucursal)
                    await ctx.log(
                        tipo="busqueda_portal_internet",
                        mensaje=f"La URL inicial no cargó un portal válido. Buscando portal oficial en internet para {comercio_nom or ticket.sucursal or 'el comercio'}.",
                        meta={"comercio": comercio_nom, "rfc": ticket.rfc_emisor},
                    )
                    candidatos = await search_candidate_portal_urls(
                        comercio=comercio_nom,
                        rfc_emisor=ticket.rfc_emisor,
                        sucursal=ticket.sucursal,
                        current_url=url,
                        extracted_data=ticket.extracted,
                    )
                    for cand_url in candidatos:
                        if cand_url == url:
                            continue
                        logger.info("Probando portal descubierto: %s", cand_url)
                        try:
                            try:
                                await page.goto(cand_url, wait_until="domcontentloaded", timeout=12000)
                                await page.wait_for_timeout(1000)
                            except Exception:
                                await page.goto(cand_url, wait_until="commit", timeout=15000)
                                await page.wait_for_timeout(2500)
                            if await verify_portal_matches_ticket(page, ticket):
                                url = cand_url
                                portal_valido = True
                                logger.info("Portal verificado exitosamente en internet: %s", url)
                                await ctx.log(
                                    tipo="portal_encontrado_internet",
                                    mensaje=f"Portal oficial verificado y enlazado: {url}.",
                                    meta={"url_descubierta": url},
                                )
                                if hasattr(ticket, "url_facturacion"):
                                    ticket.url_facturacion = url
                                break
                        except Exception as cand_err:
                            logger.debug("Candidato web %s falló: %s", cand_url, cand_err)

                if not portal_valido:
                    return EngineResult(
                        ok=False,
                        error_code="portal_invalido",
                        mensaje="No se pudo localizar ni verificar un portal de facturación oficial para este comercio.",
                        reintentable=False,
                        duracion_segundos=Decimal(str(round(time.time() - start_time, 2))),
                    )

                # Bucle del Agente
                while True:
                    total_pasos += 1
                    # Obtener la página activa más reciente (por si se abrió target="_blank")
                    current_page = active_pages[-1] if active_pages else page

                    # 1. Comprobar tope de pasos
                    if total_pasos > settings.PASOS_MAXIMOS_AGENTE:
                        debug_key = await self._save_debug_screenshot(current_page, ctx, storage)
                        error_code = "entrega_no_confirmada" if submission_attempted else "agente_no_completo"
                        mensaje = (
                            "Se envió la factura al portal pero no fue posible confirmar el timbrado ni descargar el CFDI."
                            if submission_attempted
                            else f"El agente agotó el límite de {settings.PASOS_MAXIMOS_AGENTE} pasos sin confirmar la factura."
                        )
                        return EngineResult(
                            ok=False,
                            error_code=error_code,
                            mensaje=mensaje,
                            pasos=total_pasos,
                            tokens_input=total_tokens_in,
                            tokens_output=total_tokens_out,
                            costo_usd=total_costo_usd,
                            duracion_segundos=Decimal(str(round(time.time() - start_time, 2))),
                        )

                    # 2. Comprobar tope de costo (Decimal)
                    if pricing and total_costo_usd is not None and total_costo_usd >= settings.COSTO_MAXIMO_POR_TICKET_USD:
                        debug_key = await self._save_debug_screenshot(current_page, ctx, storage)
                        error_code = "entrega_no_confirmada" if submission_attempted else "agente_costo_excedido"
                        mensaje = (
                            "Se envió la factura pero se alcanzó el tope de costo antes de confirmar el CFDI."
                            if submission_attempted
                            else (
                                f"El agente alcanzó el tope de costo permitido (${settings.COSTO_MAXIMO_POR_TICKET_USD} USD). "
                                f"Gasto acumulado: ${total_costo_usd} USD."
                            )
                        )
                        return EngineResult(
                            ok=False,
                            error_code=error_code,
                            mensaje=mensaje,
                            pasos=total_pasos,
                            tokens_input=total_tokens_in,
                            tokens_output=total_tokens_out,
                            costo_usd=total_costo_usd,
                            duracion_segundos=Decimal(str(round(time.time() - start_time, 2))),
                        )

                    # Comprobar error terminal detectado por diálogo nativo del portal
                    if detected_terminal_error:
                        debug_key = await self._save_debug_screenshot(current_page, ctx, storage)
                        return EngineResult(
                            ok=False,
                            error_code=detected_terminal_error,
                            mensaje=detected_terminal_msg or f"El portal reportó: {detected_terminal_error}",
                            pasos=total_pasos,
                            tokens_input=total_tokens_in,
                            tokens_output=total_tokens_out,
                            costo_usd=total_costo_usd,
                            reintentable=False,
                            duracion_segundos=Decimal(str(round(time.time() - start_time, 2))),
                        )

                    # Verificación proactiva de desafío interactivo / WAF en la página (evita bloqueos o esperas infinitas)
                    detected_captcha = await detect_interactive_captcha(current_page)
                    if detected_captcha and captcha_auto_intentos < 1:
                        captcha_auto_intentos += 1
                        await ctx.log(
                            tipo="captcha_detectado",
                            mensaje=f"Se detectó un desafío interactivo ({detected_captcha}) en el portal.",
                            meta={"tipo": detected_captcha},
                        )
                        resuelto_auto = await try_solve_captcha_autonomously(current_page)
                        if resuelto_auto:
                            await ctx.log(
                                tipo="captcha_auto_resuelto",
                                mensaje=f"Desafío interactivo ({detected_captcha}) resuelto con éxito.",
                                meta={"tipo": detected_captcha},
                            )
                            historial_resumido.append(f"Paso {total_pasos}: Desafío {detected_captcha} resuelto automáticamente.")
                            await current_page.wait_for_timeout(1500)
                            continue
                        else:
                            historial_resumido.append(f"Paso {total_pasos}: Desafío {detected_captcha} no resuelto en 3.5s; delegando a intervención o rechazo limpio.")
                            if not human_handoff_intentado:
                                human_handoff_intentado = True
                                await ctx.log(
                                    tipo="handoff_solicitado",
                                    mensaje=f"El agente solicita intervención humana por: {detected_captcha}.",
                                    meta={"motivo": detected_captcha},
                                )
                                try:
                                    resuelto = await ctx.handoff.request(
                                        motivo=detected_captcha,
                                        page=current_page,
                                        ctx=ctx,
                                        submission_attempted=submission_attempted,
                                    )
                                    if resuelto:
                                        await ctx.log(
                                            tipo="handoff_resuelto",
                                            mensaje=f"Intervención humana completada para {detected_captcha}. El agente continúa.",
                                            meta={"motivo": detected_captcha},
                                        )
                                        historial_resumido.append(f"Paso {total_pasos}: Intervención humana completada ({detected_captcha}).")
                                        await current_page.wait_for_timeout(1000)
                                        continue
                                except HandoffConcurrenciaExcedidaException:
                                    return EngineResult(
                                        ok=False,
                                        error_code="handoff_tope_concurrencia",
                                        mensaje=f"Tope de handoffs concurrentes alcanzado ({settings.HANDOFF_MAX_CONCURRENTES}). Ticket reencolado.",
                                        reintentable=True,
                                        pasos=total_pasos,
                                        duracion_segundos=Decimal(str(round(time.time() - start_time, 2))),
                                    )
                                except NotImplementedError:
                                    pass

                            debug_key = await self._save_debug_screenshot(current_page, ctx, storage)
                            return EngineResult(
                                ok=False,
                                error_code="captcha_requerido",
                                mensaje=f"El portal requiere resolver un desafío interactivo ({detected_captcha}).",
                                pasos=total_pasos,
                                tokens_input=total_tokens_in,
                                tokens_output=total_tokens_out,
                                costo_usd=total_costo_usd,
                                reintentable=True,
                                duracion_segundos=Decimal(str(round(time.time() - start_time, 2))),
                            )

                    # Verificación proactiva de modo invitado / facturar sin cuenta (Prioridad 1)
                    if total_pasos == 1 and not ctx.credenciales:
                        try:
                            guest_loc = current_page.locator(
                                "a:has-text('Facturación sin usuario'), button:has-text('Facturación sin usuario'), "
                                "a:has-text('Facturar sin cuenta'), button:has-text('Facturar sin cuenta'), "
                                "a:has-text('Continuar como invitado'), button:has-text('Continuar como invitado'), "
                                "a:has-text('Facturar sin registrarse'), button:has-text('Facturar sin registrarse')"
                            ).first
                            if await guest_loc.is_visible(timeout=800):
                                txt = (await guest_loc.inner_text()).strip()
                                logger.info("Modo invitado/express detectado proactivamente: '%s'. Avanzando sin requerir login...", txt)
                                await ctx.log(
                                    tipo="modo_invitado_detectado",
                                    mensaje=f"Detectada opción '{txt}'. Ingresando por vía express sin registro.",
                                    meta={"opcion": txt},
                                )
                                await guest_loc.click(timeout=4000)
                                await current_page.wait_for_timeout(1500)
                                historial_resumido.append(f"Paso {total_pasos}: Se seleccionó automáticamente '{txt}' para facturar sin cuenta.")
                                continue
                        except Exception as guest_err:
                            logger.debug("Omitiendo clic proactivo de modo invitado: %s", guest_err)

                    # Auto-aprendizaje continuo: extraer selectores y reglas de inputs si están visibles
                    if not learned_recipe:
                        try:
                            num_inputs = await current_page.locator("input:visible, select:visible").count()
                            if num_inputs >= 2:
                                learned_recipe = await extract_portal_recipe_from_page(current_page, ticket)
                        except Exception as exc:
                            logger.debug("No se pudo extraer receta preliminar: %s", exc)

                    # Captura de pantalla ligera (JPEG quality 75, ventana deslizante)
                    try:
                        screenshot_bytes = await current_page.screenshot(
                            type="jpeg", quality=75, timeout=7000, animations="disabled"
                        )
                    except Exception:
                        screenshot_bytes = await current_page.screenshot(
                            type="jpeg", quality=60, timeout=5000
                        )
                    screenshot_b64 = base64.b64encode(screenshot_bytes).decode("ascii")

                    # Consultar al cerebro del agente
                    decision = await self.brain.decide_step(
                        paso_numero=total_pasos,
                        historial_resumido=historial_resumido,
                        screenshot_b64=screenshot_b64,
                        ticket=ticket,
                        perfil=perfil,
                        page_url=current_page.url,
                        submission_attempted=submission_attempted,
                        merchant_config=merchant_config,
                        credenciales=ctx.credenciales,
                    )

                    # Contabilidad de tokens y costo
                    total_tokens_in += decision.tokens_input
                    total_tokens_out += decision.tokens_output
                    if pricing and total_costo_usd is not None:
                        costo_paso = (
                            Decimal(str(decision.tokens_input)) * Decimal(str(pricing["input"])) / Decimal("1000000")
                            + Decimal(str(decision.tokens_output)) * Decimal(str(pricing["output"])) / Decimal("1000000")
                        )
                        total_costo_usd += costo_paso

                    # -------------------------------------------------------
                    # CASO: FACTURADO CON ÉXITO
                    # -------------------------------------------------------
                    if decision.tipo == "facturado_success":
                        # Auto-aprendizaje de la receta del comercio
                        try:
                            final_recipe = learned_recipe or await extract_portal_recipe_from_page(current_page, ticket)
                            await learn_merchant_recipe(ticket, current_page.url, final_recipe)
                        except Exception as learn_err:
                            logger.debug("Error durante auto-aprendizaje de receta: %s", learn_err)

                        # Esperar tareas de descarga pendientes si las hubiere
                        if download_tasks:
                            await asyncio.gather(*download_tasks, return_exceptions=True)

                        # Verificar si se descargaron archivos
                        pdf_bytes = None
                        xml_bytes = None
                        for fname, content in downloaded_files.items():
                            if fname.lower().endswith(".pdf"):
                                pdf_bytes = content
                            elif fname.lower().endswith(".xml"):
                                xml_bytes = content

                        entrega_final = decision.entrega
                        if pdf_bytes or xml_bytes:
                            entrega_final = "descarga"

                        await ctx.log(
                            tipo="facturado_agente",
                            mensaje="Facturación completada exitosamente por el agente genérico.",
                            meta={
                                "pasos": total_pasos,
                                "tokens_in": total_tokens_in,
                                "tokens_out": total_tokens_out,
                                "costo_usd": float(total_costo_usd) if total_costo_usd is not None else None,
                                "cfdi_uuid": decision.cfdi_uuid,
                                "historial": historial_resumido,
                            },
                        )

                        return EngineResult(
                            ok=True,
                            cfdi_uuid=decision.cfdi_uuid or str(uuid.uuid4()),
                            entrega=entrega_final,
                            correo_capturado=decision.correo_capturado or perfil.email_receptor,
                            pdf=pdf_bytes,
                            xml=xml_bytes,
                            pasos=total_pasos,
                            tokens_input=total_tokens_in,
                            tokens_output=total_tokens_out,
                            costo_usd=total_costo_usd,
                            duracion_segundos=Decimal(str(round(time.time() - start_time, 2))),
                        )

                    # -------------------------------------------------------
                    # CASO: PERFIL INCOMPLETO (Regla 10)
                    # -------------------------------------------------------
                    if decision.tipo == "perfil_incompleto":
                        campo = decision.campo_faltante or "dato no disponible"
                        await ctx.log(
                            tipo="perfil_incompleto",
                            mensaje=f"El portal requiere el campo '{campo}' que no está en el perfil fiscal.",
                            meta={"campo_faltante": campo},
                        )
                        return EngineResult(
                            ok=False,
                            error_code="perfil_incompleto",
                            mensaje=f"El portal requiere el campo '{campo}' para facturar. Completa tus datos fiscales.",
                            pasos=total_pasos,
                            tokens_input=total_tokens_in,
                            tokens_output=total_tokens_out,
                            costo_usd=total_costo_usd,
                            reintentable=False,
                            duracion_segundos=Decimal(str(round(time.time() - start_time, 2))),
                        )

                    # -------------------------------------------------------
                    # CASO: SOLICITUD DE HANDOFF (Captcha o Agente Atorado)
                    # -------------------------------------------------------
                    if decision.tipo == "request_handoff":
                        motivo = decision.handoff_motivo or "captcha"

                        # 1. Intento autónomo de resolución si es captcha (máximo 1 intento rápido)
                        if motivo == "captcha" and captcha_auto_intentos < 1:
                            captcha_auto_intentos += 1
                            await ctx.log(
                                tipo="captcha_auto_resolucion_intento",
                                mensaje="Intentando resolver captcha automáticamente con emulación limpia (intento 1/1)...",
                                meta={"intento": captcha_auto_intentos},
                            )
                            resuelto_auto = await try_solve_captcha_autonomously(current_page)
                            if resuelto_auto:
                                await ctx.log(
                                    tipo="captcha_auto_resuelto",
                                    mensaje="Captcha interactivo resuelto exitosamente sin intervención humana.",
                                    meta={"intento": captcha_auto_intentos},
                                )
                                historial_resumido.append(f"Paso {total_pasos}: Captcha resuelto automáticamente.")
                                await current_page.wait_for_timeout(2000)
                                continue
                            else:
                                historial_resumido.append(f"Paso {total_pasos}: Resolución autónoma de captcha no tuvo éxito; solicitando intervención o rechazo limpio.")

                        # 2. Control de sesiones: No abrir sesiones repetidas si ya se intentó para este ticket
                        if human_handoff_intentado:
                            logger.warning("Intervención humana ya fue intentada previamente para este ticket. Evitando bucle de sesiones redundantes.")
                            debug_key = await self._save_debug_screenshot(current_page, ctx, storage)
                            return EngineResult(
                                ok=False,
                                error_code="handoff_expirado",
                                mensaje=f"Intervención humana para {motivo} no se pudo completar.",
                                pasos=total_pasos,
                                tokens_input=total_tokens_in,
                                tokens_output=total_tokens_out,
                                costo_usd=total_costo_usd,
                                reintentable=True,
                                duracion_segundos=Decimal(str(round(time.time() - start_time, 2))),
                            )

                        human_handoff_intentado = True
                        await ctx.log(
                            tipo="handoff_solicitado",
                            mensaje=f"El agente solicita intervención humana por: {motivo}.",
                            meta={"motivo": motivo},
                        )
                        # Solicitar handoff al protocolo
                        try:
                            resuelto = await ctx.handoff.request(
                                motivo=motivo,
                                page=current_page,
                                ctx=ctx,
                                submission_attempted=submission_attempted,
                            )
                        except HandoffConcurrenciaExcedidaException:
                            logger.warning("Tope de handoffs concurrentes alcanzado. Reencolando.")
                            return EngineResult(
                                ok=False,
                                error_code="handoff_tope_concurrencia",
                                mensaje=f"Tope de handoffs concurrentes alcanzado ({settings.HANDOFF_MAX_CONCURRENTES}). Ticket reencolado.",
                                reintentable=True,
                                pasos=total_pasos,
                                tokens_input=total_tokens_in,
                                tokens_output=total_tokens_out,
                                costo_usd=total_costo_usd,
                                duracion_segundos=Decimal(str(round(time.time() - start_time, 2))),
                            )
                        except NotImplementedError:
                            resuelto = False

                        if resuelto:
                            # Intervención humana completada con éxito
                            await ctx.log(
                                tipo="handoff_resuelto",
                                mensaje=f"Intervención humana completada para {motivo}. El agente continúa.",
                                meta={"motivo": motivo},
                            )
                            historial_resumido.append(f"Paso {total_pasos}: Intervención humana completada con éxito ({motivo}).")
                            await current_page.wait_for_timeout(1000)
                            continue

                        # Si expiró o fue cancelado
                        debug_key = await self._save_debug_screenshot(current_page, ctx, storage)
                        return EngineResult(
                            ok=False,
                            error_code="handoff_expirado",
                            mensaje=f"Intervención humana para {motivo} expiró o fue cancelada.",
                            pasos=total_pasos,
                            tokens_input=total_tokens_in,
                            tokens_output=total_tokens_out,
                            costo_usd=total_costo_usd,
                            reintentable=True,
                            duracion_segundos=Decimal(str(round(time.time() - start_time, 2))),
                        )

                    # -------------------------------------------------------
                    # CASO: ENVÍO FINAL IRREVERSIBLE (submit_final)
                    # -------------------------------------------------------
                    if decision.tipo == "submit_final":
                        if submission_attempted:
                            # PROHIBIDO segundo envío
                            logger.warning("Intento de segundo submit_final bloqueado por política de irreversibilidad.")
                            historial_resumido.append(f"Paso {total_pasos}: Intento de segundo envío bloqueado (modo solo lectura activo).")
                            continue

                        selector = decision.submit_selector
                        if not selector:
                            selector = "button[type='submit'], button:has-text('Facturar'), button:has-text('Generar')"

                        try:
                            # Ejecutar el clic final
                            await current_page.locator(selector).first.click(timeout=8000)
                            submission_attempted = True
                            historial_resumido.append(f"Paso {total_pasos}: Envío final ejecutado en {selector}. Modo solo lectura activado.")
                            await ctx.log(
                                tipo="envio_final_ejecutado",
                                mensaje="Botón final de facturación presionado. Entrando en modo observación irreversible.",
                                meta={"selector": selector},
                            )
                            # Esperar respuesta de la red
                            await current_page.wait_for_timeout(3000)
                        except Exception as exc:
                            historial_resumido.append(f"Paso {total_pasos}: Fallo al hacer clic en {selector}: {exc}")

                        continue

                    # -------------------------------------------------------
                    # CASO: LOTE DE ACCIONES (batch_actions)
                    # -------------------------------------------------------
                    if decision.tipo == "batch_actions":
                        if not decision.batch:
                            historial_resumido.append(f"Paso {total_pasos}: Lote vacío recibido.")
                            continue

                        lote_exitoso = True
                        acciones_hechas = 0

                        for act in decision.batch:
                            # Seguridad: NUNCA permitir re-envíos ni mutaciones de formulario tras el envío final
                            if submission_attempted:
                                sel_lower = (act.selector or "").lower()
                                is_submit_action = any(k in sel_lower for k in ("submit", "facturar", "generar", "timbrar", "enviar"))
                                if act.tipo == "click" and is_submit_action:
                                    logger.warning(
                                        "Acción de envío repetida %s omitida tras el envío final (modo solo observación activo).",
                                        act.selector,
                                    )
                                    continue
                                elif act.tipo in ("type", "select_option"):
                                    logger.warning(
                                        "Acción mutativa de campo %s omitida tras el envío final (modo solo observación activo).",
                                        act.tipo,
                                    )
                                    continue

                            try:
                                if act.tipo == "type":
                                    loc = current_page.locator(act.selector).first
                                    await loc.fill(act.texto or "", timeout=5000)
                                    acciones_hechas += 1
                                elif act.tipo == "click":
                                    loc = current_page.locator(act.selector).first
                                    await loc.click(timeout=5000)
                                    # Breve pausa para que el navegador procese eventos y descargas asociadas
                                    await current_page.wait_for_timeout(600)
                                    acciones_hechas += 1
                                elif act.tipo == "select_option":
                                    loc = current_page.locator(act.selector).first
                                    await loc.select_option(value=act.valor, timeout=5000)
                                    acciones_hechas += 1
                                elif act.tipo == "wait":
                                    await current_page.wait_for_timeout(1000)
                                    acciones_hechas += 1
                            except Exception as act_err:
                                # ABORTO INMEDIATO DEL LOTE (Regla 5 del usuario)
                                lote_exitoso = False
                                historial_resumido.append(
                                    f"Paso {total_pasos}: Lote abortado en acción {acciones_hechas + 1} ({act.tipo} en {act.selector}): {act_err}"
                                )
                                break

                        if lote_exitoso:
                            historial_resumido.append(
                                f"Paso {total_pasos}: Lote de {len(decision.batch)} acciones completado con éxito."
                            )

                        # Breve pausa para asentar DOM
                        await current_page.wait_for_timeout(800)
                        continue

                    # -------------------------------------------------------
                    # CASO: TERMINAR ERROR
                    # -------------------------------------------------------
                    if decision.tipo == "terminar_error":
                        msg_err = decision.mensaje_error or ""
                        msg_lower = msg_err.lower()
                        reintentable = False
                        if any(k in msg_lower for k in ("ya se encuentra facturado", "ya ha sido facturado", "ya fue facturado", "ya facturado", "folio ya facturado")):
                            error_code = "ticket_ya_facturado"
                        elif any(k in msg_lower for k in ("no cuadra", "no coinciden", "datos incorrectos", "monto capturado")):
                            error_code = "datos_no_coinciden"
                        elif any(k in msg_lower for k in ("vencido", "antigüedad", "fuera de plazo", "ha expirado")):
                            error_code = "ticket_vencido"
                        elif any(k in msg_lower for k in ("no encontrado", "folio no existe", "inexistente")):
                            error_code = "folio_no_encontrado"
                        else:
                            error_code = "entrega_no_confirmada" if submission_attempted else "agente_error"
                            reintentable = False if submission_attempted else True

                        error_msg = msg_err or (
                            "Se envió la factura pero no fue posible confirmar la entrega en el portal."
                            if submission_attempted
                            else "El agente no pudo procesar el portal."
                        )
                        debug_key = await self._save_debug_screenshot(current_page, ctx, storage)
                        return EngineResult(
                            ok=False,
                            error_code=error_code,
                            mensaje=error_msg,
                            pasos=total_pasos,
                            tokens_input=total_tokens_in,
                            tokens_output=total_tokens_out,
                            costo_usd=total_costo_usd,
                            reintentable=reintentable,
                            duracion_segundos=Decimal(str(round(time.time() - start_time, 2))),
                        )

        finally:
            # Limpieza rigurosa del directorio temporal (Regla 4: disco limpio)
            shutil.rmtree(temp_dir, ignore_errors=True)

    async def _save_debug_screenshot(
        self, page: Page, ctx: EngineContext, storage: Any
    ) -> Optional[str]:
        """
        Guarda la captura de depuración en el bucket S3 efímero (Regla 2 de corrección:
        NUNCA como binario dentro de ticket_events).
        """
        try:
            try:
                shot_bytes = await page.screenshot(
                    type="jpeg", quality=70, timeout=7000, animations="disabled"
                )
            except Exception:
                shot_bytes = await page.screenshot(type="jpeg", quality=60, timeout=5000)
            key = f"debug/{ctx.ticket.tenant_id}/{ctx.ticket.id}.jpg"
            await storage.upload_bytes(
                key=key,
                data=shot_bytes,
                content_type="image/jpeg",
            )
            await ctx.log(
                tipo="captura_depuracion_guardada",
                mensaje="Captura de pantalla de depuración guardada en almacenamiento efímero.",
                meta={"screenshot_key": key},
            )
            return key
        except Exception as exc:
            logger.warning("No se pudo guardar captura de depuración en S3: %s", exc)
            return None
