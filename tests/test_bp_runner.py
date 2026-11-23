"""Runner integration against a synthetic BP form in official Chrome."""
import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import AsyncMock, Mock, patch

from playwright.async_api import async_playwright

from facturador.db import Store
from facturador.models import Action, Target
from facturador.runner import Runner
from test_bp_ticket import EVIDENCE, FORM, PROFILE, RECEIPT, fixture


ISSUE = Action(kind='click', target=Target(by='role', role='link', name='Facturar', exact=True),
               value=None, reason='Ticket sintético cotejado', risk='issue')
GOOD_ROW = "<tr><td>1234567</td><td>Premium</td><td>42.567</td><td>1234.56</td></tr>"
CROSSED_ROWS = ("<tr><td>1234567</td><td>Premium</td><td>42.567</td><td>999.00</td></tr>"
                "<tr><td>7654321</td><td>Premium</td><td>1.000</td><td>1234.56</td></tr>")


def simulated_portal(*, total='1234.56', crossed_rows=False):
    html = fixture(total=total)
    if crossed_rows:
        assert GOOD_ROW in html
        html = html.replace(GOOD_ROW, CROSSED_ROWS)
    return html + '''<script>
      window.__issueClicks=0;
      document.getElementById('A40').addEventListener('click', event => {
        event.preventDefault();
        window.__issueClicks++;
        document.body.insertAdjacentHTML('beforeend', '<p>Factura generada</p>');
      });
    </script>'''


@unittest.skipUnless(os.path.isfile('/usr/bin/google-chrome-stable'), 'Requiere Google Chrome oficial')
class BpRunnerTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        root = Path(directory.name)
        source = root / 'ticket-sintetico.txt'
        source.write_text('TICKET DE PRUEBA', encoding='utf-8')
        self.store = Store(root)
        self.key = self.store.add(str(source), 'bp-runner-synthetic')[0]['id']
        self.store.save_receipt(self.key, RECEIPT, ready=True)
        self.plan = Mock()
        self.runner = Runner(self.store, self.plan)
        self.playwright = await async_playwright().start()
        self.browser = await self.playwright.chromium.launch(executable_path='/usr/bin/google-chrome-stable')
        self.runner.context = await self.browser.new_context()
        self.html = simulated_portal()
        await self.runner.context.route('https://facturacion.grupocentra.mx/**',
                                        lambda route: route.fulfill(content_type='text/html', body=self.html))
        self.addAsyncCleanup(self.playwright.stop)
        self.addAsyncCleanup(self.browser.close)
        self.addAsyncCleanup(self.runner.context.close)
        opener = patch('facturador.runner.open_ticket_portal', side_effect=self.open_form)
        evidence = patch('facturador.runner.evidence_from_original', return_value=EVIDENCE)
        planner = patch('facturador.runner.AI.next_action', return_value=ISSUE)
        opener.start(); evidence.start(); planner.start()
        self.addCleanup(opener.stop)
        self.addCleanup(evidence.stop)
        self.addCleanup(planner.stop)
        self.runner.recover = AsyncMock(return_value=True)
        self.execute_states = []
        original_execute = self.runner.execute

        async def guarded_execute(page, action, receipt):
            if action.target and action.target.name == 'Facturar':
                self.execute_states.append(self.store.get(self.key)['issue_attempted'])
            return await original_execute(page, action, receipt)

        self.runner.execute = guarded_execute

    async def open_form(self, page, *_args):
        await page.goto(FORM)

    async def issue_clicks(self):
        return await self.runner.viewer.page.evaluate('window.__issueClicks')

    async def test_auto_issue_clicks_once_after_persisting_attempt(self):
        await self.runner.run(self.key, PROFILE, auto_issue=True)
        self.assertEqual(await self.issue_clicks(), 1)
        self.assertEqual(self.execute_states, [True])
        self.assertTrue(self.store.get(self.key)['issue_attempted'])
        self.assertEqual(sum(event['kind'] == 'issue_attempt' for event in self.store.events(self.key)), 1)

    async def test_total_mismatch_never_clicks_or_records_issue(self):
        self.html = simulated_portal(total='1234.57')
        await self.runner.run(self.key, PROFILE, auto_issue=True)
        self.assertEqual(await self.issue_clicks(), 0)
        self.assertEqual(self.execute_states, [])
        self.assertFalse(self.store.get(self.key)['issue_attempted'])

    async def test_crossed_ticket_and_amount_rows_never_click(self):
        self.html = simulated_portal(crossed_rows=True)
        await self.runner.run(self.key, PROFILE, auto_issue=True)
        self.assertEqual(await self.issue_clicks(), 0)
        self.assertEqual(self.execute_states, [])
        self.assertFalse(self.store.get(self.key)['issue_attempted'])
