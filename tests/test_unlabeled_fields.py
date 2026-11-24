"""A field without an accessible name can still be found and filled by its number."""
import asyncio
import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from playwright.async_api import async_playwright

from facturador.db import Store
from facturador.models import Action, Receipt, Target
from facturador.runner import Runner

CHROME = os.getenv('FACTURADOR_CHROMIUM_PATH',
                   'C:/Program Files/Google/Chrome/Application/chrome.exe' if os.name == 'nt'
                   else '/usr/bin/google-chrome-stable')
ORIGIN = 'https://facturacion-clientes.walmart.com'
# Same trap as the live Walmart form: the e-mail caption is bound to the regime list,
# so the e-mail input has no accessible name at all.
FORM = """<form>
<label>*Razón Social:<input name="name" required></label>
<div><span>*Correo Electrónico:</span><label for="regimen">*Régimen Fiscal:</label></div>
<div><input type="email" required></div>
<select id="regimen"><option value="">Selecciona</option><option value="612">612</option></select>
<label>Referencia:<input name="ref" maxlength="12" oninput="this.value=this.value.replace(/[^0-9]/g,'')"></label>
</form>"""
RECEIPT = Receipt(provider='walmart', merchant='Walmart', amount='100.00', date='2026-11-13',
                  ticket_number='123456789', payment_code='04')


@unittest.skipUnless(os.path.isfile(CHROME), 'Requiere Google Chrome oficial')
class UnlabeledFieldTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.runner = Runner(Store(Path(self.tmp.name)))
        self.playwright = await async_playwright().start()
        self.browser = await self.playwright.chromium.launch(executable_path=CHROME, headless=True)
        self.page = await self.browser.new_page()
        await self.page.route('**/*', lambda route: route.fulfill(body=FORM, content_type='text/html; charset=utf-8'))
        await self.page.goto(ORIGIN + '/address')

    async def asyncTearDown(self):
        await self.browser.close()
        await self.playwright.stop()
        await self.runner.close()
        self.tmp.cleanup()

    async def test_unlabeled_email_is_listed_and_filled_by_number(self):
        _, fields = await self.runner.page_fields(self.page, RECEIPT)
        email = next(line for line in fields if ' email ' in line)
        self.assertIn('Correo Electrónico', email)
        self.assertIn('obligatorio', email)
        number = email.split()[0].lstrip('#')
        action = Action(kind='fill', target=Target(by='field', name=number, role=None, exact=True),
                        value='prueba@example.com', reason='correo', risk='normal')
        await self.runner.execute(self.page, action, RECEIPT)
        self.assertEqual(await self.page.locator('input[type=email]').input_value(), 'prueba@example.com')

    async def test_masked_field_is_typed_when_paste_is_rewritten(self):
        _, fields = await self.runner.page_fields(self.page, RECEIPT)
        number = next(line for line in fields if 'Referencia' in line).split()[0].lstrip('#')
        action = Action(kind='fill', target=Target(by='field', name=number, role=None, exact=True),
                        value='1234-5678', reason='referencia', risk='normal')
        # The mask drops the dash; the runner retypes it and accepts the same digits.
        await self.runner.execute(self.page, action, RECEIPT)
        self.assertEqual(await self.page.locator('input[name=ref]').input_value(), '12345678')


class ChatQuestionTests(unittest.IsolatedAsyncioTestCase):
    async def test_question_waits_for_the_chat_answer(self):
        with tempfile.TemporaryDirectory() as directory:
            store = Store(Path(directory))
            runner = Runner(store)
            try:
                asking = asyncio.create_task(runner.ask_chat('ticket-1', '¿Qué número de tienda uso?'))
                for _ in range(100):
                    if runner.open_question():
                        break
                    await asyncio.sleep(.01)
                self.assertEqual(runner.open_question(), 'ticket-1')
                self.assertIn('número de tienda', store.chat_messages()[-1]['text'])
                runner.answer_question('ticket-1', 'La 2045')
                self.assertEqual(await asyncio.wait_for(asking, 2), 'La 2045')
                self.assertIsNone(runner.open_question())
            finally:
                await runner.close()

    async def test_unanswered_question_lets_the_agent_assume(self):
        with tempfile.TemporaryDirectory() as directory:
            runner = Runner(Store(Path(directory)))
            try:
                with patch('facturador.runner.ASK_TIMEOUT', 0.05):
                    self.assertEqual(await runner.ask_chat('ticket-2', '¿Correo?'), '')
                self.assertIsNone(runner.open_question())
            finally:
                await runner.close()


class RestartTests(unittest.TestCase):
    def test_restart_does_not_relaunch_an_interrupted_ticket(self):
        with tempfile.TemporaryDirectory() as directory:
            store = Store(Path(directory))
            row, _ = store.add('ticket.png', 'restart-ticket')
            store.save_receipt(row['id'], RECEIPT, ready=True)
            store.queue_chat_job(row['id'])
            with store.connect() as db:
                db.execute("UPDATE jobs SET status='running' WHERE id=?", (row['id'],))
            store.recover_after_restart()
            job = store.get(row['id'])
            self.assertEqual((job['status'], job['auto_stage']), ('ready', 'review'))
            self.assertFalse(store.has_chat_jobs())
            self.assertIsNone(store.auto_candidates('ready'))


if __name__ == '__main__':
    unittest.main()
