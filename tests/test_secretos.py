"""Secrets at rest: same behaviour without a master key, sealed with one, and never destroyed by a wrong one."""
import json
import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import Mock, patch

from acceso_dobles import MASTER, OTHER_MASTER, key_text
from facturador import buzon, gmail, secretos
from facturador.auth import ChatGPTPlan, read_json, write_private
from facturador.portal_accounts import credential_file, site_password

SEALED = 'No se pueden abrir los secretos guardados'
SETTINGS = {'imap_host': 'imap.example.org', 'imap_port': 993, 'user': 'buzon@example.org',
            'address': 'buzon@example.org', 'smtp_host': '', 'smtp_port': 465, 'authority': ''}
MAIL_PASSWORD = 'clave-app-ficticia'
SESSION = {'client_id': 'oaiapp_prueba', 'access_token': 'acceso-ficticio', 'refresh_token': 'renovacion-ficticia',
           'scopes': ['resource.invoke', 'chatgpt.tokens.use.direct'], 'expires_at': 9e12, 'email': 'prueba@example.com'}
GMAIL_TOKEN = {'token': 'gmail-ficticio', 'refresh_token': 'gmail-renovacion', 'scope': gmail.SCOPE + ' ' + gmail.SEND_SCOPE,
               'expires_at': 9e12}
GMAIL_CLIENT = {'installed': {'client_id': 'prueba.apps.googleusercontent.com', 'client_secret': 'secreto-ficticio'}}


def snapshot(root: Path) -> dict:
    return {str(path.relative_to(root)): path.read_bytes() for path in sorted(root.rglob('*')) if path.is_file()}


class SecretsCase(unittest.TestCase):
    def setUp(self):
        folder = tempfile.TemporaryDirectory()
        self.addCleanup(folder.cleanup)
        self.root = Path(folder.name) / 'datos'
        self.root.mkdir()
        self.addCleanup(secretos.reset)
        secretos.configure(None)

    def plan(self):
        plan = ChatGPTPlan(self.root, client=Mock())
        self.addCleanup(plan.close)
        return plan


class WithoutMasterKey(SecretsCase):
    def test_files_stay_exactly_as_before(self):
        path = self.root / 'gmail_token.json'
        secretos.write_json(path, GMAIL_TOKEN, purpose=secretos.GMAIL_TOKEN)
        plain = self.root / 'otro.json'
        write_private(plain, GMAIL_TOKEN)
        self.assertEqual(path.read_bytes(), plain.read_bytes())
        self.assertEqual(secretos.read_json(path, {}, purpose=secretos.GMAIL_TOKEN), GMAIL_TOKEN)
        self.assertEqual(secretos.read_json(self.root / 'falta.json', {'x': 1}, purpose=secretos.GMAIL_TOKEN), {'x': 1})
        self.assertEqual(path.read_bytes(), plain.read_bytes())  # Reading changes nothing.
        secretos.remove(path, purpose=secretos.GMAIL_TOKEN)
        self.assertFalse(path.exists())
        self.assertFalse(secretos.active())

    def test_modules_keep_their_plain_files(self):
        buzon.save_account(self.root, SETTINGS, MAIL_PASSWORD)
        self.assertIn('key', read_json(self.root / 'auth' / 'correo' / 'vault-key.json'))
        record = credential_file(self.root, 'https://portal.example.com', 'uno@example.com')
        password = site_password(record, create=True)
        self.assertIn('key', read_json(record.parent / 'vault-key.json'))
        self.assertEqual(site_password(record, create=False), password)
        write_private(self.root / 'auth' / 'chatgpt.json', SESSION)
        self.assertEqual(self.plan().status()['status'], 'ready')
        self.assertEqual(read_json(self.root / 'auth' / 'chatgpt.json'), SESSION)


