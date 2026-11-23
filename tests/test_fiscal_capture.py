import io
import tempfile
import unittest
from pathlib import Path
from unittest.mock import Mock, patch

from fastapi.testclient import TestClient
from PIL import Image, ImageDraw, ImageFont
from pypdf import PdfReader, PdfWriter
from pypdf.generic import DecodedStreamObject, NameObject

from facturador.app import create_app
from facturador.automation import TicketAutomation
from facturador.db import Store
from facturador.models import Profile, Receipt, SAT_PAYMENT_CODES
from facturador.local_reader import ocr_languages
from facturador.receipt_evidence import printed_payment
from facturador.runner import Runner
from facturador.tax_document import constancia_draft, parse_constancia
from test_core import PROFILE, pdf_bytes
from test_ticket_automation import extracted


ID = """CONSTANCIA DE SITUACION FISCAL
Datos de identificacion del contribuyente:
RFC: XAXX010101000
Nombre (s): MARIA
Primer Apellido: PENA
Segundo Apellido: LOPEZ
Datos del domicilio registrado:
Codigo Postal: 01234 Tipo de vialidad: CALLE"""
REGIMES = """Regimenes:
Regimen Fecha Inicio Fecha Fin
605 Regimen de Sueldos y Salarios e Ingresos Asimilados a Salarios 01/01/2019 31/12/2020
Regimen de las Personas Fisicas con Actividades Empresariales y Profesionales 01/01/2021
Obligaciones:"""


def synthetic_pdf(*pages):
    writer = PdfWriter()
    for text in pages:
        page = writer.add_page(PdfReader(io.BytesIO(pdf_bytes())).pages[0])
        stream = DecodedStreamObject()
        lines = (line.replace('(',r'\(').replace(')',r'\)') for line in text.splitlines())
        stream.set_data(('BT /F1 10 Tf 30 190 Td 13 TL ' + ' '.join(f'({line}) Tj T*' for line in lines) + ' ET').encode('ascii'))
        page[NameObject('/Contents')] = writer._add_object(stream)
    file = io.BytesIO()
    writer.write(file)
    return file.getvalue()


