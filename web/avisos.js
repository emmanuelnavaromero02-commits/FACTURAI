// Notices in the panel: the bell with its list, turning on notices on this device, their settings,
// the standing permissions and the data used to create accounts in billing portals.
// Self-contained module: it reads the panel token from /api/state and never touches app.js state.

const TICKET_ID = /^[\w-]{1,128}$/;
const SVG = 'http://www.w3.org/2000/svg';
const FIELDS = [
  ['nombres', 'Nombre(s)', 'text', 80], ['apellido_paterno', 'Apellido paterno', 'text', 80],
  ['apellido_materno', 'Apellido materno', 'text', 80], ['telefono', 'Teléfono', 'tel', 20],
  ['fecha_nacimiento', 'Fecha de nacimiento', 'date', 10], ['calle', 'Calle', 'text', 120],
  ['numero_exterior', 'Número exterior', 'text', 20], ['numero_interior', 'Número interior', 'text', 20],
  ['colonia', 'Colonia', 'text', 120], ['municipio', 'Municipio o ciudad', 'text', 120],
  ['estado', 'Estado', 'text', 60], ['pais', 'País', 'text', 60]
];

let token = '';
let unread = 0;
const ui = {};

function el(tag, text = '', ...classes) {
  const node = document.createElement(tag);
  if (text) node.textContent = text;
  if (classes.length) node.className = classes.join(' ');
  return node;
}

function button(text, variant = 'secondary', type = 'button') {
  const node = el('button', text, variant);
  node.type = type;
  return node;
}

async function loadToken() {
  const response = await fetch('/api/state');
  if (!response.ok) throw Error('No se pudo abrir el panel. Recarga la página.');
  token = (await response.json()).token || '';
}

async function api(path, method = 'GET', payload, retry = true) {
  if (method !== 'GET' && !token) await loadToken();
  const headers = {'x-app-token': token};
  const options = {method, headers};
  if (payload !== undefined) {
    headers['content-type'] = 'application/json';
    options.body = JSON.stringify(payload);
  }
  const response = await fetch(path, options);
  // The panel token changes when the program restarts.
  if (response.status === 403 && retry && method !== 'GET') {
    await loadToken();
    return api(path, method, payload, false);
  }
  if (!response.ok) {
    let message = '';
    if ((response.headers.get('content-type') || '').includes('application/json')) {
      const data = await response.json();
      message = typeof data.detail === 'string' ? data.detail : 'Revisa los datos.';
    } else message = (await response.text()).slice(0, 300);
    throw Error(message || 'No se completó la solicitud.');
  }
  return response.json();
}

// ------------------------------------------------------------------ device support

function isApple() {
  return /iPhone|iPad|iPod/.test(navigator.userAgent) || (navigator.platform === 'MacIntel' && navigator.maxTouchPoints > 1);
}

function installed() {
  return Boolean(window.matchMedia && window.matchMedia('(display-mode: standalone)').matches) || navigator.standalone === true;
}

export function pushSupport() {
  if (!window.isSecureContext) {
    return {ok: false, text: 'Este navegador solo permite avisos cuando el panel se abre con una dirección segura (https) o en esta misma computadora.'};
  }
  if (isApple() && !installed()) {
    return {ok: false, text: 'En iPhone y iPad los avisos solo funcionan con el panel en tu pantalla de inicio: toca Compartir, elige «Agregar a inicio», abre FACTURAI desde ese icono y pulsa otra vez «Activar avisos en este dispositivo».'};
  }
  if (!('serviceWorker' in navigator) || !('PushManager' in window) || !('Notification' in window)) {
    return {ok: false, text: isApple()
      ? 'Para recibir avisos en iPhone o iPad necesitas iOS 16.4 o posterior.'
      : 'Este navegador no permite avisos. Prueba con Chrome, Edge, Firefox o Safari actualizados.'};
  }
  if (Notification.permission === 'denied') {
    return {ok: false, text: 'Los avisos están bloqueados para este sitio. Permítelos en la configuración del navegador (el candado junto a la dirección, luego Notificaciones) y vuelve a intentarlo.'};
  }
  return {ok: true, text: ''};
}

