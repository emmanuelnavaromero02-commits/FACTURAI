from __future__ import annotations

import asyncio
import re
import secrets
from dataclasses import dataclass
from typing import TYPE_CHECKING, Literal
from urllib.parse import urlsplit

from pydantic import BaseModel, ConfigDict, Field

if TYPE_CHECKING:
    from playwright.async_api import FileChooser, Frame

KEY = re.compile(r'(?:Backspace|Delete|Tab|Enter|Escape|Home|End|PageUp|PageDown|Arrow(?:Up|Down|Left|Right)|Space|(?:ControlOrMeta|Control)\+[aAzZyY])')


class ViewerInput(BaseModel):
    model_config = ConfigDict(extra='forbid')
    channel: str = Field(max_length=64)
    kind: Literal['down', 'move', 'up', 'wheel', 'text', 'key']
    x: float = Field(default=0, ge=0, le=1)
    y: float = Field(default=0, ge=0, le=1)
    delta: int = Field(default=0, ge=-1200, le=1200)
    value: str = Field(default='', max_length=2000)


@dataclass(frozen=True)
class PortalUpload:
    id: str
    chooser: FileChooser
    frame: Frame
    origin: str


class BrowserView:
    def __init__(self):
        self.page = None
        self.revision = 0
        self.title = ''
        self.manual = False
        self.channel = ''
        self.pointer_down = False
        self.dialog = None
        self.dialog_key = ''
        self.file_request: PortalUpload | None = None
        self.commands = asyncio.Lock()
        self.capture = asyncio.Lock()

    @property
    def open(self):
        return self.page is not None and not self.page.is_closed()

    def state(self) -> dict:
        address = urlsplit(self.page.url) if self.open else None
        url = address._replace(query='', fragment='').geturl() if address else ''
        return {'open': self.open, 'revision': self.revision, 'label': self.title,
                'url': url, 'manual': self.manual and self.open,
                'upload': {'id': self.file_request.id, 'origin': self.file_request.origin} if self.file_request else None,
                'dialog': {'id': self.dialog_key, 'type': self.dialog.type, 'text': self.dialog.message[:400]}
                if self.dialog else None}

    async def show(self, page, title: str, *, manual=False):
        async with self.commands:
            await self._release()
            self.page = page
            self.title = title
            self.manual = manual
            self.revision += 1

    async def set_manual(self, enabled: bool):
        async with self.commands:
            await self._release()
            self.manual = enabled
            self.revision += 1

    async def _release(self):
        self.channel = ''
        self.file_request = None
        if self.pointer_down and self.open and not self.dialog:
            try:
                await self.page.mouse.up()
            except Exception:
                pass
        self.pointer_down = False

    async def take_control(self, revision: int) -> str:
        async with self.commands:
            if not self.open or not self.manual or revision != self.revision or self.dialog:
                raise ValueError('Espera a que el navegador quede disponible para intervenir.')
            await self._release()
            self.channel = secrets.token_urlsafe(24)
            return self.channel

    async def release(self, channel: str):
        async with self.commands:
            if channel and secrets.compare_digest(channel, self.channel):
                await self._release()

    async def frame(self, revision: int) -> bytes | None:
        if not self.open or revision != self.revision or self.dialog or self.capture.locked():
            return None
        async with self.capture:
            page = self.page
            try:
                frame = await page.screenshot(type='jpeg', quality=68, scale='css', timeout=3000)
                return frame if revision == self.revision and page is self.page else None
            except Exception:
                return None

    async def send(self, request: ViewerInput):
        async with self.commands:
            self.require_control(request.channel)
            page = self.page
            if request.kind in {'down', 'move', 'up', 'wheel'}:
                viewport = page.viewport_size
                if not viewport:
                    raise ValueError('El navegador todavía está ajustando el tamaño de la página.')
                x = request.x * (viewport['width'] - 1)
                y = request.y * (viewport['height'] - 1)
                await page.mouse.move(x, y)
                if request.kind == 'down' and not self.pointer_down:
                    await page.mouse.down()
                    self.pointer_down = True
                elif request.kind == 'up' and self.pointer_down:
                    await page.mouse.up()
                    self.pointer_down = False
                elif request.kind == 'wheel':
                    await page.mouse.wheel(0, request.delta)
            elif request.kind == 'text' and request.value:
                await page.keyboard.insert_text(request.value)
            elif request.kind == 'key' and KEY.fullmatch(request.value):
                await page.keyboard.press(request.value)

    def require_control(self, channel: str):
        if not self.open or not self.manual or not self.channel or self.dialog or not secrets.compare_digest(channel, self.channel):
            raise ValueError('El control de esta página ya terminó. Vuelve a tomarlo desde el panel.')

    async def visible_text(self, channel: str) -> str:
        async with self.commands:
            self.require_control(channel)
            return (await self.page.locator('body').inner_text(timeout=4000))[:40_000]

    async def screenshot(self, channel: str) -> bytes:
        async with self.commands, self.capture:
            self.require_control(channel)
            return await self.page.screenshot(type='png', scale='css', timeout=6000)

    async def history(self, channel: str, direction: Literal['back', 'forward', 'reload'], *, personal=False):
        async with self.commands:
            if not (personal and self.open and self.manual and not self.channel and not channel and not self.dialog):
                self.require_control(channel)
            navigate = getattr(self.page, {'back': 'go_back', 'forward': 'go_forward', 'reload': 'reload'}[direction])
            try:
                await navigate(wait_until='domcontentloaded', timeout=10_000)
            except Exception as exc:
                raise ValueError('El portal no completó el cambio de página.') from exc

    async def receive_file_request(self, page, chooser):
        channel = self.channel
        if page is not self.page or not channel or not self.manual:
            return
        frame = await chooser.element.owner_frame()
        url = urlsplit(page.url)
        frame_url = urlsplit(frame.url) if frame else None
        if not frame_url or url.scheme != 'https' or (frame_url.scheme, frame_url.netloc) != (url.scheme, url.netloc):
            return
        if channel == self.channel and page is self.page:
            self.file_request = PortalUpload(secrets.token_urlsafe(20), chooser, frame, f'https://{url.netloc}')

    def invalidate_file_request(self, page, frame):
        if page is self.page and self.file_request and (frame is page.main_frame or frame is self.file_request.frame):
            self.file_request = None

    async def attach(self, channel: str, request_id: str, payload: dict | None):
        async with self.commands:
            self.require_control(channel)
            pending = self.file_request
            if not pending or not secrets.compare_digest(request_id, pending.id):
                raise ValueError('El selector ya cambió. Abre Adjuntar en el portal.')
            self.file_request = None
            if payload is None:
                return
            origins = [urlsplit(url) for url in (self.page.url, pending.frame.url)]
            if pending.frame.is_detached() or any(f'{url.scheme}://{url.netloc}' != pending.origin for url in origins):
                raise ValueError('La página que solicitó el archivo ya cambió.')
            try:
                await pending.chooser.set_files(payload, timeout=6000)
            except Exception as exc:
                raise ValueError('El portal cerró el selector de archivos. Ábrelo otra vez.') from exc

    async def receive_dialog(self, page, dialog):
        # The user reads and answers native dialogs in the panel. Never guess a response.
        if page is not self.page or self.dialog is not None:
            await dialog.dismiss()
            return
        self.dialog = dialog
        self.dialog_key = secrets.token_urlsafe(16)

    async def answer(self, key: str, accept: bool):
        dialog = self.dialog
        if not dialog or not secrets.compare_digest(key, self.dialog_key):
            raise ValueError('El aviso ya cambió o se cerró.')
        self.dialog, self.dialog_key = None, ''
        if accept and dialog.type != 'prompt':
            await dialog.accept()
        else:
            await dialog.dismiss()
