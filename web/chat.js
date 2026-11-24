const $ = id => document.getElementById(id);

const statuses = {
  new: 'Recibido', review: 'Revisar datos', ready: 'Listo para tramitar', queued: 'En cola',
  running: 'Trabajando en el portal', issuing: 'Emisión en curso', needs_user: 'Necesita atención',
  blocked: 'Se detuvo', verified: 'Factura comprobada', issued_waiting_files: 'Buscando comprobantes',
  uncertain: 'Comprobando el resultado', duplicate: 'Ya registrado', attached: 'Imagen agregada',
  external: 'Esperando al comercio', whatsapp_preparing: 'Preparando solicitud'
};
const eventLabels = {
  browser_opened: 'Abrió el portal', browser_reopened: 'Reabrió el portal', browser_closed: 'Cerró el navegador',
  notice_dismissed: 'Leyó un aviso del portal', portal_entry: 'Abrió el formulario',
  ticket_lookup_verified: 'Cotejó los datos del ticket', ticket_lookup_rejected: 'El portal rechazó un dato',
  campo_verificado: 'Cotejó un campo', action: 'Avanzó en el portal',
  automatic_reobservation: 'Volvió a leer el formulario', navigation_retry: 'Buscó otra ruta del portal',
  navigation_blocked: 'El portal cambió de dirección', captcha_reviewed: 'Continuó tras la verificación',
  download: 'Recibió un archivo', download_error: 'No pudo recibir un archivo',
  recovery_checked: 'Buscó comprobantes', recovery_incomplete: 'La búsqueda quedó pendiente',
  stopped: 'Trámite detenido', internal_error: 'Hubo un error interno',
  ticket_already_billed: 'El ticket ya tenía factura', portal_account: 'Revisó el acceso al portal',
  brand_entry: 'Abrió el formulario del comercio', gmail_recovery: 'Buscó archivos en el correo',
  chat_parts: 'Unió las fotos del ticket', payment_confirmed: 'Confirmó la forma de pago',
  fuentes_de_campos: 'Identificó datos del ticket'
};
const safeEventDetails = new Set(['browser_opened', 'browser_closed', 'notice_dismissed', 'portal_entry',
  'ticket_lookup_verified', 'campo_verificado', 'automatic_reobservation', 'captcha_reviewed',
  'recovery_checked', 'ticket_already_billed', 'chat_parts']);

function safeSummary(value) {
  return String(value || '')
    .replace(/[A-ZÑ&]{3,4}\d{6}[A-Z0-9]{3}/gi, 'RFC protegido')
    .replace(/[\w.+-]+@[\w.-]+\.[a-z]{2,}/gi, 'correo protegido')
    .replace(/\b\d{12,}\b/g, 'folio protegido')
    .replace(/ChatGPT|OpenAI|\bgpt-[\w.]+\b/gi, 'el servicio');
}

function localTime(value) {
  if (!value) return '';
  const date = new Date(value);
  return Number.isNaN(date.getTime()) ? '' : date.toLocaleTimeString('es-MX', {hour: '2-digit', minute: '2-digit'});
}

function currentJob(state, messages) {
  const jobs = state.jobs || [];
  const active = jobs.find(row => row.id === state.active_job);
  if (active) return active;
  const mentioned = [...messages].reverse().flatMap(message => message.job_ids || []);
  for (const id of mentioned) {
    const found = jobs.find(row => row.id === id && !row.companion_of);
    if (found) return found;
  }
  const automation = jobs.find(row => row.id === state.automation?.current_id && !row.companion_of);
  return automation || jobs.find(row => !row.companion_of) || null;
}