function deviceName() {
  const agent = navigator.userAgent;
  const browser = /Edg\//.test(agent) ? 'Edge' : /SamsungBrowser/.test(agent) ? 'Samsung Internet'
    : /Firefox\//.test(agent) ? 'Firefox' : /Chrome\//.test(agent) ? 'Chrome' : /Safari\//.test(agent) ? 'Safari' : 'Navegador';
  const system = /Android/.test(agent) ? 'Android' : /iPhone/.test(agent) ? 'iPhone' : /iPad/.test(agent) || isApple() ? 'iPad'
    : /Windows/.test(agent) ? 'Windows' : /Mac OS/.test(agent) ? 'Mac' : /Linux/.test(agent) ? 'Linux' : '';
  return system ? `${browser} en ${system}` : browser;
}

function keyBytes(value) {
  const base = value.replace(/-/g, '+').replace(/_/g, '/') + '='.repeat((4 - value.length % 4) % 4);
  return Uint8Array.from(atob(base), char => char.charCodeAt(0));
}

function sameKey(buffer, value) {
  if (!buffer) return false;
  const current = new Uint8Array(buffer);
  const wanted = keyBytes(value);
  return current.length === wanted.length && current.every((byte, index) => byte === wanted[index]);
}

async function currentSubscription() {
  if (!('serviceWorker' in navigator)) return null;
  const registration = await navigator.serviceWorker.getRegistration('/');
  return registration && registration.pushManager ? registration.pushManager.getSubscription() : null;
}

async function activate() {
  const support = pushSupport();
  if (!support.ok) throw Error(support.text);
  // Asked only here, after the person pressed the button.
  const permission = await Notification.requestPermission();
  if (permission !== 'granted') {
    throw Error('No se activaron los avisos porque el navegador no dio permiso. Puedes volver a intentarlo cuando quieras.');
  }
  const registration = await navigator.serviceWorker.register('/sw.js', {scope: '/'});
  await navigator.serviceWorker.ready;
  const {llave} = await api('/api/avisos/llave');
  let subscription = await registration.pushManager.getSubscription();
  if (subscription && !sameKey(subscription.options && subscription.options.applicationServerKey, llave)) {
    await subscription.unsubscribe();
    subscription = null;
  }
  if (!subscription) {
    subscription = await registration.pushManager.subscribe({userVisibleOnly: true, applicationServerKey: keyBytes(llave)});
  }
  const data = subscription.toJSON();
  await api('/api/avisos/dispositivos', 'POST', {endpoint: data.endpoint, keys: data.keys, nombre: deviceName()});
  return 'Listo: este dispositivo recibirá los avisos.';
}

async function deactivate() {
  const subscription = await currentSubscription();
  if (subscription) {
    await api('/api/avisos/dispositivos/quitar', 'POST', {endpoint: subscription.endpoint});
    await subscription.unsubscribe();
  }
  return 'Este dispositivo ya no recibirá avisos.';
}

// ------------------------------------------------------------------ opening a notice

function highlight(id) {
  if (!TICKET_ID.test(id)) return;
  const started = Date.now();
  const look = () => {
    const row = [...document.querySelectorAll('#tickets tr[data-job-id]')].find(item => item.dataset.jobId === id);
    if (row) {
      row.scrollIntoView({block: 'center', behavior: 'smooth'});
      row.classList.add('aviso-destacado');
      setTimeout(() => row.classList.remove('aviso-destacado'), 8000);
    } else if (Date.now() - started < 15000) setTimeout(look, 300);
  };
  look();
}

