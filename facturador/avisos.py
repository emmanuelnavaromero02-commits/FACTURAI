"""Tell the person when a ticket needs them or changes, by push, by email to themselves and in the panel.

Every notice is short and carries no personal data: a fixed title, the merchant name with numbers and
addresses removed, a fixed reason and a relative link to the ticket. Never an RFC, email, folio, amount,
password or code. Delivering a notice never raises into the caller: a failed channel is only logged.
"""
from __future__ import annotations

import logging
import re
import threading
import time
from email import policy
from email.message import EmailMessage
from email.utils import formatdate, make_msgid
from pathlib import Path

from .auth import read_json, write_private
from .avisos_push import PushDevices
from .db import now

_LOG = logging.getLogger(__name__)

# kind -> title shown to the person. The kind is also the switch in the settings.
TIPOS = {
    'te_necesito': 'Te necesito',
    'factura_lista': 'Factura lista',
    'ticket_detenido': 'Se detuvo un ticket',
    'whatsapp': 'WhatsApp se desvinculó',
    'correo_ambiguo': 'Te necesito',
}
TIPOS_TEXTO = {
    'te_necesito': 'Cuando un ticket espera algo de ti (verificación, inicio de sesión, aprobar, una pregunta)',
    'factura_lista': 'Cuando una factura queda comprobada',
    'ticket_detenido': 'Cuando un ticket se detiene o su resultado no es seguro',
    'whatsapp': 'Cuando WhatsApp se desvincula y hay que volver a vincularlo',
    'correo_ambiguo': 'Cuando llegan por correo varias facturas que podrían ser de un ticket',
}
CANALES = {'navegador': 'Avisos en este navegador y en el celular', 'correo': 'Correo a mi propia dirección',
           'panel': 'Lista de avisos en el panel'}
URGENTES = {'te_necesito', 'correo_ambiguo', 'whatsapp'}
DEFAULTS = {'tipos': {name: True for name in TIPOS},
            # Email is opt-in: nothing new leaves the computer until the person turns it on.
            'canales': {'navegador': True, 'correo': False, 'panel': True},
            'correo_destino': 'cuenta'}
EMAILS_PER_HOUR = 12
TICKET_ID = re.compile(r'[\w-]{1,128}')
SETTINGS_LOCK = threading.Lock()

# Anything that could identify the person or the purchase is cut from the merchant name.
_PERSONAL = [
    re.compile(r'(?:https?://|www\.)\S+', re.I),                                # links
    re.compile(r'[^\s@]+@[^\s@]+'),                                             # email
    re.compile(r'\b[A-ZÑ&]{3,4}-?\d{6}-?[A-Z0-9]{3}\b', re.I),                  # RFC
    re.compile(r'[0-9A-F]{8}-[0-9A-F]{4}-[0-9A-F]{4}-[0-9A-F]{4}-[0-9A-F]{12}', re.I),  # UUID
    re.compile(r'\$\s*\d[\d,.]*'),                                              # amounts
    re.compile(r'\d[\d\s().,/-]{2,}\d'),                                        # phones, folios, dates
    re.compile(r'\d{3,}'),
]


def limpiar(text: str, limit: int) -> str:
    value = ' '.join(str(text or '').split())
    for pattern in _PERSONAL:
        value = pattern.sub(' ', value)
    value = re.sub(r'[^\w .,:&\'()«»¿?¡!-]', ' ', value)
    value = ' '.join(value.split()).strip(' .,:-()')
    return value[:limit].rstrip(' .,:-(')


def liga(job_id: str = '', screen: str = 'bandeja') -> str:
    """Relative link that opens the panel on the ticket; the id is a random key, not personal data."""
    screen = screen if screen in {'bandeja', 'chat', 'ajustes'} else 'bandeja'
    if job_id and TICKET_ID.fullmatch(job_id):
        return f'/?ticket={job_id}#{screen}'
    return f'/#{screen}'


def create_tables(db):
    db.execute('''CREATE TABLE IF NOT EXISTS avisos (
        id INTEGER PRIMARY KEY, clave TEXT NOT NULL UNIQUE, tipo TEXT NOT NULL, titulo TEXT NOT NULL,
        comercio TEXT NOT NULL DEFAULT '', motivo TEXT NOT NULL DEFAULT '', liga TEXT NOT NULL,
        job_id TEXT NOT NULL DEFAULT '', creado TEXT NOT NULL, leido INTEGER NOT NULL DEFAULT 0,
        visible INTEGER NOT NULL DEFAULT 1, entregado INTEGER NOT NULL DEFAULT 0, correo_at INTEGER NOT NULL DEFAULT 0)''')
    db.execute('CREATE INDEX IF NOT EXISTS avisos_sin_leer ON avisos(visible, leido)')


# ---------------------------------------------------------------- settings

def _settings_path(root) -> Path:
    return Path(root) / 'avisos.json'


