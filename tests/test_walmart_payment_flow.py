"""Walmart's real order: fiscal form, «¿Están correctos?» dialog, payment page, emission.

The live portal asks the payment form on its own page after Aceptar and the
confirmation dialog. Only that page's submit emits, once, and going back from it
must not lock the agent in a loop.
"""
import asyncio
import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from facturador.db import Store
from facturador.models import Action, Profile, Receipt, Target
from facturador.runner import Runner

CHROME = os.getenv('FACTURADOR_CHROMIUM_PATH',
                   'C:/Program Files/Google/Chrome/Application/chrome.exe' if os.name == 'nt'
                   else '/usr/bin/google-chrome-stable')
ROOT = 'https://facturacion-clientes.walmart.com/'
TC = '1234567890123456789012'
TR = '01234'
PROFILE = Profile(rfc='XAXX010101000', name='PRUEBA AUTOMATIZACION', postal_code='76146',
                  regime='612', cfdi_use='G03', email='prueba@example.com')
RECEIPT = Receipt(provider='walmart', merchant='Walmart de prueba', amount='100.00',
                  date='2026-11-20', ticket_number=TC, billing_codes=['TR#' + TR], payment_code='08')
LOOKUP = f'''<!doctype html><html><body><form>
  <label>MEMBRESÍA O RFC<input value="{PROFILE.rfc}"></label>
  <label>Código Postal<input value="{PROFILE.postal_code}"></label>
  <label>Número de Ticket<input value="{TC}"></label>
  <label># Transacción<input value="{TR}"></label>
  <button type="button" onclick="setTimeout(() => location.href='/address', 300)">Continuar</button>
</form></body></html>'''
ADDRESS = f'''<!doctype html><html><body><form>
  <label>RFC<input value="{PROFILE.rfc}"></label>
  <label>Razón Social<input value="{PROFILE.name}"></label>
  <label>Código Postal<input value="{PROFILE.postal_code}"></label>
  <label>Correo Electrónico<input value="{PROFILE.email}"></label>
  <label>Régimen Fiscal<select><option value="612" selected>Personas Físicas con Actividades Empresariales y Profesionales</option></select></label>
  <label>Uso Factura<select><option value="G03" selected>Gastos en general</option></select></label>
  <button type="button" onclick="document.getElementById('alerta').hidden=false">Aceptar</button>
</form>
<div id="alerta" role="dialog" aria-label="Alerta" hidden><p>¿Están correctos todos sus datos?</p>
  <button type="button" onclick="document.getElementById('alerta').hidden=true">Cerrar</button>
  <button type="button" onclick="location.href='/payment'">Continuar</button></div>
</body></html>'''
PAYMENT = '''<!doctype html><html><body><h1>Forma de pago</h1><form>
  <label>Forma de pago<select>
    <option value="">Seleccione</option><option value="1">Efectivo</option>
    <option value="2">Tarjeta de débito</option><option value="3">Tarjeta de crédito</option>
    <option value="4">Vales de despensa</option></select></label>
  <button type="button" onclick="console.log('TEST_WALMART_ISSUE');
    document.body.innerHTML='<p>Factura emitida</p>'">Continuar</button>
</form></body></html>'''


def click(name):
    return Action(kind='click', target=Target(by='role', role='button', name=name, exact=True),
                  value=None, reason='Avanzar el ticket sintético', risk='normal')


