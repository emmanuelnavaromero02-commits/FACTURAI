"""Banco de portales: mide qué tan bien resuelve el motor portales de facturación que nunca ha visto.

    python -m facturador.banco [--ia-real] [--datos CARPETA] [--portales a,b,c] [--salida archivo.json]

Cada portal del banco es inventado (``facturador/banco_portales``) y vive en un dominio
``.example`` servido desde memoria. Para cada uno se crean un perfil y un ticket ficticios
en una carpeta temporal y se corre el ``Runner`` real de principio a fin; luego una segunda
pasada con otro ticket en los mismos portales mide lo que la memoria ahorra. El navegador
solo puede hablar con los dominios del banco: cualquier otra petición se bloquea y se cuenta.
Nunca se toca un portal real, ni el perfil, los tickets o la memoria del dueño.

Por omisión decide la IA guionizada (determinista). ``--ia-real`` usa la IA configurada en
``--datos``; solo se usa cuando alguien corre el comando a mano.
"""
from __future__ import annotations

import argparse
import asyncio
import hashlib
import json
import random
import statistics
import struct
import sys
import tempfile
import threading
import time
import zlib
from contextlib import ExitStack
from datetime import date
from pathlib import Path
from unittest.mock import patch
from urllib.parse import urlsplit

from . import runner as motor
from .auth import PlanError
from .banco_portales import PORTALES, PortalSimulado, Vista
from .catalog import CUSTOM_PORTAL
from .db import Store
from .documents import reconcile, save_candidate
from .models import Action, Plan, Profile, Receipt

PERFIL = Profile(rfc='XAXX010101000', name='Cliente de Prueba del Banco', postal_code='06000', regime='612',
                 cfdi_use='G03', email='banco.prueba@example.com')
RESULTADOS = {'verificada': 'factura comprobada', 'emitida_espera': 'emitida y esperando archivos',
              'persona': 'necesita a la persona', 'fallo': 'no terminó'}
# A paused ticket gets this long to go on by itself (the runner may observe again) before it counts as waiting.
GRACIA_PERSONA = 6.0


def portales(slugs: list[str] | None = None) -> list[PortalSimulado]:
    """Fresh instances of the bank portals, all or the named ones, in bank order."""
    every = [kind() for kind in PORTALES]
    if not slugs:
        return every
    known = {portal.slug: portal for portal in every}
    missing = [slug for slug in slugs if slug not in known]
    if missing:
        raise ValueError('Portales que no están en el banco: ' + ', '.join(missing) +
                         '. Usa: ' + ', '.join(known) + '.')
    return [known[slug] for slug in dict.fromkeys(slugs)]


def ticket_para(portal: PortalSimulado, pasada: int) -> Receipt:
    """A made-up purchase at that portal; the same one for the same portal and pass."""
    rng = random.Random(f'{portal.slug}:{pasada}')
    cents = rng.choice(['00', '50', '90', f'{rng.randint(1, 99):02d}'])
    return Receipt(provider=CUSTOM_PORTAL, portal_url=portal.portal_url, merchant=portal.marca,
                   amount=f'{rng.randint(45, 1890)}.{cents}', date=date(2026, 11, rng.randint(1, 25)).isoformat(),
                   ticket_number=portal.folio_formato.format(n=rng.randint(100000, 999999)),
                   payment_code=rng.choice(['01', '04', '28']))


def foto_png(path: Path, texto: str) -> None:
    """A small grey PNG standing for the photo of the ticket (some portals ask for it)."""
    width, height = 40, 60
    shade = int(hashlib.sha256(texto.encode()).hexdigest()[:2], 16)
    rows = b''.join(b'\x00' + bytes([shade, shade, shade]) * width for _ in range(height))

    def chunk(kind, data):
        return struct.pack('>I', len(data)) + kind + data + struct.pack('>I', zlib.crc32(kind + data) & 0xffffffff)
    # The text chunk makes every photo distinct: the store refuses a photo it already has.
    path.write_bytes(b'\x89PNG\r\n\x1a\n' + chunk(b'IHDR', struct.pack('>IIBBBBB', width, height, 8, 2, 0, 0, 0)) +
                     chunk(b'tEXt', b'Comment\x00' + texto.encode()) +
                     chunk(b'IDAT', zlib.compress(rows)) + chunk(b'IEND', b''))


