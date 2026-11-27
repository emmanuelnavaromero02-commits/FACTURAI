"""One private application per account, started on first use and closed when it rests.

The entrance never looks inside a customer's data. It starts the application with that account's
folder, runs its lifespan and decides when it may sleep. A shared limit bounds how many browsers
are open at once: a ticket that finds them all busy waits for its turn instead of failing.
"""
from __future__ import annotations

import asyncio
import logging
import re
import sqlite3
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path

from . import secretos
from .profile_lock import close_context

log = logging.getLogger(__name__)

OWNER = re.compile(r'[0-9a-f]{16}')
BROWSER_PATHS = ('/api/browser/', '/api/whatsapp')
WAITING = ('En espera de turno: los navegadores están ocupados con otros trámites. '
           'Tu ticket sigue en la fila y continuará solo.')
TURN = 'Llegó tu turno. Abriendo el portal.'
BUSY = 'Los navegadores están ocupados con otros trámites. Intenta de nuevo en unos minutos.'
YIELDED = 'Se cerró el navegador para dar turno a otro trámite. El expediente conserva el resultado.'
CLOSING = 'El servicio se está reiniciando. Intenta de nuevo en un momento.'
UNOPENED = 'No se pudo abrir tu espacio de trabajo. Intenta de nuevo en un momento.'
# Tickets in these states wait for the person or are finished: nothing runs for them in the background.
RESTING = ('verified', 'duplicate', 'attached', 'historical', 'blocked')
# A ticket that waits for its files is followed by mail and portal checks for at most this long.
WATCH_DAYS = 4


class Unavailable(Exception):
    """The instance cannot serve right now; the text is for the person."""


