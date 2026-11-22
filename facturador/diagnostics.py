"""Comprobaciones locales, con datos sintéticos y sin acceder a cuentas externas."""

import asyncio
import io
import zipfile
from pathlib import Path
from tempfile import TemporaryDirectory

from PIL import Image, ImageDraw, ImageFont
from pypdf import PdfReader

from .cfdi import collect_documents, verify_xml
from .local_reader import ocr_languages, ocr_text, printed_codes, render_pdf_page
from .models import Profile, Receipt


def item(name, status, detail, fallback):
    return {'name': name, 'status': status, 'detail': detail, 'fallback': fallback}


def local_checks(store):
    def storage():
        with store.connect() as db:
            if db.execute('PRAGMA quick_check(1)').fetchone()[0] != 'ok':
                raise ValueError('SQLite no pudo verificar el archivo.')
            db.execute('SAVEPOINT diagnostico')
            db.execute("INSERT INTO events(job_id,at,kind,detail) VALUES(NULL,'','diagnostico','')")
            db.execute('ROLLBACK TO diagnostico')
        return 'SQLite leyó y escribió en el volumen. La operación de prueba se revirtió.'

    def ocr():
        ocr_languages.cache_clear()
        photo = Image.new('L', (560, 100), 255)
        ImageDraw.Draw(photo).text((12, 20), 'FACTURA 12345', font=ImageFont.load_default(size=43), fill=0)
        if '12345' not in ocr_text(photo):
            raise ValueError('La lectura de la muestra no coincidió.')
        return 'Texto leído con Tesseract (' + ocr_languages() + ').'

    def barcodes():
        import zxingcpp
        for format_ in (zxingcpp.BarcodeFormat.QRCode, zxingcpp.BarcodeFormat.Code128):
            code = zxingcpp.write_barcode_to_image(zxingcpp.create_barcode('FACTURAI1234', format_), scale=3)
            photo = Image.frombytes('L', (code.shape[1], code.shape[0]), bytes(code))
            if 'FACTURAI1234' not in printed_codes(photo):
                raise ValueError('El lector no reconoció un código de prueba.')
        return 'Se decodificaron un QR y un código de barras.'

    def pdf_zip():
        with TemporaryDirectory(dir=store.root) as folder:
            path = Path(folder)
            source = io.BytesIO()
            Image.new('RGB', (150, 80), 'white').save(source, 'PDF')
            archive = path / 'muestra.zip'
            with zipfile.ZipFile(archive, 'w') as bundle:
                bundle.writestr('muestra.pdf', source.getvalue())
            pdf, = collect_documents([archive], path / 'extraido')
            if len(PdfReader(pdf).pages) != 1 or min(render_pdf_page(pdf, 0).size) < 1:
                raise ValueError('No se pudo leer la página extraída.')
        return 'Se escribió un ZIP, se extrajo el PDF y se renderizó su página.'

    def check_xml():
        buyer = Profile(rfc='XAXX010101000', name='PRUEBA LOCAL', postal_code='01000', regime='616',
                        cfdi_use='S01', email='prueba@example.invalid')
        sample = (b'<c:Comprobante xmlns:c="http://www.sat.gob.mx/cfd/4" xmlns:t="http://www.sat.gob.mx/TimbreFiscalDigital" '
                  b'Version="4.0" Total="10.00" Moneda="MXN" FormaPago="01"><c:Receptor Rfc="XAXX010101000" '
                  b'Nombre="PRUEBA LOCAL" DomicilioFiscalReceptor="01000" RegimenFiscalReceptor="616" UsoCFDI="S01"/>'
                  b'<c:Complemento><t:TimbreFiscalDigital UUID="00000000-0000-4000-8000-000000000001" '
                  b'SelloSAT="muestra-local"/></c:Complemento></c:Comprobante>')
        ticket = Receipt(amount='10.00', payment_code='01')
        verify_xml(sample, buyer, ticket)
        try:
            verify_xml(sample, buyer, ticket.model_copy(update={'amount': '11.00'}))
        except ValueError:
            return 'El cotejo aceptó un XML de prueba y detectó un importe distinto.'
        raise ValueError('El cotejo aceptó un importe incorrecto.')

    checks = []
    for name, check, fallback in (
        ('Expedientes y almacenamiento', storage, 'Conserva los originales y revisa el volumen de Docker.'),
        ('OCR local', ocr, 'Usa los códigos, el texto digital, la lectura con IA o la captura manual.'),
        ('QR y códigos de barras', barcodes, 'Copia el folio impreso o pega la liga en Portales.'),
        ('PDF y ZIP', pdf_zip, 'Sube fotos de los tickets o los comprobantes por separado.'),
        ('Datos del CFDI 4.0', check_xml, 'Conserva los archivos originales y revisa las diferencias en el expediente.'),
    ):
        try:
            checks.append(item(name, 'ok', check(), fallback))
        except Exception:
            checks.append(item(name, 'error', 'Falló la comprobación local.', fallback))
    return checks


async def check_tools(runner, gmail):
    results = await asyncio.to_thread(local_checks, runner.store)
    name = 'Navegador y Playwright'
    fallback = 'Reabre el navegador o importa comprobantes desde un archivo.'
    if runner.lock.locked() or runner.has_active_jobs():
        results.append(item(name, 'busy', 'Hay un trámite en curso. Se conservó la página activa.', fallback))
    else:
        async with runner.lock:
            page = None
            try:
                await runner.start_browser()
                page = await runner.context.new_page()
                await page.set_content('<label>Prueba<input></label>')
                await page.get_by_label('Prueba').fill('FACTURAI')
                snapshot = await page.locator('body').aria_snapshot()
                frame = await page.screenshot(type='png', timeout=5000)
                if 'FACTURAI' not in snapshot or not frame.startswith(b'\x89PNG'):
                    raise ValueError('No se confirmó el control del navegador.')
                results.append(item(name, 'ok', runner.engine + ' completó el formulario, la lectura accesible y la captura.', fallback))
            except Exception:
                results.append(item(name, 'error', 'El navegador interno no completó la prueba.', fallback))
            finally:
                if page and not page.is_closed():
                    try:
                        await page.close()
                    except Exception:
                        pass  # Chromium may already have exited during the probe.
    plan = runner.plan.status()['status']
    gmail_state = gmail.status()
    results.extend([
        item('Cuenta de ChatGPT', 'configured' if plan == 'ready' else 'pending',
             'Autorización guardada.' if plan == 'ready' else 'Revisa tu conexión o cupo en Ajustes.',
             'Opera el ticket a mano en el navegador interno.'),
        item('Gmail', 'configured' if gmail_state == 'connected' else 'pending',
             'Autorización guardada.' if gmail_state == 'connected' else 'Gmail requiere vinculación.',
             'Importa el correo .eml o los archivos de la factura.'),
        item('WhatsApp Web', 'manual', 'La vinculación se comprueba en la pantalla de WhatsApp.',
             'Importa fotos o el ZIP exportado del chat.'),
        item('Portales y CAPTCHA', 'manual', 'El comercio verifica el ticket y la sesión al iniciar cada trámite.',
             'Toma el control del navegador, o recupera la factura existente por correo.'),
    ])
    return results
