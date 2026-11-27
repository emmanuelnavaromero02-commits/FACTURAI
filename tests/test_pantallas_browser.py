"""New settings and ticket screens in a real browser, at phone (390 px) and desktop (1280 px) widths.

The real application runs with fictitious data. Mail goes to in-process IMAP/SMTP doubles, the AI key is
checked against a simulated transport and WhatsApp answers come from intercepted routes: nothing leaves
the machine.
"""
import asyncio
import io
import json
import re
import tempfile
import threading
import time
import unittest
from pathlib import Path
from unittest.mock import patch

import httpx
import uvicorn
from PIL import Image
from playwright.async_api import async_playwright, expect

from facturador import app as panel, buzon
from facturador.ingest import save_input
from facturador.models import Profile, Receipt
from facturador.proveedores_ia import SelectorIA
from tests.correo_dobles import FakeImap, FakeSmtp, parsed, trusted_context
from tests.test_panel_browser import EXECUTABLE, free_port

WIDTHS = (390, 1280)
PASSWORD = 'clave-app-ficticia'
AI_KEY = 'sk-ant-api03-PRUEBA-ficticia-0000000000000000wxyz'
PROFILE = Profile(rfc='AAA010101AAA', name='PERSONA DE PRUEBA', postal_code='01000', regime='612', cfdi_use='G03',
                  email='persona@example.test')
MERCHANT_MAIL = 'facturas@comercio.example'
MERCHANT_PHONE = '+525512345678'


def picture() -> bytes:
    output = io.BytesIO()
    Image.new('RGB', (60, 90), 'white').save(output, 'PNG')
    return output.getvalue()


def anthropic_models(request: httpx.Request) -> httpx.Response:
    if request.url.path.endswith('/v1/models') and request.headers.get('x-api-key') == AI_KEY:
        return httpx.Response(200, json={'data': [{'id': 'claude-modelo-a', 'display_name': 'Modelo A'},
                                                  {'id': 'claude-modelo-b', 'display_name': 'Modelo B'}],
                                         'has_more': False})
    return httpx.Response(401, json={'type': 'error', 'error': {'type': 'authentication_error', 'message': 'no'}})


