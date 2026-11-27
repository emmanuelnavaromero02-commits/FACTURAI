import {ACCEPTED_FILES, ACCEPTED_TYPES, prepareFiles, sendForm, sizeLabel} from './subida.js';
import {stageSignature, stageView, startStageClock, ticketStage, uploadStage} from './avance.js';
const $ = id => document.getElementById(id);

const statuses = {
  new: 'Recibido', review: 'Revisar datos', ready: 'Listo para tramitar', queued: 'En cola',
  running: 'Trabajando en el portal', issuing: 'Emisión en curso', needs_user: 'Necesita atención',
  blocked: 'Se detuvo', verified: 'Factura comprobada', issued_waiting_files: 'Buscando comprobantes',
  uncertain: 'Comprobando el resultado', duplicate: 'Ya registrado', attached: 'Imagen agregada',
  external: 'Esperando al comercio', whatsapp_preparing: 'Preparando solicitud'
};
export const eventLabels = {
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
  fuentes_de_campos: 'Identificó datos del ticket',
  memory_replay: 'Repitió una pantalla que ya conocía', memory_learned: 'Aprendió este portal',
  memory_disabled: 'Dejó de repetir una pantalla que falló', limit_reached: 'Se agotó lo previsto para este intento',
  question_waiting: 'Espera tu respuesta',
  mail_attempt: 'Pidió la factura por correo', mail_sent: 'Envió el correo al comercio',
  mail_uncertain: 'El correo pudo no llegar', whatsapp_needs_person: 'WhatsApp necesita tu ayuda'
};
// Measurements kept for the owner's dashboard and permission audits are not steps of the ticket.
export const hiddenEvents = new Set(['read_timing', 'session_metrics', 'memory_miss', 'permiso']);
const safeEventDetails = new Set(['browser_opened', 'browser_closed', 'notice_dismissed', 'portal_entry',
  'ticket_lookup_verified', 'campo_verificado', 'automatic_reobservation', 'captcha_reviewed',
  'recovery_checked', 'ticket_already_billed', 'chat_parts', 'question', 'question_waiting']);

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

const imageName = /\.(jpe?g|png|webp)$/i;

function messageKey(message, index) {
  return message.id || [message.at || message.created || '', message.role || '', index].join('|');
}

