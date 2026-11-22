import {installViewer} from './visor.js';
import {installPortalMail} from './correo-portal.js';
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
let refreshNumber = 0;
let appliedRefresh = 0;
let savingAutomation = false;
let lastLiveJob = null;
const handledLiveJobs = new Set();
let selectedRetry = '';
const retryStrip = document.createElement('div');
retryStrip.id = 'ticket-retry';
retryStrip.className = 'buttons';
retryStrip.hidden = true;
$('automation-review').after(retryStrip);

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
  renderCaptcha();
}

const viewer = installViewer({api, token: () => state.token, showError: error => notify(error, true), perform,
  captchaId: () => state.supports_captcha_control && captchaTicket()?.id,
  accountEmail: () => state.profile?.email || state.plan?.email || ''});
installPortalMail({api, gmailState: () => state.gmail});
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

function edit(row, focusField) {
  editing = row;
  const form = $('receipt-form');
  form.reset();
  const {provider} = form.elements;
  provider.replaceChildren(new Option('Portal que aparece en mi ticket', 'portal_ticket'));
  const saved = document.createElement('optgroup');
  saved.label = 'Comercios guardados';
  for (const [id, value] of Object.entries(state.catalog)) saved.append(new Option(value.name, id));
  provider.append(saved);
  provider.add(new Option('Otro comercio · solicitar directamente', 'solicitud_directa'));
  form.elements.portal_url.value = '';
  for (const [key, value] of Object.entries(row.receipt)) {
    if (form.elements.namedItem(key)) form.elements[key].value = key === 'billing_codes' ? value.join('\n') : value;
  }
  if (!state.catalog[row.receipt.provider] && row.receipt.provider !== 'solicitud_directa') provider.value = 'portal_ticket';
  provider.onchange = () => {
    const custom = provider.value === 'portal_ticket';
    $('receipt-portal-row').hidden = !custom;
    form.elements.portal_url.disabled = !custom;
    form.elements.portal_url.required = custom;
  };
  provider.onchange();
  $('view-source').href = `/api/jobs/${row.id}/source`;
  $('editor').showModal();
  if (focusField) form.elements.namedItem(focusField)?.focus();
}

function prepare(rows) {
  if (!state.profile) {
    route('ajustes');
    return notify('Guarda tus datos fiscales antes de iniciar el trámite.');
  }
  prepared = rows.filter(row => (row.status === 'ready' || row.can_retry) && !existingAttempt(row) && !row.receipt.source.startsWith('Ejemplo:'));
  if (!prepared.length) return notify('No hay tickets reales revisados para preparar.');
  const names = [...new Set(prepared.map(({receipt}) => receipt.provider === 'portal_ticket'
    ? receipt.merchant + ' (' + new URL(receipt.portal_url).origin + ')' : state.catalog[receipt.provider]?.name))];
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
  const agent = state.plan.status === 'ready';
  if (agent && !window.confirm('¿Leer el original con tu plan de ChatGPT?')) return;
  return perform(async () => {
    const result = await api('/api/jobs/' + row.id + '/analyze', 'POST', {mode: agent ? 'agent' : 'local', consent: agent});
    if (result.status === 'review') edit(result);
    return result.message;
  });
}

function reading(row) {
  return state.automation?.phase === 'reading' && state.automation.current_id === row.id;
}

function isLive(row) {
  return state.browser.open && row.id === state.active_job && row.id === state.browser.job_id &&
    ['running', 'issuing', 'needs_user'].includes(row.status);
}

function liveJob() {
  return state.active_job ? state.jobs.find(isLive) : null;
}

function captchaTicket() {
  return state.jobs.find(row => row.id === state.active_job && row.status === 'needs_user' && row.human_kind === 'captcha');
}

function captchaView(key) {
  const browser = state.browser;
  return browser.mode === 'job' && browser.job_id === key && browser.open && browser.manual && !browser.dialog;
}

