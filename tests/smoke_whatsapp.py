"""Prueba de WhatsApp con el DOM, selector de archivos y descargas reales del navegador.

Todo el tráfico se intercepta antes de navegar. El número pertenece al bloque
ficticio +1 202 555 01xx y cada ejecución usa un perfil temporal.
"""

import asyncio
from io import BytesIO
import json
import os
from pathlib import Path
from tempfile import TemporaryDirectory
from urllib.parse import urlsplit

from playwright.async_api import async_playwright
from pypdf import PdfWriter

from facturador.db import Store
from facturador.runner import Runner
from facturador.whatsapp import WhatsAppWeb


PHONE = '12025550103'
REFERENCE = 'FACTURAI-0A0123456789BEEF'


def pdf_bytes(title):
    stream = BytesIO()
    writer = PdfWriter()
    writer.add_blank_page(width=100, height=100)
    writer.add_metadata({'/Title': title})
    writer.write(stream)
    return stream.getvalue()

CHAT = """<!doctype html><html lang="es"><meta charset="utf-8">
<main id="main">
  <header><span title="+1 202 555 0103">+1 202 555 0103</span></header>
  <section id="messages" aria-label="Mensajes"></section>
  <footer>
    <button aria-label="Adjuntar" title="Adjuntar" id="attach">Adjuntar</button>
    <button id="doc" hidden>Documento</button>
    <input id="files" type="file" multiple accept="*" hidden>
    <div role="textbox" aria-label="Escribe un mensaje" contenteditable="true"></div>
  </footer>
  <div id="preview" role="dialog" hidden>
    <div id="selection"></div>
    <button id="send" aria-label="Enviar">Enviar</button>
  </div>
</main>
<script>
const qs = new URL(location.href).searchParams;
const phone = qs.get('phone');
const chat = document.getElementById('messages');
const editor = document.querySelector('[contenteditable]');
const source = JSON.parse(localStorage.getItem(phone) || '[]');
const replies = REPLY_NAMES;
if (phone === '12025550109') {
  const title = document.querySelector('header span');
  title.title = title.textContent = 'Contacto guardado de prueba';
  title.onclick = () => {
    const drawer = document.createElement('div');
    drawer.dataset.testid = 'contact-info-drawer';
    drawer.textContent = 'Datos del negocio sin teléfono visible';
    document.body.append(drawer);
  };
} else if (phone !== EXPECTED_PHONE) {
  document.querySelector('header span').title = '+1 202 555 0199';
  document.querySelector('header span').textContent = '+1 202 555 0199';
}
const draftKey = 'draft:' + phone;
editor.textContent = qs.get('text') || localStorage.getItem(draftKey) || '';
editor.addEventListener('input', () => localStorage.setItem(draftKey, editor.innerText));
function row(type, text) {
  const outer = document.createElement('div');
  outer.className = 'message-' + type;
  outer.append(document.createTextNode(text));
  chat.append(outer);
  return outer;
}
function incoming(name) {
  const item = row('in',name);
  const link = document.createElement('button');
  link.setAttribute('aria-label','Descargar');
  link.textContent = 'Descargar';
  link.onclick = async () => {
    const result = await fetch('/synthetic-files/' + name);
    if (!result.ok) return;
    const anchor = document.createElement('a');
    anchor.href = URL.createObjectURL(await result.blob());
    anchor.download = name;
    document.body.append(anchor);
    anchor.click();
    anchor.remove();
  };
  item.append(' ',link);
}
function outgoing(text) {
  source.push(text);
  localStorage.setItem(phone, JSON.stringify(source));
  row('out',text);
}
incoming('fuera-del-expediente.pdf');
source.forEach(text => row('out',text));
replies.forEach(incoming);
if (replies.length) {
  row('out','Solicito otro CFDI. Referencia FACTURAI-1111222233334444');
  incoming('otro-pedido.pdf');
}
editor.addEventListener('keydown', async event => {
  if (event.key !== 'Enter' || event.shiftKey) return;
  event.preventDefault();
  const text = editor.innerText;
  editor.textContent = '';
  localStorage.removeItem(draftKey);
  outgoing(text);
  await record({kind:'text',phone,text});
});
const attach = document.getElementById('attach');
const doc = document.getElementById('doc');
const files = document.getElementById('files');
const preview = document.getElementById('preview');
attach.onclick = () => { doc.hidden = false; };
doc.onclick = () => { files.click(); doc.hidden = true; };
files.onchange = () => {
  document.getElementById('selection').textContent = [...files.files].map(f => f.name).join(' ');
  preview.hidden = false;
};
document.getElementById('send').onclick = async () => {
  for (const file of files.files) {
    await record({kind:'file',phone,name:file.name,bytes:Array.from(new Uint8Array(await file.arrayBuffer()))});
    outgoing(file.name);
  }
  preview.hidden = true;
};
</script></html>"""