class IAGuionizada:
    """Deterministic stand-in for the model: each bank portal says what to do on its screens.

    It answers like the plan objects the runner receives (``infer``, ``status``, ``close``).
    """

    def __init__(self, banco: list[PortalSimulado]):
        self.por_origen = {origin: portal for portal in banco for origin in portal.origenes}

    def status(self) -> dict:
        return {'status': 'ready', 'email': '', 'model': 'guion', 'message': '', 'registered': True}

    def close(self) -> None:
        pass

    def infer(self, instructions, content, output_type, *, effort='medium', web_search=False):
        if output_type is not Plan:
            raise PlanError('La IA guionizada solo decide pantallas del banco.', 'model')
        facts = json.loads(instructions.rsplit('\n', 1)[1])
        view = json.loads(content[0]['text'])
        portal = self.por_origen.get(view.get('url'))
        if portal is None:
            return Plan(steps=[Action(kind='human', target=None, value=None, reason='Portal fuera del banco', risk='unknown')])
        return portal.guion(Vista(view, facts['perfil'], facts['ticket']))


class Contador:
    """Wraps the plan the runner uses and notes each screen consultation: before or after the emission."""

    def __init__(self, plan):
        self.plan = plan
        self.lock = threading.Lock()
        self.ticket = ''
        self.registro: dict[str, list[str]] = {}

    def __getattr__(self, name):
        plan = self.__dict__.get('plan')
        if plan is None or name.startswith('__'):
            raise AttributeError(name)
        return getattr(plan, name)

    def status(self):
        return self.plan.status()

    def infer(self, instructions, content, output_type, **options):
        if output_type is Plan:
            try:
                view = json.loads(content[0]['text'])
                phase = 'final' if view.get('emision_intentada') or view.get('emision_visible') else 'preparatoria'
            except (ValueError, LookupError, TypeError, AttributeError):
                phase = 'preparatoria'
            with self.lock:
                self.registro.setdefault(self.ticket, []).append(phase)
        return self.plan.infer(instructions, content, output_type, **options)


class _Ruta:
    """What the runner's own navigation guard sees: continuing serves the bank page, aborting blocks it."""

    def __init__(self, route, portal: PortalSimulado, aislamiento: 'Aislamiento'):
        self._route, self._portal, self._aislamiento = route, portal, aislamiento
        self.request = route.request

    async def continue_(self, **_):
        await self._portal.atender(self._route)

    fallback = continue_

    async def abort(self, *_args, **_kw):
        self._aislamiento.bloqueadas_por_motor.append(self.request.url[:200])
        await self._route.abort('blockedbyclient')


class Aislamiento:
    """The only door of the harness browser: bank domains reach their simulated portal, nothing else leaves."""

    def __init__(self, banco: list[PortalSimulado]):
        self.por_host = {host: portal for portal in banco for host in portal.dominios}
        self.fuera: list[str] = []
        self.bloqueadas_por_motor: list[str] = []
        self.contextos = []
        self.runner = None

    async def resuelve(self, url: str) -> bool:
        # Stands for the public DNS check: only bank domains «resolve», and nothing is looked up.
        return urlsplit(url).hostname in self.por_host

    async def atender(self, route) -> None:
        url = route.request.url
        portal = self.por_host.get(urlsplit(url).hostname or '')
        if portal is None or urlsplit(url).scheme != 'https':
            self.fuera.append(url[:200])
            await route.abort('blockedbyclient')
            return
        await self.runner.guard_navigation(_Ruta(route, portal, self))

    def vigilar(self, runner) -> None:
        """Put this door in front of every browser context the runner opens, before any page loads."""
        self.runner = runner
        original = runner.start_browser

        async def start_browser():
            await original()
            context = runner.context
            if context is not None and all(context is not known for known in self.contextos):
                # Registered after the runner's guard, so it runs first for every request.
                await context.route('**/*', self.atender)
                self.contextos.append(context)
        runner.start_browser = start_browser


