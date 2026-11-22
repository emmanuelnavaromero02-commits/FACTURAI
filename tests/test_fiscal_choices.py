import unittest

from facturador.fiscal_choices import matches
from facturador.models import Action, Target
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