function renderCaptcha() {
  const banner = $('captcha-alert');
  if (!banner) return;
  const row = captchaTicket();
  banner.hidden = !row;
  if (!row) return;
  const key = row.id;
  const viewing = captchaView(key);
  const occupied = viewing && viewer.hasControl() && activeScreen === 'navegador';
  const returning = state.browser.mode === 'personal' && state.browser.return_job_id === key;
  const title = 'Verificación del portal · ' + (row.receipt.merchant || row.receipt.ticket_number || 'Tu ticket');
  const detail = occupied ? 'Resuelve el reto en la imagen del portal. Cuando el portal avance, pulsa Devolver control.' :
    viewing || returning ? '¿Quieres verificarte ahora? Tú completas el reto del comercio en el visor de este ticket.' :
    'Tú completas el reto del comercio. Esperando a que el visor de este ticket esté disponible.';
  if ($('captcha-title').textContent !== title) $('captcha-title').textContent = title;
  if ($('captcha-detail').textContent !== detail) $('captcha-detail').textContent = detail;
  const control = $('captcha-action');
  control.dataset.ticket = key;
  control.textContent = occupied ? 'Devolver control' : 'Resolver verificación';
  control.disabled = !viewing && !returning;
}

if ($('captcha-action')) $('captcha-action').onclick = () => {
  const key = $('captcha-action').dataset.ticket;
  const returningControl = captchaView(key) && activeScreen === 'navegador' && viewer.hasControl();
  perform(async () => {
    await refresh();
    if (captchaTicket()?.id !== key) throw Error('Este ticket ya no solicita tu verificación.');
    if (returningControl) {
      if (!captchaView(key) || !viewer.hasControl()) throw Error('La pantalla de este ticket cambió.');
      await viewer.releaseControl();
      return;
    }
    if (state.browser.mode === 'personal' && state.browser.return_job_id === key) {
      if (!state.supports_captcha_control) { route('navegador'); return 'Pulsa Volver al trámite y comprueba el ticket antes de tomar control.'; }
      await viewer.returnToCaptcha(key);
      await refresh();
    }
    if (captchaTicket()?.id !== key || !captchaView(key)) throw Error('El visor de este ticket ya no está disponible.');
    route('navegador');
    if (!state.supports_captcha_control) return 'Comprueba el nombre del ticket y pulsa Tomar control.';
    await viewer.acquireControl(key);
    if (viewer.hasControl()) $('browser-image').scrollIntoView({block: 'center'});
  });
};

function showLive(row) {
  handledLiveJobs.add(row.id);
  lastLiveJob = row.id;
  route('navegador');
  if (state.browser.mode === 'personal' && state.browser.return_job_id === row.id) viewer.returnToJob();
}

function trackLive(interacting) {
  const row = liveJob();
  if (!row || handledLiveJobs.has(row.id)) return;
  handledLiveJobs.add(row.id);
  if (activeScreen === 'navegador') lastLiveJob = row.id;
  if (state.automation?.enabled && !interacting && activeScreen === 'bandeja') showLive(row);
}

function renderAutomation() {
  const automation = state.automation;
  const control = $('automation-enabled');
  control.disabled = !automation || savingAutomation;
  if (!savingAutomation) control.checked = Boolean(automation?.enabled);
  const ticket = state.jobs.find(row => row.id === automation?.current_id);
  const name = ticket?.receipt.merchant || ticket?.receipt.source;
  const progress = automation?.message || '';
  const text = name && progress ? name + ' · ' + progress : progress;
  if ($('automation-status').textContent !== text) $('automation-status').textContent = text;
  const reviewButton = $('automation-review');
  const action = automation?.enabled && automation.phase === 'review' && ticket?.status === 'review' ? reviewAction(ticket) : null;
  reviewButton.hidden = !action;
  if (action) {
    if (reviewButton.textContent !== action.label) reviewButton.textContent = action.label;
    reviewButton.onclick = () => edit(ticket, action.field);
  }
}

function reviewAction(row) {
  if (!row.receipt.payment_code && (/forma de pago/i.test(row.message) || (row.receipt.merchant && row.receipt.date && row.receipt.amount))) {
    return {label: 'Completar forma de pago', field: 'payment_code'};
  }
  if (row.receipt.provider === 'portal_ticket' && row.receipt.portal_url && state.automation?.enabled) {
    return {label: 'Revisar portal', field: 'portal_url'};
  }
  return {label: 'Revisar'};
}

function existingAttempt(row) {
  return Boolean(row.issue_attempted || row.issuance_seen || row.human_exposed || row.uuid ||
    ['issued_waiting_files', 'uncertain'].includes(row.status));
}

