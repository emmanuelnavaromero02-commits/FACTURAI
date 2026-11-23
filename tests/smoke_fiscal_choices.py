"""Use the normal runner with an offline toll portal and confirmed, synthetic fiscal data."""
import asyncio
from pathlib import Path
import tempfile
from unittest.mock import patch

from facturador.db import Store
from facturador.fiscal_choices import fiscal_choices
from facturador.models import Action, Receipt, Target
from facturador.runner import Runner
from test_core import PROFILE

URL = 'https://facturacioncapufe.com.mx/Capufe/facturacionrapida'
TICKET = Receipt(provider='capufe', merchant='CAPUFE', billing_codes=['ABCDE1234567890000'],
                 date='2026-11-21', amount='50.00', payment_code='04')
HTML = """
<label for="rfc">RFC</label><input id=rfc><input id=nombre><input id=domicilioFiscalReceptor><input id=correo>
<label for="regimenFiscalReceptor">Régimen fiscal</label>
<div id="regimenFiscalReceptor" class="p-dropdown" tabindex="0">
  <select hidden><option value=""></option></select><label class="p-dropdown-label">Seleccionar</label>
  <button type=button aria-haspopup=listbox>keyboard_arrow_down</button>
  <ul role=listbox hidden><li role=option>601 - General de Ley Personas Morales</li>
  <li role=option>612 - Personas Físicas con Actividades Empresariales y Profesionales</li></ul>
</div>
<label for=usoCfdi>Uso del CFDI</label><select id=usoCfdi disabled>
<option value="">Selecciona</option><option value="G03">Gastos en general</option></select>
<label for=formaPago>Forma de pago</label><select id=formaPago>
<option value=01>Efectivo</option><option value=04>Tarjeta de crédito</option><option value=28>Tarjeta de débito</option></select>
<label for=metodo> Método de pago </label><select id=metodo><option value=PPD>PPD</option></select>
<input id=codigoPorCfdi type=checkbox><table><tbody><tr>
<td></td><td>ABCDE1234567890000</td><td>Plaza de prueba</td><td>2026-noviembre-21 12:00</td><td>$50.00</td>
</tr></tbody></table>
<button id=preparar>Facturar conceptos</button><label>Folio de revisión<input oninput="emitir.disabled=false"></label>
<button id=emitir disabled hidden>Sí estoy seguro</button>
<script>
const regime = document.getElementById('regimenFiscalReceptor');
const list = regime.querySelector('ul');
regime.onclick = event => {
  if (!rfc.value) return;
  if (event.target.matches('li')) {
    const text = event.target.textContent;
    regime.querySelector('select').replaceChildren(new Option(text, text.slice(0,3), true, true));
    regime.querySelector('label').textContent = text;
    usoCfdi.disabled = false;
    list.hidden = true;
  } else list.hidden = !list.hidden;
};
preparar.onclick = () => { preparar.hidden = true; emitir.hidden = false; };
window.submits = 0;
emitir.onclick = () => { window.submits++; document.body.innerHTML = '<p>Factura emitida</p>'; };
</script>"""


def click(name):
    return Action(kind='click', target=Target(by='role', role='button', name=name, exact=True),
                  value=None, reason='Prueba de formulario', risk='issue')


