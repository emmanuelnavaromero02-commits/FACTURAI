import base64
import unittest
from types import SimpleNamespace

from facturador.capufe_recovery import HOST, LOOKUP, LookupProblem, _is_lookup, _payload, _reply


class TollLookupTests(unittest.TestCase):
    def test_network_gate_only_accepts_the_official_read_operation_for_this_code(self):
        key = 'X' * 18
        req = SimpleNamespace(method='POST', post_data=None, url=f'https://{HOST}{LOOKUP}?codigo={key}')
        self.assertTrue(_is_lookup(req, key))
        self.assertFalse(_is_lookup(req, 'Y' * 18))
        for url in (req.url.replace(HOST, 'otro.example'), req.url.replace(LOOKUP, '/timbrar'), req.url + '&emitir=1'):
            self.assertFalse(_is_lookup(SimpleNamespace(method='POST', post_data=None,url=url), key))

    def test_an_ambiguous_negative_is_not_evidence_of_nonissuance(self):
        for msg in ('El código aún no ha sido facturado.', 'El código no se encuentra facturado'):
            self.assertEqual(_reply({'mensaje':msg})[:2], ({},True))
        for msg in ('No existe información con los valores ingresados', 'El código ya se encuentra facturado.',
                    'Límite de consultas', 'Código inválido', 'Factura cancelada'):
            self.assertEqual(_reply({'mensaje':msg})[:2], ({},False))

    def test_recover_actual_bytes_and_preserve_the_cotejo_as_a_separate_step(self):
        pdf = b'%PDF-1.7\nficticio'
        xml = b'<?xml version="1.0"?><cfdi:Comprobante />'
        files, negative, _ = _reply({'documentos':{'pdf':base64.b64encode(pdf).decode(),'xml':xml.decode()},
                                  'mensaje':'El código aún no ha sido facturado.'})
        self.assertEqual(files, {'pdf':pdf, 'xml':xml})
        self.assertFalse(negative)
        self.assertEqual(_reply({'pdf':'archivos no disponibles'})[:2], ({},False))

    def test_direct_files_are_candidates_and_html_errors_cannot_authorize_another_issue(self):
        for kind, raw in [('zip',b'PK\x03\x04doc'),('pdf',b'%PDF-1.6'),('xml',b'<cfdi:Comprobante/>')]:
            self.assertEqual(_payload(raw), ({kind:raw}, False, ''))
        with self.assertRaises(LookupProblem):
            _payload(b'<html>Consulta no disponible</html>')
