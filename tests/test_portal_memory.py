"""The portal memory keeps what solved each screen as data, with links instead of values."""
import tempfile
import unittest
from pathlib import Path

from facturador.models import Action, Profile, Receipt, Target
from facturador.portal_memory import (Attempt, Known, PortalMemory, compact, describe, locate, plain,
                                      screen_signature)

PROFILE = Profile(rfc='XAXX010101000', name='PRUEBA AUTOMATIZACION', postal_code='76146',
                  regime='612', cfdi_use='G03', email='prueba@example.com')
RECEIPT = Receipt(provider='portal_ticket', portal_url='https://facturas.tienda-ejemplo.mx/', merchant='Tienda Ejemplo',
                  amount='1137.45', date='2026-11-13', ticket_number='TKT-88231-Q', billing_codes=['WEB-55/7001'],
                  payment_code='28')
FIELDS = [{'n': 1, 'kind': 'text', 'label': 'Folio del ticket', 'key': 'folio|f1', 'filled': False},
          {'n': 2, 'kind': 'text', 'label': 'Dirección 1', 'key': 'd1|', 'filled': False},
          {'n': 3, 'kind': 'text', 'label': 'Dirección 2', 'key': 'd2|', 'filled': False},
          {'n': 4, 'kind': 'lista', 'label': 'Forma de pago', 'key': 'pago|', 'filled': True}]
SCREEN = ('https://facturas.tienda-ejemplo.mx', '/buscar', 'firma-a')


def act(kind, name, value=None, by='label', role=None):
    return Action(kind=kind, target=Target(by=by, name=name, role=role, exact=True), value=value,
                  reason='paso', risk='normal')


class SignatureTests(unittest.TestCase):
    def test_values_never_change_the_signature(self):
        typed = [{**item, 'filled': True} for item in FIELDS]
        self.assertEqual(screen_signature(FIELDS, ['Buscar']), screen_signature(typed, ['  BUSCAR ']))

    def test_fields_and_buttons_do(self):
        base = screen_signature(FIELDS, ['Buscar'])
        self.assertNotEqual(base, screen_signature(FIELDS[:-1], ['Buscar']))
        self.assertNotEqual(base, screen_signature(FIELDS, ['Buscar', 'Limpiar']))
        relabeled = [dict(FIELDS[0], label='Número de orden'), *FIELDS[1:]]
        self.assertNotEqual(base, screen_signature(relabeled, ['Buscar']))

    def test_digits_in_a_label_do_not_make_another_screen(self):
        self.assertEqual(plain('Paso 2 de 3: Correo electrónico'), 'paso # de # correo electronico')
        other = [dict(FIELDS[0], label='Folio del ticket 4471'), *FIELDS[1:]]
        marked = [dict(FIELDS[0], label='Folio del ticket 9'), *FIELDS[1:]]
        self.assertEqual(screen_signature(other, []), screen_signature(marked, []))


class DescriptorTests(unittest.TestCase):
    def test_field_is_found_again_after_the_numbers_move(self):
        target = describe(FIELDS, 3)
        self.assertEqual(target, {'by': 'campo', 'kind': 'text', 'label': 'direccion #', 'ordinal': 1, 'count': 2})
        moved = [{'n': 1, 'kind': 'email', 'label': 'Correo', 'key': 'mail|'},
                 *({**item, 'n': item['n'] + 1} for item in FIELDS)]
        self.assertEqual(locate(moved, target, 'd2|'), 4)

    def test_no_single_match_means_no_field(self):
        target = describe(FIELDS, 3)
        self.assertIsNone(locate(FIELDS[:2], target))                      # one of the equal fields left
        extra = [*FIELDS, {'n': 5, 'kind': 'text', 'label': 'Dirección 3', 'key': 'd3|'}]
        self.assertIsNone(locate(extra, target))                           # another equal one appeared
        replaced = [dict(item, key='otro|x') if item['n'] == 3 else item for item in FIELDS]
        self.assertIsNone(locate(replaced, target, 'd2|'))                 # same label, another control
        self.assertEqual(locate(replaced, target), 3)                      # a recipe has no anchor to compare
        self.assertIsNone(describe(FIELDS, 9))


