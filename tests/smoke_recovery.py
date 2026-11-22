"""Pérdida real de Chromium o Firefox y de la disponibilidad del modelo en un comercio sintético."""

import asyncio
import os
import tempfile
from pathlib import Path
from urllib.parse import urlsplit
from unittest.mock import patch

from playwright.async_api import Error as BrowserError

from facturador.auth import PlanError
from facturador.catalog import CATALOG
from facturador.db import Store
from facturador.models import Action, Target
from facturador.runner import Runner
from test_core import FISCAL_UUID, PROFILE, RECEIPT, pdf_bytes, xml_bytes


async def smoke():
    origin = 'https://recuperacion.example.test'
    provider = {'name': 'Comercio de prueba', 'url': origin + '/', 'hosts': ['recuperacion.example.test']}
    submissions = []
    contexts = []

    async def merchant(route):
        path = urlsplit(route.request.url).path
        if path == '/':
            await route.fulfill(content_type='text/html; charset=utf-8',
                                body='<iframe title="Datos del ticket" src="/datos"></iframe>'
                                     '<form method="post" action="/emitir"><button>Facturar</button></form>')
        elif path == '/datos':
            await route.fulfill(content_type='text/html; charset=utf-8',
                                body='<label>Pago<select><option value="">Elegir</option>'
                                     '<option value="04">Crédito</option></select></label>')
        elif path == '/emitir' and route.request.method == 'POST':
            submissions.append(route.request.method)
            await route.fulfill(content_type='text/html',
                                body='<h1>Factura emitida</h1><a href="/pdf">Descargar PDF</a> '
                                     '<a href="/xml">Descargar XML</a>')
        elif path in {'/pdf', '/xml'}:
            pdf = path == '/pdf'
            await route.fulfill(content_type='application/pdf' if pdf else 'application/xml',
                                body=pdf_bytes() if pdf else xml_bytes(),
                                headers={'Content-Disposition': f'attachment; filename="factura.{path[1:]}"'})
        else:
            await route.fulfill(status=404, body='')

    def action(kind, role, name, value=None):
        return Action(kind=kind, target=Target(by='role', role=role, name=name, exact=True),
                      value=value, reason='Prueba de recuperación', risk='normal')

    with tempfile.TemporaryDirectory() as folder, patch.dict(CATALOG, {'prueba': provider}):
        store = Store(Path(folder))
        if os.getenv('EXPECTED_ENGINE') == 'Firefox':
            # Keep the regression reproducible for existing volumes with the previous browser preference.
            old_profile = store.root / 'browser_profile_firefox'
            old_profile.mkdir()
            (old_profile / 'prefs.js').write_text('user_pref("dom.serviceWorkers.enabled", false);\n', encoding='utf-8')
        runner = Runner(store)
        start = runner.start_browser
        run_session = runner._session
        session_errors = []

        async def observe_session(*args):
            try:
                return await run_session(*args)
            except Exception as exc:
                session_errors.append(exc)
                raise

        async def start_test_browser():
            await start()
            expected = 'Google Chrome' if os.getenv('FACTURADOR_CHROME_HEADED') == '1' else 'Chromium'
            assert runner.engine == os.getenv('EXPECTED_ENGINE', expected), runner.engine
            if not contexts or contexts[-1] is not runner.context:
                contexts.append(runner.context)
                await runner.context.route(origin + '/**', merchant)

        def ticket(folio):
            row, _ = store.add('ticket-de-prueba', folio)
            key = row['id']
            store.save_receipt(key, RECEIPT.model_copy(update={'provider': 'prueba', 'billing_codes': [folio]}), ready=True)
            return key

        async def pending(key, kind, task, previous=None):
            async with asyncio.timeout(35):
                while True:
                    item = runner.pending.get(key)
                    if item and item['kind'] == kind and item['future'] is not previous:
                        return item
                    if task.done():
                        await task
                        failure = AssertionError((kind, store.get(key)['status'], store.get(key)['message'], store.events(key)))
                        raise failure from (session_errors[-1] if session_errors else None)
                    await asyncio.sleep(.04)

        def infer(instructions, state, screenshot):
            if 'Factura emitida' in state:
                raise PlanError('Cuota de prueba agotada.', 'limit')
            return action('click', 'button', 'Facturar')

        try:
            with patch.object(runner, 'start_browser', start_test_browser), patch.object(runner, '_session', observe_session):
                key = ticket('primera')
                with patch('facturador.runner.AI.next_action', side_effect=infer):
                    runner.schedule(key, PROFILE)
                    task = runner.job_tasks[key]
                    first = await pending(key, 'issue', task)
                    assert not submissions and not store.get(key)['issue_attempted']
                    ticket_page = runner.viewer.page
                    try:
                        await runner.context.close()
                    except BrowserError as exc:
                        # The runner can close this same context after its ticket page closes.
                        if not ticket_page.is_closed() or 'Target page, context or browser has been closed' not in exc.message:
                            raise
                    await pending(key, 'issue', task, first['future'])
                    assert len(contexts) == 2 and not submissions
                    assert sum(e['kind'] == 'browser_reopened' for e in store.events(key)) == 1
                    await runner.continue_job(key, 'approve')
                    await pending(key, 'plan', task)
                    assert submissions == ['POST']
                    assert store.get(key)['issuance_seen'] and not store.get(key)['uuid']
                    await runner.context.close()
                    await asyncio.wait_for(task, 15)
                    assert len(contexts) == 2 and submissions == ['POST']
                    assert store.get(key)['status'] == 'issued_waiting_files', store.get(key)
                    assert not store.queue(key, PROFILE)

                key = ticket('segunda')
                with patch('facturador.runner.AI.next_action', side_effect=PlanError('Cuota de prueba.', 'limit')):
                    runner.schedule(key, PROFILE)
                    task = runner.job_tasks[key]
                    await pending(key, 'plan', task)
                    page = runner.viewer.page
                    await runner.continue_job(key, 'manual')
                    await pending(key, 'operator', task)
                    assert runner.viewer.page is page and store.get(key)['issue_attempted']
                    assert not store.queue(key, PROFILE)
                    receipt = RECEIPT.model_copy(update={'provider': 'prueba'})
                    await page.frame_locator('iframe').get_by_label('Pago').wait_for()
                    await runner.execute(page, action('select', 'combobox', 'Pago', 'Crédito'), receipt)
                    assert await page.frame_locator('iframe').get_by_label('Pago').input_value() == '04'
                    await page.get_by_role('button', name='Facturar').click()
                    for extension in ('PDF', 'XML'):
                        async with page.expect_download():
                            await page.get_by_text('Descargar ' + extension).click()
                    await runner.continue_job(key, 'resume')
                    await asyncio.wait_for(task, 25)
                row = store.get(key)
                assert row['status'] == 'verified' and row['uuid'] == FISCAL_UUID, row
                assert Path(row['pdf']).read_bytes() == pdf_bytes() and Path(row['xml']).read_bytes() == xml_bytes()
                assert submissions == ['POST', 'POST']
                print(f'OK en {runner.engine}: reapertura antes de emitir, recuperación después de emitir y relevo manual sin cuota, con PDF/XML.')
        finally:
            await runner.close()


if __name__ == '__main__':
    asyncio.run(smoke())