class Instance:
    def __init__(self, owner: str, root: Path, app, clock):
        self.owner, self.root, self.app, self.clock = owner, root, app, clock
        self.state: dict = {}
        self.requests = 0
        self.last_used = clock()
        # When someone last looked at this browser, and when a ticket was last seen running in it.
        self.last_browser = self.last_job = float('-inf')
        self.retiring = False
        self.opening = 0
        self.gate = asyncio.Lock()  # Serialises opening this browser with handing its place to another customer.
        self._inbox: asyncio.Queue = asyncio.Queue()
        self._task: asyncio.Task | None = None

    @property
    def runner(self):
        return self.app.state.runner

    @property
    def browser_open(self) -> bool:
        return self.runner.context is not None or self.runner.personal.context is not None

    # ------------------------------------------------------------ lifespan

    async def start(self) -> None:
        """Run the application's start-up exactly as a server would."""
        started = asyncio.get_running_loop().create_future()

        async def send(message):
            if message['type'] == 'lifespan.startup.complete' and not started.done():
                started.set_result(None)
            elif message['type'] == 'lifespan.startup.failed' and not started.done():
                started.set_exception(RuntimeError('lifespan'))

        scope = {'type': 'lifespan', 'asgi': {'version': '3.0', 'spec_version': '2.0'}, 'state': self.state}
        self._task = asyncio.create_task(self.app(scope, self._inbox.get, send))
        await self._inbox.put({'type': 'lifespan.startup'})
        await asyncio.wait({started, self._task}, return_when=asyncio.FIRST_COMPLETED)
        if not started.done() or started.exception():
            started.cancel()
            await asyncio.gather(self._task, return_exceptions=True)
            self._task = None
            raise Unavailable(UNOPENED)

    async def stop(self) -> None:
        """Orderly shutdown: the application closes its workers, browser and connections itself."""
        task, self._task = self._task, None
        if task is None:
            return
        await self._inbox.put({'type': 'lifespan.shutdown'})
        # Waiting this way, a caller that is cancelled does not cut the application's shutdown short.
        await asyncio.wait({task})
        if not task.cancelled() and task.exception():
            log.error('Una instancia no cerró limpiamente (%s).', type(task.exception()).__name__)

    # ------------------------------------------------------------ activity

    def touch(self, path: str = '') -> None:
        now = self.clock()
        self.last_used = now
        if path.startswith(BROWSER_PATHS) or '/whatsapp' in path:
            self.last_browser = now
        if self.runner.has_active_jobs():
            self.last_job = now

    def work(self) -> str:
        """Why this instance must stay awake, or '' when nothing runs and nothing is pending."""
        try:
            runner, automation, store = self.runner, self.app.state.automation, self.app.state.store
            viewer = runner.viewer
            if runner.has_active_jobs() or runner.lock.locked() or runner.pending or runner.active_id or runner.downloads:
                return 'tramite'
            if viewer.channel or viewer.dialog or viewer.file_request or runner.personal.interacting:
                return 'navegador'
            if automation.reads:
                return 'lectura'
            automatic = int(bool(automation.enabled))
            old = (datetime.now(timezone.utc) - timedelta(days=WATCH_DAYS)).isoformat()
            marks = ','.join('?' * len(RESTING))
            with store.connect() as db:
                if db.execute('SELECT 1 FROM chat_queue LIMIT 1').fetchone():
                    return 'cola'
                if db.execute(f"""SELECT 1 FROM jobs WHERE NOT (status IN ({marks})
                        OR (status='external' AND issue_attempted=0)
                        OR (status IN ('issued_waiting_files','uncertain','external') AND updated<?)
                        OR (?=0 AND status IN ('new','ready','review') AND auto_stage NOT LIKE 'reading:%')
                        OR (?=1 AND status='review' AND auto_stage IN ('','review'))) LIMIT 1""",
                              (*RESTING, old, automatic, automatic)).fetchone():
                    return 'tickets'
                if (db.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='whatsapp_followups'").fetchone()
                        and db.execute("""SELECT 1 FROM whatsapp_followups f JOIN jobs j ON j.id=f.job_id
                            WHERE f.enabled=1 AND j.status<>'verified' LIMIT 1""").fetchone()):
                    return 'seguimiento'
            return ''
        except (AttributeError, sqlite3.Error):
            return 'desconocido'  # Never close on a guess.

    # ------------------------------------------------------------ browser

    def parked_pages(self) -> list:
        return [page for page in self.runner.page_job if not page.is_closed()]

    def _in_use(self) -> bool:
        runner = self.runner
        viewer = runner.viewer
        return bool(runner.has_active_jobs() or runner.lock.locked() or runner.browser_start.locked()
                    or runner.pending or runner.active_id or runner.downloads
                    or viewer.channel or viewer.dialog or viewer.file_request
                    or runner.personal.interacting or runner.personal.lock.locked())

    def _quiet(self, now: float, grace: float, parked_grace: float) -> bool:
        if now - self.last_browser < grace:
            return False  # Someone was looking at this browser a moment ago.
        # A page kept to read the result of an uncertain ticket is protected for longer after its ticket.
        return not self.parked_pages() or now - max(self.last_browser, self.last_job) >= parked_grace

    def browser_idle(self, now: float, grace: float, parked_grace: float) -> bool:
        """True when closing this browser interrupts nobody: no ticket, no person, no pending step."""
        return not (self.opening or self.gate.locked() or self._in_use()) and self._quiet(now, grace, parked_grace)

    async def close_browser(self, now: float, grace: float, parked_grace: float, released) -> bool:
        """Close this idle browser, as «Reiniciar navegador» does, so another customer's ticket can use its place."""
        runner = self.runner
        if not self.browser_idle(now, grace, parked_grace):
            return False
        # Nothing is awaited between the check above and these locks, so the browser is still idle.
        async with self.gate, runner.lock:
            waiting = {runner.page_job[page] for page in self.parked_pages()}
            context = runner.context
            try:
                # First, and at once: it takes the personal lock that «Navegación libre» also uses.
                await runner.personal.close()
                await runner.viewer.set_manual(False)
                if context:
                    await close_context(context)
                if runner.context is context:
                    runner.context, runner.engine = None, ''
                driver, runner.playwright = runner.playwright, None
                if driver:
                    await driver.stop()
            except Exception as exc:
                log.warning('No se pudo cerrar un navegador en reposo (%s).', type(exc).__name__)
            for key in waiting:
                runner.store.event(key, 'browser_closed', YIELDED)
            if self.browser_open:
                return False
            released(self)
            return True

    def announce(self, message: str, kind: str) -> None:
        """Tell the ticket in progress, in its own record, why it is not moving."""
        key = self.runner.active_id
        if key:
            try:
                self.runner.store.running_message(key, message)
                self.runner.store.event(key, kind, message)
            except sqlite3.Error:
                pass