class LinkTests(unittest.TestCase):
    def setUp(self):
        self.known = Known(PROFILE, RECEIPT)

    def test_typed_values_become_links(self):
        cases = {
            ('fill', 'XAXX010101000', ''): '$perfil.rfc',
            ('fill', 'xaxx010101000', ''): '$perfil.rfc|minus',
            ('fill', 'PRUEBA@EXAMPLE.COM', ''): '$perfil.email|mayus',
            ('fill', 'Prueba Automatizacion', ''): '$perfil.name|titulo',
            ('fill', '76146', ''): '$perfil.postal_code',
            ('fill', 'TKT-88231-Q', ''): '$ticket.ticket_number',
            ('fill', 'TKT88231Q', ''): '$ticket.ticket_number|compacto',
            ('fill', '88231', ''): '$ticket.ticket_number|digitos',
            ('fill', 'TKT', ''): '$ticket.ticket_number|parte1',
            ('fill', 'WEB-55/7001', ''): '$ticket.billing_codes[0]',
            ('fill', 'Tienda Ejemplo', ''): '$ticket.merchant',
            ('fill', '7001', ''): '$ticket.billing_codes[0]|parte3',
            ('fill', '1137.45', ''): '$ticket.amount|0.00',
            ('fill', '1,137.45', ''): '$ticket.amount|0,0.00',
            ('fill', '$1,137.45', ''): '$ticket.amount|$0,0.00',
            ('fill', '13/11/2026', ''): '$ticket.date|dd/mm/aaaa',
            ('fill', '2026-11-13', ''): '$ticket.date|aaaa-mm-dd',
            ('fill', '13112026', ''): '$ticket.date|ddmmaaaa',
            ('select', 'Noviembre', ''): '$ticket.date|Mes',
            ('select', '13', 'Día de compra'): '$ticket.date|dd',
            ('select', '11', 'Mes'): '$ticket.date|mm',
            ('select', '612', ''): '$perfil.regime',
            ('select', 'Gastos en general', ''): '$sat.use',
            ('select', '28 - Tarjeta de débito', ''): '$sat.payment',
            ('select', 'Ticket de caja', ''): '=Ticket de caja',
            ('press', 'Tab', ''): '=Tab',
            ('upload', 'ticket', ''): '=ticket',
        }
        for (kind, typed, label), link in cases.items():
            with self.subTest(typed=typed):
                self.assertEqual(self.known.link(kind, typed, label), link)

    def test_what_cannot_be_traced_or_could_be_personal_is_not_kept(self):
        for kind, typed, label in [('fill', 'Sucursal Centro', ''), ('fill', '5512345678', ''),
                                   ('select', 'Sucursal 4471', ''), ('select', 'PRUEBA AUTOMATIZACION SA', ''),
                                   ('select', '605 - Sueldos y Salarios', ''), ('select', 'Efectivo', ''),
                                   ('select', '13', ''), ('fill', '13', 'Referencia'), ('press', 'F5', '')]:
            with self.subTest(typed=typed):
                self.assertIsNone(self.known.link(kind, typed, label))

    def test_links_are_rebuilt_with_another_ticket(self):
        other = Known(PROFILE.model_copy(update={'rfc': 'EKU9003173C9', 'email': 'otra@example.org'}),
                      RECEIPT.model_copy(update={'ticket_number': 'TKT-10007-Z', 'amount': '89.00', 'date': '2026-12-02',
                                                 'billing_codes': []}))
        self.assertEqual(other.variant, 'M')
        self.assertEqual(other.value('$perfil.rfc'), 'EKU9003173C9')
        self.assertEqual(other.value('$ticket.ticket_number|parte2'), '10007')
        self.assertEqual(other.value('$ticket.amount|0.00'), '89.00')
        self.assertEqual(other.value('$ticket.amount|g'), '89')
        self.assertEqual(other.value('$ticket.date|dd/mm/aaaa'), '02/12/2026')
        self.assertEqual(other.value('$ticket.date|Mes'), 'Diciembre')
        self.assertEqual(other.value('=Ticket de caja'), 'Ticket de caja')
        self.assertIsNone(other.value('$ticket.billing_codes[0]'))   # this ticket has no such code
        self.assertIsNone(other.value('$sat.use'))                   # chosen on the page, not built here

    def test_personal_data_is_recognized_without_mistaking_ordinary_words(self):
        company = Known(PROFILE.model_copy(update={'rfc': 'EKU9003173C9', 'name': 'GRUPO FACTURA FACIL SA DE CV'}), RECEIPT)
        self.assertFalse(company.reveals('Emitir factura'))                   # one ordinary word of the name
        self.assertTrue(company.reveals('Bienvenido, Grupo Factura Fácil'))
        for private in ('Titular: prueba automatización', 'Enviar a prueba@example.com', 'RFC XAXX010101000',
                        'Ticket tkt 88231 q', 'Total $1,137.45', 'Cuenta 55123'):
            with self.subTest(private=private):
                self.assertTrue(self.known.reveals(private))
        for plain_text in ('Buscar ticket', 'Paso 2 de 3', 'Código postal', 'CFDI 4.0', 'Prueba de conexión'):
            with self.subTest(plain_text=plain_text):
                self.assertFalse(self.known.reveals(plain_text))

    def test_route_holds_no_ticket_data(self):
        self.assertEqual(self.known.route('/ticket/TKT-88231-Q/datos'), '/ticket/{ticket.ticket_number}/datos')
        self.assertEqual(self.known.route('/solicitud/839201/paso'), '/solicitud/#/paso')
        self.assertEqual(self.known.route('/cliente/prueba%40example.com'), '/cliente/{perfil.email}')
        self.assertEqual(self.known.route('/facturar'), '/facturar')
        self.assertEqual(self.known.route('/tienda ejemplo/facturar'), '/tienda ejemplo/facturar')   # the merchant stays


