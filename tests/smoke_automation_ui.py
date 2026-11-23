"""Seguimiento de tickets en Google Chrome con API y archivos de prueba en memoria."""

import asyncio
import io
from pathlib import Path
from urllib.parse import urlsplit

from PIL import Image
from playwright.async_api import async_playwright, expect


WEB = Path(__file__).resolve().parents[1] / 'web'
ORIGIN = 'http://127.0.0.1:8765'
TICKET_ID = 'prueba-subida'


def ticket(key, merchant, status):
    return {
        'id': key, 'status': status, 'message': '', 'historical': False, 'issue_attempted': False,
        'receipt': {
            'provider': 'portal_ticket', 'portal_url': 'https://comercio.example/facturas',
            'merchant': merchant, 'amount': '10.00', 'date': '2026-11-01', 'payment_code': '01',
            'ticket_number': key, 'source': 'Carga: ticket.jpg', 'billing_codes': [], 'details': '',
        },
    }


class Fixture:
    def __init__(self, duplicate=False):
        self.upload = ticket(TICKET_ID, 'Tienda de prueba', 'new')
        self.duplicate = duplicate
        self.automation = {'enabled': True, 'phase': 'idle', 'message': '', 'current_id': None}
        self.state = {
            'jobs': ([self.upload] if duplicate else []) + [ticket('otro-ticket', 'Otra tienda', 'review')], 'active_job': None,
            'catalog': {}, 'token': 'test-ui', 'plan': {'status': 'ready'}, 'gmail': 'disconnected',
            'profile': {}, 'automation': self.automation,
            'browser': {'open': False, 'revision': 0, 'manual': False, 'engine': 'Google Chrome'},
        }
        self.puts = []
        image = io.BytesIO()
        Image.new('RGB', (64, 48), 'white').save(image, 'JPEG')
        self.picture = image.getvalue()

    async def serve(self, route):
        url = urlsplit(route.request.url)
        path = url.path
        if url.netloc != '127.0.0.1:8765':
            return await route.abort()
        if path == '/api/state':
            return await route.fulfill(json=self.state)
        if path == '/api/automation' and route.request.method == 'PUT':
            enabled = route.request.post_data_json['enabled']
            self.puts.append(enabled)
            self.automation.update(enabled=enabled)
            return await route.fulfill(json=self.automation)
        if path == '/api/upload' and route.request.method == 'POST':
            if not self.duplicate:
                self.state['jobs'].insert(0, self.upload)
                self.automation.update(current_id=TICKET_ID, phase='reading', message='Leyendo con ChatGPT')
            return await route.fulfill(json=[{'id': TICKET_ID, 'created': not self.duplicate,
                                             'status': self.upload['status']}])
        if path == f'/api/jobs/{TICKET_ID}' and route.request.method == 'PUT':
            self.upload['receipt'].update(route.request.post_data_json)
            self.upload.update(status='ready')
            return await route.fulfill(json=self.upload)
        if path == '/api/browser/frame':
            return await route.fulfill(body=self.picture, content_type='image/jpeg')
        asset = WEB / (path.removeprefix('/') or 'index.html')
        if asset.parent == WEB and asset.is_file():
            types = {'.js': 'text/javascript', '.css': 'text/css', '.html': 'text/html'}
            return await route.fulfill(path=asset, content_type=types[asset.suffix])
        return await route.fulfill(status=404, body='Recurso fuera de la prueba')

    def start_portal(self):
        self.upload.update(status='running', message='El portal recibió el ticket de prueba')
        self.automation.update(current_id=TICKET_ID, phase='queued', message='Ticket enviado al navegador')
        self.state.update(active_job=TICKET_ID)
        self.state['browser'].update(open=True, revision=1, job_id=TICKET_ID,
                                     label='Tienda de prueba', url=self.upload['receipt']['portal_url'])