export function installChat({api, getState, actionsFor}) {
  const thread = $('chat-thread');
  const text = $('chat-text');
  const input = $('chat-files');
  const attachments = $('chat-attachments');
  const status = $('chat-status');
  const send = $('chat-send');
  let files = [];
  let previews = [];
  let sending = false;
  let messages = [];
  let messageSignature = '';
  let eventSignature = '';
  let eventsJob = '';
  let lastMessagesRead = 0;
  let lastEventsRead = 0;
  let messageReadInFlight = false;
  let eventReadInFlight = false;
  let pendingClientId = '';

  function feedback(value, error = false) {
    status.textContent = value;
    status.classList.toggle('error', error);
  }

  function updateSend() {
    send.disabled = sending || !(text.value.trim() || files.length);
    $('chat-attach').disabled = sending;
  }

  function renderAttachments() {
    for (const url of previews) URL.revokeObjectURL(url);
    previews = [];
    attachments.replaceChildren(...files.map((file, index) => {
      const item = document.createElement('div');
      item.className = 'chat-attachment';
      if (file.type.startsWith('image/')) {
        const thumb = document.createElement('img');
        thumb.className = 'chat-attachment-thumb';
        thumb.alt = '';
        thumb.src = URL.createObjectURL(file);
        previews.push(thumb.src);
        item.append(thumb);
      }
      const name = document.createElement('span');
      name.textContent = file.name;
      const remove = document.createElement('button');
      remove.type = 'button';
      remove.textContent = '×';
      remove.setAttribute('aria-label', 'Quitar ' + file.name);
      remove.onclick = () => { files.splice(index, 1); pendingClientId = ''; renderAttachments(); updateSend(); };
      item.append(name, remove);
      return item;
    }));
  }

  function addFiles(candidates) {
    const allowed = new Set(['image/jpeg', 'image/png', 'image/webp', 'application/pdf']);
    const incoming = [...candidates].filter(file => allowed.has(file.type) || /\.(jpe?g|png|webp|pdf)$/i.test(file.name));
    if (!incoming.length) return feedback('Adjunta fotos JPG, PNG, WEBP o un PDF.', true);
    if (files.length + incoming.length > 4) return feedback('Envía hasta cuatro imágenes del mismo ticket por mensaje.', true);
    files.push(...incoming);
    pendingClientId = '';
    feedback(files.length > 1 ? 'Estas imágenes se leerán juntas como un solo ticket.' : 'Archivo listo para enviar.');
    renderAttachments();
    updateSend();
  }

  function drawMessages() {
    const signature = JSON.stringify(messages);
    if (signature === messageSignature) return;
    messageSignature = signature;
    if (!messages.length) {
      const welcome = document.createElement('div');
      welcome.className = 'chat-welcome';
      const heading = document.createElement('h2');
      heading.textContent = '¿Qué necesitas facturar?';
      const hint = document.createElement('p');
      hint.textContent = 'Escribe o manda fotos del ticket. Si está partido, adjunta todas las partes en el mismo mensaje.';
      welcome.append(heading, hint);
      thread.replaceChildren(welcome);
      return;
    }
    const items = messages.map(message => {
      const card = document.createElement('article');
      card.className = 'chat-message';
      card.dataset.role = message.role === 'user' ? 'user' : 'assistant';
      if (message.text) {
        const paragraph = document.createElement('p');
        paragraph.textContent = message.text;
        card.append(paragraph);
      }
      if (message.files?.length) {
        const list = document.createElement('div');
        list.className = 'chat-message-files';
        for (const file of message.files) {
          const name = document.createElement('a');
          const url = typeof file.url === 'string' ? file.url : '';
          if (/^\/api\/(?:chat\/files|jobs)\/[a-zA-Z0-9/_-]+$/.test(url)) {
            name.href = url;
            name.target = '_blank';
            name.rel = 'noopener noreferrer';
            if (/\.(jpe?g|png|webp)$/i.test(file.name || '')) {
              const preview = document.createElement('img');
              preview.src = url;
              preview.alt = file.name || 'Foto del ticket';
              preview.loading = 'lazy';
              name.append(preview);
            }
          } else name.removeAttribute('href');
          const caption = document.createElement('span');
          caption.textContent = file.name || 'Archivo';
          name.append(caption);
          list.append(name);
        }
        card.append(list);
      }
      const timestamp = localTime(message.at || message.created);
      if (timestamp) {
        const time = document.createElement('small');
        time.textContent = timestamp;
        card.append(time);
      }
      return card;
    });
    thread.replaceChildren(...items);
  }

  function drawEvents(events) {
    const signature = JSON.stringify([eventsJob, events]);
    if (signature === eventSignature) return;
    eventSignature = signature;
    const list = $('chat-events');
    list.replaceChildren(...events.map(event => {
      const item = document.createElement('div');
      item.className = 'chat-event';
      const body = document.createElement('div');
      const label = document.createElement('strong');
      label.textContent = eventLabels[event.kind] || 'Avance del trámite';
      if (safeEventDetails.has(event.kind) && event.detail) {
        const detail = document.createElement('p');
        detail.textContent = safeSummary(event.detail).slice(0, 180);
        body.append(label, detail);
      } else body.append(label);
      const time = document.createElement('p');
      time.textContent = localTime(event.at);
      body.append(time);
      item.append(body);
      return item;
    }));
    list.scrollTop = list.scrollHeight;
  }

  function drawProgress(state) {
    const row = currentJob(state, messages);
    const progress = $('chat-progress');
    progress.hidden = !row;
    if (!row) { eventsJob = ''; drawEvents([]); return; }
    if (eventsJob !== row.id) {
      eventsJob = row.id;
      lastEventsRead = 0;
      eventSignature = '';
      $('chat-events').replaceChildren();
    }
    $('chat-progress-title').textContent = safeSummary(row.receipt?.merchant || 'Ticket');
    const reading = state.automation?.current_id === row.id && state.automation?.phase === 'reading';
    const pause = state.automation?.current_id === row.id ? state.automation?.phase : '';
    $('chat-progress-state').textContent = reading ? 'Leyendo imágenes' : pause === 'waiting_profile' ? 'Faltan datos fiscales' :
      pause === 'waiting_plan' ? 'Falta conexión' : statuses[row.status] || row.status;
    $('chat-progress-message').textContent = reading ? 'Identificando los datos del ticket…' :
      pause === 'waiting_profile' ? 'Completa tu perfil fiscal para que pueda continuar.' :
      pause === 'waiting_plan' ? 'Revisa la conexión del servicio para continuar.' : safeSummary(row.message || '');
    $('chat-progress-actions').replaceChildren(...actionsFor(row));
  }

  async function readMessages(force = false) {
    if (messageReadInFlight || !force && Date.now() - lastMessagesRead < 2200) return;
    messageReadInFlight = true;
    lastMessagesRead = Date.now();
    try {
      const result = await api('/api/chat');
      messages = Array.isArray(result.messages) ? result.messages : [];
      drawMessages();
      drawProgress(getState());
    } catch { /* Keep the conversation visible during a transient interruption. */ }
    finally { messageReadInFlight = false; }
  }

  async function readEvents(force = false) {
    const key = eventsJob;
    if (!key || eventReadInFlight || !force && Date.now() - lastEventsRead < 2200) return;
    eventReadInFlight = true;
    lastEventsRead = Date.now();
    try {
      const result = await api('/api/jobs/' + encodeURIComponent(key) + '/events');
      if (eventsJob === key && Array.isArray(result)) drawEvents(result);
    } catch { /* Status still comes from /api/state. */ }
    finally { eventReadInFlight = false; }
  }

  async function refresh(state, force = false) {
    drawMessages();
    drawProgress(state);
    await Promise.all([readMessages(force), readEvents(force)]);
  }

  async function submit(event) {
    event.preventDefault();
    if (sending || !(text.value.trim() || files.length)) return;
    sending = true;
    updateSend();
    feedback('Enviando…');
    const draft = text.value;
    const sentFiles = [...files];
    if (!pendingClientId) pendingClientId = globalThis.crypto?.randomUUID?.() || String(Date.now()) + Math.random().toString(36).slice(2);
    const body = new FormData();
    body.set('text', draft.trim());
    body.set('client_id', pendingClientId);
    for (const file of sentFiles) body.append('files', file);
    try {
      await api('/api/chat', 'POST', body);
      text.value = '';
      files = [];
      pendingClientId = '';
      renderAttachments();
      feedback('');
      await readMessages(true);
      await refresh(getState(), true);
    } catch (error) {
      feedback(error.message || 'No se pudo enviar. Puedes volver a intentarlo.', true);
    } finally {
      sending = false;
      updateSend();
      text.focus();
    }
  }

  $('chat-form').addEventListener('submit', submit);
  $('chat-attach').onclick = () => input.click();
  input.onchange = event => { addFiles(event.target.files); input.value = ''; };
  text.addEventListener('input', () => { pendingClientId = ''; updateSend(); text.style.height = 'auto'; text.style.height = Math.min(text.scrollHeight, 160) + 'px'; });
  text.addEventListener('keydown', event => {
    if (event.key === 'Enter' && !event.shiftKey && !event.isComposing) {
      event.preventDefault();
      $('chat-form').requestSubmit();
    }
  });
  $('screen-chat').addEventListener('paste', event => {
    const copied = [...(event.clipboardData?.files || [])].filter(file => file.type.startsWith('image/'));
    if (copied.length) { event.preventDefault(); addFiles(copied); }
  });
  for (const name of ['dragenter', 'dragover']) $('screen-chat').addEventListener(name, event => {
    if (!event.dataTransfer?.types?.includes('Files')) return;
    event.preventDefault();
    $('screen-chat').classList.add('chat-dragging');
  });
  $('screen-chat').addEventListener('dragleave', event => {
    if (!event.currentTarget.contains(event.relatedTarget)) $('screen-chat').classList.remove('chat-dragging');
  });
  $('screen-chat').addEventListener('drop', event => {
    event.preventDefault();
    $('screen-chat').classList.remove('chat-dragging');
    addFiles(event.dataTransfer.files);
  });
  updateSend();
  drawMessages();
  return {refresh};
}
