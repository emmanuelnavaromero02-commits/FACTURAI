"""Recover an uncertain ticket, resume a blocked ticket, and hand off Google without portal secrets."""
import asyncio
from urllib.parse import urlsplit

from playwright.async_api import async_playwright, expect
from smoke_automation_ui import ORIGIN, TICKET_ID, ticket
from smoke_personal_ui import BrowserFixture


async def exercise(browser, modern):
    app = BrowserFixture()
    app.switch('personal')
    app.checkout.update(status='uncertain', issue_attempted=False, human_exposed=True, issuance_seen=False, uuid=None)
    retrying = ticket('segundo', 'Otra tienda', 'blocked')
    if modern:
        app.state['supports_ticket_retry'] = True
        app.checkout['retry_action'] = 'recover'
        retrying.update(retry_action='queue', can_retry=True)
        app.state['jobs'].append(retrying)
    calls = []
    async def serve(route):
        req = route.request
        path = urlsplit(req.url).path
        if path.endswith(('/retry', '/recover-local')) and req.method == 'POST':
            calls.append(path)
            assert req.headers['x-app-token'] == 'test-ui' and req.post_data is None
            if path.endswith('/segundo/retry'):
                return await route.fulfill(json=dict(id='segundo', status='queued', action='queued', message='Trámite en cola.'))
            assert path == '/api/jobs/' + TICKET_ID + ('/retry' if modern else '/recover-local')
            return await route.fulfill(json=dict(id=TICKET_ID, status='uncertain', action='recovering', message='Se buscarán los comprobantes del intento anterior.'))
        return await app.serve(route)

    context = await browser.new_context()
    try:
        await context.route('**/*', serve)
        page = await context.new_page()
        page.set_default_timeout(5000)
        await page.goto(ORIGIN + '/#bandeja')
        await page.get_by_role('button', name='Por revisar', exact=True).click()
        await expect(page.locator('#tickets tr').filter(has_text='Tienda de prueba').get_by_role('button', name='Recuperar comprobantes')).to_be_visible()
        await page.locator('#ticket-search').fill('dato que no coincide')
        await expect(page.locator('#tickets')).to_contain_text('Sin tickets con estos filtros')
        recovery = page.locator('#ticket-retry').get_by_role('button', name='Recuperar comprobantes')
        await recovery.click()
        await expect(page.locator('#notice')).to_contain_text('intento anterior')
        assert calls == ['/api/jobs/' + TICKET_ID + ('/retry' if modern else '/recover-local')]
        await page.get_by_role('link', name='Navegador', exact=True).click()
        await expect(page.locator('#browser-task').get_by_role('button', name='Recuperar comprobantes')).to_be_visible()

        address = page.locator('#browser-url')
        secret = 'CODIGO-DE-PRUEBA'
        await address.fill('https://accounts.google.com/o/oauth2/v2/auth?state=' + secret + '&redirect_uri=https://comercio.example/registro')
        await address.press('Enter')
        hint = page.locator('#browser-google-hint a').first
        await expect(hint).to_have_attribute('href', 'https://accounts.google.com/')
        await expect(hint).to_be_focused()
        assert not any(secret in str(body) for _, body in app.commands)
        await address.fill('gmail')
        await address.press('Enter')
        await expect(hint).to_have_attribute('href', 'https://mail.google.com/')
        await address.fill('https://identidad.example/oauth/authorize?client_id=prueba&state=' + secret)
        await address.press('Enter')
        await expect(hint).to_have_attribute('href', 'https://identidad.example/')
        await page.reload()
        app.tabs[0].update(url='https://tienda.example/tickets/' + secret, title='tienda.example')
        app.switch('personal')
        await expect(address).to_have_value('https://tienda.example/tickets/' + secret)
        app.tabs[0].update(url='https://accounts.google.com/signin/oauth/error?state=' + secret, title='accounts.google.com')
        app.switch('personal')
        await expect(hint).to_be_visible()
        await expect(hint).to_have_attribute('href', 'https://accounts.google.com/')
        await expect(page.locator('#browser-google-hint').get_by_role('link', name='Abrir tienda.example en Chrome')).to_have_attribute('href', 'https://tienda.example/')
        await expect(address).to_have_value('https://accounts.google.com/')
        assert not any(secret in str(body) for _, body in app.commands)
        if modern:
            await page.get_by_role('link', name='Bandeja', exact=True).click()
            await page.locator('#ticket-retry select').select_option('segundo')
            await page.locator('#ticket-retry').get_by_role('button', name='Reintentar').click()
            await expect(page.locator('#notice')).to_contain_text('Trámite en cola.')
            assert calls[-1] == '/api/jobs/segundo/retry'
    finally:
        await context.close()


async def main():
    async with async_playwright() as pw:
        browser = await pw.chromium.launch(executable_path='/usr/bin/google-chrome-stable', headless=False, chromium_sandbox=True)
        try:
            for modern in (False, True):
                await exercise(browser, modern)
        finally:
            await browser.close()
    print('Chrome recuperó el trámite incierto desde filtros y visor, reintentó antes de emitir y conservó los datos de OAuth en su origen.')


if __name__ == '__main__':
    asyncio.run(main())
