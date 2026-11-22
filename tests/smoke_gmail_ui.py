"""Test Gmail OAuth in an external Google Chrome tab with a synthetic account and no network."""
import asyncio
from urllib.parse import urlsplit

from playwright.async_api import async_playwright, expect
from smoke_automation_ui import ORIGIN, TICKET_ID
from smoke_personal_ui import BrowserFixture


async def smoke():
    fixture = BrowserFixture()
    fixture.state['active_job'] = TICKET_ID
    fixture.switch('job')
    authorize = 'https://accounts.google.com/o/oauth2/v2/auth?state=prueba'
    calls = []

    async def serve(route):
        request = route.request
        url = urlsplit(request.url)
        if url.hostname == 'accounts.google.com':
            return await route.fulfill(content_type='text/html', body='<h1>Autorización simulada</h1>')
        if url.hostname == 'mail.google.com':
            return await route.fulfill(content_type='text/html', body='<h1>Buzón simulado</h1>')
        if url.path == '/api/gmail/connect':
            assert request.method == 'POST' and request.headers['x-app-token'] == 'test-ui'
            calls.append(request.url)
            return await route.fulfill(json={'url': authorize})
        return await fixture.serve(route)

    async with async_playwright() as pw:
        browser = await pw.chromium.launch(executable_path='/usr/bin/google-chrome-stable',
                                           headless=False, chromium_sandbox=True)
        try:
            context = await browser.new_context()
            await context.route('**/*', serve)
            page = await context.new_page()
            page.set_default_timeout(6000)
            await page.goto(ORIGIN + '/#navegador')
            await expect(page.locator('#browser-google')).to_be_visible()
            async with context.expect_page() as inbox_open:
                await page.locator('#browser-webmail').click()
            inbox = await inbox_open.value
            await expect(inbox).to_have_url('https://mail.google.com/')
            assert await inbox.evaluate('window.opener === null')
            await inbox.close()
            await page.locator('#browser-google').click()
            await expect(page.locator('#gmail-configure')).to_be_focused()
            assert not calls

            fixture.state['gmail'] = 'configured'
            await expect(page.locator('#gmail-connect')).to_be_enabled()
            await page.get_by_role('link', name='Navegador', exact=True).click()
            async with context.expect_page() as consent_open:
                await page.locator('#browser-google').click()
            consent = await consent_open.value
            await expect(consent).to_have_url(authorize)
            assert await consent.evaluate('window.opener === null')
            await expect(page).to_have_url(ORIGIN + '/#navegador')
            fixture.state['gmail'] = 'connected'
            await consent.goto(ORIGIN + '/?conexion=gmail-ok#ajustes')
            await expect(consent.locator('#notice')).to_contain_text('pestaña original')
            await expect(page.locator('#notice')).to_contain_text('Gmail vinculado.')
            await consent.close()
            await page.locator('#browser-google').click()
            await expect(page.locator('#gmail-status')).to_have_text('Conectado')

            fixture.state['gmail'] = 'configured'
            await expect(page.locator('#gmail-connect')).to_have_text('Conectar Gmail')
            await page.evaluate('window.open = () => null')
            await page.locator('#gmail-connect').click()
            retry = page.get_by_role('link', name='Continuar en Google')
            await expect(retry).to_have_attribute('href', authorize)
            await expect(retry).to_be_focused()
            assert len(calls) == 2 and not any(path == '/api/browser/personal' for path, _ in fixture.commands)
            await page.goto(ORIGIN + '/?conexion=gmail-error&motivo=permiso#ajustes')
            await expect(page.locator('#notice')).to_contain_text('Faltó el permiso de lectura')
            print('Chrome verificó Gmail externo, autorización de solo lectura en una segunda pestaña, relectura del estado y alternativa al bloqueo de ventanas.')
        finally:
            await browser.close()


if __name__ == '__main__':
    asyncio.run(smoke())
