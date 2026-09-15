import logging

from playwright.async_api import BrowserContext, Page
from playwright_stealth import Stealth

logger = logging.getLogger(__name__)

_stealth = Stealth()

CHROMIUM_STEALTH_ARGS = [
    "--disable-blink-features=AutomationControlled",
    "--disable-features=IsolateOrigins,site-per-process,AudioServiceOutOfProcess",
    "--disable-infobars",
    "--no-sandbox",
    "--no-first-run",
    "--no-service-autorun",
    "--password-store=basic",
    "--use-mock-keychain",
    "--disable-dev-shm-usage",
    "--disable-ipc-flooding-protection",
    "--force-color-profile=srgb",
]

DEFAULT_STEALTH_VIEWPORT = {"width": 1366, "height": 768}
DEFAULT_STEALTH_USER_AGENT = (
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
    "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36"
)

DEFAULT_STEALTH_EXTRA_HEADERS = {
    "Sec-Ch-Ua": '"Google Chrome";v="131", "Chromium";v="131", "Not_A Brand";v="24"',
    "Sec-Ch-Ua-Mobile": "?0",
    "Sec-Ch-Ua-Platform": '"macOS"',
    "Sec-Ch-Ua-Platform-Version": '"15.0.0"',
    "Sec-Ch-Ua-Model": '""',
    "Accept-Language": "es-MX,es;q=0.9,en-US;q=0.8,en;q=0.7",
    "Upgrade-Insecure-Requests": "1",
}

DEEP_STEALTH_SCRIPT = """
// 1. Prototype hardening para navigator (sin crear own-property detectable)
try {
    const navProto = Object.getPrototypeOf(navigator);
    if (!navigator.deviceMemory) {
        Object.defineProperty(navProto, 'deviceMemory', { get: () => 8, configurable: true });
    }
    if (!navigator.hardwareConcurrency || navigator.hardwareConcurrency < 4) {
        Object.defineProperty(navProto, 'hardwareConcurrency', { get: () => 8, configurable: true });
    }
} catch (e) {}

// 2. Consistencia en dimensiones de ventana y pantalla física
try {
    if (window.outerWidth === 0 || window.outerWidth === window.innerWidth) {
        Object.defineProperty(window, 'outerWidth', { get: () => window.innerWidth, configurable: true });
        Object.defineProperty(window, 'outerHeight', { get: () => window.innerHeight + 85, configurable: true });
    }
    if (screen.availHeight === screen.height) {
        Object.defineProperty(screen, 'availHeight', { get: () => screen.height - 40, configurable: true });
        Object.defineProperty(screen, 'availWidth', { get: () => screen.width, configurable: true });
    }
} catch (e) {}

// 3. Permisos de notificación consistentes con comportamiento de usuario real
try {
    if (window.Notification) {
        Object.defineProperty(Notification, 'permission', { get: () => 'default', configurable: true });
    }
    if (navigator.permissions && navigator.permissions.query) {
        const origQuery = navigator.permissions.query;
        navigator.permissions.query = function (params) {
            if (params && params.name === 'notifications') {
                return Promise.resolve({ state: 'default', onchange: null });
            }
            return origQuery.apply(this, arguments);
        };
    }
} catch (e) {}

// 4. Ruido microscópico de Canvas para evitar hashes fijos de navegadores sin aceleración
try {
    const origGetContext = HTMLCanvasElement.prototype.getContext;
    HTMLCanvasElement.prototype.getContext = function (type, ...args) {
        const ctx = origGetContext.apply(this, [type, ...args]);
        if (ctx && type === '2d') {
            const origGetImageData = ctx.getImageData;
            ctx.getImageData = function (x, y, w, h) {
                const imgData = origGetImageData.apply(this, arguments);
                if (imgData && imgData.data && imgData.data.length > 0) {
                    for (let i = 0; i < Math.min(imgData.data.length, 128); i += 16) {
                        imgData.data[i] = imgData.data[i] ^ 1;
                    }
                }
                return imgData;
            };
        }
        return ctx;
    };
} catch (e) {}
"""


async def apply_stealth(target: BrowserContext | Page) -> None:
    """
    Aplica evasión profunda de huellas de automatización en el contexto o página.
    Parchea:
      - Client Hints (Sec-Ch-Ua) eliminando HeadlessChrome de la red
      - navigator.webdriver (a nivel prototipo sin own-property)
      - window.chrome (app, csi, loadTimes)
      - navigator.plugins y navigator.mimeTypes (PluginArray real)
      - WebGL UNMASKED_VENDOR y UNMASKED_RENDERER (evita SwiftShader)
      - deviceMemory, hardwareConcurrency, Notification.permission
      - Dimensiones reales de pantalla (screen.availHeight < screen.height)
      - Micro-variación de Canvas para hash de hardware único
    """
    try:
        await target.add_init_script(DEEP_STEALTH_SCRIPT)
    except Exception as exc:
        logger.debug("Aviso al registrar script de evasión profunda: %s", exc)

    try:
        await _stealth.apply_stealth_async(target)
    except Exception as exc:
        logger.warning("No se pudo aplicar stealth completo a la página o contexto: %s", exc)

