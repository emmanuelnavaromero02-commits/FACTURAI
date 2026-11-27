"""CAPUFE's own steps around the general engine: its recipient form, its two-step emission and its recovery.

Everything here used to live inside the runner's loop and its recovery. It runs only for CAPUFE tickets.
"""
import re
from urllib.parse import urlsplit

from playwright.async_api import Error as BrowserError

from .complementos import GO, Complement, observe
from .portals import capufe_proof, capufe_recipient, normalize

QUICK_FORM = '/capufe/facturacionrapida'


class CapufeComplement(Complement):
    prepares_after_negative_check = True

    async def before_fiscal_choices(self, view):
        if urlsplit(view.page.url).path.rstrip('/').lower() == QUICK_FORM:
            try:
                field = await capufe_recipient(view.page, self.profile)
            except BrowserError:
                field = None
            if field:
                self.event('campo_verificado', 'Campo cotejado: ' + field)
                return observe()
        return GO

    def prepares_issue(self, view):
        # «Facturar conceptos» opens the confirmation; «Sí estoy seguro» is the emission.
        return (normalize(view.name) == 'FACTURAR CONCEPTOS' and
                view.action.kind == 'click' and view.unsent and
                urlsplit(view.page.url).path.rstrip('/').lower() == QUICK_FORM)

    def known_preparation(self, view):
        return bool(view.action.kind == 'click' and
                    re.fullmatch(r'agregar (?:c[oó]digo|ticket|concepto)(?: de peaje)?', view.name.strip(), re.I))

    async def prove_preparation(self, view):
        if not await capufe_proof(view.page, self.profile, self.receipt, preparation=True):
            error = 'CAPUFE aún no refleja los datos fiscales, agrupación, códigos, fecha o total del expediente.'
            if view.recent and view.recent[-1].get('bloqueo') == error:
                await self.runner.human(self.key, view.page, 'blocked', error)
            view.recent.append({'bloqueo': error})
            return observe(error=error)
        return GO

    async def issue_proof(self, view):
        return (normalize(view.name) in {'SI ESTOY SEGURO', 'SÍ ESTOY SEGURO'} and
                await capufe_proof(view.page, self.profile, self.receipt))

    @staticmethod
    async def recover_documents(runner, key, profile, receipt, notes):
        from .capufe_recovery import recover_capufe
        # The lookup runs in the engine's own browser, so it counts as one with it.
        report = await recover_capufe(runner.store, key, receipt, profile, runner=runner)
        if report:
            notes.append(report)
        return await runner.recover(key, profile, notes=notes)