def ajustes(root) -> dict:
    try:
        saved = read_json(_settings_path(root), {})
    except ValueError:
        saved = {}
    saved = saved if isinstance(saved, dict) else {}
    tipos = saved.get('tipos') if isinstance(saved.get('tipos'), dict) else {}
    canales = saved.get('canales') if isinstance(saved.get('canales'), dict) else {}
    destino = saved.get('correo_destino')
    return {'tipos': {k: tipos.get(k, v) is True for k, v in DEFAULTS['tipos'].items()},
            'canales': {k: canales.get(k, v) is True for k, v in DEFAULTS['canales'].items()},
            'correo_destino': destino if destino in {'cuenta', 'perfil'} else DEFAULTS['correo_destino']}


def guardar_ajustes(root, cambios: dict) -> dict:
    with SETTINGS_LOCK:
        current = ajustes(root)
        for group in ('tipos', 'canales'):
            values = cambios.get(group) or {}
            if not isinstance(values, dict):
                raise ValueError('Ajustes de avisos inválidos.')
            for name, value in values.items():
                if name not in current[group] or not isinstance(value, bool):
                    raise ValueError('Ajustes de avisos inválidos.')
                current[group][name] = value
        if 'correo_destino' in cambios and cambios['correo_destino'] is not None:
            if cambios['correo_destino'] not in {'cuenta', 'perfil'}:
                raise ValueError('Elige tu cuenta de correo o el correo de tu perfil.')
            current['correo_destino'] = cambios['correo_destino']
        write_private(_settings_path(root), current)
        return current


# ---------------------------------------------------------------- email to oneself

class CorreoPropio:
    """Email only to the person's own address: the connected account or the profile email."""

    def __init__(self, root, senders=None):
        self.root = Path(root)
        self._senders = senders

    def disponible(self) -> bool:
        from .correo_envio import can_send
        try:
            return can_send(self.root)
        except Exception:
            return False

    def destino(self, choice: str) -> str:
        from . import buzon
        account = None
        try:
            account = buzon.load_account(self.root)
        except Exception:
            account = None
        try:
            profile = read_json(self.root / 'profile.json', {}) or {}
        except ValueError:
            profile = {}
        own = [(account or {}).get('address') or (account or {}).get('user') or '', profile.get('email') or '']
        ordered = own if choice == 'cuenta' else own[::-1]
        for value in ordered:
            try:
                return buzon.valid_address(value)
            except ValueError:
                continue
        return ''

    def enviar(self, choice: str, subject: str, body: str) -> bool:
        if not self.disponible():
            return False
        address = self.destino(choice)
        if not address:
            return False
        from .correo_envio import default_sender
        sender = (self._senders or (lambda: default_sender(self.root)))()
        try:
            message = EmailMessage()
            if sender.address:
                message['From'] = sender.address
            message['To'] = address
            message['Subject'] = subject
            message['Date'] = formatdate(localtime=True)
            domain = sender.address.rpartition('@')[2] if sender.address else 'facturai.invalid'
            message['Message-ID'] = make_msgid('facturai-aviso', domain=domain or 'facturai.invalid')
            message.set_content(body)
            sender.prepare(address)
            sender.deliver(message.as_bytes(policy=policy.SMTP))
            return True
        finally:
            sender.close()


# ---------------------------------------------------------------- notices

