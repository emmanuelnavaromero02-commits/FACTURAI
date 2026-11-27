"""A link that only navigates, a file the portal asks for and fiscal lists that load late.

The real runner drives Chrome against pages served from memory and the «model» is a function:
no merchant portal and no AI service is contacted.
"""
import io
import json
import sys
import unittest
from pathlib import Path
from unittest.mock import patch
from urllib.parse import urlsplit

from PIL import Image

sys.path.insert(0, str(Path(__file__).resolve().parent))

from facturador.auth import write_private
from facturador.models import Action, Plan
from facturador.portal_accounts import credential_file
from simulated_portal import ORIGIN, PAGE, Brain, Portal, WAIT, step, ticket
from test_core import PROFILE
from test_screen_plans import PortalCase, kinds, metrics

HTML = 'text/html; charset=utf-8'
COVER = '<h1>Librería de Prueba</h1><p>Bienvenido. Aquí puedes facturar tu compra.</p>%s'
LINK = '<a href="/consulta">Ir a facturar</a>'
OPERATOR = 'https://cfdi.operador-ejemplo.mx/emisor/libreria'
HINT = 'usa upload con value="ticket"'


class CoverPortal(Portal):
    """The billing portal of the engine tests behind a cover page; its form lives in /consulta."""

    def __init__(self, cover=COVER % LINK, **options):
        super().__init__(**options)
        self.cover, self.requests = cover, []

    async def handle(self, route):
        request = route.request
        path = urlsplit(request.url).path
        if path != '/favicon.ico':
            self.requests.append((request.method, path))
        if path == '/':
            await route.fulfill(content_type=HTML, body=PAGE % self.cover)
        elif path == '/consulta':
            await route.fulfill(content_type=HTML, body=self.lookup())
        elif path == '/emitir-directo':
            await route.fulfill(content_type=HTML, body=PAGE % '<h1>Solicitud recibida</h1>')
        else:
            await super().handle(route)


class CoverBrain(Brain):
    """Presses the control of the cover page, labelled as the test says, and then plans like any model."""

    def __init__(self, role='link', risk='normal'):
        super().__init__()
        self.role, self.risk = role, risk

    def plan(self, profile, receipt, view):
        if 'Bienvenido' in view['estado_visible']:
            return Plan(steps=[step('click', self.role, 'Ir a facturar', risk=self.risk)])
        return super().plan(profile, receipt, view)


