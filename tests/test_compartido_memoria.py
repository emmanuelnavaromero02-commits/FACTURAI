"""What one account learns about a screen the others repeat, and nothing of anybody leaves an account.

Data folders stand for the accounts of one installation and attempts are driven by hand. Nothing
here reaches the network: no portal, no model, no browser. Every person and ticket is made up.
"""
import asyncio
import json
import shutil
import sqlite3
import tempfile
import unittest
from contextlib import closing
from pathlib import Path
from unittest.mock import patch

from acceso_dobles import make_entrance, use_master_key
from compartido_dobles import (ANA, BETO, CARLA, DANTE, ELSA, FIDEL, FIELDS, FIRST, FOLIO, MEMORY, ORIGIN, SCREEN, SEARCH,
                               SECOND, SIGNATURE, THIRD, Installation, SharedMemoryCase, act, button, person, rows, tables,
                               ticket)
from facturador import compartido
from facturador.models import Profile
from facturador.portal_memory import (Attempt, Commons, Known, PortalMemory, common_screen, common_steps, describe,
                                      screen_signature)
from facturador.registro_datos import guardar_datos


class CommonRecipeTests(unittest.TestCase):
    """What a recipe must be made of to live in the file every account reads."""

    def test_links_fields_buttons_and_keys_are_common(self):
        field = FOLIO['target']
        steps = [FOLIO, SEARCH,
                 {'kind': 'fill', 'target': field, 'value': '$perfil.rfc|minus', 'risk': 'normal'},
                 {'kind': 'fill', 'target': field, 'value': '$ticket.amount|$0,0.00', 'risk': 'normal'},
                 {'kind': 'fill', 'target': field, 'value': '$ticket.billing_codes[1]|parte2', 'risk': 'normal'},
                 {'kind': 'fill', 'target': field, 'value': '=', 'risk': 'normal'},
                 {'kind': 'select', 'target': {**field, 'kind': 'lista'}, 'value': '$sat.payment', 'risk': 'normal'},
                 {'kind': 'select', 'target': {**field, 'kind': 'lista'}, 'value': '$ticket.date|Mes', 'risk': 'normal'},
                 {'kind': 'press', 'target': field, 'value': '=Enter', 'risk': 'normal'},
                 {'kind': 'click', 'target': {**field, 'kind': 'checkbox'}, 'value': None, 'risk': 'normal'},
                 {'kind': 'upload', 'target': None, 'value': '=ticket', 'risk': 'normal'},
                 {**SEARCH, 'target': {**SEARCH['target'], 'role': 'link', 'name': 'Facturación 4.0'}, 'risk': 'issue'}]
        for step in steps:
            self.assertTrue(common_steps([step]), step)
        self.assertTrue(common_screen(SCREEN, 'F', steps))
        self.assertTrue(common_screen(list(SCREEN), 'M', steps))

    def test_a_literal_a_choice_free_text_or_anything_unknown_is_not(self):
        field, named = FOLIO['target'], SEARCH['target']
        refused = {
            'literal of a list': {'kind': 'select', 'target': {**field, 'kind': 'lista'}, 'value': '=Jalisco', 'risk': 'normal'},
            'typed text': {**FOLIO, 'value': '=Sucursal Centro'},
            'unknown link': {**FOLIO, 'value': '$cuenta.clave'},
            'unknown style': {**FOLIO, 'value': '$perfil.rfc|base64'},
            'link with a tail': {**FOLIO, 'value': '$perfil.rfc y algo más'},
            'SAT choice written in a field': {**FOLIO, 'value': '$sat.payment'},
            'unknown key': {'kind': 'press', 'target': field, 'value': '=F5', 'risk': 'normal'},
            'another file': {'kind': 'upload', 'target': None, 'value': '=constancia', 'risk': 'normal'},
            'click that carries a value': {**SEARCH, 'value': '=algo'},
            'radio': {'kind': 'click', 'target': {**field, 'kind': 'radio'}, 'value': None, 'risk': 'normal'},
            'option': {**SEARCH, 'target': {**named, 'role': 'option', 'name': 'Jalisco'}},
            'row': {**SEARCH, 'target': {**named, 'role': 'row'}},
            'any text of the page': {**SEARCH, 'target': {**named, 'by': 'text', 'role': None}},
            'label of something that is not a field': {**SEARCH, 'target': {**named, 'by': 'label', 'role': None}},
            'name with a number': {**SEARCH, 'target': {**named, 'name': 'Caja 12'}},
            'name with an address': {**SEARCH, 'target': {**named, 'name': 'Enviar a a@b.example'}},
            'endless name': {**SEARCH, 'target': {**named, 'name': 'x' * 81}},
            'label with digits': {**FOLIO, 'target': {**field, 'label': 'folio 4471'}},
            'label with capitals': {**FOLIO, 'target': {**field, 'label': 'Folio'}},
            'field out of range': {**FOLIO, 'target': {**field, 'ordinal': 1}},
            'extra data in a target': {**FOLIO, 'target': {**field, 'valor': ANA.rfc}},
            'extra data in a step': {**FOLIO, 'nota': ANA.rfc},
            'missing target': {**FOLIO, 'target': None},
            'navigation': {'kind': 'goto', 'target': None, 'value': ORIGIN + '/otra', 'risk': 'normal'},
            'unknown risk': {**SEARCH, 'risk': 'login'},
            'not a step': 'Buscar ticket',
        }
        for name, step in refused.items():
            self.assertFalse(common_steps([FOLIO, step]), name)
        for steps in ([], None, 'pasos', [FOLIO] * 41, {'kind': 'click'}):
            self.assertFalse(common_steps(steps), steps)

    def test_only_a_public_portal_and_a_path_of_words_make_a_common_screen(self):
        steps = [FOLIO, SEARCH]
        for key in (('http://facturas.tienda-ejemplo.mx', '/buscar', SIGNATURE), ('', '/buscar', SIGNATURE),
                    ('https://localhost', '/buscar', SIGNATURE), (ORIGIN + '/', '/buscar', SIGNATURE),
                    (ORIGIN, '/(S(lit3py55t21z5v55vlm25s55))/buscar.aspx', SIGNATURE), (ORIGIN, '/t/9f8a7b6c5d4e', SIGNATURE),
                    (ORIGIN, '/buscar', 'firma-a'), (ORIGIN, '/buscar', SIGNATURE.upper()), (ORIGIN, '/buscar'),
                    (ORIGIN, None, SIGNATURE), None):
            self.assertFalse(common_screen(key, 'F', steps), key)
        self.assertFalse(common_screen(SCREEN, 'X', steps))
        self.assertFalse(common_screen(SCREEN, 'F', [FOLIO, {**FOLIO, 'value': '=texto'}]))


