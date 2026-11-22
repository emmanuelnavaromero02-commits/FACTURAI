"""Private panel handoff to the existing ticket, including a touch viewport and stale screens."""
import asyncio
from urllib.parse import urlsplit

from playwright.async_api import async_playwright, expect
from smoke_automation_ui import ORIGIN, TICKET_ID
from smoke_personal_ui import BrowserFixture


async def exercise(browser, mobile):
    app = BrowserFixture()
    app.state.update(active_job=TICKET_ID, supports_captcha_control=True)
    app.checkout.update(status='needs_user', human_kind='captcha')
    app.switch('personal' if mobile else 'job')
    resolved = False
    async def serve(route):
        request = route.request
        path = urlsplit(request.url).path
        if request.method == 'POST' and path in ('/api/browser/return', '/api/browser/control'):
            assert request.post_data_json['job_id'] == TICKET_ID
            assert app.state['active_job'] == TICKET_ID
        if path == '/api/browser/control':
            assert app.state['browser']['mode'] == 'job' and not app.lease
        if path == '/api/browser/release' and resolved and not request.post_data_json.get('abandon'):
            app.checkout.update(status='running', human_kind=None)
        return await app.serve(route)

    context = await browser.new_context(viewport={'width':390 if mobile else 1280, 'height':844}, has_touch=mobile,
                                        is_mobile=mobile)
    try:
        await context.route('**/*', serve)
        page = await context.new_page()
        page.set_default_timeout(5500)
        await page.goto(ORIGIN + '/#ajustes')
        alert = page.locator('#captcha-alert')
        control = alert.get_by_role('button', name='Resolver verificación')
        await expect(control).to_be_enabled()
        await expect(alert).to_contain_text('Tienda de prueba')
        assert (await control.bounding_box())['height'] >= 44
        if mobile:
            await control.tap()
        else:
            await control.click()
        await expect(page).to_have_url(ORIGIN + '/#navegador')
        release = alert.get_by_role('button', name='Devolver control')
        await expect(release).to_be_visible()
        await expect(alert).to_contain_text('Cuando el portal avance')
        assert app.lease and len([p for p,_ in app.commands if p == '/api/browser/control']) == 1
        assert len([p for p,_ in app.commands if p == '/api/browser/return']) == int(mobile)
        if mobile:
            # Observe the lifecycle request before tearing down Playwright's offline route interceptor.
            async with page.expect_response(lambda r: r.url.endswith('/api/browser/release') and r.request.method == 'POST'):
                await page.evaluate("window.dispatchEvent(new PageTransitionEvent('pagehide'))")
            await page.reload()
            await expect(control).to_be_enabled()
            assert not app.lease and any(p == '/api/browser/release' and body.get('abandon') for p,body in app.commands)
            await control.tap()
            await expect(release).to_be_visible()
        await release.click()
        await expect(control).to_be_enabled()
        assert not app.lease
        # A refreshed panel cannot attach to another ticket, even if the view has the old revision.
        app.state['browser']['job_id'] = 'otra-sesion'
        await control.click()
        await expect(control).to_be_disabled()
        assert not app.lease and len([p for p,_ in app.commands if p == '/api/browser/control']) == 1 + int(mobile)
        app.state['browser']['job_id'] = TICKET_ID
        await expect(control).to_be_enabled()
        await control.click()
        await expect(release).to_be_visible()
        resolved = True
        await release.click()
        await expect(alert).to_be_hidden()
        assert not app.lease
        assert not any(p in ['/api/browser/open','/api/browser/personal'] for p,_ in app.commands)
    finally:
        await context.close()


async def main():
    async with async_playwright() as pw:
        browser = await pw.chromium.launch(executable_path='/usr/bin/google-chrome-stable', headless=False, chromium_sandbox=True)
        try:
            for mobile in (True, False):
                await exercise(browser, mobile)
        finally:
            await browser.close()
    print('Chrome móvil y escritorio: aviso, control del ticket correcto, devolución, reto pendiente y sesión equivocada.')


if __name__ == '__main__':
    asyncio.run(main())
