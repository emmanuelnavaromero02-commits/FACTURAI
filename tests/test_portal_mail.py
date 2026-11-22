from email.message import EmailMessage
import unittest

from facturador.portal_mail import matches_sender, registration_actions

ORIGIN = 'https://factura.comercio.example.com'
USER = 'cliente@example.org'


def email_message(sender, recipient, text, html=None):
    msg = EmailMessage()
    msg['From'] = sender
    msg['To'] = recipient
    msg.set_content(text)
    if html:
        msg.add_alternative(html, subtype='html')
    return msg


class RegistrationMailTests(unittest.TestCase):
    def test_selected_mail_only_offers_explicit_same_origin_link_and_labeled_temporary(self):
        msg = email_message('registro@factura.comercio.example.com', USER, 'Contraseña temporal: Ejemplo+123',
                            '<a href="https://factura.comercio.example.com/cuenta/activate?k=demo">Verificar cuenta</a>'
                            '<a href="https://otra.example.net/activate?k=demo">Activar otra cuenta</a>')
        self.assertEqual(registration_actions(msg, ORIGIN, USER), {
            'activate': ORIGIN + '/cuenta/activate?k=demo', 'temporary': 'Ejemplo+123'})

    def test_other_sender_or_recipient_is_rejected(self):
        for sender, recipient in [('a@example.net', USER), ('registro@factura.comercio.example.com', 'otro@example.org')]:
            with self.subTest(sender=sender, recipient=recipient), self.assertRaises(ValueError):
                registration_actions(email_message(sender, recipient, 'Clave temporal: Ejemplo+123'), ORIGIN, USER)

    def test_multiple_secrets_and_cross_origin_links_are_not_guessed(self):
        msg = email_message('registro@factura.comercio.example.com', USER,
                            'Clave temporal: Ejemplo+123\nClave temporal: Otra+456\nhttps://externo.example.net/verificar')
        with self.assertRaises(ValueError):
            registration_actions(msg, ORIGIN, USER)

    def test_private_suffix_tenants_cannot_read_each_others_registration(self):
        self.assertFalse(matches_sender('registro@otro.github.io', 'tienda.github.io'))

    def test_external_redirect_destination_in_html_is_not_selected_by_its_label(self):
        msg = email_message('registro@factura.comercio.example.com', USER, 'Revisa el enlace.',
                            '<a href="https://externo.example.net">Activar cuenta</a>')
        with self.assertRaises(ValueError):
            registration_actions(msg, ORIGIN, USER)
