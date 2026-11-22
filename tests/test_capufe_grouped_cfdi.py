"""Cotejo de CFDI de peajes con datos enteramente ficticios."""
import tempfile
import unittest
from pathlib import Path
from xml.etree import ElementTree as ET

from facturador.catalog import CATALOG
from facturador.cfdi import NS, find_valid_pair, normalize_capufe_code, verify_xml
from test_core import FISCAL_UUID, PROFILE, RECEIPT, pdf_bytes


FIRST = '0ABC12DEFGHJKLMNPQ'
SECOND = '7QRS89TUVWXYZABCDE'
THIRD = '5TST54QWERTYUIOPAS'


def toll(codes=(FIRST,), amount='50.00'):
    return RECEIPT.model_copy(update={'provider':'capufe', 'amount':amount, 'billing_codes':list(codes), 'payment_code':'01'})


def grouped_xml():
    items = ''.join(f'''
        <cfdi:Concepto NoIdentificacion="{code}" Cantidad="1" ValorUnitario="43.10" Importe="43.10" Descripcion="Peaje de prueba" ObjetoImp="02">
          <cfdi:Impuestos><cfdi:Traslados><cfdi:Traslado Base="43.10" Impuesto="002" TipoFactor="Tasa" TasaOCuota="0.160000" Importe="6.90"/></cfdi:Traslados></cfdi:Impuestos>
        </cfdi:Concepto>''' for code in (FIRST, SECOND))
    return ET.fromstring(f'''<cfdi:Comprobante xmlns:cfdi="{NS['c']}" xmlns:tfd="{NS['t']}" Version="4.0"
      Fecha="2026-01-10T09:30:00" TipoDeComprobante="I" Moneda="MXN" FormaPago="01" SubTotal="86.20" Total="100.00">
      <cfdi:Emisor Rfc="{CATALOG['capufe']['issuer_rfcs'][0]}"/>
      <cfdi:Receptor Rfc="{PROFILE.rfc}" Nombre="{PROFILE.name}" DomicilioFiscalReceptor="{PROFILE.postal_code}"
            RegimenFiscalReceptor="{PROFILE.regime}" UsoCFDI="{PROFILE.cfdi_use}"/>
      <cfdi:Conceptos>{items}</cfdi:Conceptos>
      <cfdi:Impuestos TotalImpuestosTrasladados="13.80"><cfdi:Traslados>
        <cfdi:Traslado Base="86.20" Impuesto="002" TipoFactor="Tasa" TasaOCuota="0.160000" Importe="13.80"/>
      </cfdi:Traslados></cfdi:Impuestos>
      <cfdi:Complemento><tfd:TimbreFiscalDigital Version="1.1" UUID="{FISCAL_UUID}" SelloSAT="SELLO-DE-PRUEBA"/></cfdi:Complemento>
    </cfdi:Comprobante>''')


