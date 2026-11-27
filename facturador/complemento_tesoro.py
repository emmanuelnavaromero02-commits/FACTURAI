"""Tesoro's own steps around the general engine: its menu, recipient form and code lookup.

Everything here used to live inside the runner's loop. It runs only for Tesoro tickets.
"""
import re
import unicodedata
from urllib.parse import urljoin, urlsplit

from playwright.async_api import Error as BrowserError

from .catalog import allowed_url, public_portal_origin
from .complementos import DONE, GO, Complement, Signal, observe
from .portals import normalize
from .tesoro_consulta import existing_invoice_folio, recover_tesoro_consulta
from .tesoro_recipient import tesoro_recipient
from .tesoro_result import tesoro_verification_failure
from .ticket_lookup import prepare_tesoro_lookup

FISCAL_INPUT = re.compile(r'\b(?:textbox|combobox|spinbutton|radio)\s+"[^"\n]*\b(?:rfc|régimen|regimen|postal|cfdi|forma de pago|folio|ticket|peaje|razón social|razon social)\b', re.I)
TESORO_HOME = re.compile(r'opciones de facturaci[oó]n', re.I)
TESORO_SINGLE = re.compile(r'generar factura de estaci[oó]n y znak', re.I)


def portal_entry_home(page, snapshot: str, receipt) -> bool:
    """The Tesoro menu is a navigation page, not the fiscal submission form."""
    from .runner import SUCCESS, already_billed  # the engine's own reading of an issued invoice

    current = urlsplit(page.url)
    return (receipt.provider == 'tesoro' and current.path in {'', '/'} and
            not current.query and not current.fragment and
            bool(TESORO_HOME.search(snapshot)) and bool(TESORO_SINGLE.search(snapshot)) and
            not FISCAL_INPUT.search(snapshot) and not SUCCESS.search(snapshot) and
            not already_billed(snapshot))


async def safe_portal_entry_href(page, action, receipt, snapshot: str) -> str | None:
    """Open only the documented single-ticket Tesoro form by a plain same-origin GET."""
    target = action.target
    # The live site's Font Awesome icons are part of Playwright's accessible
    # link name (for example, "Continuar... \uf0a9"). Keep the exact locator
    # below, but ignore those private-use glyphs when classifying its label.
    label = ' '.join(''.join(char for char in (target.name if target else '')
                             if unicodedata.category(char) != 'Co').split()).casefold()
    if (not portal_entry_home(page, snapshot, receipt) or action.kind != 'click' or
            not target or target.by != 'role' or target.role != 'link' or not target.exact or
            label not in {'continuar...', 'generar factura de estación y znak'}):
        return None
    try:
        links = page.get_by_role('link', name=target.name, exact=True)
        count = await links.count()
        if not 1 <= count <= 16:
            return None
        matches = []
        for index in range(count):
            link = links.nth(index)
            if not await link.is_visible():
                continue
            attributes = await link.evaluate("""a => ({tag: a.tagName, href: a.getAttribute('href'),
                download: a.hasAttribute('download'), target: a.hasAttribute('target'),
                onclick: a.hasAttribute('onclick'), form: Boolean(a.closest('form'))})""")
            if (attributes['tag'] != 'A' or not attributes['href'] or attributes['download'] or
                    attributes['target'] or attributes['onclick'] or attributes['form']):
                continue
            href = urljoin(page.url, attributes['href'])
            parts = urlsplit(href)
            if (parts.path != '/GenerarCFDI/Index' or parts.query != 'avanzada=0' or parts.fragment or
                    public_portal_origin(href) != public_portal_origin(page.url) or
                    not allowed_url(href, receipt.provider, receipt.portal_url)):
                continue
            matches.append(href)
        return matches[0] if len(matches) == 1 else None
    except BrowserError:
        return None


class TesoroComplement(Complement):
    def __init__(self, *args):
        super().__init__(*args)
        self.lookup_attempted = False    # the code verification was submitted; it is never sent twice

    async def rejection(self, page):
        """What the portal answered to the verified code: recover an invoice it already has, or stop."""
        rejected = await tesoro_verification_failure(page, self.receipt, self.profile)
        if rejected:
            self.event('ticket_lookup_rejected', rejected)
            if existing_invoice_folio(rejected):
                if await recover_tesoro_consulta(self.runner, page, self.key, self.profile, self.receipt, rejected):
                    return DONE
                raise ValueError(rejected + ' La consulta de Tesoro no devolvió un PDF y XML cotejables.')
            raise ValueError(rejected)
        return GO

    async def after_reading(self, view):
        if self.lookup_attempted and view.unsent:
            return await self.rejection(view.page)
        return GO

    async def before_fiscal_choices(self, view):
        try:
            changed, pending = await tesoro_recipient(view.page, self.profile, apply=True)
        except BrowserError:
            changed, pending = None, ['ficha fiscal del receptor']
        if changed:
            self.event('campo_verificado', 'Campo fiscal cotejado: ' + changed)
            return observe()
        return Signal(pending=tuple(pending))

    async def after_ticket_lookup(self, view):
        try:
            field = await prepare_tesoro_lookup(
                view.page, self.receipt, verify_attempted=self.lookup_attempted)
        except BrowserError:
            # A validation POST may have reached the portal before Chrome
            # reported a navigation error. Never repeat it automatically.
            self.lookup_attempted = True
            return observe()
        if field:
            if field.startswith('Verificación'):
                self.lookup_attempted = True
                if await self.rejection(view.page) is DONE:
                    return DONE
            self.event('campo_verificado', 'Dato del ticket cotejado: ' + field)
            return observe()
        return GO

    async def before_acting(self, view):
        page, snapshot, action, name, recent = view.page, view.snapshot, view.action, view.name, view.recent
        if (action.kind == 'click' and
                normalize(''.join(char for char in name if unicodedata.category(char) != 'Co'))
                == 'GUARDAR Y CONTINUAR' and
                urlsplit(page.url).path.rstrip('/') == '/GenerarCFDI'):
            _, unresolved = await tesoro_recipient(page, self.profile)
            if unresolved:
                error = 'Antes de guardar el receptor faltan: ' + ', '.join(unresolved) + '.'
                if recent and recent[-1].get('bloqueo') == error:
                    raise ValueError(error)
                recent.append({'bloqueo': error})
                return observe(error=error)
        if view.unsent and portal_entry_home(page, snapshot, self.receipt) and action.kind == 'click' and action.target and action.target.by == 'role' and action.target.role == 'link':
            href = await safe_portal_entry_href(page, action, self.receipt, snapshot)
            if href:
                before = page.url, snapshot
                await page.goto(href, wait_until='domcontentloaded', timeout=18000)
                recent.append({'tipo': 'goto', 'control': name[:120]})
                self.event('portal_entry', 'Se abrió el formulario de un ticket del mismo comercio.')
                return observe(before=before)
            raise ValueError('El enlace del menú no corresponde al formulario de un ticket verificado. Revisa el destino en el visor.')
        return GO

    async def before_issue(self, view):
        mismatch = await tesoro_verification_failure(view.page, self.receipt, self.profile, issue_gate=True)
        if mismatch:
            raise ValueError(mismatch)
        return GO
