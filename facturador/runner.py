from __future__ import annotations

import asyncio
import hashlib
import html
import ipaddress
import json
import os
import re
from pathlib import Path
from urllib.parse import urlsplit

from .ai import AI
from .auth import ChatGPTPlan, PlanError
from .catalog import CATALOG, allowed_url, provider_for_url
from .cfdi import UUID_RE
from .db import Store
from .documents import reconcile
from .models import Action, Profile, Receipt
from .portals import capufe_proof, normalize
from .viewer import BrowserView

ISSUE = re.compile(r'\b(facturar|emitir|timbrar|generar\s+(?:mi\s+)?(?:factura|cfdi)|solicitar\s+factura|enviar\s+(?:mi\s+)?(?:factura|cfdi)|s[ií] estoy seguro|confirmar (?:la )?emisi[oó]n)\b', re.I)
FORBIDDEN = re.compile(r'\b(pagar|comprar|transferir|cancelar\s+factura|eliminar|borrar)\b', re.I)
TERMS = re.compile(r'acepto|términos|terminos|condiciones|contrato|privacidad', re.I)
CHALLENGE = re.compile(r'captcha|verificaci[oó]n de seguridad|no soy un robot', re.I)
CHALLENGE_TEXT = re.compile(r'no soy un robot|verifica que eres humano|verify (?:that )?you are human|introdu(?:ce|zca) el captcha|c[oó]digo (?:de )?captcha', re.I)
ACCESS_DENIED = re.compile(r'access denied|error 16\b|blocked by our security service|acceso denegado|err_cert_', re.I)
SUCCESS = re.compile(r'factura\s+(?:emitida|generada)\b|cfdi\s+(?:emitido|generado)\b|timbrad[ao]\s+correctamente|resultado facturaci[oó]n[^\n]*\n[\s\S]{0,130}facturas generadas|se gener[oó]\s+exitosamente', re.I)
DOWNLOAD = re.compile(r'descargar|download|\bpdf\b|\bxml\b|imprimir', re.I)
MANUAL = {'captcha', 'login', 'terms', 'unknown', 'blocked', 'confirm', 'attachment'}


def action_risk(action: Action) -> str:
    name = action.target.name if action.target else ''
    if FORBIDDEN.search(name):
        return 'forbidden'
    if action.risk in {'captcha', 'terms', 'login', 'unknown'}:
        return action.risk
    if TERMS.search(name):
        return 'terms'
    if CHALLENGE.search(name):
        return 'captcha'
    if action.kind == 'press' and (action.value or '').lower() in {'enter', 'return'} and name.strip().upper() != 'RFC':
        return 'unknown'
    if ISSUE.search(name) or action.risk == 'issue':
        return 'issue'
    if action.kind == 'click' and re.fullmatch(r'aceptar|confirmar|enviar|submit|s[ií]', name.strip(), re.I):
        return 'confirm'
    return 'normal'


def private_destination(url: str) -> bool:
    hostname = urlsplit(url).hostname or ''
    if hostname and ('.' not in hostname and ':' not in hostname or hostname.endswith(('.localhost', '.local', '.internal'))):
        return True
    try:
        return not ipaddress.ip_address(hostname).is_global
    except ValueError:
        return False


async def visible_challenge(page, snapshot: str) -> str | None:
    if any(CHALLENGE_TEXT.search(line) and not ('checkbox' in line and '[checked]' in line)
           for line in snapshot.splitlines()):
        return 'captcha'
    if hasattr(page, 'frames'):
        if await page.locator('input[type="password"]:visible').count():
            return 'login'
        frames = page.locator('iframe[src*="/bframe"]:visible, iframe[title*="challenge" i]:visible, iframe[title*="desafío" i]:visible')
        if await frames.count():
            return 'captcha'
        for frame in page.frames[1:]:
            host = urlsplit(frame.url).hostname or ''
            if host in {'www.google.com', 'www.recaptcha.net', 'recaptcha.net'} and '/recaptcha/' in frame.url:
                checked = frame.locator('[role="checkbox"][aria-checked="false"]:visible')
                if await checked.count():
                    return 'captcha'
        if await page.locator('input[type="file"][required]:invalid:visible').count():
            return 'attachment'
    return None


