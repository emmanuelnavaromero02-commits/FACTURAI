"""Invoice a synthetic café ticket through a printed, parameterized URL outside the catalog."""
import asyncio
import io
import tempfile
import zipfile
from pathlib import Path
from urllib.parse import parse_qs, urlsplit
from unittest.mock import patch

import httpx

from facturador.app import create_app
from facturador.catalog import CATALOG, CUSTOM_PORTAL
from facturador.models import Action, Target
from facturador.viewer import CAPTCHA_IDLE_SECONDS
from test_core import FISCAL_UUID, PROFILE, pdf_bytes, xml_bytes
from test_fiscal_capture import synthetic_pdf

ORIGIN = 'https://cafeteria.example.com'
URL = ORIGIN + '/autoservicio/factura?sucursal=7&ticket=CAF-42'
FOLIO = 'CAF-42'
FORM = """
<form method="post" action="/comprobante">
<label>Folio de consumo<input name="consumo" required></label>
<label>RFC del cliente<input name="receptor" required></label>
<label>Régimen fiscal del receptor<select name="categoria" required>
<option value="">Elige</option><option value="601">General de Ley</option><option value="612">612 - Actividades empresariales</option></select></label>
<label>Uso del CFDI<select name="destino" required>
<option value="">Elige</option><option value="G03">Gastos en general</option></select></label>
<label>Forma de pago<select name="cobro" required>
<option value="01">Efectivo</option><option value="28">Tarjeta de débito</option></select></label>
<button>Emitir factura</button></form>
"""


def action(kind, role, name, value=None):
    return Action(kind=kind, target=Target(by='role', role=role, name=name, exact=True),
                  value=value, reason='Comercio de prueba', risk='normal')


