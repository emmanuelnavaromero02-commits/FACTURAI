"""Chrome regression for Tesoro's menu: opening a form is not issuing a CFDI."""

import asyncio
import json
import tempfile
from pathlib import Path
from unittest.mock import patch
from urllib.parse import urlsplit

from facturador.catalog import CATALOG
from facturador.db import Store
from facturador.models import Action, Target
from facturador.runner import Runner, action_risk
from test_core import PROFILE, RECEIPT


def click(role, name, risk='normal'):
    return Action(kind='click', target=Target(by='role', role=role, name=name, exact=True),
                  value=None, risk=risk, reason='Abrir el formulario del ticket')


def home(first_href):
    return ('<h5>Opciones de facturación</h5>'
            '<nav><a href="/GenerarCFDI/Index?avanzada=0">&#xf571; Generar factura de Estación y Znak</a></nav>'
            '<div><h3>Generar factura de Estación y Znak</h3><p>Facture su nota de venta</p>'
            f'<a href="{first_href}">Continuar... &#xf0a9;</a></div>'
            '<div><h3>Generar factura de Estación con múltiples tickets</h3>'
            '<a href="/GenerarCFDI/Index?avanzada=1">Continuar... &#xf0a9;</a></div>'
            '<div><h3>Descargar XML/PDF</h3><a href="/CFDI/Index">Continuar... &#xf0a9;</a></div>')


async def smoke():
    origin = 'https://tesoro.facturas.example'
    provider = {'name': 'Tesoro de prueba', 'url': origin+'/', 'hosts': ['tesoro.facturas.example']}
    with tempfile.TemporaryDirectory() as temp, patch.dict(CATALOG, {'tesoro': provider}):
        store = Store(Path(temp))
        runner = Runner(store)
        start = runner.start_browser
        contexts = set()
        first_href = '/GenerarCFDI/Index?avanzada=0'

        async def merchant(route):
            path = urlsplit(route.request.url).path
            if path == '/':
                body = home(first_href)
            elif path == '/GenerarCFDI/Index':
                body = '<h1>Generar CFDI</h1><label>RFC receptor<input></label><button>Facturar</button>'
            else:
                body = '<h1>Emisión no autorizada</h1>'
            await route.fulfill(content_type='text/html; charset=utf-8', body=body)

        async def open_browser():
            await start()
            assert runner.engine == 'Google Chrome'
            if runner.context not in contexts:
                contexts.add(runner.context)
                await runner.context.route(origin+'/**', merchant)

        def infer(_, observation, __):
            page = json.loads(observation)['estado_visible']
            if 'RFC receptor' in page:
                return click('button', 'Facturar', 'issue')
            return click('link', 'Continuar... \uf0a9')

        try:
            with patch.object(runner, 'start_browser', open_browser), patch('facturador.runner.AI.next_action', side_effect=infer):
                assert action_risk(click('link', 'Generar factura de Estación y Znak')) == 'issue'
                for index, destination in enumerate(('/GenerarCFDI/Index?avanzada=0',
                                                     '/GenerarCFDI/Emitir?avanzada=0')):
                    first_href = destination
                    key = store.add('fixture.png', f'tesoro-{index}')[0]['id']
                    receipt = RECEIPT.model_copy(update={'provider': 'tesoro', 'billing_codes': [f'CODIGO{index}']})
                    store.save_receipt(key, receipt, ready=True)
                    runner.schedule(key, PROFILE)
                    await asyncio.wait_for(runner.job_tasks[key], 15)
                    page = runner.viewer.page
                    assert page.url == (origin+'/GenerarCFDI/Index?avanzada=0' if index == 0 else origin+'/')
                    assert not store.get(key)['issue_attempted'] and not store.get(key)['human_exposed']
                    assert store.get(key)['status'] == 'blocked'
                    assert sum(event['kind'] == 'portal_entry' for event in store.events(key)) == (1 if index == 0 else 0)
                    if index == 0:
                        assert await page.get_by_label('RFC receptor').is_visible()
            print('OK Chrome: Tesoro abre sólo el formulario de un ticket y bloquea el enlace de emisión.')
        finally:
            await runner.close()


if __name__ == '__main__':
    asyncio.run(smoke())
