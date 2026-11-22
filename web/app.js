import {installViewer} from './visor.js';
import {installReceiptTools} from './herramientas.js';
import {installSAT, installWhatsApp} from './seguimiento.js';

const $ = id => document.getElementById(id);
const sections = {bandeja: 'Bandeja', navegador: 'Navegador', comprobantes: 'Comprobantes', portales: 'Portales', ajustes: 'Ajustes'};
const jobLabels = {new: 'Por leer', review: 'Por revisar', external: 'Solicitar al comercio', ready: 'Listo', queued: 'En cola', running: 'En el portal',
  issuing: 'Emisión en trámite', needs_user: 'Requiere tu intervención', issued_waiting_files: 'Emitido · recuperar',
  verified: 'Comprobado', blocked: 'Pendiente', uncertain: 'Resultado incierto', duplicate: 'Duplicado', historical: 'Histórico',
  whatsapp_preparing: 'Preparando WhatsApp'};
const planLabels = {disconnected: 'Por conectar', ready: 'Plan conectado', limit: 'Cupo en pausa',
  identity_only: 'Autoriza el uso del plan', reconnect: 'Vuelve a conectar'};
const payments = {'01': 'Efectivo', '03': 'Transferencia', '04': 'Crédito', '28': 'Débito', '99': 'Por definir'};
const working = new Set(['queued', 'running', 'needs_user', 'issuing']);
const review = new Set(['new', 'review', 'ready', 'blocked', 'external']);
const documents = new Set(['verified', 'issued_waiting_files', 'uncertain', 'historical']);
const terminal = new Set(['verified', 'duplicate', 'historical']);
const amountFormat = new Intl.NumberFormat('es-MX', {style: 'currency', currency: 'MXN'});
const currency = value => value && Number.isFinite(Number(value)) ? amountFormat.format(Number(value)) : '—';
let state = {token: '', jobs: [], catalog: {}, plan: {}, browser: {}, gmail: 'disconnected'};
let activeScreen = 'bandeja';
let filter = 'all';
let busy = false;
let editing = null;
let prepared = [];
let attachJob = null;
let gmailJob = null;
let profileDirty = false;
let rowSignature = '';
let taskSignature = '';

function el(tag, content = '', ...classes) {
  const value = document.createElement(tag);
  value.textContent = content;
  if (classes.length) value.className = classes.join(' ');
  return value;
}

function notify(message, failed = false) {
  $('notice').textContent = message;
  $('notice').hidden = !message;
  $('notice').classList.toggle('error', failed);
}

async function api(path, method = 'GET', payload) {
  const headers = {'x-app-token': state.token};
  const options = {method, headers};
  if (payload instanceof FormData) options.body = payload;
  else if (payload !== undefined) {
    headers['content-type'] = 'application/json';
    options.body = JSON.stringify(payload);
  }
  const response = await fetch(path, options);
  if (!response.ok) {
    if ((response.headers.get('content-type') || '').includes('application/json')) {
      const data = await response.json();
      throw Error(typeof data.detail === 'string' ? data.detail : 'Revisa los campos capturados.');
    }
    throw Error((await response.text()).slice(0, 450) || 'No se completó la solicitud.');
  }
  if (response.headers.get('content-type') === 'image/png') return response.blob();
  return response.json();
}

async function perform(fn) {
  if (busy) return;
  busy = true;
  try {
    notify('');
    const message = await fn();
    if (typeof message === 'string') notify(message);
    await refresh();
  } catch (error) {
    notify(error.message, true);
    await refresh().catch(() => {});
  } finally {
    busy = false;
  }
}

function button(label, handler, variant = 'secondary') {
  const node = el('button', label, variant);
  node.type = 'button';
  node.onclick = handler;
  return node;
}

function link(label, href, variant = 'inline-link') {
  const node = el('a', label, variant);
  node.href = href;
  return node;
}

function route(screen) {
  location.hash = screen;
  openScreen();
}