# The runner's own browser is the container's Google Chrome for Linux.
@unittest.skipUnless(os.path.isfile('/usr/bin/google-chrome-stable'), 'Requiere el Chrome del contenedor')
class WalmartPaymentFlowTests(unittest.IsolatedAsyncioTestCase):
    async def run_flow(self, go_back_once=False, payment=PAYMENT, wait_question=False):
        directory = self.enterContext(tempfile.TemporaryDirectory())
        store = Store(Path(directory))
        runner = Runner(store)
        try:
            await runner.start_browser()
            issued = []
            runner.context.on('page', lambda page: page.on(
                'console', lambda message: issued.append(message.text)
                if message.text == 'TEST_WALMART_ISSUE' else None))

            async def portal(route):
                path = route.request.url.removeprefix(ROOT).split('?')[0]
                body = {'': LOOKUP, 'address': ADDRESS, 'payment': payment}.get(path)
                if body is None:
                    await route.abort()
                else:
                    await route.fulfill(content_type='text/html; charset=utf-8', body=body)

            await runner.context.route('**/*', portal)
            row, _ = store.add('synthetic.png', 'walmart-payment')
            key = row['id']
            store.save_receipt(key, RECEIPT, ready=True)
            backs = []

            def next_action(_instructions, observation, *_rest):
                if 'Factura emitida' in observation:
                    return Action(kind='done', target=None, value=None, reason='Emitida', risk='normal')
                if 'Número de Ticket' in observation:
                    return click('Continuar')
                if 'Forma de pago' in observation:
                    if go_back_once and not backs:
                        backs.append(1)
                        return Action(kind='back', target=None, value=None, reason='Revisar', risk='normal')
                    return click('Continuar')
                if '¿Están correctos' in observation:
                    return click('Continuar')
                return click('Aceptar')

            with patch('facturador.runner.AI.next_action', side_effect=next_action), \
                    patch('facturador.runner.ASK_TIMEOUT', 0.2):
                runner.schedule(key, PROFILE, auto_issue=True)
                if wait_question:
                    async with asyncio.timeout(60):
                        while key not in runner.pending:
                            await asyncio.sleep(.05)
                    await runner.stop(key)
                    await asyncio.gather(runner.job_tasks.get(key) or asyncio.sleep(0), return_exceptions=True)
                else:
                    await asyncio.wait_for(runner.job_tasks[key], timeout=60)
            return store, key, issued, runner
        except BaseException:
            await runner.close()
            raise

    async def check(self, go_back_once, payment=PAYMENT):
        store, key, issued, runner = await self.run_flow(go_back_once, payment)
        try:
            events = [(event['kind'], event['detail']) for event in store.events(key)]
            self.assertEqual(issued, ['TEST_WALMART_ISSUE'], events)
            result = store.get(key)
            self.assertTrue(result['issue_attempted'], events)
            self.assertFalse(result['human_exposed'], events)
            self.assertFalse(runner.pending)
            self.assertNotIn('Antes de continuar, coteja', str(events))
        finally:
            await runner.close()

    async def test_payment_page_submit_is_the_single_automatic_emission(self):
        await self.check(go_back_once=False)

    async def test_going_back_from_the_payment_page_does_not_loop(self):
        await self.check(go_back_once=True)

    async def test_unlabeled_payment_list_is_chosen_by_the_executor(self):
        # The live page shows the list without a caption the model can target.
        await self.check(False, PAYMENT.replace('<label>Forma de pago<select>', '<select>')
                         .replace('</select></label>', '</select>'))

    async def test_missing_payment_option_is_asked_in_the_chat_before_any_emission(self):
        without_vales = PAYMENT.replace('<option value="4">Vales de despensa</option>', '')
        store, key, issued, runner = await self.run_flow(False, without_vales, wait_question=True)
        try:
            self.assertEqual(issued, [])
            questions = [m['text'] for m in store.chat_messages() if 'página de pago de Walmart' in m['text']]
            self.assertTrue(questions)
            self.assertIn('Tarjeta de débito', questions[0])
            self.assertFalse(store.get(key)['issue_attempted'])
        finally:
            await runner.close()


class LiveChatNoteTests(unittest.IsolatedAsyncioTestCase):
    async def test_message_reaches_the_running_ticket(self):
        with tempfile.TemporaryDirectory() as directory:
            store = Store(Path(directory))
            runner = Runner(store)
            try:
                row, _ = store.add('ticket.png', 'nota-en-vivo')
                key = row['id']
                runner.active_id = key
                runner.job_tasks[key] = asyncio.create_task(asyncio.sleep(5))
                self.assertEqual(runner.running_job(), key)
                runner.tell(key, '  fue   tarjeta de vales ')
                self.assertEqual(runner.live_notes[key], ['fue tarjeta de vales'])
                runner.job_tasks[key].cancel()
                await asyncio.gather(runner.job_tasks.pop(key), return_exceptions=True)
                self.assertIsNone(runner.running_job())
            finally:
                await runner.close()


if __name__ == '__main__':
    unittest.main()