class NavigationLinkTests(PortalCase):
    async def test_cover_link_named_like_an_emission_is_followed_and_then_repeated_from_memory(self):
        portal = await self.serve(CoverPortal())
        brain = CoverBrain()
        key = await self.bill(portal.add(ticket('TKT-88231-Q', '2026-11-13', '1137.45')), brain)
        row = self.store.get(key)
        self.assertEqual(row['status'], 'verified', (row['message'], self.store.events(key)[-8:]))
        events = kinds(self.store, key)
        # Nobody was asked, and the only emission attempt is the real one, with its proof on screen.
        self.assertNotIn('needs_user', events)
        self.assertEqual(events.count('issue_attempt'), 1)
        self.assertEqual((brain.seen('Bienvenido'), len(brain.calls)), (1, 5))
        # The link asked for its page and nothing else: nothing was sent before the lookup.
        self.assertEqual(portal.requests[:3], [('GET', '/'), ('GET', '/consulta'), ('GET', '/datos')])
        self.assertEqual(len(portal.emitted), 1)
        # Learned as an ordinary step, it is repeated for the next ticket through the same check.
        brain = CoverBrain()
        key = await self.bill(portal.add(ticket('TKT-10457-Z', '2026-11-21', '89.50', payment='01')), brain)
        self.assertEqual(self.store.get(key)['status'], 'verified', self.store.events(key)[-8:])
        self.assertEqual(brain.calls, [])
        events = kinds(self.store, key)
        self.assertEqual((events.count('memory_replay'), events.count('issue_attempt')), (5, 1))
        self.assertNotIn('needs_user', events)
        self.assertEqual(portal.emitted[1]['folio'], 'TKT-10457-Z')

    async def test_submit_button_with_the_same_words_keeps_the_emission_gate(self):
        form = '<form method="post" action="/emitir-directo"><button>Ir a facturar</button></form>'
        portal = await self.serve(CoverPortal(COVER % form))
        brain = CoverBrain('button')
        key = await self.bill(portal.add(ticket('TKT-40021-B', '2026-11-14', '250.00')), brain, until='issue')
        # The page shows neither the RFC, the folio nor the amount: it is never sent without the person.
        self.assertEqual(portal.requests, [('GET', '/')])
        self.assertFalse(self.store.get(key)['issue_attempted'])
        self.assertEqual(len(brain.calls), 1)
        await self.runner.stop(key)
        self.assertNotIn(('POST', '/emitir-directo'), portal.requests)

    async def test_link_the_model_calls_an_emission_keeps_the_gate(self):
        portal = await self.serve(CoverPortal())
        key = await self.bill(portal.add(ticket('TKT-40022-C', '2026-11-14', '251.00')), CoverBrain(risk='issue'),
                              until='issue')
        self.assertEqual(portal.requests, [('GET', '/')])
        await self.runner.stop(key)

    async def check(self, body, *, name='Ir a facturar', role='link', risk='normal', path='/portada', before=None):
        """Whether the runner takes a click on that control of that page for plain navigation."""
        receipt = ticket('TKT-55120-R', '2026-11-20', '312.40')
        key = self.add(receipt)
        if before:
            before(key)
        page = await self.runner.context.new_page()
        self.addAsyncCleanup(page.close)
        await page.goto(ORIGIN + path)
        await page.set_content(PAGE % body)
        return await self.runner.navigation_link(key, page, step('click', role, name, risk=risk), PROFILE, receipt)

    async def test_only_a_plain_link_on_a_page_with_nothing_to_send_is_navigation(self):
        async def blank(route):
            await route.fulfill(content_type=HTML, body=PAGE % '')
        await self.runner.context.route(ORIGIN + '/**', blank)
        link = '<a href="%s">Ir a facturar</a>'
        plain = {
            'otra página del portal': COVER % LINK,
            'operador de facturación en otro dominio': COVER % (link % OPERATOR),
            'pestaña nueva': COVER % '<a href="/consulta" target="_blank">Ir a facturar</a>',
            'texto dentro de otro elemento': COVER % '<a href="/consulta"><span>Ir a facturar</span></a>',
            'campos vacíos y una lista sin elegir': COVER % (
                '<input aria-label="Buscar"><select aria-label="Sucursal"><option value="">Elige</option></select>' + LINK),
        }
        for name, body in plain.items():
            with self.subTest(name):
                self.assertTrue(await self.check(body))
        doubtful = {
            'botón': COVER % '<button>Ir a facturar</button>',
            'botón que envía un formulario': COVER % '<form method="post" action="/x"><button>Ir a facturar</button></form>',
            'enlace dentro de un formulario': COVER % ('<form method="post" action="/x">' + LINK + '</form>'),
            'enlace con código propio': COVER % '<a href="/consulta" onclick="enviar()">Ir a facturar</a>',
            'enlace que no sale de la página': COVER % '<a href="#">Ir a facturar</a>',
            'enlace a la misma página': COVER % '<a href="/portada#paso">Ir a facturar</a>',
            'enlace de código': COVER % '<a href="javascript:void(0)">Ir a facturar</a>',
            'enlace que hace de botón': COVER % '<a href="/consulta" role="button">Ir a facturar</a>',
            'enlace de descarga': COVER % '<a href="/consulta" download>Ir a facturar</a>',
            'enlace que avisa a otro sitio': COVER % '<a href="/consulta" ping="/aviso">Ir a facturar</a>',
            'enlace a un marco con nombre': COVER % '<a href="/consulta" target="marco">Ir a facturar</a>',
            'destino sin cifrar': COVER % (link % 'http://facturas.tienda-ejemplo.mx/consulta'),
            'destino que no es de facturación': COVER % (link % 'https://www.otro-sitio.mx/promociones'),
            'dirección con el folio': COVER % (link % '/consulta?folio=TKT-55120-R'),
            'dirección con el RFC': COVER % (link % ('/consulta?rfc=' + PROFILE.rfc.lower())),
            'campo ya capturado': COVER % ('<input aria-label="Referencia" value="algo">' + LINK),
            'casilla marcada': COVER % ('<label><input type="checkbox" checked> Recordarme</label>' + LINK),
            'lista con una opción elegida': COVER % (
                '<select aria-label="Tienda"><option value="7">Centro</option></select>' + LINK),
            'página con el folio': COVER % ('<p>Ticket TKT-55120-R</p>' + LINK),
            'página con el importe': COVER % ('<p>Total: $312.40</p>' + LINK),
            'página con el RFC': COVER % ('<p>Receptor ' + PROFILE.rfc + '</p>' + LINK),
            'página con el correo': COVER % ('<p>Sesión de ' + PROFILE.email + '</p>' + LINK),
            'página con el nombre': COVER % ('<p>Hola, ' + PROFILE.name.title() + '</p>' + LINK),
        }
        for name, body in doubtful.items():
            with self.subTest(name):
                role = 'button' if name.startswith(('botón', 'enlace que hace')) else 'link'
                self.assertFalse(await self.check(body, role=role))

    async def test_link_is_an_emission_again_once_the_run_gave_the_portal_anything(self):
        async def blank(route):
            await route.fulfill(content_type=HTML, body=PAGE % '')
        await self.runner.context.route(ORIGIN + '/**', blank)
        body = COVER % LINK
        self.assertTrue(await self.check(body))
        self.assertFalse(await self.check(body, risk='issue'))                       # the model calls it an emission
        self.assertFalse(await self.check(body, before=self.runner.captured.add))    # something was typed or chosen
        self.assertFalse(await self.check(body, before=lambda key: self.runner.pauses.update({key: 1})))
        self.assertFalse(await self.check(body, before=self.runner.explicit_approval.add))

        def exposed(key):
            with self.store.connect() as db:
                db.execute('UPDATE jobs SET human_exposed=1 WHERE id=?', (key,))
        self.assertFalse(await self.check(body, before=exposed))
        # A portal where the program keeps an account of this person may already know what to invoice.
        account = credential_file(self.store.root, ORIGIN, PROFILE.email)
        write_private(account, {'credential': 'cifrada'})
        self.assertFalse(await self.check(body))
        account.unlink()
        self.assertTrue(await self.check(body))