class Runner:
    def __init__(self, store: Store, plan: ChatGPTPlan | None = None):
        self.store = store
        self.plan = plan or ChatGPTPlan(store.root)
        self.owns_plan = plan is None
        self.viewer = BrowserView()
        self.context = None
        self.playwright = None
        self.lock = asyncio.Lock()
        self.browser_start = asyncio.Lock()
        self.pending: dict[str, dict] = {}
        self.job_tasks: dict[str, asyncio.Task] = {}
        self.downloads: set[asyncio.Task] = set()
        self.page_job = {}
        self.active_id: str | None = None
        self.whatsapp = None
        self.portal_page = None
        self.manual_download_error = ''

    async def start_browser(self):
        async with self.browser_start:
            if self.context:
                return
            from playwright.async_api import async_playwright

            if self.playwright is not None:
                await self.playwright.stop()
                self.playwright = None
            settings = {'user_data_dir': str(self.store.root / 'browser_profile'), 'headless': True,
                        'accept_downloads': True, 'chromium_sandbox': True, 'service_workers': 'block',
                        'viewport': {'width': 1280, 'height': 850}, 'timeout': 15_000}
            executable = os.getenv('FACTURADOR_CHROMIUM_PATH')
            if executable:
                settings['executable_path'] = executable
            try:
                self.playwright = await async_playwright().start()
                self.context = await self.playwright.chromium.launch_persistent_context(**settings)
            except Exception as exc:
                if self.playwright is not None:
                    await self.playwright.stop()
                    self.playwright = None
                raise ValueError('No se pudo iniciar Chromium con su aislamiento. Revisa la instalación o inicia con Docker Compose.') from exc
            self.context.set_default_timeout(7000)
            self.context.on('close', lambda _: setattr(self, 'context', None))
            self.context.on('page', self.register_page)
            for page in self.context.pages:
                self.register_page(page)
            await self.context.route('**/*', self.guard_navigation)

    def register_page(self, page):
        def on_download(download):
            key = self.page_job.get(page)
            if key or (page is self.portal_page and page is self.viewer.page and self.viewer.manual):
                task = asyncio.create_task(self.save_download(download, key))
                self.downloads.add(task)
                task.add_done_callback(self.downloads.discard)

        async def on_popup(popup):
            key = self.page_job.get(page)
            if key and key == self.active_id:
                self.page_job[popup] = key
                await self.viewer.show(popup, self.viewer.title, manual=self.viewer.manual)
            elif self.viewer.page is page and self.viewer.manual and not self.has_active_jobs():
                if page is self.portal_page:
                    self.portal_page = popup
                await self.viewer.show(popup, self.viewer.title, manual=True)

        async def on_dialog(dialog):
            await self.viewer.receive_dialog(page, dialog)

        async def on_file_request(chooser):
            if any(allowed_url(page.url, key) for key in CATALOG) or urlsplit(page.url).netloc == 'web.whatsapp.com':
                await self.viewer.receive_file_request(page, chooser)

        page.on('download', on_download)
        page.on('filechooser', on_file_request)
        page.on('framenavigated', lambda frame: self.viewer.invalidate_file_request(page, frame))
        page.on('popup', on_popup)
        page.on('dialog', on_dialog)
        page.on('close', lambda _: self.page_job.pop(page, None))

    async def guard_navigation(self, route):
        request = route.request
        if urlsplit(request.url).scheme not in {'https', 'blob'} or private_destination(request.url):
            await route.abort()
            return
        if request.is_navigation_request():
            try:
                main_frame = request.frame == request.frame.page.main_frame
            except Exception:
                main_frame = True
            if main_frame and self.active_id:
                provider = self.store.get(self.active_id)['receipt']['provider']
                if not allowed_url(request.url, provider):
                    self.store.event(self.active_id, 'navigation_blocked', 'Navegación fuera del portal registrado')
                    await route.abort()
                    return
            elif main_frame and not (any(allowed_url(request.url, key) for key in CATALOG)
                                     or urlsplit(request.url).netloc == 'web.whatsapp.com'):
                await route.abort()
                return
        await route.continue_()

    async def save_download(self, download, key: str | None):
        directory = self.store.root / 'downloads' / key if key else self.store.root / 'descargas-navegador'
        directory.mkdir(parents=True, exist_ok=True, mode=0o700)
        original = Path(download.suggested_filename.replace('\\', '/')).name
        suffix = Path(original).suffix.lower()
        if suffix not in {'.pdf', '.xml', '.zip'}:
            await download.cancel()
            return
        stem = re.sub(r'[^\w.()-]', '_', Path(original).stem) or 'comprobante'
        # Bound the UTF-8 byte length as well as the character count for the filesystem.
        name = stem.encode('utf-8')[:145].decode('utf-8', errors='ignore') + suffix
        from tempfile import NamedTemporaryFile

        with NamedTemporaryFile(dir=directory, prefix='pending_', delete=False) as stream:
            temporary = Path(stream.name)
        try:
            await download.save_as(temporary)
            if temporary.stat().st_size > 50 * 1024 * 1024:
                raise ValueError('Descarga supera 50 MB')
            digest = hashlib.sha256(temporary.read_bytes()).hexdigest()[:16]
            temporary.replace(directory / (digest + '_' + name))
            if key:
                self.store.event(key, 'download', name)
            else:
                self.manual_download_error = ''
        except Exception:
            message = 'La descarga del comprobante no terminó.'
            if key:
                self.store.event(key, 'download_error', message)
            else:
                self.manual_download_error = message
        finally:
            temporary.unlink(missing_ok=True)

    def has_active_jobs(self) -> bool:
        return bool(self.job_tasks)

    async def open_portal(self, provider: str | None = None, url: str | None = None):
        if url is not None:
            provider = provider or provider_for_url(url)
            if provider is None or not allowed_url(url, provider):
                raise ValueError('La liga del ticket debe pertenecer a un portal HTTPS del catálogo.')
        if provider is not None and provider not in CATALOG:
            raise ValueError('Selecciona un portal del catálogo.')
        if self.has_active_jobs():
            raise ValueError('Hay un trámite en curso en el navegador.')
        async with self.lock:
            if self.has_active_jobs():
                raise ValueError('Hay un trámite en curso en el navegador.')
            await self.start_browser()
            if self.portal_page is None or self.portal_page.is_closed():
                self.portal_page = await self.context.new_page()
            page = self.portal_page
            await self.viewer.set_manual(False)
            if provider is None:
                await page.goto('about:blank')
                links = [f'<li><a href="{html.escape(item["url"], quote=True)}">{html.escape(item["name"])}</a></li>'
                         for item in sorted(CATALOG.values(), key=lambda item: item['name'].casefold())]
                template = (Path(__file__).resolve().parent.parent / 'web' / 'inicio-navegador.html').read_text(encoding='utf-8')
                await page.set_content(template.replace('<!--PORTALES-->', '\n'.join(links)))
                await self.viewer.show(page, 'Portales de facturación', manual=True)
            else:
                item = CATALOG[provider]
                try:
                    await page.goto(url or item['url'], wait_until='domcontentloaded', timeout=30_000)
                except Exception as exc:
                    await self.viewer.show(page, item['name'], manual=True)
                    raise ValueError(f'No se pudo cargar {item["name"]}. Revisa la página en el navegador.') from exc
                await self.viewer.show(page, item['name'], manual=True)

    async def open_whatsapp(self):
        if self.has_active_jobs():
            raise ValueError('Termina la facturación en curso para abrir WhatsApp.')
        await self.start_browser()
        if self.whatsapp is None or self.whatsapp.is_closed():
            self.whatsapp = await self.context.new_page()
            await self.whatsapp.goto('https://web.whatsapp.com/', wait_until='domcontentloaded')
        await self.viewer.show(self.whatsapp, 'WhatsApp Web', manual=True)

    async def capture_whatsapp_media(self) -> list[dict]:
        from .ingest import save_upload

        page = self.whatsapp
        if self.has_active_jobs() or not page or page.is_closed() or urlsplit(page.url).hostname != 'web.whatsapp.com':
            raise ValueError('Abre WhatsApp y selecciona una foto en el visor.')
        await self.viewer.set_manual(False)
        try:
            buttons = page.get_by_role('button', name=re.compile(r'^(Descargar|Download)$'))
            if await buttons.count() != 1 or not await buttons.is_visible():
                raise ValueError('Abre la foto de un ticket en WhatsApp y vuelve a importar.')
            async with page.expect_download(timeout=30000) as info:
                await buttons.click()
            download = await info.value
            path = Path(await download.path())
            if path.stat().st_size > 25 * 1024 * 1024:
                raise ValueError('La foto supera 25 MB.')
            return await asyncio.to_thread(save_upload, self.store, download.suggested_filename, path.read_bytes(), 'WhatsApp')
        finally:
            await self.viewer.set_manual(True)

    def schedule(self, key: str, profile: Profile, auto_issue: bool = False):
        if not self.store.queue(key, profile):
            raise ValueError('Solo se puede encolar un ticket revisado y sin emisión previa.')
        task = asyncio.create_task(self.run(key, profile, auto_issue))
        self.job_tasks[key] = task
        task.add_done_callback(lambda _: self.job_tasks.pop(key, None))

    async def stop(self, key: str):
        task = self.job_tasks.get(key)
        if not task:
            raise ValueError('El ticket ya no está en ejecución.')
        queued = self.store.get(key)['status'] == 'queued'
        task.cancel()
        await asyncio.gather(task, return_exceptions=True)
        if queued:
            self.store.update(key, status='ready', message='Retirado de la cola.')

    async def human(self, key: str, page, kind: str, message: str):
        future = asyncio.get_running_loop().create_future()
        self.pending[key] = {'kind': kind, 'future': future, 'page': page}
        await self.viewer.set_manual(kind in MANUAL)
        self.store.update(key, status='needs_user', message=message)
        self.store.event(key, 'needs_user', kind)
        try:
            if await future == 'cancel':
                raise ValueError('Proceso detenido por el usuario.')
        finally:
            await self.viewer.set_manual(False)
            self.pending.pop(key, None)
        self.store.update(key, status='running', message='Continuando en el portal…')

    def decide(self, key: str, decision: str):
        pending = self.pending.get(key)
        if not pending or pending['future'].done():
            raise ValueError('La intervención ya no está activa.')
        pending['future'].set_result(decision)

    async def continue_job(self, key: str, decision: str):
        pending = self.pending.get(key)
        if not pending or pending['future'].done():
            raise ValueError('La intervención ya no está activa.')
        kind = pending['kind']
        if decision != 'cancel':
            if (kind == 'issue') != (decision == 'approve'):
                raise ValueError('Revisa la acción que solicita el panel.')
            if self.viewer.dialog:
                raise ValueError('Responde primero al aviso del portal.')
            if kind in {'captcha', 'login', 'attachment'}:
                page = self.viewer.page
                if await visible_challenge(page, await page.locator('body').aria_snapshot()) == kind:
                    message = 'Todavía falta el archivo que pide el portal.' if kind == 'attachment' else 'El portal todavía muestra la verificación.'
                    raise ValueError(message + ' Completa el paso y pulsa Continuar.')
            if kind == 'plan' and self.plan.status()['status'] != 'ready':
                raise ValueError('Conecta ChatGPT o revisa el cupo antes de continuar.')
        await self.viewer.set_manual(False)
        self.decide(key, decision)

    @staticmethod
    def locator(page, target):
        if target is None or not target.name.strip():
            raise ValueError('Falta la etiqueta visible de la acción.')
        if target.by == 'role':
            if not target.role:
                raise ValueError('Falta el rol del elemento.')
            return page.get_by_role(target.role, name=target.name, exact=target.exact)
        method = {'label': 'get_by_label', 'text': 'get_by_text', 'placeholder': 'get_by_placeholder'}[target.by]
        return getattr(page, method)(target.name, exact=target.exact)

    async def execute(self, page, action: Action, receipt: Receipt):
        if action.kind == 'goto':
            if not allowed_url(action.value or '', receipt.provider):
                raise ValueError('Destino fuera del portal registrado.')
            await page.goto(action.value, wait_until='domcontentloaded')
            return
        if action.kind == 'wait':
            await asyncio.sleep(1)
            return
        if action.kind == 'scroll':
            await page.mouse.wheel(0, max(-1800, min(1800, int(action.value or '600'))))
            return
        locator = self.locator(page, action.target)
        if await locator.count() != 1:
            raise ValueError('La etiqueta no identifica un único control visible.')
        if action.kind == 'click':
            await locator.click()
        elif action.kind == 'fill':
            await locator.fill((action.value or '')[:1000])
        elif action.kind == 'select':
            await locator.select_option(action.value or '')
        elif action.kind == 'press' and action.value in {'Enter', 'Tab', 'ArrowDown', 'ArrowUp', 'Escape', 'Space'}:
            await locator.press(action.value)
        else:
            raise ValueError('Tipo de acción o tecla no admitida.')

    async def recover(self, key: str, profile: Profile) -> bool:
        files = [path for path in (self.store.root / 'downloads' / key).rglob('*')
                 if path.is_file() and 'extracted' not in path.parts and not path.name.startswith('pending_')]
        if not files:
            return False
        try:
            await asyncio.to_thread(reconcile, self.store, key, profile, files)
        except ValueError:
            return False
        return True

    async def run(self, key: str, profile: Profile, auto_issue: bool = False):
        async with self.lock:
            if not self.store.claim(key):
                return
            committed = issued = False
            page = None
            try:
                receipt = Receipt(**self.store.get(key)['receipt'])
                errors = receipt.ready_errors()
                if errors or receipt.provider not in CATALOG:
                    raise ValueError(' · '.join(errors) or 'Portal sin configurar.')
                ai = AI(self.plan)
                provider = CATALOG[receipt.provider]
                await self.start_browser()
                page = await self.context.new_page()
                self.active_id = key
                self.page_job[page] = key
                await self.viewer.show(page, provider['name'])
                arrival = await page.goto(provider['url'], wait_until='domcontentloaded')
                prompt = (Path(__file__).resolve().parent.parent / 'PROMPT_FACTURADOR.md').read_text(encoding='utf-8')
                instructions = prompt + '\n' + json.dumps({'perfil': profile.model_dump(), 'ticket': receipt.model_dump(),
                                                          'portal': provider}, ensure_ascii=False)
                recent = []
                last_error = ''
                for number in range(65):
                    page = self.viewer.page
                    status = self.store.get(key)['status']
                    if status in {'verified', 'duplicate', 'historical'}:
                        return
                    if page.is_closed() or not allowed_url(page.url, receipt.provider):
                        raise ValueError('El portal se cerró o cambió a un destino fuera del catálogo.')
                    snapshot = await page.locator('body').aria_snapshot()
                    if ACCESS_DENIED.search(snapshot):
                        raise ValueError('El portal rechazó el acceso a este navegador. El ticket quedó pendiente.')
                    if number == 0 and arrival and arrival.status in {403, 429} and not await visible_challenge(page, snapshot):
                        raise ValueError(f'El portal respondió HTTP {arrival.status}. El ticket quedó pendiente.')
                    issued = issued or bool(SUCCESS.search(snapshot))
                    if issued:
                        self.store.update(key, status='issued_waiting_files', message='Emisión confirmada. Recuperando PDF y XML.')
                        uuids = set(value.upper() for value in UUID_RE.findall(snapshot))
                        if len(uuids) == 1 and not self.store.get(key)['uuid']:
                            self.store.update(key, uuid=next(iter(uuids)))
                        if self.downloads:
                            await asyncio.wait(list(self.downloads), timeout=2)
                        if await self.recover(key, profile):
                            return
                    challenge = await visible_challenge(page, snapshot)
                    if challenge:
                        message = {'captcha': 'Completa la verificación en este navegador.',
                                   'login': 'Inicia sesión en el portal y regresa al trámite.',
                                   'attachment': 'Toma el control y abre el selector del portal. Elige el documento en el panel y pulsa Adjuntar al portal.'}[challenge]
                        await self.human(key, page, challenge, message)
                        continue
                    observation = json.dumps({'url': page.url, 'paso': number, 'emision_intentada': committed,
                                              'emision_visible': issued, 'acciones_recientes': recent[-6:],
                                              'ultimo_error': last_error, 'estado_visible': snapshot[:26_000]}, ensure_ascii=False)
                    try:
                        image = await page.screenshot(type='jpeg', quality=76, scale='css')
                        action = await asyncio.to_thread(ai.next_action, instructions, observation, image)
                    except PlanError as exc:
                        if not issued and not committed and exc.status in {'limit', 'disconnected', 'reconnect', 'identity_only', 'model'}:
                            await self.human(key, page, 'plan', str(exc))
                            continue
                        raise
                    risk = action_risk(action)
                    name = action.target.name if action.target else ''
                    if risk == 'forbidden':
                        raise ValueError('La acción no corresponde a solicitar el CFDI de esta compra.')
                    if action.kind == 'done':
                        if not issued:
                            if committed and await self.recover(key, profile):
                                return
                            raise ValueError('No hay comprobante ni confirmación visible. Recupera el resultado antes de volver a intentar.')
                        if self.downloads:
                            await asyncio.wait(list(self.downloads), timeout=12)
                        await self.recover(key, profile)
                        return
                    if risk in MANUAL or action.kind == 'human':
                        await self.human(key, page, risk if risk in MANUAL else 'unknown', 'Revisa el paso pendiente en el navegador.')
                        continue
                    preparing_capufe = (receipt.provider == 'capufe' and normalize(name) == 'FACTURAR CONCEPTOS' and
                                       action.kind == 'click' and not committed and not issued and
                                       urlsplit(page.url).path.rstrip('/').lower() == '/capufe/facturacionrapida' and
                                       await capufe_proof(page, profile, receipt))
                    if risk == 'issue' and not preparing_capufe:
                        if committed or issued:
                            raise ValueError('Ya hubo un intento de emisión. Consulta o recupera ese CFDI.')
                        if action.kind != 'click' or not action.target.exact:
                            last_error = 'Para emitir, elige el botón o enlace exacto de la confirmación.'
                            continue
                        valid = auto_issue and normalize(name) in {'SI ESTOY SEGURO', 'SÍ ESTOY SEGURO'} and await capufe_proof(page, profile, receipt)
                        if not valid:
                            approved_at = (page.url, snapshot)
                            await self.human(key, page, 'issue', 'Revisa ticket, importe, forma de pago y datos fiscales en el portal para aprobar esta emisión.')
                            if approved_at != (page.url, await page.locator('body').aria_snapshot()):
                                last_error = 'La página cambió durante la revisión. Lee el formulario nuevamente.'
                                continue
                        self.store.begin_emission(key, name)
                        committed = True
                    elif committed or issued:
                        if action.kind not in {'wait', 'scroll'} and not (action.kind == 'click' and DOWNLOAD.search(name)):
                            if action.kind == 'click' and re.fullmatch(r'continuar', name.strip(), re.I) and not issued:
                                await self.human(key, page, 'confirm', 'Revisa la confirmación que muestra el portal y completa ese paso si coincide con tu ticket.')
                                continue
                            raise ValueError('Revisa o recupera la factura del intento actual.')
                    try:
                        await self.execute(page, action, receipt)
                        last_error = ''
                    except Exception:
                        if committed:
                            raise ValueError('El portal no confirmó el resultado del intento de emisión. Busca el CFDI antes de repetir.')
                        last_error = 'No se completó el paso. Revisa la pantalla y el control.'
                    recent.append({'tipo': action.kind, 'control': name[:120]})
                    self.store.event(key, 'action', action.kind + ' · ' + name[:120])
                    if len(recent) >= 4 and recent[-4:].count(recent[-1]) == 4:
                        await self.human(key, page, 'blocked', 'El portal no avanzó tras varios pasos. Toma el control para revisar.')
                raise ValueError('La factura requiere revisar el resultado del portal.')
            except asyncio.CancelledError:
                self.store.update(key, status='issued_waiting_files' if issued else 'uncertain' if committed else 'blocked',
                                  message='Trámite detenido. Revisa el estado antes de volver a preparar el ticket.')
                raise
            except Exception as exc:
                if self.store.get(key)['status'] not in {'verified', 'duplicate'}:
                    status = 'issued_waiting_files' if issued else 'uncertain' if committed else 'blocked'
                    message = str(exc)[:450] if isinstance(exc, ValueError) else 'El navegador no completó el trámite. Revisa el portal.'
                    self.store.update(key, status=status, message=message)
                    self.store.event(key, 'stopped', message)
            finally:
                self.active_id = None
                await self.viewer.set_manual(False)

    async def close(self):
        jobs = list(self.job_tasks.values())
        for task in jobs:
            task.cancel()
        await asyncio.gather(*jobs, return_exceptions=True)
        if self.viewer.dialog:
            await self.viewer.answer(self.viewer.dialog_key, False)
        if self.context:
            await self.context.close()
        if self.downloads:
            await asyncio.gather(*self.downloads, return_exceptions=True)
        if self.playwright:
            await self.playwright.stop()
        if self.owns_plan:
            self.plan.close()