export function openLink(href) {
  let url;
  try { url = new URL(href, location.origin); } catch { return; }
  if (url.origin !== location.origin) return;
  const screen = url.hash.slice(1) || 'bandeja';
  if (location.hash.slice(1) !== screen) location.hash = screen;
  const ticket = url.searchParams.get('ticket');
  if (ticket) highlight(ticket);
}

// ------------------------------------------------------------------ bell and list

function bellIcon() {
  const svg = document.createElementNS(SVG, 'svg');
  svg.setAttribute('viewBox', '0 0 24 24');
  svg.setAttribute('aria-hidden', 'true');
  svg.setAttribute('fill', 'none');
  svg.setAttribute('stroke', 'currentColor');
  svg.setAttribute('stroke-width', '1.8');
  svg.setAttribute('stroke-linecap', 'round');
  svg.setAttribute('stroke-linejoin', 'round');
  for (const d of ['M6 8a6 6 0 0 1 12 0c0 7 3 9 3 9H3s3-2 3-9', 'M10.3 21a1.94 1.94 0 0 0 3.4 0']) {
    const path = document.createElementNS(SVG, 'path');
    path.setAttribute('d', d);
    svg.append(path);
  }
  return svg;
}

function showCount(count) {
  unread = Math.max(0, Number(count) || 0);
  ui.count.textContent = unread > 99 ? '99+' : String(unread);
  ui.count.hidden = unread === 0;
  ui.bell.setAttribute('aria-label', unread ? `Avisos: ${unread} sin leer` : 'Avisos');
  ui.bell.title = ui.bell.getAttribute('aria-label');
  try {
    if (unread && navigator.setAppBadge) navigator.setAppBadge(unread).catch(() => {});
    else if (navigator.clearAppBadge) navigator.clearAppBadge().catch(() => {});
  } catch { /* Badges are optional. */ }
}

async function refreshCount() {
  try { showCount((await api('/api/avisos/sin-leer')).sin_leer); } catch { /* Next round. */ }
}

function when(value) {
  const date = new Date(value);
  if (Number.isNaN(date.getTime())) return '';
  return date.toLocaleString('es-MX', {day: 'numeric', month: 'short', hour: '2-digit', minute: '2-digit'});
}

function renderList(data) {
  ui.list.replaceChildren();
  ui.empty.hidden = data.avisos.length > 0;
  for (const item of data.avisos) {
    const entry = el('li', '', item.leido ? 'leido' : 'sin-leer');
    const anchor = el('a');
    anchor.href = item.liga;
    anchor.append(el('strong', item.titulo));
    anchor.append(el('span', item.comercio ? `${item.comercio}: ${item.motivo}` : item.motivo));
    anchor.append(el('small', when(item.creado)));
    anchor.onclick = async event => {
      event.preventDefault();
      ui.dialog.close();
      openLink(item.liga);
      if (!item.leido) {
        try { showCount((await api('/api/avisos/leidos', 'POST', {ids: [item.id]})).sin_leer); } catch { /* Kept unread. */ }
      }
    };
    entry.append(anchor);
    ui.list.append(entry);
  }
  showCount(data.sin_leer);
}

async function openList() {
  ui.status.textContent = '';
  if (!ui.dialog.open) ui.dialog.showModal();
  try { renderList(await api('/api/avisos?limite=50')); } catch (error) { ui.status.textContent = error.message; }
}