class Sealing(SecretsCase):
    def setUp(self):
        super().setUp()
        secretos.configure(MASTER)

    def assert_sealed(self, path: Path, *secrets_inside):
        stored = json.loads(path.read_text(encoding='utf-8'))
        self.assertEqual(stored[secretos.MARK], 1)
        self.assertEqual(set(stored), {secretos.MARK, 'uso', 'nonce', 'datos'})
        for value in secrets_inside:
            self.assertNotIn(value.encode(), path.read_bytes())

    def test_plain_files_are_sealed_the_first_time_they_are_read(self):
        chatgpt = self.root / 'auth' / 'chatgpt.json'
        write_private(chatgpt, SESSION)
        write_private(self.root / 'gmail_token.json', GMAIL_TOKEN)
        write_private(self.root / 'google_client.json', GMAIL_CLIENT)
        plan = self.plan()
        self.assertEqual(plan.status()['status'], 'ready')
        self.assert_sealed(chatgpt, 'acceso-ficticio', 'renovacion-ficticia')
        self.assertEqual(plan._credentials()[0], 'acceso-ficticio')
        self.assertTrue(gmail.can_send(self.root))
        self.assert_sealed(self.root / 'gmail_token.json', 'gmail-ficticio', 'gmail-renovacion')
        self.assertEqual(gmail.access_token(self.root), 'gmail-ficticio')
        connection = gmail.GmailConnection(self.root)
        self.assertEqual(connection.status(), 'connected')
        connection.configure(json.dumps(GMAIL_CLIENT).encode())  # Same client: read, compared and left in place.
        self.assert_sealed(self.root / 'google_client.json', 'secreto-ficticio')
        # A second start with the same key reads everything back.
        secretos.configure(MASTER)
        self.assertEqual(secretos.read_json(chatgpt, {}, purpose=secretos.CHATGPT_SESSION), SESSION)
        self.assertEqual(secretos.read_json(self.root / 'google_client.json', purpose=secretos.GMAIL_CLIENT), GMAIL_CLIENT)

    def test_vault_keys_written_before_the_master_key_keep_opening_their_passwords(self):
        secretos.configure(None)
        buzon.save_account(self.root, SETTINGS, MAIL_PASSWORD)
        record = credential_file(self.root, 'https://portal.example.com', 'uno@example.com')
        password = site_password(record, create=True)
        mail_key = read_json(self.root / 'auth' / 'correo' / 'vault-key.json')['key']
        portal_key = read_json(record.parent / 'vault-key.json')['key']
        secretos.configure(MASTER)
        self.assertEqual(buzon.account_password(self.root), MAIL_PASSWORD)
        self.assertEqual(site_password(record, create=False), password)
        self.assert_sealed(self.root / 'auth' / 'correo' / 'vault-key.json', mail_key)
        self.assert_sealed(record.parent / 'vault-key.json', portal_key)
        for path in self.root.rglob('*'):
            if path.is_file():
                self.assertNotIn(mail_key.encode(), path.read_bytes())
                self.assertNotIn(portal_key.encode(), path.read_bytes())

    def test_new_secrets_are_born_sealed(self):
        buzon.save_account(self.root, SETTINGS, MAIL_PASSWORD)
        record = credential_file(self.root, 'https://portal.example.com', 'uno@example.com')
        password = site_password(record, create=True)
        self.assert_sealed(self.root / 'auth' / 'correo' / 'vault-key.json')
        self.assert_sealed(record.parent / 'vault-key.json')
        self.assertEqual(buzon.account_password(self.root), MAIL_PASSWORD)
        self.assertEqual(site_password(record, create=False), password)
        plan = self.plan()
        plan._save_session(SESSION)
        self.assert_sealed(plan.session_path, 'acceso-ficticio')
        plan.clear_limit()
        self.assertEqual(plan.status()['status'], 'ready')
        buzon.remove_account(self.root)
        self.assertFalse(buzon.mailbox_connected(self.root))

    def test_every_owner_and_every_purpose_has_its_own_key(self):
        first, second = self.root / 'usuarios' / ('a' * 16), self.root / 'usuarios' / ('b' * 16)
        secretos.register(first, 'a' * 16)
        secretos.register(second, 'b' * 16)
        keys = {secretos.derive(purpose, owner) for purpose in (secretos.GMAIL_TOKEN, secretos.MAIL_VAULT, secretos.PASSWORDS)
                for owner in ('a' * 16, 'b' * 16, secretos.LOCAL)}
        self.assertEqual(len(keys), 9)
        self.assertNotIn(MASTER, keys)
        for folder in (first, second):
            secretos.write_json(folder / 'gmail_token.json', GMAIL_TOKEN, purpose=secretos.GMAIL_TOKEN)
        self.assertEqual(secretos.read_json(first / 'gmail_token.json', purpose=secretos.GMAIL_TOKEN), GMAIL_TOKEN)
        # The sealed file of one customer placed in another customer's folder does not open there.
        stolen = (first / 'gmail_token.json').read_bytes()
        (second / 'gmail_token.json').write_bytes(stolen)
        with self.assertRaisesRegex(secretos.SealedSecrets, SEALED):
            secretos.read_json(second / 'gmail_token.json', purpose=secretos.GMAIL_TOKEN)
        # Nor does a file of one kind open as another kind.
        (first / 'google_client.json').write_bytes(stolen)
        with self.assertRaisesRegex(secretos.SealedSecrets, SEALED):
            secretos.read_json(first / 'google_client.json', purpose=secretos.GMAIL_CLIENT)
        # Outside every registered folder the owner is the local installation.
        secretos.write_json(self.root / 'gmail_token.json', GMAIL_TOKEN, purpose=secretos.GMAIL_TOKEN)
        (first / 'gmail_token.json').write_bytes((self.root / 'gmail_token.json').read_bytes())
        with self.assertRaises(secretos.SealedSecrets):
            secretos.read_json(first / 'gmail_token.json', purpose=secretos.GMAIL_TOKEN)

    def test_tampered_envelope_is_refused(self):
        path = self.root / 'gmail_token.json'
        secretos.write_json(path, GMAIL_TOKEN, purpose=secretos.GMAIL_TOKEN)
        stored = json.loads(path.read_text())
        import base64
        data = bytearray(base64.b64decode(stored['datos']))
        data[5] ^= 1
        path.write_text(json.dumps({**stored, 'datos': base64.b64encode(bytes(data)).decode()}))
        with self.assertRaisesRegex(secretos.SealedSecrets, SEALED):
            secretos.read_json(path, purpose=secretos.GMAIL_TOKEN)