def _eventos(store: Store, key: str) -> dict:
    events = store.events(key)
    kinds = [event['kind'] for event in events]
    sessions = []
    for event in events:
        if event['kind'] == 'session_metrics':
            try:
                sessions.append(json.loads(event['detail']))
            except ValueError:
                pass
    learned = 0
    for event in events:
        if event['kind'] == 'memory_learned':
            try:
                learned += int(event['detail'].split()[0])
            except (ValueError, IndexError):
                pass
    return {'sesiones': len(sessions), 'consultas': sum(item.get('ai_calls', 0) for item in sessions),
            'acciones': sum(item.get('actions', 0) for item in sessions),
            'acciones_repetidas': sum(item.get('replayed_actions', 0) for item in sessions),
            'segundos': round(sum(item.get('seconds', 0) for item in sessions), 1),
            'cierres': [item.get('outcome', '') for item in sessions],
            'pantallas_repetidas': kinds.count('memory_replay'), 'pantallas_sin_receta': kinds.count('memory_miss'),
            'pantallas_aprendidas': learned, 'intentos_de_emision': kinds.count('issue_attempt'),
            'redirecciones': kinds.count('portal_redirect'), 'navegacion_bloqueada': kinds.count('navigation_blocked'),
            'ultimos_eventos': [f"{event['kind']}: {event['detail'][:160]}" for event in events[-8:]]}


class Banco:
    """One measurement: a temporary data folder, its own memory and one browser for every portal."""

    def __init__(self, banco: list[PortalSimulado], plan, *, carpeta: Path, tiempo_max: float = 240.0,
                 entregar_correo: bool = True):
        self.banco, self.carpeta, self.tiempo_max, self.entregar_correo = banco, Path(carpeta), tiempo_max, entregar_correo
        self.store = Store(self.carpeta / 'datos')
        self.contador = Contador(plan)
        self.runner = motor.Runner(self.store, self.contador)
        self.aislamiento = Aislamiento(banco)
        self.aislamiento.vigilar(self.runner)
        self.fotos = self.carpeta / 'fotos'
        self.fotos.mkdir(parents=True, exist_ok=True)

    async def ticket(self, portal: PortalSimulado, pasada: int) -> dict:
        receipt = ticket_para(portal, pasada)
        portal.registrar(receipt, PERFIL)
        photo = self.fotos / f'{portal.slug}-{pasada}.png'
        foto_png(photo, f'{portal.slug}:{pasada}:{receipt.ticket_number}')
        key = self.store.add(str(photo), hashlib.sha256(photo.read_bytes()).hexdigest())[0]['id']
        self.store.save_receipt(key, receipt, ready=True)
        blocked_before = len(self.aislamiento.fuera)
        persona, preguntas, agotado = None, 0, False
        started = time.monotonic()
        self.contador.ticket = key
        self.runner.schedule(key, PERFIL, auto_issue=True, confirmed_url=receipt.portal_url)
        task = self.runner.job_tasks[key]
        paused_since = None
        while not task.done():
            pending = self.runner.pending.get(key)
            if pending and not pending['future'].done():
                paused_since = paused_since or time.monotonic()
                if time.monotonic() - paused_since >= GRACIA_PERSONA:
                    persona = {'tipo': pending['kind'], 'mensaje': self.store.get(key)['message']}
            else:
                paused_since = None
            if key in self.runner.questions and not self.runner.questions[key].done():
                # Nobody answers the chat in the bank: the ticket goes on with its assumption.
                preguntas += 1
                self.runner.answer_question(key, '')
            if persona is None and time.monotonic() - started > self.tiempo_max:
                agotado = True
            if persona or agotado:
                try:
                    await self.runner.stop(key)
                except ValueError:
                    pass
                break
            await asyncio.sleep(0.05)
        await asyncio.gather(task, return_exceptions=True)
        row = self.store.get(key)
        obtained = ('persona' if persona else 'verificada' if row['status'] == 'verified' else
                    'emitida_espera' if row['status'] == 'issued_waiting_files' else 'fallo')
        result = {'portal': portal.slug, 'pasada': pasada, 'esperado': portal.esperado, 'obtenido': obtained,
                  'acierto': obtained == portal.esperado, 'estado': row['status'], 'mensaje': row['message'],
                  'persona': persona, 'agotado': agotado, 'preguntas_al_chat': preguntas,
                  'segundos_reloj': round(time.monotonic() - started, 1),
                  'peticiones_fuera_del_banco': self.aislamiento.fuera[blocked_before:], **_eventos(self.store, key)}
        phases = self.contador.registro.get(key, [])
        result['consultas_preparatorias'] = phases.count('preparatoria')
        result['consultas_tras_emitir'] = phases.count('final')
        if obtained == 'emitida_espera' and self.entregar_correo and portal.emisiones.get(receipt.ticket_number):
            # The mail with the files «arrives»: they are checked as the panel would, so the memory can learn.
            save_candidate(self.store, key, 'factura-correo.zip', portal.correo(receipt.ticket_number))
            try:
                reconcile(self.store, key, PERFIL)
            except ValueError:
                pass
            result['correo_entregado'] = True
            result['verificada_tras_correo'] = self.store.get(key)['status'] == 'verified'
        return result

    async def correr(self, pasadas: int = 2) -> list[dict]:
        results = []
        try:
            await self.runner.start_browser()
            for pasada in range(1, pasadas + 1):
                for portal in self.banco:
                    results.append(await self.ticket(portal, pasada))
        finally:
            await self.runner.close()
        return results


