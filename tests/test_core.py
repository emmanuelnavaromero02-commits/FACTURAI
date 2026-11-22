import asyncio
import io
import tempfile
import unittest
import zipfile
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from unittest.mock import patch

from fastapi.testclient import TestClient
from PIL import Image

from facturador.app import create_app
from facturador.catalog import allowed_url
from facturador.cfdi import collect_documents, find_valid_pair, verify_xml
from facturador.db import Store
from facturador.documents import reconcile
from facturador.models import Action, Profile, Receipt, Target, money
from facturador.runner import Runner, SUCCESS, action_risk

PROFILE = Profile(rfc="XAXX010101000", name="PRUEBA AUTOMATIZACION", postal_code="76146",
                  regime="612", cfdi_use="G03", email="prueba@example.com")
RECEIPT = Receipt(provider="samara", merchant="SAMARA", amount="100.00", date="2026-11-13",
                  billing_codes=["COD-O0-I1"], payment_code="04")
FISCAL_UUID = "12345678-1234-4234-8234-1234567890AB"


def xml_bytes(**changes):
    root = {"Version":"4.0", "Total":"100.0", "Moneda":"MXN", "FormaPago":"04", "Fecha":"2026-11-16T08:00:00"}
    receiver = {"Rfc":PROFILE.rfc, "Nombre":PROFILE.name, "DomicilioFiscalReceptor":PROFILE.postal_code,
                "RegimenFiscalReceptor":PROFILE.regime, "UsoCFDI":"G03"}
    issuer = changes.pop("issuer", "DBM11011043A")
    stamp = changes.pop("uuid", FISCAL_UUID)
    seal = changes.pop("seal", "SELLO_SINTETICO_NO_VALIDO_ANTE_SAT")
    for key, value in changes.items():
        (receiver if key in receiver else root)[key] = value
    attrs = lambda values: " ".join(key+'="'+str(value)+'"' for key,value in values.items())
    return ('<c:Comprobante xmlns:c="http://www.sat.gob.mx/cfd/4" xmlns:t="http://www.sat.gob.mx/TimbreFiscalDigital" '+attrs(root)+'>'
            '<c:Emisor Rfc="'+issuer+'"/><c:Receptor '+attrs(receiver)+'/>'
            '<c:Complemento><t:TimbreFiscalDigital UUID="'+stamp+'" SelloSAT="'+seal+'"/></c:Complemento></c:Comprobante>').encode()


def pdf_bytes(uuid_value=FISCAL_UUID, rfc=PROFILE.rfc):
    text = "BT /F1 11 Tf 30 100 Td (RFC " + rfc + " UUID " + uuid_value + ") Tj ET"
    objects = ["<< /Type /Catalog /Pages 2 0 R >>", "<< /Type /Pages /Kids [3 0 R] /Count 1 >>",
               "<< /Type /Page /Parent 2 0 R /MediaBox [0 0 600 200] /Resources << /Font << /F1 4 0 R >> >> /Contents 5 0 R >>",
               "<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica >>", "<< /Length "+str(len(text))+" >>\nstream\n"+text+"\nendstream"]
    result = bytearray(b"%PDF-1.4\n"); offsets = [0]
    for number, obj in enumerate(objects, 1):
        offsets.append(len(result)); result.extend((str(number)+" 0 obj\n"+obj+"\nendobj\n").encode())
    xref = len(result)
    result.extend(("xref\n0 6\n0000000000 65535 f \n"+"".join(f"{x:010d} 00000 n \n" for x in offsets[1:])+
                   "trailer\n<< /Size 6 /Root 1 0 R >>\nstartxref\n"+str(xref)+"\n%%EOF").encode())
    return bytes(result)


class MoneyTests(unittest.TestCase):
    def test_decimal_text_normalizes_without_float(self):
        self.assertEqual(str(money("$1,228.48")), "1228.48")
        self.assertEqual(money("131.0"), money("131.00"))

    def test_invalid_amounts_are_rejected(self):
        for value in [0,-1,"NaN","Infinity","1.001",0.1,"1e999"]:
            with self.subTest(value=value), self.assertRaises(ValueError):
                money(value)


class StoreTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(); self.store = Store(Path(self.tmp.name))

    def tearDown(self):
        self.tmp.cleanup()

    def add_ready(self, sha="image1", receipt=RECEIPT):
        row,_ = self.store.add("test.jpg", sha)
        return self.store.save_receipt(row["id"], receipt, ready=True)

    def test_same_image_only_one_record(self):
        first, created = self.store.add("a.jpg", "samehash")
        second, created_again = self.store.add("other.jpg", "samehash")
        self.assertTrue(created); self.assertFalse(created_again); self.assertEqual(first["id"],second["id"])

    def test_other_photo_of_same_ticket_is_duplicate(self):
        first = self.add_ready()
        second = self.add_ready("image2")
        self.assertEqual(second["status"], "duplicate")
        self.assertIn(first["id"], second["message"])

    def test_individual_code_already_in_group_is_duplicate(self):
        group = RECEIPT.model_copy(update={"billing_codes":["ONE","TWO"],"amount":"200.00"})
        self.add_ready(receipt=group)
        row = self.add_ready("another", RECEIPT.model_copy(update={"billing_codes":["TWO"]}))
        self.assertEqual(row["status"], "duplicate")

    def test_only_one_worker_can_claim(self):
        row = self.add_ready()
        with ThreadPoolExecutor(max_workers=5) as workers:
            result = list(workers.map(lambda _: self.store.claim(row["id"]), range(10)))
        self.assertEqual(sum(result),1)

    def test_no_reissue_after_interrupted_commit(self):
        row = self.add_ready(); self.store.update(row["id"],status="issuing")
        self.store.recover_after_restart()
        self.assertEqual(self.store.get(row["id"])["status"],"uncertain")
        self.assertFalse(self.store.claim(row["id"]))
        with self.assertRaises(ValueError): self.store.save_receipt(row["id"],RECEIPT,ready=True)

    def test_queued_not_started_can_be_recovered(self):
        row=self.add_ready(); self.assertTrue(self.store.queue(row["id"]))
        self.store.recover_after_restart()
        self.assertEqual(self.store.get(row["id"])["status"],"ready")

    def test_uuid_unique(self):
        first = self.add_ready()
        self.store.update(first["id"],uuid=FISCAL_UUID)
        second = self.add_ready("next",RECEIPT.model_copy(update={"billing_codes":["OTHER"]}))
        import sqlite3
        with self.assertRaises(sqlite3.IntegrityError): self.store.update(second["id"],uuid=FISCAL_UUID)

    def test_codes_do_not_replace_o_with_zero(self):
        self.assertNotEqual(RECEIPT.identity(), RECEIPT.model_copy(update={"billing_codes":["COD-00-I1"]}).identity())

    def test_uuid_is_canonical_before_uniqueness_check(self):
        first=self.add_ready()
        self.store.update(first['id'],uuid=FISCAL_UUID.lower())
        self.assertEqual(self.store.get(first['id'])['uuid'],FISCAL_UUID)
        second=self.add_ready('image_case',RECEIPT.model_copy(update={'billing_codes':['CASE']}))
        import sqlite3
        with self.assertRaises(sqlite3.IntegrityError): self.store.update(second['id'],uuid=FISCAL_UUID)


class CFDITests(unittest.TestCase):
    def test_valid_structure_and_decimal_comparison(self):
        result=verify_xml(xml_bytes(),PROFILE,RECEIPT)
        self.assertEqual(result["uuid"],FISCAL_UUID); self.assertEqual(result["total"],"100.00")

    def test_profile_amount_and_payment_mismatches(self):
        for changes in [{"Rfc":"XEXX010101000"},{"Nombre":"OTRO RECEPTOR"},{"DomicilioFiscalReceptor":"00000"},
                        {"RegimenFiscalReceptor":"616"},{"UsoCFDI":"S01"},{"Total":"100.01"},{"FormaPago":"28"},{"Moneda":"USD"},
                        {"issuer":"OTR010101AAA"},{"uuid":"no-es-uuid"},{"seal":""}]:
            with self.subTest(changes=changes), self.assertRaises(ValueError): verify_xml(xml_bytes(**changes),PROFILE,RECEIPT)

    def test_entity_declarations_are_rejected(self):
        with self.assertRaises(ValueError): verify_xml(b'<!DOCTYPE x [<!ENTITY y SYSTEM "file:///etc/passwd">]>'+xml_bytes(),PROFILE,RECEIPT)

    def test_pdf_must_contain_matching_uuid_and_rfc(self):
        with tempfile.TemporaryDirectory() as tmp:
            xml=Path(tmp)/"a.xml"; pdf=Path(tmp)/"a.pdf"
            xml.write_bytes(xml_bytes()); pdf.write_bytes(pdf_bytes())
            self.assertEqual(find_valid_pair([xml,pdf],PROFILE,RECEIPT)[0]["uuid"],FISCAL_UUID)
            pdf.write_bytes(pdf_bytes("99999999-1234-4234-8234-1234567890AB"))
            with self.assertRaises(ValueError): find_valid_pair([xml,pdf],PROFILE,RECEIPT)
            pdf.write_bytes(pdf_bytes(rfc="OTR010101AAA"))
            with self.assertRaises(ValueError): find_valid_pair([xml,pdf],PROFILE,RECEIPT)

    def test_two_possible_cfdi_require_selection(self):
        with tempfile.TemporaryDirectory() as tmp:
            paths=[]
            for n,value in enumerate([FISCAL_UUID,"99999999-1234-4234-8234-1234567890AB"]):
                xml=Path(tmp)/(str(n)+".xml"); pdf=Path(tmp)/(str(n)+".pdf")
                xml.write_bytes(xml_bytes(uuid=value)); pdf.write_bytes(pdf_bytes(value)); paths.extend([xml,pdf])
            with self.assertRaisesRegex(ValueError,"varios CFDI"): find_valid_pair(paths,PROFILE,RECEIPT)

    def test_zip_cannot_write_outside_destination(self):
        with tempfile.TemporaryDirectory() as tmp:
            root=Path(tmp); path=root/"a.zip"
            with zipfile.ZipFile(path,"w") as z: z.writestr("../../escaped.xml",xml_bytes())
            files=collect_documents([path],root/"safe")
            self.assertEqual(len(files),1); self.assertTrue(files[0].is_relative_to(root/"safe"))
            self.assertFalse((root/"escaped.xml").exists())

    def test_zip_file_count_limit(self):
        with tempfile.TemporaryDirectory() as tmp:
            path=Path(tmp)/"many.zip"
            with zipfile.ZipFile(path,"w") as z:
                for n in range(101): z.writestr(str(n)+".xml",b"<x/>")
            with self.assertRaises(ValueError): collect_documents([path],Path(tmp)/"out")