function renderMessage(message) {
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
        if (imageName.test(file.name || '')) {
          const preview = document.createElement('img');
          // Reduced, cacheable previews; the link keeps the original photo.
          preview.src = url.startsWith('/api/chat/files/') ? url + '?w=320' : url;
          if (url.startsWith('/api/chat/files/')) preview.srcset = url + '?w=320 1x, ' + url + '?w=640 2x';
          preview.alt = file.name || 'Foto del ticket';
          preview.loading = 'lazy';
          preview.decoding = 'async';
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
}

function renderEvent(event) {
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
}

function actionSignature(row, actions) {
  const {message, updated, ...stable} = row;
  return JSON.stringify([stable, actions.map(node => [node.tagName, node.textContent, node.className,
    node.getAttribute('href'), node.disabled, node.title])]);
}

export function installChat({api, getState, actionsFor}) {
  const thread = $('chat-thread');
  const text = $('chat-text');
  const input = $('chat-files');
  const camera = $('chat-camera-file');
  const attachments = $('chat-attachments');
  const status = $('chat-status');
  const send = $('chat-send');
  const bar = $('chat-upload');
  let files = [];
  let previews = [];
  let sending = false;
  let messages = [];
  const rendered = new Map();
  let lastSeq = 0;
  let messagesTag = '';
  let events = [];
  let eventsDrawn = 0;
  let lastEventId = 0;
  let eventsJob = '';
  let lastMessagesRead = 0;
  let lastEventsRead = 0;
  let messageReadInFlight = false;
  let eventReadInFlight = false;
  let pendingClientId = '';
  let upload = null;
  let stageKey = '';
  let actionsKey = '';
  startStageClock();

  function feedback(value, error = false) {
    status.textContent = value;
    status.classList.toggle('error', error);
  }

  function updateSend() {
    send.disabled = sending || !(text.value.trim() || files.length);
    $('chat-attach').disabled = sending;
    $('chat-camera').disabled = sending;
  }

  function renderAttachments() {
    for (const url of previews) URL.revokeObjectURL(url);
    previews = [];
    attachments.replaceChildren(...files.map((file, index) => {
      const item = document.createElement('div');
      item.className = 'chat-attachment';
      if (file.type.startsWith('image/') && !/hei[cf]/i.test(file.type)) {
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
    const incoming = [...candidates].filter(file => ACCEPTED_TYPES.has(file.type) || ACCEPTED_FILES.test(file.name));
    if (!incoming.length) return feedback('Adjunta fotos JPG, PNG, WEBP, HEIC o un PDF.', true);
    if (files.length + incoming.length > 4) return feedback('Envía hasta cuatro imágenes del mismo ticket por mensaje.', true);
    files.push(...incoming);
    pendingClientId = '';
    feedback(files.length > 1 ? 'Estas imágenes se leerán juntas como un solo ticket.' : 'Archivo listo para enviar.');
    renderAttachments();
    updateSend();
  }

  function welcome() {
    const box = document.createElement('div');
    box.className = 'chat-welcome';
    const heading = document.createElement('h2');
    heading.textContent = '¿Qué necesitas facturar?';
    const hint = document.createElement('p');
    hint.textContent = 'Escribe o manda fotos del ticket. Si está partido, adjunta todas las partes en el mismo mensaje.';
    box.append(heading, hint);
    return box;
  }

  // Add new messages and replace only the ones that changed, so previews are not downloaded again.
  function drawMessages() {
    if (!messages.length) {
      rendered.clear();
      if (!thread.querySelector('.chat-welcome') || thread.children.length !== 1) thread.replaceChildren(welcome());
      return;
    }
    thread.querySelector('.chat-welcome')?.remove();
    const wanted = new Set(messages.map(messageKey));
    for (const [key, item] of rendered) {
      if (!wanted.has(key)) { item.node.remove(); rendered.delete(key); }
    }
    let previous = null;
    messages.forEach((message, index) => {
      const key = messageKey(message, index);
      const signature = JSON.stringify(message);
      let item = rendered.get(key);
      if (!item || item.signature !== signature) {
        const node = renderMessage(message);
        if (item) item.node.replaceWith(node);
        item = {signature, node};
        rendered.set(key, item);
      }
      const expected = previous ? previous.nextSibling : thread.firstChild;
      if (item.node !== expected) thread.insertBefore(item.node, expected);
      previous = item.node;
    });
  }

  function mergeMessages(incoming, partial) {
    if (!partial) { messages = incoming; return; }
    const known = new Set(messages.map(message => message.id));
    messages = [...messages, ...incoming.filter(message => !message.id || !known.has(message.id))].slice(-200);
  }

  function drawEvents() {
    const list = $('chat-events');
    if (eventsDrawn > events.length || list.children.length !== eventsDrawn) {
      list.replaceChildren();
      eventsDrawn = 0;
    }
    if (eventsDrawn === events.length) return;
    list.append(...events.slice(eventsDrawn).map(renderEvent));
    eventsDrawn = events.length;
    list.scrollTop = list.scrollHeight;
  }

  function resetEvents(key) {
    eventsJob = key;
    events = [];
    eventsDrawn = 0;
    lastEventId = 0;
    lastEventsRead = 0;
    $('chat-events').replaceChildren();
  }

  function drawStage(stage) {
    const target = $('chat-stages');
    const key = stageSignature(stage);
    if (key === stageKey) return;
    stageKey = key;
    target.hidden = !stage;
    target.replaceChildren(...(stage ? [stageView(stage)] : []));
  }

  function drawActions(row) {
    const actions = actionsFor(row);
    const key = actionSignature(row, actions);
    if (key === actionsKey) return;
    actionsKey = key;
    $('chat-progress-actions').replaceChildren(...actions);
  }

  function drawProgress(state) {
    const progress = $('chat-progress');
    if (upload) {
      progress.hidden = false;
      $('chat-progress-title').textContent = upload.title;
      $('chat-progress-state').textContent = 'Subiendo';
      $('chat-progress-message').textContent = upload.detail;
      drawStage(uploadStage(upload.fraction, upload.started));
      actionsKey = '';
      $('chat-progress-actions').replaceChildren();
      return;
    }
    const row = currentJob(state, messages);
    progress.hidden = !row;
    if (!row) { if (eventsJob) resetEvents(''); drawStage(null); return; }
    if (eventsJob !== row.id) resetEvents(row.id);
    $('chat-progress-title').textContent = safeSummary(row.receipt?.merchant || 'Ticket');
    const reading = state.automation?.current_id === row.id && state.automation?.phase === 'reading';
    const pause = state.automation?.current_id === row.id ? state.automation?.phase : '';
    const stage = ticketStage(row, state);
    const readingDetail = stage?.index === 1 && !stage.waiting && stage.detail ? stage.detail : 'Identificando los datos del ticket…';
    $('chat-progress-state').textContent = reading ? 'Leyendo imágenes' : pause === 'waiting_profile' ? 'Faltan datos fiscales' :
      pause === 'waiting_plan' ? 'Falta conexión' : statuses[row.status] || row.status;
    $('chat-progress-message').textContent = reading ? readingDetail :
      pause === 'waiting_profile' ? 'Completa tu perfil fiscal para que pueda continuar.' :
      pause === 'waiting_plan' ? 'Revisa la conexión del servicio para continuar.' : safeSummary(row.message || stage?.detail || '');
    drawStage(stage);
    drawActions(row);
  }

  async function readJSON(path, tag) {
    const headers = {'x-app-token': getState().token || ''};
    if (tag) headers['if-none-match'] = tag;
    const response = await fetch(path, {headers});
    if (response.status === 304) return {unchanged: true};
    if (!response.ok) throw Error('No se pudo leer la conversación.');
    return {data: await response.json(), tag: response.headers.get('etag') || ''};
  }

  async function readMessages(force = false) {
    if (messageReadInFlight || !force && Date.now() - lastMessagesRead < 2200) return;
    messageReadInFlight = true;
    lastMessagesRead = Date.now();
    try {
      const result = await readJSON('/api/chat' + (lastSeq ? '?after=' + lastSeq : ''), lastSeq ? messagesTag : '');
      if (result.unchanged) return;
      const incoming = Array.isArray(result.data.messages) ? result.data.messages : [];
      // A reply without sequence numbers is a complete list.
      const partial = Boolean(lastSeq && result.data.partial);
      mergeMessages(incoming, partial);
      const highest = Math.max(0, ...messages.map(message => Number(message.seq) || 0), Number(result.data.last) || 0);
      lastSeq = highest;
      messagesTag = highest ? result.tag : '';
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
      const result = await api('/api/jobs/' + encodeURIComponent(key) + '/events?after=' + lastEventId);
      if (eventsJob !== key || !Array.isArray(result) || !result.length) return;
      if (result.every(event => Number.isInteger(event.id))) {
        const fresh = result.filter(event => event.id > lastEventId);
        events.push(...fresh.filter(event => !hiddenEvents.has(event.kind)));
        lastEventId = Math.max(lastEventId, ...fresh.map(event => event.id));
      } else if (JSON.stringify(result.filter(event => !hiddenEvents.has(event.kind))) !== JSON.stringify(events)) {
        // A list without ids is complete: draw it again from the start.
        events = result.filter(event => !hiddenEvents.has(event.kind));
        $('chat-events').replaceChildren();
        eventsDrawn = 0;
      }
      drawEvents();
    } catch { /* Status still comes from /api/state. */ }
    finally { eventReadInFlight = false; }
  }

  async function refresh(state, force = false) {
    drawMessages();
    drawProgress(state);
    await Promise.all([readMessages(force), readEvents(force)]);
  }

  function showUpload(fraction) {
    if (!upload) return;
    upload.fraction = fraction;
    const percent = Math.round(fraction * 100);
    upload.detail = (fraction >= 1 ? 'Recibido. Preparando la lectura…' : 'Enviando ' + upload.size + ' · ' + percent + '%');
    bar.hidden = false;
    bar.value = percent;
    feedback(fraction >= 1 ? 'Archivo recibido.' : 'Subiendo… ' + percent + '%');
    drawProgress(getState());
  }

  async function submit(event) {
    event.preventDefault();
    if (sending || !(text.value.trim() || files.length)) return;
    sending = true;
    updateSend();
    feedback(files.length ? 'Preparando las fotos…' : 'Enviando…');
    const draft = text.value;
    const sentFiles = [...files];
    if (!pendingClientId) pendingClientId = globalThis.crypto?.randomUUID?.() || String(Date.now()) + Math.random().toString(36).slice(2);
    try {
      const ready = await prepareFiles(sentFiles);
      const body = new FormData();
      body.set('text', draft.trim());
      body.set('client_id', pendingClientId);
      for (const file of ready) body.append('files', file);
      if (ready.length) {
        const bytes = ready.reduce((total, file) => total + file.size, 0);
        upload = {title: ready.length > 1 ? ready.length + ' fotos del ticket' : 'Ticket nuevo', started: Date.now(),
          size: sizeLabel(bytes), fraction: 0, detail: ''};
        showUpload(0);
        await sendForm('/api/chat', body, {token: getState().token, onProgress: showUpload});
      } else await api('/api/chat', 'POST', body);
      text.value = '';
      files = [];
      pendingClientId = '';
      renderAttachments();
      feedback('');
      upload = null;
      bar.hidden = true;
      await readMessages(true);
      await refresh(getState(), true);
    } catch (error) {
      feedback(error.message || 'No se pudo enviar. Puedes volver a intentarlo.', true);
    } finally {
      upload = null;
      bar.hidden = true;
      sending = false;
      stageKey = '';
      updateSend();
      drawProgress(getState());
      text.focus();
    }
  }

  $('chat-form').addEventListener('submit', submit);
  $('chat-attach').onclick = () => input.click();
  $('chat-camera').onclick = () => camera.click();
  input.onchange = event => { addFiles(event.target.files); input.value = ''; };
  camera.onchange = event => { addFiles(event.target.files); camera.value = ''; };
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
  return {refresh, busy: () => sending};
}