function buildBell() {
  ui.bell = button('', 'avisos-campana');
  ui.bell.append(bellIcon());
  ui.count = el('span', '0', 'avisos-contador');
  ui.count.hidden = true;
  ui.bell.append(ui.count);
  const slot = document.getElementById('avisos-lugar');
  if (slot) slot.append(ui.bell);
  else {
    ui.bell.classList.add('flotante');
    const main = document.getElementById('main');
    if (main && main.parentElement === document.body) document.body.insertBefore(ui.bell, main);
    else document.body.append(ui.bell);
  }
  ui.dialog = el('dialog');
  ui.dialog.id = 'avisos-dialog';
  ui.dialog.setAttribute('aria-labelledby', 'avisos-titulo');
  const body = el('div', '', 'dialog-body');
  const heading = el('div', '', 'dialog-heading');
  const title = el('h2', 'Avisos');
  title.id = 'avisos-titulo';
  const close = button('×', 'icon-button');
  close.setAttribute('aria-label', 'Cerrar avisos');
  close.onclick = () => ui.dialog.close();
  heading.append(title, close);
  ui.status = el('p');
  ui.status.setAttribute('role', 'status');
  ui.list = el('ul', '', 'avisos-lista');
  ui.empty = el('p', 'No tienes avisos. Te escribiré aquí cuando un ticket te necesite.', 'avisos-vacio');
  const actions = el('div', '', 'dialog-actions');
  const all = button('Marcar todo como leído', 'secondary');
  all.onclick = async () => {
    try {
      await api('/api/avisos/leidos', 'POST', {todos: true});
      renderList(await api('/api/avisos?limite=50'));
    } catch (error) { ui.status.textContent = error.message; }
  };
  const settings = button('Ajustes de avisos', 'plain');
  settings.onclick = () => { ui.dialog.close(); location.hash = 'ajustes'; ui.card?.scrollIntoView({block: 'start'}); };
  actions.append(settings, all);
  body.append(heading, ui.status, ui.list, ui.empty, actions);
  ui.dialog.append(body);
  document.body.append(ui.dialog);
  ui.bell.onclick = openList;
}

// ------------------------------------------------------------------ settings cards

function card(title, description) {
  const section = el('section', '', 'card', 'settings-card');
  const heading = el('div', '', 'card-heading');
  const text = el('div');
  text.append(el('h2', title), el('p', description));
  heading.append(text);
  const body = el('div', '', 'settings-body');
  section.append(heading, body);
  return {section, heading, body};
}

function checkRow(label, checked, onChange, detail = '') {
  const row = el('label', '', 'check-row');
  const input = el('input');
  input.type = 'checkbox';
  input.checked = checked;
  const text = el('span', label);
  if (detail) text.append(el('small', detail));
  row.append(input, text);
  input.onchange = async () => {
    input.disabled = true;
    try { await onChange(input.checked); } catch (error) { input.checked = !input.checked; setStatus(error.message, true); }
    finally { input.disabled = false; }
  };
  return row;
}

function setStatus(message, failed = false) {
  if (!ui.cardStatus) return;
  ui.cardStatus.textContent = message;
  ui.cardStatus.classList.toggle('error', failed);
}

async function renderDeviceState() {
  const support = pushSupport();
  let subscribed = false;
  try { subscribed = Boolean(await currentSubscription()); } catch { subscribed = false; }
  ui.activate.hidden = subscribed;
  ui.deactivate.hidden = !subscribed;
  ui.pill.textContent = subscribed ? 'Activos aquí' : 'Sin activar aquí';
  ui.help.hidden = support.ok;
  ui.help.textContent = support.text;
}

