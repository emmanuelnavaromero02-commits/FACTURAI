import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import Mock

from facturador.auth import read_json, write_private
from facturador.portal_accounts import check_target, confirm_password_change, credential_file, site_password


class CredentialTests(unittest.TestCase):
    def test_password_survives_retries_and_is_scoped_to_merchant_and_email(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            path = credential_file(root, 'https://portal.example.com', 'one@example.com')
            with self.assertRaises(ValueError):
                site_password(path, create=False)
            password = site_password(path, create=True)
            self.assertEqual(site_password(path, create=False), password)
            self.assertEqual(site_password(path, create=True), password)
            self.assertNotIn(password, path.read_text())
            self.assertNotIn('one@example.com', json.dumps(list(p.name for p in path.parent.iterdir())))
            for origin, email in [('https://otro.example.com', 'one@example.com'),
                                  ('https://portal.example.com', 'two@example.com')]:
                self.assertNotEqual(site_password(credential_file(root, origin, email), True), password)

    def test_verified_origin_is_bound_to_live_lease_and_exact_page(self):
        view = Mock()
        view.page.url = 'https://portal.example.com/alta'
        check_target(view, 'channel-1', 'https://portal.example.com')
        view.require_control.assert_called_once_with('channel-1')
        for target in ('https://otro.example.com', 'http://portal.example.com', 'https://127.0.0.1',
                       'https://portal.example.com/alta'):
            with self.assertRaises(ValueError):
                check_target(view, 'channel-1', target)

    def test_rotation_preserves_old_secret_until_acceptance_and_reuses_proposal(self):
        with tempfile.TemporaryDirectory() as directory:
            path = credential_file(Path(directory), 'https://portal.example.com', 'one@example.com')
            original = site_password(path, True)
            old_encrypted = read_json(path)['credential']
            proposal = site_password(path, True, rotation=True)
            self.assertNotEqual(proposal, original)
            self.assertNotIn(proposal, path.read_text())
            self.assertEqual(site_password(path, False), original)
            self.assertEqual(site_password(path, True, rotation=True), proposal)
            with self.assertRaises(ValueError):
                confirm_password_change(path)
            write_private(path, {**read_json(path), 'pending_filled': True})
            confirm_password_change(path)
            self.assertEqual(site_password(path, False), proposal)
            self.assertEqual(read_json(path)['previous_credential'], old_encrypted)
            self.assertNotIn('pending_credential', read_json(path))

    def test_first_rotation_after_temporary_survives_without_an_existing_password(self):
        with tempfile.TemporaryDirectory() as directory:
            path = credential_file(Path(directory), 'https://portal.example.com', 'one@example.com')
            proposal = site_password(path, True, rotation=True)
            self.assertEqual(site_password(Path(str(path)), True, rotation=True), proposal)
            with self.assertRaises(ValueError):
                site_password(path, False)
            write_private(path, {**read_json(path), 'pending_filled': True})
            confirm_password_change(path)
            self.assertEqual(site_password(path, False), proposal)