async def medir(slugs: list[str] | None = None, *, banco: list[PortalSimulado] | None = None, plan=None,
                ia: str = 'guionizada', carpeta: Path | None = None, rapido: bool = False, tiempo_max: float = 240.0,
                pasadas: int = 2) -> dict:
    """Run the bank and return every figure. ``plan`` is the AI to use; by default the scripted one.

    ``rapido`` only shortens the engine's waits for pages that already moved on (for tests).
    """
    banco = banco if banco is not None else portales(slugs)
    plan = plan if plan is not None else IAGuionizada(banco)
    with ExitStack() as stack:
        if carpeta is None:
            carpeta = Path(stack.enter_context(tempfile.TemporaryDirectory(prefix='banco-portales-')))
        harness = Banco(banco, plan, carpeta=carpeta, tiempo_max=tiempo_max)
        stack.enter_context(patch.object(motor, 'resolves_publicly', harness.aislamiento.resuelve))
        if rapido:
            # The pause between remembered steps stays as in real use: timing is part of what is measured.
            stack.enter_context(patch.object(motor, 'SETTLE_PAUSE', 0.05))
        started = time.monotonic()
        tickets = await harness.correr(pasadas)
        outside = list(harness.aislamiento.fuera)
        by_engine = list(harness.aislamiento.bloqueadas_por_motor)
    return {'ia': ia, 'fecha': time.strftime('%Y-%m-%d %H:%M:%S'), 'segundos_totales': round(time.monotonic() - started, 1),
            'portales': [{'portal': portal.slug, 'marca': portal.marca, 'forma': portal.forma, 'esperado': portal.esperado,
                          'limitacion': portal.limitacion, 'limitacion_memoria': portal.limitacion_memoria,
                          'dominios': sorted(portal.dominios)} for portal in banco],
            'tickets': tickets, 'peticiones_fuera_del_banco': outside, 'bloqueadas_por_el_motor': by_engine,
            'pasadas': {str(number): cifras([item for item in tickets if item['pasada'] == number])
                        for number in range(1, pasadas + 1)}}


def _mediana(values: list[float]) -> float:
    return round(statistics.median(values), 1) if values else 0.0


def cifras(tickets: list[dict]) -> dict:
    alone = [item for item in tickets if item['esperado'] != 'persona']
    waits = [item for item in tickets if item['esperado'] == 'persona']
    return {'portales': len(tickets), 'pueden_salir_solos': len(alone),
            'salieron_solos': sum(item['acierto'] for item in alone),
            'deben_pedir_a_la_persona': len(waits), 'pidieron_a_la_persona': sum(item['acierto'] for item in waits),
            'mediana_segundos': _mediana([item['segundos'] for item in tickets]),
            'mediana_consultas': _mediana([item['consultas'] for item in tickets]),
            'consultas_preparatorias': sum(item['consultas_preparatorias'] for item in tickets),
            'sin_consultas_preparatorias': sum(item['consultas_preparatorias'] == 0 for item in alone if item['acierto']),
            'acciones_repetidas': sum(item['acciones_repetidas'] for item in tickets),
            'distintos_de_lo_esperado': [item['portal'] for item in tickets if not item['acierto']]}


