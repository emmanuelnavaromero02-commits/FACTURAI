"""Accounts, sessions, the brake on guesses and one-use invitations."""
import hashlib
import logging
import tempfile
import threading
import unittest
from pathlib import Path

from acceso_dobles import FAST, PASSWORD, FakeClock
from facturador import usuarios
from facturador.usuarios import (ALLOWANCE, FIRST_WAIT, IDLE_SECONDS, LIFETIME_SECONDS, LONGEST_WAIT, AccessError,
                                 Accounts, BadCredentials, InvalidInvitation, Throttled)

ADDRESS = '203.0.113.7'


class AccountsCase(unittest.TestCase):
    def setUp(self):
        folder = tempfile.TemporaryDirectory()
        self.addCleanup(folder.cleanup)
        self.root = Path(folder.name)
        self.clock = FakeClock()
        self.accounts = Accounts(self.root, pepper=b'p' * 32, clock=self.clock, cost=FAST)
        self.hashes = []
        original = self.accounts._hash
        self.accounts._hash = lambda password, salt, cost: self.hashes.append(dict(cost)) or original(password, salt, cost)
        self.ana = self.accounts.create_user('Ana', PASSWORD)
        self.hashes.clear()

    def stored(self) -> bytes:
        with self.accounts.connect() as db:
            db.execute('PRAGMA wal_checkpoint(TRUNCATE)')
        return b''.join(path.read_bytes() for path in self.root.iterdir() if path.is_file())


class PasswordTests(AccountsCase):
    def test_default_cost_is_strong_and_verifies(self):
        self.assertEqual(usuarios.SCRYPT, {'n': 2 ** 17, 'r': 8, 'p': 1})
        self.assertGreaterEqual(usuarios.MIN_PASSWORD, 12)
        real = Accounts(self.root / 'real', pepper=b'p' * 32)
        real.create_user('real', PASSWORD)
        self.assertEqual(real.authenticate('real', PASSWORD, ADDRESS)['nombre'], 'real')
        with real.connect() as db:
            self.assertEqual(db.execute('SELECT costo FROM usuarios').fetchone()[0], '131072:8:1')

    def test_password_is_salted_per_account_and_never_stored(self):
        self.accounts.create_user('beto', PASSWORD)
        with self.accounts.connect() as db:
            rows = db.execute('SELECT sal,clave FROM usuarios ORDER BY nombre').fetchall()
        self.assertNotEqual(rows[0]['sal'], rows[1]['sal'])
        self.assertNotEqual(rows[0]['clave'], rows[1]['clave'])
        self.assertEqual({len(row['sal']) for row in rows}, {16})
        self.assertNotIn(PASSWORD.encode(), self.stored())
        # The pepper is not in the database either: another pepper cannot verify the same password.
        other = Accounts(self.root, pepper=b'q' * 32, clock=self.clock, cost=FAST)
        with self.assertRaises(BadCredentials):
            other.authenticate('ana', PASSWORD, ADDRESS)

    def test_rules_for_names_and_passwords(self):
        for name in ('ab', 'con espacio', 'ñandú', 'a' * 65, '.punto', '', None):
            with self.subTest(name=name), self.assertRaises(AccessError):
                self.accounts.create_user(name, PASSWORD)
        for password in ('corta', 'once-caract', 'x' * 257, None, 'usuario.nuevo'):
            with self.subTest(password=password), self.assertRaises(AccessError):
                self.accounts.create_user('usuario.nuevo', password)
        self.assertEqual(self.accounts.create_user('  Persona@Example.com ', 'doce-caracte')['nombre'], 'persona@example.com')
        with self.assertRaises(AccessError):
            self.accounts.create_user('ANA', 'otra frase distinta 9')

    def test_unknown_name_and_wrong_password_look_and_cost_the_same(self):
        outcomes = []
        for name, password in (('ana', 'equivocada-123456'), ('nadie', PASSWORD), ('no vale!', PASSWORD), (None, None)):
            self.hashes.clear()
            with self.assertRaises(BadCredentials) as caught:
                self.accounts.authenticate(name, password, ADDRESS)
            outcomes.append((str(caught.exception), caught.exception.status, self.hashes))
            self.assertEqual(self.hashes, [FAST])  # Exactly one hash, with the same cost, in every case.
        self.assertEqual(len({(text, status) for text, status, _ in outcomes}), 1)
        self.hashes.clear()
        self.assertEqual(self.accounts.authenticate(' ANA ', PASSWORD, ADDRESS)['id'], self.ana['id'])
        self.assertEqual(self.hashes, [FAST])

    def test_turned_off_account_is_refused_like_an_unknown_one(self):
        admin = self.accounts.create_user('jefa', PASSWORD, role='admin')
        self.accounts.set_active(self.ana['id'], False, by=admin['id'])
        with self.assertRaises(BadCredentials) as caught:
            self.accounts.authenticate('ana', PASSWORD, ADDRESS)
        self.assertEqual(str(caught.exception), usuarios.WRONG)

    def test_older_cost_is_brought_up_to_date_when_the_person_enters(self):
        stronger = Accounts(self.root, pepper=b'p' * 32, clock=self.clock, cost={'n': 2 ** 11, 'r': 8, 'p': 1})
        self.assertEqual(stronger.authenticate('ana', PASSWORD, ADDRESS)['id'], self.ana['id'])
        with stronger.connect() as db:
            self.assertEqual(db.execute('SELECT costo FROM usuarios').fetchone()[0], '2048:8:1')
        self.assertEqual(stronger.authenticate('ana', PASSWORD, ADDRESS)['id'], self.ana['id'])

    def test_passwords_never_reach_the_log_or_the_error(self):
        records = []
        handler = logging.Handler(logging.DEBUG)
        handler.emit = records.append
        logging.getLogger().addHandler(handler)
        self.addCleanup(logging.getLogger().removeHandler, handler)
        secret = 'secreto-equivocado-98765'
        for attempt in (lambda: self.accounts.authenticate('ana', secret, ADDRESS),
                        lambda: self.accounts.change_password(self.ana['id'], secret, 'nueva-frase-larga-2', ADDRESS),
                        lambda: self.accounts.create_user('ana', secret)):
            with self.assertRaises(AccessError) as caught:
                attempt()
            self.assertNotIn(secret, str(caught.exception))
            self.assertIsNone(caught.exception.__cause__)
        self.assertFalse(any(secret in record.getMessage() for record in records))
        self.assertNotIn(secret.encode(), self.stored())