async def smoke():
    with tempfile.TemporaryDirectory() as tmp:
        store = Store(Path(tmp))
        runner = Runner(store)
        try:
            await runner.start_browser()
            await runner.context.route(URL, lambda route: route.fulfill(body=HTML, content_type='text/html; charset=utf-8'))
            row, _ = store.add('prueba.jpg', 'hash-de-prueba')
            key = row['id']
            store.save_receipt(key, TICKET, ready=True)
            fill = Action(kind='fill', target=Target(by='role', role='textbox', name='Folio de revisión', exact=True),
                          value=TICKET.billing_codes[0], reason='Completar el campo que habilita la emisión', risk='normal')
            actions = iter([click('Facturar conceptos'), click('Sí estoy seguro'), fill, click('Sí estoy seguro'),
                            Action(kind='done', target=None, value=None, reason='Terminó', risk='normal')])
            def next_action(instructions, observation, screenshot):
                action = next(actions)
                if action is fill:
                    assert 'deshabilitado' in observation and not store.get(key)['issue_attempted']
                return action
            with patch('facturador.runner.AI.next_action', side_effect=next_action):
                runner.schedule(key, PROFILE)
                task = runner.job_tasks[key]
                async with asyncio.timeout(30):
                    while key not in runner.pending and not task.done():
                        await asyncio.sleep(.05)
                assert key in runner.pending and runner.pending[key]['kind'] == 'issue', store.get(key)['message']
                page = runner.viewer.page
                assert await page.locator('#regimenFiscalReceptor select').input_value() == PROFILE.regime
                assert await page.locator('#usoCfdi').input_value() == PROFILE.cfdi_use
                assert await page.locator('#formaPago').input_value() == TICKET.payment_code
                assert await page.locator('#metodo').input_value() == 'PPD'
                assert not store.get(key)['issue_attempted'] and not await page.evaluate('window.submits')
                await runner.continue_job(key, 'approve')
                await asyncio.wait_for(task, timeout=10)
                assert await page.evaluate('window.submits') == 1
                assert store.get(key)['issuance_seen']
            await page.set_content('<label>Forma de pago<select onchange="this.value=\'01\'">'
                                   '<option value="01">Efectivo</option><option value="04">Crédito</option></select></label>')
            changed, pending = await fiscal_choices(page, PROFILE, TICKET, apply=True)
            assert changed is None and pending == ['forma de pago (04)']
            await page.set_content("""<button role=combobox aria-controls=tarjetas aria-label='Forma de pago'
                aria-expanded=false onclick="this.setAttribute('aria-expanded','true'); tarjetas.hidden=false">Elegir</button>
                <div id=tarjetas role=listbox hidden><button role=option onclick="
                const c=document.querySelector('[role=combobox]');c.innerText=this.innerText;
                c.setAttribute('aria-expanded','false');tarjetas.hidden=true">Tarjeta de crédito</button></div>""")
            assert await fiscal_choices(page, PROFILE, TICKET, apply=True) == ('payment', [])
            assert await fiscal_choices(page, PROFILE, TICKET) == (None, [])
            # Prime may portal its list under body and put aria-owns on its button.
            await page.set_content("""<div id=formaPago class=p-dropdown tabindex=0
                onclick="pago.hidden=!pago.hidden"><select hidden><option value=""></option></select>
                <span class=p-dropdown-label>Seleccionar</span><button aria-owns=pago>Elegir</button></div>
                <ul role=listbox id=pago hidden><li role=option onclick="
                formaPago.querySelector('select').replaceChildren(new Option('Tarjeta de crédito','04',true,true));
                formaPago.querySelector('span').innerText=this.innerText;pago.hidden=true">Tarjeta de crédito</li></ul>
                <ul role=listbox><li role=option onclick="window.wrong=true">Tarjeta de crédito</li></ul>""")
            assert await fiscal_choices(page, PROFILE, TICKET, apply=True) == ('payment', [])
            assert not await page.evaluate('window.wrong')
            # Two linked panels (or a second owner for the first) are ambiguous.
            await page.evaluate("""() => {
                formaPago.querySelector('select').replaceChildren(new Option('','',true,true));
                formaPago.querySelector('span').innerText='Seleccionar';
                document.querySelectorAll('ul')[1].id='otra';
                formaPago.setAttribute('aria-controls','otra');
            }""")
            assert await fiscal_choices(page, PROFILE, TICKET, apply=True) == (None, ['forma de pago (04)'])
            await page.evaluate("""() => {
                formaPago.removeAttribute('aria-controls');
                const otro = document.createElement('button'); otro.setAttribute('aria-controls','pago');
                document.body.append(otro);
            }""")
            assert await fiscal_choices(page, PROFILE, TICKET, apply=True) == (None, ['forma de pago (04)'])
            assert not await page.evaluate('window.wrong')
            await page.set_content("""<label>Forma de pago<select onchange="window.changed=true">
                <option value="">Seleccionar</option><option value=28>04 - Tarjeta de crédito</option>
                </select></label>""")
            assert await fiscal_choices(page, PROFILE, TICKET, apply=True) == (None, ['forma de pago (04)'])
            assert not await page.evaluate('window.changed')
            cheque = TICKET.model_copy(update={'payment_code':'02'})
            await page.set_content("""<label>Forma de pago<select>
                <option value=''>Seleccionar</option><option value='CASH'>Efectivo</option>
                <option value='CHEQUE'>Cheque nominativo</option></select></label>""")
            assert await fiscal_choices(page, PROFILE, cheque, apply=True) == ('payment', [])
            assert await page.locator('select').input_value() == 'CHEQUE'
            assert await fiscal_choices(page, PROFILE, cheque) == (None, [])
            await page.set_content('<label>Régimen fiscal del emisor<select><option>601</option>'
                                   '<option>612</option></select></label>')
            assert await fiscal_choices(page, PROFILE, TICKET, apply=True) == (None, [])
            print('Chrome verificó régimen personalizado, uso dependiente, pago confirmado y una sola emisión simulada.')
        finally:
            await runner.close()


if __name__ == '__main__':
    asyncio.run(smoke())
