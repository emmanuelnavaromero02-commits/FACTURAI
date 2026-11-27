"""BP's own steps around the general engine: its franchise cover, RFC lookup and ticket form.

Everything here used to live inside the runner's loop. It runs only for BP tickets.
"""
import asyncio
from pathlib import Path
from urllib.parse import urlsplit

from playwright.async_api import Error as BrowserError

from .bp_recipient import prepare_bp_rfc
from .bp_reprint import recover_bp_reprint
from .bp_ticket import BPAlreadyBilled, BPProgress, at_form, bp_issue_proof, evidence_from_original, prepare_bp_ticket
from .brand_entry import BP_HOME, enter_bp_franchise
from .complementos import DONE, GO, Complement, Signal, observe, patched
from .models import Action, Target
from .portals import normalize

# Still patched in the engine module by the tests of this flow.
ENGINE_NAMES = ('evidence_from_original', 'recover_bp_reprint')


class BPComplement(Complement):
    def __init__(self, *args):
        super().__init__(*args)
        self.evidence = None
        self.progress = BPProgress()
        self.entry_attempted = False
        self.lookup_attempted = False

    async def start(self, job):
        if job.get('source_path'):
            self.evidence = await asyncio.to_thread(patched('evidence_from_original', evidence_from_original),
                                                    self.receipt, Path(job['source_path']))

    async def before_fiscal_choices(self, view):
        page, snapshot, receipt, profile = view.page, view.snapshot, self.receipt, self.profile
        if not self.entry_attempted and urlsplit(page.url).path.rstrip('/') == BP_HOME:
            self.entry_attempted = True
            prior_url = page.url
            try:
                entered = await enter_bp_franchise(page, receipt, snapshot)
            except BrowserError as exc:
                raise ValueError('La portada BP no confirmó la selección de la franquicia. Reintenta el ticket.') from exc
            if entered:
                view.recent.append({'tipo': 'click', 'control': 'Franquicia BP'})
                self.event('brand_entry', 'Se abrió el formulario de la franquicia BP verificada.')
                return observe(before=(prior_url, snapshot))
        try:
            field = await prepare_bp_rfc(
                page, receipt, profile, lookup_attempted=self.lookup_attempted)
        except BrowserError as exc:
            # Buscar may have reached the portal even if Chrome lost
            # its response. Never submit the same lookup twice.
            self.lookup_attempted = True
            raise ValueError('BP no confirmó la consulta del RFC. Revisa el portal antes de reintentar.') from exc
        if field:
            if field == 'Buscar RFC':
                self.lookup_attempted = True
            self.event('campo_verificado', 'Consulta fiscal cotejada: ' + field)
            return observe()
        if at_form(page, receipt):
            try:
                field = await prepare_bp_ticket(page, receipt, profile, self.evidence, self.progress)
            except BPAlreadyBilled as exc:
                self.event('ticket_already_billed', 'BP confirmó que la venta ya tiene factura; se buscaron los comprobantes existentes.')
                if await patched('recover_bp_reprint', recover_bp_reprint)(
                        self.runner, page, self.key, profile, receipt, self.evidence):
                    return DONE
                raise ValueError(str(exc)) from exc
            if field:
                self.event('campo_verificado', 'Ticket BP cotejado: ' + field)
                return observe()
            if self.auto_issue and await bp_issue_proof(page, receipt, profile, self.evidence):
                # The form is complete and proven: the executor emits by its own rules.
                return Signal(action=Action(
                    kind='click', target=Target(by='role', role='link', name='Facturar', exact=True),
                    value=None, reason='Emitir el único ticket BP cotejado con el perfil.', risk='issue'))
        return GO

    async def before_issue(self, view):
        if not await bp_issue_proof(view.page, self.receipt, self.profile, self.evidence):
            raise ValueError('BP no mostró una única compra cotejada con el ticket y el perfil fiscal.')
        return GO

    async def issue_proof(self, view):
        return (normalize(view.name) == 'FACTURAR' and
                await bp_issue_proof(view.page, self.receipt, self.profile, self.evidence))