function openScreen() {
  const current = location.hash.slice(1);
  activeScreen = Object.hasOwn(sections, current) ? current : 'bandeja';
  for (const page of Object.keys(sections)) $('screen-' + page).hidden = page !== activeScreen;
  for (const anchor of document.querySelectorAll('[data-nav]')) {
    if (anchor.dataset.nav === activeScreen) anchor.setAttribute('aria-current', 'page');
    else anchor.removeAttribute('aria-current');
  }
  $('breadcrumb').textContent = sections[activeScreen];
  if (activeScreen === 'portales') renderPortals();
}

const viewer = installViewer({api, token: () => state.token, showError: error => notify(error, true), perform});
const receiptTools = installReceiptTools({api, el, button, link, perform, currentTicket: () => editing,
  gmailConnected: () => state.gmail === 'connected', openSettings: () => {
    route('ajustes');
    notify('Conecta Gmail para elegir el correo que contiene el ticket.');
  }});
const showSAT = installSAT({api, currency});
const showWhatsApp = installWhatsApp({api, currency, profile: () => state.profile,
  showBrowser: () => route('navegador'), showSettings: () => route('ajustes')});

function blank(title, description, mark = '▤') {
  const box = el('div', '', 'empty');
  box.append(el('span', mark, 'empty-symbol'), el('h3', title), el('p', description));
  return box;
}

function edit(row) {
  editing = row;
  const form = $('receipt-form');
  const {provider} = form.elements;
  provider.replaceChildren(new Option('Selecciona un comercio', ''));
  for (const [id, value] of Object.entries(state.catalog)) provider.add(new Option(value.name, id));
  provider.add(new Option('Otro comercio · solicitar directamente', 'solicitud_directa'));
  for (const [key, value] of Object.entries(row.receipt)) {
    if (form.elements.namedItem(key)) form.elements[key].value = key === 'billing_codes' ? value.join('\n') : value;
  }
  if (!state.catalog[row.receipt.provider] && row.receipt.provider !== 'solicitud_directa') provider.value = '';
  $('view-source').href = `/api/jobs/${row.id}/source`;
  $('editor').showModal();
}

function prepare(rows) {
  if (!state.profile) {
    route('ajustes');
    return notify('Guarda tus datos fiscales antes de iniciar el trámite.');
  }
  prepared = rows.filter(row => (row.status === 'ready' || row.can_retry) && !row.receipt.source.startsWith('Ejemplo:'));
  if (!prepared.length) return notify('No hay tickets reales revisados para preparar.');
  const names = [...new Set(prepared.map(row => state.catalog[row.receipt.provider]?.name))];
  $('prepare-summary').textContent = `${prepared.length} ${prepared.length === 1 ? 'ticket' : 'tickets'} en ${names.join(', ')}.`;
  $('prepare-form').reset();
  $('run-mode').querySelector('[value=agent]').disabled = state.plan.status !== 'ready';
  $('run-mode').value = state.plan.status === 'ready' ? 'agent' : 'manual';
  $('run-mode').onchange();
  $('prepare-dialog').showModal();
}

function request(row) {
  const ticket = row.receipt;
  $('request-text').value = `Buen día. Solicito el CFDI de mi compra del ${ticket.date || '[fecha]'} por ${currency(ticket.amount)} MXN${ticket.ticket_number ? `, folio ${ticket.ticket_number}` : ''}.\n\nAdjunto el ticket y mis datos fiscales. Agradeceré el envío del PDF y XML a ${state.profile?.email || '[correo]'}.\n\nGracias.`;
  $('request-dialog').showModal();
}

async function logs(row) {
  const events = await api(`/api/jobs/${row.id}/events`);
  $('logs-label').textContent = row.receipt.merchant || 'Ticket por identificar';
  const values = events.toReversed().map(event => {
    const item = el('div', '', 'log-item');
    item.append(el('strong', event.kind.replaceAll('_', ' ')), el('p', event.detail),
      el('small', new Date(event.at).toLocaleString('es-MX')));
    return item;
  });
  $('logs').replaceChildren(...(values.length ? values : [el('p', 'Aún no hay actividad.', 'muted')]));
  $('logs-dialog').showModal();
}