class WrongOrMissingKey(SecretsCase):
    """Everything is sealed with one key; then the program starts with another key, or with none."""

    def setUp(self):
        super().setUp()
        secretos.configure(MASTER)
        buzon.save_account(self.root, SETTINGS, MAIL_PASSWORD)
        self.record = credential_file(self.root, 'https://portal.example.com', 'uno@example.com')
        self.portal_password = site_password(self.record, create=True)
        secretos.write_json(self.root / 'auth' / 'chatgpt.json', SESSION, purpose=secretos.CHATGPT_SESSION)
        secretos.write_json(self.root / 'gmail_token.json', GMAIL_TOKEN, purpose=secretos.GMAIL_TOKEN)
        secretos.write_json(self.root / 'google_client.json', GMAIL_CLIENT, purpose=secretos.GMAIL_CLIENT)
        secretos.verify(self.root)
        self.before = snapshot(self.root)

    def attempts(self):
        plan = self.plan()
        connection = gmail.GmailConnection(self.root)
        other = credential_file(self.root, 'https://otro.example.com', 'uno@example.com')
        return [lambda: buzon.account_password(self.root), lambda: buzon.save_account(self.root, SETTINGS, 'otra-clave-ficticia'),
                lambda: buzon.remove_account(self.root), lambda: site_password(self.record, create=False),
                lambda: site_password(self.record, create=True), lambda: site_password(other, create=True),
                plan.status, plan._credentials, plan.disconnect, plan.clear_limit, lambda: plan._save_session({}),
                lambda: plan.start(), lambda: gmail.access_token(self.root), lambda: gmail.can_send(self.root),
                connection.disconnect, lambda: connection.configure(json.dumps(GMAIL_CLIENT).encode()),
                connection.start, lambda: secretos.verify(self.root)]

    def check_nothing_is_destroyed(self):
        for number, attempt in enumerate(self.attempts()):
            with self.subTest(attempt=number):
                with self.assertRaises(ValueError) as caught:
                    attempt()
                self.assertIn(SEALED, str(caught.exception))
                self.assertEqual(snapshot(self.root), self.before)

    def test_another_master_key_gets_a_clear_error_and_changes_nothing(self):
        secretos.configure(OTHER_MASTER)
        self.check_nothing_is_destroyed()
        # With the right key again everything is still there.
        secretos.configure(MASTER)
        self.assertEqual(buzon.account_password(self.root), MAIL_PASSWORD)
        self.assertEqual(site_password(self.record, create=False), self.portal_password)
        self.assertEqual(gmail.access_token(self.root), 'gmail-ficticio')
        self.assertEqual(self.plan().status()['status'], 'ready')

    def test_no_master_key_gets_a_clear_error_and_changes_nothing(self):
        secretos.configure(None)
        self.check_nothing_is_destroyed()


