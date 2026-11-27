"""Shared doubles for the tests of what the accounts of one installation share.

Data folders stand for the accounts. Every person, ticket and merchant is made up, and nothing
here reaches the network.
"""
import sqlite3
import tempfile
import threading
import unittest
from contextlib import closing
from pathlib import Path

from facturador import compartido
from facturador.models import Action, Profile, Receipt, Target
from facturador.portal_memory import Attempt, Known, PortalMemory, describe, screen_signature

ORIGIN = 'https://facturas.tienda-ejemplo.mx'
ISSUER = 'CES101010AB1'  # Fictitious merchant RFC.
PORTAL = 'https://facturas.cafe-estrella.com.mx/emitir'
MEMORY, DIRECTORY = 'memoria_portales.sqlite', 'directorio_portales.sqlite'


def person(letter: str, name: str, postal: str) -> Profile:
    return Profile(rfc=letter * 4 + '010101' + letter * 3, name=name, postal_code=postal, regime='612', cfdi_use='G03',
                   email=f'{name.split()[0].lower()}.{name.split()[1].lower()}@correo-{letter.lower()}.example',
                   whatsapp_phone='+5255000000' + postal[:2])


ANA = person('A', 'ANA FICTICIA PRIMERA', '01000')
BETO = person('B', 'BETO INVENTADO SEGUNDO', '02000')
CARLA = person('C', 'CARLA SUPUESTA TERCERA', '03000')
DANTE = person('D', 'DANTE FINGIDO CUARTO', '04000')
ELSA = person('E', 'ELSA APARENTE QUINTA', '05000')
FIDEL = person('F', 'FIDEL SIMULADO SEXTO', '06000')


def ticket(folio: str, amount: str = '1137.45', day: str = '2026-11-13', codes=()) -> Receipt:
    return Receipt(provider='portal_ticket', portal_url=ORIGIN + '/', merchant='Tienda Ejemplo', amount=amount, date=day,
                   ticket_number=folio, billing_codes=list(codes), payment_code='28')


FIRST, SECOND, THIRD = ticket('TKT-88231-Q', codes=['WEB-55/7001']), ticket('TKT-10457-Z', '89.50', '2026-11-21'), ticket('TKT-30990-C')
FIELDS = [{'n': 1, 'kind': 'text', 'label': 'Folio del ticket', 'key': 'folio|', 'filled': False},
          {'n': 2, 'kind': 'text', 'label': 'Total pagado', 'key': 'total|', 'filled': False},
          {'n': 3, 'kind': 'lista', 'label': 'Estado', 'key': 'estado|', 'filled': False},
          {'n': 4, 'kind': 'radio', 'label': 'Con desglose', 'key': 'desglose|', 'filled': False},
          {'n': 5, 'kind': 'text', 'label': 'Receptor ' + ANA.rfc, 'key': 'receptor|', 'filled': False}]
SIGNATURE = screen_signature(FIELDS, ['Buscar ticket'])
SCREEN = (ORIGIN, '/buscar', SIGNATURE)
# The two steps that solve that screen, as the memory keeps them.
FOLIO = {'kind': 'fill', 'target': {'by': 'campo', 'kind': 'text', 'label': 'folio del ticket', 'ordinal': 0, 'count': 1},
         'value': '$ticket.ticket_number', 'risk': 'normal'}
SEARCH = {'kind': 'click', 'target': {'by': 'role', 'name': 'Buscar ticket', 'role': 'button', 'exact': True},
          'value': None, 'risk': 'normal'}


def act(kind, name, value=None, by='label', role=None):
    return Action(kind=kind, target=Target(by=by, name=name, role=role, exact=True), value=value, reason='paso',
                  risk='normal')


def button(name):
    return act('click', name, by='role', role='button')


def rows(path: Path, table: str) -> list[dict]:
    with closing(sqlite3.connect(path)) as db:
        db.row_factory = sqlite3.Row
        return [dict(row) for row in db.execute('SELECT * FROM ' + table)]


