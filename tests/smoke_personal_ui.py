"""Exercise the operator-facing Chrome toolbar with simulated Google, WhatsApp and job views."""
import asyncio
from urllib.parse import urlsplit

from playwright.async_api import async_playwright, expect
from smoke_automation_ui import Fixture, ORIGIN, TICKET_ID


class BrowserFixture(Fixture):
    def __init__(self):
        super().__init__()
        self.checkout = self.upload
        self.state['jobs'] = [self.checkout]
        self.tabs = [dict(id='t1', title='Nueva pestaña', url='', selected=True)]
        self.commands = []
        self.lease = ''
        self.switch('whatsapp')

    def switch(self, mode):
        self.lease = ''
        view = self.state['browser']
        selected = next(tab for tab in self.tabs if tab['selected'])
        view.update(open=True, manual=True, mode=mode, revision=view['revision'] + 1,
                    tabs=self.tabs if mode == 'personal' else [], job_id=TICKET_ID if mode == 'job' else None,
                    return_job_id=self.state['active_job'] if mode == 'personal' else None)
        view['label'], view['url'] = {
            'whatsapp': ('WhatsApp Web', 'https://web.whatsapp.com/'),
            'personal': ('Navegador personal', selected['url']),
            'job': ('Tienda de prueba', 'https://tienda.example/facturar'),
        }[mode]

    async def serve(self, route):
        path = urlsplit(route.request.url).path
        if route.request.method != 'POST' or not path.startswith(('/api/browser/', '/api/whatsapp/')):
            return await super().serve(route)
        params = route.request.post_data_json or {}
        self.commands.append((path, params))
        if path == '/api/browser/control':
            self.lease = 'canal-simulado'
            return await route.fulfill(json={'channel': self.lease})
        if path == '/api/browser/release':
            assert params['channel'] == self.lease
            self.lease = ''
        elif path == '/api/browser/input':
            assert self.lease and params['channel'] == self.lease
        elif path == '/api/browser/personal':
            assert params['channel'] == self.lease
            if params.get('url'):
                selected = next(t for t in self.tabs if t['selected'])
                selected.update(url=params['url'], title=urlsplit(params['url']).hostname)
            self.switch('personal')
        elif path == '/api/browser/personal/tabs':
            action = params['action']
            if action == 'new':
                self.tabs.append(dict(id='t2', title='Nueva pestaña', url='', selected=False))
            if action == 'close':
                self.tabs[:] = [t for t in self.tabs if t['id'] != params['tab_id']]
            target = 't2' if action == 'new' else 't1' if action == 'close' else params['tab_id']
            for tab in self.tabs:
                tab['selected'] = tab['id'] == target
            self.switch('personal')
        elif path == '/api/browser/history':
            assert params['direction'] in {'back', 'forward', 'reload'}
        elif path == '/api/whatsapp/open':
            self.switch('whatsapp')
        elif path == '/api/browser/return':
            self.switch('job')
        else:
            return await route.abort()
        return await route.fulfill(json=self.state['browser'])


async def smoke():
    async with async_playwright() as playwright:
        browser = await playwright.chromium.launch(executable_path='/usr/bin/google-chrome-stable',
                                                   headless=False, chromium_sandbox=True)
        try:
            app = BrowserFixture()
            page = await browser.new_page()
            page.set_default_timeout(6000)
            errors = []
            page.on('pageerror', lambda error: errors.append(str(error)))
            await page.route('**/*', app.serve)
            await page.goto(ORIGIN + '/#navegador')
            await expect(page.get_by_role('heading', name='Nueva pestaña')).to_be_visible()
            address = page.get_by_role('textbox', name='Buscar en Google o escribir una dirección')
            await address.fill('consulta de prueba')
            async with page.expect_response(lambda r: r.url.endswith('/api/state')):
                await expect(address).to_be_focused()
            assert not any(params.get('url') for _, params in app.commands)
            await address.press('Enter')
            await expect(address).to_have_value('https://www.google.com/search?q=consulta+de+prueba')
            await address.fill('http://ejemplo.test')
            await address.press('Enter')
            assert not await address.evaluate('e => e.checkValidity()')
            await address.fill('ejemplo.test')
            async with page.expect_response(lambda r: r.url.endswith('/api/state')):
                await expect(address).to_be_focused()
            await expect(address).to_have_value('ejemplo.test')
            await address.press('Enter')
            await expect(address).to_have_value('https://ejemplo.test/')
            await page.get_by_role('button', name='Nueva pestaña', exact=True).click()
            await expect(address).to_be_focused()
            await expect(address).to_have_value('')
            await address.fill('consulta.example')
            await address.press('Enter')
            await expect(address).to_have_value('https://consulta.example/')
            await page.get_by_role('button', name='Recargar página').click()
            await page.get_by_role('button', name='ejemplo.test', exact=True).click()
            await expect(address).to_have_value('https://ejemplo.test/')
            await page.get_by_role('button', name='consulta.example', exact=True).click()
            await expect(address).to_have_value('https://consulta.example/')
            await page.get_by_role('button', name='Cerrar pestaña actual').click()
            await expect(address).to_have_value('https://ejemplo.test/')
            await page.get_by_role('button', name='Abrir WhatsApp Web').click()
            await expect(page.get_by_role('button', name='Importar foto abierta')).to_be_visible()
            await page.get_by_role('button', name='Tomar control').click()
            await page.get_by_role('button', name='Navegación libre', exact=True).click()
            await expect(address).to_have_value('https://ejemplo.test/')
            await page.locator('#browser-image').click()
            await expect(page.get_by_role('button', name='Devolver control')).to_be_visible()
            async with page.expect_response(lambda r: r.url.endswith('/api/browser/release')):
                await page.get_by_role('link', name='Bandeja', exact=True).click()
            assert not app.lease
            app.state['active_job'] = TICKET_ID
            app.checkout.update(status='needs_user', human_kind='issue')
            app.switch('job')
            await expect(page.get_by_role('button', name='Aprobar emisión')).to_be_visible()
            command_count = len(app.commands)
            await page.locator('#browser-google').click()
            await expect(page).to_have_url(ORIGIN + '/#ajustes')
            await expect(page.locator('#gmail-configure')).to_be_focused()
            assert app.state['browser']['job_id'] == TICKET_ID and app.state['browser']['mode'] == 'job'
            assert not any(path == '/api/browser/personal' for path, _ in app.commands[command_count:])
            await page.get_by_role('link', name='Navegador', exact=True).click()
            await expect(page.get_by_role('button', name='Aprobar emisión')).to_be_visible()
            await page.get_by_role('button', name='Navegación libre', exact=True).click()
            await expect(page.get_by_role('button', name='Volver al trámite')).to_be_visible()
            await expect(page.get_by_role('button', name='Aprobar emisión')).to_have_count(0)
            await page.get_by_role('button', name='Volver al trámite').click()
            await expect(page.get_by_role('button', name='Aprobar emisión')).to_be_visible()
            assert not errors, errors
            assert any(p == '/api/browser/personal' and body.get('channel') for p, body in app.commands)
            print('OK Chrome: URL, pestañas, historial, Google, WhatsApp, liberar control, consultar fuera del trámite y volver.')
        finally:
            await browser.close()


if __name__ == '__main__':
    asyncio.run(smoke())
