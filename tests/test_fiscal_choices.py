import os
import unittest
from types import SimpleNamespace

from playwright.async_api import async_playwright

from facturador.fiscal_choices import fiscal_choices, matches
from facturador.models import Action, Receipt, Target
from facturador.runner import action_risk, already_billed


class ConfirmedChoiceTests(unittest.TestCase):
    def test_ticket_field_is_not_an_issuance_button(self):
        action = Action(kind='fill', target=Target(by='role', role='textbox', name='Concepto a facturar', exact=True),
                        value='ejemplo', risk='normal', reason='Escribir el código')
        self.assertEqual(action_risk(action), 'normal')
        self.assertEqual(action_risk(action.model_copy(update={'kind': 'click'})), 'issue')

    def test_exact_confirmed_code_or_description_and_conflicting_labels(self):
        self.assertTrue(matches('internal', '612 - Personas físicas', 'regime', '612'))
        self.assertTrue(matches('internal', 'Personas Físicas con Actividades Empresariales y Profesionales', 'regime', '612'))
        self.assertTrue(matches('B', 'Tarjeta de crédito', 'payment', '04'))
        self.assertFalse(matches('04', 'Tarjeta de débito', 'payment', '04'))
        self.assertFalse(matches('04', '28 - Tarjeta de débito', 'payment', '04'))
        self.assertFalse(matches('28', '04 - Tarjeta de crédito', 'payment', '04'))
        self.assertFalse(matches('28', 'Tarjeta de crédito', 'payment', '04'))
        self.assertFalse(matches('601', '612 - Personas físicas', 'regime', '612'))
        self.assertFalse(matches('G01', 'Gastos en general', 'use', 'G03'))
        self.assertFalse(matches('internal', 'Tarjeta', 'payment', '04'))
        self.assertFalse(matches('internal', 'Crédito o débito', 'payment', '04'))

    def test_existing_invoice_is_not_a_menu_entry_or_help_text(self):
        self.assertTrue(already_billed('- alert: El ticket ya fue facturado.'))
        self.assertTrue(already_billed('- paragraph: Este código ABCDE1234567890000 ya se encuentra facturado'))
        self.assertFalse(already_billed('- link "Recuperar ticket ya facturado"'))
        self.assertFalse(already_billed('- text: Si el ticket ya fue facturado, consulta el historial.'))
        self.assertFalse(already_billed('El ticket aún no está facturado'))


@unittest.skipUnless(os.path.isfile('/usr/bin/google-chrome-stable'), 'Requiere Google Chrome oficial')
class WalmartUseFacturaTests(unittest.IsolatedAsyncioTestCase):
    async def test_use_factura_and_uso_cfdi_select_only_confirmed_code(self):
        async with async_playwright() as playwright:
            browser = await playwright.chromium.launch(executable_path='/usr/bin/google-chrome-stable')
            try:
                context = await browser.new_context(offline=True)
                page = await context.new_page()
                portal = 'https://facturacion-clientes.walmart.com/'
                await context.route('**/*', lambda route: route.fulfill(
                    content_type='text/html', body='<p>Formulario de prueba</p>')
                    if route.request.url == portal else route.abort())
                await page.goto(portal)
                profile = SimpleNamespace(regime='612', cfdi_use='G03')
                receipt = Receipt(provider='walmart', payment_code='08')
                for caption in ('*Uso Factura:', 'Uso del CFDI'):
                    with self.subTest(caption=caption):
                        await page.set_content(f'''<label for="choice">{caption}</label>
                            <select id="choice"><option value="">Seleccione</option>
                            <option value="G01">Adquisición de mercancías</option>
                            <option value="G03">Gastos en general</option></select>''')
                        self.assertEqual(await fiscal_choices(page, profile, receipt, apply=True), ('use', []))
                        self.assertEqual(await page.locator('#choice').input_value(), 'G03')
                        self.assertEqual(await fiscal_choices(page, profile, receipt), (None, []))
            finally:
                await browser.close()