class SharedMemoryTests(SharedMemoryCase):
    def test_what_one_account_verifies_the_next_one_repeats(self):
        self.assertEqual(self.recalled(self.beto, BETO, SECOND), (None, False))
        self.assertEqual(self.solved(self.ana, ANA, FIRST).commit(), 1)
        steps, family = self.recalled(self.beto, BETO, SECOND)
        self.assertFalse(family)
        self.assertEqual(steps, [FOLIO, SEARCH])
        # The links are written again with the other person's ticket.
        self.assertEqual(Known(BETO, SECOND).value(steps[0]['value']), 'TKT-10457-Z')
        # Each account keeps its own file as a single person does; Beto has verified nothing himself yet.
        self.assertEqual(PortalMemory(self.ana).screen(SCREEN, 'F')['successes'], 1)
        self.assertIsNone(PortalMemory(self.beto).screen(SCREEN, 'F'))
        self.assertEqual((self.row()['successes'], self.row()['accounts']), (1, 1))
        # His own verified invoice with those steps counts him once, however many times he repeats them.
        self.assertEqual(self.repeated(self.beto, BETO, SECOND).commit(), 0)
        self.repeated(self.beto, BETO, THIRD).commit()
        self.repeated(self.ana, ANA, FIRST).commit()
        self.assertEqual((self.row()['successes'], self.row()['accounts'], self.row()['active']), (4, 2, 1))
        self.assertEqual(PortalMemory(self.beto).screen(SCREEN, 'F')['successes'], 2)

    def test_nothing_leaves_an_account_before_its_invoice_is_verified(self):
        attempt = self.solved(self.ana, ANA, FIRST)
        self.assertIsNone(self.row())
        attempt.hold('ticket-de-ana')
        self.assertEqual(self.recalled(self.beto, BETO, SECOND), (None, False))
        memory = PortalMemory(self.ana)
        self.assertEqual(memory.settle({'ticket-de-ana': 'issued_waiting_files'}.get), 0)
        self.assertIsNone(self.row())
        # Its files arrived and matched: what that attempt did is learned, and shared, then.
        self.assertEqual(memory.settle({'ticket-de-ana': 'verified'}.get), 1)
        self.assertEqual(self.recalled(self.beto, BETO, SECOND)[0], [FOLIO, SEARCH])
        # A failed attempt teaches nobody anything.
        other = (ORIGIN, '/otra', SIGNATURE)
        self.solved(self.ana, ANA, FIRST, other).abandon()
        self.assertIsNone(self.row(other))

    def test_shared_file_holds_nothing_of_the_person_nor_of_the_ticket(self):
        guardar_datos(self.ana, {'nombres': 'Ana', 'apellido_paterno': 'Ficticia', 'telefono': '5500000001',
                                 'calle': 'Avenida Imaginaria', 'numero_exterior': '742', 'colonia': 'Jardines del Bosque',
                                 'municipio': 'Villa Inventada', 'estado': 'Jalisco', 'fecha_nacimiento': '1980-01-01'})
        typed = {'RFC': ANA.rfc, 'Nombre o razón social': ANA.name, 'Correo electrónico': ANA.email,
                 'Código postal': ANA.postal_code, 'Total pagado': '1,137.45', 'Fecha de compra': '13/11/2026',
                 'Código de facturación': 'WEB-55/7001', 'Folio del ticket': FIRST.ticket_number}
        form = [{'n': number, 'kind': 'text', 'label': label, 'key': f'c{number}|', 'filled': False}
                for number, label in enumerate(typed, 1)]
        screen = (ORIGIN, '/ticket/' + FIRST.ticket_number + '/datos', screen_signature(form, ['Continuar']))
        attempt = Attempt(PortalMemory(self.ana), Known(ANA, FIRST))
        screen = (screen[0], attempt.known.route(screen[1]), screen[2])
        for number, (label, value) in enumerate(typed.items(), 1):
            attempt.step(screen, act('fill', label, value), describe(form, number), 'normal', 'ia')
        attempt.step(screen, button('Continuar'), None, 'issue', 'ia')
        attempt.hold('ticket-en-espera')  # What waits for its files stays in the account's own file.
        self.assertEqual(attempt.commit(), 1)
        steps, _ = self.recalled(self.beto, BETO, SECOND, screen)
        self.assertEqual([step['value'] for step in steps],
                         ['$perfil.rfc', '$perfil.name', '$perfil.email', '$perfil.postal_code', '$ticket.amount|0,0.00',
                          '$ticket.date|dd/mm/aaaa', '$ticket.billing_codes[0]', '$ticket.ticket_number', None])
        self.assert_absent(ANA.rfc, ANA.email, ANA.name, 'FICTICIA', 'PRIMERA', ANA.postal_code, ANA.whatsapp_phone,
                           '5500000001', FIRST.ticket_number, '88231', 'WEB-55/7001', '1137.45', '1,137.45', '13/11/2026',
                           '2026-11-13', 'Imaginaria', 'Jardines', 'Inventada', 'Jalisco', '1980-01-01',
                           self.ana.name, 'ticket-en-espera')
        stored = self.stored()
        for common in (b'$perfil.rfc', b'$ticket.amount|0,0.00', ORIGIN.encode(), b'/ticket/{ticket.ticket_number}/datos',
                       b'correo electronico', b'Continuar'):
            self.assertIn(common, stored)
        # The shared file has no table for what waits for its files, nor for what an account notes for itself.
        self.assertEqual(tables(self.shared / MEMORY), {'screens', 'store'})
        self.assertEqual(tables(self.ana / MEMORY), {'screens', 'pending', 'shared_marks'})

    def test_a_choice_among_options_stays_in_the_account_that_made_it(self):
        chosen = {'literal of a list': (act('select', 'Estado', 'Jalisco'), describe(FIELDS, 3)),
                  'radio': (act('click', 'Con desglose'), describe(FIELDS, 4)),
                  'option of a made-up list': (act('click', 'Jalisco', by='role', role='option'), None),
                  'text that may be a row of data': (act('click', 'Sucursal Centro', by='text'), None)}
        for number, (name, extra) in enumerate(chosen.items()):
            screen = (ORIGIN, f'/paso{number}', SIGNATURE)
            with self.subTest(case=name):
                attempt = self.solved(self.ana, ANA, FIRST, screen, [extra])
                self.assertEqual([(item['needs_ai'], item['shared']) for item in attempt.export()['screens']], [(False, False)])
                self.assertEqual(attempt.commit(), 1)
                self.assertIsNone(self.row(screen))
                self.assertEqual(self.recalled(self.beto, BETO, SECOND, screen), (None, False))
                # Whoever made the choice keeps repeating it, as a single person does.
                self.assertEqual(len(self.recalled(self.ana, ANA, FIRST, screen)[0]), 3)
        self.assert_absent('Jalisco', 'Sucursal Centro', 'Con desglose')

    def test_a_label_or_a_name_that_says_something_of_the_person_stays_too(self):
        guardar_datos(self.ana, {'colonia': 'Jardines del Bosque'})
        told = {'one word of the name': (button('Seguir como Ficticia'), None),
                'letters of the RFC in a label that lost its digits': (act('fill', 'Receptor', ANA.email), describe(FIELDS, 5)),
                'a word the person saved for registrations': (act('click', 'Jardines', by='role', role='link'), None),
                'a number that names a record': (button('Caja 12'), None)}
        for number, (name, extra) in enumerate(told.items()):
            screen = (ORIGIN, f'/dato{number}', SIGNATURE)
            with self.subTest(case=name):
                attempt = self.solved(self.ana, ANA, FIRST, screen, [extra])
                self.assertEqual([(item['needs_ai'], item['shared']) for item in attempt.export()['screens']], [(False, False)])
                attempt.commit()
                self.assertEqual(self.recalled(self.beto, BETO, SECOND, screen), (None, False))
                self.assertEqual(len(self.recalled(self.ana, ANA, FIRST, screen)[0]), 3)
        # The same steps are common when they say nothing of whoever takes them.
        screen = (ORIGIN, '/dato9', SIGNATURE)
        self.solved(self.beto, BETO, SECOND, screen, [(button('Seguir como Ficticia'), None)]).commit()
        self.assertEqual(len(self.recalled(self.ana, ANA, FIRST, screen)[0]), 3)
        # A path that may carry a session or that names something of hers, and a portal that is not a
        # public one, stay as well.
        for place in ((ORIGIN, '/(S(lit3py55t21z5v55vlm25s55))/buscar.aspx', SIGNATURE),
                      (ORIGIN, '/sucursal/jardines/buscar', SIGNATURE), ('http://127.0.0.1:8080', '/buscar', SIGNATURE)):
            self.solved(self.ana, ANA, FIRST, place).commit()
            self.assertEqual(self.recalled(self.beto, BETO, SECOND, place), (None, False))
            self.assertTrue(self.recalled(self.ana, ANA, FIRST, place)[0])
        self.assert_absent('Jardines', 'lit3py55', 'Caja 12', 'aaaa#aaa', '127.0.0.1')

    def test_nothing_is_shared_while_what_is_personal_cannot_be_told(self):
        (self.ana / 'profile.json').write_text('{esto no es json', encoding='utf-8')
        attempt = self.solved(self.ana, ANA, FIRST)
        self.assertEqual([item['shared'] for item in attempt.export()['screens']], [False])
        self.assertEqual(attempt.commit(), 1)      # The account learns as always.
        self.assertIsNone(self.row())
        self.assertEqual(self.recalled(self.beto, BETO, SECOND), (None, False))

    def test_an_account_goes_first_by_what_it_verified_itself(self):
        self.solved(self.ana, ANA, FIRST).commit()
        # Beto's model solved the same screen another way. The latest verified solution is the published one.
        total = (act('fill', 'Total pagado', SECOND.amount), describe(FIELDS, 2))
        self.solved(self.beto, BETO, SECOND, extra=[total]).commit()
        row = self.row()
        self.assertEqual((len(json.loads(row['steps'])), row['successes'], row['accounts']), (3, 1, 1))
        carla = self.account(CARLA)
        self.assertEqual(len(self.recalled(carla, CARLA, THIRD)[0]), 3)
        # Ana keeps repeating what works for her, and her next verified invoice leaves the published one alone.
        self.assertEqual(self.recalled(self.ana, ANA, FIRST)[0], [FOLIO, SEARCH])
        self.repeated(self.ana, ANA, FIRST).commit()
        row = self.row()
        self.assertEqual((len(json.loads(row['steps'])), row['successes'], row['accounts']), (3, 1, 1))
        self.assertEqual(PortalMemory(self.ana).screen(SCREEN, 'F')['successes'], 2)

    def test_what_two_accounts_verified_is_not_replaced_on_the_word_of_one(self):
        def another_way(root, profile, receipt):
            total = (act('fill', 'Total pagado', receipt.amount), describe(FIELDS, 2))
            self.assertEqual(self.solved(root, profile, receipt, extra=[total]).commit(), 1)

        def published():
            row = self.row()
            return len(json.loads(row['steps'])), row['accounts'], row['streak'], row['active']

        carla, dante, elsa = (self.account(profile) for profile in (CARLA, DANTE, ELSA))
        self.solved(self.ana, ANA, FIRST).commit()
        self.repeated(carla, CARLA, THIRD).commit()
        # Beto's model solved the screen another way: with two accounts behind the other, his stays with him.
        another_way(self.beto, BETO, SECOND)
        self.assertEqual(published(), (2, 2, 0, 1))
        self.assertEqual(len(self.recalled(self.beto, BETO, SECOND)[0]), 3)
        self.assertEqual(self.recalled(dante, DANTE, THIRD)[0], [FOLIO, SEARCH])
        # Nor does failing with it first open the way: one account in a row is still one account.
        self.assertFalse(self.failed(dante, DANTE, THIRD))
        another_way(dante, DANTE, THIRD)
        self.assertEqual(published(), (2, 2, 1, 1))
        # A second account in a row fails with it: the next verified solution takes its place.
        self.assertFalse(self.failed(elsa, ELSA, THIRD))
        another_way(elsa, ELSA, THIRD)
        self.assertEqual(published(), (3, 1, 0, 1))
        # Whoever verified the old one keeps it, and whoever found the new one is counted when it proves itself again.
        self.assertEqual(self.recalled(self.ana, ANA, FIRST)[0], [FOLIO, SEARCH])
        self.repeated(dante, DANTE, THIRD).commit()
        self.assertEqual(published(), (3, 2, 0, 1))

    def test_a_recipe_travels_to_another_portal_only_with_two_accounts_behind_it(self):
        one, two, three = ((f'https://facturas.comercio-{name}.mx', '/buscar', SIGNATURE) for name in ('uno', 'dos', 'tres'))
        self.solved(self.ana, ANA, FIRST, one).commit()
        self.solved(self.ana, ANA, FIRST, two).commit()
        # Ana has two successes of her own: a third portal of that shape is hers to repeat, as always.
        self.assertEqual(self.recalled(self.ana, ANA, FIRST, three, True), ([FOLIO, SEARCH], True))
        # For Beto those are the successes of one account: on a portal nobody solved, the model decides.
        self.assertEqual(self.recalled(self.beto, BETO, SECOND, three, True), (None, True))
        # Where Ana did solve it he repeats her steps, and his verified invoice puts a second account behind them.
        self.repeated(self.beto, BETO, SECOND, one).commit()
        carla = self.account(CARLA)
        self.assertEqual(self.recalled(carla, CARLA, THIRD, three, True), ([FOLIO, SEARCH], True))
        self.assertEqual(self.recalled(self.beto, BETO, SECOND, three, True), ([FOLIO, SEARCH], True))
        self.assertEqual(self.recalled(carla, CARLA, THIRD, three, False), (None, False))  # Portal not approved for the ticket.
        company = Profile(**{**CARLA.model_dump(), 'rfc': 'CCC010101CCC'})
        self.assertEqual(self.recalled(carla, company, THIRD, three, True), (None, True))   # Another kind of taxpayer.

    def test_one_account_alone_never_switches_off_what_works_for_the_others(self):
        self.solved(self.ana, ANA, FIRST).commit()
        carla = self.account(CARLA)
        for round_ in range(3):
            for expected in (False, False, True):
                self.assertEqual(self.failed(self.beto, BETO, SECOND), expected)
            # Three failures in a row switched it off for Beto, in his own file, as for a single person.
            self.assertEqual(self.recalled(self.beto, BETO, SECOND, family=True), (None, False))
            self.assertIsNone(self.failed(self.beto, BETO, SECOND))
            row = self.row()
            self.assertEqual((row['active'], row['streak'], row['failures']), (1, 1, round_ + 1))
            self.assertEqual(self.recalled(carla, CARLA, THIRD)[0], [FOLIO, SEARCH])
            # His model solves the screen again: for him it is learned again, and his own success clears his failure.
            self.solved(self.beto, BETO, SECOND).commit()
            self.assertEqual((self.row()['active'], self.row()['streak']), (1, 0))
        self.assertEqual(self.row()['accounts'], 2)

    def test_three_accounts_failing_in_a_row_switch_a_recipe_off_for_everyone_until_it_is_learned_again(self):
        self.solved(self.ana, ANA, FIRST).commit()
        carla, dante, elsa, fidel = (self.account(profile) for profile in (CARLA, DANTE, ELSA, FIDEL))
        self.assertFalse(self.failed(self.beto, BETO, SECOND))
        self.assertFalse(self.failed(self.beto, BETO, SECOND))      # The same account again: it does not count twice.
        self.assertEqual(self.row()['streak'], 1)
        # Somebody succeeds with it: the count starts over, as it does for a single person.
        self.repeated(carla, CARLA, THIRD).commit()
        self.assertEqual(self.row()['streak'], 0)
        self.assertTrue(self.failed(self.beto, BETO, SECOND))       # His third in a row: off for him.
        self.assertFalse(self.failed(carla, CARLA, THIRD))
        self.assertEqual((self.row()['streak'], self.row()['active']), (2, 1))
        self.assertEqual(self.recalled(elsa, ELSA, THIRD)[0], [FOLIO, SEARCH])
        self.assertFalse(self.failed(dante, DANTE, THIRD))          # The third different account in a row.
        self.assertEqual((self.row()['streak'], self.row()['active']), (3, 0))
        self.assertEqual(self.recalled(elsa, ELSA, THIRD, family=True), (None, False))
        self.assertIsNone(self.failed(elsa, ELSA, THIRD))
        # Whoever verified it keeps what works for them.
        self.assertEqual(self.recalled(self.ana, ANA, FIRST)[0], [FOLIO, SEARCH])
        # The model solves it again for somebody: it is back for everyone.
        self.assertEqual(self.solved(elsa, ELSA, THIRD).commit(), 1)
        self.assertEqual((self.row()['streak'], self.row()['active']), (0, 1))
        self.assertEqual(self.recalled(fidel, FIDEL, THIRD)[0], [FOLIO, SEARCH])

    def test_an_account_whose_run_dies_between_its_note_and_the_count_is_never_counted_twice(self):
        self.solved(self.ana, ANA, FIRST).commit()
        noted = Commons._note

        def dies(commons, *values):
            noted(commons, *values)
            raise sqlite3.OperationalError('el proceso murió aquí')

        with patch.object(Commons, '_note', dies):
            self.assertEqual(self.repeated(self.beto, BETO, SECOND).commit(), 0)
        self.assertEqual((self.row()['successes'], self.row()['accounts']), (1, 1))     # Nothing was counted.
        self.assertEqual(PortalMemory(self.beto).screen(SCREEN, 'F')['successes'], 1)   # His own file did learn.
        # His note says he was already counted: he is counted one time less, never one time more.
        self.repeated(self.beto, BETO, THIRD).commit()
        self.assertEqual((self.row()['successes'], self.row()['accounts']), (2, 1))
        self.repeated(self.account(CARLA), CARLA, THIRD).commit()
        self.assertEqual((self.row()['successes'], self.row()['accounts']), (3, 2))

    def test_what_is_read_is_checked_like_what_is_written(self):
        PortalMemory(self.ana)
        literal = {'kind': 'select', 'target': {'by': 'campo', 'kind': 'lista', 'label': 'estado', 'ordinal': 0, 'count': 1},
                   'value': '=Jalisco', 'risk': 'normal'}
        with closing(sqlite3.connect(self.shared / MEMORY)) as db, db:
            for path, steps in (('/buscar', [literal, SEARCH]), ('/roto', 'esto no es una receta')):
                db.execute('INSERT INTO screens VALUES(?,?,?,?,?,0,9,0,0,1,0,9)',
                           (ORIGIN, path, SIGNATURE, 'F', steps if isinstance(steps, str) else json.dumps(steps)))
        for path in ('/buscar', '/roto'):
            self.assertEqual(self.recalled(self.beto, BETO, SECOND, (ORIGIN, path, SIGNATURE), True), (None, True))

    def test_a_shared_file_that_starts_again_counts_every_account_again(self):
        ana, beto = PortalMemory(self.ana), PortalMemory(self.beto)  # Long-lived, as each account's engine keeps it.

        def verified(memory, profile, receipt):
            attempt = Attempt(memory, Known(profile, receipt))
            attempt.step(SCREEN, act('fill', 'Folio del ticket', receipt.ticket_number), describe(FIELDS, 1), 'normal', 'ia')
            attempt.step(SCREEN, button('Buscar ticket'), None, 'normal', 'ia')
            attempt.commit()

        verified(ana, ANA, FIRST)
        verified(beto, BETO, SECOND)
        self.assertEqual((self.row()['successes'], self.row()['accounts']), (2, 2))
        first = ana.commons.store
        shutil.rmtree(self.shared)      # Emptied by hand, folder and all.
        verified(ana, ANA, FIRST)       # It finds the file gone: nothing breaks, and it is prepared again.
        verified(ana, ANA, FIRST)
        verified(beto, BETO, SECOND)
        verified(beto, BETO, SECOND)
        self.assertNotEqual(ana.commons.store, first)
        self.assertEqual(self.row()['accounts'], 2)
        self.assertEqual(ana.screen(SCREEN, 'F')['successes'], 3)   # Its own file never noticed.

    def test_a_shared_file_that_cannot_be_used_never_stops_an_account(self):
        memory = PortalMemory(self.ana)
        for path in self.shared.glob(MEMORY + '*'):
            path.unlink()
        (self.shared / MEMORY).write_bytes(b'esto no es una base de datos')
        attempt = Attempt(memory, Known(ANA, FIRST))
        attempt.step(SCREEN, act('fill', 'Folio del ticket', FIRST.ticket_number), describe(FIELDS, 1), 'normal', 'ia')
        attempt.step(SCREEN, button('Buscar ticket'), None, 'normal', 'ia')
        self.assertEqual(attempt.commit(), 1)
        self.assertFalse(attempt.blocked)
        self.assertEqual(self.recalled(self.ana, ANA, FIRST)[0], [FOLIO, SEARCH])
        self.assertEqual(self.recalled(self.beto, BETO, SECOND, family=True), (None, True))
        self.assertFalse(self.failed(self.ana, ANA, FIRST))


