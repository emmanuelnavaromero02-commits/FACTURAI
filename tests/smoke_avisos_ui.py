"""Ejercita en Chromium la campana, los ajustes de avisos, los permisos, los datos para registros y el
service worker, sin servicio de avisos real: el push se entrega por el protocolo de depuración del navegador."""

import asyncio
import hashlib
import json
import os
import socket
import tempfile
from pathlib import Path

import uvicorn
from PIL import Image
from playwright.async_api import async_playwright, expect

from facturador.app import BASE, create_app
from facturador.avisos import Avisos
from facturador.avisos_push import PushDevices
from facturador.avisos_rutas import avisos_router, piezas_router
from facturador.models import Receipt
from facturador.permisos import permitido
from facturador.permisos_rutas import permisos_router
from facturador.registro_datos import datos_de_registro, registro_router


def seed_ticket(store, root):
    source = root / 'originales' / 'ticket.png'
    source.parent.mkdir(parents=True, exist_ok=True)
    Image.new('RGB', (20, 20), (200, 120, 40)).save(source)
    row, _ = store.add(str(source), hashlib.sha256(source.read_bytes()).hexdigest())
    store.save_receipt(row['id'], Receipt(provider='samara', merchant='Cafetería Luna', amount='100.00',
                                          date='2026-11-13', ticket_number='A-100', payment_code='04'), ready=True)
    return row['id']