class CompactTests(unittest.TestCase):
    def step(self, kind, label, value=None, field='text'):
        return {'kind': kind, 'target': {'by': 'campo', 'kind': field, 'label': label, 'ordinal': 0, 'count': 1},
                'value': value, 'risk': 'normal'}

    def test_last_value_and_last_press_win(self):
        search = {'kind': 'click', 'target': {'by': 'role', 'role': 'button', 'name': 'Buscar', 'exact': True},
                  'value': None, 'risk': 'normal'}
        steps = [self.step('fill', 'fecha', '$ticket.date|aaaa-mm-dd'), self.step('fill', 'total', '$ticket.amount|0.00'),
                 search, self.step('fill', 'fecha', '$ticket.date|dd/mm/aaaa'), search]
        self.assertEqual(compact(steps), [self.step('fill', 'total', '$ticket.amount|0.00'),
                                          self.step('fill', 'fecha', '$ticket.date|dd/mm/aaaa'), search])

    def test_checkbox_clicked_twice_is_left_alone(self):
        box = self.step('click', 'acepto', field='checkbox')
        self.assertEqual(compact([box, self.step('fill', 'rfc', '$perfil.rfc'), box]),
                         [self.step('fill', 'rfc', '$perfil.rfc')])

    def test_a_loop_over_ticket_codes_is_left_to_the_agent(self):
        steps = [self.step('fill', 'codigo', '$ticket.billing_codes[0]'), self.step('fill', 'codigo', '$ticket.billing_codes[1]')]
        self.assertIsNone(compact(steps))