class BrowserPool:
    """At most ``limit`` instances hold an open browser at the same time."""

    def __init__(self, limit: int, *, clock=time.monotonic, grace: float = 60, parked_grace: float = 900, poll: float = 2):
        self.limit = limit
        self.clock = clock
        self.grace, self.parked_grace, self.poll = grace, parked_grace, poll
        self.holders: dict[Instance, float] = {}
        self.queue: list[Instance] = []  # Tickets waiting for their turn, oldest first.
        self._changed = asyncio.Event()

    def release(self, instance: Instance) -> None:
        if self.holders.pop(instance, None) is not None:
            self._changed.set()

    def _prune(self) -> None:
        for instance in [item for item in self.holders if not item.browser_open and not item.opening]:
            del self.holders[instance]  # Its browser closed on its own (restart, crash or shutdown).

    async def _reclaim(self, requester: Instance) -> bool:
        now = self.clock()
        for item in self.holders:
            if item.runner.has_active_jobs():
                item.last_job = now
        idle = [item for item in self.holders
                if item is not requester and item.browser_idle(now, self.grace, self.parked_grace)]
        for victim in sorted(idle, key=lambda item: (bool(item.parked_pages()), max(item.last_browser, item.last_job))):
            if await victim.close_browser(now, self.grace, self.parked_grace, self.release):
                return True
        return False

    async def acquire(self, instance: Instance, *, wait: bool) -> None:
        """Take a place. A ticket waits for its turn; any other request is told to come back later."""
        queued = False
        try:
            while True:
                self._prune()
                if instance in self.holders:
                    return
                turn = self.queue[0] is instance if queued else not self.queue
                if turn and len(self.holders) >= self.limit:
                    await self._reclaim(instance)
                if turn and len(self.holders) < self.limit:
                    self.holders[instance] = self.clock()
                    if queued:
                        instance.announce(TURN, 'browser_turn')
                    return
                if not wait:
                    raise ValueError(BUSY)
                if not queued:
                    queued = True
                    self.queue.append(instance)
                    instance.announce(WAITING, 'browser_wait')
                self._changed.clear()
                try:
                    await asyncio.wait_for(self._changed.wait(), self.poll)
                except asyncio.TimeoutError:
                    pass
        finally:
            if queued:
                self.queue.remove(instance)
                self._changed.set()

    def guard(self, instance: Instance) -> None:
        """Make this instance ask for a place before it opens its browser."""
        runner = instance.runner
        original = runner.start_browser

        async def start_browser():
            # Only the ticket itself waits in line; a click in the panel gets an answer right away.
            waits = asyncio.current_task() in runner.job_tasks.values()
            while True:
                instance.opening += 1
                try:
                    await self.acquire(instance, wait=waits)
                    async with instance.gate:
                        if instance not in self.holders:
                            continue  # The place was handed over meanwhile: ask again.
                        if waits:
                            instance.last_job = self.clock()
                        return await original()
                finally:
                    instance.opening -= 1
                    if not instance.opening and not instance.browser_open:
                        self.release(instance)

        runner.start_browser = start_browser