function retryKind(row) {
  if (!row || row.historical || terminal.has(row.status) || row.receipt.source.startsWith('Ejemplo:')) return null;
  if (state.supports_ticket_retry) return ['resume', 'queue', 'recover'].includes(row.retry_action) ? row.retry_action : null;
  if (existingAttempt(row) && ['blocked', 'issued_waiting_files', 'uncertain'].includes(row.status)) return 'local';
  if (row.can_retry && !existingAttempt(row)) return 'prepare';
  if (row.status === 'needs_user' && row.id === state.active_job && ['plan', 'unknown', 'blocked'].includes(row.human_kind) && !existingAttempt(row)) return 'resume';
  return null;
}

function retryButton(row) {
  const kind = retryKind(row);
  if (!kind) return null;
  const recovery = kind === 'recover' || kind === 'local';
  const label = recovery ? 'Recuperar comprobantes' : kind === 'resume' ? 'Retomar trámite' : 'Reintentar';
  const control = button(label, () => {
    selectedRetry = row.id;
    lastLiveJob = row.id;
    if (kind === 'prepare') return prepare([row]);
    return perform(async () => {
      const path = '/api/jobs/' + encodeURIComponent(row.id);
      if (kind === 'local') {
        await api(path + '/recover-local', 'POST');
        return 'PDF y XML del intento anterior cotejados.';
      }
      if (!state.supports_ticket_retry) {
        await api(path + '/decision', 'POST', {decision: 'resume'});
        showLive(row);
        return 'Reanudando el trámite en su sesión.';
      }
      const result = await api(path + '/retry', 'POST');
      if (result.action === 'needs_user' && kind === 'queue' && state.plan.status !== 'ready') route('ajustes');
      else if (['resumed', 'active', 'queued', 'needs_user'].includes(result.action) &&
               (!state.active_job || state.active_job === row.id)) showLive(row);
      return result.message;
    });
  }, 'primary');
  control.title = kind === 'local' ? 'Buscar el PDF y XML del intento anterior entre los archivos del expediente.' :
    recovery ? 'Buscar el PDF y XML del intento existente.' : label;
  return control;
}

function renderRetryStrip() {
  const choices = state.jobs.filter(row => retryKind(row));
  retryStrip.hidden = !choices.length;
  if (!choices.length) { retryStrip.replaceChildren(); return; }
  const ticket = choices.find(row => row.id === selectedRetry) ||
    choices.find(row => row.id === state.automation?.current_id) || choices[0];
  const name = row => [row.receipt.merchant || 'Ticket', row.receipt.ticket_number || row.receipt.billing_codes[0] || currency(row.receipt.amount)].filter(Boolean).join(' · ');
  const title = choices.length === 1 ? el('strong', name(ticket)) : document.createElement('select');
  if (choices.length > 1) {
    title.setAttribute('aria-label', 'Seleccionar trámite pendiente');
    title.replaceChildren(...choices.map(row => new Option(name(row), row.id)));
    title.value = ticket.id;
    title.onchange = () => { selectedRetry = title.value; renderRetryStrip(); };
  }
  const action = retryButton(ticket);
  retryStrip.replaceChildren(title, action);
  if (['recover', 'local'].includes(retryKind(ticket))) retryStrip.append(el('span', action.title, 'muted'));
}

function primaryAction(row, realTicket) {
  if (isLive(row) || row.id === state.browser.return_job_id) return button('Ver en vivo', () => showLive(row), 'primary');
  if (reading(row)) return null;
  if (row.status === 'new' && realTicket) {
    if (!state.automation?.enabled) return button(state.plan.status === 'ready' ? 'Leer con ChatGPT' : 'Leer en el contenedor',
      () => readTicket(row), 'primary');
    if (state.plan.status !== 'ready') return button('Conectar ChatGPT', () => route('ajustes'));
    return null;
  }
  if (row.status === 'review') {
    const action = reviewAction(row);
    return button(action.label, () => edit(row, action.field), 'primary');
  }
  if (row.status === 'new') return button('Revisar', () => edit(row), 'primary');
  if (realTicket && ['external', 'whatsapp_preparing'].includes(row.status)) {
    return button(row.issue_attempted || row.status === 'whatsapp_preparing' ? 'Ver solicitud WhatsApp' : 'Solicitar por WhatsApp',
      () => showWhatsApp(row), 'primary');
  }
  if (row.status === 'external') return button('Preparar solicitud', () => request(row), 'primary');
  if (row.status === 'ready' && realTicket) {
    const custom = row.receipt.provider === 'portal_ticket';
    if (!state.profile) return button('Completar perfil fiscal', () => route('ajustes'));
    if (custom || !state.automation?.enabled) return button(custom ? 'Confirmar portal' : 'Preparar', () => prepare([row]), 'primary');
    if (state.plan.status !== 'ready') return button('Conectar ChatGPT', () => route('ajustes'));
  }
  return null;
}

