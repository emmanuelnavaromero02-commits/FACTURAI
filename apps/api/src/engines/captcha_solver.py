import asyncio
import logging
import random
from typing import Optional

from playwright.async_api import FrameLocator, Page

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

    except Exception as exc:
        logger.debug("Error comprobando presencia de captcha: %s", exc)

    return None


async def try_solve_captcha_autonomously(page: Page) -> bool:
    """
    Intenta resolver automáticamente un captcha interactivo mediante
    emulación de movimientos y comportamiento humano antes de recurrir al handoff.
    Retorna True si fue resuelto exitosamente, False si no pudo resolverse.
    """
    captcha_type = await detect_interactive_captcha(page)
    if not captcha_type:
        logger.info("No se detectó ningún widget de captcha explícito en la página.")
        return False

    logger.info("Intentando resolver automáticamente captcha de tipo: %s", captcha_type)

    try:
        if captcha_type == "turnstile":
            return await _solve_turnstile(page)
        elif captcha_type == "recaptcha":
            return await _solve_recaptcha(page)
        elif captcha_type == "hcaptcha":
            return await _solve_hcaptcha(page)
    except Exception as exc:
        logger.warning("Fallo durante intento de resolución autónoma de %s: %s", captcha_type, exc)

    return False


async def _human_click_locator(page: Page, locator) -> None:
    """
    Mueve el ratón suavemente hacia el elemento simulando aceleración biológica
    y hace clic tras una pausa natural.
    """
    box = await locator.bounding_box()
    if not box:
        # Fallback al click directo de Playwright
        await locator.click()
        return

    # Punto objetivo con ligera variación aleatoria respecto al centro
    target_x = box["x"] + box["width"] / 2 + random.uniform(-3, 3)
    target_y = box["y"] + box["height"] / 2 + random.uniform(-2, 2)

    # Movimiento fluido en pasos
    steps = random.randint(12, 22)
    await page.mouse.move(target_x, target_y, steps=steps)

    # Pausa humana antes de presionar
    await asyncio.sleep(random.uniform(0.35, 0.70))
    await page.mouse.down()
    await asyncio.sleep(random.uniform(0.07, 0.15))
    await page.mouse.up()


async def _solve_turnstile(page: Page) -> bool:
    """Emula la interacción con Cloudflare Turnstile."""
    iframe_loc = page.locator('iframe[src*="challenges.cloudflare.com"], iframe[src*="turnstile"]').first
    if not await iframe_loc.is_visible():
        return False

    frame = page.frame_locator('iframe[src*="challenges.cloudflare.com"], iframe[src*="turnstile"]').first
    checkbox = frame.locator('input[type="checkbox"], .ctp-checkbox-label, #challenge-stage, body').first

    if await checkbox.count() > 0:
        await _human_click_locator(page, checkbox)
        # Esperar resolución de validación de Cloudflare
        for _ in range(8):
            await page.wait_for_timeout(500)
            token_val = await page.evaluate("""() => {
                const inp = document.querySelector('input[name="cf-turnstile-response"], input[name="turnstile-response"]');
                return inp ? inp.value : '';
            }""")
            if token_val and len(token_val) > 10:
                logger.info("Cloudflare Turnstile resuelto con éxito (token generado).")
                return True

            # O verificar si el iframe desapareció o cambió de estado a checked
            if not await iframe_loc.is_visible():
                logger.info("Cloudflare Turnstile resuelto (desafío completado y cerrado).")
                return True

    return False


async def _solve_recaptcha(page: Page) -> bool:
    """Emula la interacción con Google reCAPTCHA v2 (checkbox)."""
    iframe_loc = page.locator('iframe[src*="recaptcha/api2/anchor"], iframe[src*="google.com/recaptcha"]').first
    if not await iframe_loc.is_visible():
        return False

    frame = page.frame_locator('iframe[src*="recaptcha/api2/anchor"], iframe[src*="google.com/recaptcha"]').first
    anchor = frame.locator('#recaptcha-anchor, .recaptcha-checkbox-border').first

    if await anchor.count() > 0:
        await _human_click_locator(page, anchor)
        await page.wait_for_timeout(2500)

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

        # Si se abrió el iframe de desafío de imágenes (bframe), no se pudo resolver con checkbox
        bframe = page.locator('iframe[src*="recaptcha/api2/bframe"]')
        if await bframe.count() > 0 and await bframe.first.is_visible():
            logger.info("reCAPTCHA abrió desafío de imágenes complejas; requiere intervención humana.")
            return False

    return False


async def _solve_hcaptcha(page: Page) -> bool:
    """Emula la interacción con hCaptcha."""
    iframe_loc = page.locator('iframe[src*="hcaptcha.com"]').first
    if not await iframe_loc.is_visible():
        return False

    frame = page.frame_locator('iframe[src*="hcaptcha.com"]').first
    checkbox = frame.locator('#checkbox').first

    if await checkbox.count() > 0:
        await _human_click_locator(page, checkbox)
        await page.wait_for_timeout(2500)

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
