"""Two accounts of one installation in front of the same made-up portal, each with its own Chrome.

What the first account learns the second repeats without the model, through the same emission
lock, and nothing of either of them is left in the folder they share. Pages are served from
memory and the «model» is a function: no merchant portal and no AI service is contacted.
"""
import asyncio
import io
import os
import sqlite3
import tempfile
import unittest
import uuid
import zipfile
from contextlib import closing
from pathlib import Path
from unittest.mock import AsyncMock, patch

from facturador import compartido, portal_directory as directory
from facturador.db import Store
from facturador.models import Profile
from facturador.runner import Runner
from simulated_portal import CHROME, ORIGIN, Brain, Portal, ticket
from test_core import pdf_bytes, xml_bytes
from test_screen_plans import kinds, metrics

ISSUER = 'TEJ101010AB1'  # Fictitious RFC of the made-up merchant.
ANA = Profile(rfc='AAAA010101AAA', name='ANA FICTICIA PRIMERA', postal_code='01000', regime='612', cfdi_use='G03',
              email='ana.ficticia@correo-a.example', whatsapp_phone='+525500000001')
BETO = Profile(rfc='BBBB020202BBB', name='BETO INVENTADO SEGUNDO', postal_code='02000', regime='605', cfdi_use='G03',
               email='beto.inventado@correo-b.example', whatsapp_phone='+525500000002')
MEMORY, DIRECTORY = 'memoria_portales.sqlite', 'directorio_portales.sqlite'


class OpenPortal(Portal):
    """The made-up portal, issuing each invoice to whoever asked for it."""

    def invoice(self, folio: str) -> tuple[str, bytes]:
        receipt = self.tickets[folio]
        sent = next(item for item in reversed(self.emitted) if item['folio'] == folio)
        fiscal = str(uuid.uuid5(uuid.NAMESPACE_URL, self.origin + '/' + folio)).upper()
        archive = io.BytesIO()
        with zipfile.ZipFile(archive, 'w') as pair:
            pair.writestr('factura.xml', xml_bytes(
                Total=receipt.amount, FormaPago=receipt.payment_code, uuid=fiscal, issuer=ISSUER, Rfc=sent['rfc'],
                Nombre=sent['nombre'], DomicilioFiscalReceptor=sent['cp'], RegimenFiscalReceptor=sent['regimen'],
                UsoCFDI=sent['uso']))
            pair.writestr('factura.pdf', pdf_bytes(fiscal, sent['rfc']))
        return fiscal, archive.getvalue()


class Account:
    """One account: its own data folder, its own engine and its own browser."""

    def __init__(self, root: Path, profile: Profile):
        self.root, self.profile, self.count = root, profile, 0
        root.mkdir(parents=True)
        (root / 'profile.json').write_text(profile.model_dump_json(), encoding='utf-8')
        self.store = Store(root)
        self.runner = Runner(self.store)

    def own(self) -> bytes:
        """The account's own memory and directory, byte by byte."""
        return b''.join(path.read_bytes() for name in (MEMORY, DIRECTORY) for path in sorted(self.root.glob(name + '*')))


