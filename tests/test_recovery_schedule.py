import tempfile
import time
import unittest
from pathlib import Path
from facturador.db import Store
from test_core import PROFILE


class RecoveryScheduleTests(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.root = Path(tmp.name)
        self.store = Store(self.root)
        self.key = self.store.add('foto', 'foto-hash')[0]['id']
        with self.store.connect() as db:
            db.execute("UPDATE jobs SET status='uncertain',billing_profile=?,human_exposed=1 WHERE id=?",
                       (PROFILE.model_dump_json(),self.key))

    def test_four_checks_with_backoff_survive_restart_and_never_request_issuance(self):
        now = int(time.time())
        self.assertEqual(self.store.next_recovery(now), self.key)
        restarted = Store(self.root)
        self.assertIsNone(restarted.next_recovery(now+89))
        for dt in (90, 690, 4290):
            self.assertEqual(restarted.next_recovery(now+dt), self.key)
        self.assertIsNone(restarted.next_recovery(now+99999))
        self.assertEqual(restarted.get(self.key)['issue_attempted'],0)

    def test_user_stop_and_confirmed_uuids_are_excluded(self):
        now = int(time.time())
        self.store.pause_automatic_recovery(self.key)
        self.assertIsNone(self.store.next_recovery(now))
        self.store.pause_automatic_recovery(self.key, False)
        self.store.update(self.key, status='issued_waiting_files', uuid='12345678-1234-1234-1234-123456789ABC')
        self.assertIsNone(self.store.next_recovery(now))

    def test_service_wait_survives_restart_and_preserves_a_user_pause(self):
        now = int(time.time())
        self.store.defer_recovery_until(self.key, now + 4800)
        self.assertIsNone(Store(self.root).next_recovery(now + 4799))
        self.assertEqual(self.store.next_recovery(now + 4800), self.key)
        self.store.pause_automatic_recovery(self.key)
        self.store.defer_recovery_until(self.key, now + 6000)
        self.assertIsNone(Store(self.root).next_recovery(now + 6001))
