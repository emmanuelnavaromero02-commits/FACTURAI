"""Vianney's own step around the general engine: its rejected ticket lookup.

Everything here used to live inside the runner's loop. It runs only for Vianney tickets.
"""
from .complementos import DONE, GO, Complement
from .vianney_consulta import lookup_rejected, rejection_for_current_ticket, recover_vianney_consulta


class VianneyComplement(Complement):
    async def after_reading(self, view):
        page, snapshot, receipt = view.page, view.snapshot, self.receipt
        if not view.unsent or not lookup_rejected(snapshot):
            return GO
        submitted_here = any(item.get('tipo') == 'click' and item.get('control', '').casefold() == 'siguiente'
                             for item in view.recent)
        prior_submission = any(event['kind'] == 'action' and event['detail'].casefold() == 'click · siguiente'
                               for event in self.runner.store.events(self.key)) if not submitted_here else False
        rejected = await rejection_for_current_ticket(
            page, snapshot, receipt, submitted_here=submitted_here, prior_submission=prior_submission)
        if rejected:
            self.event('ticket_lookup_rejected', rejected)
            if await recover_vianney_consulta(self.runner, page, self.key, self.profile, receipt):
                return DONE
            raise ValueError(rejected + ' La consulta de comprobantes no devolvió un PDF y XML cotejables.')
        return GO