function renderSettings(data) {
  const {tipos, canales, textos} = data;
  ui.types.replaceChildren(el('legend', 'Qué avisarme'));
  for (const [name, label] of Object.entries(textos.tipos)) {
    ui.types.append(checkRow(label, Boolean(tipos[name]), async value => {
      renderSettings(await api('/api/avisos/ajustes', 'PUT', {tipos: {[name]: value}}));
    }));
  }
  ui.channels.replaceChildren(el('legend', 'Por dónde'));
  for (const [name, label] of Object.entries(textos.canales)) {
    const detail = name === 'correo' && !data.correo_disponible
      ? 'Para usarlo, conecta en Ajustes una cuenta de correo que pueda enviar.' : '';
    ui.channels.append(checkRow(label, Boolean(canales[name]), async value => {
      renderSettings(await api('/api/avisos/ajustes', 'PUT', {canales: {[name]: value}}));
    }, detail));
  }
  const destination = el('label');
  destination.textContent = 'Enviar el correo a';
  const select = el('select');
  for (const [value, label] of [['cuenta', 'La dirección de mi cuenta de correo'], ['perfil', 'El correo de mis datos fiscales']]) {
    const option = el('option', label);
    option.value = value;
    option.selected = data.correo_destino === value;
    select.append(option);
  }
  select.disabled = !canales.correo;
  select.onchange = async () => {
    try { renderSettings(await api('/api/avisos/ajustes', 'PUT', {correo_destino: select.value})); }
    catch (error) { setStatus(error.message, true); }
  };
  destination.append(select);
  ui.channels.append(destination);
  ui.devices.replaceChildren();
  ui.devicesTitle.textContent = data.dispositivos.length
    ? `Dispositivos que reciben avisos: ${data.dispositivos.length}` : 'Ningún dispositivo recibe avisos todavía.';
  for (const device of data.dispositivos) {
    const item = el('li');
    item.append(el('span', `${device.nombre || 'Dispositivo'} · desde ${when(device.creado)}`));
    const remove = button('Quitar', 'plain');
    remove.onclick = async () => {
      try {
        await api(`/api/avisos/dispositivos/${encodeURIComponent(device.id)}`, 'DELETE');
        await loadSettings();
        await renderDeviceState();
      } catch (error) { setStatus(error.message, true); }
    };
    item.append(remove);
    ui.devices.append(item);
  }
  ui.test.disabled = data.dispositivos.length === 0;
}

async function loadSettings() {
  renderSettings(await api('/api/avisos/ajustes'));
}

function buildNoticeCard(grid) {
  const {section, heading, body} = card('Avisos', 'Te aviso cuando un ticket te necesita, aunque no tengas el panel abierto.');
  ui.card = section;
  ui.pill = el('span', 'Sin activar aquí', 'pill');
  heading.append(ui.pill);
  ui.cardStatus = el('p');
  ui.cardStatus.setAttribute('role', 'status');
  ui.help = el('p', '', 'avisos-ayuda');
  ui.help.hidden = true;
  const actions = el('div', '', 'buttons');
  ui.activate = button('Activar avisos en este dispositivo', 'primary');
  ui.deactivate = button('Quitar este dispositivo', 'secondary');
  ui.deactivate.hidden = true;
  ui.test = button('Enviar aviso de prueba', 'secondary');
  const run = action => async () => {
    for (const control of [ui.activate, ui.deactivate, ui.test]) control.disabled = true;
    try { setStatus(await action()); } catch (error) { setStatus(error.message, true); }
    finally {
      for (const control of [ui.activate, ui.deactivate]) control.disabled = false;
      await loadSettings().catch(() => {});
      await renderDeviceState();
    }
  };
  ui.activate.onclick = run(activate);
  ui.deactivate.onclick = run(deactivate);
  ui.test.onclick = run(async () => {
    const result = await api('/api/avisos/prueba', 'POST');
    return result.enviados ? `Aviso de prueba enviado a ${result.enviados} dispositivo(s).`
      : 'No se pudo entregar el aviso de prueba. Quita el dispositivo y vuelve a activarlo.';
  });
  actions.append(ui.activate, ui.deactivate, ui.test);
  ui.types = el('fieldset', '', 'avisos-grupo');
  ui.channels = el('fieldset', '', 'avisos-grupo');
  ui.devicesTitle = el('p', '', 'muted');
  ui.devices = el('ul', '', 'avisos-dispositivos');
  body.append(ui.help, actions, ui.cardStatus, ui.types, ui.channels, ui.devicesTitle, ui.devices);
  grid.append(section);
}

