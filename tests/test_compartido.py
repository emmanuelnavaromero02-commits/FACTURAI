"""The folder the accounts of one installation share: where it is, who shares it and what fits in it.

Nothing here reaches the network. Every person is made up.
"""
import asyncio
import os
import tempfile
import unittest
from pathlib import Path
from urllib.parse import unquote, urlsplit

from acceso_dobles import ORIGIN as PANEL, make_accounts, make_entrance, quiet_app, use_master_key
from compartido_dobles import ANA
from facturador import compartido
from facturador.catalog import CATALOG
from facturador.entrada import Entrance, Settings, settings_from_env
from facturador.registro_datos import guardar_datos


class SharedFolderTests(unittest.TestCase):
    def setUp(self):
        self.base = Path(self.enterContext(tempfile.TemporaryDirectory()))
        self.addCleanup(compartido.reset)

    def test_single_person_shares_nothing(self):
        root = self.base / 'datos'
        root.mkdir()
        self.assertEqual(compartido.shared_root(root), root)
        self.assertFalse(compartido.shares(root))
        self.assertEqual(list(self.base.iterdir()), [root])
        # Nor does a folder that only looks like an account of somebody else's installation.
        compartido.enable(self.base / 'otra' / 'usuarios', self.base / 'otra' / 'compartido')
        lookalike = self.base / 'usuarios' / ('a' * 16)
        lookalike.mkdir(parents=True)
        for folder in (root, lookalike, self.base / 'otra', self.base / 'otra' / 'usuarios'):
            self.assertEqual(compartido.shared_root(folder), folder)

    def test_accounts_of_one_installation_share_one_private_folder(self):
        users, shared = self.base / 'usuarios', self.base / 'compartido'
        self.assertEqual(compartido.enable(users, shared), shared.resolve())
        self.assertTrue(shared.is_dir())
        if os.name != 'nt':
            self.assertEqual(shared.stat().st_mode & 0o777, 0o700)
        ana, beto = users / ('a' * 16), users / ('b' * 16)
        self.assertEqual(compartido.shared_root(ana), shared.resolve())
        self.assertEqual(compartido.shared_root(str(beto)), shared.resolve())
        self.assertTrue(compartido.shares(ana))
        # Only the account folders themselves: nothing deeper, and not the folder that holds them.
        self.assertFalse(compartido.shares(ana / 'downloads'))
        self.assertFalse(compartido.shares(users))
        compartido.disable(users)
        self.assertFalse(compartido.shares(ana))
        compartido.enable(users, shared)
        compartido.reset()
        self.assertFalse(compartido.shares(beto))

    def test_paths_of_words_are_plain_and_whatever_may_carry_a_session_or_a_record_is_not(self):
        plain = ['/', '', '/facturar', '/facturacion/ticket', '/facturacionElectronica-web/views/layout/inicio.do',
                 '/FhAutoFacturacionAdmin/facturacion/nuevaAutoFactura.htm', '/RegistroDocumento.aspx', '/es-mx/facturacion',
                 '/cgi-bin/cfds/cfdsORACLE33', '/ticket/{ticket.ticket_number}/datos', '/solicitud/#/paso',
                 '/cliente/{perfil.email}', '/tienda ejemplo/facturar', '{dato}', '/paso2', '/cfdi40/nueva',
                 '/facturación/inicio', '/factura#/ver', '/v2/facturas', '/autofactura/{ticket.billing_codes[0]}']
        coded = ['/(S(lit3py55t21z5v55vlm25s55))/Facturacion.aspx', '/inicio.do;jsessionid=0A1B2C3D4E5F6A7B',
                 '/t/9f8a7b6c5d4e', '/factura/UEVQSjgwMDEwMUFCQ3wxMjM0NXwyNTAuMDA=', '/s/eyJhbGciOiJIUzI1NiIsInR5cCI6IkpXVCJ9',
                 '/f/AbC123xyz', '/r/QWxhZGRpbjpvcGVuIHNlc2FtZQ', '/x/kJhGfDsQwErTy', '/c/alguien@correo.example',
                 '/a?b', '/q/abc#def', '/q/ab12cd', '/z/xkcdqwrtzpsdfghjklmn', '/t/TKT#Q', '/k/a1b2c3',
                 '/x/' + 'a' * 41, '/a/b:c', '/u/%7Bx', '/x' * 151, None, 5]
        for path in plain:
            self.assertTrue(compartido.plain_path(path), path)
        for path in coded:
            self.assertFalse(compartido.plain_path(path), path)
        for key, item in CATALOG.items():
            self.assertTrue(compartido.plain_path(unquote(urlsplit(item['url']).path)), key)

    def test_personal_words_come_from_the_profile_and_from_what_was_saved_for_registrations(self):
        root = self.base / 'datos'
        root.mkdir()
        self.assertEqual(compartido.personal_words(root), set())
        (root / 'profile.json').write_text(ANA.model_dump_json(), encoding='utf-8')
        guardar_datos(root, {'calle': 'Avenida Imaginaria', 'colonia': 'Jardín del Río', 'estado': 'Jalisco',
                             'pais': 'México', 'telefono': '5500000001'})
        self.assertEqual(compartido.personal_words(root),
                         {'ficticia', 'primera', 'avenida', 'imaginaria', 'jardin', 'jalisco'})
        # What cannot be read is not guessed: whoever asks shares nothing.
        (root / 'profile.json').write_text('{esto no es json', encoding='utf-8')
        self.assertIsNone(compartido.personal_words(root))


