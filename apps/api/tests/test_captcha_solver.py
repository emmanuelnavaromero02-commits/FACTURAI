import pytest
from playwright.async_api import async_playwright

from src.engines.captcha_solver import (
    detect_interactive_captcha,
    try_solve_captcha_autonomously,
)


@pytest.mark.asyncio
async def test_detect_no_captcha():
    async with async_playwright() as p:
        browser = await p.chromium.launch(headless=True, args=["--no-sandbox"])
        page = await browser.new_page()
        await page.set_content("<html><body><h1>Formulario sin captcha</h1><input id='test'/></body></html>")
        detected = await detect_interactive_captcha(page)
        assert detected is None
        solved = await try_solve_captcha_autonomously(page)
        assert solved is False
        await browser.close()


@pytest.mark.asyncio
async def test_detect_and_solve_mock_turnstile():
    async with async_playwright() as p:
        browser = await p.chromium.launch(headless=True, args=["--no-sandbox"])
        page = await browser.new_page()

        # HTML simulando un widget Cloudflare Turnstile interactivo
        await page.set_content("""
            <!DOCTYPE html>
            <html>
            <head><title>Test Turnstile</title></head>
            <body>
                <input type="hidden" name="cf-turnstile-response" id="cf-resp" value="" />
                <iframe src="https://challenges.cloudflare.com/cdn-cgi/challenge-platform/h/g/turnstile" id="cf-iframe"></iframe>
            </body>
            </html>
        """)

        detected = await detect_interactive_captcha(page)
        assert detected == "turnstile"
        await browser.close()


@pytest.mark.asyncio
async def test_detect_recaptcha():
    async with async_playwright() as p:
        browser = await p.chromium.launch(headless=True, args=["--no-sandbox"])
        page = await browser.new_page()

        await page.set_content("""
            <!DOCTYPE html>
            <html>
            <head><title>Test reCAPTCHA</title></head>
            <body>
                <iframe src="https://www.google.com/recaptcha/api2/anchor" id="rc-iframe"></iframe>
            </body>
            </html>
        """)

        detected = await detect_interactive_captcha(page)
        assert detected == "recaptcha"
        await browser.close()


@pytest.mark.asyncio
async def test_stealth_fingerprint_evasion():
    """Verifica que el entorno stealth oculte automatización y configure huellas reales."""
    from src.engines.stealth_utils import (
        CHROMIUM_STEALTH_ARGS,
        DEFAULT_STEALTH_USER_AGENT,
        DEFAULT_STEALTH_VIEWPORT,
        apply_stealth,
    )

    async with async_playwright() as p:
        browser = await p.chromium.launch(
            headless=True,
            args=CHROMIUM_STEALTH_ARGS,
        )
        context = await browser.new_context(
            viewport=DEFAULT_STEALTH_VIEWPORT,
            user_agent=DEFAULT_STEALTH_USER_AGENT,
        )
        await apply_stealth(context)
        page = await context.new_page()

        res = await page.evaluate("""() => ({
            webdriver: navigator.webdriver,
            hasOwnWebdriver: Object.prototype.hasOwnProperty.call(navigator, 'webdriver'),
            hasChrome: !!window.chrome,
            pluginsLen: navigator.plugins ? navigator.plugins.length : 0,
            mimeTypesLen: navigator.mimeTypes ? navigator.mimeTypes.length : 0
        })""")

        assert res["webdriver"] is False
        assert res["hasOwnWebdriver"] is False
        assert res["hasChrome"] is True
        assert res["pluginsLen"] > 0
        assert res["mimeTypesLen"] > 0

        await browser.close()

