export function installReceiptTools({api, el, button, link, perform, gmailConnected, openSettings, currentTicket}) {
  const $ = id => document.getElementById(id);
  let reading = 0;
  let taxFile = null;
  let downloadTicket = null;
  let messageId = null;
  let mailRequest = 0;

  function checked(container) {
    return [...$(container).querySelectorAll('input:checked')].map(node => node.value);
  }

  function choice(id, title) {
    const label = el('label');
    const input = el('input');
    input.type = 'checkbox';
    input.value = id;
    label.append(input, document.createTextNode(title));
    return label;
  }

  async function readPage(force = false) {
    const sequence = ++reading;
    const ticket = currentTicket();
    $('reader-text').textContent = '';
    $('reader-codes').replaceChildren();
    $('reader-status').textContent = 'Leyendo en el contenedor…';
    try {
      const values = {page: Number($('reader-page').value || 1), rotation: Number($('reader-rotation').value), force_ocr: force};
      let result;
      if (taxFile) {
        const data = new FormData();
        data.append('file', taxFile);
        data.append('mode', 'local');
        for (const [field, value] of Object.entries(values)) data.append(field, String(value));
        result = await api('/api/profile/read-csf', 'POST', data);
      } else {
        result = await api('/api/jobs/' + ticket.id + '/read-local', 'POST', values);
      }
      if (sequence !== reading) return;
      $('reader-page').replaceChildren(...Array.from({length: result.pages}, (_, n) => new Option(String(n + 1), n + 1)));
      $('reader-page').value = String(result.page);
      $('reader-ocr').disabled = $('reader-rotation').disabled = result.method === 'email';
      const source = {pdf: 'Texto del PDF', ocr: 'OCR local', email: 'Texto del correo', codes: 'Códigos impresos'}[result.method];
      $('reader-status').textContent = [source, 'Página ' + result.page + ' de ' + result.pages, result.notice].filter(Boolean).join(' · ');
      $('reader-text').textContent = result.text || 'No se encontró texto legible.';
      for (const code of result.codes) {
        const row = el('div', '', 'tool-entry');
        const add = button('Usar código', () => {
          const field = $('receipt-form').elements.billing_codes;
          const codes = field.value.split('\n').filter(Boolean);
          if (!codes.includes(code)) field.value = [...codes, code].join('\n');
          add.textContent = 'Agregado';
          add.disabled = true;
        });
        row.append(el('span', code), add);
        if (result.portal_links.includes(code)) {
          row.append(button('Cargar en Chromium', async () => {
            try {
              await api('/api/browser/open', 'POST', {url: code});
              $('reader-status').textContent = 'Enlace abierto en la sección Navegador.';
            } catch (error) {
              $('reader-status').textContent = error.message;
            }
          }));
        }
        $('reader-codes').append(row);
      }
    } catch (error) {
      if (sequence === reading) $('reader-status').textContent = error.message;
    }
  }

  function openReader(file = null) {
    taxFile = file;
    $('reader-title').textContent = file ? 'Lectura local de la constancia fiscal' : 'Lectura local del ticket';
    $('reader-page').replaceChildren(new Option('1', 1));
    $('reader-rotation').value = '0';
    $('reader-ocr').disabled = $('reader-rotation').disabled = false;
    $('reader-dialog').showModal();
    readPage();
  }
  $('local-reader').onclick = () => openReader();
  $('reader-dialog').addEventListener('close', () => {
    reading++;
    taxFile = null;
    $('reader-text').textContent = '';
    $('reader-codes').replaceChildren();
  });
  $('reader-page').onchange = () => readPage();
  $('reader-rotation').onchange = $('reader-ocr').onclick = () => readPage(true);

  async function showDownloads(ticket = null) {
    downloadTicket = ticket;
    const result = await api('/api/browser/downloads');
    const items = $('download-items');
    items.replaceChildren();
    $('check-files').hidden = !ticket || !result.files.length;
    $('check-files').disabled = true;
    $('downloads-status').textContent = result.message || (result.files.length
      ? (ticket ? 'Selecciona los archivos de este ticket.' : 'Archivos guardados al navegar por tu cuenta.')
      : 'Las descargas PDF, XML y ZIP de los portales aparecerán aquí.');
    for (const file of result.files) {
      const title = file.name + ' · ' + Math.max(1, Math.ceil(file.size / 1024)) + ' KB';
      const row = el('div', '', 'tool-entry');
      const save = link('Descargar', '/api/browser/downloads/' + encodeURIComponent(file.id));
      save.download = file.name;
      row.append(ticket ? choice(file.id, title) : el('span', title), save);
      items.append(row);
    }
    $('downloads-dialog').showModal();
  }
  $('browser-downloads').onclick = () => perform(() => showDownloads());
  $('download-items').onchange = () => {$('check-files').disabled = !checked('download-items').length;};
  $('check-files').onclick = async () => {
    const files = checked('download-items');
    if (!downloadTicket || !files.length) return;
    $('check-files').disabled = true;
    try {
      await api('/api/jobs/' + downloadTicket.id + '/recover-browser', 'POST', {files});
      $('downloads-dialog').close();
      await perform(async () => 'PDF y XML cotejados con el ticket.');
    } catch (error) {
      $('downloads-status').textContent = error.message;
    } finally {
      $('check-files').disabled = !checked('download-items').length;
    }
  };

  function clearParts() {
    messageId = null;
    $('mail-parts').replaceChildren();
    $('mail-import').hidden = true;
    $('mail-import').disabled = true;
  }
  $('gmail-inbox').onclick = () => {
    if (!gmailConnected()) return openSettings();
    $('mail-messages').replaceChildren();
    clearParts();
    $('mail-status').textContent = '';
    $('mail-inbox-dialog').showModal();
    $('mail-query').focus();
  };
  $('mail-inbox-dialog').addEventListener('close', () => {mailRequest++;});
  async function selectMessage(message) {
    const sequence = ++mailRequest;
    clearParts();
    $('mail-status').textContent = 'Buscando texto y adjuntos…';
    try {
      const parts = await api('/api/gmail/receipts/' + encodeURIComponent(message.id) + '/parts', 'POST');
      if (sequence !== mailRequest) return;
      messageId = message.id;
      $('mail-status').textContent = parts.length ? 'Selecciona lo que quieres importar de ' + (message.subject || 'este correo') + '.' :
        'El correo no contiene texto ni adjuntos compatibles.';
      $('mail-parts').replaceChildren(...parts.map(part => choice(part.id, part.name)));
      $('mail-import').hidden = !parts.length;
    } catch (error) {
      if (sequence === mailRequest) $('mail-status').textContent = error.message;
    }
  }
  $('mail-search-form').onsubmit = async event => {
    event.preventDefault();
    const sequence = ++mailRequest;
    clearParts();
    $('mail-messages').replaceChildren();
    $('mail-status').textContent = 'Buscando en Gmail…';
    try {
      const messages = await api('/api/gmail/receipts/search', 'POST', {query: $('mail-query').value});
      if (sequence !== mailRequest) return;
      $('mail-status').textContent = messages.length ? 'Elige un mensaje.' : 'Sin coincidencias.';
      for (const message of messages) {
        const item = el('div', '', 'tool-entry');
        item.append(el('span', [message.subject, message.sender, message.date].filter(Boolean).join(' · ')),
                    button('Ver contenido', () => selectMessage(message)));
        $('mail-messages').append(item);
      }
    } catch (error) {
      if (sequence === mailRequest) $('mail-status').textContent = error.message;
    }
  };
  $('mail-parts').onchange = () => {$('mail-import').disabled = !checked('mail-parts').length;};
  $('mail-import').onclick = async () => {
    const selected = checked('mail-parts');
    if (!messageId || !selected.length) return;
    const sequence = ++mailRequest;
    $('mail-import').disabled = true;
    try {
      const files = await api('/api/gmail/receipts/' + encodeURIComponent(messageId) + '/import', 'POST', {selected});
      if (sequence !== mailRequest) return;
      $('mail-inbox-dialog').close();
      const count = files.filter(file => file.created).length;
      await perform(async () => 'Importados ' + count + '. Ya registrados ' + (files.length - count) + '.');
    } catch (error) {
      if (sequence === mailRequest) $('mail-status').textContent = error.message;
    } finally {
      if (sequence === mailRequest) $('mail-import').disabled = !checked('mail-parts').length;
    }
  };
  return {showDownloads, openReader};
}
