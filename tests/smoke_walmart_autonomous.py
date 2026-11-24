"""Run the Walmart lookup and issue gate against offline synthetic pages."""
import asyncio
import tempfile
from pathlib import Path
from unittest.mock import patch

from facturador.db import Store
from facturador.models import Action, Profile, Receipt, Target
from facturador.runner import Runner


ROOT = 'https://facturacion-clientes.walmart.com/'
ADDRESS = ROOT + 'address'
TC = '1234567890123456789012'
TR = '01234'
PROFILE = Profile(rfc='XAXX010101000', name='PRUEBA AUTOMATIZACION', postal_code='76146',
                  regime='612', cfdi_use='G03', email='prueba@example.com')
RECEIPT = Receipt(provider='walmart', merchant='Walmart de prueba', amount='100.00',
                  date='2026-11-20', ticket_number=TC, billing_codes=['TR#' + TR], payment_code='08')
LOOKUP = f'''<!doctype html><html><body><form>
  <label>MEMBRESÍA O RFC<input value="{PROFILE.rfc}"></label>
  <label>Código Postal<input value="{PROFILE.postal_code}"></label>
  <label>Número de Ticket<input value="{TC}"></label>
  <label># Transacción<input value="{TR}"></label>
  <button type="button" onclick="location.href='/address'">Continuar</button>
</form></body></html>'''
FISCAL = f'''<!doctype html><html><body><form>
  <label>RFC<input value="{PROFILE.rfc}"></label>
  <label>Razón Social<input value="{PROFILE.name}"></label>
  <label>Código Postal<input value="{PROFILE.postal_code}"></label>
  <label>Correo Electrónico<input value="{PROFILE.email}"></label>
  <label>Régimen Fiscal<select><option value="612" selected>Personas Físicas con Actividades Empresariales y Profesionales</option></select></label>
  <label>Uso Factura<select><option value="G03" selected>Gastos en general</option></select></label>
  <button type="button" onclick="window.submits=(window.submits||0)+1;
    document.body.innerHTML='<p>Factura emitida</p>'">Aceptar</button>
</form></body></html>'''


def click(name, risk='normal'):
    return Action(kind='click', target=Target(by='role', role='button', name=name, exact=True),
                  value=None, reason='Avanzar el ticket sintético cotejado', risk=risk)


async def smoke():
    with tempfile.TemporaryDirectory() as directory:
        store = Store(Path(directory))
        runner = Runner(store)
        try:
            await runner.start_browser()

            async def portal(route):
                if route.request.url == ROOT:
                    await route.fulfill(content_type='text/html; charset=utf-8', body=LOOKUP)
                elif route.request.url == ADDRESS:
                    await route.fulfill(content_type='text/html; charset=utf-8', body=FISCAL)
                else:
                    await route.abort()

            await runner.context.route('**/*', portal)
            row, _ = store.add('synthetic.png', 'walmart-synthetic')
            key = row['id']
            store.save_receipt(key, RECEIPT, ready=True)

            def next_action(_instructions, observation, _screenshot):
                if 'Número de Ticket' in observation:
                    return click('Continuar')
                if 'Razón Social' in observation:
                    return click('Aceptar')
                return Action(kind='done', target=None, value=None, reason='Emitida en prueba', risk='normal')

            with patch('facturador.runner.AI.next_action', side_effect=next_action):
                runner.schedule(key, PROFILE, auto_issue=True)
                await asyncio.wait_for(runner.job_tasks[key], timeout=30)

            page = runner.viewer.page
            assert await page.evaluate('window.submits') == 1, {
                'url': page.url, 'row': store.get(key)['status'], 'message': store.get(key)['message'],
                'events': [(event['kind'], event['detail']) for event in store.events(key)],
                'body': (await page.locator('body').inner_text())[:500],
            }
            result = store.get(key)
            assert result['issue_attempted'] and result['issuance_seen']
            assert not result['human_exposed'] and result['status'] == 'issued_waiting_files'
            assert not runner.pending
            assert [event['kind'] for event in store.events(key)].count('ticket_lookup_verified') == 1
            print('Walmart offline: TC/TR y perfil cotejados; un solo envío automático; sin pausa humana.')
        finally:
            await runner.close()


if __name__ == '__main__':
    asyncio.run(smoke())