function rowActions(row) {
  const group = el('div', '', 'row-actions');
  const realTicket = !row.historical && !row.receipt.source.startsWith('Ejemplo:');
  const whatsappPrimary = realTicket && ['external', 'whatsapp_preparing'].includes(row.status);
  const primary = primaryAction(row, realTicket);
  const retry = retryButton(row);
  if (retry) group.append(retry);
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
  if (review.has(row.status) && !existingAttempt(row) && !reading(row)) {
    const readLabel = state.plan.status === 'ready' ? 'Leer con ChatGPT' : 'Leer en el contenedor';
    if (primary?.textContent !== readLabel) panel.append(button(readLabel, () => readTicket(row), 'plain'));
    if (row.status !== 'review') panel.append(button('Editar datos', () => edit(row), 'plain'));
    if (row.status === 'ready' && realTicket && row.receipt.provider !== 'portal_ticket' && state.automation?.enabled) {
      panel.append(button('Preparar', () => prepare([row]), 'plain'));
    }
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
    if (!isDocs && state.jobs.length) {
      const empty = blank('Sin tickets con estos filtros', 'Prueba con otro comercio o folio.');
      empty.append(button('Mostrar todos los tickets', () => {
        $('ticket-search').value = '';
        document.querySelector('[data-filter="all"]').click();
      }));
      container.replaceChildren(empty);
    } else {
      container.replaceChildren(isDocs
        ? blank('Aún no hay comprobantes', 'Los CFDI y las emisiones pendientes aparecerán cuando avances en un trámite.')
        : blank('Aquí llegan tus tickets', 'Sube tu primer recibo o carga un ejemplo para practicar.'));
    }
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
    const statusLabel = reading(row) ? 'Leyendo con ChatGPT' :
      row.status === 'new' && state.automation?.enabled && !row.historical && state.plan.status === 'ready' ? 'En cola de lectura' :
      row.status === 'external' && row.issue_attempted ? 'Solicitud en seguimiento' : jobLabels[row.status];
    stateCell.append(el('span', statusLabel || row.status, 'job-status', row.status));
    const actions = el('td');
    actions.append(rowActions(row));
    const tr = el('tr');
    const amountCell = el('td', currency(receipt.amount), 'ticket-amount');
    if (row.match_type === 'agrupado') {
      amountCell.replaceChildren(el('strong', currency(row.ticket_total)),
        el('span', 'De este ticket', 'ticket-meta'), el('span', 'Total del CFDI ' + currency(row.invoice_total), 'ticket-meta'));
    }
    tr.append(info, amountCell, stateCell, actions);
    body.append(tr);
  }
  content.append(head, body);
  wrap.append(content);
  container.replaceChildren(wrap);
}

function renderTickets(force = false) {
  const search = $('ticket-search');
  $('clear-ticket-search').hidden = !search.value;
  const query = search.value.trim().toLowerCase();
  const signature = JSON.stringify([filter, query, state.jobs, state.supports_ticket_retry, state.automation?.enabled, state.automation?.current_id,
    state.automation?.phase, liveJob()?.id, state.plan.status, Boolean(state.profile)]);
  if (!force && signature === rowSignature) return;
  rowSignature = signature;
  renderRetryStrip();
  const rows = state.jobs.filter(row => (filter === 'all' || filter === 'review' && (review.has(row.status) && !existingAttempt(row) || retryKind(row)) ||
    filter === 'working' && (working.has(row.status) || row.status === 'whatsapp_preparing' || row.status === 'external' && row.issue_attempted))
    && [row.receipt.merchant, row.receipt.ticket_number,
      row.receipt.amount, row.receipt.source, ...row.receipt.billing_codes].join(' ').toLowerCase().includes(query));
  table($('tickets'), rows);
  table($('documents'), state.jobs.filter(row => documents.has(row.status) || row.has_pdf || row.has_xml ||
    row.status === 'external' && row.issue_attempted), true);
  $('count-total').textContent = state.jobs.length;
  $('count-pending').textContent = state.jobs.filter(row => !terminal.has(row.status)).length;
  $('count-verified').textContent = new Set(state.jobs.filter(row => row.status === 'verified').map(row => row.uuid || row.id)).size;
  $('batch').disabled = !state.jobs.some(row => row.status === 'ready' && !row.receipt.source.startsWith('Ejemplo:'));
}

