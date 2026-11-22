"""Exercise a concurrent close and reopen of the same real Chrome profile in Linux."""
import asyncio
import sys
import tempfile
from pathlib import Path

from playwright.async_api import async_playwright

from facturador.profile_lock import close_context, launch_chrome


async def smoke():
    with tempfile.TemporaryDirectory() as tmp:
        profile = Path(tmp) / 'perfil'
        async with async_playwright() as pw:
            async def open_profile():
                return await launch_chrome(pw.chromium.launch_persistent_context, profile,
                    executable_path='/usr/bin/google-chrome-stable', headless=False, chromium_sandbox=True)

            context = await open_profile()
            cookie_url = 'https://perfil.example.test'
            await context.add_cookies([dict(name='prueba', value='guardada', url=cookie_url, expires=2000000000)])
            # A second process cannot open this profile while its original browser is running.
            competitor = await asyncio.create_subprocess_exec(sys.executable, '-c',
                'from pathlib import Path\nfrom facturador.profile_lock import lock_profile\n'
                'import sys\ntry: lock_profile(Path(sys.argv[1]))\nexcept ValueError: sys.exit(0)\nsys.exit(1)',
                str(profile))
            assert await competitor.wait() == 0
            page_closed = asyncio.Event()
            context.pages[0].on('close', lambda _: page_closed.set())
            original_close = asyncio.create_task(context.close())
            await asyncio.wait_for(page_closed.wait(), timeout=10)
            await close_context(context)
            await original_close
            reopened = await open_profile()
            try:
                assert any(c['name'] == 'prueba' and c['value'] == 'guardada' for c in await reopened.cookies(cookie_url))
            finally:
                await close_context(reopened)
    print('Chrome excluyó al segundo proceso, completó el cierre concurrente y reabrió el perfil con su cookie.')


if __name__ == '__main__':
    asyncio.run(smoke())