@unittest.skipUnless(EXECUTABLE, 'Requiere Google Chrome o Chromium')
class NewScreensInBrowser(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.playwright = await async_playwright().start()
        self.addAsyncCleanup(self.playwright.stop)
        self.browser = await self.playwright.chromium.launch(executable_path=EXECUTABLE, headless=True)
        self.addAsyncCleanup(self.browser.close)
        for target in [patch('facturador.buzon.tls_context', trusted_context),
                       patch.object(panel, 'SelectorIA', lambda root, port: SelectorIA(
                           root, port, client=httpx.Client(transport=httpx.MockTransport(anthropic_models)), environ={}))]:
            target.start()
            self.addCleanup(target.stop)

    async def start_app(self):
        folder = tempfile.TemporaryDirectory()
        self.addCleanup(folder.cleanup)
        root = Path(folder.name)
        port = free_port()
        app = panel.create_app(root, port=port)
        for name in ('automation', 'whatsapp'):
            quiet = patch.object(getattr(app.state, name) if name == 'automation' else app.state.whatsapp.watcher,
                                 'start', lambda: None)
            quiet.start()
            self.addCleanup(quiet.stop)
        server = uvicorn.Server(uvicorn.Config(app, host='127.0.0.1', port=port, log_level='warning'))
        thread = threading.Thread(target=server.run, daemon=True)
        thread.start()
        deadline = time.monotonic() + 15
        while not server.started and time.monotonic() < deadline:
            await asyncio.sleep(.05)
        self.assertTrue(server.started)

        async def stop():
            server.should_exit = True
            await asyncio.to_thread(thread.join, 15)
        self.addAsyncCleanup(stop)
        return app, root, f'http://127.0.0.1:{port}'

    def add_ticket(self, app, root, **receipt):
        (root / 'profile.json').write_text(PROFILE.model_dump_json(), encoding='utf-8')
        item = save_input(app.state.store, 'ticket.png', picture(), 'Prueba')
        data = {'provider': 'solicitud_directa', 'merchant': 'Comercio de prueba', 'amount': '120.50', 'date': '2026-11-20',
                'ticket_number': 'F-0001', 'payment_code': '04', 'merchant_email': MERCHANT_MAIL,
                'merchant_whatsapp': MERCHANT_PHONE, **receipt}
        app.state.store.save_receipt(item['id'], Receipt(source=app.state.store.get(item['id'])['receipt']['source'], **data),
                                     ready=True)
        return item['id']

    async def page_at(self, width):
        context = await self.browser.new_context(viewport={'width': width, 'height': 900}, service_workers='block')
        self.addAsyncCleanup(context.close)
        page = await context.new_page()
        errors = []
        page.on('pageerror', lambda error: errors.append(str(error)))
        self.addCleanup(lambda: self.assertEqual(errors, []))
        return page

    async def assert_fits(self, page):
        overflow = await page.evaluate('document.documentElement.scrollWidth - window.innerWidth')
        self.assertLessEqual(overflow, 0)

    async def test_mail_account_connects_asks_merchant_and_disconnects(self):
        for width in WIDTHS:
            with self.subTest(width=width):
                app, root, base = await self.start_app()
                key = self.add_ticket(app, root)
                imap, smtp = FakeImap(password=PASSWORD), FakeSmtp('ssl', password=PASSWORD)
                self.addCleanup(imap.close)
                self.addCleanup(smtp.close)
                page = await self.page_at(width)
                await page.goto(base + '/#ajustes')
                card = page.locator('#mail-card')
                await expect(card.get_by_text('Por conectar')).to_be_visible()
                await card.get_by_label('Usuario de la cuenta').fill(imap.user)
                await card.get_by_label('Contraseña de aplicación').fill(PASSWORD)
                await card.get_by_label('Servidor de entrada (IMAP)').fill('127.0.0.1')
                await card.get_by_label('Puerto de entrada').fill(str(imap.port))
                await card.get_by_label('Servidor de salida (SMTP)').fill('127.0.0.1')
                await card.get_by_label('Puerto de salida').fill(str(smtp.port))
                await card.get_by_role('button', name='Conectar correo').click()
                await expect(page.locator('#mail-feedback')).to_have_text('Correo conectado. La cuenta respondió bien.')
                await expect(page.locator('#mail-pill')).to_have_text('Conectado')
                await expect(page.locator('#mail-summary')).to_contain_text('puede pedir facturas por correo')
                self.assertEqual(await page.locator('#mail-form [name=password]').input_value(), '')
                self.assertNotIn(PASSWORD, await page.content())
                self.assertNotIn(PASSWORD, json.dumps(await page.evaluate(
                    '[...document.querySelectorAll("input")].map(input => input.value)')))
                await page.get_by_role('button', name='Probar conexión').click()
                await expect(page.locator('#mail-feedback')).to_have_text('La conexión con tu correo funciona.')
                await self.assert_fits(page)

                # Ask the merchant from the conversation: the address comes from the ticket.
                await page.goto(base + '/#chat')
                await page.get_by_role('button', name='Pedir por correo').click()
                dialog = page.locator('#mail-job-dialog')
                await expect(dialog.get_by_text('Sin solicitud enviada')).to_be_visible()
                await expect(dialog.get_by_label('Correo del comercio para facturar')).to_have_value(MERCHANT_MAIL)
                await expect(dialog.get_by_text('Lo tomé del ticket.')).to_be_visible()
                await self.assert_fits(page)
                await dialog.get_by_role('button', name='Pedir la factura por correo').click()
                self.assertEqual(smtp.received, [])  # The consent box is required.
                await dialog.get_by_label('Autorizo enviar a esta dirección').check()
                await dialog.get_by_role('button', name='Pedir la factura por correo').click()
                await expect(page.locator('#mail-job-state')).to_have_text('Solicitud enviada')
                await expect(page.locator('#mail-job-record')).to_contain_text('Enviada a ' + MERCHANT_MAIL)
                self.assertEqual([item['to'] for item in smtp.received], [[MERCHANT_MAIL]])
                self.assertIn('F-0001', parsed(smtp.received[0])['Subject'])
                self.assertEqual(app.state.store.get(key)['issue_attempted'], 1)
                await expect(dialog.get_by_role('button', name='Buscar la factura en mi correo')).to_be_visible()
                await dialog.get_by_role('button', name='Buscar la factura en mi correo').click()
                await expect(page.locator('#mail-job-state')).to_have_text('Aún no llega')
                await dialog.get_by_role('button', name='Cerrar solicitud por correo').click()

                await page.goto(base + '/#ajustes')
                await expect(page.locator('#mail-pill')).to_have_text('Conectado')
                page.once('dialog', lambda prompt: asyncio.ensure_future(prompt.accept()))
                await page.locator('#mail-disconnect').click()
                await expect(page.locator('#mail-feedback')).to_have_text('Correo desconectado.')
                await expect(page.locator('#mail-pill')).to_have_text('Por conectar')
                await expect(page.locator('#mail-form')).to_be_visible()
                self.assertIsNone(buzon.load_account(root))

    async def test_whatsapp_link_state_number_from_ticket_and_reply_switch(self):
        for width in WIDTHS:
            with self.subTest(width=width):
                app, root, base = await self.start_app()
                self.add_ticket(app, root)
                page = await self.page_at(width)
                checked = {'state': 'listo', 'message': 'WhatsApp está vinculado y listo.', 'ready': True,
                           'checked': '2026-11-27T10:00:00+00:00'}
                await page.route('**/api/whatsapp/link/check', lambda route: route.fulfill(json=checked))
                status = {'status': 'new', 'phone': '', 'reference': '', 'message': '', 'updated': '', 'count': 0,
                          'completable': False, 'watch': False, 'reply': False, 'conversation': 'off',
                          'conversation_message': '', 'replies': 0, 'attention': False}
                puts = []

                async def whatsapp(route):
                    request = route.request
                    if request.method == 'PUT' and request.url.endswith('/whatsapp/reply'):
                        puts.append(json.loads(request.post_data))
                        status.update(reply=True, conversation='needs_person', attention=True, replies=2,
                                      conversation_message='El comercio pidió un dato que no tengo.')
                    await route.fulfill(json=status)
                await page.route('**/api/jobs/*/whatsapp', whatsapp)
                await page.route('**/api/jobs/*/whatsapp/reply', whatsapp)

                await page.goto(base + '/#ajustes')
                await expect(page.locator('#wa-link-pill')).to_have_text('Sin abrir')
                await expect(page.locator('#wa-link-message')).to_have_text('WhatsApp no está abierto en el navegador.')
                await expect(page.locator('#wa-photos')).to_contain_text('Mis fotos: apagado')
                await page.get_by_role('button', name='Comprobar', exact=True).click()
                await expect(page.locator('#wa-link-pill')).to_have_text('Vinculado')
                await expect(page.locator('#wa-link-message')).to_have_text('WhatsApp está vinculado y listo.')
                await self.assert_fits(page)

                await page.goto(base + '/#chat')
                await page.get_by_role('button', name='Pedir por WhatsApp').click()
                dialog = page.locator('#wa-job-dialog')
                phone = dialog.get_by_label('Número de WhatsApp del comercio')
                await expect(phone).to_have_value(MERCHANT_PHONE)
                self.assertFalse(await phone.evaluate('input => input.required'))
                await expect(dialog.get_by_text('Lo tomé del ticket.')).to_be_visible()
                await expect(dialog.locator('#wa-job-link-state')).to_contain_text('WhatsApp Web:')
                await expect(dialog.locator('#wa-job-form').get_by_label('Contestar al comercio por mí')).not_to_be_checked()
                await self.assert_fits(page)

                status.update(status='uncertain', phone=MERCHANT_PHONE, reference='FACTURAI-PRUEBA', completable=True,
                              message='El envío pudo haber llegado.')
                await dialog.get_by_role('button', name='Actualizar estado').click()
                switch = dialog.locator('#wa-job-reply-row').get_by_label('Contestar al comercio por mí')
                await expect(switch).to_be_visible()
                await switch.check()
                await expect(dialog.get_by_role('alert')).to_contain_text('El comercio pidió un dato que no tengo.')
                self.assertEqual(puts, [{'reply': True}])
                await expect(dialog.locator('#wa-job-conversation')).to_contain_text('Necesita tu ayuda')
                await expect(dialog.locator('#wa-job-conversation')).to_contain_text('2 respuestas enviadas')
                await expect(switch).to_be_checked()
                await expect(dialog.get_by_role('button', name='Completar envío')).to_be_visible()
                await self.assert_fits(page)

    async def test_ai_key_saved_against_simulated_transport_and_provider_chosen(self):
        for width in WIDTHS:
            with self.subTest(width=width):
                app, root, base = await self.start_app()
                page = await self.page_at(width)
                await page.goto(base + '/#ajustes')
                card = page.locator('#ia-card')
                await expect(card.locator('#ia-pill')).to_have_text('Plan de ChatGPT')
                claude = card.get_by_role('radio', name='Claude de Anthropic (llave de API)')
                await expect(claude).to_be_disabled()
                block = card.get_by_role('region', name='Claude de Anthropic (llave de API)')
                await block.get_by_label('Llave de API').fill('sk-ant-equivocada-0000000000000000')
                await block.get_by_role('button', name='Guardar y probar').click()
                await expect(page.locator('#ia-feedback')).to_have_class('error')
                await block.get_by_label('Llave de API').fill(AI_KEY)
                await block.get_by_role('button', name='Guardar y probar').click()
                await expect(page.locator('#ia-feedback')).to_have_text('Llave guardada y probada. Termina en ····wxyz.')
                await expect(card.get_by_text('Llave guardada que termina en ····wxyz.')).to_be_visible()
                self.assertNotIn(AI_KEY, await page.content())
                self.assertNotIn(AI_KEY, json.dumps(await page.evaluate(
                    '[...document.querySelectorAll("input")].map(input => input.value)')))
                await expect(claude).to_be_enabled()
                await claude.check()
                await expect(card.locator('#ia-pill')).to_have_text('Claude de Anthropic (llave de API)')
                self.assertEqual(app.state.plan.settings.active(), 'anthropic')
                await expect(page.locator('#plan-message')).to_contain_text('Ahora uso Claude')

                # Model per tier and a price row.
                await block.get_by_text('Modelo por nivel').click()
                fast = block.get_by_label('Rápido · pasos sencillos')
                await expect(fast).to_be_visible()
                await fast.select_option('claude-modelo-b')
                await block.get_by_role('button', name='Guardar modelos').click()
                await expect(page.locator('#ia-feedback')).to_have_text('Modelos guardados.')
                self.assertEqual(app.state.plan.providers['anthropic'].tier_models()['rapido'], 'claude-modelo-b')
                await card.get_by_text('Precios por millón de tokens').click()
                await card.get_by_role('button', name='Agregar modelo').click()
                row = card.locator('.ia-price-row').last
                await row.get_by_label('IA').select_option('anthropic')
                await row.get_by_label('Modelo').fill('claude-modelo-b')
                await row.get_by_label('Entrada').fill('1.5')
                await row.get_by_label('Salida').fill('7')
                await card.get_by_role('button', name='Guardar precios').click()
                await expect(page.locator('#ia-feedback')).to_have_text('Precios guardados.')
                saved = json.loads((root / 'precios_ia.json').read_text(encoding='utf-8'))
                self.assertEqual(saved['precios']['anthropic']['claude-modelo-b'], {'entrada': 1.5, 'salida': 7.0})
                await self.assert_fits(page)

    async def test_measurement_with_data_and_without(self):
        for width in WIDTHS:
            with self.subTest(width=width):
                app, root, base = await self.start_app()
                page = await self.page_at(width)
                await page.goto(base + '/#medicion')
                summary = page.locator('#medicion-resumen')
                tile = lambda label: summary.locator('.kpi').filter(has=page.get_by_text(label, exact=True)).locator('.kpi-value')
                await expect(tile('Tickets subidos')).to_have_text('0')
                await expect(tile('Lectura del ticket')).to_have_text('sin dato')
                await expect(tile('Costo por ticket')).to_have_text('sin dato')
                await expect(tile('Sin intervención')).to_have_text('sin dato')
                await expect(page.locator('#medicion-portales')).to_contain_text('Ningún ticket llegó a un portal')
                await self.assert_fits(page)

                key = self.add_ticket(app, root, provider='portal_ticket', portal_url='https://portal.ejemplo.mx/facturar')
                store = app.state.store
                store.event(key, 'read_timing', json.dumps({'total_ms': 4200}))
                store.event(key, 'browser_opened', 'Portal')
                store.event(key, 'session_metrics', json.dumps({'seconds': 75, 'ai_calls': 3, 'actions': 10,
                                                                'replayed_actions': 4}))
                store.update(key, status='verified')
                app.state.plan.usage.record(task='leer_ticket', ticket=key, provider='anthropic', model='claude-sin-precio',
                                            effort='low', duration_ms=900, input_tokens=1000, output_tokens=100)
                await page.get_by_role('button', name='Todo').click()
                await expect(tile('Tickets subidos')).to_have_text('1')
                await expect(tile('Facturados')).to_have_text('1')
                await expect(tile('Sin intervención')).to_have_text('100 %')
                await expect(tile('Lectura del ticket')).to_have_text('4.2 s')
                await expect(tile('Tiempo en el portal')).to_have_text('1 min 15 s')
                await expect(tile('Llamadas a la IA por ticket')).to_have_text('1')
                await expect(tile('Costo por ticket')).to_have_text('sin precio')
                table = page.locator('#medicion-portales table')
                row = table.locator('tbody tr')
                await expect(row).to_have_count(1)
                await expect(row.locator('td')).to_have_text(['1', '1', '1', '100 %', '40 %'])
                self.assertEqual(await page.get_by_role('button', name='Todo').get_attribute('aria-pressed'), 'true')
                await self.assert_fits(page)

                await page.goto(base + '/#bandeja')
                await page.locator('#tickets summary[aria-label="Más acciones para Comercio de prueba"]').click()
                await page.get_by_role('button', name='Ver medición').click()
                detail = page.locator('#medicion-dialog')
                await expect(detail.get_by_text('Repetidas de memoria')).to_be_visible()
                await expect(detail.locator('dd').filter(has_text='sin precio')).to_have_count(1)
                await self.assert_fits(page)

    async def test_question_pause_offers_answered_and_new_event_labels(self):
        question = '¿El ticket dice «Sucursal 2» o «Sucursal 3»?'
        for width in WIDTHS:
            with self.subTest(width=width):
                app, root, base = await self.start_app()
                key = self.add_ticket(app, root, provider='portal_ticket', portal_url='https://portal.ejemplo.mx/facturar')
                store = app.state.store
                for kind, detail in [('browser_opened', 'Portal'), ('memory_replay', 'Pantalla de datos'),
                                     ('memory_learned', 'Portal'), ('memory_disabled', 'Una pantalla aprendida falló.'),
                                     ('limit_reached', 'pasos'), ('memory_miss', 'sin receta'),
                                     ('session_metrics', json.dumps({'seconds': 5})), ('read_timing', '{}'),
                                     ('question_waiting', question)]:
                    store.event(key, kind, detail)
                store.update(key, status='needs_user', message='Necesito tu respuesta para seguir: ' + question)
                page = await self.page_at(width)
                decisions = []

                # The engine's pause exists only while it runs: the state says this ticket is the live one.
                async def live_state(route):
                    response = await route.fetch(headers={**route.request.headers, 'if-none-match': ''})
                    data = await response.json()
                    data['active_job'] = key
                    for row in data['jobs']:
                        if row['id'] == key:
                            row.update(human_kind='unknown', retry_action='resume')
                    await route.fulfill(json=data)

                async def decision(route):
                    decisions.append(json.loads(route.request.post_data))
                    await route.fulfill(json={'ok': True})
                await page.route('**/api/state', live_state)
                await page.route('**/api/jobs/*/decision', decision)

                await page.goto(base + '/#bandeja')
                tickets = page.locator('#tickets')
                await expect(tickets.get_by_role('button', name='Ya contesté')).to_be_visible()
                await expect(tickets.get_by_role('button', name='Retomar trámite')).to_have_count(0)
                await tickets.get_by_role('button', name='Contestar en el chat').click()
                await expect(page).to_have_url(re.compile('#chat$'))
                await self.assert_fits(page)
                answered = page.locator('#chat-progress-actions').get_by_role('button', name='Ya contesté')
                await expect(answered).to_be_visible()
                await answered.click()
                await expect(page.locator('#notice')).to_have_text('Retomé el trámite con tu respuesta.')
                self.assertEqual(decisions, [{'decision': 'resume'}])
                await expect(page).to_have_url(re.compile('#chat$'))

                await page.goto(base + '/#bandeja')
                await page.locator('#tickets summary[aria-label="Más acciones para Comercio de prueba"]').click()
                await page.locator('#tickets').get_by_role('button', name='Ver actividad').click()
                activity = page.locator('#logs')
                for label in ['Repitió una pantalla que ya conocía', 'Aprendió este portal',
                              'Dejó de repetir una pantalla que falló', 'Se agotó lo previsto para este intento',
                              'Espera tu respuesta']:
                    await expect(activity.get_by_text(label, exact=True)).to_be_visible()
                await expect(activity.locator('.log-item').filter(has_text='Espera tu respuesta')).to_contain_text(question)
                text = await activity.inner_text()
                for hidden in ['memory miss', 'session metrics', 'read timing', 'memory_miss', 'session_metrics']:
                    self.assertNotIn(hidden, text)
                await self.assert_fits(page)

    async def test_review_saves_merchant_contact_fields(self):
        for width in WIDTHS:
            with self.subTest(width=width):
                app, root, base = await self.start_app()
                key = self.add_ticket(app, root, provider='portal_ticket', portal_url='https://portal.ejemplo.mx/facturar',
                                      merchant_email='', merchant_whatsapp='')
                store = app.state.store
                with store.connect() as db:
                    db.execute("UPDATE jobs SET status='review' WHERE id=?", (key,))
                page = await self.page_at(width)
                await page.goto(base + '/#bandeja')
                await page.locator('#tickets').get_by_role('button', name=re.compile('^Revisar')).first.click()
                editor = page.locator('#editor')
                await expect(editor.get_by_label('RFC del comercio')).to_have_value('')
                await editor.get_by_label('RFC del comercio').fill('mal')
                await editor.get_by_role('button', name='Guardar revisión').click()
                await expect(editor).to_be_visible()  # The browser refuses an RFC with the wrong shape.
                await editor.get_by_label('RFC del comercio').fill('CCC010101AAA')
                await editor.get_by_label('WhatsApp para facturar').fill('+52 55 8765 4321')
                await editor.get_by_label('Correo para facturar').fill('cfdi@comercio.example')
                await self.assert_fits(page)
                await editor.get_by_role('button', name='Guardar revisión').click()
                await expect(editor).to_be_hidden()
                saved = store.get(key)['receipt']
                self.assertEqual((saved['issuer_rfc'], saved['merchant_whatsapp'], saved['merchant_email']),
                                 ('CCC010101AAA', '+525587654321', 'cfdi@comercio.example'))


if __name__ == '__main__':
    unittest.main()