function renderBrowser() {
  const personal = state.browser.mode === 'personal';
  const opening = !personal && state.active_job && state.browser.job_id !== state.active_job;
  const browser = opening ? {open: false, revision: state.browser.revision} : state.browser;
  viewer.update(browser, Boolean(state.active_job));
  $('browser-empty').querySelector('h3').textContent = opening ? 'Abriendo el portal del ticket…' :
    personal ? 'Nueva pestaña' : 'Sin página abierta';
  $('browser-empty').querySelector('p').textContent = opening ? 'Aquí verás a Chrome en cuanto abra el portal.' :
    'Escribe la dirección del portal que quieres abrir.';
  const row = state.jobs.find(job => job.id === (state.active_job || state.browser.job_id || lastLiveJob)) ||
    state.jobs.find(job => job.id === selectedRetry && retryKind(job)) || state.jobs.find(job => retryKind(job));
  const signature = JSON.stringify([row, state.active_job, personal, state.profile?.rfc, state.plan.status]);
  if (signature === taskSignature) return;
  taskSignature = signature;
  $('browser-task-title').textContent = row && !state.active_job ? 'Último trámite' : 'Trámite actual';
  const target = $('browser-task');
  if (!row) {
    target.replaceChildren(el('p', 'Los datos del ticket y las intervenciones aparecerán aquí.', 'muted'));
    return;
  }
  const detail = el('dl');
  for (const [label, value] of [['Comercio', row.receipt.merchant], ['Importe del ticket', currency(row.ticket_total || row.receipt.amount)],
    ['Total del CFDI agrupado', row.match_type === 'agrupado' ? currency(row.invoice_total) : ''],
    ['Fecha', row.receipt.date], ['Folio', row.receipt.ticket_number || row.receipt.billing_codes.join(' · ')],
    ['Forma de pago', payments[row.receipt.payment_code]], ['RFC receptor', state.profile?.rfc]]) {
    if (!value) continue;
    const line = el('div');
    line.append(el('dt', label), el('dd', value));
    detail.append(line);
  }
  target.replaceChildren(el('span', jobLabels[row.status], 'job-status', row.status), detail, el('p', row.message, 'muted'));
  const actions = el('div', '', 'buttons');
  const retry = retryButton(row);
  if (retry) actions.append(retry);
  if (row.id !== state.active_job) {
    if (row.has_pdf || row.has_xml) actions.append(link('Ver comprobantes', '#comprobantes', 'secondary button-link'));
    actions.append(link('Volver a la bandeja', '#bandeja', 'secondary button-link'));
    target.append(actions);
    return;
  }
  if (row.human_kind && !personal) {
    const issue = row.human_kind === 'issue';
    const label = issue ? 'Aprobar emisión' : row.human_kind === 'operator' ? 'Terminar y cotejar' : 'Continuar';
    const resume = button(label, () => perform(() =>
      api(`/api/jobs/${row.id}/decision`, 'POST', {decision: issue ? 'approve' : 'resume'})), 'primary');
    if (!retry || (row.human_kind !== 'plan' && row.human_kind !== 'blocked' && row.human_kind !== 'unknown')) actions.append(resume);
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
    for (const [key, value] of Object.entries(state.profile)) {
      if ($('profile').elements[key]) $('profile').elements[key].value = value || '';
    }
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
  $('gmail-connect').textContent = state.gmail === 'connected' ? 'Vincular otra cuenta' : 'Conectar Gmail';
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
  const request = ++refreshNumber;
  const latest = await api('/api/state');
  if (request < appliedRefresh) return;
  appliedRefresh = request;
  const interacting = document.hidden || viewer.hasControl() || Boolean(document.querySelector('dialog[open], .row-actions details[open]')) ||
    Boolean(document.activeElement?.matches('input:not([type=checkbox]):not([type=file]), select, textarea, [contenteditable=true]'));
  state = latest;
  trackLive(interacting);
  renderAutomation();
  renderTickets();
  renderBrowser();
  renderCaptcha();
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
    const duplicates = rows.length - count;
    const readingSoon = count && state.automation?.enabled ? ' La bandeja mostrará el avance.' : '';
    return `${count} ${count === 1 ? 'ticket añadido' : 'tickets añadidos'}.` +
      (duplicates ? ` Ya registrados: ${duplicates}.` : '') + readingSoon;
  });
}

