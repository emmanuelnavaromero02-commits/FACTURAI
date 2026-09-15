import asyncio
import logging
from typing import Optional

from playwright.async_api import Page

from .stealth_utils import apply_stealth

logger = logging.getLogger(__name__)


async def detect_interactive_captcha(page: Page) -> Optional[str]:
    """
    Detecta si en la página actual existe un desafío de captcha interactivo.
    Retorna 'turnstile', 'recaptcha', 'hcaptcha' o None.
    """
    try:
        # 1. Cloudflare Turnstile
        turnstile_frame = page.locator('iframe[src*="challenges.cloudflare.com"], iframe[src*="turnstile"]')
        if await turnstile_frame.count() > 0 and await turnstile_frame.first.is_visible():
            return "turnstile"

        turnstile_elem = page.locator('.cf-turnstile, [data-sitekey]:has-text("Cloudflare")')
        if await turnstile_elem.count() > 0 and await turnstile_elem.first.is_visible():
            return "turnstile"

        # 2. Google reCAPTCHA
        recaptcha_frame = page.locator('iframe[src*="recaptcha/api2/anchor"], iframe[src*="google.com/recaptcha"]')
        if await recaptcha_frame.count() > 0 and await recaptcha_frame.first.is_visible():
            return "recaptcha"

        # 3. hCaptcha
        hcaptcha_frame = page.locator('iframe[src*="hcaptcha.com"]')
        if await hcaptcha_frame.count() > 0 and await hcaptcha_frame.first.is_visible():
            return "hcaptcha"

        # 4. Página de desafío WAF / Cloudflare Managed Challenge
        title = (await page.title() or "").lower()
        if "just a moment" in title or "un momento" in title:
            return "turnstile"

    except Exception as exc:
        logger.debug("Error comprobando presencia de captcha: %s", exc)

    return None


async def try_solve_captcha_autonomously(page: Page) -> bool:
    """
    Intenta resolver automáticamente un captcha interactivo con límite estricto
    de 3.5 segundos. Si requiere rompecabezas, puzzle o desafío interactivo complejo,
    retorna False de inmediato para no congelar el flujo y permitir un handoff/rechazo limpio.
    """
    try:
        return await asyncio.wait_for(_try_solve_internal(page), timeout=3.5)
    except asyncio.TimeoutError:
        logger.info("Resolución de captcha excedió el límite de 3.5s; delegando limpiamente a handoff o rechazo.")
        return False
    except Exception as exc:
        logger.warning("Fallo durante intento de resolución autónoma de captcha: %s", exc)
        return False


async def _try_solve_internal(page: Page) -> bool:
    captcha_type = await detect_interactive_captcha(page)
    if not captcha_type:
        logger.info("No se detectó ningún widget de captcha explícito en la página.")
        return False

    logger.info("Intentando resolver automáticamente captcha de tipo: %s con huella limpia", captcha_type)
    await apply_stealth(page)

    if captcha_type == "turnstile":
        return await _solve_turnstile(page)
    elif captcha_type == "recaptcha":
        return await _solve_recaptcha(page)
    elif captcha_type == "hcaptcha":
        return await _solve_hcaptcha(page)

    return False


async def _solve_turnstile(page: Page) -> bool:
    """Interactúa con Cloudflare Turnstile con huella de navegador limpia."""
    iframe_loc = page.locator('iframe[src*="challenges.cloudflare.com"], iframe[src*="turnstile"]').first
    if not await iframe_loc.is_visible():
        return False

    frame = page.frame_locator('iframe[src*="challenges.cloudflare.com"], iframe[src*="turnstile"]').first
    checkbox = frame.locator('input[type="checkbox"], .ctp-checkbox-label, #challenge-stage, body').first

    if await checkbox.count() > 0:
        await checkbox.click(timeout=2000)
        # Esperar resolución rápida (máx 2s)
        for _ in range(4):
            await page.wait_for_timeout(500)
            token_val = await page.evaluate("""() => {
                const inp = document.querySelector('input[name="cf-turnstile-response"], input[name="turnstile-response"]');
                return inp ? inp.value : '';
            }""")
            if token_val and len(token_val) > 10:
                logger.info("Cloudflare Turnstile resuelto con éxito (token generado).")
                return True

            # Si el iframe desapareció o completó el challenge
            if not await iframe_loc.is_visible():
                logger.info("Cloudflare Turnstile resuelto (desafío completado y cerrado).")
                return True

    return False


async def _solve_recaptcha(page: Page) -> bool:
    """Interactúa con Google reCAPTCHA v2 (checkbox) con huella limpia."""
    iframe_loc = page.locator('iframe[src*="recaptcha/api2/anchor"], iframe[src*="google.com/recaptcha"]').first
    if not await iframe_loc.is_visible():
        return False

    frame = page.frame_locator('iframe[src*="recaptcha/api2/anchor"], iframe[src*="google.com/recaptcha"]').first
    anchor = frame.locator('#recaptcha-anchor, .recaptcha-checkbox-border').first

    if await anchor.count() > 0:
        await anchor.click(timeout=4000)
        await page.wait_for_timeout(2000)

        # Si se abrió el iframe de desafío de imágenes complejas (bframe), delegar de inmediato a handoff
        bframe = page.locator('iframe[src*="recaptcha/api2/bframe"]')
        if await bframe.count() > 0 and await bframe.first.is_visible():
            logger.info("reCAPTCHA abrió desafío de imágenes; requiere intervención humana.")
            return False

        # Verificar si quedó marcado con checkmark
        is_checked = await anchor.get_attribute("aria-checked")
        if is_checked == "true":
            logger.info("Google reCAPTCHA v2 resuelto con éxito (checkbox marcado).")
            return True

        # Verificar valor en textarea oculto
        token_val = await page.evaluate("""() => {
            const el = document.getElementById('g-recaptcha-response');
            return el ? el.value : '';
        }""")
        if token_val and len(token_val) > 10:
            logger.info("Google reCAPTCHA v2 resuelto (token generado en g-recaptcha-response).")
            return True

    return False


async def _solve_hcaptcha(page: Page) -> bool:
    """Interactúa con hCaptcha."""
    iframe_loc = page.locator('iframe[src*="hcaptcha.com"]').first
    if not await iframe_loc.is_visible():
        return False

    frame = page.frame_locator('iframe[src*="hcaptcha.com"]').first
    checkbox = frame.locator('#checkbox').first

    if await checkbox.count() > 0:
        await checkbox.click(timeout=4000)
        await page.wait_for_timeout(2000)

        is_checked = await checkbox.get_attribute("aria-checked")
        if is_checked == "true":
            logger.info("hCaptcha resuelto con éxito.")
            return True

        token_val = await page.evaluate("""() => {
            const el = document.querySelector('[name="h-captcha-response"]');
            return el ? el.value : '';
        }""")
        if token_val and len(token_val) > 10:
            logger.info("hCaptcha resuelto con éxito (token presente).")
            return True

    return False
