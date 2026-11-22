import asyncio
import os
import socket
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import AsyncMock, Mock

from facturador.profile_lock import close_context, launch_chrome, lock_profile, release_stale_links


@unittest.skipUnless(sys.platform.startswith('linux'), 'Linux Chrome singleton sockets')
class ChromeLockTests(unittest.TestCase):
    def test_stale_managed_links_release_only_after_exclusive_lease_and_dead_socket(self):
        with tempfile.TemporaryDirectory() as tmp:
            profile = Path(tmp)
            shared = profile/'.runtime'
            path = shared/'com.google.Chrome.test'/'SingletonSocket'
            path.parent.mkdir(parents=True)
            (profile/'SingletonSocket').symlink_to(path)
            (profile/'SingletonCookie').symlink_to('fake-cookie')
            (profile/'SingletonLock').symlink_to('retired-host-123')
            (profile/'Preferences').write_text('saved')
            with lock_profile(profile) as lease:
                with self.assertRaisesRegex(ValueError, 'Otra instancia'):
                    lock_profile(profile)
                self.assertFalse(release_stale_links(profile, lease, shared))
                lease.write('retired-host-123')
                lease.flush()
                with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as listener:
                    listener.bind(str(path))
                    listener.listen()
                    self.assertFalse(release_stale_links(profile, lease, shared))
                self.assertTrue(release_stale_links(profile, lease, shared))
            self.assertEqual((profile/'Preferences').read_text(), 'saved')

    def test_legacy_and_current_owners_are_never_guessed(self):
        with tempfile.TemporaryDirectory() as tmp:
            profile = Path(tmp)
            shared = profile/'.runtime'
            (profile/'SingletonSocket').symlink_to(profile/'previous-tmp'/'SingletonSocket')
            lock = profile/'SingletonLock'
            lock.symlink_to('retired-host-123')
            with lock_profile(profile) as lease:
                lease.write('retired-host-123')
                lease.flush()
                self.assertFalse(release_stale_links(profile, lease, shared))
                self.assertFalse(release_stale_links(profile, lease, shared, 'wrong-host'))
                self.assertTrue(release_stale_links(profile, lease, shared, 'retired-host'))
                lock.symlink_to(socket.gethostname() + '-32')
                (profile/'SingletonSocket').symlink_to(profile/'previous-tmp'/'SingletonSocket')
                self.assertFalse(release_stale_links(profile, lease, shared, socket.gethostname()))


@unittest.skipUnless(sys.platform.startswith('linux'), 'Linux Chrome singleton sockets')
class ChromeCloseTests(unittest.IsolatedAsyncioTestCase):
    async def test_missing_close_event_waits_for_the_original_local_process_to_exit(self):
        with tempfile.TemporaryDirectory() as tmp:
            profile = Path(tmp)
            child = await asyncio.create_subprocess_exec(sys.executable, '-c', 'import time; time.sleep(30)')
            context = Mock(close=AsyncMock(), is_closed=Mock(return_value=True))
            async def launch(directory, **options):
                (directory/'SingletonLock').symlink_to(f'{socket.gethostname()}-{child.pid}')
                (directory/'SingletonSocket').symlink_to(directory/'retired-socket')
                return context
            try:
                await launch_chrome(launch, profile)
                closing = asyncio.create_task(close_context(context))
                await asyncio.sleep(.3)
                self.assertFalse(closing.done())
                with self.assertRaisesRegex(ValueError, 'Otra instancia'):
                    lock_profile(profile)
                child.terminate()
                await child.wait()
                await asyncio.wait_for(closing, timeout=2)
                with lock_profile(profile):
                    pass
                context.close.assert_not_awaited()
            finally:
                if child.returncode is None:
                    child.kill()
                    await child.wait()