async def smoke():
    assert not any(ORIGIN == 'https://' + host for entry in CATALOG.values() for host in entry['hosts'])
    released_by_operator = False
    visits, submissions = [], []
    invoice_pdf, invoice_xml = pdf_bytes(), xml_bytes(FormaPago='28')
    archive = io.BytesIO()
    with zipfile.ZipFile(archive, 'w') as pair:
        pair.writestr('cafeteria.pdf', invoice_pdf)
        pair.writestr('cafeteria.xml', invoice_xml)

    async def merchant(route):
        request = route.request
        address = urlsplit(request.url)
        visits.append((request.method, request.url))
        if request.method == 'GET' and request.url == URL:
            # This local fixture changes only when the synthetic operator reports completion.
            html = FORM if released_by_operator else '<h1>Verifica que eres humano</h1>'
            await route.fulfill(content_type='text/html; charset=utf-8', body=html)
        elif request.method == 'POST' and address.path == '/comprobante':
            submissions.append(parse_qs(request.post_data or ''))
            await route.fulfill(content_type='text/html; charset=utf-8', body=(
                f'<h1>Factura emitida</h1><p>{FISCAL_UUID}</p><a href="/archivos">Descargar comprobantes</a>'))
        elif request.method == 'GET' and address.path == '/archivos':
            await route.fulfill(content_type='application/zip', body=archive.getvalue(),
                                headers={'Content-Disposition': 'attachment; filename="cafeteria.zip"'})
        else:
            await route.fulfill(status=404)

    with tempfile.TemporaryDirectory() as folder:
        app = create_app(Path(folder))
        runner, store = app.state.runner, app.state.store
        transport = httpx.ASGITransport(app=app)
        headers = {'X-App-Token': app.state.csrf_token, 'Origin': 'http://127.0.0.1:8765'}
        try:
            await runner.start_browser()
            assert runner.engine == 'Google Chrome'
            await runner.context.route(ORIGIN + '/**', merchant)
            async with httpx.AsyncClient(transport=transport, base_url=headers['Origin'], headers=headers) as client:
                (await client.put('/api/profile', json=PROFILE.model_dump())).raise_for_status()
                printed = synthetic_pdf('CAFETERIA PRUEBA\nFecha: 2026-11-13\nFolio: CAF-42\nTOTAL $100.00\n'
                                        'Forma de pago: Tarjeta de debito\nFacturacion: ' + URL)
                upload = await client.post('/api/upload', files={'files': ('consumo.pdf', printed, 'application/pdf')})
                upload.raise_for_status()
                key = upload.json()[0]['id']
                path = '/api/jobs/' + key
                reading = await client.post(path + '/read-local', json={})
                reading.raise_for_status()
                assert URL in reading.json()['portal_links']
                analyzed = await client.post(path + '/analyze', json={'mode': 'local'})
                analyzed.raise_for_status()
                ticket = analyzed.json()['receipt']
                assert (ticket['ticket_number'], ticket['amount'], ticket['date'], ticket['payment_code']) == (FOLIO, '100.00', '2026-11-13', '28'), ticket
                ticket.update(provider=CUSTOM_PORTAL, merchant='Cafetería Prueba', portal_url=URL)
                (await client.put(path, json=ticket)).raise_for_status()
                options = {'consent': True, 'mode': 'agent', 'auto_issue': True}
                planner = iter([action('fill', 'textbox', 'Folio de consumo', FOLIO),
                                action('fill', 'textbox', 'RFC del cliente', PROFILE.rfc),
                                action('click', 'button', 'Emitir factura'),
                                action('click', 'link', 'Descargar comprobantes')])
                wait = Action(kind='wait', target=None, value=None, reason='Esperando archivos', risk='normal')
                def plan(*_):
                    assert released_by_operator
                    return next(planner, wait)

                async def pending(kind, task):
                    async with asyncio.timeout(22):
                        while runner.pending.get(key, {}).get('kind') != kind:
                            if task.done():
                                await task
                                raise AssertionError((kind, store.get(key)['message'], store.events(key)[-4:]))
                            await asyncio.sleep(.04)
                # Only the fixture origin is available. No DNS, Google sign-in or external challenge is involved.
                async def fixture_dns(url):
                    return urlsplit(url).netloc == 'cafeteria.example.com'
                with (patch.object(app.state.plan, 'status', return_value={'status': 'ready'}),
                      patch('facturador.runner.resolves_publicly', side_effect=fixture_dns),
                      patch('facturador.runner.AI.next_action', side_effect=plan) as model):
                    for unconfirmed in ('', URL.replace('sucursal=7', 'sucursal=9')):
                        response = await client.post(path + '/run', json={**options, 'confirmed_url': unconfirmed})
                        assert response.status_code == 422 and 'Confirma la dirección' in response.text and not visits
                    (await client.post(path + '/run', json={**options, 'confirmed_url': URL})).raise_for_status()
                    task = runner.job_tasks[key]
                    await pending('captcha', task)
                    assert not submissions and not model.called and visits[0] == ('GET', URL)
                    revision = runner.viewer.revision
                    control = '/api/browser/control'
                    wrong = await client.post(control, json={'job_id': 'otro-ticket', 'revision': revision})
                    assert wrong.status_code == 422 and not runner.viewer.channel
                    accepted = await client.post(control, json={'job_id': key, 'revision': revision})
                    accepted.raise_for_status()
                    channel = accepted.json()['channel']
                    concurrent = await client.post(control, json={'job_id': key, 'revision': revision})
                    assert concurrent.status_code == 422 and runner.viewer.channel == channel
                    old_client = await client.post(control, json={'revision': revision})
                    assert old_client.status_code == 422 and runner.viewer.channel == channel
                    alive = '/api/browser/control/alive'
                    (await client.post(alive, json={'job_id': key, 'channel': channel})).raise_for_status()
                    # Leaving the panel gives up only this lease and keeps the ticket paused.
                    (await client.post('/api/browser/release', json={'channel':channel, 'abandon':True})).raise_for_status()
                    assert not runner.viewer.channel and not runner.pending[key]['future'].done()
                    old_client = await client.post(control, json={'revision': revision})
                    old_client.raise_for_status()
                    abandoned = old_client.json()['channel']
                    assert abandoned != channel and runner.viewer.channel_protected
                    # Advance only the recorded activity time to represent a crashed browser.
                    runner.viewer.channel_activity -= CAPTCHA_IDLE_SECONDS + 1
                    accepted = await client.post(control, json={'job_id':key, 'revision':revision})
                    accepted.raise_for_status()
                    channel = accepted.json()['channel']
                    assert channel != abandoned
                    stale = await client.post(alive, json={'job_id':key, 'channel':abandoned})
                    assert stale.status_code == 422 and runner.viewer.channel == channel
                    (await client.post('/api/browser/release', json={'channel': channel})).raise_for_status()
                    assert runner.pending[key]['kind'] == 'captcha' and not runner.viewer.channel
                    refused = await client.post(path + '/decision', json={'decision': 'resume'})
                    assert refused.status_code == 422 and 'verificación' in refused.text and not model.called, (refused.status_code, refused.text)
                    accepted = await client.post(control, json={'job_id': key, 'revision': runner.viewer.revision})
                    accepted.raise_for_status()
                    released_by_operator = True
                    # The fixture displays its form in the same document after human completion.
                    await runner.viewer.page.set_content(FORM)
                    (await client.post('/api/browser/release', json=accepted.json())).raise_for_status()
                    await pending('issue', task)
                    page = runner.viewer.page
                    for field, expected in [('categoria',PROFILE.regime), ('destino',PROFILE.cfdi_use), ('cobro','28')]:
                        assert await page.locator(f'select[name="{field}"]').input_value() == expected
                    assert not submissions and not store.get(key)['issue_attempted']
                    (await client.post(path + '/decision', json={'decision': 'approve'})).raise_for_status()
                    await asyncio.wait_for(task, timeout=30)
            result = store.get(key)
            assert submissions == [dict(consumo=[FOLIO], receptor=[PROFILE.rfc], categoria=['612'], destino=['G03'], cobro=['28'])]
            assert result['status'] == 'verified' and result['uuid'] == FISCAL_UUID, result['message']
            assert Path(result['pdf']).read_bytes() == invoice_pdf and Path(result['xml']).read_bytes() == invoice_xml
            print('Cafetería fuera del catálogo: ticket PDF y URL parametrizada, revisión del dominio, pausa humana, SAT 612/G03/28, aprobación única y cotejo PDF/XML.')
        finally:
            await runner.close()


if __name__ == '__main__':
    asyncio.run(smoke())
