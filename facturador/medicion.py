"""Cifras del facturador calculadas desde la base: por ticket, por periodo y por portal.

Sin datos personales: no devuelve RFC, nombres, importes ni comercios por nombre; el portal se
identifica por su clave de catálogo o por el origen HTTPS del portal propio del ticket.
Un dato que la base no tiene sale como `None` («sin dato»), nunca como cero.

Definición de «sin intervención» (`sin_intervencion`): el ticket terminó con la factura comprobada
(estado `verified`) y en toda su historia no hubo ninguna pausa humana (eventos `needs_user`, que
incluyen aprobar la emisión, CAPTCHA, acceso y modo manual, ni `question` al chat), ni toma de
control del navegador (`human_control_taken` o la marca `human_exposed`), ni corrección manual
(`payment_confirmed` o `fuentes_de_campos` con la herramienta `correccion_usuario`).
"""
from __future__ import annotations

import json
import sqlite3
import statistics
from datetime import datetime, timedelta, timezone
from pathlib import Path

from fastapi import APIRouter

from .catalog import CUSTOM_PORTAL, public_portal_origin
from .uso_ia import SIN_PRECIO, costo, leer_precios

PERIODOS = {'hoy', '7d', '30d', 'todo'}
PAUSES = {'needs_user', 'question'}
CORRECTIONS = {'payment_confirmed', 'human_control_taken'}
SESSION_START = 'browser_opened'
SESSION_END = {'verified', 'stopped', 'browser_closed', 'session_retry', 'browser_reopened', 'browser_opened'}
MEMORY = {'memory_replay', 'memory_learned', 'memory_miss'}
SIN_DATO = None


def period_start(periodo: str, *, now: datetime | None = None) -> str:
    """UTC ISO lower bound of a period ('' for everything). «hoy» starts at local midnight."""
    if periodo not in PERIODOS:
        raise ValueError('El periodo debe ser hoy, 7d, 30d o todo.')
    if periodo == 'todo':
        return ''
    current = (now or datetime.now(timezone.utc)).astimezone()
    if periodo == 'hoy':
        start = current.replace(hour=0, minute=0, second=0, microsecond=0)
    else:
        start = current - timedelta(days=7 if periodo == '7d' else 30)
    return start.astimezone(timezone.utc).isoformat()


def _when(value: str) -> datetime | None:
    try:
        parsed = datetime.fromisoformat(value)
    except (TypeError, ValueError):
        return None
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=timezone.utc)


def _json(detail: str):
    try:
        value = json.loads(detail)
    except (TypeError, ValueError):
        return None
    return value if isinstance(value, dict) else None


def _number(value):
    return value if isinstance(value, (int, float)) and not isinstance(value, bool) and value >= 0 else None


def portal_of(receipt: dict) -> str:
    """Portal identity without personal data: catalog key, or the HTTPS origin of the ticket's own portal."""
    provider = str(receipt.get('provider') or 'unknown')
    if provider == CUSTOM_PORTAL:
        return public_portal_origin(str(receipt.get('portal_url') or '')) or CUSTOM_PORTAL
    return provider[:80]


# ------------------------------------------------------------------ loading

def _connect(path: Path):
    db = sqlite3.connect(path, timeout=20)
    db.row_factory = sqlite3.Row
    return db


def load(db_path: Path, since: str = '', key: str | None = None) -> tuple[list[dict], dict, dict]:
    """Jobs (no attached companions, no historical imports, no practice examples), their events and usage."""
    if not Path(db_path).exists():
        return [], {}, {}
    db = _connect(db_path)
    try:
        where = "historical=0 AND status NOT IN ('attached','historical') AND created>=? " \
                "AND COALESCE(json_extract(receipt,'$.source'),'') NOT LIKE 'Ejemplo:%'"
        args: list = [since]
        if key is not None:
            where += ' AND id=?'
            args.append(key)
        jobs = [dict(row) for row in db.execute(
            f'SELECT id,status,receipt,created,human_exposed FROM jobs WHERE {where} ORDER BY created', args)]
        events: dict[str, list[dict]] = {job['id']: [] for job in jobs}
        for row in db.execute(f'SELECT job_id,at,kind,detail FROM events WHERE job_id IN '
                              f'(SELECT id FROM jobs WHERE {where}) ORDER BY id', args):
            events[row['job_id']].append(dict(row))
        usage: dict[str, list[dict]] = {}
        try:
            for row in db.execute(f'SELECT * FROM ai_usage WHERE ticket IN (SELECT id FROM jobs WHERE {where}) '
                                  'ORDER BY id', args):
                usage.setdefault(row['ticket'], []).append(dict(row))
        except sqlite3.OperationalError:
            pass  # No AI call has been logged yet.
    finally:
        db.close()
    for job in jobs:
        try:
            job['receipt'] = json.loads(job['receipt'])
        except (TypeError, ValueError):
            job['receipt'] = {}
    return jobs, events, usage