def png(name: str) -> bytes:
    output = io.BytesIO()
    Image.new('RGB', (24, 30), (len(name) * 9 % 255, 120, 60)).save(output, 'PNG')
    return output.getvalue()


class PhotoPortal(Portal):
    """The same portal, whose lookup also demands the photo of the ticket."""

    def lookup(self, error='') -> str:
        return super().lookup(error).replace(
            '<label>Total pagado', '<label>Foto del ticket<input name="foto" type="file" accept="image/*" required>'
                                   '</label><label>Total pagado')


class PhotoBrain(Brain):
    """Attaches the ticket only when the runner tells it how; ``attach=False`` never does."""

    def __init__(self, attach=True):
        super().__init__()
        self.attach = attach

    def plan(self, profile, receipt, view):
        photo = next((line for line in view['campos'] if 'Foto del ticket' in line), None)
        if photo is not None and photo.split('»:', 1)[1].strip().startswith('('):   # no file chosen yet
            if self.attach and HINT in view['ultimo_error']:
                return Plan(steps=[Action(kind='upload', target=None, value='ticket', reason='Lo pide el portal',
                                          risk='normal')])
            return Plan(steps=[WAIT])
        return super().plan(profile, receipt, view)


class AttachmentHintTests(PortalCase):
    def add(self, receipt):
        """Like the base one, with a real photo behind the ticket: the upload sends that file."""
        self.count += 1
        photo = self.root / f'foto-{self.count}.png'
        photo.write_bytes(png(photo.name))
        key = self.store.add(str(photo), f'sintetico-{self.count}')[0]['id']
        self.store.save_receipt(key, receipt, ready=True)
        return key

    async def test_hint_reaches_the_model_and_the_ticket_is_attached_then_repeated_from_memory(self):
        portal = await self.serve(PhotoPortal())
        brain = PhotoBrain()
        key = await self.bill(portal.add(ticket('TKT-88231-Q', '2026-11-13', '1137.45')), brain)
        row = self.store.get(key)
        self.assertEqual(row['status'], 'verified', (row['message'], self.store.events(key)[-8:]))
        # The page that demands the file was shown to the model together with the hint, once.
        self.assertIn(HINT, brain.calls[0]['ultimo_error'])
        self.assertIn('Foto del ticket', json.dumps(brain.calls[0]['campos'], ensure_ascii=False))
        self.assertEqual(sum(HINT in call['ultimo_error'] for call in brain.calls), 1)
        self.assertEqual(portal.lookups[0]['foto'], 'foto-1.png')
        events = kinds(self.store, key)
        self.assertEqual(events.count('attachment_uploaded'), 1)
        self.assertNotIn('needs_user', events)
        # The next ticket attaches its own photo from memory: not one consultation.
        brain = PhotoBrain()
        key = await self.bill(portal.add(ticket('TKT-10457-Z', '2026-11-21', '89.50', payment='01')), brain)
        self.assertEqual(self.store.get(key)['status'], 'verified', self.store.events(key)[-8:])
        self.assertEqual(brain.calls, [])
        self.assertEqual(portal.lookups[1]['foto'], 'foto-2.png')
        self.assertEqual(metrics(self.store, key)[0]['ai_calls'], 0)
        self.assertNotIn('needs_user', kinds(self.store, key))

    async def test_model_that_does_not_attach_after_the_hints_leaves_the_same_pause_as_before(self):
        portal = await self.serve(PhotoPortal())
        brain = PhotoBrain(attach=False)
        key = await self.bill(portal.add(ticket('TKT-40021-B', '2026-11-14', '250.00')), brain, until='attachment')
        # Told twice, in two consultations; the third time the person is asked.
        self.assertEqual([HINT in call['ultimo_error'] for call in brain.calls], [True, True])
        self.assertEqual(self.store.get(key)['message'],
                         'Toma el control y abre el selector del portal. Elige el documento en el panel y pulsa Adjuntar al portal.')
        self.assertEqual(portal.lookups, [])
        await self.runner.stop(key)


