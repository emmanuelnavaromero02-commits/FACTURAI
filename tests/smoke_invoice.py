"""Trámite de prueba en Chromium. Portal y decisiones de IA son sintéticos."""

import asyncio
import io
import tempfile
import zipfile
from pathlib import Path
from unittest.mock import patch
from urllib.parse import parse_qs, urlsplit

from facturador.catalog import CATALOG
from facturador.db import Store
from facturador.models import Action, Target
from facturador.runner import Runner
from test_core import FISCAL_UUID, PROFILE, RECEIPT, pdf_bytes, xml_bytes


async def smoke():
    origin = 'https://comercio.example.test'
    merchant = {'url': origin + '/', 'hosts': ['comercio.example.test'], 'name': 'Comercio de prueba'}
    xml, pdf = xml_bytes(), pdf_bytes()
    archive = io.BytesIO()
    with zipfile.ZipFile(archive, 'w') as compressed:
        compressed.writestr('factura.xml', xml)
        compressed.writestr('factura.pdf', pdf)
    submissions = []
    requests = []

    async def portal(route):
        path = urlsplit(route.request.url).path
        requests.append((route.request.method, path))
        if path == '/':
            await route.fulfill(content_type='text/html; charset=utf-8', body=(
                '<form method="post" action="/emision"><label>RFC<input name="rfc" required></label>'
                '<label>Constancia<input type="file" required></label>'
                '<button>Facturar</button></form>'))
        elif path == '/emision' and route.request.method == 'POST':
            submissions.append(parse_qs(route.request.post_data or ''))
            await route.fulfill(content_type='text/html; charset=utf-8', body=(
                f'<h1>Factura emitida</h1><p>{FISCAL_UUID}</p>'
                '<a href="/archivos">Descargar PDF y XML</a>'))
        elif path == '/archivos':
            await route.fulfill(content_type='application/zip', body=archive.getvalue(),
                                headers={'Content-Disposition': 'attachment; filename="factura.zip"'})
        else:
            await route.fulfill(status=404, body='')

    def action(kind, role, name, value=None):
        return Action(kind=kind, target=Target(by='role', role=role, name=name, exact=True),
                      value=value, reason='Trámite sintético', risk='normal')

    actions = iter([action('fill', 'textbox', 'RFC', PROFILE.rfc),
                    action('click', 'button', 'Facturar'),
                    action('click', 'link', 'Descargar PDF y XML')])
    wait = Action(kind='wait', target=None, value=None, reason='Esperar la descarga', risk='normal')
    ask = Action(kind='human', target=None, value=None, reason='El portal pide la constancia.', risk='unknown')

    def decide(*seen):
        # Told that the portal wants a file that is not the ticket, the model asks the person for it.
        if any('El portal exige un archivo' in str(item) for item in seen):
            return ask
        return next(actions, wait)
    with tempfile.TemporaryDirectory() as folder, patch.dict(CATALOG, {'prueba': merchant}):
        store = Store(Path(folder))
        runner = Runner(store)
        try:
            await runner.start_browser()
            await runner.context.route(origin + '/**', portal)
            row, _ = store.add('ticket-de-prueba', 'prueba-integracion')
            key = row['id']
            store.save_receipt(key, RECEIPT.model_copy(update={'provider': 'prueba'}), ready=True)
            with patch('facturador.runner.AI.next_action', side_effect=decide):
                runner.schedule(key, PROFILE)
                task = runner.job_tasks[key]
                async with asyncio.timeout(30):
                    while key not in runner.pending and not task.done():
                        await asyncio.sleep(0.05)
                assert runner.pending[key]['kind'] == 'attachment'
                lease = await runner.viewer.take_control(runner.viewer.revision)
                await runner.viewer.page.get_by_label('Constancia').click()
                async with asyncio.timeout(8):
                    while not runner.viewer.state()['upload']:
                        await asyncio.sleep(.05)
                await runner.viewer.attach(lease, runner.viewer.state()['upload']['id'],
                                           {'name': 'ejemplo.pdf', 'mimeType': 'application/pdf', 'buffer': pdf})
                await runner.continue_job(key, 'resume')
                async with asyncio.timeout(30):
                    while runner.pending.get(key, {}).get('kind') != 'issue' and not task.done():
                        await asyncio.sleep(0.05)
                assert key in runner.pending and runner.pending[key]['kind'] == 'issue', store.get(key)['message']
                assert not submissions
                await runner.continue_job(key, 'approve')
                async with asyncio.timeout(30):
                    while not task.done():
                        pending = runner.pending.get(key, {})
                        if pending and pending['kind'] != 'issue':
                            raise AssertionError((pending['kind'], requests, store.events(key),
                                                  await runner.viewer.page.locator('body').aria_snapshot()))
                        await asyncio.sleep(0.05)
                await task
            result = store.get(key)
            assert submissions == [{'rfc': [PROFILE.rfc]}], submissions
            assert result['status'] == 'verified', result['message']
            assert result['uuid'] == FISCAL_UUID
            assert Path(result['xml']).read_bytes() == xml and Path(result['pdf']).read_bytes() == pdf
            print('OK: la cola pidió el adjunto, Chromium llenó el RFC, esperó aprobación, descargó y cotejó PDF/XML sintéticos.')
        finally:
            await runner.close()


if __name__ == '__main__':
    asyncio.run(smoke())
