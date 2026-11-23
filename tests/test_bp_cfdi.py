"""BP recovery requires the exact printed sale identifier in a timbrado CFDI."""
import unittest

from facturador.cfdi import verify_xml
from facturador.models import Receipt
from test_core import PROFILE, xml_bytes


RECEIPT = Receipt(provider='bp', merchant='BP PLAYAS', amount='100.00',
                  date='2026-11-21', ticket_number='1234567', payment_code='04',
                  details='Estación 11739, consumo de gasolina.')


def bp_xml(identifier):
    document = xml_bytes(issuer='ACO030325PHA').decode('utf-8')
    concept = '<c:Conceptos><c:Concepto NoIdentificacion="' + identifier + '"/></c:Conceptos>'
    return document.replace('<c:Complemento>', concept + '<c:Complemento>').encode('utf-8')


class BpCfdiTests(unittest.TestCase):
    def test_exact_ticket_and_station_prefix_are_required(self):
        good = bp_xml('PL/20876/EXP/ES/2018-1234567')
        self.assertEqual(verify_xml(good, PROFILE, RECEIPT)['match_type'], 'total')
        for identifier in ('PL/20876/EXP/ES/2018-7654321',
                           'OTRA/ESTACION-1234567', 'PL/20876/EXP/ES/2018-12345678'):
            with self.subTest(identifier=identifier), self.assertRaises(ValueError):
                verify_xml(bp_xml(identifier), PROFILE, RECEIPT)

    def test_same_total_and_buyer_do_not_substitute_for_ticket(self):
        other = RECEIPT.model_copy(update={'ticket_number': '7654321'})
        with self.assertRaisesRegex(ValueError, 'ticket'):
            verify_xml(bp_xml('PL/20876/EXP/ES/2018-1234567'), PROFILE, other)

    def test_unknown_station_fails_closed_until_its_mapping_is_verified(self):
        other = RECEIPT.model_copy(update={'details': 'Estación 99881'})
        with self.assertRaisesRegex(ValueError, 'mapeo comprobado'):
            verify_xml(bp_xml('OTRA/ESTACION-1234567'), PROFILE, other)

    def test_same_ticket_and_amount_from_another_issuer_is_rejected(self):
        other_issuer = bp_xml('PL/20876/EXP/ES/2018-1234567').replace(
            b'ACO030325PHA', b'DBM11011043A')
        with self.assertRaisesRegex(ValueError, 'RFC emisor BP'):
            verify_xml(other_issuer, PROFILE, RECEIPT)

    def test_multiple_concepts_are_not_accepted_as_one_ticket(self):
        document = bp_xml('PL/20876/EXP/ES/2018-1234567').decode('utf-8')
        document = document.replace('</c:Conceptos>', '<c:Concepto NoIdentificacion="OTRO-7654321"/></c:Conceptos>')
        with self.assertRaisesRegex(ValueError, 'una sola partida'):
            verify_xml(document.encode('utf-8'), PROFILE, RECEIPT)


if __name__ == '__main__':
    unittest.main()
