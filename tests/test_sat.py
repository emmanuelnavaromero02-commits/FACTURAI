import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch
from urllib.parse import parse_qs, urlsplit
from xml.etree import ElementTree as ET

import httpx
from fastapi import FastAPI
from fastapi.testclient import TestClient

from facturador import sat
from facturador.db import Store
from facturador.documents import reconcile
from test_core import FISCAL_UUID, PROFILE, RECEIPT, pdf_bytes, xml_bytes


def acuse(code, estado):
    root = ET.Element(f'{{{sat.SOAP}}}Envelope')
    body = ET.SubElement(root, f'{{{sat.SOAP}}}Body')
    response = ET.SubElement(body, f'{{{sat.SERVICE}}}ConsultaResponse')
    result = ET.SubElement(response, f'{{{sat.SERVICE}}}ConsultaResult')
    ET.SubElement(result, f'{{{sat.ACUSE}}}CodigoEstatus').text = code
    ET.SubElement(result, f'{{{sat.ACUSE}}}Estado').text = estado
    return ET.tostring(root)


class SatChecks(unittest.TestCase):
    def setUp(self):
        folder = tempfile.TemporaryDirectory()
        self.addCleanup(folder.cleanup)
        self.root = Path(folder.name)
        self.store = Store(self.root)
        draft, _ = self.store.add('foto.jpg', 'foto-sintetica')
        self.key = draft['id']
        self.store.save_receipt(self.key, RECEIPT)
        self.url = f'/api/jobs/{self.key}/sat'
        self.client = self.client_for_store()

    def client_for_store(self):
        app = FastAPI()
        app.include_router(sat.sat_router(self.store))
        client = TestClient(app)
        self.addCleanup(client.close)
        return client

    def archive(self, **xml_attrs):
        xml = self.root / 'emisor.xml'
        pdf = self.root / 'emisor.pdf'
        xml.write_bytes(xml_bytes(**{'Sello': 'aGVsbG8+Z/abcQ==', **xml_attrs}))
        pdf.write_bytes(pdf_bytes())
        reconcile(self.store, self.key, PROFILE, [xml, pdf])

    def test_unverified_never_contacts_sat(self):
        with patch.object(sat, 'query_sat') as service:
            self.assertEqual(self.client.get(self.url).json()['status'], 'sin_consultar')
            self.assertEqual(self.client.post(self.url).status_code, 409)
            service.assert_not_called()

    def test_queries_sat_persists_and_retains_last_confirmation(self):
        self.archive()
        replies = iter([(200, acuse('S - obtenido', 'Vigente')), (200, acuse('S - obtenido', 'CANCELADO'))] + [(503, b'')] * 3)
        def service(request):
            self.assertEqual(request.method, 'POST')
            self.assertEqual(str(request.url), sat.ENDPOINT)
            self.assertEqual(request.headers['SOAPAction'].strip('"'), sat.ACTION)
            doc = ET.fromstring(request.content)
            expression = doc.findtext(f'{{{sat.SOAP}}}Body/{{{sat.SERVICE}}}Consulta/{{{sat.SERVICE}}}expresionImpresa')
            self.assertEqual(urlsplit(expression).hostname, 'verificacfdi.facturaelectronica.sat.gob.mx')
            self.assertEqual(parse_qs(urlsplit(expression).query), {
                'id': [FISCAL_UUID], 're': ['DBM11011043A'], 'rr': [PROFILE.rfc], 'tt': ['100.0'], 'fe': ['Z/abcQ=='],
            })
            self.assertNotIn(b'PRUEBA AUTOMATIZACION', request.content)
            self.assertNotIn(b'SELLO_SINTETICO', request.content)
            status, content = next(replies)
            return httpx.Response(status, content=content)
        with httpx.Client(transport=httpx.MockTransport(service)) as http:
            with patch.object(sat.httpx, 'stream', http.stream), patch('facturador.read_http.time.sleep'):
                self.assertEqual(self.client.post(self.url).json()['status'], 'vigente')
                canceled = self.client.post(self.url).json()
                self.assertEqual(canceled['status'], 'cancelado')
                failure = self.client.post(self.url).json()
                self.assertEqual(failure['status'], 'no_disponible')
                self.assertEqual(failure['last_confirmed'], {'status': 'cancelado', 'checked_at': canceled['checked_at']})
        self.assertEqual(self.client_for_store().get(self.url).json(), failure)
        self.assertEqual(self.store.get(self.key)['status'], 'verified')
        self.assertFalse(self.store.queue(self.key, PROFILE))

    def test_rejected_expression_not_confused_with_not_found(self):
        self.assertEqual(sat._read_status(acuse('N - 602: Comprobante no encontrado.', 'No Encontrado'))[0], 'no_encontrado')
        self.assertEqual(sat._read_status(acuse('N 601: Expresión inválida', 'No Encontrado'))[0], 'no_disponible')
        self.assertEqual(sat._read_status(acuse('N-601', 'Vigente'))[0], 'no_disponible')

    def test_special_rfc_and_seal_in_printed_expression(self):
        self.store.save_receipt(self.key, RECEIPT.model_copy(update={'provider': 'solicitud_directa'}))
        self.archive(issuer='Ñ&amp;A010101AAA', Total='00100.000000', Sello='AAAAab+/cd==')
        packet = ET.fromstring(sat._envelope(self.store, self.store.get(self.key)))
        expression = packet.findtext(f'{{{sat.SOAP}}}Body/{{{sat.SERVICE}}}Consulta/{{{sat.SERVICE}}}expresionImpresa')
        self.assertIn('&re=Ñ&amp;A010101AAA&', expression)
        self.assertTrue(expression.endswith('&tt=100.0&fe=ab+/cd=='))

    def test_malformed_unexpected_and_large_responses_or_timeout_are_unavailable(self):
        cases = [b'<html>Portal en mantenimiento</html>', b'<!DOCTYPE node><node/>',
                 acuse('S - obtenido', 'nuevo_estado'), b'A' * (sat.MAX_REPLY + 1)]
        for data in cases:
            with self.subTest(size=len(data)), httpx.Client(transport=httpx.MockTransport(
                    lambda req: httpx.Response(200, content=data))) as http:
                with patch.object(sat.httpx, 'stream', http.stream):
                    self.assertEqual(sat.query_sat(b'prueba')[0], 'no_disponible')
        with patch.object(sat.httpx, 'stream', side_effect=httpx.ReadTimeout('timeout')):
            self.assertEqual(sat.query_sat(b'prueba')[0], 'no_disponible')

    def test_only_archived_matching_xml_can_supply_query_parameters(self):
        self.archive()
        row = self.store.get(self.key)
        xml = Path(row['xml'])
        original = xml.read_bytes()
        bad_uuid = '22345678-1234-4234-8234-1234567890AB'
        with patch.object(sat, 'query_sat') as service:
            xml.write_bytes(original.replace(FISCAL_UUID.encode(), bad_uuid.encode()))
            self.assertEqual(self.client.post(self.url).status_code, 409)
            xml.write_bytes(original.replace(b'Sello="aGVsbG8+Z/abcQ=="', b'Sello=""'))
            self.assertEqual(self.client.post(self.url).status_code, 409)
            self.store.update(self.key, xml=str(self.root / 'emisor.xml'))
            self.assertEqual(self.client.post(self.url).status_code, 409)
            service.assert_not_called()
