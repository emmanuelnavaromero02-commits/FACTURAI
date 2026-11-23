"""Real Chrome checks for unlabeled series and ticket fields used by many POS portals."""
import os
import unittest

from playwright.async_api import async_playwright

from facturador.ticket_lookup import prefill_ticket_lookup, split_series_ticket


HTML = '''<table>
<tr><td>Empresa</td><td><select><option>Vianney</option></select></td></tr>
<tr><td><span>Serie</span></td><td><input type=text maxlength=10></td></tr>
<tr><td><span>Tiquet</span></td><td><input type=text maxlength=10></td></tr>
<tr><td colspan=2><input type=button value=Siguiente></td></tr>
</table>'''


class TicketLookupTests(unittest.IsolatedAsyncioTestCase):
    def test_split_only_explicit_series_and_numeric_ticket(self):
        self.assertEqual(split_series_ticket('E51T / 148,390'), ('E51T', '148390'))
        self.assertEqual(split_series_ticket('A-12 / 204'), ('A-12', '204'))
        self.assertIsNone(split_series_ticket('148390'))
        self.assertIsNone(split_series_ticket('E51T / 14O390'))

    @unittest.skipUnless(os.path.isfile('/usr/bin/google-chrome-stable'), 'Requiere Google Chrome')
    async def test_unlabeled_table_fields_are_mapped_without_guessing_position(self):
        async with async_playwright() as playwright:
            browser = await playwright.chromium.launch(executable_path='/usr/bin/google-chrome-stable')
            try:
                page = await browser.new_page()
                await page.set_content(HTML)
                self.assertEqual(await prefill_ticket_lookup(page, 'E51T / 148,390'), 'series')
                self.assertEqual(await prefill_ticket_lookup(page, 'E51T / 148,390'), 'ticket')
                self.assertIsNone(await prefill_ticket_lookup(page, 'E51T / 148,390'))
                self.assertEqual(await page.locator('tr').nth(1).locator('input').input_value(), 'E51T')
                self.assertEqual(await page.locator('tr').nth(2).locator('input').input_value(), '148390')
                await page.set_content(HTML.replace('<tr><td><span>Serie</span>',
                                                    '<tr><td><span>Serie</span><input type=text value=otro>'))
                self.assertIsNone(await prefill_ticket_lookup(page, 'E51T / 148,390'))
                await page.set_content(HTML.replace('</table>', '<tr><td>Serie</td><td><input></td></tr></table>'))
                self.assertIsNone(await prefill_ticket_lookup(page, 'E51T / 148,390'))
                await page.set_content('<table><tr><td>Serie</td><td><input></td></tr></table>'
                                       '<table><tr><td>Tiquet</td><td><input></td></tr></table>')
                self.assertIsNone(await prefill_ticket_lookup(page, 'E51T / 148,390'))
            finally:
                await browser.close()