function recoverGmail(row) {
  if (state.gmail !== 'connected') {
    route('ajustes');
    return notify('Conecta Gmail de solo lectura para recuperar los adjuntos.');
  }
  gmailJob = row;
  $('gmail-job').textContent = row.receipt.merchant || 'Ticket';
  $('gmail-form').elements.query.value = row.uuid ? `"${row.uuid}" has:attachment` :
    `newer_than:14d has:attachment${row.receipt.ticket_number ? ` "${row.receipt.ticket_number.replaceAll('"', '')}"` : ''}`;
  $('gmail-dialog').showModal();
}

function readTicket(row) {
  const readLocal = () => {
    edit(row);
    $('local-reader').click();
  };
  if (state.plan.status !== 'ready') {
    readLocal();
    return;
  }
  if (window.confirm('¿Leer el original con tu plan de ChatGPT?')) {
    perform(async () => {
      let result;
      try {
        result = await api(`/api/jobs/${row.id}/analyze`, 'POST', {consent: true});
      } catch (error) {
        readLocal();
        return error.message + ' Se abrió el lector local.';
      }
      if (result.status === 'review') edit(result);
      return result.message;
    });
  }
}

function rowActions(row) {
  const group = el('div', '', 'row-actions');
  const realTicket = !row.historical && !row.receipt.source.startsWith('Ejemplo:');
  const whatsappPrimary = realTicket && ['external', 'whatsapp_preparing'].includes(row.status);
  const primary = row.status === 'new' || row.status === 'review' ? button('Revisar', () => edit(row)) :
    whatsappPrimary ? button(row.issue_attempted || row.status === 'whatsapp_preparing' ? 'Ver solicitud WhatsApp' : 'Solicitar por WhatsApp',
      () => showWhatsApp(row), 'primary') :
    row.status === 'external' ? button('Preparar solicitud', () => request(row), 'primary') :
    row.can_retry ? button('Reintentar', () => prepare([row]), 'primary') :
    row.status === 'ready' && !row.receipt.source.startsWith('Ejemplo:') ? button('Preparar', () => prepare([row]), 'primary') :
    ['running', 'needs_user', 'issuing'].includes(row.status) ? button('Ver trámite', () => route('navegador')) : null;
  if (primary) group.append(primary);
  if (row.status === 'verified' && row.has_pdf && row.has_xml) group.append(button('Vigencia SAT', () => showSAT(row)));
  for (const format of ['pdf', 'xml']) {
    if (!row[`has_${format}`]) continue;
    const file = link(format.toUpperCase(), `/api/jobs/${row.id}/files/${format}`, 'secondary button-link');
    file.download = '';
    group.append(file);
  }
  const menu = el('details');
  const toggle = el('summary', '⋯');
  toggle.setAttribute('aria-label', 'Más acciones para ' + (row.receipt.merchant || 'el ticket'));
  const panel = el('div', '', 'more-panel');
  if (review.has(row.status) && !row.issue_attempted) {
    panel.append(button(state.plan.status === 'ready' ? 'Leer con ChatGPT' : 'Leer en el contenedor', () => readTicket(row), 'plain'));
    if (row.status !== 'review' && row.status !== 'new') panel.append(button('Editar datos', () => edit(row), 'plain'));
  }
  if (!row.historical) {
    const original = link('Ver original ↗', `/api/jobs/${row.id}/source`, 'plain button-link');
    original.target = '_blank';
    original.rel = 'noopener';
    panel.append(original);
    if (!terminal.has(row.status) && !working.has(row.status)) {
      panel.append(button('Usar descargas del navegador', () => perform(() => receiptTools.showDownloads(row)), 'plain'));
    }
  }
  if (!row.historical && !working.has(row.status) && row.status !== 'duplicate') {
    panel.append(button('Recuperar de Gmail', () => recoverGmail(row), 'plain'),
      button('Adjuntar PDF/XML, ZIP o correo', () => {attachJob = row.id; $('cfdi-file').click();}, 'plain'),
      button('Revisar descargas', () => perform(async () => {
        await api(`/api/jobs/${row.id}/recover-local`, 'POST');
        return 'Comprobantes cotejados.';
      }), 'plain'));
  }
  if (working.has(row.status)) panel.append(button('Detener', () => perform(() => api(`/api/jobs/${row.id}/stop`, 'POST')), 'plain'));
  if (realTicket && !whatsappPrimary && (row.status === 'ready' || row.issue_attempted) && !working.has(row.status)) {
    panel.append(button(row.issue_attempted ? 'Ver solicitud WhatsApp' : 'Solicitar por WhatsApp', () => showWhatsApp(row), 'plain'));
  }
  if (!row.issue_attempted && !terminal.has(row.status) && !working.has(row.status) && row.status !== 'whatsapp_preparing') {
    panel.append(button('Copiar solicitud al comercio', () => request(row), 'plain'));
  }
  panel.append(button('Ver actividad', () => perform(() => logs(row)), 'plain'));
  menu.append(toggle, panel);
  group.append(menu);
  return group;
}

