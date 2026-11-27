"""Medición desde la base: tickets viejos sin eventos nuevos y tickets nuevos con todo."""
from __future__ import annotations

import json
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from tempfile import TemporaryDirectory

from fastapi import FastAPI
from fastapi.responses import PlainTextResponse
from fastapi.testclient import TestClient

from facturador import medicion, uso_ia
from facturador.db import Store

START = datetime(2026, 11, 20, 15, 0, tzinfo=timezone.utc)


def at(minutes: float) -> str:
    return (START + timedelta(minutes=minutes)).isoformat()


class MeasurementTests(unittest.TestCase):
    def setUp(self):
        self.tmp = TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.store = Store(self.root)
        self.registro = uso_ia.RegistroUso(self.root)

    def job(self, key, status, *, provider='capufe', portal_url='', created=0, human_exposed=0, source='archivo: a.jpg',
            historical=0):
        receipt = {'provider': provider, 'portal_url': portal_url, 'merchant': 'Comercio ficticio', 'source': source}
        with self.store.connect() as db:
            db.execute('INSERT INTO jobs(id,receipt,status,created,updated,human_exposed,historical) VALUES(?,?,?,?,?,?,?)',
                       (key, json.dumps(receipt), status, at(created), at(created), human_exposed, historical))

    def event(self, key, minutes, kind, detail=''):
        if not isinstance(detail, str):
            detail = json.dumps(detail)
        with self.store.connect() as db:
            db.execute('INSERT INTO events(job_id,at,kind,detail) VALUES(?,?,?,?)', (key, at(minutes), kind, detail))

    def usage(self, key, *, tokens=(1000, 100)):
        self.registro.record(task='paso_portal', ticket=key, provider='anthropic', model='claude-opus-5-5',
                             effort='medium', duration_ms=900, input_tokens=tokens[0], output_tokens=tokens[1])

    def seed(self):
        # Old ticket: invoiced before read_timing existed, with the usual pauses.
        self.job('viejo', 'verified')
        self.event('viejo', 5, 'browser_opened')
        self.event('viejo', 6, 'needs_user', 'issue')
        self.event('viejo', 16, 'action', 'click · Facturar')
        self.event('viejo', 20, 'verified', '{}')
        # New ticket: read, portal session and invoice without any human pause.
        self.job('nuevo', 'verified', provider='portal_ticket', portal_url='https://facturas.example.org/emitir', created=1)
        self.event('nuevo', 1.5, 'read_timing', {'total_ms': 4000, 'ocr_ms': 500, 'model_ms': 3500, 'web_search_ms': 0,
                                                  'effort': 'low', 'escalated': False})
        self.event('nuevo', 2, 'browser_opened')
        self.event('nuevo', 2.5, 'action', 'fill · RFC')
        self.event('nuevo', 4, 'verified', '{}')
        self.usage('nuevo')
        self.usage('nuevo')
        # Engine ticket: session metrics and memory replay, but the person fixed a field.
        self.job('motor', 'verified', created=2)
        self.event('motor', 2.2, 'read_timing', {'total_ms': 6000})
        self.event('motor', 3, 'fuentes_de_campos', {'fields': {'amount': 'correccion_usuario'}, 'tools': ['correccion_usuario']})
        self.event('motor', 4, 'session_metrics', {'ai_calls': 3, 'actions': 10, 'replayed_actions': 8, 'seconds': 50,
                                                   'outcome': 'verified'})
        self.event('motor', 4.1, 'memory_replay', 'Se repitieron 8 acciones')
        # Failing ticket on the same catalog portal.
        self.job('falla', 'blocked', created=3)
        self.event('falla', 3.5, 'browser_opened')
        self.event('falla', 4, 'question', '¿Cuál es el folio?')
        self.event('falla', 9, 'answer', 'Respuesta recibida en el chat.')
        self.event('falla', 10, 'stopped', 'El portal no respondió.')
        # Not counted: companion image, historical import and a practice example.
        self.job('parte', 'attached', created=1)
        self.job('historico', 'verified', historical=1)
        self.job('ejemplo', 'ready', source='Ejemplo: ticket de práctica')
        # Uploaded, never read.
        self.job('nada', 'new', created=4)

    def test_old_tickets_have_no_data_instead_of_zero(self):
        self.seed()
        rows = {row['ticket']: row for row in medicion.tickets(self.store.path, self.root)}
        self.assertEqual(set(rows), {'viejo', 'nuevo', 'motor', 'falla', 'nada'})
        old = rows['viejo']
        self.assertIsNone(old['lectura_ms'])
        self.assertIsNone(old['llamadas_ia'])
        self.assertIsNone(old['acciones_memoria'])
        self.assertIsNone(old['costo'])
        self.assertEqual(old['costo_estado'], 'sin_dato')
        self.assertEqual((old['pausas'], old['pausas_s']), (1, 600.0))
        self.assertEqual((old['portal_s'], old['portal_fuente']), (300.0, 'eventos'))  # 15 min minus the 10 min pause.
        self.assertFalse(old['sin_intervencion'])
        untouched = rows['nada']
        self.assertEqual((untouched['pausas'], untouched['pausas_s'], untouched['intento_portal']), (0, 0, False))
        self.assertIsNone(untouched['portal_s'])

    def test_new_tickets_and_the_exact_definition_of_without_intervention(self):
        self.seed()
        rows = {row['ticket']: row for row in medicion.tickets(self.store.path, self.root)}
        new = rows['nuevo']
        self.assertEqual((new['lectura_ms'], new['portal_s'], new['llamadas_ia'], new['llamadas_fuente']),
                         (4000, 120.0, 2, 'registro'))
        self.assertEqual(new['portal'], 'https://facturas.example.org')
        self.assertEqual((new['costo'], new['costo_estado']), ('sin precio', 'sin_precio'))
        self.assertTrue(new['sin_intervencion'])
        engine = rows['motor']
        self.assertEqual((engine['portal_s'], engine['portal_fuente'], engine['llamadas_ia'], engine['acciones_memoria']),
                         (50, 'sesion', 3, 8))
        self.assertEqual(engine['correcciones'], 1)
        self.assertFalse(engine['sin_intervencion'])  # A manual correction counts.
        self.assertFalse(rows['falla']['sin_intervencion'])  # Not invoiced (and asked in the chat).
        self.assertEqual(rows['falla']['pausas_s'], 300.0)

        uso_ia.guardar_precios(self.root, {'moneda': 'USD', 'precios': {
            'anthropic': {'claude-opus-5-5': {'entrada': 4, 'salida': 20}}}})
        priced = medicion.tickets(self.store.path, self.root, key='nuevo')[0]
        self.assertEqual((priced['costo'], priced['costo_estado']), (0.012, 'ok'))  # 2 × (1000×4 + 100×20)/1e6

    def test_exposed_browser_or_pending_question_also_count_as_intervention(self):
        self.job('expuesto', 'verified', human_exposed=1)
        self.event('expuesto', 1, 'verified', '{}')
        self.job('pago', 'verified')
        self.event('pago', 1, 'payment_confirmed', 'Forma de pago SAT 04 confirmada por el usuario.')
        self.event('pago', 2, 'verified', '{}')
        self.job('limpio', 'verified')
        self.event('limpio', 1, 'fuentes_de_campos', {'fields': {'amount': 'ocr'}, 'tools': ['ocr']})
        self.event('limpio', 2, 'verified', '{}')
        rows = {row['ticket']: row['sin_intervencion'] for row in medicion.tickets(self.store.path, self.root)}
        self.assertEqual(rows, {'expuesto': False, 'pago': False, 'limpio': True})

    def test_summary_and_portals(self):
        self.seed()
        uso_ia.guardar_precios(self.root, {'moneda': 'USD', 'precios': {
            'anthropic': {'claude-opus-5-5': {'entrada': 4, 'salida': 20}}}})
        summary = medicion.resumen(medicion.tickets(self.store.path, self.root), 'todo')
        self.assertEqual((summary['subidos'], summary['facturados'], summary['sin_intervencion']), (5, 3, 1))
        self.assertEqual(summary['pct_sin_intervencion'], 33.3)
        self.assertEqual(summary['mediana_lectura_ms'], 5000)
        self.assertEqual(summary['mediana_portal_s'], 105.0)  # Median of 300, 120, 50 and 90 seconds.
        self.assertEqual(summary['promedio_llamadas_ia'], 2.5)  # Only tickets with data: 2 and 3.
        self.assertEqual(summary['costo_por_ticket'], 0.012)
        self.assertEqual(summary['portales_que_mas_fallan'], [{'portal': 'capufe', 'fallos': 1, 'intentos': 3}])
        portals = {p['portal']: p for p in medicion.portales(medicion.tickets(self.store.path, self.root))}
        self.assertEqual((portals['capufe']['intentos'], portals['capufe']['exitos'], portals['capufe']['pct_memoria']),
                         (3, 2, 80.0))
        self.assertIsNone(portals['https://facturas.example.org']['pct_memoria'])

    def test_empty_database_and_periods(self):
        summary = medicion.resumen(medicion.tickets(self.store.path, self.root), 'hoy')
        self.assertEqual((summary['subidos'], summary['pct_sin_intervencion'], summary['mediana_lectura_ms'],
                          summary['costo_por_ticket']), (0, None, None, None))
        now = datetime(2026, 11, 27, 18, 0, tzinfo=timezone.utc)
        self.assertEqual(medicion.period_start('todo'), '')
        self.assertTrue(medicion.period_start('7d', now=now).startswith('2026-11-20T18:00'))
        self.assertLessEqual(medicion.period_start('hoy', now=now), now.isoformat())
        with self.assertRaises(ValueError):
            medicion.period_start('ayer')

    def test_routes_carry_no_personal_data(self):
        self.seed()
        app = FastAPI()

        @app.exception_handler(ValueError)
        async def invalid(request, exc):
            return PlainTextResponse(str(exc), status_code=422)

        @app.exception_handler(KeyError)
        async def missing(request, exc):
            return PlainTextResponse('Registro inexistente', status_code=404)
        app.include_router(medicion.medicion_router(self.store))
        client = TestClient(app)
        summary = client.get('/api/medicion/resumen?periodo=todo')
        self.assertEqual(summary.json()['subidos'], 5)
        self.assertEqual(client.get('/api/medicion/resumen?periodo=ayer').status_code, 422)
        portals = client.get('/api/medicion/portales?periodo=todo').json()
        self.assertEqual({p['portal'] for p in portals['portales']}, {'capufe', 'https://facturas.example.org'})
        one = client.get('/api/medicion/tickets/nuevo').json()
        self.assertTrue(one['sin_intervencion'])
        self.assertEqual(client.get('/api/medicion/tickets/no-existe').status_code, 404)
        for text in (summary.text, json.dumps(portals), json.dumps(one)):
            self.assertNotIn('Comercio ficticio', text)
            self.assertNotIn('folio', text)


if __name__ == '__main__':
    unittest.main()
