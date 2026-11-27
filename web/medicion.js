// "Medición" screen: headline figures per period, a table per portal and one ticket's figures.
import {money} from './ia.js';

const $ = id => document.getElementById(id);
const NO_DATA = 'sin dato';

function el(tag, text = '', className = '') {
  const node = document.createElement(tag);
  if (text !== '') node.textContent = text;
  if (className) node.className = className;
  return node;
}

const known = value => typeof value === 'number' && Number.isFinite(value);
const count = value => known(value) ? value.toLocaleString('es-MX') : NO_DATA;
const percent = value => known(value) ? value.toLocaleString('es-MX', {maximumFractionDigits: 1}) + ' %' : NO_DATA;
const decimal = value => known(value) ? value.toLocaleString('es-MX', {maximumFractionDigits: 2}) : NO_DATA;

export function duration(seconds) {
  if (!known(seconds)) return NO_DATA;
  if (seconds < 10) return seconds.toLocaleString('es-MX', {maximumFractionDigits: 1}) + ' s';
  if (seconds < 60) return Math.round(seconds) + ' s';
  const minutes = Math.floor(seconds / 60);
  const rest = Math.round(seconds % 60);
  if (minutes < 60) return rest ? `${minutes} min ${rest} s` : `${minutes} min`;
  const hours = Math.floor(minutes / 60);
  return `${hours} h ${minutes % 60} min`;
}

const milliseconds = value => known(value) ? duration(value / 1000) : NO_DATA;

function tile(label, value, detail = '') {
  const node = el('div', '', 'kpi');
  node.append(el('span', label, 'kpi-label'), el('strong', value, value === NO_DATA ? 'kpi-value kpi-empty' : 'kpi-value'));
  if (detail) node.append(el('span', detail, 'kpi-detail'));
  return node;
}

export function installMeasurement({api, catalog}) {
  let period = '7d';
  let request = 0;

  function portalName(id) {
    if (id === 'portal_ticket') return 'Portal del ticket';
    if (id === 'unknown') return 'Sin portal identificado';
    return catalog()?.[id]?.name || id;
  }

  function renderSummary(data) {
    const invoiced = data.facturados;
    $('medicion-resumen').replaceChildren(
      tile('Tickets subidos', count(data.subidos)),
      tile('Facturados', count(invoiced), known(data.subidos) && data.subidos ? 'de ' + count(data.subidos) + ' subidos' : ''),
      tile('Sin intervención', percent(data.pct_sin_intervencion),
        known(data.pct_sin_intervencion) ? count(data.sin_intervencion) + ' de ' + count(invoiced) + ' facturados' : 'aún no hay facturados'),
      tile('Lectura del ticket', milliseconds(data.mediana_lectura_ms), 'mediana'),
      tile('Tiempo en el portal', duration(data.mediana_portal_s), 'mediana, sin pausas'),
      tile('Llamadas a la IA por ticket', decimal(data.promedio_llamadas_ia), 'promedio'),
      tile('Costo por ticket', money(data.costo_por_ticket, data.moneda || 'USD'),
        data.costo_por_ticket === 'sin precio' ? 'falta el precio de algún modelo' : 'promedio estimado'),
    );
  }

  function renderPortals(data) {
    const rows = data.portales || [];
    if (!rows.length) {
      $('medicion-portales').replaceChildren(el('p', 'Ningún ticket llegó a un portal en este periodo.', 'muted medicion-empty'));
      return;
    }
    const table = el('table', '', 'number-table');
    const caption = el('caption', 'Intentos y éxitos por portal');
    caption.className = 'visually-hidden';
    const head = el('tr');
    for (const [label, numeric] of [['Portal'], ['Tickets', 1], ['Intentos', 1], ['Éxitos', 1], ['% de éxito', 1],
      ['% repetido de memoria', 1]]) {
      const cell = el('th', label, numeric ? 'num' : '');
      cell.scope = 'col';
      head.append(cell);
    }
    const thead = el('thead');
    thead.append(head);
    const body = el('tbody');
    for (const row of rows) {
      const line = el('tr');
      const name = el('th', portalName(row.portal), 'portal-name');
      name.scope = 'row';
      line.append(name);
      for (const value of [count(row.tickets), count(row.intentos), count(row.exitos), percent(row.pct_exito),
        percent(row.pct_memoria)]) line.append(el('td', value, value === NO_DATA ? 'num kpi-empty' : 'num'));
      body.append(line);
    }
    table.append(caption, thead, body);
    const wrap = el('div', '', 'table-wrap');
    wrap.append(table);
    $('medicion-portales').replaceChildren(wrap);
  }

  async function load() {
    const current = ++request;
    const status = $('medicion-status');
    status.textContent = 'Calculando…';
    try {
      const query = '?periodo=' + encodeURIComponent(period);
      const [summary, portals] = await Promise.all([api('/api/medicion/resumen' + query), api('/api/medicion/portales' + query)]);
      if (current !== request) return;
      renderSummary(summary);
      renderPortals(portals);
      status.textContent = '';
    } catch (error) {
      if (current === request) status.textContent = error.message;
    }
  }

  for (const choice of document.querySelectorAll('[data-periodo]')) {
    choice.onclick = () => {
      period = choice.dataset.periodo;
      for (const other of document.querySelectorAll('[data-periodo]')) other.setAttribute('aria-pressed', String(other === choice));
      load();
    };
  }

  async function showTicket(row) {
    const dialog = $('medicion-dialog');
    $('medicion-ticket').textContent = row.receipt.merchant || 'Ticket';
    $('medicion-detalle').replaceChildren(el('p', 'Calculando…', 'muted'));
    dialog.showModal();
    try {
      const data = await api('/api/medicion/tickets/' + encodeURIComponent(row.id));
      const list = el('dl', '', 'medicion-lista');
      const yes = value => value ? 'Sí' : 'No';
      const source = {sesion: 'medido en la sesión', eventos: 'calculado con la actividad'};
      for (const [label, value] of [
        ['Portal', portalName(data.portal)], ['Facturado', yes(data.facturado)],
        ['Sin intervención', data.facturado ? yes(data.sin_intervencion) : 'aún no se factura'],
        ['Lectura del ticket', milliseconds(data.lectura_ms) + (data.lecturas > 1 ? ` (${data.lecturas} lecturas)` : '')],
        ['Tiempo en el portal', duration(data.portal_s) + (source[data.portal_fuente] ? ' · ' + source[data.portal_fuente] : '')],
        ['Intentos en el portal', count(data.intentos_portal)],
        ['Pausas por ti', count(data.pausas) + (known(data.pausas_s) && data.pausas ? ' · ' + duration(data.pausas_s) : '')],
        ['Llamadas a la IA', count(data.llamadas_ia)], ['Acciones en el portal', count(data.acciones)],
        ['Repetidas de memoria', count(data.acciones_memoria)], ['Correcciones tuyas', count(data.correcciones)],
        ['Costo estimado', money(data.costo, data.moneda || 'USD')]]) {
        const line = el('div');
        line.append(el('dt', label), el('dd', value, value === NO_DATA ? 'kpi-empty' : ''));
        list.append(line);
      }
      $('medicion-detalle').replaceChildren(list);
    } catch (error) {
      $('medicion-detalle').replaceChildren(el('p', error.message, 'muted'));
    }
  }

  return {show: load, showTicket};
}