class GroupedTollTests(unittest.TestCase):
    def check(self, root, receipt=None):
        return verify_xml(ET.tostring(root), PROFILE, receipt or toll())

    def test_fragment_from_full_invoice_is_identified_by_its_own_concept(self):
        receipt = toll(('0ABC 12DE-FGHJ KLMNPQ',))
        result = self.check(grouped_xml(), receipt)
        self.assertEqual(result['match_type'], 'agrupado')
        self.assertEqual((result['total'], result['ticket_total'], result['concept_count']), ('100.00', '50.00', 2))
        self.assertEqual((result['matched_codes'], result['matched_concepts'], result['invoice_codes']), ([FIRST], [0], [FIRST, SECOND]))
        other = self.check(grouped_xml(), toll((SECOND.lower(),)))
        self.assertEqual((other['matched_codes'], other['matched_concepts']), ([SECOND], [1]))
        combined = self.check(grouped_xml(), toll((SECOND,FIRST), '100.00'))
        self.assertEqual((combined['match_type'], combined['matched_concepts'], combined['ticket_total']), ('total', [1,0], '100.00'))

    def test_no_fuzzy_match_by_same_amount_description_duplicate_or_partial_code(self):
        for code in ('', FIRST[:-1], 'X'+FIRST, FIRST+'0', FIRST.replace('0','O'), THIRD, FIRST[:5]+'_'+FIRST[5:]):
            with self.subTest(code=code), self.assertRaises(ValueError):
                self.check(grouped_xml(),toll((code,)))
        for codes in ([], [FIRST,FIRST], [FIRST,SECOND,THIRD], [FIRST, '0ABC 12DE FGHJ KLMNPQ']):
            with self.subTest(codes=codes), self.assertRaises(ValueError):
                self.check(grouped_xml(),toll(codes))
        for value in (FIRST+'X','Z'+FIRST,SECOND, 'REFERENCIA '+FIRST, ''):
            doc = grouped_xml()
            item = doc.find('c:Conceptos/c:Concepto',NS)
            item.set('NoIdentificacion',value)
            item.set('Descripcion','Ticket '+FIRST)
            with self.subTest(value=value), self.assertRaises(ValueError):
                self.check(doc)
        with self.assertRaises(ValueError):
            self.check(grouped_xml(), toll((FIRST,), '100.00'))
        self.assertNotEqual(normalize_capufe_code('O'+FIRST[1:]), normalize_capufe_code(FIRST))

    def test_no_best_effort_rounding_or_invalid_arithmetic(self):
        for xpath, attr, value in (
                ('.','Total','100.01'), ('.','SubTotal','86.21'), ('.','Descuento','0.01'),
                ('c:Conceptos/c:Concepto','Importe','43.11'), ('c:Conceptos/c:Concepto','Cantidad','0'),
                ('c:Conceptos/c:Concepto','ValorUnitario','43.10999'), ('c:Conceptos/c:Concepto','Descuento','-3'),
                ('c:Conceptos/c:Concepto','Importe','4.31e1'),
                ('c:Conceptos/c:Concepto/c:Impuestos/c:Traslados/c:Traslado','Importe','6.91'),
                ('c:Conceptos/c:Concepto/c:Impuestos/c:Traslados/c:Traslado','Base','10.00'),
                ('c:Impuestos','TotalImpuestosTrasladados','13.79'), ('c:Impuestos/c:Traslados/c:Traslado','Base','86.19'),
                ('c:Impuestos/c:Traslados/c:Traslado','Importe', '13.800001'), ('.','Total','NaN')):
            doc = grouped_xml()
            doc.find(xpath, NS).set(attr,value)
            with self.subTest(xpath=xpath,attr=attr,value=value), self.assertRaises(ValueError):
                self.check(doc)
        with self.assertRaises(ValueError):
            self.check(grouped_xml(),toll((FIRST,), '49.99'))

    def test_discount_and_withholding_are_charged_only_to_the_selected_concept(self):
        doc=grouped_xml()
        item=doc.find('c:Conceptos/c:Concepto',NS)
        item.set('Importe','45.10')
        item.set('ValorUnitario','45.10')
        item.set('Descuento','2.00')
        taxroot=item.find('c:Impuestos',NS)
        retain=ET.SubElement(taxroot,'{'+NS['c']+'}Retenciones')
        ET.SubElement(retain,'{'+NS['c']+'}Retencion',
                      {'Base':'43.10','TipoFactor':'Tasa','TasaOCuota':'0.100000','Importe':'4.31','Impuesto':'001'})
        doc.set('SubTotal','88.20')
        doc.set('Descuento','2.00')
        doc.set('Total','95.69')
        summary=doc.find('c:Impuestos',NS)
        summary.set('TotalImpuestosRetenidos','4.31')
        lines=ET.SubElement(summary,'{'+NS['c']+'}Retenciones')
        ET.SubElement(lines,'{'+NS['c']+'}Retencion', {'Impuesto':'001','Importe':'4.31'})
        self.assertEqual(self.check(doc,toll((FIRST,), '45.69'))['ticket_total'], '45.69')
        self.assertEqual(self.check(doc,toll((SECOND,)))['ticket_total'], '50.00')
        summary.set('TotalImpuestosRetenidos','4.30')
        with self.assertRaises(ValueError):
            self.check(doc,toll((FIRST,), '45.69'))

    def test_stamp_receiver_issuer_currency_payment_and_pair_must_match(self):
        for xpath, attr, value in (
                ('c:Receptor','Rfc','OTRO'),('c:Receptor','Nombre','OTRA PERSONA'),
                ('c:Receptor','DomicilioFiscalReceptor','01000'),('c:Receptor','RegimenFiscalReceptor','601'),
                ('c:Receptor','UsoCFDI','S01'),('c:Emisor','Rfc','XEXX010101000'),('.','Moneda','USD'),
                ('.','FormaPago','04'),('.','TipoDeComprobante','E'),
                ('c:Complemento/t:TimbreFiscalDigital','UUID','NO-ES-UUID'),('c:Complemento/t:TimbreFiscalDigital','SelloSAT','')):
            root=grouped_xml()
            root.find(xpath,NS).set(attr,value)
            with self.subTest(xpath=xpath,attr=attr), self.assertRaises(ValueError):
                self.check(root)
        with tempfile.TemporaryDirectory() as tmp:
            root=Path(tmp)
            xml,pdf=root/'factura.xml',root/'factura.pdf'
            xml.write_bytes(ET.tostring(grouped_xml()))
            pdf.write_bytes(pdf_bytes())
            result=find_valid_pair([pdf,xml], PROFILE, toll(), FISCAL_UUID)[0]
            self.assertEqual((result['uuid'],result['ticket_total']), (FISCAL_UUID, '50.00'))
            for body in (pdf_bytes(rfc=PROFILE.rfc+'X'),pdf_bytes(rfc='OTRO'),pdf_bytes('99999999-1234-4234-8234-1234567890AB')):
                pdf.write_bytes(body)
                with self.assertRaises(ValueError):
                    find_valid_pair([pdf,xml],PROFILE,toll())
            pdf.write_bytes(pdf_bytes())
            variant=root/'otra.xml'
            altered=grouped_xml()
            altered.set('Folio','OTRO')
            variant.write_bytes(ET.tostring(altered))
            with self.assertRaisesRegex(ValueError,'mismo UUID'):
                find_valid_pair([xml,variant,pdf],PROFILE,toll())
