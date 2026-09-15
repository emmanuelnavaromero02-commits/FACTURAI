import logging

from playwright.async_api import BrowserContext, Page
from playwright_stealth import Stealth

logger = logging.getLogger(__name__)

_stealth = Stealth()

CHROMIUM_STEALTH_ARGS = [
    "--disable-blink-features=AutomationControlled",
    "--disable-features=IsolateOrigins,site-per-process",
    "--disable-infobars",
    "--no-sandbox",
    "--no-first-run",
    "--no-service-autorun",
    "--password-store=basic",
    "--use-mock-keychain",
    "--disable-dev-shm-usage",
]

DEFAULT_STEALTH_VIEWPORT = {"width": 1366, "height": 768}
DEFAULT_STEALTH_USER_AGENT = (
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
    "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/128.0.0.0 Safari/537.36"
)


async def apply_stealth(target: BrowserContext | Page) -> None:
    """
    Aplica evasión profunda de huellas de automatización en el contexto o página.
    Parchea:
      - navigator.webdriver (evita que sea detectable en Navigator.prototype sin crear own-property)
      - window.chrome (app, csi, loadTimes)
      - navigator.plugins y navigator.mimeTypes (emula plugins nativos de PDF/Viewer)
      - WebGL UNMASKED_VENDOR y UNMASKED_RENDERER (evita SwiftShader / Mesa)
      - navigator.languages y Client Hints (navigator.userAgentData)
    """
    try:
        await _stealth.apply_stealth_async(target)
    except Exception as exc:
        logger.warning("No se pudo aplicar stealth completo a la página o contexto: %s", exc)