# ------------------------------------------------------------------ one ticket

def _pauses(events: list[dict]) -> list[tuple[datetime, datetime | None]]:
    """Each pause lasts until the next event of the same ticket (the moment work went on)."""
    result = []
    for index, event in enumerate(events):
        if event['kind'] not in PAUSES:
            continue
        start = _when(event['at'])
        if start is None:
            continue
        end = next((_when(later['at']) for later in events[index + 1:] if _when(later['at'])), None)
        result.append((start, end))
    return result


def _sessions(events: list[dict]) -> list[tuple[datetime, datetime]]:
    result = []
    for index, event in enumerate(events):
        if event['kind'] != SESSION_START or not (start := _when(event['at'])):
            continue
        end = next((_when(later['at']) for later in events[index + 1:] if later['kind'] in SESSION_END), None)
        if end and end >= start:
            result.append((start, end))
    return result


def metricas_ticket(job: dict, events: list[dict], usage: list[dict], prices: dict) -> dict:
    kinds = [event['kind'] for event in events]
    reads = [data for event in events if event['kind'] == 'read_timing' and (data := _json(event['detail']))]
    read_ms = [_number(data.get('total_ms')) for data in reads]
    sessions = [data for event in events if event['kind'] == 'session_metrics' and (data := _json(event['detail']))]
    pauses = _pauses(events)
    known = [(end - start).total_seconds() for start, end in pauses if end is not None]

    portal_s, portal_source = SIN_DATO, None
    seconds = [_number(data.get('seconds')) for data in sessions]
    if sessions and all(value is not None for value in seconds):
        portal_s, portal_source = round(sum(seconds), 1), 'sesion'
    elif windows := _sessions(events):
        total = 0.0
        for start, end in windows:
            paused = sum(max(0.0, (min(end, p_end) - max(start, p_start)).total_seconds())
                         for p_start, p_end in pauses if p_end is not None)
            total += max(0.0, (end - start).total_seconds() - paused)
        portal_s, portal_source = round(total, 1), 'eventos'

    if usage:
        ai_calls, ai_source = len(usage), 'registro'
    elif sessions and all(_number(data.get('ai_calls')) is not None for data in sessions):
        ai_calls, ai_source = sum(data['ai_calls'] for data in sessions), 'sesion'
    else:
        ai_calls, ai_source = SIN_DATO, None

    replayed = SIN_DATO
    if sessions and all(_number(data.get('replayed_actions')) is not None for data in sessions):
        replayed = sum(data['replayed_actions'] for data in sessions)
    elif any(kind in MEMORY for kind in kinds):
        replayed = kinds.count('memory_replay')
    actions = (sum(data['actions'] for data in sessions)
               if sessions and all(_number(data.get('actions')) is not None for data in sessions) else SIN_DATO)

    costs = [costo(prices, row['provider'], row['model'], row['input_tokens'], row['output_tokens']) for row in usage]
    if not usage:
        cost, cost_state = SIN_DATO, 'sin_dato'
    elif any(value is None for value in costs):
        cost, cost_state = SIN_PRECIO, 'sin_precio'
    else:
        cost, cost_state = round(sum(costs), 6), 'ok'

    corrections = sum(kind in CORRECTIONS for kind in kinds) + sum(
        1 for event in events if event['kind'] == 'fuentes_de_campos'
        and 'correccion_usuario' in ((_json(event['detail']) or {}).get('tools') or []))
    verified = job['status'] == 'verified'
    attempted = verified or any(kind in {SESSION_START, 'session_metrics', 'issue_attempt'} for kind in kinds)
    return {
        'ticket': job['id'], 'estado': job['status'], 'portal': portal_of(job['receipt']), 'creado': job['created'],
        'facturado': verified, 'intento_portal': attempted,
        'intentos_portal': max(kinds.count(SESSION_START), len(sessions), 1 if attempted else 0),
        'lectura_ms': sum(read_ms) if reads and all(v is not None for v in read_ms) else SIN_DATO,
        'lecturas': len(reads),
        'portal_s': portal_s, 'portal_fuente': portal_source,
        'pausas': len(pauses), 'pausas_s': round(sum(known), 1) if known else (0 if not pauses else SIN_DATO),
        'llamadas_ia': ai_calls, 'llamadas_fuente': ai_source,
        'acciones': actions, 'acciones_memoria': replayed,
        'costo': cost, 'costo_estado': cost_state, 'moneda': prices.get('moneda', 'USD'),
        'correcciones': corrections,
        'sin_intervencion': bool(verified and not pauses and not corrections and not job.get('human_exposed')),
    }