class GuardTests(unittest.TestCase):
    def test_hostname_boundary(self):
        self.assertTrue(allowed_url("https://estacionamientos.fibrauno.mx/facturar","samara"))
        for url in ["http://estacionamientos.fibrauno.mx/facturar","https://estacionamientos.fibrauno.mx.evil.invalid/", "https://evil.invalid/?next=estacionamientos.fibrauno.mx", "https://secret@estacionamientos.fibrauno.mx/", "javascript:alert(1)"]:
            with self.subTest(url=url): self.assertFalse(allowed_url(url,"samara"))

    def test_model_cannot_label_issue_as_normal(self):
        a=Action(kind="click",target=Target(by="role",name="Facturar",role="button",exact=True),value=None,reason="paso",risk="normal")
        self.assertEqual(action_risk(a),"issue")
        self.assertEqual(action_risk(a.model_copy(update={"target":a.target.model_copy(update={"name":"Pagar"})})),"forbidden")

    def test_uuid_field_does_not_prove_issuance(self):
        self.assertFalse(SUCCESS.search('textbox "Folio fiscal UUID"'))
        self.assertTrue(SUCCESS.search('generic: Factura emitida'))

    def test_enter_cannot_submit_an_unreviewed_form_by_accident(self):
        action=Action(kind='press',target=Target(by='role',name='Correo',role='textbox',exact=True),value='Enter',reason='seguir',risk='normal')
        self.assertEqual(action_risk(action),'unknown')
        self.assertEqual(action_risk(action.model_copy(update={'target':action.target.model_copy(update={'name':'RFC'})})),'normal')


