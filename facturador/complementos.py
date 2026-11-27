"""What one merchant's portal needs beyond the general engine, kept out of the engine.

The runner solves any portal by itself: it reads the screen, plans it, remembers what
worked and guards the emission. A few merchants had rules of their own written inside
the runner's loop. Each of them is now a «complemento»: one object per attempt, with
its own state, that the runner calls at fixed points of its loop. A portal without one
(any unknown portal, any portal the person typed) runs on the general engine alone.

The rule for whoever changes this: what is new goes in the general engine; what belongs
to one merchant lives only in its complement. A new portal never gets a complement.

FACTURADOR_COMPLEMENTOS=0 turns every complement off: all merchants, those listed in
``REGISTRY`` too, go through the general engine only.
"""
from __future__ import annotations

import importlib
import os
import sys
from dataclasses import dataclass

# Catalog key -> «module:class» of its complement. This table is the only place that
# relates a merchant to code; the runner never names one.
REGISTRY = {
    'bp': 'complemento_bp:BPComplement',
    'capufe': 'complemento_capufe:CapufeComplement',
    'tesoro': 'complemento_tesoro:TesoroComplement',
    'vianney': 'complemento_vianney:VianneyComplement',
    'walmart': 'complemento_walmart:WalmartComplement',
}

# How a pass of the runner's loop goes on after a hook.
PROCEED = 'proceed'    # nothing to do here: the engine continues the same pass
OBSERVE = 'observe'    # the page changed or must be read again: the pass ends and the next one starts
FINISH = 'finish'      # the attempt is over (the invoice was recovered or the person took over)


@dataclass(frozen=True)
class Signal:
    """What a hook answers: how the pass goes on and what it leaves for the engine.

    A hook that has to stop the ticket raises ``ValueError`` with the message for the
    person, exactly as the engine does.
    """
    then: str = PROCEED
    error: str | None = None       # what the model reads as the last error on its next consultation
    before: tuple | None = None    # (address, snapshot) before something the complement did by itself
    note: str | None = None        # the person's answer to a question the complement asked
    active: bool = False           # the ticket moved: the count of idle steps starts again
    pending: tuple = ()            # ``before_fiscal_choices``: fiscal fields of its own form still unmatched
    action: object = None          # ``before_fiscal_choices``: the complement's own next action


GO = Signal()
DONE = Signal(FINISH)


def observe(**left) -> Signal:
    """End this pass and read the page again, leaving ``left`` for the engine."""
    return Signal(OBSERVE, **left)


class View:
    """What the runner shows a complement: the page as just read and, once decided, the action about to run."""

    def __init__(self, recent: list):
        self.recent = recent     # the runner's log of the attempt; a complement adds what it did by itself
        self.page = None
        self.snapshot = ''
        self.unsent = True       # no emission was attempted or seen yet in this attempt
        self.action = None
        self.name = ''           # visible name of the control the action points at

    def read(self, page, snapshot: str, unsent: bool) -> None:
        self.page, self.snapshot, self.unsent = page, snapshot, unsent
        self.action, self.name = None, ''

    def act(self, action, name: str, unsent: bool) -> None:
        self.action, self.name, self.unsent = action, name, unsent