class EngineTests(unittest.IsolatedAsyncioTestCase):
    async def test_engine_of_every_account_keeps_its_own_memory_and_shares_one(self):
        folder = tempfile.TemporaryDirectory()
        self.addCleanup(folder.cleanup)
        # Threads of the instances may outlive their tasks; the folder goes only after them (see test_instancias).
        self.addAsyncCleanup(asyncio.get_running_loop().shutdown_default_executor)
        root = Path(folder.name)
        use_master_key(self)
        self.addCleanup(compartido.reset)
        gate = make_entrance(root)
        self.addCleanup(gate._hashing.shutdown)
        self.addAsyncCleanup(gate.instances.close)
        shared = (root / 'compartido').resolve()
        memories = []
        for owner in ('a' * 16, 'b' * 16):
            instance = await gate.instances.enter(owner)
            gate.instances.leave(instance)
            memories.append(instance.runner.memory)
            self.assertEqual(instance.runner.memory.path, instance.root.resolve() / MEMORY)
            self.assertEqual(instance.runner.memory.commons.path, shared / MEMORY)
        self.assertIsNot(memories[0].commons, memories[1].commons)
        self.assertEqual(memories[0].commons.store, memories[1].commons.store)
        # The shared folder is no account: it holds no ticket, no profile and no secret.
        self.assertEqual({path.name.split('-')[0] for path in shared.iterdir()}, {MEMORY})
        self.assertEqual(tables(shared / MEMORY), {'screens', 'store'})