class MemoryTests(unittest.TestCase):
    def setUp(self):
        folder = tempfile.TemporaryDirectory()
        self.addCleanup(folder.cleanup)
        self.root = Path(folder.name)
        self.memory = PortalMemory(self.root)
        self.known = Known(PROFILE, RECEIPT)

    def attempt(self):
        return Attempt(self.memory, self.known)

    def learned(self, screen=SCREEN):
        attempt = self.attempt()
        attempt.step(screen, act('fill', 'Folio del ticket', 'TKT-88231-Q'), describe(FIELDS, 1), 'normal', 'ia')
        attempt.step(screen, act('click', 'Buscar', by='role', role='button'), None, 'normal', 'ia')
        return attempt

    def test_nothing_is_written_until_the_invoice_is_verified(self):
        attempt = self.learned()
        self.assertEqual(attempt.recall(SCREEN, True), (None, True))
        attempt.abandon()
        self.assertIsNone(self.memory.screen(SCREEN, 'F'))
        self.assertEqual(self.learned().commit(), 1)
        steps, family = self.attempt().recall(SCREEN, False)
        self.assertFalse(family)
        self.assertEqual([step['value'] for step in steps], ['$ticket.ticket_number', None])
        self.assertEqual(steps[0]['target'], {'by': 'campo', 'kind': 'text', 'label': 'folio del ticket',
                                              'ordinal': 0, 'count': 1})

    def test_an_unlinked_value_leaves_the_screen_to_the_agent(self):
        attempt = self.learned()
        attempt.step(SCREEN, act('fill', 'Sucursal', 'Centro Histórico'), None, 'normal', 'ia')
        self.assertEqual(attempt.screens(), [{'key': list(SCREEN), 'steps': [], 'needs_ai': True, 'replayed': False}])
        attempt.commit()
        self.assertEqual(self.attempt().recall(SCREEN, True), (None, False))
        # A later clean pass with the agent makes it repeatable again.
        self.assertEqual(self.learned().commit(), 1)
        self.assertTrue(self.attempt().recall(SCREEN, False)[0])

    def test_a_target_named_after_personal_data_is_not_kept(self):
        attempt = self.attempt()
        attempt.step(SCREEN, act('click', 'Continuar como PRUEBA AUTOMATIZACION', by='text'), None, 'normal', 'ia')
        self.assertTrue(attempt.screens()[0]['needs_ai'])

    def test_back_undoes_the_way_into_a_dead_end(self):
        menu, wrong = ('https://x.mx', '/', 'menu'), ('https://x.mx', '/otra', 'otra')
        attempt = self.attempt()
        attempt.step(menu, act('click', 'Consultar', by='role', role='link'), None, 'normal', 'ia')
        attempt.step(wrong, act('fill', 'Folio del ticket', 'TKT-88231-Q'), describe(FIELDS, 1), 'normal', 'ia')
        attempt.mark(wrong, 'back')
        attempt.step(menu, act('click', 'Facturar ticket', by='role', role='link'), None, 'normal', 'ia')
        screens = attempt.screens()
        self.assertEqual([screen['key'][2] for screen in screens], ['menu'])
        self.assertEqual([step['target']['name'] for step in screens[0]['steps']], ['Facturar ticket'])

    def test_after_a_credential_nothing_else_is_noted(self):
        attempt = self.learned()
        other = ('https://x.mx', '/cuenta', 'cuenta')
        attempt.block(other)
        attempt.step(other, act('fill', 'Folio del ticket', 'TKT-88231-Q'), describe(FIELDS, 1), 'normal', 'ia')
        self.assertEqual([screen['key'][2] for screen in attempt.screens()], ['firma-a'])

    def test_three_failures_in_a_row_switch_a_recipe_off_until_it_is_learned_again(self):
        self.learned().commit()
        for expected in (False, False, True):
            attempt = self.attempt()
            steps, _ = attempt.recall(SCREEN, False)
            attempt.replay_started(SCREEN, steps, False)
            self.assertEqual(attempt.replay_failed(SCREEN), expected)
            self.assertFalse(attempt.replay_failed(SCREEN))            # once per attempt
        self.assertEqual(self.memory.screen(SCREEN, 'F')['active'], 0)
        self.assertEqual(self.attempt().recall(SCREEN, True), (None, False))
        self.learned().commit()
        row = self.memory.screen(SCREEN, 'F')
        self.assertEqual((row['active'], row['streak']), (1, 0))

    def test_a_failed_attempt_counts_against_what_it_repeated_and_a_success_clears_it(self):
        self.learned().commit()
        attempt = self.attempt()
        steps, _ = attempt.recall(SCREEN, False)
        attempt.replay_started(SCREEN, steps, False)
        attempt.replay_done(SCREEN)
        attempt.abandon()
        self.assertEqual(self.memory.screen(SCREEN, 'F')['streak'], 1)
        attempt = self.attempt()
        attempt.replay_started(SCREEN, steps, False)
        attempt.step(SCREEN, act('fill', 'Folio del ticket', 'TKT-88231-Q'), describe(FIELDS, 1), 'normal', 'memoria')
        attempt.replay_done(SCREEN)
        self.assertEqual(attempt.commit(), 0)
        row = self.memory.screen(SCREEN, 'F')
        self.assertEqual((row['streak'], row['successes'], len(row['steps'])), (0, 2, 2))

    def test_repeated_screen_a_person_also_worked_on_keeps_its_recipe_untouched(self):
        self.learned().commit()
        attempt = self.attempt()
        steps, _ = attempt.recall(SCREEN, False)
        attempt.replay_started(SCREEN, steps, False)
        attempt.step(SCREEN, act('fill', 'Folio del ticket', 'TKT-88231-Q'), describe(FIELDS, 1), 'normal', 'memoria')
        attempt.replay_done(SCREEN)
        attempt.taint(SCREEN)
        self.assertEqual(attempt.screens(), [])
        attempt.commit()
        row = self.memory.screen(SCREEN, 'F')
        self.assertEqual((row['successes'], row['needs_ai'], len(row['steps'])), (1, 0, 2))

    def test_family_needs_two_successes_and_the_same_kind_of_taxpayer(self):
        other = ('https://facturas.otro-comercio.mx', '/buscar', 'firma-a')
        third = ('https://facturas.tercero.mx', '/buscar', 'firma-a')
        self.learned().commit()
        self.assertEqual(self.attempt().recall(other, True), (None, True))
        self.learned(other).commit()
        steps, family = self.attempt().recall(third, True)
        self.assertTrue(family and steps)
        self.assertEqual(self.attempt().recall(third, False), (None, False))   # portal not approved for the ticket
        company = Attempt(self.memory, Known(PROFILE.model_copy(update={'rfc': 'EKU9003173C9'}), RECEIPT))
        self.assertEqual(company.recall(third, True), (None, True))

    def test_attempt_parked_until_its_files_are_verified(self):
        self.learned().hold('ticket-1')
        self.learned(('https://x.mx', '/buscar', 'firma-b')).hold('ticket-2')
        self.assertEqual(self.memory.settle({'ticket-1': 'issued_waiting_files', 'ticket-2': 'blocked'}.get), 0)
        self.assertIsNone(self.memory.screen(SCREEN, 'F'))
        self.assertEqual(self.memory.settle({'ticket-1': 'verified'}.get), 1)
        self.assertIsNotNone(self.memory.screen(SCREEN, 'F'))
        self.assertIsNone(self.memory.screen(('https://x.mx', '/buscar', 'firma-b'), 'F'))
        self.assertEqual(self.memory.settle({'ticket-1': 'verified'}.get), 0)

    def test_the_file_holds_no_personal_or_ticket_data(self):
        attempt = self.learned()
        attempt.step(SCREEN, act('fill', 'RFC', PROFILE.rfc), None, 'normal', 'ia')
        attempt.step(SCREEN, act('fill', 'Correo', PROFILE.email), None, 'normal', 'ia')
        attempt.step(SCREEN, act('fill', 'Total', '1,137.45'), None, 'normal', 'ia')
        attempt.step(SCREEN, act('fill', 'Código', 'WEB-55/7001'), None, 'normal', 'ia')
        attempt.commit()
        attempt.hold('ticket-en-espera')
        stored = b''.join(path.read_bytes() for path in self.root.glob('memoria_portales.sqlite*'))
        for private in (PROFILE.rfc, PROFILE.email, PROFILE.name, 'TKT-88231-Q', '88231', 'WEB-55/7001', '1137.45',
                        '1,137.45', PROFILE.postal_code):
            self.assertNotIn(private.encode(), stored)
        self.assertIn(b'$perfil.rfc', stored)

    def test_an_unreadable_memory_never_stops_a_ticket(self):
        self.learned().commit()
        for path in self.root.glob('memoria_portales.sqlite*'):
            path.unlink()
        self.memory.path.write_bytes(b'esto no es una base de datos')
        attempt = self.attempt()
        self.assertEqual(attempt.recall(SCREEN, True), (None, True))
        self.assertEqual(self.learned().commit(), 0)
        self.assertFalse(self.memory.failed(SCREEN, 'F', []))


if __name__ == '__main__':
    unittest.main()