class Complement:
    """One merchant's own rules during one attempt. This one has none: the engine works alone.

    The hooks are listed in the order the runner calls them on each pass. Those that
    answer a ``Signal`` may let the pass go on (``GO``), ask for a new reading
    (``observe``), end the attempt (``DONE``) or raise ``ValueError``.
    """

    # After the merchant's public lookup said «not billed yet», the ticket may be prepared
    # again by the agent. Nothing is emitted without the person on that second round.
    prepares_after_negative_check = False

    def __init__(self, runner, key: str, profile, receipt, auto_issue: bool):
        self.runner, self.key, self.profile, self.receipt, self.auto_issue = runner, key, profile, receipt, auto_issue

    def event(self, kind: str, detail: str) -> None:
        self.runner.store.event(self.key, kind, detail)

    async def start(self, job: dict) -> None:
        """Once, before the browser opens: what the complement needs from the ticket's record."""

    async def after_reading(self, view: View) -> Signal:
        """Right after each reading of the page: notices to close, rejections to detect, recoveries."""
        return GO

    async def before_fiscal_choices(self, view: View) -> Signal:
        """Before an emission, ahead of the engine's own fiscal lists: the merchant's form, one checked step per pass.

        It may leave ``pending`` (its fiscal fields still unmatched) and ``action``: its own
        next action, which then skips the memory and the model but not the emission gate.
        """
        return GO

    async def after_fiscal_choices(self, view: View) -> Signal:
        """Before an emission, once the engine's fiscal lists are settled."""
        return GO

    async def after_ticket_lookup(self, view: View) -> Signal:
        """Before an emission, once the engine filled what it knows of the ticket: the merchant's own lookup."""
        return GO

    def adjust_risk(self, view: View, risk: str) -> str:
        """The risk of the action about to run, as this portal means it."""
        return risk

    async def check_action(self, view: View) -> Signal:
        """Its own check of that action, before the engine's general rules for it."""
        return GO

    async def before_acting(self, view: View) -> Signal:
        """After those rules: hold the action, or reach its destination by a safer route."""
        return GO

    def prepares_issue(self, view: View) -> bool:
        """The action reads like an emission but only opens the merchant's own confirmation."""
        return False

    def known_preparation(self, view: View) -> bool:
        """The action is a known preparation step of this portal while the person watches the ticket."""
        return False

    async def prove_preparation(self, view: View) -> Signal:
        """Only when ``prepares_issue``: the form shows the ticket and the profile before that confirmation opens."""
        return GO

    async def before_issue(self, view: View) -> Signal:
        """At the emission gate, before the engine's own checks: the merchant's proofs."""
        return GO

    async def issue_proof(self, view: View) -> bool | None:
        """Whether the page proves this emission by the merchant's own evidence.

        ``None`` means the complement has no proof of its own and the engine's general one
        decides (recipient RFC, ticket folio or code and amount on the page).
        """
        return None

    async def before_recording(self, view: View) -> Signal:
        """Last look before the engine records the emission attempt and clicks."""
        return GO

    async def after_acting(self, view: View) -> None:
        """Right after an action ran without error."""

    def after_logging(self, view: View) -> None:
        """After the action, done or failed, was written to the ticket's log."""

    @staticmethod
    async def recover_documents(runner, key: str, profile, receipt, notes: list) -> bool:
        """Outside any attempt: the merchant's own read-only search for an invoice already issued."""
        return False


def enabled() -> bool:
    return os.getenv('FACTURADOR_COMPLEMENTOS', '1') != '0'


def complement_class(provider: str) -> type[Complement]:
    """The complement of a catalog key; the plain one, which does nothing, without one or with them off."""
    entry = REGISTRY.get(provider) if enabled() else None
    if entry is None:
        return Complement
    if isinstance(entry, str):
        module, _, name = entry.partition(':')
        entry = getattr(importlib.import_module('.' + module, __package__), name)
    return entry


def moved(name: str):
    """A helper the engine module used to hold, served from the complement that owns it now.

    A few tests still import or patch those names in the engine module. Each complement
    lists its own in ``ENGINE_NAMES``.
    """
    if not name.startswith('__'):
        for entry in REGISTRY.values():
            if isinstance(entry, str):
                module = importlib.import_module('.' + entry.partition(':')[0], __package__)
                if name in getattr(module, 'ENGINE_NAMES', ()):
                    return getattr(module, name)
    raise AttributeError(f"module '{__package__}.runner' has no attribute {name!r}")


def patched(name: str, own):
    """The helper to call: what a test put under that name in the engine module, else the complement's own."""
    engine = sys.modules.get(__package__ + '.runner')
    return vars(engine).get(name, own) if engine is not None else own
