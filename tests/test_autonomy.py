"""The agent resolves ordinary portal steps by itself and asks only for what is the person's."""
import asyncio
import tempfile
import unittest
from pathlib import Path
from unittest.mock import Mock, patch

from facturador.auth import strict_schema
from facturador.catalog import allowed_url, billing_destination
from facturador.db import Store
from facturador.models import Action, Target
from facturador.runner import Runner, action_risk
from test_core import FakeContext, FakePage, PROFILE, RECEIPT


def click(name, risk='normal', role='button'):
    return Action(kind='click', target=Target(by='role', role=role, name=name, exact=True),
                  value=None, reason='paso', risk=risk)


class FakeFrame:
    parent_frame = None

    def __init__(self, text, url='https://estacionamientos.fibrauno.mx/facturar'):
        self.text, self.url = text, url

    async def evaluate(self, _script, *_args):
        return self.text


class RiskTests(unittest.TestCase):
    def test_terms_and_privacy_are_handled_by_the_agent(self):
        for name in ('Acepto los términos y condiciones', 'Aviso de privacidad', 'Cerrar'):
            with self.subTest(name=name):
                self.assertEqual(action_risk(click(name, role='checkbox')), 'normal')
        self.assertEqual(action_risk(click('Continuar', risk='terms')), 'normal')

    def test_generic_confirmation_is_checked_not_paused(self):
        self.assertEqual(action_risk(click('Aceptar')), 'confirm')

    def test_person_keeps_captcha_login_payment_and_emission(self):
        self.assertEqual(action_risk(click('No soy un robot', role='checkbox')), 'captcha')
        self.assertEqual(action_risk(click('Entrar', risk='login')), 'login')
        self.assertEqual(action_risk(click('Pagar')), 'forbidden')
        self.assertEqual(action_risk(click('Facturar')), 'issue')


class DomainTests(unittest.TestCase):
    def test_billing_operator_redirect_is_recognized(self):
        self.assertTrue(billing_destination('https://alsea.interfactura.com/RegistroDocumento.aspx'))
        self.assertTrue(billing_destination('https://facturacion.ejemplo-comercio.mx/'))
        self.assertTrue(billing_destination('https://portal.ejemplo.mx/autofactura'))
        for url in ('https://ejemplo.mx/promociones', 'http://facturacion.ejemplo.mx/',
                    'https://accounts.google.com/factura', 'https://sat.gob.mx/factura'):
            with self.subTest(url=url):
                self.assertFalse(billing_destination(url))

    def test_redirected_operator_is_allowed_only_for_that_ticket(self):
        url = 'https://cliente.facturehoy.com/ticket'
        self.assertFalse(allowed_url(url, 'samara'))
        self.assertTrue(allowed_url(url, 'samara', '', {'https://cliente.facturehoy.com'}))


class SchemaTests(unittest.TestCase):
    def test_optional_notes_are_listed_for_strict_output(self):
        schema = strict_schema(Action)
        self.assertEqual(schema['required'], list(schema['properties']))
        self.assertNotIn('default', schema['properties']['notes'])


class IssueProofTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.runner = Runner(Store(Path(self.tmp.name)))

    async def asyncTearDown(self):
        await self.runner.close()
        self.tmp.cleanup()

    async def proof(self, text):
        page = Mock(frames=[FakeFrame(text)])
        return await self.runner.generic_issue_proof(page, PROFILE, RECEIPT)

    async def test_page_must_show_rfc_reference_and_amount(self):
        reference = RECEIPT.ticket_number or RECEIPT.billing_codes[0]
        complete = f'Ticket {reference}\nTotal ${RECEIPT.amount}\n[Campos]\nRFC: {PROFILE.rfc}'
        self.assertTrue(await self.proof(complete))
        self.assertFalse(await self.proof(complete.replace(PROFILE.rfc, 'EKU9003173C9')))
        self.assertFalse(await self.proof(complete.replace(reference, '00000000')))
        self.assertFalse(await self.proof(complete.replace(RECEIPT.amount, '1.00')))


class SelfHelpTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.store = Store(Path(self.tmp.name))
        self.runner = Runner(self.store)
        row, _ = self.store.add('test.png', 'autonomy')
        self.key = row['id']
        self.store.save_receipt(self.key, RECEIPT, ready=True)

    async def asyncTearDown(self):
        await self.runner.close()
        self.tmp.cleanup()

    async def test_help_request_is_refused_until_the_agent_tries_itself(self):
        page = FakePage()
        self.runner.context = FakeContext(page)
        ask = Action(kind='human', target=None, value=None, reason='No sé qué folio usar', risk='unknown')
        model = Mock(next_action=Mock(side_effect=[
            ask, ask, click('Facturar'),
            Action(kind='done', target=None, value=None, reason='Fin', risk='normal')]))
        with patch('facturador.runner.AI', return_value=model):
            self.runner.schedule(self.key, PROFILE)
            task = self.runner.job_tasks[self.key]
            async with asyncio.timeout(4):
                while self.runner.pending.get(self.key, {}).get('kind') != 'issue':
                    await asyncio.sleep(.01)
            self.assertEqual(model.next_action.call_count, 3)
            observation = model.next_action.call_args_list[1].args[1]
            self.assertIn('No pidas ayuda', observation)
            self.runner.decide(self.key, 'approve')
            await asyncio.wait_for(task, 5)
        self.assertEqual(page.clicks, 1)


if __name__ == '__main__':
    unittest.main()