def tables(path: Path) -> set[str]:
    with closing(sqlite3.connect(path)) as db:
        return {row[0] for row in db.execute("SELECT name FROM sqlite_master WHERE type='table'")}


class Installation(unittest.TestCase):
    """Data folders that stand for the accounts of one installation, and the folder they share."""

    def setUp(self):
        self.base = Path(self.enterContext(tempfile.TemporaryDirectory()))
        self.users, self.shared = self.base / 'usuarios', self.base / 'compartido'
        compartido.enable(self.users, self.shared)
        self.addCleanup(compartido.reset)
        self.numbers = iter(range(1, 1000))

    def account(self, profile: Profile | None = None) -> Path:
        root = self.users / f'{next(self.numbers):016x}'
        root.mkdir(parents=True)
        if profile is not None:
            (root / 'profile.json').write_text(profile.model_dump_json(), encoding='utf-8')
        return root

    def stored(self) -> bytes:
        """Every byte of every file of the shared folder."""
        return b''.join(path.read_bytes() for path in sorted(self.shared.rglob('*')) if path.is_file())

    def assert_absent(self, *private: str):
        stored = self.stored()
        for value in private:
            for form in {value, value.lower(), value.upper()}:
                self.assertNotIn(form.encode(), stored, value)

    def run_together(self, work, count: int) -> list[str]:
        """Start ``count`` threads at the same instant and return what each one raised."""
        barrier, failures = threading.Barrier(count), []

        def task(number):
            try:
                barrier.wait(30)
                work(number)
            except Exception as exc:  # Collected: a thread that dies silently would hide a lost write.
                failures.append(repr(exc))

        threads = [threading.Thread(target=task, args=(number,)) for number in range(count)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join(120)
        self.assertFalse(any(thread.is_alive() for thread in threads))
        return failures

    def healthy(self, name: str):
        with closing(sqlite3.connect(self.shared / name)) as db:
            self.assertEqual(db.execute('PRAGMA integrity_check').fetchone()[0], 'ok')
            self.assertEqual(db.execute('PRAGMA journal_mode').fetchone()[0], 'wal')


class SharedMemoryCase(Installation):
    """Two accounts and what an attempt of any of them does with the memory."""

    def setUp(self):
        super().setUp()
        self.ana, self.beto = self.account(ANA), self.account(BETO)

    def solved(self, root, profile, receipt, screen=SCREEN, extra=()):
        """An attempt of that account in which the model solved the screen: the folio and its button."""
        attempt = Attempt(PortalMemory(root), Known(profile, receipt))
        attempt.step(screen, act('fill', 'Folio del ticket', receipt.ticket_number), describe(FIELDS, 1), 'normal', 'ia')
        for action, field in extra:
            attempt.step(screen, action, field, 'normal', 'ia')
        attempt.step(screen, button('Buscar ticket'), None, 'normal', 'ia')
        return attempt

    def recalled(self, root, profile, receipt, screen=SCREEN, family=False):
        return Attempt(PortalMemory(root), Known(profile, receipt)).recall(screen, family)

    def repeated(self, root, profile, receipt, screen=SCREEN):
        """An attempt of that account that repeated what the memory had for the screen."""
        attempt = Attempt(PortalMemory(root), Known(profile, receipt))
        steps, family = attempt.recall(screen, True)
        attempt.replay_started(screen, steps, family)
        attempt.step(screen, act('fill', 'Folio del ticket', receipt.ticket_number), describe(FIELDS, 1), 'normal', 'memoria')
        attempt.replay_done(screen)
        return attempt

    def failed(self, root, profile, receipt, screen=SCREEN):
        """One failed attempt with what the memory has. None when it has nothing to repeat."""
        attempt = Attempt(PortalMemory(root), Known(profile, receipt))
        steps, family = attempt.recall(screen, True)
        if not steps:
            return None
        attempt.replay_started(screen, steps, family)
        return attempt.replay_failed(screen)

    def row(self, screen=SCREEN) -> dict | None:
        found = [row for row in rows(self.shared / MEMORY, 'screens')
                 if (row['origin'], row['path'], row['signature']) == tuple(screen)]
        return found[0] if found else None