function table(container, rows, isDocs = false) {
  if (!rows.length) {
    container.replaceChildren(blank(isDocs ? 'Aún no hay comprobantes' : 'Aquí llegan tus tickets',
      isDocs ? 'Los CFDI y las emisiones pendientes aparecerán cuando avances en un trámite.' :
        state.jobs.length ? 'Prueba otra búsqueda o cambia el filtro.' : 'Sube tu primer recibo o carga un ejemplo para practicar.'));
    return;
  }
  const wrap = el('div', '', 'table-wrap');
  const content = el('table');
  const headers = el('tr');
  for (const name of ['Comercio', 'Importe', 'Estado', 'Acciones']) headers.append(el('th', name));
  const head = el('thead');
  head.append(headers);
  const body = el('tbody');
  for (const row of rows) {
    const receipt = row.receipt;
    const title = receipt.merchant || 'Ticket por identificar';
    const info = el('td');
    info.append(el('strong', title, 'ticket-name'), el('span', [receipt.date, receipt.ticket_number || receipt.billing_codes[0],
      row.uuid ? `UUID ${row.uuid}` : null].filter(Boolean).join(' · ') || receipt.source, 'ticket-meta'));
    if (row.message) info.append(el('p', row.message, 'ticket-note'));
    const stateCell = el('td');
    const statusLabel = row.status === 'external' && row.issue_attempted ? 'Solicitud en seguimiento' : jobLabels[row.status];
    stateCell.append(el('span', statusLabel || row.status, 'job-status', row.status));
    const actions = el('td');
    actions.append(rowActions(row));
    const tr = el('tr');
    tr.append(info, el('td', currency(receipt.amount), 'ticket-amount'), stateCell, actions);
    body.append(tr);
  }
  content.append(head, body);
  wrap.append(content);
  container.replaceChildren(wrap);
}

function renderTickets(force = false) {
  const query = $('ticket-search').value.trim().toLowerCase();
  const signature = JSON.stringify([filter, query, state.jobs]);
  if (!force && signature === rowSignature) return;
  rowSignature = signature;
  const rows = state.jobs.filter(row => (filter === 'all' || filter === 'review' && review.has(row.status) && !row.issue_attempted ||
    filter === 'working' && (working.has(row.status) || row.status === 'whatsapp_preparing' || row.status === 'external' && row.issue_attempted))
    && [row.receipt.merchant, row.receipt.ticket_number,
      row.receipt.amount, row.receipt.source, ...row.receipt.billing_codes].join(' ').toLowerCase().includes(query));
  table($('tickets'), rows);
  table($('documents'), state.jobs.filter(row => documents.has(row.status) || row.has_pdf || row.has_xml ||
    row.status === 'external' && row.issue_attempted), true);
  $('count-total').textContent = state.jobs.length;
  $('count-pending').textContent = state.jobs.filter(row => !terminal.has(row.status)).length;
  $('count-verified').textContent = state.jobs.filter(row => row.status === 'verified').length;
  $('batch').disabled = !state.jobs.some(row => row.status === 'ready' && !row.receipt.source.startsWith('Ejemplo:'));
}