class Instances:
    def __init__(self, data_root: Path, factory, *, port: int, public_origin: str, max_browsers: int = 3,
                 idle_seconds: float = 30 * 60, clock=time.monotonic, pool: BrowserPool | None = None):
        self.folder = Path(data_root) / 'usuarios'
        self.factory = factory
        self.port, self.public_origin = port, public_origin
        self.idle_seconds = idle_seconds
        self.clock = clock
        self.browsers = pool or BrowserPool(max_browsers, clock=clock)
        self.closing = False
        self._live: dict[str, Instance] = {}
        self._locks: dict[str, asyncio.Lock] = {}

    def root(self, owner: str) -> Path:
        # Owners are identifiers made by this program, never text typed by someone.
        if not isinstance(owner, str) or not OWNER.fullmatch(owner):
            raise Unavailable(UNOPENED)
        return self.folder / owner

    def live(self, owner: str) -> Instance | None:
        return self._live.get(owner)

    def _lock(self, owner: str) -> asyncio.Lock:
        return self._locks.setdefault(owner, asyncio.Lock())

    async def enter(self, owner: str, path: str = '') -> Instance:
        """The running instance of this account, created now if needed and counted as in use."""
        root = self.root(owner)
        async with self._lock(owner):
            if self.closing:
                raise Unavailable(CLOSING)
            instance = self._live.get(owner)
            if instance is None:
                instance = await self._create(owner, root)
                if self.closing:
                    await self._stop(instance)
                    raise Unavailable(CLOSING)
                self._live[owner] = instance
            elif instance.retiring:
                # The account is active again before its instance went away: put its workers back.
                instance.retiring = False
                instance.app.state.whatsapp.watcher.start()
                instance.app.state.automation.start()
            instance.requests += 1
            instance.touch(path)
            return instance

    def leave(self, instance: Instance) -> None:
        instance.requests -= 1
        instance.touch()

    async def _create(self, owner: str, root: Path) -> Instance:
        try:
            self.folder.mkdir(parents=True, exist_ok=True, mode=0o700)
            root.mkdir(exist_ok=True, mode=0o700)
            secretos.register(root, owner)
            app = self.factory(root, port=self.port, public_origin=self.public_origin, lazy_browser=True)
            instance = Instance(owner, root, app, self.clock)
            self.browsers.guard(instance)
        except Exception as exc:
            log.error('No se pudo crear una instancia (%s).', type(exc).__name__)
            raise Unavailable(UNOPENED) from None
        await instance.start()
        return instance

    async def _stop(self, instance: Instance) -> None:
        try:
            await instance.stop()
        finally:
            self.browsers.release(instance)

    def _closable(self, instance: Instance, now: float) -> bool:
        if instance.requests:
            return False
        if instance.retiring:
            # A ticket in the browser may finish. One that waits for the person, or for its turn, will not go on.
            return (not instance.runner.has_active_jobs() or bool(instance.runner.pending)
                    or instance in self.browsers.queue)
        return now - instance.last_used >= self.idle_seconds and not instance.work()

    async def sweep(self) -> list[str]:
        """Close the instances that rested long enough with nothing running or pending."""
        closed = []
        for owner, instance in list(self._live.items()):
            now = self.clock()
            if instance.runner.has_active_jobs():
                instance.last_job = now
            if not self._closable(instance, now):
                continue
            async with self._lock(owner):
                if self._live.get(owner) is not instance or not self._closable(instance, self.clock()):
                    continue
                del self._live[owner]
                await self._stop(instance)
                closed.append(owner)
        return closed

    async def retire(self, owner: str) -> None:
        """The account was turned off: start nothing new and close as soon as no ticket is running."""
        async with self._lock(owner):
            instance = self._live.get(owner)
            if instance is None:
                return
            instance.retiring = True
            try:
                await instance.app.state.automation.close()
                await instance.app.state.whatsapp.watcher.close()
            except Exception as exc:
                log.error('No se detuvieron las tareas de una cuenta desactivada (%s).', type(exc).__name__)
            if self._closable(instance, self.clock()):
                del self._live[owner]
                await self._stop(instance)

    async def close(self) -> None:
        """Orderly shutdown of every instance."""
        self.closing = True
        instances, self._live = list(self._live.values()), {}
        await asyncio.gather(*(self._stop(instance) for instance in instances), return_exceptions=True)