def resumen(resultado: dict) -> str:
    """Short Spanish summary of a measurement."""
    lines = [f"Banco de portales con IA {resultado['ia']}: {len(resultado['portales'])} portales, "
             f"{resultado['segundos_totales']} s en total."]
    names = {'1': 'Primera pasada (portales nuevos)', '2': 'Segunda pasada (con memoria)'}
    for number, data in resultado['pasadas'].items():
        line = (f"{names.get(number, 'Pasada ' + number)}: {data['salieron_solos']} de {data['pueden_salir_solos']} "
                f"portales {'nuevos ' if number == '1' else ''}salieron solos")
        if data['deben_pedir_a_la_persona']:
            line += f"; {data['pidieron_a_la_persona']} de {data['deben_pedir_a_la_persona']} pidieron a la persona como debían"
        line += f". Mediana: {data['mediana_segundos']} s y {data['mediana_consultas']} consultas."
        if number != '1':
            line += (f" Sin consultas antes de emitir: {data['sin_consultas_preparatorias']} de {data['salieron_solos']}; "
                     f"{data['acciones_repetidas']} acciones repetidas de memoria.")
        lines.append(line)
        if data['distintos_de_lo_esperado']:
            details = [f"{item['portal']} ({RESULTADOS[item['obtenido']]})" for item in resultado['tickets']
                       if item['pasada'] == int(number) and not item['acierto']]
            lines.append('  Distinto de lo esperado: ' + ', '.join(details) + '.')
    pending = [f"{item['portal']} ({'primera pasada' if item['limitacion'] else 'con memoria'})"
               for item in resultado['portales'] if item['limitacion'] or item['limitacion_memoria']]
    if pending:
        lines.append('El motor todavía no puede: ' + ', '.join(pending) + '.')
    outside = len(resultado['peticiones_fuera_del_banco'])
    lines.append('Peticiones fuera del banco: ' + (f'{outside} (bloqueadas; error del arnés).' if outside else 'ninguna.'))
    return '\n'.join(lines)


def ia_configurada(datos: Path | None, uso: Path):
    """The AI configured in ``datos``, or a clear error. Its use is counted in ``uso``, never in that folder."""
    from .proveedores_ia import SelectorIA
    from .uso_ia import RegistroUso

    if datos is None or not Path(datos).is_dir():
        raise ValueError('--ia-real necesita --datos con la carpeta de datos donde está configurada la IA.')
    plan = SelectorIA(Path(datos), usage=RegistroUso(uso))
    state = plan.status()
    if state.get('status') != 'ready':
        plan.close()
        raise ValueError('La carpeta de datos no tiene una IA lista (' + str(state.get('status', 'sin estado')) +
                         '). Conéctala en Ajustes antes de medir con --ia-real.')
    return plan


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog='python -m facturador.banco',
                                     description='Mide el motor con portales de facturación inventados que nunca ha visto.')
    parser.add_argument('--ia-real', action='store_true', help='usar la IA configurada en --datos en vez de la guionizada')
    parser.add_argument('--datos', type=Path, help='carpeta de datos con la IA configurada (solo con --ia-real)')
    parser.add_argument('--portales', help='lista de portales separados por coma; por omisión, todos')
    parser.add_argument('--salida', type=Path, default=Path('banco-medicion.json'), help='archivo JSON con todas las cifras')
    parser.add_argument('--tiempo', type=float, default=240.0, help='segundos máximos por ticket')
    args = parser.parse_args(argv)
    slugs = [item.strip() for item in args.portales.split(',') if item.strip()] if args.portales else None
    try:
        portales(slugs)
    except ValueError as exc:
        print(exc, file=sys.stderr)
        return 2
    with tempfile.TemporaryDirectory(prefix='banco-portales-') as folder:
        plan = None
        if args.ia_real:
            try:
                plan = ia_configurada(args.datos, Path(folder))
            except ValueError as exc:
                print(exc, file=sys.stderr)
                return 2
        try:
            result = asyncio.run(medir(slugs, plan=plan, ia='real' if plan else 'guionizada', carpeta=Path(folder),
                                       tiempo_max=args.tiempo))
        finally:
            if plan is not None:
                plan.close()
    figures = json.dumps(result, ensure_ascii=False, indent=2)
    output = args.salida
    try:
        output.write_text(figures, encoding='utf-8')
    except OSError:
        # The folder may be read-only (inside the container): the measurement is not thrown away.
        output = Path(tempfile.gettempdir()) / output.name
        output.write_text(figures, encoding='utf-8')
    print(resumen(result))
    print('Cifras completas en ' + str(output))
    return 0


if __name__ == '__main__':
    sys.exit(main())
