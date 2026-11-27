"""Registro de cada llamada a la IA: qué tarea, qué proveedor, cuánto tardó y cuántos tokens.

Nunca guarda el contenido de la petición ni de la respuesta. La tarea y el ticket llegan por
una variable de contexto para no tocar a quienes llaman; si nadie la fija, la tarea se deduce
del tipo de respuesta pedido y el ticket queda vacío.
"""
from __future__ import annotations

import contextvars
import json
import math
import re
import sqlite3
import threading
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path

from .auth import read_json, write_private

# (task, ticket). Empty values mean «unknown»: the task is then inferred from the output type.
CONTEXTO_IA: contextvars.ContextVar[tuple[str, str]] = contextvars.ContextVar('contexto_ia', default=('', ''))

TAREAS = {'leer_ticket', 'buscar_portal', 'paso_portal', 'investigar', 'constancia', 'chat', 'whatsapp', 'prueba',
          'otra'}
# Output types already used by the program, so the task is known without touching any caller.
TAREA_POR_TIPO = {'Extraction': 'leer_ticket', 'PortalSearch': 'buscar_portal', 'Action': 'paso_portal',
                  'Plan': 'paso_portal', 'RegistrationPlan': 'paso_portal',
                  'WebAnswer': 'investigar', 'TaxProfileExtraction': 'constancia', 'ChatDecision': 'chat',
                  'ChatAnswer': 'chat', 'BotTurn': 'whatsapp'}
TICKET = re.compile(r'[\w-]{1,160}')
OUTCOME = re.compile(r'[a-z_]{1,40}')
PRECIOS = 'precios_ia.json'
SIN_PRECIO = 'sin precio'


def _clean_task(value: str) -> str:
    return value if value in TAREAS else ('otra' if value else '')


def _clean_ticket(value) -> str:
    return value if isinstance(value, str) and TICKET.fullmatch(value) else ''


@contextmanager
def en_tarea(tarea: str = '', ticket: str | None = None):
    """Set the task and/or ticket for every AI call made inside the block (threads included via to_thread)."""
    current_task, current_ticket = CONTEXTO_IA.get()
    token = CONTEXTO_IA.set((_clean_task(tarea) or current_task,
                             _clean_ticket(ticket) if ticket is not None else current_ticket))
    try:
        yield
    finally:
        CONTEXTO_IA.reset(token)


def en_ticket(ticket: str):
    """Shorthand: keep the inferred task, attach the ticket."""
    return en_tarea('', ticket)


def tarea_actual(output_type=None) -> tuple[str, str]:
    task, ticket = CONTEXTO_IA.get()
    if not task:
        task = TAREA_POR_TIPO.get(getattr(output_type, '__name__', ''), 'otra')
    return task, ticket


def now() -> str:
    return datetime.now(timezone.utc).isoformat()


# ------------------------------------------------------------------ prices

def _price(value):
    if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value) or value < 0:
        raise ValueError('Cada precio debe ser un número positivo por millón de tokens.')
    return float(value)


def validar_precios(data) -> dict:
    """{'moneda': 'USD', 'precios': {proveedor: {modelo: {'entrada': n, 'salida': n}}}} (per million tokens)."""
    if not isinstance(data, dict):
        raise ValueError('La tabla de precios debe ser un objeto.')
    currency = data.get('moneda', 'USD')
    if not isinstance(currency, str) or not re.fullmatch(r'[A-Z]{3}', currency):
        raise ValueError('La moneda debe ser un código de tres letras, por ejemplo USD o MXN.')
    prices = data.get('precios', {})
    if not isinstance(prices, dict) or len(prices) > 20:
        raise ValueError('Revisa la lista de precios.')
    clean = {}
    for provider, models in prices.items():
        if not isinstance(provider, str) or not re.fullmatch(r'[a-z0-9_-]{1,40}', provider) or not isinstance(models, dict) \
                or len(models) > 200:
            raise ValueError('Revisa el proveedor de la tabla de precios.')
        clean[provider] = {}
        for model, price in models.items():
            if not isinstance(model, str) or not re.fullmatch(r'[\w.:/-]{1,120}', model) or not isinstance(price, dict) \
                    or set(price) - {'entrada', 'salida'} or not {'entrada', 'salida'} <= set(price):
                raise ValueError('Cada modelo necesita precio de entrada y de salida por millón de tokens.')
            clean[provider][model] = {'entrada': _price(price['entrada']), 'salida': _price(price['salida'])}
    return {'moneda': currency, 'precios': clean}


def leer_precios(root: Path) -> dict:
    """Owner-editable price table. Missing or broken file means «no prices»: never invented."""
    try:
        return validar_precios(read_json(Path(root) / PRECIOS, {}) or {})
    except (ValueError, OSError):
        return {'moneda': 'USD', 'precios': {}}


def guardar_precios(root: Path, data) -> dict:
    clean = validar_precios(data)
    write_private(Path(root) / PRECIOS, clean)
    return clean


def costo(prices: dict, provider: str, model: str, input_tokens, output_tokens) -> float | None:
    """Estimated cost of one call, or None when the price or the token counts are unknown."""
    price = prices.get('precios', {}).get(provider, {}).get(model)
    if not price or input_tokens is None or output_tokens is None:
        return None
    return (input_tokens * price['entrada'] + output_tokens * price['salida']) / 1_000_000


