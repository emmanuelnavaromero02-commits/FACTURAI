"""Chrome checks that BP preparation cannot change the invoice until proved."""
import os
import unittest

from playwright.async_api import async_playwright

from facturador.bp_ticket import (BPProgress, bp_issue_proof, evidence_from_text,
                                  prepare_bp_ticket)
from facturador.models import Receipt
from test_core import PROFILE


RECEIPT = Receipt(provider='bp', merchant='BP PLAYAS (Emisor)', amount='1234.56',
                  date='2026-10-19', ticket_number='1234567',
                  billing_codes=['999911739'], payment_code='04',
                  details='Estación 11739. Consumo: 42.567 L de gasolina Premium.')
OCR = 'M.PAGO: TARJETA CREDITO\n19/10/2026 03:20:00 p.m. T3\nBP PLAYAS\nNo Ticket:1234567'
EVIDENCE = evidence_from_text(RECEIPT, OCR)
FORM = 'https://facturacion.grupocentra.mx/Karmi_FacturacionWeb/PAGE_Facturar_Gasolina_CFDI33/example'
LOAD = "javascript:if(clWDUtil.pfGetTraitement('PAGE_FACTURAR_GASOLINA_CFDI33',18,undefined)()){_JAEE(_PAGE_,'A17',16,2,48);}"
ADD = "javascript:if(clWDUtil.pfGetTraitement('PAGE_FACTURAR_GASOLINA_CFDI33',18,undefined)()){_JAEE(_PAGE_,'A24',16,2,48);}"


def fixture(*, total='1234.56', duplicate_station=False, same_empty=False):
    duplicate = '<option value="3">PLAYAS DUPLICADA #11739</option>' if duplicate_station else ''
    empty_cities = '<option value="3">VACIA A</option><option value="4">VACIA B</option>' if same_empty else ''
    grid = '<tr><td>1234567</td><td>Premium</td><td>42.567</td><td>1234.56</td></tr>'
    return f'''<style>input,select,a {{display:inline-block;min-width:80px;min-height:20px}}</style>
    <script>
      const clWDUtil={{pfGetTraitement:() => () => true}}, _PAGE_='test';
      function cityChanged() {{
        const city=document.getElementById('A15').value, station=document.getElementById('A14');
        station.innerHTML=city==='2' ? '<option value="1">&lt;SELECCIONE&gt;</option><option value="2">OTRA #11742</option>' :
          (city==='3'||city==='4') ? '<option value="1">&lt;SELECCIONE&gt;</option>' :
          '<option value="1">&lt;SELECCIONE&gt;</option><option value="4">PLAYAS DE TIJUANA #11739</option>{duplicate}';
      }}
      function stationChanged() {{
        document.getElementById('A41').value='Emisor BP';
        document.getElementById('A13').value='Playas de Tijuana';
      }}
      function _JAEE(page,id) {{
        if(id==='A17') {{
          document.getElementById('A23').value='1,234.56';
          document.getElementById('A19').value='42.567';
          document.getElementById('A18').value='Premium';
        }}
        if(id==='A24') {{
          document.getElementById('A28').value='{total}';
          document.getElementById('A33').disabled=false;
          document.getElementById('grid').innerHTML='{grid}';
        }}
      }}
    </script>
    <table><tr><td id="lzA15">Ciudad:</td><td><select id="A15" onchange="cityChanged()"><option value="1">&lt;SELECCIONE&gt;</option><option value="2">SAN LUIS</option>{empty_cities}<option value="16">TIJUANA</option></select></td></tr>
    <tr><td id="lzA14">Sucursal:</td><td><select id="A14" onchange="stationChanged()"><option value="1">&lt;SELECCIONE&gt;</option></select></td></tr></table>
    <input id="A4" value="{PROFILE.rfc}"><input id="A3" value="{PROFILE.name}">
    <input id="A7" value="{PROFILE.email}"><input id="A71" value="612 Personas Fisicas con Actividades Empresariales y Profesionales">
    <input id="A117" value="{PROFILE.postal_code}"><input id="A41" value=""><input id="A13" value="">
    <table><tr><td id="lzA12">No. Ticket:</td><td><input id="A12"></td>
    <td id="lzA47">Fecha Ticket:</td><td><input id="A47"></td>
    <td id="lzA48">Hora:</td><td><input id="A48"></td></tr></table>
    <a id="A17" href="{LOAD}">Cargar</a><a id="A24" href="{ADD}">Agregar</a>
    <input id="A18" disabled><input id="A19" disabled><input id="A23" disabled>
    <table id="ctzA49"><tbody id="grid"><tr><th># Ticket</th><th>Descripcion</th><th>Importe</th></tr></tbody></table>
    <span id="lzA33">Forma de Pago</span><select id="A33" disabled>
      <option value="1">Efectivo</option><option value="2">Tarjeta Debito</option><option value="3">Tarjeta Credito</option></select>
    <span id="lzA45">Uso de CFDI</span><select id="A45"><option value="5">Por definir</option><option value="6" selected>Gastos en general</option></select>
    <input id="A28" disabled><a id="A40" href="javascript:clWDUtil.pfGetTraitement('A40',0,undefined)();">Facturar</a>'''