class LocalModeTests(unittest.TestCase):
    """A single person: the memory stays in the data folder, as it was, and nothing else appears."""

    def setUp(self):
        self.base = Path(self.enterContext(tempfile.TemporaryDirectory()))
        self.addCleanup(compartido.reset)
        # An installation with accounts somewhere else in the same process changes nothing here.
        compartido.enable(self.base / 'otra' / 'usuarios', self.base / 'otra' / 'compartido')
        self.root = self.base / 'datos'
        self.root.mkdir()

    def test_memory_stays_where_it_was(self):
        memory = PortalMemory(self.root)
        self.assertIsNone(memory.commons)
        attempt = Attempt(memory, Known(ANA, FIRST))
        attempt.step(SCREEN, act('fill', 'Folio del ticket', FIRST.ticket_number), describe(FIELDS, 1), 'normal', 'ia')
        attempt.step(SCREEN, act('select', 'Estado', 'Jalisco'), describe(FIELDS, 3), 'normal', 'ia')
        attempt.step(SCREEN, button('Buscar ticket'), None, 'normal', 'ia')
        exported = attempt.export()
        self.assertEqual(exported, {'variant': 'F', 'screens': [{'key': list(SCREEN), 'needs_ai': False, 'replayed': False,
                                                               'steps': exported['screens'][0]['steps']}]})
        self.assertEqual(attempt.commit(), 1)
        again = Attempt(memory, Known(ANA, SECOND))
        steps, family = again.recall(SCREEN, True)
        self.assertEqual([step['value'] for step in steps], ['$ticket.ticket_number', '=Jalisco', None])
        again.replay_started(SCREEN, steps, family)
        self.assertFalse(again.replay_failed(SCREEN))
        self.assertEqual({path.name.split('-')[0] for path in self.root.iterdir()}, {MEMORY})
        self.assertEqual({path.name for path in self.base.iterdir()}, {'datos', 'otra'})
        self.assertEqual(list((self.base / 'otra' / 'compartido').iterdir()), [])
        # The file is the one of always: no new table and no new column.
        self.assertEqual(tables(self.root / MEMORY), {'screens', 'pending'})
        self.assertEqual(set(rows(self.root / MEMORY, 'screens')[0]),
                         {'origin', 'path', 'signature', 'variant', 'steps', 'needs_ai', 'successes', 'failures', 'streak',
                          'active', 'updated'})


