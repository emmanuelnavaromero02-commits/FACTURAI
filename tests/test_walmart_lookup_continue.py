"""Walmart lookup advances without a model call only after visible ticket proof."""

import asyncio
import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from facturador.auth import PlanError
from facturador.db import Store
from facturador.models import Profile, Receipt
from facturador.runner import Runner
from facturador.walmart_issue import walmart_lookup_continue, walmart_lookup_rejection


CHROME = os.getenv('FACTURADOR_CHROMIUM_PATH',
                   'C:/Program Files/Google/Chrome/Application/chrome.exe' if os.name == 'nt'
                   else '/usr/bin/google-chrome-stable')
ROOT = 'https://facturacion-clientes.walmart.com/'
LOOKUP = ROOT + 'ticket'
ADDRESS = ROOT + 'address'
TC = '1234567890123456789012'
TR = '01234'
PROFILE = Profile(rfc='XAXX010101000', name='PRUEBA AUTOMATIZACION', postal_code='76146',
                  regime='612', cfdi_use='G03', email='prueba@example.com')
RECEIPT = Receipt(provider='walmart', merchant='Walmart de prueba', amount='100.00',
                  date='2026-11-20', ticket_number=TC, billing_codes=['TR#' + TR], payment_code='08')
LOOKUP_HTML = f'''<!doctype html><html><body><form>
  <label>MEMBRESÍA O RFC<input value="{PROFILE.rfc}"></label>
  <label>Código Postal<input value="{PROFILE.postal_code}"></label>
  <label>Número de Ticket<input value="{TC}"></label>
  <label># Transacción<input value="{TR}"></label>
  <button type="button" onclick="location.href='/address'">Continuar</button>
</form></body></html>'''
PLACEHOLDER_LOOKUP_HTML = f'''<!doctype html><html><body>
<main id="app_main"><div id="invoice_tab_content">
  <form id="facturar_form">
    <label>*</label><input id="membershipOrRFC" placeholder="MEMBRESÍA O RFC" value="{PROFILE.rfc}">
    <label>*</label><input id="postalCode" placeholder="Código Postal" value="{PROFILE.postal_code}">
    <label>*</label><input id="ticketNumber" placeholder="Número de Ticket" value="{TC}">
    <label>*</label><input id="transactionNumber" placeholder="# Transacción" value="{TR}">
  </form>
  <div id="form_actions_row"><button id="form_btn_accept" type="button"
    onclick="location.href='/address'">Continuar</button></div>
</div></main></body></html>'''


