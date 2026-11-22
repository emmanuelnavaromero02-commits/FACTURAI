"""Operator-owned Chrome profile. No ticket runner or model receives its pages."""
import asyncio
import os
import secrets
from pathlib import Path
from urllib.parse import urlsplit

from playwright.async_api import Error as BrowserError

from .catalog import public_portal_origin, resolves_publicly
from .profile_lock import close_context, launch_chrome
from .viewer import BrowserView


class PersonalBrowser:
    MAX_TABS = 12

    def __init__(self, root: Path, on_download):
        self.root = root
        self.context = None
        self.view = BrowserView()
        # A stale frame/input request from the fiscal viewer must never target a personal tab.
        self.view.revision = 1 << 40
        self.tabs = {}
        self.lock = asyncio.Lock()
        self.on_download = on_download
        self.closing = False

    async def start(self, playwright, engine):
        if self.context:
            return
        options = dict(accept_downloads=True, service_workers='block', viewport={'width': 1280, 'height': 850},
                       timeout=15_000)
        if engine == 'Firefox':
            launch = playwright.firefox.launch_persistent_context
            options.update(headless=True, firefox_user_prefs={'dom.serviceWorkers.enabled': True})
            directory = 'browser_personal_firefox'
        else:
            chrome = engine == 'Google Chrome'
            launch = playwright.chromium.launch_persistent_context
            options.update(headless=not chrome, chromium_sandbox=True,
                           executable_path='/usr/bin/google-chrome-stable' if chrome else os.getenv('FACTURADOR_CHROMIUM_PATH'))
            directory = 'browser_personal_chrome' if chrome else 'browser_personal_chromium'
        try:
            if engine == 'Google Chrome':
                context = await launch_chrome(launch, self.root / directory, **options)
            else:
                context = await launch(self.root / directory, **options)
        except BrowserError as exc:
            raise ValueError('No se pudo abrir el perfil personal del navegador.') from exc
        try:
            await context.route('**/*', self.guard_request)
            await context.route_web_socket('**/*', self.guard_websocket)
        except Exception:
            await close_context(context)
            raise
        self.context = context
        context.set_default_timeout(7000)
        context.on('close', lambda _: self._closed(context))
        context.on('page', self._register)
        for page in context.pages:
            self._register(page)

    def _closed(self, context):
        if self.context is context:
            self.context = None
            self.tabs.clear()
            self.view.page = None
            self.view.channel = ''
            self.view.dialog = None
            self.view.file_request = None
            self.view.revision += 1

    @staticmethod
    async def guard_request(route):
        parts = urlsplit(route.request.url)
        # Large Google asset and OAuth query strings can exceed the operator's address-bar limit.
        # Only the origin determines eligibility. Let Chrome keep the complete request unchanged.
        if await resolves_publicly(f'{parts.scheme}://{parts.netloc}'):
            await route.continue_()
        else:
            await route.abort()

    @staticmethod
    async def guard_websocket(socket):
        parts = urlsplit(socket.url)
        if parts.scheme == 'wss' and await resolves_publicly('https://' + parts.netloc):
            socket.connect_to_server()
        else:
            await socket.close()

    def _register(self, page):
        if page in self.tabs.values():
            return
        if len(self.tabs) >= self.MAX_TABS:
            asyncio.create_task(page.close())
            return
        tab_id = secrets.token_hex(8)
        self.tabs[tab_id] = page
        async def popup(child):
            if self.view.page is page and child in self.tabs.values() and not self.view.dialog:
                await self.select(child)
        async def closed(_):
            self.tabs.pop(tab_id, None)
            if self.view.page is page and not self.closing and self.context:
                self.view.dialog = None
                self.view.dialog_key = ''
                await self.select(next(iter(self.tabs.values()), None))
        page.on('popup', popup)
        page.on('download', self.on_download)
        page.on('close', closed)
        page.on('dialog', lambda dialog: self.view.receive_dialog(page, dialog))
        page.on('filechooser', lambda chooser: self.view.receive_file_request(page, chooser))
        page.on('framenavigated', lambda frame: self.view.invalidate_file_request(page, frame))

    async def select(self, page):
        # Bring only the personal tab to the front of its own Chrome window.
        if self.closing:
            return
        if page is not None:
            try:
                await page.bring_to_front()
            except BrowserError:
                if self.closing or not self.context or page.is_closed():
                    return
                raise
        await self.view.show(page, 'Navegador personal', manual=page is not None)

    async def close(self):
        async with self.lock:
            self.closing = True
            try:
                if self.view.dialog:
                    await self.view.answer(self.view.dialog_key, False)
                if self.context:
                    await close_context(self.context)
            finally:
                self.closing = False

    def check_control(self, channel):
        if self.view.dialog:
            raise ValueError('Resuelve el aviso de la página antes de cambiar de pestaña.')
        if self.view.channel and not secrets.compare_digest(self.view.channel, channel):
            raise ValueError('La página está bajo el control de otra ventana del panel.')

    @property
    def interacting(self):
        return bool(self.view.channel or self.view.dialog or self.view.file_request)

    async def open(self, url=None):
        page = self.view.page
        if page is None or page.is_closed():
            page = next((p for p in self.tabs.values() if not p.is_closed()), None)
            if page is None:
                page = await self.context.new_page()
            await self.select(page)
        if url:
            if not public_portal_origin(url) or not await resolves_publicly(url):
                raise ValueError('Introduce una dirección HTTPS de un sitio público.')
            try:
                await page.goto(url, wait_until='domcontentloaded', timeout=25_000)
            except BrowserError as exc:
                raise ValueError('Chrome no pudo abrir esa dirección. Comprueba el sitio e inténtalo otra vez.') from exc

    async def change_tab(self, action, tab_id):
        if action == 'new':
            if len(self.tabs) >= self.MAX_TABS:
                raise ValueError('Cierra una pestaña para abrir otra.')
            await self.select(await self.context.new_page())
            return
        page = self.tabs.get(tab_id)
        if page is None or page.is_closed():
            raise ValueError('Esta pestaña ya se cerró.')
        if action == 'select':
            await self.select(page)
        else:
            # Keep one page in the persistent context: closing the last window may exit Chrome.
            if len(self.tabs) == 1:
                await self.select(await self.context.new_page())
            elif self.view.page is page:
                await self.select(next(p for p in self.tabs.values() if p is not page))
            await page.close()
            self.tabs.pop(tab_id, None)

    def state(self):
        state = self.view.state()
        tabs = []
        for key, page in list(self.tabs.items()):
            if page.is_closed():
                continue
            parsed = urlsplit(page.url)
            address = parsed._replace(query='', fragment='').geturl() if public_portal_origin(f'{parsed.scheme}://{parsed.netloc}') else ''
            tabs.append(dict(id=key, url=address, title=parsed.hostname or 'Nueva pestaña', selected=page is self.view.page))
        return {**state, 'url': next((t['url'] for t in tabs if t['selected']), ''),
                'mode': 'personal', 'tabs': tabs, 'job_id': None}