LISTS = r"""<script>
const lista = (id, opciones, aviso) => {
  const s = document.getElementById(id);
  s.innerHTML = '<option value="">' + aviso + '</option>' + opciones.map(([v, t]) => '<option value="' + v + '">' + t + '</option>').join('');
  s.disabled = !opciones.length;
};
document.getElementById('rfc').addEventListener('change', () => {
  lista('regimen', [], 'Cargando…'); lista('uso', [], 'Primero elige tu régimen');
  if (DEMORA >= 0) setTimeout(() => lista('regimen', [['605', '605 - Sueldos y salarios'], ['612', '612 - Actividades empresariales']], 'Elige'), DEMORA);
});
document.getElementById('regimen').addEventListener('change', () => {
  lista('uso', [], 'Cargando…');
  setTimeout(() => lista('uso', [['G03', 'G03 - Gastos en general'], ['S01', 'S01 - Sin efectos fiscales']], 'Elige'), DEMORA);
});
</script>"""


class LatePortal(Portal):
    """One page for everything; régimen and uso arrive ``delay`` ms after the RFC (never with a negative one)."""

    def __init__(self, delay=700, **options):
        super().__init__(**options)
        self.delay = delay

    def lookup(self, error='') -> str:
        return PAGE % ('<h1>Factura tu compra</h1><form action="/emitir" method="post">'
                       '<label>Folio del ticket<input name="folio" required></label>'
                       '<label>Total pagado<input name="total" required></label>'
                       '<label>RFC<input name="rfc" id="rfc" required></label>'
                       # Labels beside their lists, not around them: the name of a field must not change
                       # when its options arrive, or the screen would stop being the one that was learned.
                       '<label for="regimen">Régimen fiscal</label><select name="regimen" id="regimen" required disabled>'
                       '<option value="">Primero captura tu RFC</option></select>'
                       '<label for="uso">Uso del CFDI</label><select name="uso" id="uso" required disabled>'
                       '<option value="">Primero elige tu régimen</option></select>'
                       '<label for="pago">Forma de pago</label><select name="pago" id="pago" required>'
                       '<option value="">Elige</option><option value="01">Efectivo</option>'
                       '<option value="28">Tarjeta de débito</option><option value="04">Tarjeta de crédito</option></select>'
                       '<button>Emitir factura</button></form>' + LISTS.replace('DEMORA', str(self.delay)))