class TaxCaptureTests(unittest.TestCase):
    @unittest.skipUnless(ocr_languages(), 'Requiere Tesseract local')
    def test_rotated_photo_reads_recipient_postal_code_and_regime(self):
        image = Image.new('RGB', (1450, 650), 'white')
        draw = ImageDraw.Draw(image)
        lines = [*ID.splitlines(), 'Regimen fiscal: 612']
        for i, line in enumerate(lines):
            draw.text((30, 20 + i * 50), line, font=ImageFont.load_default(size=31), fill='black')
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder)/'csf.png'
            image.rotate(90, expand=True).save(path)
            draft = constancia_draft(path)
        self.assertEqual(draft['postal_code'], '01234')
        self.assertEqual(draft['regime'], '612')

    def test_digital_pdf_local_and_previous_owner_phone_are_preserved(self):
        with tempfile.TemporaryDirectory() as folder, TestClient(create_app(Path(folder))) as client:
            headers = {'x-app-token': client.get('/api/state').json()['token']}
            contact = {**PROFILE.model_dump(), 'whatsapp_phone': '+52 (555) 555-0100'}
            client.put('/api/profile', json=contact, headers=headers).raise_for_status()
            data = synthetic_pdf(ID, REGIMES)
            with patch('facturador.ai.AI.tax_profile', side_effect=AssertionError('No se usa IA')):
                result = client.post('/api/profile/read-csf', files={'file': ('a.pdf', data)},
                                     data={'mode':'extract'}, headers=headers)
            result.raise_for_status()
            draft = result.json()
            self.assertEqual([draft[k] for k in ('name','rfc','postal_code','regime','email')],
                             ['MARIA PENA LOPEZ','XAXX010101000','01234','612',None])
            self.assertEqual([opt['code'] for opt in draft['regime_options']], ['612'])
            previous = client.get('/api/state').json()['profile']
            self.assertEqual(previous['name'], PROFILE.name)
            self.assertFalse(list(Path(folder).rglob('constancia.pdf')))
            contact.pop('whatsapp_phone')
            client.put('/api/profile', json=contact, headers=headers).raise_for_status()
            self.assertEqual(client.get('/api/state').json()['profile']['whatsapp_phone'], '+525555550100')
            self.assertEqual(Profile.model_validate(contact).whatsapp_phone,'')

    def test_multiple_regimes_and_unrelated_document(self):
        document = ID + '\nRegimenes:\n605 Sueldos y Salarios e Ingresos Asimilados a Salarios 01/01/2020\n626 Simplificado de Confianza 01/01/2022'
        draft = parse_constancia(document)
        self.assertIsNone(draft['regime'])
        self.assertEqual(draft['regimes'], ['605','626'])
        with self.assertRaises(ValueError):
            parse_constancia('CFDI 4.0 RFC: XAXX010101000')
        with self.assertRaisesRegex(ValueError,'más de un contribuyente'):
            parse_constancia(ID+'\nRFC: XEXX010101000')

    def test_mixed_payment_and_card_brand_are_ambiguous(self):
        self.assertEqual(printed_payment('Forma de pago: Mastercard'), '')
        self.assertEqual(printed_payment('Forma de pago: 04'), '04')
        self.assertEqual(printed_payment('Forma de pago: 02'), '02')
        self.assertEqual(printed_payment('Pago con cheque nominativo'), '02')
        self.assertEqual(printed_payment('TIPO OPER: EFECTIVO'), '01')
        self.assertEqual(printed_payment('TIPO OPER! EFECTIVO'), '01')
        self.assertEqual(printed_payment('Aceptamos efectivo o crédito'), '')
        self.assertEqual(printed_payment('Forma de pago: Efectivo\nPago con débito'), '')
        self.assertEqual(printed_payment('Tipo oper: efectivo\nForma de pago: 28'), '')
        self.assertEqual(printed_payment('Tipo oper: venta'), '')

    def test_receipt_accepts_sat_payment_catalog_but_not_unknown_or_undefined_payment(self):
        self.assertIn('02', SAT_PAYMENT_CODES)
        self.assertEqual(Receipt(payment_code='02').payment_code, '02')
        self.assertEqual({Receipt(payment_code=code).payment_code for code in SAT_PAYMENT_CODES},
                         set(SAT_PAYMENT_CODES))
        with self.assertRaises(ValueError):
            Receipt(payment_code='07')
        self.assertIn('La clave 99 no confirma cómo se pagó esta compra.', Receipt(payment_code='99').ready_errors())

    def test_manual_offline_api_reads_a_receipt_without_plan(self):
        with tempfile.TemporaryDirectory() as folder:
            app = create_app(Path(folder))
            document = Path(folder)/'ticket.txt'
            document.write_text('CAPUFE\nFolio: 000AB3\nFecha: 01/05/2026\nTotal: $12.00\nForma de pago: efectivo', encoding='utf-8')
            row, _ = app.state.store.add(str(document), 'ejemplo')
            with TestClient(app) as client:
                headers = {'x-app-token': client.get('/api/state').json()['token']}
                route = '/api/jobs/' + row['id'] + '/analyze'
                with patch('facturador.automation.AI.extract', side_effect=AssertionError('No se usa el plan')):
                    result = client.post(route, json={'mode':'local'}, headers=headers)
                self.assertEqual(result.json()['receipt']['payment_code'], '01')
                self.assertEqual(result.json()['receipt']['ticket_number'], '000AB3')
                self.assertEqual(client.post(route, json={'mode':'agent'}, headers=headers).status_code, 422)


class OfflineReceiptTests(unittest.IsolatedAsyncioTestCase):
    async def test_offline_queue_skips_processed_tickets_and_recovers_payment_once(self):
        with tempfile.TemporaryDirectory() as folder:
            store = Store(Path(folder))
            plan = Mock()
            plan.status.return_value = {'status':'limit','message':'Cuota agotada'}
            worker = TicketAutomation(store, Runner(store,plan), plan, lambda: PROFILE)
            worker.configure(True)
            ids = []
            for folio in ('000003','000004'):
                file = Path(folder)/(folio+'.txt')
                file.write_text(f'CAPUFE\nFolio: {folio}\nFecha: 15/06/2026\nTOTAL $40.50\nPago con tarjeta de débito', encoding='utf-8')
                ids.append(store.add(str(file), folio)[0]['id'])
            with patch('facturador.automation.AI.extract', side_effect=AssertionError('no se usa el plan')):
                await worker.step()
                await worker.step()
                self.assertFalse(await worker.step())
            row = store.get(ids[0])
            self.assertEqual([row['receipt'][k] for k in ('provider','ticket_number','amount','date','payment_code')],
                             ['capufe','000003','40.50','2026-06-15','28'])
            self.assertEqual(store.get(ids[1])['auto_stage'], 'offline')
            # The user inspected the original and corrected the printed payment.
            receipt = Receipt.model_validate(row['receipt']).model_copy(update={'payment_code':'01'})
            store.save_receipt(row['id'], receipt, ready=True)
            plan.status.return_value = {'status':'ready'}
            with patch('facturador.automation.AI.extract', return_value=extracted(payment_code='28',
                       uncertainties=['El ticket no indica la forma de pago.'])):
                reread = await worker.analyze(row['id'])
            self.assertEqual(reread['receipt']['payment_code'],'01')
            self.assertNotIn('forma de pago', reread['message'])