function buildPermissionsCard(grid) {
  const {section, body} = card('Permisos', 'Lo que me dejas hacer sin preguntarte en cada ticket. Puedes quitarlo cuando quieras.');
  const list = el('div', '', 'avisos-grupo');
  const status = el('p');
  status.setAttribute('role', 'status');
  const render = items => {
    list.replaceChildren();
    for (const item of items) {
      const since = item.activo && item.desde ? `Activo desde ${new Date(item.desde * 1000).toLocaleDateString('es-MX')}` : '';
      const row = checkRow(item.texto, item.activo, async value => {
        render(await api(`/api/permisos/${encodeURIComponent(item.nombre)}`, 'PUT', {activo: value}));
        status.textContent = value ? 'Permiso activado.' : 'Permiso retirado.';
      }, since);
      row.classList.add('avisos-permiso');
      list.append(row);
    }
  };
  body.append(list, status);
  grid.append(section);
  return async () => render(await api('/api/permisos'));
}

function buildRegistrationCard(grid) {
  const {section, body} = card('Datos para registros',
    'Los uso solo para crear tu cuenta cuando un portal de facturación lo exige y lo permites. Todos son opcionales y no se comparten con el servicio de lectura.');
  const form = el('form');
  const fields = el('div', '', 'fields');
  for (const [name, label, type, max] of FIELDS) {
    const row = el('label', label);
    const input = el('input');
    input.name = name;
    input.type = type;
    input.maxLength = max;
    input.autocomplete = 'off';
    if (type === 'tel') input.inputMode = 'tel';
    row.append(input);
    fields.append(row);
  }
  const status = el('p');
  status.setAttribute('role', 'status');
  const actions = el('div', '', 'buttons');
  actions.append(button('Guardar datos para registros', 'primary', 'submit'));
  form.append(fields, actions, status);
  const fill = data => { for (const [name] of FIELDS) form.elements[name].value = data[name] || ''; };
  form.onsubmit = async event => {
    event.preventDefault();
    const values = Object.fromEntries(FIELDS.map(([name]) => [name, form.elements[name].value]));
    try {
      fill((await api('/api/registro/datos', 'PUT', values)).datos);
      status.textContent = 'Datos para registros guardados.';
    } catch (error) { status.textContent = error.message; }
  };
  body.append(form);
  grid.append(section);
  return async () => fill((await api('/api/registro/datos')).datos);
}

// ------------------------------------------------------------------ start

export function installAvisos() {
  if (ui.bell) return;
  if (!document.querySelector('link[href="/avisos.css"]')) {
    const style = el('link');
    style.rel = 'stylesheet';
    style.href = '/avisos.css';
    document.head.append(style);
  }
  buildBell();
  const grid = document.querySelector('#screen-ajustes .settings-grid');
  const loaders = [];
  if (grid) {
    buildNoticeCard(grid);
    loaders.push(loadSettings, renderDeviceState, buildPermissionsCard(grid), buildRegistrationCard(grid));
  }
  const reload = () => { for (const load of loaders) load().catch(error => setStatus(error.message, true)); };
  reload();
  window.addEventListener('hashchange', () => { if (location.hash === '#ajustes') reload(); });
  if ('serviceWorker' in navigator) {
    navigator.serviceWorker.addEventListener('message', event => {
      const data = event.data || {};
      if (data.tipo === 'facturai-abrir' && typeof data.liga === 'string') openLink(data.liga);
      if (data.tipo === 'facturai-abrir' || data.tipo === 'facturai-aviso-nuevo') refreshCount();
    });
  }
  const params = new URLSearchParams(location.search);
  const ticket = params.get('ticket');
  if (ticket) {
    history.replaceState(null, '', location.pathname + location.hash);
    highlight(ticket);
  }
  refreshCount();
  setInterval(() => { if (document.visibilityState === 'visible') refreshCount(); }, 20000);
  document.addEventListener('visibilitychange', () => { if (document.visibilityState === 'visible') refreshCount(); });
}

if (typeof document !== 'undefined') {
  if (document.readyState === 'loading') document.addEventListener('DOMContentLoaded', installAvisos, {once: true});
  else installAvisos();
}