class AppTests(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory(); self.app=create_app(Path(self.tmp.name)); self.client=TestClient(self.app)
        self.client.__enter__(); self.token=self.client.get('/api/state').json()['token']
        self.headers={'x-app-token':self.token}
        self.client.put('/api/profile',json=PROFILE.model_dump(),headers=self.headers).raise_for_status()

    def tearDown(self):
        self.client.__exit__(None,None,None); self.tmp.cleanup()

    def upload(self):
        data=io.BytesIO(); Image.new('RGB',(30,30),'white').save(data,'PNG')
        return self.client.post('/api/upload',files={'files':('ticket.png',data.getvalue(),'image/png')},headers=self.headers)

    def test_profile_persists_and_token_is_required(self):
        self.assertEqual(self.client.get('/api/state').json()['profile']['rfc'],PROFILE.rfc)
        self.assertEqual(self.client.put('/api/profile',json=PROFILE.model_dump()).status_code,403)

    def test_foreign_origin_rejected(self):
        headers={**self.headers,'origin':'https://evil.invalid'}
        self.assertEqual(self.client.put('/api/profile',json=PROFILE.model_dump(),headers=headers).status_code,403)

    def test_duplicate_upload_reuses_job(self):
        a=self.upload().json()[0]; b=self.upload().json()[0]
        self.assertTrue(a['created']); self.assertFalse(b['created']); self.assertEqual(a['id'],b['id'])

    def test_incomplete_receipt_cannot_be_marked_ready(self):
        key=self.upload().json()[0]['id']
        response=self.client.put('/api/jobs/'+key,json=RECEIPT.model_copy(update={'payment_code':''}).model_dump(),headers=self.headers)
        self.assertEqual(response.status_code,422)

    def test_document_recovery_does_not_reissue(self):
        key=self.upload().json()[0]['id']
        self.client.put('/api/jobs/'+key,json=RECEIPT.model_dump(),headers=self.headers).raise_for_status()
        response=self.client.post('/api/jobs/'+key+'/attachments',files=[('files',('a.xml',xml_bytes(),'application/xml')),('files',('a.pdf',pdf_bytes(),'application/pdf'))],headers=self.headers)
        self.assertEqual(response.status_code,200,response.text)
        row=self.app.state.store.get(key); self.assertEqual(row['status'],'verified'); self.assertFalse(self.app.state.store.claim(key))
        self.assertEqual(self.client.get('/api/jobs/'+key+'/files/pdf').content,pdf_bytes())

    def test_arbitrary_file_download_is_blocked(self):
        key=self.upload().json()[0]['id']; self.app.state.store.update(key,pdf='/etc/passwd')
        self.assertEqual(self.client.get('/api/jobs/'+key+'/files/pdf').status_code,404)

    def test_csv_does_not_execute_ocr_formula(self):
        key=self.upload().json()[0]['id']
        for value in ['=1+1','\t=1+1']:
            self.app.state.store.update(key,receipt=RECEIPT.model_copy(update={'merchant':value}).model_dump())
            self.assertIn("'"+value,self.client.get('/api/export.csv').text)


class FakeLocator:
    def __init__(self,page): self.page=page
    async def count(self): return 1
    async def click(self, *, trial=False, timeout=None):
        if trial: return
        self.page.clicks+=1
        if self.page.success: self.page.body='generic: Factura emitida'
    async def aria_snapshot(self): return self.page.body


class FakePage:
    def __init__(self,success=True):
        self.success=success; self.clicks=0; self.body='Formulario listo'; self.url='https://estacionamientos.fibrauno.mx/facturar'
    async def goto(self,url,**kwargs): self.url=url
    def is_closed(self): return False
    def locator(self,selector): return FakeLocator(self)
    def get_by_role(self,*args,**kwargs): return FakeLocator(self)
    async def screenshot(self,**kwargs):
        if kwargs.get('path'): Path(kwargs['path']).write_bytes(b'fixture')
        return b'fixture'


class FakeContext:
    def __init__(self,page): self.page=page
    async def new_page(self): return self.page
    async def close(self): pass


class FakeAI:
    def __init__(self,retry=False): self.calls=0; self.retry=retry
    def next_action(self,*args):
        self.calls+=1
        if self.calls==1 or self.retry:
            return Action(kind='click',target=Target(by='role',role='button',name='Facturar',exact=True),value=None,reason='Emitir',risk='normal')
        return Action(kind='done',target=None,value=None,reason='Emisión visible; faltan adjuntos',risk='normal')


class RunnerTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.tmp=tempfile.TemporaryDirectory(); self.store=Store(Path(self.tmp.name)); self.runner=Runner(self.store)
        row,_=self.store.add('test.png','unique'); self.key=row['id']; self.store.save_receipt(self.key,RECEIPT,ready=True)

    async def asyncTearDown(self):
        await self.runner.close(); self.tmp.cleanup()

    async def wait_for_approval(self):
        for _ in range(150):
            if self.key in self.runner.pending: return
            await asyncio.sleep(.01)
        self.fail('No apareció revisión de emisión')

    async def test_emission_cannot_click_before_user_approval(self):
        page=FakePage(); self.runner.context=FakeContext(page)
        with patch('facturador.runner.AI',return_value=FakeAI()):
            self.runner.schedule(self.key,PROFILE); task=self.runner.job_tasks[self.key]
            await self.wait_for_approval()
            self.assertEqual(page.clicks,0); self.assertEqual(self.store.get(self.key)['status'],'needs_user')
            self.runner.decide(self.key,'approve'); await asyncio.wait_for(task,5)
        self.assertEqual(page.clicks,1); self.assertEqual(self.store.get(self.key)['status'],'issued_waiting_files')

    async def test_uncertain_result_does_not_repeat_commit(self):
        page=FakePage(success=False); self.runner.context=FakeContext(page)
        with patch('facturador.runner.AI',return_value=FakeAI(retry=True)):
            self.runner.schedule(self.key,PROFILE); task=self.runner.job_tasks[self.key]
            await self.wait_for_approval(); self.runner.decide(self.key,'approve'); await asyncio.wait_for(task,5)
        self.assertEqual(page.clicks,1); self.assertEqual(self.store.get(self.key)['status'],'uncertain')


if __name__=='__main__': unittest.main()