class MasterKeyConfiguration(SecretsCase):
    def test_key_must_be_32_random_bytes(self):
        self.assertEqual(secretos.parse_key(key_text()), MASTER)
        self.assertEqual(secretos.parse_key(MASTER.hex()), MASTER)
        self.assertEqual(secretos.parse_key('  ' + key_text() + '\n'), MASTER)
        for bad in ('', 'corta', 'una frase escrita a mano que no es una llave', key_text()[:20], 'A' * 43, '00' * 32,
                    '0123456789abcdef' * 4):
            with self.subTest(bad=bad), self.assertRaises(secretos.MasterKeyError):
                secretos.parse_key(bad)

    def test_environment_gives_the_key_inline_or_from_a_file_outside_the_data_folder(self):
        self.assertFalse(secretos.load_from_env(self.root, {}))
        self.assertTrue(secretos.load_from_env(self.root, {secretos.KEY_VARIABLE: key_text()}))
        self.assertEqual(secretos.derive('prueba'), secretos.derive('prueba'))
        outside = self.root.parent / 'llave-maestra'
        outside.write_text(key_text() + '\n')
        self.assertTrue(secretos.load_from_env(self.root, {secretos.FILE_VARIABLE: str(outside)}))
        inside = self.root / 'auth' / 'llave-maestra'
        inside.parent.mkdir()
        inside.write_text(key_text())
        for env in ({secretos.FILE_VARIABLE: str(inside)}, {secretos.FILE_VARIABLE: str(self.root)},
                    {secretos.FILE_VARIABLE: str(outside), secretos.KEY_VARIABLE: key_text()},
                    {secretos.FILE_VARIABLE: str(self.root.parent / 'no-existe')}, {secretos.KEY_VARIABLE: 'no-es-una-llave'}):
            with self.subTest(env=sorted(env)), self.assertRaises(secretos.MasterKeyError):
                secretos.load_from_env(self.root, env)

    def test_first_use_reads_the_environment_by_itself(self):
        secretos.reset()
        with patch.dict(os.environ, {secretos.KEY_VARIABLE: key_text()}):
            self.assertTrue(secretos.active())
            secretos.write_json(self.root / 'gmail_token.json', GMAIL_TOKEN, purpose=secretos.GMAIL_TOKEN)
        self.assertIn(secretos.MARK, read_json(self.root / 'gmail_token.json'))

    def test_key_file_is_created_once_private_and_outside_the_data_folder(self):
        target = self.root.parent / 'llaves' / 'maestra.key'
        made = secretos.create_key_file(target, self.root)
        self.assertEqual(len(secretos.parse_key(made.read_text())), 32)
        if os.name == 'posix':
            self.assertEqual(made.stat().st_mode & 0o777, 0o600)
        content = made.read_bytes()
        with self.assertRaises(secretos.MasterKeyError):
            secretos.create_key_file(target, self.root)
        self.assertEqual(made.read_bytes(), content)
        with self.assertRaises(secretos.MasterKeyError):
            secretos.create_key_file(self.root / 'maestra.key', self.root)
        self.assertFalse((self.root / 'maestra.key').exists())

    def test_check_file_tells_a_different_key_before_anything_else_runs(self):
        secretos.configure(MASTER)
        secretos.verify(self.root)
        secretos.verify(self.root)
        check = (self.root / secretos.CHECK_FILE).read_bytes()
        for key in (OTHER_MASTER, None):
            secretos.configure(key)
            with self.subTest(key=bool(key)), self.assertRaisesRegex(secretos.SealedSecrets, SEALED):
                secretos.verify(self.root)
            self.assertEqual((self.root / secretos.CHECK_FILE).read_bytes(), check)
        # A folder that never had a master key starts without one, as always.
        fresh = self.root.parent / 'nuevo'
        fresh.mkdir()
        secretos.configure(None)
        secretos.verify(fresh)
        self.assertEqual(list(fresh.iterdir()), [])


if __name__ == '__main__':
    unittest.main()