@unittest.skipUnless(os.path.isfile(CHROME), 'Requiere Google Chrome oficial')
class WalmartLookupContinueTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.store = Store(Path(temporary.name))
        self.runner = Runner(self.store)
        browser_env = patch.dict(os.environ, {'FACTURADOR_CHROMIUM_PATH': CHROME})
        browser_env.start()
        self.addCleanup(browser_env.stop)
        await self.runner.start_browser()
        self.addAsyncCleanup(self.runner.close)
        self.lookup_html = LOOKUP_HTML

        async def portal(route):
            if route.request.url == LOOKUP:
                await route.fulfill(content_type='text/html; charset=utf-8', body=self.lookup_html)
            elif route.request.url == ADDRESS:
                await route.fulfill(content_type='text/html; charset=utf-8',
                                    body='<html><body><h1>Datos fiscales</h1></body></html>')
            else:
                await route.abort()

        await self.runner.context.route('**/*', portal)

    async def test_runner_continues_ticket_without_model_and_never_attempts_issue(self):
        row, _ = self.store.add('synthetic.png', 'walmart-lookup-without-model')
        key = row['id']
        self.store.save_receipt(key, RECEIPT, ready=True)

        async def open_lookup(page, *_args):
            await page.goto(LOOKUP)

        with (patch('facturador.runner.open_ticket_portal', side_effect=open_lookup),
              patch('facturador.runner.PLAN_RETRIES', 0),
              patch('facturador.runner.AI.next_action', side_effect=PlanError('prueba', 'transient')) as model):
            self.runner.schedule(key, PROFILE, auto_issue=False)
            await asyncio.wait_for(self.runner.job_tasks[key], timeout=20)

        events = [event['kind'] for event in self.store.events(key)]
        result = self.store.get(key)
        self.assertEqual(model.call_count, 1)  # The model is reached only after /address.
        self.assertEqual(events.count('ticket_lookup_verified'), 1)
        self.assertEqual(events.count('issue_attempt'), 0)
        self.assertIn('plan_failure', events)
        self.assertEqual(result['status'], 'blocked')
        self.assertFalse(result['issue_attempted'])
        self.assertFalse(result['human_exposed'])
        self.assertEqual(self.runner.browser_state()['mode'], 'idle')

    async def test_runner_continues_real_placeholder_layout_without_model(self):
        self.lookup_html = PLACEHOLDER_LOOKUP_HTML
        row, _ = self.store.add('synthetic.png', 'walmart-placeholder-lookup')
        key = row['id']
        self.store.save_receipt(key, RECEIPT, ready=True)

        async def open_lookup(page, *_args):
            await page.goto(LOOKUP)

        with (patch('facturador.runner.open_ticket_portal', side_effect=open_lookup),
              patch('facturador.runner.PLAN_RETRIES', 0),
              patch('facturador.runner.AI.next_action', side_effect=PlanError('prueba', 'transient')) as model):
            self.runner.schedule(key, PROFILE, auto_issue=False)
            await asyncio.wait_for(self.runner.job_tasks[key], timeout=20)

        events = [event['kind'] for event in self.store.events(key)]
        self.assertEqual(events.count('ticket_lookup_verified'), 1)
        self.assertEqual(events.count('issue_attempt'), 0)
        self.assertEqual(model.call_count, 1)  # Only after the safe lookup reaches /address.
        self.assertFalse(self.store.get(key)['issue_attempted'])

    async def test_placeholder_lookup_requires_unique_exact_values(self):
        page = await self.runner.context.new_page()
        await page.goto(LOOKUP)
        await page.set_content(PLACEHOLDER_LOOKUP_HTML)
        self.assertIsNotNone(await walmart_lookup_continue(page, PROFILE, RECEIPT))
        await page.set_content(PLACEHOLDER_LOOKUP_HTML.replace(TR, '99999'))
        self.assertIsNone(await walmart_lookup_continue(page, PROFILE, RECEIPT))
        duplicate = PLACEHOLDER_LOOKUP_HTML.replace('</form>',
            '<input placeholder="Código Postal" value="76146"></form>')
        await page.set_content(duplicate)
        self.assertIsNone(await walmart_lookup_continue(page, PROFILE, RECEIPT))

    async def test_lookup_button_requires_unique_visible_verified_control(self):
        page = await self.runner.context.new_page()
        await page.goto(LOOKUP)
        self.assertIsNotNone(await walmart_lookup_continue(page, PROFILE, RECEIPT))
        await page.set_content(LOOKUP_HTML.replace(TC, '0000000000000000000000'))
        self.assertIsNone(await walmart_lookup_continue(page, PROFILE, RECEIPT))
        await page.set_content(LOOKUP_HTML.replace('</form>', '<button>Continuar</button></form>'))
        self.assertIsNone(await walmart_lookup_continue(page, PROFILE, RECEIPT))
        await page.set_content(LOOKUP_HTML + '<div role="dialog">Aviso</div>')
        self.assertIsNone(await walmart_lookup_continue(page, PROFILE, RECEIPT))

    async def test_formless_button_must_share_container_with_fields(self):
        page = await self.runner.context.new_page()
        separated = LOOKUP_HTML.replace('<form>', '<main>').replace('</form>', '</main>')
        await page.goto(LOOKUP)
        await page.set_content(separated)
        self.assertIsNotNone(await walmart_lookup_continue(page, PROFILE, RECEIPT))
        outside = separated.replace('  <button type="button" onclick="location.href=\'/address\'">Continuar</button>\n', '')
        outside = outside.replace('</main>', '</main><button type="button">Continuar</button>')
        await page.set_content(outside)
        self.assertIsNone(await walmart_lookup_continue(page, PROFILE, RECEIPT))

    async def test_button_outside_form_needs_one_shared_panel(self):
        page = await self.runner.context.new_page()
        await page.goto(LOOKUP)
        panel = LOOKUP_HTML.replace('<form>', '<main><form>')
        panel = panel.replace('  <button type="button" onclick="location.href=\'/address\'">Continuar</button>\n',
                              '</form><button type="button">Continuar</button>')
        panel = panel.replace('</form></body>', '</main></body>')
        await page.set_content(panel)
        self.assertIsNotNone(await walmart_lookup_continue(page, PROFILE, RECEIPT))
        await page.locator('main > button').evaluate('button => button.parentElement.after(button)')
        self.assertIsNone(await walmart_lookup_continue(page, PROFILE, RECEIPT))

    async def test_visible_rejection_is_redacted_without_another_lookup(self):
        page = await self.runner.context.new_page()
        await page.goto(LOOKUP)
        before = await page.locator('body').aria_snapshot()
        await page.locator('body').evaluate(
            "(body) => body.insertAdjacentHTML('beforeend', '<div role=\"alert\">El ticket "
            + TC + " del RFC " + PROFILE.rfc + " venció después de 30 días.</div>')")
        message = await walmart_lookup_rejection(page, before)
        self.assertIn('venció', message)
        self.assertIn('[dato]', message)
        self.assertNotIn(TC, message)
        self.assertNotIn(PROFILE.rfc, message)


if __name__ == '__main__':
    unittest.main()