class Avisos:
    def __init__(self, store, *, push: PushDevices | None = None, correo: CorreoPropio | None = None,
                 panel_url: str = ''):
        self.store = store
        self.root = store.root
        self.push = push or PushDevices(store)
        self.correo = correo or CorreoPropio(self.root)
        self.panel_url = panel_url.rstrip('/')
        with store.connect() as db:
            create_tables(db)

    # The ledger row is written first (it is also what prevents a second notice for the same change);
    # delivery happens after the transaction that recorded it.
    def registrar(self, db, tipo: str, *, clave: str, job_id: str = '', comercio: str = '', motivo: str = '',
                  pantalla: str = 'bandeja') -> dict | None:
        if tipo not in TIPOS:
            raise ValueError('Tipo de aviso desconocido.')
        settings = ajustes(self.root)
        if not settings['tipos'][tipo]:
            return None
        job_id = job_id if job_id and TICKET_ID.fullmatch(job_id) else ''
        row = {'clave': clave[:200], 'tipo': tipo, 'titulo': TIPOS[tipo], 'comercio': limpiar(comercio, 40),
               'motivo': limpiar(motivo, 90), 'liga': liga(job_id, pantalla), 'job_id': job_id, 'creado': now(),
               'visible': int(settings['canales']['panel'])}
        cursor = db.execute('''INSERT OR IGNORE INTO avisos(clave,tipo,titulo,comercio,motivo,liga,job_id,creado,visible)
            VALUES(:clave,:tipo,:titulo,:comercio,:motivo,:liga,:job_id,:creado,:visible)''', row)
        if cursor.rowcount != 1:
            return None
        return {**row, 'id': cursor.lastrowid}

    def entregar(self, aviso: dict) -> dict:
        """Push and email for one recorded notice. Never raises."""
        outcome = {'navegador': None, 'correo': None}
        try:
            settings = ajustes(self.root)
            body = f"{aviso['comercio']}: {aviso['motivo']}" if aviso['comercio'] else aviso['motivo']
            if settings['canales']['navegador']:
                try:
                    outcome['navegador'] = self.push.send(
                        {'id': aviso['id'], 'titulo': aviso['titulo'], 'cuerpo': body, 'liga': aviso['liga'],
                         'etiqueta': (aviso['job_id'] or aviso['tipo'])[:64]}, urgent=aviso['tipo'] in URGENTES)
                except Exception:
                    _LOG.warning('No se enviaron los avisos del navegador.')
            if settings['canales']['correo'] and self._email_allowed():
                try:
                    link = (self.panel_url + aviso['liga']) if self.panel_url else 'tu panel de FACTURAI'
                    text = (f"{aviso['titulo']}\n\n{body}\n\nAbre {link} para verlo.\n\n"
                            'Este aviso lo envía tu propio FACTURAI. Puedes apagarlo en Ajustes.')
                    sent = self.correo.enviar(settings['correo_destino'], 'FACTURAI · ' + aviso['titulo'], text)
                    outcome['correo'] = sent
                    if sent:
                        with self.store.connect() as db:
                            db.execute('UPDATE avisos SET correo_at=? WHERE id=?', (int(time.time()), aviso['id']))
                except Exception:
                    outcome['correo'] = False
                    _LOG.warning('No se envió el aviso por correo.')
        except Exception:
            _LOG.warning('No se completó un aviso.')
        finally:
            try:
                with self.store.connect() as db:
                    db.execute('UPDATE avisos SET entregado=1 WHERE id=?', (aviso.get('id'),))
            except Exception:
                pass
        return outcome

    def _email_allowed(self) -> bool:
        with self.store.connect() as db:
            sent = db.execute('SELECT COUNT(*) FROM avisos WHERE correo_at>?', (int(time.time()) - 3600,)).fetchone()[0]
        return sent < EMAILS_PER_HOUR

    def avisar(self, tipo: str, *, clave: str, job_id: str = '', comercio: str = '', motivo: str = '',
               pantalla: str = 'bandeja') -> dict | None:
        """Central entry point: record once by ``clave`` and deliver. Returns the notice or None."""
        try:
            with self.store.connect() as db:
                aviso = self.registrar(db, tipo, clave=clave, job_id=job_id, comercio=comercio, motivo=motivo,
                                       pantalla=pantalla)
        except Exception:
            _LOG.warning('No se pudo registrar un aviso.')
            return None
        if aviso:
            self.entregar(aviso)
        return aviso

    def pendientes_de_entrega(self, max_age: int = 900) -> None:
        """After a restart, deliver notices recorded moments before it; older ones are only listed."""
        try:
            with self.store.connect() as db:
                rows = [dict(r) for r in db.execute('SELECT * FROM avisos WHERE entregado=0 ORDER BY id')]
            limit = time.time() - max_age
            from datetime import datetime
            for row in rows:
                try:
                    recent = datetime.fromisoformat(row['creado']).timestamp() >= limit
                except ValueError:
                    recent = False
                if recent:
                    self.entregar(row)
                else:
                    with self.store.connect() as db:
                        db.execute('UPDATE avisos SET entregado=1 WHERE id=?', (row['id'],))
        except Exception:
            _LOG.warning('No se revisaron los avisos pendientes.')

    # ------------------------------------------------------------ panel list

    def lista(self, limit: int = 50) -> dict:
        with self.store.connect() as db:
            rows = db.execute('''SELECT id,tipo,titulo,comercio,motivo,liga,job_id,creado,leido FROM avisos
                WHERE visible=1 ORDER BY id DESC LIMIT ?''', (limit,)).fetchall()
        return {'avisos': [{**dict(r), 'leido': bool(r['leido'])} for r in rows], 'sin_leer': self.sin_leer()}

    def sin_leer(self) -> int:
        with self.store.connect() as db:
            return db.execute('SELECT COUNT(*) FROM avisos WHERE visible=1 AND leido=0').fetchone()[0]

    def marcar_leidos(self, ids: list[int] | None = None, todos: bool = False) -> int:
        with self.store.connect() as db:
            if todos:
                db.execute('UPDATE avisos SET leido=1 WHERE leido=0')
            elif ids:
                db.executemany('UPDATE avisos SET leido=1 WHERE id=?', [(int(i),) for i in ids])
        return self.sin_leer()

    def prueba(self) -> dict:
        """A test notice to this person's own devices only; it is not listed or emailed."""
        return self.push.send({'id': 0, 'titulo': 'Aviso de prueba',
                               'cuerpo': 'Así te avisaré cuando un ticket te necesite.', 'liga': '/#ajustes',
                               'etiqueta': 'prueba'}, urgent=True)
