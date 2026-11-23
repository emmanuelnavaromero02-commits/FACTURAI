import asyncio
import json
import tempfile
import threading
import time
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock, patch

from fastapi.testclient import TestClient

from facturador.app import create_app
from facturador.ai import AI
from facturador.auth import PlanError
from facturador.automation import TicketAutomation, read_ticket, reconcile_uncertainties
from facturador.db import Store
from facturador.models import Profile, Receipt
from facturador.runner import Runner


PROFILE = Profile(rfc='XAXX010101000', name='PERSONA DE PRUEBA', postal_code='12345',
                  regime='612', email='persona@example.com')


def extracted(**changes):
    fields = dict(provider='capufe', merchant='CAPUFE', is_receipt=True, amount='40.50',
                  date='2026-06-15', ticket_number='PRUEBA-A', billing_codes=[], payment_code='28',
                  payment_evidence='Pago con tarjeta de débito', details='', uncertainties=[], billing_url=None)
    return SimpleNamespace(**{**fields, **changes})


class UncertaintyReconciliationTests(unittest.TestCase):
    def test_vianney_survey_qr_and_unreadable_fiscal_url_are_observations(self):
        note = ('El QR inferior está identificado como el de facturación el superior es de la encuesta. '
                'No se pudo verificar la URL de facturación a partir de la imagen.')
        self.assertEqual(reconcile_uncertainties(
            [note], payment_confirmed=False, catalog_provider='vianney',
            custom_portal_confirmed=False), [])
        self.assertEqual(reconcile_uncertainties(
            [note], payment_confirmed=False, catalog_provider='',
            custom_portal_confirmed=False), [note])

    def test_survey_qr_and_fiscal_photo_wording_variations(self):
        notes = ['El QR inferior es de facturación y el superior es de la encuesta.',
                 'No fue posible leer la liga fiscal en la fotografía.']
        self.assertEqual(reconcile_uncertainties(
            notes, payment_confirmed=False, catalog_provider='vianney',
            custom_portal_confirmed=False), [])

    def test_implicit_qr_link_fragment_after_clause_split(self):
        note = ('No se pudo decodificar la URL del QR de facturación '
                'y no aparece impresa en texto junto al código.')
        self.assertEqual(reconcile_uncertainties(
            [note], payment_confirmed=False, catalog_provider='vianney',
            custom_portal_confirmed=False), [])
        uncorroborated = reconcile_uncertainties(
            [note], payment_confirmed=False, catalog_provider='',
            custom_portal_confirmed=False)
        self.assertEqual(len(uncorroborated), 1)
        self.assertIn('No se pudo decodificar', uncorroborated[0])
        self.assertIn('no aparece impresa', uncorroborated[0])
        guarded = [
            'no aparece impresa en texto junto al código del otro comercio.',
            'no aparece impresa en texto junto al código; el importe no coincide.',
            'no aparece impresa en texto junto al código; el folio no coincide.',
            'no aparece impresa en texto junto al código; el RFC no coincide.',
            'no aparece impresa en texto junto al código de otro-dominio.example.',
        ]
        self.assertEqual(reconcile_uncertainties(
            guarded, payment_confirmed=False, catalog_provider='vianney',
            custom_portal_confirmed=False), [guarded[0], 'el importe no coincide.',
                                             'el folio no coincide.', 'el RFC no coincide.',
                                             guarded[4]])

    def test_photo_url_note_keeps_unrelated_fields_and_foreign_destination(self):
        notes = [
            'No se pudo verificar la URL de facturación a partir de la imagen y el importe no coincide.',
            'No se pudo verificar la URL de facturación a partir de la imagen del otro comercio.',
            'No se pudo verificar la URL de facturación a partir de la imagen; el folio está incompleto.',
            'No se pudo verificar la URL de facturación a partir de la imagen y el RFC es distinto.',
            'La URL otro-dominio.example de facturación no se pudo verificar en la imagen.',
        ]
        self.assertEqual(reconcile_uncertainties(
            notes, payment_confirmed=False, catalog_provider='vianney',
            custom_portal_confirmed=False), [notes[0], notes[1], 'el folio está incompleto.',
                                             notes[3], notes[4]])

    def test_third_portal_wordings_are_readability_notes(self):
        examples = (
            ('tesoro', 'No se verificó una liga HTTPS en el código QR.'),
            ('bp', 'La dirección de facturación está impresa como '
                   'www.facturacion.grupocentra.mx, sin indicar el protocolo.'),
            ('vianney', 'No se pudo decodificar la liga HTTPS del QR de facturación '
                        'no aparece impresa como texto.'),
        )
        for provider, note in examples:
            with self.subTest(provider=provider):
                self.assertEqual(reconcile_uncertainties(
                    [note], payment_confirmed=False, catalog_provider=provider,
                    custom_portal_confirmed=False), [])
                self.assertEqual(reconcile_uncertainties(
                    [note], payment_confirmed=False, catalog_provider='',
                    custom_portal_confirmed=False), [note])

    def test_readability_family_preserves_other_commerce_and_field_disagreements(self):
        notes = [
            'La dirección de facturación está impresa como otro-dominio.example, sin indicar el protocolo.',
            'No se verificó una liga HTTPS en el código QR de otro comercio.',
            'No se verificó una liga HTTPS en el código QR y el folio no coincide.',
            'No se pudo decodificar el QR porque el importe no coincide.',
            'No se pudo leer el QR y el RFC del emisor es distinto.',
        ]
        self.assertEqual(reconcile_uncertainties(
            notes, payment_confirmed=False, catalog_provider='bp',
            custom_portal_confirmed=False), notes)

    def test_qr_readability_variations_without_other_subjects(self):
        notes = ['No fue posible leer el código QR de facturación.',
                 'La URL del QR no es legible en la foto.',
                 'El dominio buzonfacturas.com omite el protocolo HTTPS.']
        self.assertEqual(reconcile_uncertainties(
            notes, payment_confirmed=False, catalog_provider='tesoro',
            custom_portal_confirmed=False), [])

    def test_new_tesoro_semicolon_resolves_only_corroborated_portal_clauses(self):
        note = ('El ticket imprime el dominio buzonfacturas.com sin el prefijo HTTPS; '
                'no se verificó la URL completa del código QR. '
                'El importe no coincide con el total del ticket.')
        self.assertEqual(reconcile_uncertainties(
            [note], payment_confirmed=True, catalog_provider='tesoro',
            custom_portal_confirmed=False), ['El importe no coincide con el total del ticket.'])

    def test_new_bp_protocol_note_requires_catalog_host(self):
        note = ('La dirección de facturación está impresa como '
                'www.facturacion.grupocentra.mx, sin el prefijo de protocolo.')
        self.assertEqual(reconcile_uncertainties(
            [note], payment_confirmed=False, catalog_provider='bp',
            custom_portal_confirmed=False), [])
        changed = note.replace('www.facturacion.grupocentra.mx', 'otro-dominio.example')
        self.assertEqual(reconcile_uncertainties(
            [changed], payment_confirmed=False, catalog_provider='bp',
            custom_portal_confirmed=False), [changed])

    def test_new_vianney_payment_and_qr_clauses_keep_rfc_warning(self):
        note = ('La línea de pago no indica si la tarjeta fue de crédito o de débito, '
                'por lo que no se asigna una clave SAT. '
                'No se pudo verificar la liga HTTPS del QR de facturación y no se ve '
                'una liga de facturación impresa. '
                'El RFC del emisor no coincide con el del comercio.')
        self.assertEqual(reconcile_uncertainties(
            [note], payment_confirmed=True, catalog_provider='vianney',
            custom_portal_confirmed=False), ['El RFC del emisor no coincide con el del comercio.'])
        retained = reconcile_uncertainties(
            [note], payment_confirmed=False, catalog_provider='vianney',
            custom_portal_confirmed=False)
        self.assertEqual(retained, ['La línea de pago no indica si la tarjeta fue de crédito o de débito, '
                                    'por lo que no se asigna una clave SAT. '
                                    'El RFC del emisor no coincide con el del comercio.'])

    def test_verified_catalog_and_payment_resolve_only_their_own_sentences(self):
        paragraph = (
            'La línea de pago indica terminal inalámbrica, pero no distingue entre tarjeta de crédito y de débito. '
            'El QR inferior está identificado como el de facturación, pero no se pudo decodificar su URL. '
            'El QR superior corresponde a una encuesta. '
            'El importe impreso no coincide con el importe leído. '
            'El folio está incompleto.')
        remaining = reconcile_uncertainties(
            [paragraph], payment_confirmed=True, catalog_provider='vianney',
            custom_portal_confirmed=False)
        self.assertEqual(remaining, ['El importe impreso no coincide con el importe leído. El folio está incompleto.'])

    def test_catalog_https_notes_require_matching_domain(self):
        notes = [
            'El ticket imprime el dominio buzonfacturas.com sin el esquema HTTPS. '
            'No se pudo verificar una liga HTTPS a partir del QR.',
            'El ticket imprime el dominio otro-dominio.example sin el esquema HTTPS.',
            'La identidad del comercio no coincide con la del portal.',
        ]
        remaining = reconcile_uncertainties(
            notes, payment_confirmed=True, catalog_provider='tesoro',
            custom_portal_confirmed=False)
        self.assertEqual(remaining, notes[1:])
        self.assertEqual(reconcile_uncertainties(
            ['El portal de facturación aparece impreso como www.facturacion.grupocentra.mx, sin prefijo HTTPS.'],
            payment_confirmed=False, catalog_provider='bp', custom_portal_confirmed=False), [])

    def test_unconfirmed_evidence_does_not_erase_payment_or_portal_doubts(self):
        notes = ['No se puede distinguir si la tarjeta fue de crédito o débito.',
                 'El portal de facturación aparece impreso como www.facturacion.grupocentra.mx, sin prefijo HTTPS.']
        self.assertEqual(reconcile_uncertainties(
            notes, payment_confirmed=False, catalog_provider='', custom_portal_confirmed=False), notes)

    def test_confirmed_custom_url_keeps_destination_disagreement(self):
        notes = ['No aparece una liga de facturación.',
                 'El portal señalado corresponde a otro comercio.']
        self.assertEqual(reconcile_uncertainties(
            notes, payment_confirmed=False, catalog_provider='', custom_portal_confirmed=True), notes[1:])

    def test_owner_confirmed_payment_allows_known_ticket_with_qr_observation(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            path = root / 'consumo.txt'
            path.write_text('VIANNEY\nFOLIO PRUEBA-A\nTOTAL 40.50', encoding='utf-8')
            row = {'source_path': str(path), 'receipt': {'source': 'archivo: consumo.txt'},
                   'confirmed_payment': '28'}
            with patch('facturador.automation.AI.extract', return_value=extracted(
                    provider='vianney', merchant='Vianney', payment_code=None, payment_evidence='',
                    uncertainties=[
                        'La línea de pago indica terminal inalámbrica, pero no distingue entre tarjeta de crédito y de débito. '
                        'El QR inferior está identificado como el de facturación, pero no se pudo decodificar su URL. '
                        'El QR superior corresponde a una encuesta.'])):
                receipt, ready, note, sources = read_ticket(Mock(), row, True, root)
            self.assertTrue(ready, note)
            self.assertEqual(receipt.payment_code, '28')
            self.assertEqual(sources['fields']['payment_code'], 'correccion_usuario')


class TicketAutomationTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.root = Path(directory.name)
        self.store = Store(self.root)
        self.plan = Mock()
        self.plan.status.return_value = {'status': 'ready'}
        self.runner = Runner(self.store, self.plan)
        self.worker = TicketAutomation(self.store, self.runner, self.plan, lambda: PROFILE)
        self.profile = Receipt(provider='capufe', merchant='CAPUFE', date='2026-06-15', amount='40.50',
                               ticket_number='PRUEBA-A', payment_code='28')
        self.worker.configure(True)

    def add(self, key='subida', source='archivo: ticket.txt', text='CAPUFE'):
        path = self.root / (key + '.txt')
        path.write_text(text, encoding='utf-8')
        return self.store.add(str(path), key, source)[0]['id']

    async def test_reader_prepares_known_receipt_once_with_verified_auto_issue_option(self):
        key = self.add(source='Gmail: mensaje')
        self.add('demo', 'Ejemplo: demo')
        self.store.import_history([{'key': 'hist', 'receipt': self.profile.model_copy(
            update={'ticket_number':'ANTERIOR'}).model_dump(), 'note':'Historial'}])
        with patch('facturador.automation.AI.extract', return_value=extracted()) as extract:
            await self.worker.step()
            self.assertEqual(self.store.get(key)['status'], 'ready')
            source_event = next(event for event in self.store.events(key) if event['kind'] == 'fuentes_de_campos')
            self.assertEqual(json.loads(source_event['detail'])['fields']['payment_code'],
                             'vision_modelo_linea_pago')
            with patch.object(self.runner, 'run', new_callable=AsyncMock) as run:
                await self.worker.step()
                await asyncio.gather(*self.runner.job_tasks.values())
                run.assert_awaited_once_with(key, PROFILE, True, False)
            self.store.recover_after_restart()
            await self.worker.step()
            self.assertFalse(self.runner.has_active_jobs())
            self.assertEqual(self.store.get(key)['status'], 'ready')
            extract.assert_called_once()

    async def test_existing_review_names_missing_field_then_continues_on_user_save(self):
        key = self.add()
        self.store.save_receipt(key, self.profile.model_copy(update={'payment_code': ''}))
        with patch('facturador.automation.AI.extract', side_effect=AssertionError):
            await self.worker.step()
            self.assertIn('Falta forma de pago', self.store.get(key)['message'])
            self.assertEqual(self.store.get(key)['receipt']['payment_code'], '')
            self.store.save_receipt(key, self.profile, ready=True)
            self.runner.viewer.channel = 'manual-control'
            await self.worker.step()
            self.assertEqual(self.worker.status()['phase'], 'waiting_browser')
            self.runner.viewer.channel = ''
            self.worker.get_profile = Mock(side_effect=ValueError)
            await self.worker.step()
            self.assertEqual(self.worker.status()['phase'], 'waiting_profile')
            self.worker.get_profile = lambda: PROFILE
            with patch.object(self.runner, 'run', new_callable=AsyncMock):
                await self.worker.step()
                await self.runner.job_tasks[key]
            self.assertEqual(self.store.get(key)['status'], 'queued')

    async def test_unfamiliar_printed_portal_opens_without_catalog_entry(self):
        link = 'https://facturas.example.org/mi-ticket'
        key = self.add(source='WhatsApp: ticket.txt', text='Factura en ' + link)
        with patch('facturador.automation.AI.extract', return_value=extracted(provider='unknown', billing_url=link)):
            await self.worker.step()
        row = self.store.get(key)
        self.assertEqual(row['receipt']['portal_url'], link)
        self.assertEqual(row['receipt']['provider'], 'portal_ticket')
        self.assertEqual(row['status'], 'ready')
        with patch.object(self.runner, 'run', new_callable=AsyncMock) as run:
            await self.worker.step()
            await asyncio.gather(*self.runner.job_tasks.values())
            run.assert_awaited_once_with(key, PROFILE, True, False)
        self.assertEqual(self.store.get(key)['status'], 'queued')

    async def test_single_printed_fiscal_url_opens_when_model_does_not_transcribe_it(self):
        link = 'https://facturas.example.org/mi-ticket'
        key = self.add(text='Factura en ' + link)
        with patch('facturador.automation.AI.extract', return_value=extracted(
                provider='unknown', merchant='Café Estrella', billing_url=None,
                uncertainties=['No aparece una liga de facturación.'])):
            await self.worker.step()
        row = self.store.get(key)
        self.assertEqual((row['status'], row['receipt']['portal_url']), ('ready', link))
        self.assertNotIn('liga de facturación', row['message'])

    async def test_two_printed_fiscal_urls_are_not_selected_automatically(self):
        first = 'https://facturas.example.org/uno'
        second = 'https://facturas.otro.org/dos'
        key = self.add(text=f'Factura en {first}\nFactura en {second}')
        with patch('facturador.automation.AI.extract', return_value=extracted(
                provider='unknown', billing_url=first)):
            await self.worker.step()
        row = self.store.get(key)
        self.assertEqual(row['status'], 'review')
        self.assertEqual(row['receipt']['portal_url'], '')
        self.assertIn('varios enlaces fiscales', row['message'])
        self.assertFalse(self.runner.has_active_jobs())

    async def test_short_catalog_brand_confirms_bp_playas(self):
        key = self.add(text='BP PLAYAS\nFOLIO PRUEBA-A')
        with patch('facturador.automation.AI.extract', return_value=extracted(
                provider='bp', merchant='BP PLAYAS')):
            await self.worker.step()
        self.assertEqual(self.store.get(key)['status'], 'ready')

    async def test_original_payment_line_and_matching_catalog_host_clear_obsolete_doubts(self):
        key = self.add(text='TESORO\nFOLIO PRUEBA-A\nTIPO OPER: EFECTIVO')
        with patch('facturador.automation.AI.extract', return_value=extracted(
                provider='tesoro', merchant='Tesoro', payment_code='01',
                payment_evidence='TIPO OPER: EFECTIVO', uncertainties=[
                    'El ticket no indica la forma de pago.',
                    'El ticket imprime el dominio buzonfacturas.com sin el esquema HTTPS. '
                    'No se pudo verificar una liga HTTPS a partir del QR.'])):
            await self.worker.step()
        row = self.store.get(key)
        self.assertEqual(row['status'], 'ready', row['message'])
        self.assertEqual(row['receipt']['payment_code'], '01')

    async def test_short_catalog_brand_clears_matching_https_prefix_note(self):
        key = self.add(text='BP PLAYAS\nFOLIO PRUEBA-A')
        with patch('facturador.automation.AI.extract', return_value=extracted(
                provider='bp', merchant='BP PLAYAS', uncertainties=[
                    'El portal de facturación aparece impreso como www.facturacion.grupocentra.mx, '
                    'sin prefijo HTTPS.'])):
            await self.worker.step()
        self.assertEqual(self.store.get(key)['status'], 'ready')

    async def test_local_catalog_host_resolves_qr_note_when_brand_ocr_is_missing(self):
        key = self.add(text='FOLIO PRUEBA-A\nFACTURACIÓN EN https://buzonfacturas.com/')
        with patch('facturador.automation.AI.extract', return_value=extracted(
                provider='tesoro', merchant='Tesoro', uncertainties=[
                    'No se verificó una liga HTTPS en el código QR.'])):
            await self.worker.step()
        row = self.store.get(key)
        self.assertEqual(row['status'], 'ready', row['message'])
        self.assertEqual(row['receipt']['provider'], 'tesoro')

    async def test_model_mentioned_catalog_host_does_not_confirm_unread_original(self):
        key = self.add(text='OTRO COMERCIO\nFOLIO PRUEBA-A')
        with patch('facturador.automation.AI.extract', return_value=extracted(
                provider='tesoro', merchant='Tesoro', uncertainties=[
                    'El dominio buzonfacturas.com omite el protocolo HTTPS.'])):
            await self.worker.step()
        row = self.store.get(key)
        self.assertEqual(row['status'], 'review')
        self.assertIn('buzonfacturas.com', row['message'])

    async def test_vianney_survey_qr_note_does_not_block_verified_receipt(self):
        key = self.add(text='VIANNEY\nFOLIO PRUEBA-A\nTOTAL 40.50')
        with patch('facturador.automation.AI.extract', return_value=extracted(
                provider='vianney', merchant='Vianney', uncertainties=[
                    'El QR inferior está identificado como el de facturación el superior es de la encuesta. '
                    'No se pudo verificar la URL de facturación a partir de la imagen.'])):
            await self.worker.step()
        row = self.store.get(key)
        self.assertEqual(row['status'], 'ready', row['message'])

    async def test_vianney_implicit_qr_fragment_does_not_block_verified_receipt(self):
        key = self.add(text='VIANNEY\nFOLIO PRUEBA-A\nTOTAL 40.50')
        with patch('facturador.automation.AI.extract', return_value=extracted(
                provider='vianney', merchant='Vianney', uncertainties=[
                    'No se pudo decodificar la URL del QR de facturación '
                    'y no aparece impresa en texto junto al código.'])):
            await self.worker.step()
        row = self.store.get(key)
        self.assertEqual(row['status'], 'ready', row['message'])

    async def test_short_brand_in_folio_does_not_confirm_merchant(self):
        other = self.add(text='OTRO COMERCIO\nFOLIO BP 567')
        with patch('facturador.automation.AI.extract', return_value=extracted(
                provider='bp', merchant='BP PLAYAS', ticket_number='567')):
            await self.worker.step()
        row = self.store.get(other)
        self.assertEqual(row['status'], 'review')
        self.assertIn('Confirma que el comercio', row['message'])

    async def test_upside_down_receipt_retries_local_ocr_only_for_catalog_brand(self):
        from PIL import Image
        path = self.root / 'bp.png'
        Image.new('RGB', (16, 16), 'white').save(path)
        key = self.store.add(str(path), 'bp-girado')[0]['id']
        def page(_path, *_, rotation=0, force_ocr=False):
            if rotation:
                self.assertEqual((rotation, force_ocr), (180, True))
            return {'pages':1, 'method':'ocr', 'codes':[],
                    'text':'BP PLAYAS\nFOLIO PRUEBA-A' if rotation else 'texto ilegible'}
        with patch('facturador.automation.AI.extract', return_value=extracted(
                provider='bp', merchant='BP PLAYAS')), \
             patch('facturador.automation.read_local_page', side_effect=page) as local:
            await self.worker.step()
        self.assertEqual(self.store.get(key)['status'], 'ready')
        self.assertEqual(local.call_count, 2)
        source_event = next(event for event in self.store.events(key) if event['kind'] == 'fuentes_de_campos')
        self.assertIn('ocr_giro_180', json.loads(source_event['detail'])['tools'])

    async def test_unprinted_domain_still_needs_review(self):
        link = 'https://facturas.example.org/mi-ticket'
        key = self.add(source='WhatsApp: ticket.txt', text='Compra pagada sin dirección de facturación')
        with patch('facturador.automation.AI.extract', return_value=extracted(provider='unknown', billing_url=None)), \
             patch('facturador.automation.AI.discover_portal', return_value=('', '')):
            await self.worker.step()
        row = self.store.get(key)
        self.assertEqual(row['status'], 'review')
        receipt = Receipt.model_validate(row['receipt']).model_copy(update={'provider':'portal_ticket', 'portal_url':link})
        self.store.save_receipt(key, receipt, ready=True)
        await self.worker.step()
        self.assertEqual(self.store.get(key)['status'], 'ready')
        self.assertIn('Confirma el dominio', self.store.get(key)['message'])
        self.assertFalse(self.runner.has_active_jobs())

    async def test_verified_public_research_prepares_unfamiliar_portal(self):
        billing = 'https://facturas.cafeteria.example.org/emitir'
        evidence = 'https://cafeestrella.mx/facturacion'
        key = self.add(source='archivo: consumo.txt', text='CAFETERIA\nTOTAL: 40.50\nFOLIO PRUEBA-A')
        with patch('facturador.automation.AI.extract', return_value=extracted(
                provider='unknown', merchant='Café Estrella', billing_url=None,
                uncertainties=['No aparece una liga de facturación.'])), \
             patch('facturador.automation.AI.discover_portal', return_value=(billing, evidence)) as search, \
             patch('facturador.automation.public_page_links_to', new_callable=AsyncMock, return_value=True) as linked:
            await self.worker.step()
        search.assert_called_once_with('Café Estrella')
        linked.assert_awaited_once_with(evidence, billing)
        row = self.store.get(key)
        self.assertEqual((row['status'], row['receipt']['portal_url']), ('ready', billing))
        with patch.object(self.runner, 'run', new_callable=AsyncMock) as run:
            await self.worker.step()
            await asyncio.gather(*self.runner.job_tasks.values())
            run.assert_awaited_once_with(key, PROFILE, True, False)
        self.assertEqual(self.store.get(key)['status'], 'queued')

    async def test_unrelated_search_source_needs_domain_review(self):
        billing = 'https://facturas.example.org/emitir'
        evidence = 'https://directorio.example.org/cafe-estrella'
        key = self.add(source='archivo: consumo.txt', text='CAFE ESTRELLA\nTOTAL: 40.50')
        with patch('facturador.automation.AI.extract', return_value=extracted(
                provider='unknown', merchant='Café Estrella', billing_url=None)), \
             patch('facturador.automation.AI.discover_portal', return_value=(billing, evidence)), \
             patch('facturador.automation.public_page_links_to', new_callable=AsyncMock) as linked:
            await self.worker.step()
        linked.assert_not_awaited()
        row = self.store.get(key)
        self.assertEqual(row['status'], 'review')
        self.assertEqual(row['receipt']['portal_url'], billing)


    async def test_bad_date_and_payment_do_not_discard_legible_ticket_fields(self):
        key = self.add()
        with patch('facturador.automation.AI.extract', return_value=extracted(date='ayer', payment_code='tarjeta')):
            await self.worker.step()
        row = self.store.get(key)
        self.assertEqual(row['receipt']['ticket_number'], 'PRUEBA-A')
        self.assertEqual(row['receipt']['date'], '')
        self.assertEqual(row['receipt']['payment_code'], '')
        self.assertIn('Falta fecha', row['message'])
        self.assertIn('Falta forma de pago', row['message'])

    async def test_only_one_read_and_concurrent_user_edit_wins(self):
        key = self.add()
        started, finish = threading.Event(), threading.Event()

        def slow_reader(*_):
            started.set()
            finish.wait(5)
            return extracted()

        with patch('facturador.automation.AI.extract', side_effect=slow_reader) as extract:
            task = asyncio.create_task(self.worker.step())
            try:
                self.assertTrue(await asyncio.to_thread(started.wait, 3))
                with self.assertRaisesRegex(ValueError, 'lectura en curso'):
                    await self.worker.analyze(key)
                self.store.save_receipt(key, self.profile.model_copy(update={'ticket_number':'EDITADO'}), ready=True)
            finally:
                finish.set()
            await task
            extract.assert_called_once()
        self.assertEqual(self.store.get(key)['receipt']['ticket_number'], 'EDITADO')

    async def test_paused_plan_then_uncertain_read_needs_operator(self):
        key = self.add()
        self.plan.status.return_value = {'status':'limit','message':'Cuota agotada'}
        with patch('facturador.automation.AI.extract') as extract:
            await self.worker.step()
            self.assertEqual(self.worker.status()['phase'], 'waiting_plan')
            extract.assert_not_called()
        self.plan.status.return_value = {'status':'ready'}
        with patch('facturador.automation.AI.extract', side_effect=PlanError('Conexión interrumpida.')) as extract:
            await self.worker.step()
            await self.worker.step()
            extract.assert_called_once()
        self.assertEqual(self.store.get(key)['status'], 'review')
        self.assertIn('interrumpida', self.store.get(key)['message'])

    async def test_restart_recovers_unstarted_but_does_not_rebill_an_interrupted_read(self):
        waiting = self.add()
        inflight = self.add('incidente')
        self.store.claim_read(inflight, automatic=True)
        self.store.recover_after_restart()
        reopened = TicketAutomation(Store(self.root), self.runner, self.plan, lambda: PROFILE)
        self.assertTrue(reopened.enabled)
        self.assertEqual(self.store.get(inflight)['status'], 'review')
        with patch('facturador.automation.AI.extract', return_value=extracted(uncertainties=['Confirma la fecha.'])) as reader:
            await reopened.step()
            await reopened.step()
            reader.assert_called_once()
        self.assertIn('Confirma la fecha', self.store.get(waiting)['message'])


class AutomationAPITests(unittest.TestCase):
    def test_web_research_rejects_uncited_portal(self):
        candidate = 'https://facturas.example.org/emitir'
        source = 'https://cafeteria.example.org/facturacion'
        plan = Mock()
        plan.infer.return_value = (SimpleNamespace(billing_url=candidate, evidence_url=source),
                                   ['https://directorio.example.org/lista'])
        self.assertEqual(AI(plan).discover_portal('Cafetería Prueba'), ('', ''))
        plan.infer.return_value = (SimpleNamespace(billing_url=candidate, evidence_url=source), [source])
        self.assertEqual(AI(plan).discover_portal('Cafetería Prueba'), (candidate, source))

    def test_uploaded_ticket_is_read_by_background_worker(self):
        from PIL import Image
        from io import BytesIO

        with tempfile.TemporaryDirectory() as directory:
            app = create_app(Path(directory))
            with (patch.object(app.state.plan, 'status', return_value={'status':'ready'}),
                  patch('facturador.automation.AI.extract', return_value=extracted(payment_code=None)) as read,
                  TestClient(app) as client):
                headers = {'x-app-token': app.state.csrf_token}
                client.put('/api/automation', json={'enabled':True}, headers=headers).raise_for_status()
                image = BytesIO()
                Image.new('RGB', (160, 120), 'white').save(image, 'PNG')
                reply = client.post('/api/upload', files={'files':('ticket.png', image.getvalue(), 'image/png')},
                                    headers=headers)
                reply.raise_for_status()
                key = reply.json()[0]['id']
                until = time.monotonic() + 5
                while app.state.store.get(key)['status'] == 'new' and time.monotonic() < until:
                    time.sleep(.05)
                row = next(r for r in client.get('/api/state').json()['jobs'] if r['id'] == key)
                self.assertEqual(row['status'], 'review')
                self.assertIn('Falta forma de pago', row['message'])
                read.assert_called_once()

    def test_preferences_are_opt_in_and_persist_over_restart(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            with TestClient(create_app(root)) as client:
                state = client.get('/api/state').json()
                self.assertFalse(state['automation']['enabled'])
                self.assertIsNone(state['browser']['job_id'])
                response = client.put('/api/automation', json={'enabled': True}, headers={'x-app-token': state['token']})
                self.assertTrue(response.json()['enabled'])
            with TestClient(create_app(root)) as client:
                self.assertTrue(client.get('/api/state').json()['automation']['enabled'])