# ------------------------------------------------------------------ log

class RegistroUso:
    """One row per AI call in the `ai_usage` table of the app database. Recording never raises."""

    SCHEMA = """CREATE TABLE IF NOT EXISTS ai_usage(
        id INTEGER PRIMARY KEY, at TEXT NOT NULL, task TEXT NOT NULL, ticket TEXT NOT NULL DEFAULT '',
        provider TEXT NOT NULL, model TEXT NOT NULL DEFAULT '', effort TEXT NOT NULL DEFAULT '',
        duration_ms INTEGER NOT NULL, input_tokens INTEGER, output_tokens INTEGER,
        web_search INTEGER NOT NULL DEFAULT 0, outcome TEXT NOT NULL);
        CREATE INDEX IF NOT EXISTS ai_usage_ticket ON ai_usage(ticket) WHERE ticket<>'';
        CREATE INDEX IF NOT EXISTS ai_usage_at ON ai_usage(at);"""

    def __init__(self, root: Path):
        self.root = Path(root)
        self.path = self.root / 'facturador.sqlite'
        self._ready = False
        self._lock = threading.Lock()

    def _connect(self):
        db = sqlite3.connect(self.path, timeout=20)
        db.row_factory = sqlite3.Row
        if not self._ready:
            with self._lock:
                db.executescript(self.SCHEMA)
                self._ready = True
        return db

    @staticmethod
    def _tokens(value):
        return value if isinstance(value, int) and not isinstance(value, bool) and value >= 0 else None

    def record(self, *, task: str, ticket: str, provider: str, model: str, effort: str, duration_ms: int,
               input_tokens=None, output_tokens=None, web_search=False, outcome='ok') -> None:
        try:
            self.root.mkdir(parents=True, exist_ok=True)
            db = self._connect()
            try:
                with db:
                    db.execute('INSERT INTO ai_usage(at,task,ticket,provider,model,effort,duration_ms,input_tokens,'
                               'output_tokens,web_search,outcome) VALUES(?,?,?,?,?,?,?,?,?,?,?)',
                               (now(), _clean_task(task) or 'otra', _clean_ticket(ticket),
                                provider if re.fullmatch(r'[a-z0-9_-]{1,40}', provider or '') else 'otro',
                                model if re.fullmatch(r'[\w.:/-]{1,120}', model or '') else '',
                                effort if re.fullmatch(r'[a-z]{1,12}', effort or '') else '',
                                max(0, int(duration_ms)), self._tokens(input_tokens), self._tokens(output_tokens),
                                int(bool(web_search)), outcome if OUTCOME.fullmatch(outcome or '') else 'error'))
            finally:
                db.close()
        except (sqlite3.Error, OSError, ValueError, TypeError):
            pass  # Measuring must never stop a ticket.

    def rows(self, *, ticket: str | None = None, since: str = '') -> list[dict]:
        if not self.path.exists():
            return []
        try:
            db = self._connect()
            try:
                query, args = 'SELECT * FROM ai_usage WHERE at>=?', [since]
                if ticket is not None:
                    query += ' AND ticket=?'
                    args.append(ticket)
                return [dict(row) for row in db.execute(query + ' ORDER BY id', args)]
            finally:
                db.close()
        except sqlite3.Error:
            return []

    def with_cost(self, rows: list[dict]) -> list[dict]:
        prices = leer_precios(self.root)
        return [{**row, 'costo': costo(prices, row['provider'], row['model'], row['input_tokens'], row['output_tokens'])}
                for row in rows]


def resumen_uso(registro: RegistroUso, since: str = '') -> dict:
    """Calls grouped by task and provider, with estimated cost or «sin precio»."""
    rows = registro.with_cost(registro.rows(since=since))
    groups: dict[tuple[str, str, str], dict] = {}
    for row in rows:
        group = groups.setdefault((row['task'], row['provider'], row['model']), {
            'tarea': row['task'], 'proveedor': row['provider'], 'modelo': row['model'], 'llamadas': 0, 'fallidas': 0,
            'ms_total': 0, 'tokens_entrada': 0, 'tokens_salida': 0, 'tokens_conocidos': True, 'costo': 0.0,
            'con_precio': True, 'busquedas_web': 0})
        group['llamadas'] += 1
        group['fallidas'] += row['outcome'] != 'ok'
        group['ms_total'] += row['duration_ms']
        group['busquedas_web'] += row['web_search']
        if row['input_tokens'] is None or row['output_tokens'] is None:
            group['tokens_conocidos'] = False
        else:
            group['tokens_entrada'] += row['input_tokens']
            group['tokens_salida'] += row['output_tokens']
        if row['costo'] is None:
            group['con_precio'] = False
        else:
            group['costo'] += row['costo']
    result = []
    for group in groups.values():
        if not group.pop('tokens_conocidos'):
            group['tokens_entrada'] = group['tokens_salida'] = None
        group['costo'] = round(group['costo'], 6) if group.pop('con_precio') else SIN_PRECIO
        result.append(group)
    return {'moneda': leer_precios(registro.root)['moneda'], 'grupos': sorted(
        result, key=lambda g: (-g['llamadas'], g['tarea']))}


def precios_json(root: Path) -> str:
    return json.dumps(leer_precios(root), ensure_ascii=False)