class EntranceTests(unittest.IsolatedAsyncioTestCase):
    """The entrance for several customers is what makes its accounts share a folder."""

    async def asyncSetUp(self):
        folder = tempfile.TemporaryDirectory()
        self.addCleanup(folder.cleanup)
        # Threads of the instances may outlive their tasks; the folder goes only after them (see test_instancias).
        self.addAsyncCleanup(asyncio.get_running_loop().shutdown_default_executor)
        self.root = Path(folder.name)
        use_master_key(self)
        self.addCleanup(compartido.reset)

    def opened(self, gate):
        self.addCleanup(gate._hashing.shutdown)
        self.addAsyncCleanup(gate.instances.close)
        return gate

    async def test_every_account_the_entrance_starts_shares_one_folder_of_the_installation(self):
        gate = self.opened(make_entrance(self.root))
        shared = (self.root / 'compartido').resolve()
        self.assertTrue(shared.is_dir())
        self.assertFalse((self.root / 'usuarios').exists())  # Still nobody's data.
        for owner in ('a' * 16, 'b' * 16):
            instance = await gate.instances.enter(owner)
            gate.instances.leave(instance)
            self.assertEqual(compartido.shared_root(instance.app.state.store.root), shared)
            self.assertEqual(compartido.shared_root(instance.root), shared)
        # The folders next to the accounts are not accounts.
        for folder in (self.root, self.root / 'acceso', shared):
            self.assertFalse(compartido.shares(folder), folder)

    async def test_sharing_can_be_switched_off_and_then_every_account_works_with_what_it_knows(self):
        base = {'FACTURADOR_MODO': 'usuarios', 'FACTURADOR_ORIGEN_PUBLICO': PANEL}
        for value, expected in ((None, True), ('', True), ('1', True), ('0', False), (' 0 ', False)):
            env = base if value is None else {**base, 'FACTURADOR_COMPARTIDO': value}
            self.assertIs(settings_from_env(env).share, expected, value)
        compartido.enable(self.root / 'usuarios', self.root / 'compartido')  # As an earlier start of this folder left it.
        (self.root / 'compartido').rmdir()
        gate = self.opened(Entrance(Settings(public_origin=PANEL, data_root=self.root, share=False),
                                    make_accounts(self.root), factory=quiet_app))
        instance = await gate.instances.enter('a' * 16)
        gate.instances.leave(instance)
        self.assertFalse(compartido.shares(instance.app.state.store.root))
        self.assertFalse((self.root / 'compartido').exists())


if __name__ == '__main__':
    unittest.main()