function renderBrowser() {
  viewer.update(state.browser, Boolean(state.active_job));
  $('browser-open').disabled = $('wa-open').disabled = Boolean(state.active_job);
  $('browser-open').textContent = state.browser.open ? 'Ver comercios' : 'Abrir navegador';
  const row = state.jobs.find(job => job.id === state.active_job);
  const signature = JSON.stringify([row, state.profile?.rfc, state.plan.status]);
  if (signature === taskSignature) return;
  taskSignature = signature;
  const target = $('browser-task');
  if (!row) {
    target.replaceChildren(el('p', 'Los datos del ticket y las intervenciones aparecerán aquí.', 'muted'));
    return;
  }
  const detail = el('dl');
  for (const [label, value] of [['Comercio', row.receipt.merchant], ['Total', currency(row.receipt.amount)],
    ['Fecha', row.receipt.date], ['Folio', row.receipt.ticket_number || row.receipt.billing_codes.join(' · ')],
    ['Forma de pago', payments[row.receipt.payment_code]], ['RFC receptor', state.profile?.rfc]]) {
    if (!value) continue;
    const line = el('div');
    line.append(el('dt', label), el('dd', value));
    detail.append(line);
  }
  target.replaceChildren(el('span', jobLabels[row.status], 'job-status', row.status), detail, el('p', row.message, 'muted'));
  const actions = el('div', '', 'buttons');
  if (row.human_kind) {
    const issue = row.human_kind === 'issue';
    const label = issue ? 'Aprobar emisión' : row.human_kind === 'operator' ? 'Terminar y cotejar' : 'Continuar';
    const resume = button(label, () => perform(() =>
      api(`/api/jobs/${row.id}/decision`, 'POST', {decision: issue ? 'approve' : 'resume'})), 'primary');
    actions.append(resume);
    if (['plan', 'blocked', 'unknown'].includes(row.human_kind)) {
      actions.append(button('Continuar a mano', () => perform(async () => {
        await api(`/api/jobs/${row.id}/decision`, 'POST', {decision: 'manual'});
        return 'El expediente registrará la operación manual y cotejará las descargas.';
      })));
    }
    if (row.human_kind === 'plan') actions.append(link('Revisar conexión', '#ajustes'));
  }
  actions.append(button('Detener', () => perform(() => api(`/api/jobs/${row.id}/stop`, 'POST'))));
  target.append(actions);
}

function renderSettings() {
  $('read-csf').disabled = state.plan.status !== 'ready';
  $('restart-browser').disabled = Boolean(state.active_job);
  if (state.profile && !profileDirty && !document.activeElement.closest('#profile')) {
    for (const [key, value] of Object.entries(state.profile)) $('profile').elements[key].value = value;
  }
  const plan = state.plan;
  const labels = planLabels[plan.status] || 'Por conectar';
  $('sidebar-plan').textContent = labels;
  $('sidebar-dot').classList.toggle('ready', plan.status === 'ready');
  $('plan-status').textContent = labels;
  $('plan-email').textContent = plan.email || '';
  $('plan-message').textContent = plan.message || '';
  $('pod-connect').hidden = ['ready', 'limit'].includes(plan.status);
  $('pod-connect').textContent = plan.status === 'identity_only' ? 'Autorizar uso del plan' : 'Continuar con ChatGPT';
  $('pod-disconnect').hidden = !['ready', 'identity_only', 'limit'].includes(plan.status);
  $('pod-other').hidden = !plan.registered || ['ready', 'limit'].includes(plan.status);
  $('pod-retry').hidden = plan.status !== 'limit';
  $('model-row').hidden = plan.status !== 'ready';
  $('current-model').textContent = plan.model || '';
  $('plan-card-description').textContent = plan.status === 'ready' ? 'La lectura y la navegación usan tu suscripción.' :
    plan.message || 'Conecta tu suscripción para leer tickets y preparar los portales.';
  $('plan-card-link').textContent = plan.status === 'ready' ? 'Administrar plan →' : 'Conectar ChatGPT →';
  $('gmail-status').textContent = state.gmail === 'connected' ? 'Conectado' : state.gmail === 'configured' ? 'Listo para vincular' : 'Por configurar';
  $('gmail-connect').disabled = state.gmail === 'disconnected';
  $('gmail-disconnect').hidden = state.gmail !== 'connected';
}