async def smoke():
    traffic, outbound, arrivals = [], [], []
    replies = {'factura.pdf': pdf_bytes('CFDI'), 'factura.xml': b'<muestra>CFDI de prueba</muestra>'}
    docs = [
        {'name': 'ticket.pdf', 'mimeType': 'application/pdf', 'buffer': pdf_bytes('TICKET')},
        {'name': 'constancia.pdf', 'mimeType': 'application/pdf', 'buffer': pdf_bytes('CONSTANCIA')},
    ]

    async def intercept(route):
        url = urlsplit(route.request.url)
        traffic.append((url.netloc, url.path))
        if url.scheme != 'https' or url.netloc != 'web.whatsapp.com':
            return await route.abort()
        if url.path.rstrip('/') in ('', '/send'):
            html = CHAT.replace('REPLY_NAMES', json.dumps(arrivals)).replace('EXPECTED_PHONE', json.dumps(PHONE))
            await route.fulfill(content_type='text/html', body=html)
            return
        name = url.path.removeprefix('/synthetic-files/')
        if url.path.startswith('/synthetic-files/') and name in replies:
            await route.fulfill(body=replies[name], content_type='application/octet-stream',
                                headers={'Content-Disposition': f'attachment; filename="{name}"'})
            return
        await route.fulfill(status=404, body='')

    with TemporaryDirectory() as folder:
        async with async_playwright() as playwright:
            if os.getenv('FACTURADOR_SMOKE_FIREFOX') == '1':
                browser = await playwright.firefox.launch()
            else:
                browser = await playwright.chromium.launch(executable_path=os.getenv('FACTURADOR_CHROMIUM_PATH'),
                                                           chromium_sandbox=True)
            try:
                context = await browser.new_context(accept_downloads=True, service_workers='block')
                await context.route('**/*', intercept)
                await context.expose_binding('record', lambda _, message: outbound.append(message))
                runner = Runner(Store(Path(folder)))
                # The real UI and network hooks run in an isolated context. No browser profile is shared.
                runner.context = context
                context.on('page', runner.register_page)
                web = WhatsAppWeb(runner)
                attempts = []
                message = f'Solicitud de prueba {REFERENCE}. Enviar el CFDI al correo de prueba.'
                await web.send(PHONE, REFERENCE, message, docs, lambda: attempts.append(True))
                assert attempts == [True], attempts
                assert [m['text'] for m in outbound if m['kind'] == 'text'] == [message], outbound
                assert all(m['phone'] == PHONE for m in outbound), outbound
                received = {m['name']: bytes(m['bytes']) for m in outbound if m['kind'] == 'file'}
                assert received == {doc['name']: doc['buffer'] for doc in docs}, received.keys()

                arrivals.extend(replies)
                downloads = {}
                await web.recover(PHONE, REFERENCE, downloads.__setitem__)
                assert downloads == replies, downloads.keys()
                assert not any(path.endswith(('fuera-del-expediente.pdf', 'otro-pedido.pdf'))
                               for _, path in traffic), traffic
                before = len(outbound)

                portal = await context.new_page()
                await portal.set_content('<h1>Portal que utiliza el operador</h1>')
                await runner.viewer.show(portal, 'Portal del operador', manual=True)
                channel = await runner.viewer.take_control(runner.viewer.revision)
                revision = runner.viewer.revision
                original_pages = list(context.pages)
                opened = []
                context.on('page', lambda page: opened.append(page))
                downloads.clear()

                def receive_in_background(name, data):
                    assert runner.viewer.page is portal and runner.viewer.revision == revision
                    assert runner.viewer.channel == channel
                    downloads[name] = data

                count = await web.recover(PHONE, REFERENCE, receive_in_background, background=True)
                assert count == len(replies) and downloads == replies, downloads.keys()
                assert len(opened) == 1 and opened[0].is_closed() and context.pages == original_pages
                assert runner.viewer.page is portal and runner.viewer.revision == revision
                assert runner.viewer.channel == channel and len(outbound) == before

                # Simulate a draft already stored by the account in this conversation.
                draft = 'Borrador del operador que todavía está escribiendo'
                await runner.whatsapp.evaluate('([key, value]) => localStorage.setItem(key, value)',
                                               ['draft:' + PHONE, draft])
                try:
                    await web.send(PHONE, 'SOLICITUD-NUEVA', 'Otra prueba', docs, lambda: attempts.append(True))
                except ValueError as error:
                    assert 'borrador' in str(error).lower(), str(error)
                else:
                    raise AssertionError('Se sobrescribió un borrador del operador al enviar.')
                assert await runner.whatsapp.evaluate('(key) => localStorage.getItem(key)', 'draft:' + PHONE) == draft
                for wrong_recipient in ('12025550107', '12025550109'):
                    try:
                        await web.send(wrong_recipient, 'OTRA-REFERENCIA', 'Prueba de destinatario', docs,
                                       lambda: attempts.append(True))
                    except ValueError as error:
                        assert 'número' in str(error), str(error)
                    else:
                        raise AssertionError('No se rechazó el destinatario diferente o sin teléfono verificable.')
                assert len(outbound) == before and attempts == [True]
                assert all(host == 'web.whatsapp.com' for host, _ in traffic), traffic
                print('OK: destinatario, texto y dos adjuntos exactos, lectura de respuestas en segundo plano '
                      'con pestaña aislada, conservación del borrador y rechazo del chat equivocado.')
            finally:
                await browser.close()


if __name__ == '__main__':
    asyncio.run(smoke())
