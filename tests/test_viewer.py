from __future__ import annotations

import unittest
from types import SimpleNamespace
from unittest.mock import AsyncMock

from facturador.models import Profile, Receipt
from facturador.portals import capufe_proof
from facturador.runner import visible_challenge
from facturador.viewer import BrowserView, ViewerInput


class ViewerTests(unittest.IsolatedAsyncioTestCase):
    async def test_input_requires_manual_control_and_stops_on_resume(self):
        viewer = BrowserView()
        page = SimpleNamespace(is_closed=lambda: False, url='https://example.test/?session=example',
                               keyboard=SimpleNamespace(insert_text=AsyncMock()), screenshot=AsyncMock(return_value=b'image'))
        await viewer.show(page, 'Portal típico')
        with self.assertRaises(ValueError):
            await viewer.take_control(viewer.revision)
        await viewer.set_manual(True)
        lease = await viewer.take_control(viewer.revision)
        event = ViewerInput(channel=lease, kind='text', value='Ejemplo')
        await viewer.send(event)
        page.keyboard.insert_text.assert_awaited_once_with('Ejemplo')
        self.assertEqual(viewer.state()['url'], 'https://example.test/')
        await viewer.set_manual(False)
        with self.assertRaises(ValueError):
            await viewer.send(event)
        page.keyboard.insert_text.assert_awaited_once()

    async def test_challenge_requires_pause_until_visible_challenge_clears(self):
        self.assertEqual(await visible_challenge(None, 'checkbox: No soy un robot'), 'captcha')
        self.assertIsNone(await visible_challenge(None, 'checkbox [checked]: No soy un robot'))
        self.assertIsNone(await visible_challenge(None, 'formulario de datos fiscales'))

    async def test_capufe_proof_rejects_a_different_date_and_amount(self):
        profile = Profile(rfc='XAXX010101000', name='Prueba SAT', postal_code='01234', regime='612', email='demo@example.test')
        code = 'ABCDE1234567890000'
        receipt = Receipt(provider='capufe', billing_codes=[code], payment_code='01', date='2026-11-21', amount='50.00')
        values = {'rfc': profile.rfc, 'name': profile.name, 'postal': profile.postal_code, 'email': profile.email,
                  'regime': profile.regime, 'use': profile.cfdi_use, 'defaultGrouping': True,
                  'rows': [['', code, 'PLAYAS DE TIJUANA', '2026-noviembre-21 18:02:30 pm', '$50.00']]}
        page = SimpleNamespace(evaluate=AsyncMock(return_value=values))
        self.assertTrue(await capufe_proof(page, profile, receipt))
        values['rows'][0][3] = '2026-noviembre-20 18:02:30 pm'
        self.assertFalse(await capufe_proof(page, profile, receipt))
        values['rows'][0][3] = '2026-noviembre-21 18:02:30 pm'
        values['rows'][0][4] = '$51.00'
        self.assertFalse(await capufe_proof(page, profile, receipt))