@unittest.skipUnless(os.path.isfile(CHROME), 'Requiere Google Chrome oficial')
class TwoAccountsCase(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        # As in test_screen_plans: the loop's debug mode makes a browser session several times slower.
        asyncio.get_running_loop().set_debug(False)
        folder = tempfile.TemporaryDirectory()
        self.addCleanup(folder.cleanup)
        self.base = Path(folder.name)
        self.users, self.shared = self.base / 'usuarios', self.base / 'compartido'
        compartido.enable(self.users, self.shared)
        self.addCleanup(compartido.reset)
        for target, value in (('facturador.runner.resolves_publicly', AsyncMock(return_value=True)),
                              ('facturador.runner.REPLAY_PAUSE', 0), ('facturador.runner.SETTLE_PAUSE', 0.05)):
            patcher = patch(target, value)
            patcher.start()
            self.addCleanup(patcher.stop)
        environment = patch.dict(os.environ, {'FACTURADOR_CHROMIUM_PATH': CHROME})
        environment.start()
        self.addCleanup(environment.stop)
        self.accounts = []
        self.ana = await self.account('a' * 16, ANA)
        self.beto = await self.account('b' * 16, BETO)

    async def account(self, owner: str, profile: Profile) -> Account:
        account = Account(self.users / owner, profile)
        await account.runner.start_browser()
        self.addAsyncCleanup(account.runner.close)
        self.accounts.append(account)
        return account

    async def serve(self, portal):
        for account in self.accounts:
            await account.runner.context.route(portal.origin + '/**', portal.handle)
        return portal

    async def bill(self, account, receipt, brain, *, auto_issue=True, until=None, timeout=90):
        """Run one ticket of that account. With ``until`` it returns when the ticket pauses for that reason."""
        account.count += 1
        key = account.store.add(f'ticket-{account.count}.png', f'sintetico-{account.count}')[0]['id']
        account.store.save_receipt(key, receipt, ready=True)
        with patch('facturador.runner.AI.next_action', side_effect=brain):
            account.runner.schedule(key, account.profile, auto_issue=auto_issue, confirmed_url=receipt.portal_url)
            task = account.runner.job_tasks[key]
            async with asyncio.timeout(timeout):
                if until:
                    while account.runner.pending.get(key, {}).get('kind') != until:
                        if task.done():
                            raise AssertionError((account.store.get(key)['message'], account.store.events(key)[-6:]))
                        await asyncio.sleep(.03)
                else:
                    await task
        return key

    async def verified(self, account, receipt, brain):
        key = await self.bill(account, receipt, brain)
        row = account.store.get(key)
        self.assertEqual(row['status'], 'verified', (row['message'], account.store.events(key)[-8:]))
        return key

    def stored(self) -> bytes:
        """Every byte of every file of the folder the accounts share."""
        return b''.join(path.read_bytes() for path in sorted(self.shared.rglob('*')) if path.is_file())

    def screens(self) -> dict:
        with closing(sqlite3.connect(self.shared / MEMORY)) as db:
            db.row_factory = sqlite3.Row
            return {row['path']: dict(row) for row in db.execute('SELECT * FROM screens')}


class TwoAccountsTests(TwoAccountsCase):
    async def test_what_one_account_learns_the_other_repeats_without_the_model(self):
        portal = await self.serve(OpenPortal())
        first = Brain()
        ana_key = await self.verified(self.ana, portal.add(ticket('TKT-88231-Q', '2026-11-13', '1137.45')), first)
        self.assertEqual(len(first.calls), 4)
        self.assertEqual({path: (row['successes'], row['accounts']) for path, row in self.screens().items()},
                         {path: (1, 1) for path in ('/', '/datos', '/confirmar', '/emitir')})
        # One account got its invoice there: for anybody else that portal is still only a proposal.
        portal_url = ORIGIN + '/'
        self.assertFalse(directory.invoice_confirmed(self.beto.root, ISSUER, portal_url))
        self.assertEqual(directory.lookup(self.beto.root, rfc=ISSUER)['level'], 'busqueda')

        brain = Brain()
        key = await self.verified(self.beto, portal.add(ticket('TKT-10457-Z', '2026-11-21', '89.50', payment='01')), brain)
        self.assertEqual(brain.calls, [])                                     # not one consultation
        self.assertEqual(portal.lookups[1], {'folio': 'TKT-10457-Z', 'fecha': '21/11/2026', 'total': '89.50'})
        sent = portal.fiscal[1]
        self.assertEqual((sent['rfc'], sent['nombre'], sent['cp'], sent['correo']),
                         (BETO.rfc, BETO.name, BETO.postal_code, BETO.email))
        self.assertEqual((sent['regimen'], sent['uso'], sent['pago']), ('605', 'G03', '01'))
        self.assertEqual((portal.emitted[1]['folio'], portal.emitted[1]['rfc']), ('TKT-10457-Z', BETO.rfc))
        events = kinds(self.beto.store, key)
        self.assertEqual((events.count('memory_replay'), events.count('issue_attempt')), (4, 1))
        self.assertEqual(metrics(self.beto.store, key)[0] | {'seconds': 0},
                         {'ai_calls': 0, 'actions': 11, 'replayed_actions': 11, 'seconds': 0, 'outcome': 'verified'})
        self.assertEqual({path: (row['successes'], row['accounts'], row['active']) for path, row in self.screens().items()},
                         {path: (2, 2, 1) for path in ('/', '/datos', '/confirmar', '/emitir')})

        # Nothing of either person, of their tickets or of their accounts is in the shared folder.
        stored = self.stored()
        private = [ANA.rfc, ANA.email, ANA.name, 'FICTICIA', 'PRIMERA', ANA.postal_code, ANA.whatsapp_phone, '5500000001',
                   BETO.rfc, BETO.email, BETO.name, 'INVENTADO', 'SEGUNDO', BETO.postal_code, BETO.whatsapp_phone,
                   '5500000002', 'TKT-88231-Q', 'TKT-10457-Z', '88231', '10457', '1137.45', '89.50', '13/11/2026',
                   '21/11/2026', '2026-11-13', '2026-11-21', ana_key, key, self.ana.root.name, self.beto.root.name,
                   portal.invoice('TKT-88231-Q')[0], portal.invoice('TKT-10457-Z')[0]]
        for value in private:
            for form in {value, value.lower(), value.upper()}:
                self.assertNotIn(form.encode(), stored, value)
        for common in (b'$perfil.rfc', b'$perfil.email', b'$ticket.ticket_number', b'$ticket.date|dd/mm/aaaa',
                       ORIGIN.encode(), b'Buscar ticket', b'Emitir factura', ISSUER.encode(), b'tienda ejemplo'):
            self.assertIn(common, stored)
        # And neither account holds anything of the other in its own memory and directory.
        for value in (ANA.rfc, ANA.email, 'FICTICIA', 'TKT-88231-Q'):
            self.assertNotIn(value.encode(), self.beto.own())
        for value in (BETO.rfc, BETO.email, 'INVENTADO', 'TKT-10457-Z'):
            self.assertNotIn(value.encode(), self.ana.own())

        # Two accounts got a verified invoice from that portal: a third one needs no domain question.
        third = self.users / ('c' * 16)
        third.mkdir()
        entry = directory.lookup(third, rfc=ISSUER, merchant='Tienda Ejemplo')
        self.assertEqual((entry['url'], entry['level'], entry['matched_by']), (portal_url, 'factura', 'rfc'))
        self.assertTrue(directory.invoice_confirmed(third, ISSUER, portal_url))

    async def test_shared_recipe_goes_through_the_same_emission_lock(self):
        portal = await self.serve(OpenPortal())
        await self.verified(self.ana, portal.add(ticket('TKT-88231-Q', '2026-11-13', '1137.45')), Brain())
        # The confirmation stops showing the amount: the page no longer proves the ticket. Automatic
        # emission is allowed, and still the repeated steps stop at the lock and ask the person.
        portal.show_amount = False
        brain = Brain()
        key = await self.bill(self.beto, portal.add(ticket('TKT-20001-A', '2026-11-22', '310.00')), brain, until='issue')
        self.assertEqual(brain.calls, [])
        self.assertEqual(len(portal.emitted), 1)
        self.assertFalse(self.beto.store.get(key)['issue_attempted'])
        self.assertEqual((portal.fiscal[-1]['folio'], portal.fiscal[-1]['rfc']), ('TKT-20001-A', BETO.rfc))
        await self.beto.runner.stop(key)
        self.assertEqual(len(portal.emitted), 1)
        # Without automatic emission the lock asks as well, and the person's approval is what emits.
        portal.show_amount = True
        key = await self.bill(self.beto, portal.add(ticket('TKT-20002-B', '2026-11-23', '512.75')), brain, auto_issue=False,
                              until='issue')
        self.assertEqual(brain.calls, [])
        self.assertEqual(len(portal.emitted), 1)
        self.assertFalse(self.beto.store.get(key)['issue_attempted'])
        with patch('facturador.runner.AI.next_action', side_effect=brain):
            self.beto.runner.decide(key, 'approve')
            await asyncio.wait_for(self.beto.runner.job_tasks[key], 60)
        self.assertEqual(self.beto.store.get(key)['status'], 'verified', self.beto.store.events(key)[-8:])
        self.assertEqual((len(portal.emitted), portal.emitted[1]['rfc']), (2, BETO.rfc))
        self.assertEqual(kinds(self.beto.store, key).count('issue_attempt'), 1)

    async def test_choice_in_a_list_is_solved_again_by_each_account_and_never_shared(self):
        # The lookup has a payment list the fiscal rules do not recognize and a plain list of the portal.
        portal = await self.serve(OpenPortal(choices=True))
        first = Brain()
        await self.verified(self.ana, portal.add(ticket('TKT-88231-Q', '2026-11-13', '1137.45')), first)
        self.assertEqual(len(first.calls), 4)
        self.assertEqual((portal.lookups[0]['como'], portal.lookups[0]['tipo']), ('2', 'T'))
        self.assertEqual(set(self.screens()), {'/datos', '/confirmar', '/emitir'})
        brain = Brain()
        key = await self.verified(self.beto, portal.add(ticket('TKT-10457-Z', '2026-11-21', '89.50', payment='01')), brain)
        # His model was asked for the one screen with a choice; the other three came from what Ana learned.
        self.assertEqual((len(brain.calls), brain.seen('¿Cómo pagaste?')), (1, 1))
        events = kinds(self.beto.store, key)
        self.assertEqual((events.count('memory_miss'), events.count('memory_replay')), (1, 3))
        self.assertEqual((portal.lookups[1]['como'], portal.lookups[1]['tipo'], portal.fiscal[1]['pago']), ('1', 'T', '01'))
        self.assertEqual(set(self.screens()), {'/datos', '/confirmar', '/emitir'})
        stored = self.stored()
        for choice in ('Ticket de caja', 'Tarjeta de débito', 'Efectivo', '$sat.payment'):
            self.assertNotIn(choice.encode(), stored)
        self.assertIn('=Ticket de caja'.encode(), self.ana.own())
        # Each of them repeats the choice with their own next ticket, as a single person does.
        for account, receipt in ((self.ana, ticket('TKT-12001-C', '2026-11-18', '64.20')),
                                 (self.beto, ticket('TKT-12002-D', '2026-11-19', '75.30', payment='04'))):
            again = Brain()
            await self.verified(account, portal.add(receipt), again)
            self.assertEqual(again.calls, [])
        self.assertEqual((portal.lookups[3]['como'], portal.fiscal[3]['pago'], portal.fiscal[3]['rfc']), ('3', '04', BETO.rfc))


if __name__ == '__main__':
    unittest.main()