function renderPortals() {
  const search = $('portal-search').value.toLowerCase().trim();
  const items = Object.entries(state.catalog).filter(([, item]) => item.name.toLowerCase().includes(search));
  const cards = items.map(([key, item]) => {
    const card = el('article', '', 'card', 'portal-card');
    card.append(el('h2', item.name), el('p', item.guide), el('p', new URL(item.url).host),
      button('Abrir en el navegador', () => openBrowser(key)));
    return card;
  });
  $('portal-list').replaceChildren(...(cards.length ? cards : [blank('Sin resultados', 'Busca por el nombre del comercio.')])) ;
}

async function refresh() {
  state = await api('/api/state');
  renderTickets();
  renderBrowser();
  renderSettings();
  if (activeScreen === 'portales' && !$('portal-list').children.length) renderPortals();
}

function upload(files) {
  if (!files.length) return;
  perform(async () => {
    const data = new FormData();
    for (const file of files) data.append('files', file);
    const rows = await api('/api/upload', 'POST', data);
    const count = rows.filter(row => row.created).length;
    return `Tickets añadidos: ${count}. Ya registrados: ${rows.length - count}.`;
  });
}

for (const id of ['primary-upload', 'choose-file']) $(id).onclick = () => $('file-upload').click();
$('file-upload').onchange = event => {upload([...event.target.files]); event.target.value = '';};
$('batch').onclick = () => prepare(state.jobs.filter(row => row.status === 'ready'));
$('add-sample').onclick = () => perform(async () => {
  await api('/api/demo', 'POST');
  return 'Ticket de ejemplo disponible para revisar.';
});
for (const event of ['dragenter', 'dragover']) $('dropzone').addEventListener(event, value => {
  value.preventDefault();
  $('dropzone').classList.add('dragover');
});
$('dropzone').addEventListener('dragleave', () => $('dropzone').classList.remove('dragover'));
$('dropzone').addEventListener('drop', event => {
  event.preventDefault();
  $('dropzone').classList.remove('dragover');
  upload([...event.dataTransfer.files]);
});
for (const node of document.querySelectorAll('[data-filter]')) node.onclick = () => {
  filter = node.dataset.filter;
  for (const item of document.querySelectorAll('[data-filter]')) item.setAttribute('aria-pressed', String(node === item));
  renderTickets(true);
};
$('ticket-search').oninput = () => renderTickets(true);
$('portal-search').oninput = renderPortals;
$('profile').oninput = () => {profileDirty = true;};
let csfMode = 'local';
for (const [id, mode] of [['read-csf-local', 'local'], ['read-csf', 'agent']]) $(id).onclick = () => {
  csfMode = mode;
  $('csf-file').click();
};
$('csf-file').onchange = event => {
  const file = event.target.files[0];
  event.target.value = '';
  if (!file) return;
  if (csfMode === 'local') return receiptTools.openReader(file);
  if (!window.confirm('¿Usar tu plan de ChatGPT para leer esta constancia fiscal?')) return;
  perform(async () => {
    const data = new FormData();
    data.append('file', file);
    let draft;
    try {
      draft = await api('/api/profile/read-csf', 'POST', data);
    } catch (error) {
      receiptTools.openReader(file);
      return error.message + ' Se abrió la lectura local de la constancia.';
    }
    profileDirty = true;
    for (const key of ['rfc', 'name', 'postal_code', 'regime']) $('profile').elements[key].value = draft[key] || '';
    return ['Comprueba los datos y pulsa Guardar datos fiscales.', ...draft.uncertainties].join(' ');
  });
};
$('profile').onsubmit = event => {
  event.preventDefault();
  const data = Object.fromEntries(new FormData(event.target));
  perform(async () => {
    await api('/api/profile', 'PUT', data);
    profileDirty = false;
    return 'Datos fiscales guardados.';
  });
};
$('receipt-form').onsubmit = event => {
  event.preventDefault();
  const values = Object.fromEntries(new FormData(event.target));
  values.billing_codes = values.billing_codes.split(/\r?\n/).map(x => x.trim()).filter(Boolean);
  perform(async () => {
    const result = await api(`/api/jobs/${editing.id}`, 'PUT', values);
    $('editor').close();
    return result.status === 'duplicate' ? result.message : 'Revisión guardada.';
  });
};
$('run-mode').onchange = () => {
  const manual = $('run-mode').value === 'manual';
  $('manual-notice').hidden = !manual;
  $('auto-row').hidden = manual || !prepared.some(row => row.receipt.provider === 'capufe');
};
$('prepare-form').onsubmit = event => {
  event.preventDefault();
  const data = new FormData(event.target);
  perform(async () => {
    for (const row of prepared) await api(`/api/jobs/${row.id}/run`, 'POST',
      {consent: data.has('consent'), mode: data.get('mode'), auto_issue: data.get('mode') === 'agent' && data.has('auto_issue') && row.receipt.provider === 'capufe'});
    $('prepare-dialog').close();
    route('navegador');
    return 'Trámite en cola.';
  });
};
function openBrowser(provider = null, url = null) {
  return perform(async () => {
    route('navegador');
    await api('/api/browser/open', 'POST', {provider, url});
  });
}
$('browser-open').onclick = () => openBrowser();
$('check-tools').onclick = async () => {
  $('check-tools').disabled = true;
  $('tools-status').textContent = 'Comprobando herramientas…';
  try {
    const report = await api('/api/tools/check', 'POST');
    const entries = [];
    const labels = {ok: 'Comprobado', error: 'Requiere atención', busy: 'En uso',
      configured: 'Configurado', pending: 'Por conectar', manual: 'Verificación manual'};
    for (const tool of report) {
      entries.push(el('dt', tool.name + ' · ' + labels[tool.status]), el('dd', tool.detail + ' ' + tool.fallback));
    }
    $('tools-report').replaceChildren(...entries);
    $('tools-status').textContent = 'Comprobación terminada.';
  } catch (error) {
    $('tools-status').textContent = error.message;
  } finally {
    $('check-tools').disabled = false;
  }
};
$('restart-browser').onclick = () => perform(async () => {
  if (state.browser.open && !window.confirm('Se cerrarán las pestañas del navegador interno. ¿Reiniciarlo?')) return;
  await api('/api/browser/restart', 'POST');
  route('navegador');
  return 'Navegador listo.';
});
$('portal-link-form').onsubmit = event => {
  event.preventDefault();
  openBrowser(null, $('ticket-url').value.trim());
};
$('wa-open').onclick = () => perform(async () => {await api('/api/whatsapp/open', 'POST'); return 'Elige el chat y abre una foto en WhatsApp.';});
$('wa-capture').onclick = () => perform(async () => {
  await api('/api/whatsapp/capture', 'POST');
  route('bandeja');
  return 'Foto de WhatsApp importada.';
});
$('pod-connect').onclick = () => perform(async () => {
  const {url} = await api('/api/chatgpt/connect', 'POST', {consent: state.plan.status === 'identity_only'});
  location.assign(url);
});
$('pod-other').onclick = () => perform(async () => {
  const {url} = await api('/api/chatgpt/connect', 'POST', {new_account: true});
  location.assign(url);
});
$('pod-disconnect').onclick = () => perform(async () => {
  const result = await api('/api/chatgpt/disconnect', 'POST');
  return result.remote_revoked ? 'Sesión de ChatGPT desconectada.' : 'Sesión local cerrada. Revisa la conexión en Ajustes de ChatGPT si quieres revocarla.';
});
$('pod-retry').onclick = () => perform(async () => {await api('/api/chatgpt/retry', 'POST'); return 'Puedes retomar la lectura o el trámite.';});
$('model-list').onclick = () => perform(async () => {
  const models = await api('/api/chatgpt/models');
  if (!models.length) throw Error('Este plan no mostró modelos disponibles.');
  const select = $('model-form').elements.model;
  select.replaceChildren(...models.map(model => new Option(model.name, model.id)));
  if (models.some(model => model.id === state.plan.model)) select.value = state.plan.model;
  $('model-dialog').showModal();
});
$('model-form').onsubmit = event => {
  event.preventDefault();
  perform(async () => {
    await api('/api/chatgpt/model', 'PUT', {model: event.target.elements.model.value});
    $('model-dialog').close();
    return 'Modelo guardado.';
  });
};
function attach(input, url, field, message) {
  const files = [...input.files];
  input.value = '';
  if (!files.length) return;
  perform(async () => {
    const data = new FormData();
    for (const file of files) data.append(field, file);
    await api(url, 'POST', data);
    return message;
  });
}
$('gmail-configure').onclick = () => $('gmail-file').click();
$('gmail-file').onchange = event => attach(event.target, '/api/gmail/client', 'file', 'Cliente OAuth cargado. Pulsa Conectar Gmail.');
$('gmail-connect').onclick = () => perform(async () => { const {url} = await api('/api/gmail/connect', 'POST'); location.assign(url); });
$('gmail-disconnect').onclick = () => perform(async () => { await api('/api/gmail/disconnect', 'POST'); return 'Gmail desvinculado de este equipo.'; });
$('import-history').onclick = () => $('history-file').click();
$('history-file').onchange = event => attach(event.target, '/api/history', 'file', 'Historial incorporado.');
$('cfdi-file').onchange = event => attach(event.target, `/api/jobs/${attachJob}/attachments`, 'files', 'PDF y XML comprobados.');
$('gmail-form').onsubmit = event => {
  event.preventDefault();
  perform(async () => {
    await api(`/api/jobs/${gmailJob.id}/gmail`, 'POST', {query: event.target.elements.query.value});
    $('gmail-dialog').close();
    return 'PDF y XML recuperados y comprobados.';
  });
};
$('copy-request').onclick = async () => {
  try { await navigator.clipboard.writeText($('request-text').value); $('request-dialog').close(); notify('Solicitud copiada.'); }
  catch { $('request-text').select(); }
};
for (const close of document.querySelectorAll('[data-close]')) close.onclick = () => $(close.dataset.close).close();
window.addEventListener('hashchange', openScreen);
openScreen();
const outcome = new URLSearchParams(location.search).get('conexion');
if (outcome) {
  history.replaceState(null, '', '/#ajustes');
  if (outcome.endsWith('-error')) notify('La conexión no se completó. Puedes volver a iniciarla.', true);
  else notify(outcome.startsWith('chatgpt') ? 'Conexión con ChatGPT guardada. Revisa el estado de tu plan.' : 'Gmail conectado en modo lectura.');
}
refresh().then(() => {if (activeScreen === 'portales') renderPortals();}).catch(error => notify(error.message, true));
setInterval(() => {
  if (!busy && !document.hidden) refresh().catch(error => notify(error.message, true));
}, 1250);
