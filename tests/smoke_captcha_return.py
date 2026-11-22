"""Simulate the operator returning Chrome after a challenge using offline merchant pages."""
import asyncio
import tempfile
from pathlib import Path
from unittest.mock import patch
from urllib.parse import urlsplit

from facturador.catalog import CATALOG
from facturador.db import Store
from facturador.models import Action, Target
from facturador.runner import Runner
from test_core import PROFILE, RECEIPT


FORM = '<label>RFC<input></label><button>Facturar</button>'


async def smoke():
    origin = 'https://comercio.recibos.example'
    provider = {'name':'Comercio de prueba', 'url':origin+'/', 'hosts':['comercio.recibos.example']}
    with tempfile.TemporaryDirectory() as tmp, patch.dict(CATALOG, {'sintetico': provider}):
        store = Store(Path(tmp))
        runner = Runner(store)
        start = runner.start_browser
        handled = set()
        async def merchant(route):
            path = urlsplit(route.request.url).path
            content = FORM if path == '/otra-pagina' else '<h1>Verifica que eres humano</h1>'
            await route.fulfill(content_type='text/html; charset=utf-8', body=content)
        async def open_browser():
            await start()
            assert runner.engine == 'Google Chrome'
            if runner.context not in handled:
                handled.add(runner.context)
                await runner.context.route('**/*', merchant)
        async def pending(key, kind):
            async with asyncio.timeout(12):
                while runner.pending.get(key, {}).get('kind') != kind:
                    await asyncio.sleep(.03)
        action = Action(kind='click',target=Target(by='role',role='button',name='Facturar',exact=True),
                        value=None,risk='issue',reason='Confirmar emisión')
        try:
            with patch.object(runner, 'start_browser', open_browser), patch('facturador.runner.AI.next_action', return_value=action) as model:
                for index, mode in enumerate(('persistent', 'error', 'redirect', 'ambiguous', 'completed', 'capufe')):
                    key = store.add('fixture.png', 'caso-'+mode)[0]['id']
                    receipt = RECEIPT.model_copy(update={'provider': 'capufe' if mode=='capufe' else 'sintetico',
                                                         'billing_codes':[f'PRUEBA{index}']})
                    store.save_receipt(key, receipt, ready=True)
                    runner.schedule(key, PROFILE, auto_issue=True)
                    task = runner.job_tasks[key]
                    await pending(key, 'captcha')
                    page = runner.viewer.page
                    calls = model.call_count
                    await runner.release_control('no-es-la-sesion')
                    assert runner.pending[key]['kind'] == 'captcha'
                    lease = await runner.viewer.take_control(runner.viewer.revision)
                    if mode == 'redirect':
                        await page.goto(origin+'/otra-pagina')
                    elif mode == 'error':
                        await page.set_content('<h1>Error 503</h1>'+FORM)
                    elif mode == 'ambiguous':
                        await page.evaluate("fetch('/accion-del-comercio',{method:'POST'})")
                        await page.set_content(FORM)
                    elif mode in ('completed', 'capufe'):
                        # The offline fixture supplies the state after the human completed the challenge.
                        await page.set_content(FORM)
                    await runner.release_control(lease)
                    if mode in ('persistent', 'error', 'redirect'):
                        assert runner.pending[key]['kind'] == 'captcha'
                        try:
                            await runner.continue_job(key, 'resume')
                        except ValueError:
                            pass
                        else:
                            raise AssertionError('No hay evidencia de que el operador resolviera la verificación.')
                        assert model.call_count == calls
                        await runner.stop(key)
                    elif mode == 'ambiguous':
                        await asyncio.wait_for(task, 5)
                        assert store.get(key)['status'] == 'uncertain' and model.call_count == calls
                    else:
                        await pending(key, 'issue')
                        assert runner.viewer.page is page and model.call_count == calls+1
                        assert not store.get(key)['issue_attempted']
                        await runner.stop(key)
                    assert store.get(key)['human_exposed'] and not store.get(key)['issue_attempted']
                print('OK Chrome: desafío persistente, error, redirección y acción ambigua conservan protecciones; dos comercios retoman el formulario tras devolución humana.')
        finally:
            await runner.close()


if __name__ == '__main__':
    asyncio.run(smoke())