for (const id of ['primary-upload', 'choose-file']) $(id).onclick = () => $('file-upload').click();
$('file-upload').onchange = event => {upload([...event.target.files]); event.target.value = '';};
$('batch').onclick = () => prepare(state.jobs.filter(row => row.status === 'ready'));
$('automation-enabled').onchange = async event => {
  const enabled = event.target.checked;
  savingAutomation = true;
  renderAutomation();
  try {
    await api('/api/automation', 'PUT', {enabled});
    await refresh();
  } catch (error) {
    notify(error.message, true);
  } finally {
    savingAutomation = false;
    renderAutomation();
  }
};
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
$('clear-ticket-search').onclick = () => {
  $('ticket-search').value = '';
  $('ticket-search').focus();
  renderTickets(true);
};
$('portal-search').oninput = renderPortals;
$('profile').oninput = () => {profileDirty = true;};
let csfMode = 'extract';
for (const [id, mode] of [['upload-csf', 'extract'], ['read-csf-local', 'local'], ['read-csf', 'agent']]) $(id).onclick = () => {
  csfMode = mode;
  $('csf-file').click();
};
$('csf-file').onchange = event => {
  const file = event.target.files[0];
  event.target.value = '';
  if (!file) return;
  if (csfMode === 'local') return receiptTools.openReader(file);
  const mode = csfMode;
  if (mode === 'agent' && !window.confirm('¿Usar tu plan de ChatGPT para leer esta constancia fiscal?')) return;
  perform(async () => {
    const data = new FormData();
    data.append('file', file);
    data.append('mode', mode);
    $('csf-status').textContent = 'Leyendo la constancia…';
    let draft;
    try {
      draft = await api('/api/profile/read-csf', 'POST', data);
    } catch (error) {
      $('csf-status').textContent = error.message;
      throw error;
    }
    const fields = $('profile').elements;
    const previousRFC = fields.rfc.value.trim().toUpperCase();
    if (previousRFC && !draft.rfc) {
      $('csf-status').textContent = 'No se pudo cotejar el RFC. Revisa el documento o usa Leer con ChatGPT.';
      return;
    }
    profileDirty = true;
    if (previousRFC && previousRFC !== draft.rfc) {
      for (const name of ['name','postal_code','regime','email','cfdi_use','whatsapp_phone']) fields[name].value = '';
    }
    for (const key of ['rfc', 'name', 'postal_code', 'regime', 'email']) {
      if (draft[key]) fields[key].value = draft[key];
    }
    const options = draft.regime_options || [];
    const selector = $('csf-regimes');
    selector.replaceChildren(new Option('Elige el régimen para facturar', ''));
    for (const option of options) selector.add(new Option(option.code + ' · ' + option.name, option.code));
    $('csf-regime-row').hidden = options.length < 2;
    selector.required = options.length > 1;
    if (selector.required && !options.some(option => option.code === fields.regime.value)) fields.regime.value = '';
    selector.value = fields.regime.value;
    const required = {rfc:'RFC', name:'nombre', postal_code:'CP fiscal', regime:'régimen', email:'correo de entrega', cfdi_use:'uso del CFDI'};
    const missing = Object.entries(required).filter(([key]) => !fields[key].value.trim()).map(([,label]) => label);
    $('csf-status').textContent = [...(draft.uncertainties || []), missing.length ? 'Completa ' + missing.join(', ') + '.' : '',
      'Revisa los campos y pulsa Guardar datos fiscales.'].filter(Boolean).join(' ');
  });
};
$('csf-regimes').onchange = event => { $('profile').elements.regime.value = event.target.value; };
$('profile').elements.regime.oninput = event => { $('csf-regimes').value = event.target.value; };
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
    if (state.automation?.enabled && state.profile && result.status === 'ready' && result.receipt.provider === 'portal_ticket') {
      prepare([result]);
      return 'Revisión guardada. Confirma el dominio para abrir el portal.';
    }
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
      {consent: data.has('consent'), mode: data.get('mode'), confirmed_url: row.receipt.portal_url || '',
        auto_issue: data.get('mode') === 'agent' && data.has('auto_issue') && row.receipt.provider === 'capufe'});
    $('prepare-dialog').close();
    route('navegador');
    return 'Trámite en cola.';
  });
};
function openBrowser(provider = null, url = null) {
  return perform(async () => {
    let confirmed_origin = '';
    if (url) {
      const target = new URL(url);
      if (target.protocol !== 'https:' || target.username || target.password) throw new Error('Introduce un enlace HTTPS público del comercio.');
      if (!window.confirm('¿Abrir el portal de este comercio en ' + target.origin + '?')) return;
      confirmed_origin = target.origin;
    }
    route('navegador');
    await api('/api/browser/open', 'POST', {provider, url, confirmed_origin});
  });
}
$('open-catalog').onclick = () => openBrowser();
function resumePersonalIfIdle() {
  if (activeScreen === 'navegador' && state.token && !state.active_job && state.browser.mode !== 'personal') {
    viewer.openPersonal();
  }
}
document.querySelector('[data-nav="navegador"]').addEventListener('click', event => {
  if (event.button !== 0 || event.ctrlKey || event.metaKey || event.shiftKey || event.altKey) return;
  event.preventDefault();
  route('navegador');
  resumePersonalIfIdle();
});
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
$('gmail-file').onchange = event => {
  if (event.target.files.length) viewer.gmail.reset();
  attach(event.target, '/api/gmail/client', 'file', 'Cliente OAuth cargado. Pulsa Conectar Gmail.');
};
$('gmail-connect').onclick = viewer.gmail.connect;
$('gmail-disconnect').onclick = () => perform(async () => { await api('/api/gmail/disconnect', 'POST'); viewer.gmail.reset(); return 'Gmail desvinculado de este equipo.'; });
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
window.addEventListener('hashchange', () => {
  const selected = location.hash.slice(1);
  if (selected !== activeScreen) {
    for (const row of state.jobs) {
      if (row.id === state.automation?.current_id || ['ready', 'queued', 'running', 'issuing', 'needs_user'].includes(row.status)) {
        handledLiveJobs.add(row.id);
      }
    }
    if (selected === 'navegador' && liveJob()) lastLiveJob = liveJob().id;
  }
  openScreen();
});
openScreen();
const connectionParams = new URLSearchParams(location.search);
const outcome = connectionParams.get('conexion');
if (outcome) {
  history.replaceState(null, '', '/#ajustes');
  if (outcome === 'gmail-ok' || outcome === 'gmail-error') {
    viewer.gmail.notifyReturn(outcome);
    const problems = {
      caducado: 'El enlace de Google caducó. Pulsa Conectar Gmail para obtener uno nuevo.',
      consentimiento: 'La autorización se canceló. Puedes volver a conectar Gmail cuando estés listo.',
      intercambio: 'Google no completó la conexión. Comprueba que el cliente OAuth de escritorio sea correcto y vuelve a conectar Gmail.',
      permiso: 'Faltó el permiso de lectura. Vuelve a conectar Gmail y autoriza la lectura del buzón.',
      'sin-renovacion': 'Google no concedió acceso continuo. Vuelve a conectar Gmail y confirma el permiso.',
      buzon: 'FACTURAI no pudo comprobar el buzón. Revisa que Gmail esté habilitado para esa cuenta y vuelve a conectar.'
    };
    if (outcome === 'gmail-ok') notify('Gmail vinculado. Puedes cerrar esta pestaña y volver a la pestaña original de FACTURAI.');
    else notify(problems[connectionParams.get('motivo')] || 'La conexión con Gmail no se completó. Vuelve a la pestaña original de FACTURAI e inténtalo de nuevo.', true);
  } else if (outcome === 'chatgpt-error') notify('La conexión no se completó. Puedes volver a iniciarla.', true);
  else if (outcome.startsWith('chatgpt')) notify('Conexión con ChatGPT guardada. Revisa el estado de tu plan.');
}
refresh().then(() => {
  if (activeScreen === 'portales') renderPortals();
  resumePersonalIfIdle();
}).catch(error => notify(error.message, true));
setInterval(() => {
  if (!busy && !document.hidden) refresh().catch(error => notify(error.message, true));
}, 1250);
