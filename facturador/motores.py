"""Several tickets in their portals at once: one main engine and a few helpers.

Each engine is a whole ``Runner`` with its own browser profile, viewer and lock, so the code
that issues invoices keeps working on one ticket at a time. This module only decides which
engine takes a ticket, which engine has each ticket and which engine the panel shows. With
one engine (the default) there are no helpers and every request goes to the main one as before.

Only the main engine uses WhatsApp, the personal browser and the mail. A helper opens its
browser when it gets a ticket and closes it after resting for a while. Two tickets of the
same portal never run at the same time: its session, its account and its registration
suppose a single visitor.
"""
from __future__ import annotations

import asyncio
import functools
import itertools
import logging
import os
import threading
import time
import weakref
from contextlib import asynccontextmanager

from .catalog import CATALOG, provider_for_url, public_portal_origin
from .db import now
from .profile_lock import close_context
from .runner import Runner
from .viewer import CAPTCHA_IDLE_SECONDS

log = logging.getLogger(__name__)

SETTING = 'FACTURADOR_TICKETS_A_LA_VEZ'
MOST = 4
# A helper without a ticket nor a pending step closes its browser after this long (seconds).
# A page kept to read the result of an uncertain ticket is protected for longer.
IDLE_GRACE = 60
PARKED_GRACE = 900
# How often whoever waits here looks again when nobody tells it that something changed.
TICK = 1.0
# The panel's choice of the main browser holds this long while its page is still opening.
CHOICE_GRACE = 30
# A revision number sent to the panel is the viewer's own times SLOTS plus the number of its
# engine, so a number of one screen never fits another.
SLOTS = 8
SAME_PORTAL = 'Esperando a que termine otro ticket del mismo portal.'
IN_LINE = 'En cola'
OFF_LINE = 'Retirado de la cola.'
BROWSERS_BUSY = 'Esperando a que se libere el navegador.'
RESTED = 'Se cerró el navegador en reposo de este trámite. El expediente conserva el resultado.'
HELD = 'Devuelve el control de la página antes de cambiar de trámite.'
PAGE_ASKS = 'Resuelve el aviso o selector de archivo antes de cambiar de página.'
GONE = 'Ese trámite ya no está en un navegador.'
PORTAL_AT_WORK = 'Hay un trámite en curso en ese portal. Espera a que termine para abrirlo por tu cuenta.'
_told_accounts: list[bool] = []


def tickets_at_once(env=None, *, accounts: bool = False) -> int:
    """How many tickets may be in their portals at the same time: 1 to 4, and 1 unless it is asked for.

    With several accounts each one keeps a single engine: the limit of browsers there is
    shared among the accounts (``instancias.BrowserPool``).
    """
    env = os.environ if env is None else env
    text = (env.get(SETTING) or '').strip()
    if accounts:
        if text not in {'', '1'} and not _told_accounts:
            _told_accounts.append(True)  # Once is enough: every account that opens comes through here.
            log.warning('%s no aplica con varias cuentas: cada cuenta trabaja un ticket a la vez.', SETTING)
        return 1
    if not text:
        return 1
    if text not in {str(number) for number in range(1, MOST + 1)}:
        log.warning('%s debe ser un número de 1 a %d. Se trabaja un ticket a la vez.', SETTING, MOST)
        return 1
    return int(text)


def portal_keys(receipt: dict) -> frozenset[str]:
    """What identifies the portal of a ticket: its catalog entry and every public origin it starts from."""
    provider = receipt.get('provider') or ''
    origins = [receipt.get('portal_url') or '']
    keys = set()
    if provider in CATALOG:
        keys.add('catalogo:' + provider)
        origins += [CATALOG[provider]['url'], *('https://' + host for host in CATALOG[provider].get('hosts', []))]
    keys.update(public_portal_origin(url) for url in origins)
    keys.discard(None)
    return frozenset(keys)


class ShownViewer:
    """The ticket viewer of the engine the panel shows, for the tools that act on the page in front of the person."""

    def __init__(self, engines: 'Engines'):
        self._engines = engines

    @property
    def revision(self) -> int:
        engines = self._engines
        engine = engines.focused()
        return engine.viewer.revision * SLOTS + engines.all.index(engine)

    def __getattr__(self, name):
        return getattr(self._engines.focused().viewer, name)


