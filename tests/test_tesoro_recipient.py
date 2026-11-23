"""Chrome checks that Tesoro saves only the recipient from the selected profile."""
import os
import unittest

from playwright.async_api import async_playwright

from facturador.tesoro_recipient import tesoro_recipient
from test_core import PROFILE


def form(rfc, regime='601'):
    return f'''<form action="/GenerarCFDI/GuardarCliente" method="post">
      <input id="RFC" value="{rfc}" readonly>
      <input id="RazonSocialV4" value="Otro receptor">
      <select id="ClaveRegimenFiscalSAT">
        <option value="601" {'selected' if regime == '601' else ''}>General de Ley Personas Morales</option>
        <option value="{PROFILE.regime}" {'selected' if regime == PROFILE.regime else ''}>{PROFILE.regime} - Personas Físicas con Actividades Empresariales y Profesionales</option>
      </select>
      <input id="CP" value="00000"><input id="EMail" value="otro@example.test">
      <button>Guardar y continuar</button>
    </form>'''


@unittest.skipUnless(os.path.isfile('/usr/bin/google-chrome-stable'), 'Requiere Google Chrome')
class TesoroRecipientTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.playwright = await async_playwright().start()
        self.browser = await self.playwright.chromium.launch(executable_path='/usr/bin/google-chrome-stable')
        self.page = await self.browser.new_page()

    async def asyncTearDown(self):
        await self.browser.close()
        await self.playwright.stop()

    async def open_form(self, html, path='/GenerarCFDI', host='buzonfacturas.com'):
        await self.page.route(f'https://{host}/**', lambda route: route.fulfill(
            content_type='text/html; charset=utf-8', body=html))
        await self.page.goto(f'https://{host}{path}')

    async def test_fills_exact_recipient_then_proves_all_fields_before_save(self):
        await self.open_form(form(PROFILE.rfc))
        changed = []
        for _ in range(5):
            field, pending = await tesoro_recipient(self.page, PROFILE, apply=True)
            if not field:
                self.assertEqual(pending, [])
                break
            changed.append(field)
        self.assertEqual(changed, ['razón social', 'código postal', 'correo electrónico', 'régimen fiscal'])
        self.assertEqual(await tesoro_recipient(self.page, PROFILE), (None, []))
        self.assertEqual(await self.page.locator('#RFC').input_value(), PROFILE.rfc)
        self.assertEqual(await self.page.locator('#ClaveRegimenFiscalSAT').input_value(), PROFILE.regime)

    async def test_wrong_rfc_or_form_does_not_change_recipient(self):
        await self.open_form(form('RFC_AJENO'))
        self.assertIn('RFC', (await tesoro_recipient(self.page, PROFILE, apply=True))[1])
        self.assertEqual(await self.page.locator('#RazonSocialV4').input_value(), 'Otro receptor')
        await self.page.set_content(form(PROFILE.rfc).replace('/GenerarCFDI/GuardarCliente', '/GenerarCFDI/Emitir'))
        self.assertIn('RFC', (await tesoro_recipient(self.page, PROFILE, apply=True))[1])
        self.assertEqual(await self.page.locator('#RazonSocialV4').input_value(), 'Otro receptor')


if __name__ == '__main__':
    unittest.main()