class EagerBrain(Brain):
    """Fills the form and asks for the emission at once, without waiting for the lists."""

    def plan(self, profile, receipt, view):
        visible = view['estado_visible']
        if 'Factura emitida' in visible or 'Factura tu compra' not in visible:
            return super().plan(profile, receipt, view)
        if not any(profile['rfc'] in line for line in view['campos']):
            # The RFC goes first: the portal starts loading its lists when the field is left.
            return Plan(steps=[step('fill', 'textbox', 'RFC', profile['rfc']),
                               step('fill', 'textbox', 'Folio del ticket', receipt['ticket_number']),
                               step('fill', 'textbox', 'Total pagado', receipt['amount'])])
        return Plan(steps=[step('click', 'button', 'Emitir factura', risk='issue')])


class LateFiscalListsTests(PortalCase):
    async def asyncSetUp(self):
        await super().asyncSetUp()
        patcher = patch('facturador.runner.FISCAL_POLL', 0.1)
        patcher.start()
        self.addCleanup(patcher.stop)

    async def test_emission_waits_for_lists_that_load_late_from_the_model_and_from_memory(self):
        portal = await self.serve(LatePortal())
        brain = EagerBrain()
        key = await self.bill(portal.add(ticket('TKT-88231-Q', '2026-11-13', '1137.45')), brain)
        row = self.store.get(key)
        self.assertEqual(row['status'], 'verified', (row['message'], self.store.events(key)[-8:]))
        # Three consultations: the form, the emission asked right away, and the download. Nobody
        # was asked to wait: the runner chose the three lists as they arrived.
        self.assertEqual(len(brain.calls), 3)
        events = kinds(self.store, key)
        self.assertNotIn('needs_user', events)
        self.assertEqual((events.count('issue_attempt'), events.count('campo_verificado')), (1, 3))
        self.assertEqual({name: portal.emitted[0][name] for name in ('regimen', 'uso', 'pago')},
                         {'regimen': '612', 'uso': 'G03', 'pago': '28'})
        # Repeated from memory the steps reach the button even sooner, and the same wait applies.
        brain = EagerBrain()
        key = await self.bill(portal.add(ticket('TKT-10457-Z', '2026-11-21', '89.50', payment='01')), brain)
        self.assertEqual(self.store.get(key)['status'], 'verified', self.store.events(key)[-8:])
        self.assertEqual(brain.calls, [])
        events = kinds(self.store, key)
        self.assertNotIn('needs_user', events)
        self.assertEqual(events.count('issue_attempt'), 1)
        self.assertEqual({name: portal.emitted[1][name] for name in ('folio', 'regimen', 'uso', 'pago')},
                         {'folio': 'TKT-10457-Z', 'regimen': '612', 'uso': 'G03', 'pago': '01'})

    async def test_lists_that_never_load_end_in_the_same_pause_after_the_wait(self):
        portal = await self.serve(LatePortal(delay=-1))
        with patch('facturador.runner.FISCAL_WAIT', 0.6):
            key = await self.bill(portal.add(ticket('TKT-40021-B', '2026-11-14', '250.00')), EagerBrain(), until='blocked')
        message = self.store.get(key)['message']
        self.assertTrue(message.startswith('Antes de emitir, el portal debe reflejar régimen fiscal (612)'), message)
        self.assertEqual(portal.emitted, [])
        self.assertFalse(self.store.get(key)['issue_attempted'])
        await self.runner.stop(key)


if __name__ == '__main__':
    unittest.main()