class EvidenceTests(unittest.TestCase):
    def test_requires_correlated_station_ticket_payment_and_unique_printed_time(self):
        self.assertIsNotNone(EVIDENCE)
        self.assertEqual((EVIDENCE.station_id, EVIDENCE.ticket_time, EVIDENCE.ticket_date),
                         ('11739','15:20','19/10/2026'))
        self.assertIsNone(evidence_from_text(RECEIPT, OCR.replace('1234567', 'OTRO')))
        self.assertIsNone(evidence_from_text(RECEIPT, OCR+'\n07:20:00 p.m.'))
        self.assertIsNone(evidence_from_text(RECEIPT.model_copy(update={'billing_codes':['999911740']}), OCR))
        self.assertIsNone(evidence_from_text(RECEIPT, OCR.replace('TARJETA CREDITO', 'EFECTIVO')))


@unittest.skipUnless(os.path.isfile('/usr/bin/google-chrome-stable'), 'Requiere Google Chrome oficial')
class BpTicketTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.playwright = await async_playwright().start()
        self.browser = await self.playwright.chromium.launch(executable_path='/usr/bin/google-chrome-stable')
        self.page = await self.browser.new_page()
        await self.page.route('https://facturacion.grupocentra.mx/**',
                              lambda route: route.fulfill(content_type='text/html', body=fixture()))
        await self.page.goto(FORM)
        self.addAsyncCleanup(self.playwright.stop)
        self.addAsyncCleanup(self.browser.close)

    async def prepare(self, progress=None):
        progress = progress or BPProgress()
        actions = []
        for _ in range(10):
            action = await prepare_bp_ticket(self.page, RECEIPT, PROFILE, EVIDENCE, progress)
            if action is None:
                break
            actions.append(action)
        else:
            self.fail('La preparación BP repitió acciones sin terminar.')
        return actions

    async def reload_fixture(self, **kwargs):
        await self.page.unroute('https://facturacion.grupocentra.mx/**')
        await self.page.route('https://facturacion.grupocentra.mx/**',
                              lambda route: route.fulfill(content_type='text/html', body=fixture(**kwargs)))
        await self.page.goto(FORM)

    async def test_prepares_ticket_and_proves_only_final_page(self):
        self.assertFalse(await bp_issue_proof(self.page, RECEIPT, PROFILE, EVIDENCE))
        actions = await self.prepare()
        self.assertEqual(actions, ['Sucursal 11739','No. Ticket:','Fecha Ticket:','Hora:',
                                   'Cargar ticket','Agregar ticket','Forma de pago'])
        self.assertEqual(await self.page.locator('select#A15').evaluate('e=>e.selectedOptions[0].text'), 'TIJUANA')
        self.assertEqual(await self.page.locator('select#A14').evaluate('e=>e.selectedOptions[0].text'),
                         'PLAYAS DE TIJUANA #11739')
        self.assertEqual(await self.page.locator('select#A33').evaluate('e=>e.selectedOptions[0].text'), 'Tarjeta Credito')
        self.assertTrue(await bp_issue_proof(self.page, RECEIPT, PROFILE, EVIDENCE))
        await self.page.locator('input#A28').evaluate('e=>e.value="1234.57"')
        self.assertFalse(await bp_issue_proof(self.page, RECEIPT, PROFILE, EVIDENCE))

    async def test_duplicate_station_or_wrong_total_stops_before_issue(self):
        await self.reload_fixture(duplicate_station=True)
        with self.assertRaisesRegex(ValueError, 'única sucursal'):
            await prepare_bp_ticket(self.page, RECEIPT, PROFILE, EVIDENCE, BPProgress())
        self.assertFalse(await bp_issue_proof(self.page, RECEIPT, PROFILE, EVIDENCE))
        await self.reload_fixture(total='1234.57')
        with self.assertRaisesRegex(ValueError, 'total'):
            await self.prepare()
        self.assertFalse(await bp_issue_proof(self.page, RECEIPT, PROFILE, EVIDENCE))

    async def test_fiscal_mismatch_and_wrong_payment_cannot_pass_issue_gate(self):
        await self.prepare()
        await self.page.locator('input#A71').evaluate('e=>e.value="605 Sueldos"')
        self.assertFalse(await bp_issue_proof(self.page, RECEIPT, PROFILE, EVIDENCE))
        await self.page.locator('input#A71').evaluate('e=>e.value="612 Personas Fisicas"')
        await self.page.locator('select#A33').select_option(value='1')
        self.assertFalse(await bp_issue_proof(self.page, RECEIPT, PROFILE, EVIDENCE))

    async def test_ticket_and_amount_in_different_rows_cannot_pass_issue_gate(self):
        await self.prepare()
        await self.page.locator('#grid').evaluate(
            'e => e.innerHTML = "<tr><td>1234567</td><td>999.00</td></tr>" +'
            '"<tr><td>9999999</td><td>1234.56</td></tr>"')
        self.assertFalse(await bp_issue_proof(self.page, RECEIPT, PROFILE, EVIDENCE))

    async def test_two_cities_with_same_empty_list_do_not_block_search(self):
        await self.reload_fixture(same_empty=True)
        generic_receipt = RECEIPT.model_copy(update={'merchant': 'OTRO EMISOR'})
        self.assertEqual(await prepare_bp_ticket(self.page, generic_receipt, PROFILE,
                                                 EVIDENCE, BPProgress()), 'Sucursal 11739')
        self.assertFalse(await bp_issue_proof(self.page, generic_receipt, PROFILE, EVIDENCE))
