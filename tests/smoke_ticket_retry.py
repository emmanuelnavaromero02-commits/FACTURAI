"""Resume one paused ticket in Chrome; protect a ticket operated manually from a second issue."""
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


async def smoke():
    host = 'recibos.mercado.example'
    origin = 'https://' + host
    provider = {'name':'Mercado de prueba', 'url':origin+'/', 'hosts':[host]}
    gets, posts = [], []

    async def serve(route):
        path = urlsplit(route.request.url).path
        if path == '/':
            gets.append(path)
            if len(gets) == 1:
                return await route.fulfill(status=503, content_type='text/html', body='Servicio temporalmente ocupado')
            return await route.fulfill(content_type='text/html', body='<form action="/emitir" method="post"><label>Referencia<input></label><button>Facturar</button></form>')
        if path == '/emitir' and route.request.method == 'POST':
            posts.append(path)
            return await route.fulfill(content_type='text/html', body='<h1>Factura emitida</h1>')
        await route.fulfill(status=404)

    with tempfile.TemporaryDirectory() as temporary, patch.dict(CATALOG, {'mercado_prueba': provider}):
        store = Store(Path(temporary))
        runner = Runner(store)
        start = runner.start_browser
        context = None

        async def start_with_merchant():
            nonlocal context
            await start()
            if runner.context is not context:
                context = runner.context
                assert runner.engine == 'Google Chrome'
                await context.route(origin+'/**', serve)

        async def wait_pending(key, kind):
            async with asyncio.timeout(15):
                while runner.pending.get(key, {}).get('kind') != kind:
                    if key not in runner.job_tasks or runner.job_tasks[key].done():
                        raise AssertionError(store.get(key)['message'])
                    await asyncio.sleep(.04)
            return runner.pending[key]['page']

        try:
            for mode in ('retry', 'controlled', 'automatic', 'same_form'):
                key = store.add('synthetic.png', 'prueba-'+mode)[0]['id']
                receipt = RECEIPT.model_copy(update={'provider':'mercado_prueba','billing_codes':['COD-'+mode]})
                store.save_receipt(key, receipt, ready=True)
                actions = [
                    Action(kind='human', target=None, value=None, reason='Revisar un campo del ticket', risk='unknown'),
                    Action(kind='click', target=Target(by='role', role='button', name='Facturar', exact=True),
                           value=None, reason='Emitir el CFDI', risk='issue'),
                    Action(kind='done', target=None, value=None, reason='Se emitió el CFDI', risk='normal'),
                ]
                if mode == 'same_form':
                    actions = [actions[0], actions[0]]
                with (patch.object(runner, 'start_browser', start_with_merchant),
                      patch('facturador.runner.AI.next_action', side_effect=actions) as model,
                      patch('facturador.runner.REOBSERVE_DELAY', .2 if mode in ('automatic', 'same_form') else 5)):
                    runner.schedule(key, PROFILE, auto_issue=True)
                    task = runner.job_tasks[key]
                    original = await wait_pending(key, 'unknown')
                    assert not store.get(key)['human_exposed'] and not store.get(key)['issue_attempted']
                    if mode == 'controlled':
                        lease = await runner.viewer.take_control(runner.viewer.revision)
                        store.mark_human_exposed(key)
                        await runner.viewer.release(lease)
                    if mode in ('controlled', 'retry'):
                        results = await asyncio.gather(runner.retry_job(key), runner.retry_job(key))
                    if mode == 'controlled':
                        assert any(r['action'] == 'recovering' for r in results)
                        await task
                        assert store.get(key)['status'] == 'uncertain' and not store.get(key)['issue_attempted']
                    elif mode == 'same_form':
                        async with asyncio.timeout(4):
                            while model.call_count < 2:
                                await asyncio.sleep(.03)
                        await asyncio.sleep(.5)
                        assert model.call_count == 2 and runner.pending[key]['kind'] == 'unknown'
                        assert runner.viewer.manual
                        await runner.stop(key)
                    else:
                        if mode == 'retry':
                            assert any(r['action'] == 'resumed' for r in results)
                        assert original is await wait_pending(key, 'issue')
                        assert (await runner.retry_job(key))['action'] == 'needs_user'
                        if mode == 'automatic':
                            assert any(e['kind'] == 'automatic_reobservation' for e in store.events(key))
                        await runner.continue_job(key, 'approve')
                        await task
                        assert store.get(key)['status'] == 'issued_waiting_files'
                    assert len(posts) == (1 if mode in ('retry', 'controlled') else 2)
            assert len(gets) == 5
            print('OK Chrome: 503 recuperado, doble clic, reobservación automática acotada, aprobación y recuperación tras control manual.')
        finally:
            await runner.close()


if __name__ == '__main__':
    asyncio.run(smoke())