async def exercise(browser, scenario):
    app = Fixture(duplicate=scenario == 'duplicate')
    context = await browser.new_context(service_workers='block')
    errors = []
    try:
        await context.route('**/*', app.serve)
        page = await context.new_page()
        page.on('pageerror', lambda error: errors.append(str(error)))
        page.on('dialog', lambda dialog: dialog.accept())
        await page.goto(ORIGIN + '/#bandeja')
        enabled = page.locator('#automation-enabled')
        await expect(enabled).to_be_checked()
        if scenario == 'manual':
            app.state['jobs'][0]['status'] = 'new'
            app.state['jobs'][0]['message'] = 'Ticket pendiente de leer'
            await enabled.uncheck()
            await expect(page.locator('#tickets').get_by_role('button', name='Leer con ChatGPT', exact=True)).to_be_visible()
            assert app.puts == [False], app.puts
            assert not errors, errors
            return
        if scenario == 'follow':
            async with page.expect_response(lambda response: response.url.endswith('/api/automation')):
                await enabled.uncheck()
            await expect(enabled).not_to_be_checked()
            async with page.expect_response(lambda response: response.url.endswith('/api/automation')):
                await enabled.check()
            await expect(enabled).to_be_checked()

        if scenario == 'duplicate':
            await page.get_by_role('button', name='Por revisar', exact=True).click()
            await page.get_by_role('searchbox', name='Buscar por comercio o folio').fill('emmanuel')
            await expect(page.locator('#tickets')).to_contain_text('Sin tickets con estos filtros')
            await page.locator('#file-upload').set_input_files(
                {'name': 'prueba.jpg', 'mimeType': 'image/jpeg', 'buffer': app.picture}
            )
            await expect(page.locator('#notice')).to_contain_text('Este archivo ya está registrado como Tienda de prueba')
            await expect(page.get_by_role('button', name='Todos', exact=True)).to_have_attribute('aria-pressed', 'true')
            await expect(page.get_by_role('searchbox', name='Buscar por comercio o folio')).to_have_value('')
            await expect(page.locator('#tickets').get_by_role('row').filter(has_text='Tienda de prueba')).to_be_visible()
            assert not errors, errors
            return

        await page.locator('#file-upload').set_input_files(
            {'name': 'prueba.jpg', 'mimeType': 'image/jpeg', 'buffer': app.picture}
        )
        rows = page.locator('#tickets')
        uploaded_row = rows.get_by_role('row').filter(has_text='Tienda de prueba')
        await expect(rows).to_contain_text('Tienda de prueba')
        await expect(page.locator('#automation-status')).to_contain_text('Leyendo con ChatGPT')
        await expect(page).to_have_url(ORIGIN + '/#bandeja')
        if scenario == 'portal':
            app.upload.update(status='review', message='Confirma el portal que aparece en el ticket')
            app.automation.update(current_id=TICKET_ID, phase='review', message=app.upload['message'])
            await uploaded_row.get_by_role('button', name='Revisar portal', exact=True).click()
            portal = page.locator('#receipt-form [name=portal_url]')
            await expect(portal).to_be_focused()
            await portal.fill('https://tienda-correcta.example/facturas')
            await page.get_by_role('button', name='Guardar revisión').click()
            await expect(page.locator('#prepare-dialog')).to_be_visible()
            await expect(page.locator('#prepare-summary')).to_contain_text('https://tienda-correcta.example')
            await page.keyboard.press('Escape')
            await uploaded_row.get_by_role('button', name='Confirmar portal', exact=True).click()
            await expect(page.locator('#prepare-dialog')).to_be_visible()
            await expect(page.locator('#prepare-summary')).to_contain_text('https://tienda-correcta.example')
            assert not errors, errors
            return
        if scenario == 'payment':
            app.state['catalog']['capufe'] = {'name': 'CAPUFE'}
            app.upload['receipt'].update(payment_code='', provider='capufe', portal_url='')
            app.upload.update(status='review', message='El ticket no indica la forma de pago')
            app.automation.update(current_id=TICKET_ID, phase='review', message=app.upload['message'])
            await page.reload()
            search = page.get_by_role('searchbox', name='Buscar por comercio o folio')
            clear_search = page.get_by_role('button', name='Borrar búsqueda')
            await expect(search).to_have_attribute('autocomplete', 'off')
            await page.get_by_role('button', name='Por revisar', exact=True).click()
            await search.fill('emmanuel')
            await expect(uploaded_row).to_have_count(0)
            await clear_search.click()
            await expect(uploaded_row).to_be_visible()
            await page.get_by_role('button', name='En trámite', exact=True).click()
            await search.fill('emmanuel')
            await expect(rows).to_contain_text('Sin tickets con estos filtros')
            await page.locator('.automation-panel').get_by_role('button', name='Completar forma de pago').click()
            await expect(page.locator('#editor')).to_be_visible()
            payment = page.locator('#receipt-form [name=payment_code]')
            await expect(payment).to_be_focused()
            await expect(payment).to_have_value('')
            await payment.press('ArrowDown')
            await expect(payment).to_have_value('01')
            async with page.expect_response(lambda response: response.url.endswith('/api/state')):
                app.upload.update(message='La revisión sigue abierta')
                app.automation.update(message=app.upload['message'])
            await expect(page.locator('#automation-status')).to_contain_text('La revisión sigue abierta')
            await expect(payment).to_be_focused()
            await expect(payment).to_have_value('01')
            await payment.click()
            await payment.select_option('04')
            await expect(payment).to_have_value('04')
            await page.get_by_role('button', name='Guardar revisión').click()
            await expect(page.locator('#editor')).to_be_hidden()
            assert app.upload['receipt']['payment_code'] == '04'
            await rows.get_by_role('button', name='Mostrar todos los tickets').click()
            await expect(search).to_have_value('')
            await expect(page.get_by_role('button', name='Todos', exact=True)).to_have_attribute('aria-pressed', 'true')
            await expect(uploaded_row).to_be_visible()
            assert not errors, errors
            return
        if scenario == 'follow':
            assert app.puts == [False, True], app.puts
        elif scenario == 'dialog':
            await rows.get_by_role('row').filter(has_text='Otra tienda').get_by_role('button', name='Revisar').click()
            await expect(page.locator('#editor')).to_be_visible()
        elif scenario == 'navigation':
            await page.get_by_role('link', name='Portales', exact=True).click()
            await expect(page).to_have_url(ORIGIN + '/#portales')

        if scenario == 'follow':
            app.state.update(active_job=TICKET_ID)
            app.upload.update(status='running', message='Abriendo el navegador del ticket')
            app.automation.update(current_id=TICKET_ID, phase='queued', message=app.upload['message'])
            await expect(rows).to_contain_text(app.upload['message'])
            await expect(page).to_have_url(ORIGIN + '/#bandeja')
            app.state['browser'].update(open=True, revision=1, job_id='otro-ticket', label='Otra tienda')
            app.upload['message'] = 'Preparando la pestaña de este ticket'
            await expect(rows).to_contain_text(app.upload['message'])
            await expect(page).to_have_url(ORIGIN + '/#bandeja')
            await expect(rows.get_by_role('button', name='Ver en vivo')).to_have_count(0)
        app.start_portal()
        await expect(rows).to_contain_text(app.upload['message'])
        if scenario == 'dialog':
            await expect(page.locator('#editor')).to_be_visible()
            await expect(page).to_have_url(ORIGIN + '/#bandeja')
        else:
            if scenario == 'navigation':
                await expect(page).to_have_url(ORIGIN + '/#portales')
                await page.get_by_role('link', name='Bandeja', exact=True).click()
                await rows.get_by_role('button', name='Ver en vivo', exact=True).click()
            await expect(page).to_have_url(ORIGIN + '/#navegador')
            await expect(page.locator('#browser-task')).to_contain_text('Tienda de prueba')
            await expect(page.locator('#browser-image')).to_be_visible()
        assert not errors, errors
    finally:
        await context.close()


async def smoke():
    async with async_playwright() as playwright:
        browser = await playwright.chromium.launch(executable_path='/usr/bin/google-chrome-stable',
                                                    headless=False, chromium_sandbox=True)
        try:
            for scenario in ('follow', 'dialog', 'navigation', 'payment', 'portal', 'manual', 'duplicate'):
                await exercise(browser, scenario)
                print('OK', scenario, flush=True)
        finally:
            await browser.close()


if __name__ == '__main__':
    asyncio.run(smoke())