class ConcurrencyTests(Installation):
    """Several accounts write from their own threads at the same time: nothing is lost or broken."""
    accounts, rounds = 6, 12

    def test_accounts_learning_the_same_and_different_screens_at_once(self):
        people = [(self.account(), person(chr(ord('G') + number), f'PERSONA NUMERO{chr(ord("A") + number)} PRUEBA', '07000'))
                  for number in range(self.accounts)]
        memories = [PortalMemory(root) for root, _ in people]

        def work(number):
            (root, profile), memory = people[number], memories[number]
            for turn in range(self.rounds):
                receipt = ticket(f'TKT-{number}{turn:04d}-X')
                for screen in (SCREEN, (ORIGIN, f'/cuenta{chr(ord("a") + number)}', SIGNATURE)):
                    attempt = Attempt(memory, Known(profile, receipt))
                    attempt.step(screen, act('fill', 'Folio del ticket', receipt.ticket_number), describe(FIELDS, 1), 'normal', 'ia')
                    attempt.step(screen, button('Buscar ticket'), None, 'normal', 'ia')
                    attempt.commit()
                    if attempt.blocked:
                        raise AssertionError('la memoria falló')
                    if Attempt(memory, Known(profile, receipt)).recall(SCREEN, False)[0] != [FOLIO, SEARCH]:
                        raise AssertionError('lectura incompleta')

        self.assertEqual(self.run_together(work, self.accounts), [])
        shared = {row['path']: row for row in rows(self.shared / MEMORY, 'screens')}
        self.assertEqual(len(shared), self.accounts + 1)
        self.assertEqual((shared['/buscar']['successes'], shared['/buscar']['accounts']),
                         (self.accounts * self.rounds, self.accounts))
        for number in range(self.accounts):
            row = shared[f'/cuenta{chr(ord("a") + number)}']
            self.assertEqual((row['successes'], row['accounts'], row['steps']), (self.rounds, 1, shared['/buscar']['steps']))
            self.assertEqual(memories[number].screen(SCREEN, 'F')['successes'], self.rounds)
        self.healthy(MEMORY)


if __name__ == '__main__':
    unittest.main()