# ------------------------------------------------------------------ aggregates

def _median(values):
    values = [v for v in values if v is not None]
    return statistics.median(values) if values else SIN_DATO


def _mean(values):
    values = [v for v in values if v is not None]
    return round(statistics.fmean(values), 2) if values else SIN_DATO


def tickets(db_path: Path, root: Path, since: str = '', key: str | None = None) -> list[dict]:
    jobs, events, usage = load(db_path, since, key)
    prices = leer_precios(root)
    return [metricas_ticket(job, events.get(job['id'], []), usage.get(job['id'], []), prices) for job in jobs]


def portales(rows: list[dict]) -> list[dict]:
    groups: dict[str, dict] = {}
    for row in rows:
        if not row['intento_portal']:
            continue
        group = groups.setdefault(row['portal'], {'portal': row['portal'], 'tickets': 0, 'intentos': 0, 'exitos': 0,
                                                  'fallos': 0, '_acciones': [], '_memoria': []})
        group['tickets'] += 1
        group['intentos'] += row['intentos_portal']
        group['exitos'] += row['facturado']
        group['fallos'] += not row['facturado']
        if row['acciones'] is not None and row['acciones_memoria'] is not None:
            group['_acciones'].append(row['acciones'])
            group['_memoria'].append(row['acciones_memoria'])
    result = []
    for group in groups.values():
        acciones, memoria = sum(group.pop('_acciones')), sum(group.pop('_memoria'))
        group['pct_memoria'] = round(100 * memoria / acciones, 1) if acciones else SIN_DATO
        group['pct_exito'] = round(100 * group['exitos'] / group['tickets'], 1)
        result.append(group)
    return sorted(result, key=lambda g: (-g['intentos'], g['portal']))


def resumen(rows: list[dict], periodo: str) -> dict:
    invoiced = [row for row in rows if row['facturado']]
    alone = [row for row in invoiced if row['sin_intervencion']]
    priced = [row['costo'] for row in rows if row['costo_estado'] == 'ok']
    failing = sorted((group for group in portales(rows) if group['fallos']),
                     key=lambda g: (-g['fallos'], -g['intentos'], g['portal']))[:5]
    unpriced = any(row['costo_estado'] == 'sin_precio' for row in rows)
    return {
        'periodo': periodo, 'subidos': len(rows), 'facturados': len(invoiced),
        'sin_intervencion': len(alone),
        'pct_sin_intervencion': round(100 * len(alone) / len(invoiced), 1) if invoiced else SIN_DATO,
        'mediana_lectura_ms': _median([row['lectura_ms'] for row in rows]),
        'mediana_portal_s': _median([row['portal_s'] for row in rows]),
        'promedio_llamadas_ia': _mean([row['llamadas_ia'] for row in rows]),
        'costo_por_ticket': round(statistics.fmean(priced), 6) if priced else (SIN_PRECIO if unpriced else SIN_DATO),
        'moneda': rows[0]['moneda'] if rows else 'USD',
        'pausas_por_ticket': _mean([row['pausas'] for row in rows if row['intento_portal']]),
        'portales_que_mas_fallan': [{'portal': g['portal'], 'fallos': g['fallos'], 'intentos': g['intentos']}
                                    for g in failing],
    }


def medicion_router(store) -> APIRouter:
    router = APIRouter()

    @router.get('/api/medicion/resumen')
    def summary(periodo: str = '7d'):
        return resumen(tickets(store.path, store.root, period_start(periodo)), periodo)

    @router.get('/api/medicion/portales')
    def by_portal(periodo: str = '30d'):
        return {'periodo': periodo, 'portales': portales(tickets(store.path, store.root, period_start(periodo)))}

    @router.get('/api/medicion/tickets/{key}')
    def one(key: str):
        rows = tickets(store.path, store.root, '', key)
        if not rows:
            raise KeyError(key)
        return rows[0]

    return router