async def smoke():
    with tempfile.TemporaryDirectory() as folder, socket.socket() as sock:
        sock.bind(('127.0.0.1', 0))
        port = sock.getsockname()[1]
        root = Path(folder)
        app = create_app(root, port=port)
        store = app.state.store
        sent = []
        avisos = Avisos(store, push=PushDevices(store, send=lambda **kw: sent.append(kw)),
                        panel_url=f'http://127.0.0.1:{port}')
        # The same wiring the coordinator adds to app.py.
        app.include_router(avisos_router(store, avisos))
        app.include_router(permisos_router(store))
        app.include_router(registro_router(root))
        app.include_router(piezas_router(BASE / 'web'))
        key = seed_ticket(store, root)
        avisos.avisar('te_necesito', clave='uno', job_id=key, comercio='Cafetería Luna',
                      motivo='Resuelve la verificación «no soy un robot»')
        server = uvicorn.Server(uvicorn.Config(app, log_level='warning', access_log=False))
        serving = asyncio.create_task(server.serve(sockets=[sock]))
        origin = f'http://127.0.0.1:{port}'
        try:
            async with asyncio.timeout(40):
                while not server.started:
                    if serving.done():
                        await serving
                        raise AssertionError('No arrancó FastAPI.')
                    await asyncio.sleep(.05)
            async with async_playwright() as pw:
                browser = await pw.chromium.launch(executable_path=os.getenv('FACTURADOR_CHROMIUM_PATH'))
                context = await browser.new_context(viewport={'width': 1200, 'height': 900})
                await context.grant_permissions(['notifications'], origin=origin)
                ui = await context.new_page()
                problems = []
                ui.on('console', lambda m: problems.append(m.text) if m.type == 'error' else None)
                ui.on('pageerror', lambda e: problems.append(str(e)))
                await ui.goto(origin + '/#bandeja')
                await ui.add_script_tag(url='/avisos.js', type='module')
                bell = ui.get_by_role('button', name='Avisos: 1 sin leer')
                await expect(bell).to_be_visible(timeout=10_000)
                await bell.click()
                dialog = ui.locator('#avisos-dialog')
                await expect(dialog).to_be_visible()
                await expect(dialog.get_by_text('Cafetería Luna: Resuelve la verificación «no soy un robot»')).to_be_visible()
                await dialog.get_by_role('link').first.click()
                await expect(dialog).to_be_hidden()
                row = ui.locator(f'#tickets tr[data-job-id="{key}"]')
                await expect(row).to_have_class('aviso-destacado', timeout=10_000)
                await expect(ui.get_by_role('button', name='Avisos', exact=True)).to_be_visible()
                assert avisos.sin_leer() == 0

                await ui.goto(origin + '/#ajustes')
                await ui.add_script_tag(url='/avisos.js', type='module')
                cards = ui.locator('#screen-ajustes')
                await expect(cards.get_by_role('heading', name='Avisos', exact=True)).to_be_visible(timeout=10_000)
                await expect(cards.get_by_role('heading', name='Permisos')).to_be_visible()
                await expect(cards.get_by_role('heading', name='Datos para registros')).to_be_visible()
                await expect(ui.get_by_role('button', name='Activar avisos en este dispositivo')).to_be_visible()
                await expect(ui.locator('.avisos-ayuda')).to_be_hidden()
                await ui.get_by_label('Cuando una factura queda comprobada').uncheck()
                await expect(ui.get_by_label('Cuando una factura queda comprobada')).not_to_be_checked()
                for _ in range(50):
                    if (root / 'avisos.json').is_file():
                        break
                    await asyncio.sleep(.1)
                assert json.loads((root / 'avisos.json').read_text(encoding='utf-8'))['tipos']['factura_lista'] is False
                await ui.get_by_label('Pedir la factura por correo al comercio sin confirmarlo en cada ticket.').check()
                await expect(cards.get_by_text('Activo desde')).to_be_visible()
                assert permitido(root, 'pedir_por_correo')
                await ui.locator('input[name=telefono]').fill('123')
                await ui.get_by_role('button', name='Guardar datos para registros').click()
                await expect(cards.get_by_text('Revisa el teléfono.')).to_be_visible()
                await ui.locator('input[name=telefono]').fill('55 1234 5678')
                await ui.locator('input[name=nombres]').fill('María')
                await ui.get_by_role('button', name='Guardar datos para registros').click()
                await expect(cards.get_by_text('Datos para registros guardados.')).to_be_visible()
                assert datos_de_registro(root)['telefono'] == '5512345678'

                # Service worker: registered at the root; a push shows the notification without any cache.
                cdp = await context.new_cdp_session(ui)
                registrations = []
                cdp.on('ServiceWorker.workerRegistrationUpdated', lambda e: registrations.extend(e['registrations']))
                await cdp.send('ServiceWorker.enable')
                scope = await ui.evaluate("""async () => {
                    const r = await navigator.serviceWorker.register('/sw.js', {scope: '/'});
                    await navigator.serviceWorker.ready; return r.scope; }""")
                assert scope == origin + '/', scope
                for _ in range(50):
                    if registrations:
                        break
                    await asyncio.sleep(.1)
                registration = next(r for r in registrations if r['scopeURL'] == origin + '/')
                await cdp.send('ServiceWorker.deliverPushMessage', {
                    'origin': origin + '/', 'registrationId': registration['registrationId'],
                    'data': json.dumps({'titulo': 'Factura lista', 'cuerpo': 'Cafetería Luna: lista',
                                        'liga': f'/?ticket={key}#bandeja', 'etiqueta': key})})
                shown = []
                for _ in range(50):
                    shown = await ui.evaluate("""async () => (await (await navigator.serviceWorker.ready)
                        .getNotifications()).map(n => [n.title, n.body, n.data.liga])""")
                    if shown:
                        break
                    await asyncio.sleep(.1)
                assert shown == [['Factura lista', 'Cafetería Luna: lista', f'{origin}/?ticket={key}#bandeja']], shown
                assert await ui.evaluate('caches.keys().then(k => k.length)') == 0

                # A notice link opened from outside lands on the ticket and the address is cleaned.
                await ui.goto(origin + f'/?ticket={key}#bandeja')
                await ui.add_script_tag(url='/avisos.js', type='module')
                await expect(ui.locator(f'#tickets tr[data-job-id="{key}"]')).to_have_class('aviso-destacado', timeout=10_000)
                assert ui.url == origin + '/#bandeja', ui.url
                csp = [p for p in problems if 'Content Security Policy' in p or 'avisos' in p.lower()]
                assert not csp, csp
                await browser.close()
        finally:
            server.should_exit = True
            await serving
    print('OK: campana, liga al ticket, ajustes de avisos, permisos, datos para registros y service worker sin caché.')


if __name__ == '__main__':
    asyncio.run(smoke())