class BrakeTests(AccountsCase):
    def fail(self, name='ana', address=ADDRESS):
        with self.assertRaises(BadCredentials):
            self.accounts.authenticate(name, 'equivocada-123456', address)

    def test_waits_grow_for_one_account_and_end_when_the_time_passes(self):
        free = ALLOWANCE['usuario'][0]
        for _ in range(free):
            self.fail()
        waits = []
        for _ in range(4):
            self.hashes.clear()
            with self.assertRaises(Throttled) as caught:
                self.accounts.authenticate('ana', PASSWORD, ADDRESS)  # Even the right password waits.
            self.assertEqual(self.hashes, [])
            self.assertEqual(caught.exception.status, 429)
            waits.append(caught.exception.retry_after)
            self.clock.advance(caught.exception.retry_after)
            self.fail()
        self.assertEqual(waits, [FIRST_WAIT, FIRST_WAIT * 2, FIRST_WAIT * 4, FIRST_WAIT * 8])
        # The brake belongs to the name: from any other address it is the same.
        with self.assertRaises(Throttled):
            self.accounts.authenticate('ana', PASSWORD, '198.51.100.9')
        # Another person at the same address is not held back by it.
        self.accounts.create_user('beto', PASSWORD)
        self.assertEqual(self.accounts.authenticate('beto', PASSWORD, ADDRESS)['nombre'], 'beto')

    def test_wait_never_exceeds_the_limit_and_success_forgives(self):
        longest = 0
        for _ in range(60):
            try:
                self.accounts.authenticate('ana', 'equivocada-123456', ADDRESS)
            except Throttled as blocked:
                longest = max(longest, blocked.retry_after)
                self.clock.advance(blocked.retry_after)
            except BadCredentials:
                pass
        self.assertEqual(longest, LONGEST_WAIT)
        with self.assertRaises(AccessError):
            self.accounts.authenticate('ana', 'equivocada-123456', ADDRESS)  # Failed or still waiting: braked now.
        with self.assertRaises(Throttled) as caught:
            self.accounts.authenticate('ana', PASSWORD, ADDRESS)
        self.assertEqual(caught.exception.retry_after, LONGEST_WAIT)
        self.clock.advance(LONGEST_WAIT)
        self.assertEqual(self.accounts.authenticate('ana', PASSWORD, ADDRESS)['id'], self.ana['id'])
        for _ in range(ALLOWANCE['usuario'][0] - 1):
            self.fail()  # The count started again after entering.
        self.assertEqual(self.accounts.authenticate('ana', PASSWORD, ADDRESS)['id'], self.ana['id'])

    def test_unknown_names_are_braked_exactly_like_real_ones(self):
        for name in ('ana', 'nadie'):
            address = '203.0.113.' + str(len(name))
            for _ in range(ALLOWANCE['usuario'][0]):
                self.fail(name, address)
            with self.assertRaises(Throttled) as caught:
                self.accounts.authenticate(name, PASSWORD, address)
            self.assertEqual(caught.exception.retry_after, FIRST_WAIT)

    def test_one_address_trying_many_names_is_braked_too(self):
        free = ALLOWANCE['direccion'][0]
        for number in range(free):
            self.fail(f'persona{number}')
        with self.assertRaises(Throttled):
            self.accounts.authenticate('ana', PASSWORD, ADDRESS)
        self.assertEqual(self.accounts.authenticate('ana', PASSWORD, '198.51.100.9')['id'], self.ana['id'])
        self.assertGreater(self.accounts.blocked(ADDRESS), 0)
        self.assertEqual(self.accounts.blocked('198.51.100.9'), 0)

    def test_old_failures_are_forgotten(self):
        free, forget = ALLOWANCE['usuario']
        for _ in range(free - 1):
            self.fail()
        self.clock.advance(forget * free)
        for _ in range(free - 1):
            self.fail()
        self.assertEqual(self.accounts.authenticate('ana', PASSWORD, ADDRESS)['id'], self.ana['id'])

    def test_guesses_sent_at_the_same_time_cannot_pass_the_brake(self):
        results = []

        def guess(number):
            try:
                self.accounts.authenticate('ana', f'equivocada-{number:06d}', ADDRESS)
            except AccessError as exc:
                results.append(type(exc).__name__)

        threads = [threading.Thread(target=guess, args=(number,)) for number in range(24)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join()
        self.assertEqual(results.count('BadCredentials'), ALLOWANCE['usuario'][0])
        self.assertEqual(results.count('Throttled'), 24 - ALLOWANCE['usuario'][0])

    def test_typed_names_and_addresses_are_not_kept_in_clear(self):
        typed = 'mi-contrasena-en-el-campo-equivocado'
        self.fail(typed, '203.0.113.200')
        stored = self.stored()
        self.assertNotIn(typed.encode(), stored)
        self.assertNotIn(b'203.0.113.200', stored)


class SessionTests(AccountsCase):
    def test_token_has_256_bits_and_only_its_digest_is_stored(self):
        token = self.accounts.open_session(self.ana['id'])
        self.assertRegex(token, r'^[A-Za-z0-9_-]{43}$')
        self.assertNotEqual(token, self.accounts.open_session(self.ana['id']))
        self.assertNotIn(token.encode(), self.stored())
        with self.accounts.connect() as db:
            self.assertIn(hashlib.sha256(token.encode()).digest(), [row[0] for row in db.execute('SELECT huella FROM sesiones')])
        session = self.accounts.session(token)
        self.assertEqual((session['usuario'], session['nombre'], session['rol']), (self.ana['id'], 'ana', 'usuario'))
        for bad in ('', 'x' * 43, token[:-1] + ('A' if token[-1] != 'A' else 'B')):
            self.assertIsNone(self.accounts.session(bad))

    def test_idle_session_ends_after_twelve_hours(self):
        token = self.accounts.open_session(self.ana['id'])
        self.clock.advance(IDLE_SECONDS - 120)
        self.assertIsNotNone(self.accounts.session(token))  # Using it counts as activity.
        self.clock.advance(IDLE_SECONDS - 120)
        self.assertIsNotNone(self.accounts.session(token))
        self.clock.advance(IDLE_SECONDS + 1)
        self.assertIsNone(self.accounts.session(token))
        self.clock.advance(-IDLE_SECONDS)
        self.assertIsNone(self.accounts.session(token))  # It was removed, not just hidden.

    def test_session_ends_after_seven_days_even_when_it_is_used_every_hour(self):
        token = self.accounts.open_session(self.ana['id'])
        for _ in range(LIFETIME_SECONDS // 3600):
            self.clock.advance(3600)
            self.assertIsNotNone(self.accounts.session(token))
        self.clock.advance(3600)
        self.assertIsNone(self.accounts.session(token))

    def test_closing_sessions_on_the_server(self):
        first, second, third = (self.accounts.open_session(self.ana['id']) for _ in range(3))
        self.assertEqual(self.accounts.session_count(self.ana['id']), 3)
        self.accounts.close_session(first)
        self.assertIsNone(self.accounts.session(first))
        self.assertEqual(self.accounts.close_other_sessions(self.ana['id'], second), 1)
        self.assertIsNone(self.accounts.session(third))
        self.assertIsNotNone(self.accounts.session(second))
        self.assertEqual(self.accounts.session_count(self.ana['id']), 1)

    def test_changing_the_password_needs_the_current_one_and_ends_the_other_sessions(self):
        phone, laptop = self.accounts.open_session(self.ana['id']), self.accounts.open_session(self.ana['id'])
        new = 'otra frase larga nueva 2'
        with self.assertRaises(BadCredentials):
            self.accounts.change_password(self.ana['id'], 'no-es-la-actual-1', new, ADDRESS)
        for bad in ('corta', PASSWORD, 'ana'):
            with self.subTest(bad=bad), self.assertRaises(AccessError):
                self.accounts.change_password(self.ana['id'], PASSWORD, bad, ADDRESS)
        self.assertIsNotNone(self.accounts.session(phone))
        fresh = self.accounts.change_password(self.ana['id'], PASSWORD, new, ADDRESS)
        self.assertNotIn(fresh, (phone, laptop))
        self.assertIsNone(self.accounts.session(phone))
        self.assertIsNone(self.accounts.session(laptop))
        self.assertEqual(self.accounts.session(fresh)['usuario'], self.ana['id'])
        with self.assertRaises(BadCredentials):
            self.accounts.authenticate('ana', PASSWORD, ADDRESS)
        self.assertEqual(self.accounts.authenticate('ana', new, ADDRESS)['id'], self.ana['id'])

    def test_guessing_the_current_password_from_a_stolen_session_is_braked(self):
        for _ in range(ALLOWANCE['usuario'][0]):
            with self.assertRaises(BadCredentials):
                self.accounts.change_password(self.ana['id'], 'no-es-la-actual-1', 'otra frase larga nueva 2', ADDRESS)
        with self.assertRaises(Throttled):
            self.accounts.change_password(self.ana['id'], PASSWORD, 'otra frase larga nueva 2', ADDRESS)

    def test_turning_an_account_off_ends_its_sessions_and_keeps_an_administrator(self):
        admin = self.accounts.create_user('jefa', PASSWORD, role='admin')
        token, admin_token = self.accounts.open_session(self.ana['id']), self.accounts.open_session(admin['id'])
        self.assertFalse(self.accounts.set_active(self.ana['id'], False, by=admin['id'])['activo'])
        self.assertIsNone(self.accounts.session(token))
        self.assertEqual(self.accounts.session_count(self.ana['id']), 0)
        self.assertIsNotNone(self.accounts.session(admin_token))
        for target, by in ((admin['id'], admin['id']), (admin['id'], self.ana['id']), ('no-existe', admin['id'])):
            with self.subTest(target=target, by=by), self.assertRaises(AccessError):
                self.accounts.set_active(target, False, by=by)
        self.assertTrue(self.accounts.set_active(self.ana['id'], True, by=admin['id'])['activo'])
        self.assertEqual(self.accounts.authenticate('ana', PASSWORD, ADDRESS)['id'], self.ana['id'])

    def test_upkeep_removes_what_expired(self):
        token = self.accounts.open_session(self.ana['id'])
        self.accounts.note_failure(ADDRESS)
        self.clock.advance(LIFETIME_SECONDS + 1)
        self.accounts.purge()
        with self.accounts.connect() as db:
            self.assertEqual(db.execute('SELECT COUNT(*) FROM sesiones').fetchone()[0], 0)
            self.assertEqual(db.execute('SELECT COUNT(*) FROM intentos').fetchone()[0], 0)
        self.assertIsNone(self.accounts.session(token))


class InvitationTests(AccountsCase):
    def setUp(self):
        super().setUp()
        self.admin = self.accounts.create_user('jefa', PASSWORD, role='admin')

    def test_code_creates_one_account_once(self):
        made = self.accounts.create_invitation(self.admin['id'])
        self.assertRegex(made['codigo'], r'^[A-Za-z0-9_-]{43}$')
        self.assertNotIn(made['codigo'].encode(), self.stored())
        self.assertEqual(self.accounts.invitation(made['codigo'])['nombre'], '')
        self.assertEqual([item['id'] for item in self.accounts.invitations()], [made['id']])
        self.assertNotIn('codigo', self.accounts.invitations()[0])
        account = self.accounts.accept_invitation(made['codigo'], 'Carla', 'frase de carla 123')
        self.assertEqual((account['nombre'], account['rol'], account['activo']), ('carla', 'usuario', True))
        self.assertEqual(self.accounts.authenticate('carla', 'frase de carla 123', ADDRESS)['id'], account['id'])
        for attempt in (lambda: self.accounts.accept_invitation(made['codigo'], 'dora', 'frase de dora 1234'),
                        lambda: self.accounts.invitation(made['codigo'])):
            with self.assertRaises(InvalidInvitation) as caught:
                attempt()
            self.assertEqual(caught.exception.status, 404)
        self.assertEqual(self.accounts.invitations(), [])
        self.assertEqual(len(self.accounts.users()), 3)

    def test_expired_revoked_and_unknown_codes_are_refused(self):
        expired = self.accounts.create_invitation(self.admin['id'], hours=1)
        revoked = self.accounts.create_invitation(self.admin['id'])
        self.assertTrue(self.accounts.revoke_invitation(revoked['id']))
        self.assertFalse(self.accounts.revoke_invitation(revoked['id']))
        self.clock.advance(3600)
        for code in (expired['codigo'], revoked['codigo'], 'x' * 43, '', None, 12):
            with self.subTest(code=code), self.assertRaises(InvalidInvitation):
                self.accounts.accept_invitation(code, 'dora', 'frase de dora 1234')
        self.assertEqual(sorted(user['nombre'] for user in self.accounts.users()), ['ana', 'jefa'])
        for hours in (0, 0.5, 14 * 24 + 1, '72', True, None):
            with self.subTest(hours=hours), self.assertRaises(AccessError):
                self.accounts.create_invitation(self.admin['id'], hours=hours)

    def test_refused_account_leaves_the_invitation_usable(self):
        made = self.accounts.create_invitation(self.admin['id'])
        for name, password in (('ana', 'frase de otra ana 1'), ('dora', 'corta'), ('no vale!', 'frase de dora 1234')):
            with self.subTest(name=name), self.assertRaises(AccessError):
                self.accounts.accept_invitation(made['codigo'], name, password)
        self.assertEqual(self.accounts.accept_invitation(made['codigo'], 'dora', 'frase de dora 1234')['nombre'], 'dora')

    def test_administrator_can_reserve_the_name(self):
        with self.assertRaises(AccessError):
            self.accounts.create_invitation(self.admin['id'], name='ana')
        made = self.accounts.create_invitation(self.admin['id'], name='Elena')
        self.assertEqual(self.accounts.invitation(made['codigo'])['nombre'], 'elena')
        account = self.accounts.accept_invitation(made['codigo'], 'otro-nombre', 'frase de elena 123')
        self.assertEqual(account['nombre'], 'elena')

    def test_two_people_with_the_same_code_at_once_make_one_account(self):
        made = self.accounts.create_invitation(self.admin['id'])
        results = []

        def accept(name):
            try:
                results.append(self.accounts.accept_invitation(made['codigo'], name, 'frase compartida 12')['nombre'])
            except AccessError as exc:
                results.append(type(exc).__name__)

        threads = [threading.Thread(target=accept, args=(f'gemela{number}',)) for number in range(6)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join()
        self.assertEqual(results.count('InvalidInvitation'), 5)
        self.assertEqual(len(self.accounts.users()), 3)


if __name__ == '__main__':
    unittest.main()