class Engines:
    def __init__(self, main: Runner, limit: int = 1, *, clock=time.monotonic, build=None):
        """``limit`` engines in all. ``build(suffix)`` makes a helper; by default another Runner on the same data."""
        self.main = main
        self.store = main.store
        self.limit = limit if type(limit) is int and 1 <= limit <= MOST else 1
        self.clock = clock
        self.idle_grace, self.parked_grace = IDLE_GRACE, PARKED_GRACE
        build = build or (lambda suffix: Runner(main.store, main.plan, profile_suffix=suffix))
        self.helpers: list[Runner] = [build(f'_{number}') for number in range(2, self.limit + 1)]
        self.closing = False
        # Tickets the store has queued that no engine took yet, oldest first: key -> how to run it.
        self._line: dict[str, tuple] = {}
        self._noted: dict[str, str] = {}           # what each ticket in the line was last told
        self._portals: dict[str, frozenset] = {}   # portal of each ticket in the line or in an engine
        self._inside: dict[str, list] = {}         # tickets at work on their portal: key -> [portal, times]
        self._last: dict[str, Runner] = {}         # the engine that last worked on each portal
        self._waiters: set[asyncio.Future] = set()
        self._followed = weakref.WeakSet()
        self._retries = asyncio.Lock()
        self._watch: asyncio.Task | None = None
        self._resting: dict[Runner, float] = {}    # since when each helper rests with its browser open
        # What the panel shows: the engine in front, what the person chose and which tickets
        # already waited for the person when that choice was made. The panel's state is read
        # from a worker thread, so these are looked at and changed under their own lock.
        self._sight = threading.RLock()
        self._focus = main
        self._choice: tuple | None = None
        self._choice_wait = 0.0
        self._seen: list = []
        self._since: dict[int, tuple] = {}
        self._order = itertools.count()
        if self.helpers:
            # One conversation and one visitor per portal for all of them; alone, the main engine keeps its own ways.
            turn = asyncio.Lock()
            for engine in self.all:
                engine.dispatch = self._place
                engine.portal_turn = functools.partial(self._portal_turn, engine)
                engine.chat_turn = turn
                engine.memory = main.memory

    @property
    def all(self) -> list[Runner]:
        return [self.main, *self.helpers]

    @property
    def several(self) -> bool:
        return bool(self.helpers)

    # ------------------------------------------------------------ who has each ticket

    @staticmethod
    def _held(engine) -> set[str]:
        """The tickets an engine has: at work, waiting for the person or in its own line."""
        held = {*engine.job_tasks, *engine.pending}
        if engine.active_id:
            held.add(engine.active_id)
        return held

    def owner(self, key: str):
        """The engine that has this ticket, or None."""
        for engine in self.all:
            if key in engine.job_tasks or key in engine.pending or engine.active_id == key:
                return engine
        return None

    def of(self, key: str):
        """Where a request about a ticket goes: the engine that has it, or the main one when nobody does."""
        return self.owner(key) or self.main

    def _kept(self, key: str):
        """The engine that keeps a page of this ticket to read its result later, or None."""
        for engine in self.all:
            if any(owner == key and not page.is_closed() for page, owner in list(engine.page_job.items())):
                return engine
        return None

    def has_active_jobs(self) -> bool:
        return bool(self._line) or any(engine.has_active_jobs() for engine in self.all)

    def busy_with(self, key: str) -> bool:
        """The ticket is in the line or in an engine: its files are not touched meanwhile."""
        return key in self._line or any(key in engine.job_tasks for engine in self.all)

    def active_tickets(self) -> list[str]:
        """The tickets at work right now, the one the panel shows first."""
        engines = self.all
        if self.helpers:
            shown = self.focused()
            engines = [shown, *(engine for engine in engines if engine is not shown)]
        return [engine.active_id for engine in engines if engine.active_id]

    def open_tickets(self) -> list:
        """Every ticket the panel must keep in sight: at work, waiting for the person or in the line."""
        keys = []
        for engine in self.all:
            keys += [engine.active_id, *engine.pending]
        return [*keys, *self._line]

    def in_progress(self) -> list[str]:
        """Every ticket in an engine or in the line, those at work first."""
        keys = self.active_tickets()
        for engine in self.all:
            keys += [key for key in self._held(engine) if key not in keys]
        return [*keys, *(key for key in self._line if key not in keys)]

    def waiting_for_person(self) -> int:
        """How many tickets are stopped until the person steps in."""
        with self._sight:
            return len({key for _, key, _ in self._waits()})

    @property
    def active_id(self):
        """The ticket at work in the engine the panel shows."""
        return self.focused().active_id

    def waiting_kind(self, key: str):
        """What a ticket waits for from the person, if anything."""
        return self.of(key).pending.get(key, {}).get('kind')

    def retry_action(self, row: dict):
        return self.of(row['id']).retry_action(row)

    # ------------------------------------------------------------ portals

    def _keys(self, key: str) -> frozenset:
        found = self._portals.get(key)
        if found is None:
            try:
                found = portal_keys(self.store.get(key)['receipt'])
            except KeyError:
                found = frozenset()
            self._portals[key] = found
        return found

    def _portal_engine(self, keys: frozenset, apart: str | None = None):
        """The engine that has another ticket of one of these portals, or None.

        The invoicing operators a portal handed its ticket to count as that ticket's portal too.
        """
        if not keys:
            return None
        for engine in self.all:
            for other in self._held(engine):
                if other != apart and keys & (self._keys(other) | engine.job_origins.get(other, set())):
                    return engine
        return None

    def _gated(self, keys: frozenset, key: str | None) -> bool:
        """Another ticket is at work on one of these portals."""
        return any(other != key and keys & inside[0] for other, inside in self._inside.items())

    @staticmethod
    def _quiet(engine) -> bool:
        """Nothing of a ticket nor of the person is going on in this engine."""
        viewer = engine.viewer
        return not (engine.job_tasks or engine.pending or engine.active_id or engine.browser_start.locked()
                    or viewer.channel or viewer.dialog or viewer.file_request)

    def _free(self, engine) -> bool:
        return self._quiet(engine) and not engine.lock.locked()

    def free_engine(self):
        """An engine that could take a ticket now, or None."""
        return next((engine for engine in self.all if self._free(engine)), None)

    def _own_portal(self) -> str | None:
        """The portal the person has open by hand in the main browser, while the viewer shows it."""
        main = self.main
        page, viewer = main.portal_page, main.viewer
        if page is None or viewer.page is not page or not viewer.visible or not viewer.manual or page.is_closed():
            return None
        return public_portal_origin(page.url)

    def _engine_for(self, keys: frozenset, key: str | None = None):
        """(engine, '') when one can start a ticket of this portal now; otherwise (None, why it waits)."""
        if self._portal_engine(keys, key) or self._gated(keys, key):
            return None, SAME_PORTAL
        free = [engine for engine in self.all if self._free(engine)]
        if self._own_portal() in keys:
            # The person is on that portal in the main browser: its ticket goes there or waits, never beside it.
            return (self.main, '') if self.main in free else (None, SAME_PORTAL)
        if not free:
            return None, IN_LINE
        # The engine that worked on this portal last still has its session.
        last = next((self._last[portal] for portal in keys if portal in self._last), None)
        return (last if last in free else free[0]), ''

    def waits_for(self, receipt: dict) -> str:
        """Why a ticket of this portal cannot start right now; '' when an engine can take it."""
        if not self.helpers:
            return ''
        self._deal()  # Whoever was already in the line goes first.
        engine, reason = self._engine_for(portal_keys(receipt))
        return '' if engine else SAME_PORTAL if reason == SAME_PORTAL else BROWSERS_BUSY

    def portal_busy(self, receipt: dict) -> bool:
        """Another ticket, or the person by hand, is on the portal of a ticket like this one."""
        return bool(self.helpers) and self._engine_for(portal_keys(receipt))[1] == SAME_PORTAL

    def check_portal_free(self, provider: str | None, url: str | None) -> None:
        """Refuse to open by hand, in the main browser, a portal that a ticket of another engine is working on."""
        if not self.helpers:
            return
        keys = portal_keys({'provider': provider or (provider_for_url(url) if url else '') or '', 'portal_url': url or ''})
        if self._portal_engine(keys) or self._gated(keys, None):
            raise ValueError(PORTAL_AT_WORK)

    def _tell(self, key: str, message: str) -> None:
        """Say on a queued ticket why it is not moving."""
        with self.store.connect() as db:
            db.execute("UPDATE jobs SET message=?,updated=? WHERE id=? AND status='queued' AND message<>?",
                       (message, now(), key, message))

    def _note(self, key: str, message: str) -> None:
        """The same for a ticket in the line, written once each time its reason changes."""
        if self._noted.get(key) != message:
            self._noted[key] = message
            self._tell(key, message)

    @asynccontextmanager
    async def _portal_turn(self, engine, key: str):
        """Hold a ticket of this engine while another ticket is at work on the same portal.

        Tickets given out by ``_deal`` never wait here; a retry or a recovery that went straight
        to an engine may. The same ticket may come in again while it is still inside (a check
        that goes on to prepare it).
        """
        keys = self._keys(key)
        told = False
        while self._gated(keys, key):
            if not told:
                told = True
                self._tell(key, SAME_PORTAL)
                self.store.event(key, 'portal_wait', SAME_PORTAL)
            await self._next_change()
        inside = self._inside.setdefault(key, [keys, 0])
        inside[1] += 1
        for portal in keys:
            self._last.pop(portal, None)
            self._last[portal] = engine
        while len(self._last) > 400:
            del self._last[next(iter(self._last))]
        try:
            yield
        finally:
            inside[1] -= 1
            if inside[1] <= 0 and self._inside.get(key) is inside:
                del self._inside[key]
            self._signal()

    # ------------------------------------------------------------ the line

    def schedule(self, *args, **options):
        """Queue a reviewed ticket as ``Runner.schedule`` does; among several engines the first free one starts it."""
        self.main.schedule(*args, **options)

    def _place(self, key: str, profile, auto_issue: bool = False, manual: bool = False) -> None:
        """Take a ticket the store just queued: a free engine starts it now, or it waits here for one."""
        if self.closing:
            return
        self._line[key] = (profile, auto_issue, manual)
        self._deal()
        if key in self._line:
            self._wake()

    def _deal(self) -> None:
        """Hand every waiting ticket that can start to an engine, oldest first.

        Nothing here awaits, so two rounds never see the same ticket nor the same free engine.
        """
        if self.closing:
            return
        for key in list(self._line):
            engine, reason = self._engine_for(self._keys(key), key)
            if engine is None:
                self._note(key, reason)
                continue
            # The engine has the ticket before it leaves the line: whoever looks always finds it somewhere.
            engine.start_queued(key, *self._line[key])
            del self._line[key]
            self._noted.pop(key, None)
            self._follow(engine)
        for key in [key for key in self._portals if key not in self._line and key not in self._inside
                    and self.owner(key) is None]:
            del self._portals[key]

    def _follow(self, engine) -> None:
        """Know when the tickets of an engine end, to give the next one its turn at once."""
        for task in list(engine.job_tasks.values()):
            if task not in self._followed:
                self._followed.add(task)
                task.add_done_callback(self._ended)
        self._wake()

    def _ended(self, _task) -> None:
        self._signal()
        try:
            self._deal()
        except Exception:
            log.exception('No se pudo repartir la fila de tickets')

    def _signal(self) -> None:
        waiters, self._waiters = self._waiters, set()
        for waiter in waiters:
            if not waiter.done():
                waiter.set_result(None)

    async def _next_change(self) -> None:
        """Until something ends or is released, or a moment passes: whoever waits looks again."""
        waiter = asyncio.get_running_loop().create_future()
        self._waiters.add(waiter)
        try:
            await asyncio.wait_for(waiter, TICK)
        except asyncio.TimeoutError:
            pass
        finally:
            self._waiters.discard(waiter)

    def _wake(self) -> None:
        """Keep an eye on the line and on the helpers' browsers while there is something to look at."""
        if self.closing or (self._watch is not None and not self._watch.done()):
            return
        try:
            self._watch = asyncio.get_running_loop().create_task(self._watcher())
        except RuntimeError:
            pass  # No loop is running yet: the next request starts it.

    @staticmethod
    def _browser_up(engine) -> bool:
        """The engine holds a browser or the driver that runs it (which outlives a browser that closed by itself)."""
        return engine.context is not None or engine.playwright is not None

    def _watching(self) -> bool:
        return bool(self._line) or any(self._browser_up(engine) or self._held(engine) for engine in self.helpers)

    async def _watcher(self) -> None:
        while not self.closing and self._watching():
            await self._next_change()
            if self.closing:
                return
            try:
                self._deal()
                await self.rest_helpers()
            except Exception:
                log.exception('No se pudo atender la fila de tickets')

    # ------------------------------------------------------------ requests about one ticket

    async def retry_job(self, key: str, *args, automatic: bool = False, **options):
        """Retry or recover a ticket where it already is; a ticket nobody has goes to a free engine."""
        if automatic:
            options['automatic'] = True
        if not self.helpers:
            return await self.main.retry_job(key, *args, **options)
        async with self._retries:
            row = self.store.get(key)
            untouched = {'id': key, 'status': row['status'], 'action': 'active', 'message': row['message']}
            if key in self._line:
                return untouched
            engine = self.owner(key)
            if engine is None:
                engine = self._kept(key)
                if engine is None:
                    keys = self._keys(key)
                    engine, _ = self._engine_for(keys, key)
                    if engine is None and not automatic:
                        # Nobody can take it now: behind the ticket of its portal, or where the line is shortest.
                        engine = self._portal_engine(keys, key) or min(self.all, key=lambda item: len(item.job_tasks))
                if engine is None or (automatic and not self._free(engine)):
                    return untouched
            try:
                return await engine.retry_job(key, *args, **options)
            finally:
                self._follow(engine)

    async def stop(self, key: str):
        if key in self._line:
            del self._line[key]
            self._noted.pop(key, None)
            self.store.pause_automatic_recovery(key)
            if self.store.get(key)['status'] == 'queued':
                self.store.update(key, status='ready', message=OFF_LINE)
            return
        await self.of(key).stop(key)

    async def continue_job(self, key: str, decision: str):
        await self.of(key).continue_job(key, decision)

    # ------------------------------------------------------------ the chat: one conversation for every engine

    def open_question(self):
        """The ticket whose question the person's next message answers.

        Only one engine at a time asks with a time limit; that question goes first. A ticket
        that already stopped to wait for its answer is answered when no other question is open.
        """
        timed = paused = None
        for engine in self.all:
            for key, future in engine.questions.items():
                if future.done():
                    continue
                if key in engine.pending:
                    paused = paused or key
                else:
                    timed = timed or key
        return timed or paused

    def answer_question(self, key: str, text: str) -> None:
        for engine in self.all:
            if key in engine.questions:
                engine.answer_question(key, text)

    def secret_open(self, key: str) -> bool:
        return any(engine.secret_open(key) for engine in self.all)

    def secret_recent(self):
        """The ticket that asked for a secret in the chat; the one whose question is still open first."""
        recent = [key for key in (engine.secret_recent() for engine in self.all) if key]
        return next((key for key in recent if self.secret_open(key)), recent[0] if recent else None)

    def running_job(self):
        """The ticket a loose chat message goes to: the one the panel shows, or the first one at work."""
        engines = [self.focused(), *self.all] if self.helpers else self.all
        for engine in engines:
            key = engine.running_job()
            if key:
                return key
        return None

    def tell(self, key: str, text: str) -> None:
        self.of(key).tell(key, text)

    # ------------------------------------------------------------ what the panel shows

    def _waits(self) -> list[tuple]:
        """Tickets that wait for the person, the one that has waited longest first: (engine, ticket, what it waits on)."""
        found = []
        for engine in self.all:
            for key, pending in list(engine.pending.items()):
                if not pending['future'].done():
                    found.append((engine, key, pending))
            viewer = engine.viewer
            key = engine.page_job.get(viewer.page) if viewer.page is not None else None
            if viewer.dialog is not None and viewer.visible and key:
                # A native dialog of the portal stops its ticket until the person reads it.
                found.append((engine, key, viewer.dialog))
        since = {}
        for _, _, what in found:
            since[id(what)] = self._since.get(id(what)) or (next(self._order), what)
        self._since = since
        return sorted(found, key=lambda item: since[id(item[2])][0])

    def _chosen(self):
        """The engine the person asked to see, while that still means something."""
        if self._choice is None:
            return None
        kind, what = self._choice
        if kind == 'ticket':
            engine = self.owner(what)
            if engine is None and what not in self._line:
                self._choice = None  # That ticket is over.
            return engine
        screen = what.screen
        if self._held(what) or (screen.visible and screen.open) or self.clock() < self._choice_wait:
            return what
        self._choice = None  # The main browser no longer shows the page that was asked for.
        return None

    def _choose(self, choice: tuple) -> None:
        with self._sight:
            self._choice = choice
            self._choice_wait = self.clock() + CHOICE_GRACE
            self._seen = [what for _, _, what in self._waits()]

    def choose(self, key: str) -> None:
        """The person acted on this ticket from the panel: its engine is the one shown."""
        if self.helpers and (self.owner(key) is not None or key in self._line):
            self._choose(('ticket', key))

    def choose_main(self) -> None:
        """The person asked for the main browser itself: its own pages, WhatsApp or the personal one."""
        if self.helpers:
            self._choose(('engine', self.main))

    def focused(self):
        """The engine whose screen the panel shows.

        A screen the person is acting on stays. Otherwise: the ticket that has waited longest
        for the person, unless the person chose another one after it started to wait; then
        what the person chose; then the main engine, or the first helper at work when the
        main one has no ticket.
        """
        if not self.helpers:
            return self.main
        with self._sight:
            current = self._focus
            screen = current.screen
            if self._leased(screen) or screen.file_request or (screen.dialog and screen.visible):
                return current
            waits = self._waits()
            chosen = self._chosen()
            if chosen is not None and any(all(what is not seen for seen in self._seen) for _, _, what in waits):
                self._choice = chosen = None  # Somebody started to wait for the person after that choice.
            if chosen is None:
                chosen = (waits[0][0] if waits else self.main if self._held(self.main) else
                          next((engine for engine in self.helpers if self._held(engine)), self.main))
            self._focus = chosen
            return chosen

    @property
    def screen(self):
        return self.focused().screen

    @property
    def viewer(self):
        """The ticket viewer the panel's tools act on: the main one, or whichever is shown among several."""
        return ShownViewer(self) if self.helpers else self.main.viewer

    def view_at(self, revision: int):
        """The screen a revision number of the panel refers to, and that number as its viewer counts it.

        (None, 0) when the number belongs to a screen that is not the one shown now.
        """
        if not self.helpers:
            return self.main.screen, revision
        engine = self.focused()
        if revision < 0 or revision % SLOTS != self.all.index(engine):
            return None, 0
        return engine.screen, revision // SLOTS

    def browser_state(self) -> dict:
        if not self.helpers:
            return self.main.browser_state()
        engine = self.focused()
        state = engine.browser_state()
        return {**state, 'revision': state['revision'] * SLOTS + self.all.index(engine)}

    def panel(self) -> list[dict]:
        """What the panel can show: every engine with a ticket, the main browser with a page of its own and the one shown."""
        with self._sight:
            shown = self.focused()
            waiting = {id(engine) for engine, _, _ in self._waits()}
        choices = []
        for number, engine in enumerate(self.all, 1):
            key = engine.active_id or next(iter(list(engine.pending)), None)
            own_page = engine is self.main and engine.screen.visible and engine.screen.open
            if key or own_page or engine is shown:
                choices.append({'n': number, 'job_id': key, 'waiting': id(engine) in waiting, 'shown': engine is shown})
        return choices

    @staticmethod
    def _leased(screen) -> bool:
        """Somebody controls this screen. A lease nobody used for a while (a panel that went away) holds nothing."""
        return bool(screen.channel) and time.monotonic() - screen.channel_activity < CAPTCHA_IDLE_SECONDS

    async def _leave(self, target, channel: str) -> None:
        """Before the panel turns to another engine nothing of the person stays half done on the screen it leaves."""
        current = self.focused()
        if current is target:
            return
        screen = current.screen
        if screen.dialog or screen.file_request:
            raise ValueError(PAGE_ASKS)
        if screen.channel and not (channel and await screen.release(channel)) and self._leased(screen):
            raise ValueError(HELD)

    async def show(self, *, engine: int | None = None, job_id: str | None = None, channel: str = '') -> None:
        """The person picks what the panel shows: a ticket in a browser, or an engine by its number."""
        if not self.helpers:
            return
        if job_id is not None:
            target = self.owner(job_id)
        else:
            target = self.all[engine - 1] if type(engine) is int and 1 <= engine <= len(self.all) else None
        if target is None:
            raise ValueError(GONE)
        await self._leave(target, channel)
        key = job_id or target.active_id
        if key and target is self.main:
            await self.main.return_to_job(channel)  # Its ticket, not the personal browser.
        self._choose(('ticket', key) if key else ('engine', target))

    async def show_main(self, channel: str = '') -> None:
        """The main browser is about to be used by the person: leave the screen of any other engine first."""
        if self.helpers:
            await self._leave(self.main, channel)

    async def release_control(self, channel: str) -> None:
        """Give back the control of whichever screen holds this lease."""
        for engine in self.all:
            await engine.release_control(channel)

    async def abandon(self, channel: str) -> None:
        for engine in self.all:
            await engine.viewer.release(channel)
            await engine.personal.view.release(channel)

    # ------------------------------------------------------------ browsers of the helpers

    async def rest_helpers(self, *, force: bool = False) -> int:
        """Close the browser of every helper that has rested long enough. Returns how many were closed."""
        closed = 0
        moment = self.clock()
        for engine in self.helpers:
            if not self._browser_up(engine) or engine.lock.locked() or engine.downloads or not self._quiet(engine):
                self._resting.pop(engine, None)
                continue
            since = self._resting.setdefault(engine, moment)
            kept = any(not page.is_closed() for page in list(engine.page_job))
            if force or moment - since >= (self.parked_grace if kept else self.idle_grace):
                closed += await self._close_browser(engine)
        return closed

    async def _close_browser(self, engine) -> bool:
        if engine.lock.locked() or not self._quiet(engine):
            return False
        # Nothing is awaited between the check above and this lock, so the engine still rests.
        async with engine.lock:
            kept = {owner for page, owner in list(engine.page_job.items()) if not page.is_closed()}
            context = engine.context
            try:
                await engine.viewer.set_manual(False)
                if context:
                    await close_context(context)
                if engine.context is context:
                    engine.context, engine.engine = None, ''
                driver, engine.playwright = engine.playwright, None
                if driver:
                    await driver.stop()
            except Exception as exc:
                log.warning('No se pudo cerrar un navegador en reposo (%s).', type(exc).__name__)
            for key in kept:
                self.store.event(key, 'browser_closed', RESTED)
        self._resting.pop(engine, None)
        return not self._browser_up(engine)

    async def close(self) -> None:
        """Orderly shutdown of every engine; nothing in the line starts any more."""
        self.closing = True
        self._line.clear()
        self._signal()
        watch, self._watch = self._watch, None
        if watch is not None:
            watch.cancel()
            await asyncio.gather(watch, return_exceptions=True)
        try:
            results = await asyncio.gather(*(engine.close() for engine in self.helpers), return_exceptions=True)
            for result in results:
                if isinstance(result, Exception):
                    log.error('Un navegador auxiliar no cerró limpiamente (%s).', type(result).__name__)
        finally:
            await self.main.close()
