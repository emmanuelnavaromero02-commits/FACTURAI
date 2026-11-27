// "Inteligencia artificial" settings: active provider, API keys, model per tier, prices and usage.
const $ = id => document.getElementById(id);

const KEY_PROVIDERS = new Set(['openai', 'anthropic']);
const TIER_LABELS = {rapido: 'Rápido', normal: 'Normal', dificil: 'Difícil'};
const TIER_HELP = {rapido: 'pasos sencillos', normal: 'la mayoría de las tareas', dificil: 'lecturas y portales difíciles'};
const STATE_LABELS = {ready: 'Lista', disconnected: 'Sin conectar', limit: 'En pausa por límite',
  reconnect: 'Revisa la conexión', identity_only: 'Falta autorizar'};
const TASK_LABELS = {leer_ticket: 'Leer tickets', buscar_portal: 'Buscar portales', paso_portal: 'Pasos en portales',
  investigar: 'Investigar', constancia: 'Leer constancias', chat: 'Conversación', whatsapp: 'WhatsApp',
  prueba: 'Pruebas', otra: 'Otras'};
const PRICE_PROVIDERS = [['openai', 'OpenAI'], ['anthropic', 'Claude'], ['chatgpt', 'Plan de ChatGPT']];
const NO_DATA = 'sin dato';

function el(tag, text = '', className = '') {
  const node = document.createElement(tag);
  if (text) node.textContent = text;
  if (className) node.className = className;
  return node;
}

function control(tag, label, attributes = {}) {
  const wrap = el('label', label);
  const input = el(tag);
  for (const [name, value] of Object.entries(attributes)) {
    if (value === true) input.setAttribute(name, '');
    else if (value !== false && value !== undefined) input.setAttribute(name, value);
  }
  wrap.append(input);
  return {wrap, input};
}

function button(text, className, type = 'button') {
  const node = el('button', text, className);
  node.type = type;
  return node;
}

function integer(value) {
  return typeof value === 'number' ? value.toLocaleString('es-MX') : NO_DATA;
}

export function money(value, currency) {
  if (value === null || value === undefined) return NO_DATA;
  if (typeof value !== 'number') return String(value);  // «sin precio» is shown as it is.
  const digits = value !== 0 && Math.abs(value) < 0.01 ? 4 : 2;
  try {
    return new Intl.NumberFormat('es-MX', {style: 'currency', currency, minimumFractionDigits: digits,
      maximumFractionDigits: digits}).format(value);
  } catch {
    return value.toFixed(digits) + ' ' + currency;
  }
}

export function installAI({api, changed}) {
  const feedback = $('ia-feedback');
  let status = null;
  let saving = false;

  function say(message, failed = false) {
    feedback.textContent = message;
    feedback.classList.toggle('error', failed);
  }

  async function run(work, done) {
    if (saving) return;
    saving = true;
    $('ia-card').setAttribute('aria-busy', 'true');
    try {
      const message = await work();
      if (typeof message === 'string') say(message);
      if (done) changed?.();
    } catch (error) {
      say(error.message, true);
    } finally {
      saving = false;
      $('ia-card').removeAttribute('aria-busy');
    }
  }

  function renderChoice(data) {
    const options = $('ia-options');
    options.replaceChildren(...data.proveedores.map(provider => {
      const row = el('label', '', 'check-row ia-option');
      const input = el('input');
      input.type = 'radio';
      input.name = 'ia-proveedor';
      input.value = provider.id;
      input.checked = provider.activo;
      const usable = !KEY_PROVIDERS.has(provider.id) || provider.llave?.configurada;
      input.disabled = !usable;
      const text = el('span', provider.nombre);
      text.append(el('small', usable ? STATE_LABELS[provider.estado] || provider.estado : 'Guarda primero su llave'));
      row.append(input, text);
      input.onchange = () => run(async () => {
        render(await api('/api/ia/proveedor', 'PUT', {proveedor: provider.id}));
        return 'Ahora uso ' + provider.nombre + '.';
      }, true);
      return row;
    }));
  }

  function keyForm(provider) {
    const box = el('section', '', 'ia-provider');
    box.setAttribute('aria-label', provider.nombre);
    const heading = el('div', '', 'ia-provider-heading');
    heading.append(el('h3', provider.nombre), el('span', STATE_LABELS[provider.estado] || provider.estado, 'pill'));
    box.append(heading);
    const key = provider.llave || {};
    if (key.configurada) box.append(el('p', 'Llave guardada que termina en ····' + key.ultimos + '.', 'muted'));
    if (!key.editable) {
      box.append(el('p', 'Esta llave la fija el servidor. No se puede cambiar desde aquí.', 'muted ia-locked'));
    } else {
      const form = el('form', '', 'ia-key-form');
      const {wrap, input} = control('input', key.configurada ? 'Llave nueva' : 'Llave de API', {
        type: 'password', autocomplete: 'off', autocapitalize: 'none', spellcheck: 'false', maxlength: '400',
        minlength: '20', required: true, name: 'llave'});
      const actions = el('div', '', 'buttons');
      actions.append(button(key.configurada ? 'Cambiar llave' : 'Guardar y probar', 'primary', 'submit'));
      if (key.configurada) {
        const remove = button('Quitar llave', 'plain');
        remove.onclick = () => {
          if (!window.confirm('¿Quitar la llave de ' + provider.nombre + '? Si era la IA en uso, volveré al plan de ChatGPT.')) return;
          run(async () => {
            render(await api('/api/ia/llave/' + provider.id, 'DELETE'));
            return 'Llave quitada.';
          }, true);
        };
        actions.append(remove);
      }
      form.append(wrap, actions);
      form.onsubmit = event => {
        event.preventDefault();
        if (!form.reportValidity()) return;
        let value = input.value.trim();
        // The key leaves the page with this request; the field is emptied before waiting.
        input.value = '';
        run(async () => {
          say('Probando la llave…');
          try {
            render(await api('/api/ia/llave/' + provider.id, 'PUT', {llave: value}));
          } finally {
            value = '';
          }
          const saved = status.proveedores.find(item => item.id === provider.id)?.llave;
          return 'Llave guardada y probada. Termina en ····' + (saved?.ultimos || '') + '.';
        }, true);
      };
      box.append(form);
    }
    if (key.configurada) box.append(modelsBox(provider));
    return box;
  }

  function modelsBox(provider) {
    const details = el('details', '', 'ia-details');
    const summary = el('summary', 'Modelo por nivel');
    const chosen = provider.modelos || {};
    const current = el('p', Object.keys(TIER_LABELS).map(tier => TIER_LABELS[tier] + ': ' + (chosen[tier] || NO_DATA)).join(' · '), 'muted');
    const form = el('form', '', 'ia-models');
    details.append(summary, current, form);
    details.addEventListener('toggle', () => {
      if (!details.open || form.dataset.loaded) return;
      form.replaceChildren(el('p', 'Leyendo los modelos disponibles…', 'muted'));
      api('/api/ia/modelos/' + provider.id).then(data => {
        form.dataset.loaded = '1';
        loaded = data.elegidos || {};
        const fields = el('div', '', 'fields');
        for (const tier of Object.keys(TIER_LABELS)) {
          const {wrap, input} = control('select', TIER_LABELS[tier] + ' · ' + TIER_HELP[tier], {name: tier});
          for (const model of data.modelos) input.add(new Option(model.name || model.id, model.id));
          if (loaded[tier] && !data.modelos.some(model => model.id === loaded[tier])) {
            input.add(new Option(loaded[tier] + ' (actual)', loaded[tier]));
          }
          input.value = loaded[tier] || '';
          fields.append(wrap);
        }
        const actions = el('div', '', 'buttons');
        actions.append(button('Guardar modelos', 'primary', 'submit'));
        form.replaceChildren(fields, actions);
      }).catch(error => form.replaceChildren(el('p', error.message, 'muted')));
    });
    let loaded = {};
    form.onsubmit = event => {
      event.preventDefault();
      // Only the tiers the person changed: a current model missing from the list is kept as it is.
      const values = Object.fromEntries(Object.keys(TIER_LABELS)
        .filter(tier => form.elements[tier].value !== (loaded[tier] || ''))
        .map(tier => [tier, form.elements[tier].value]));
      if (!Object.keys(values).length) { say('No cambiaste ningún modelo.'); return; }
      run(async () => {
        render(await api('/api/ia/modelos/' + provider.id, 'PUT', values));
        return 'Modelos guardados.';
      }, true);
    };
    return details;
  }

  function render(data) {
    status = data;
    const active = data.proveedores.find(provider => provider.activo);
    $('ia-pill').textContent = active ? active.nombre : 'Sin elegir';
    renderChoice(data);
    $('ia-keys').replaceChildren(...data.proveedores.filter(provider => KEY_PROVIDERS.has(provider.id)).map(keyForm));
  }

  // ---------------------------------------------------------------- prices
  const prices = $('ia-prices-form');

  function priceRow(provider = 'openai', model = '', price = {}) {
    const row = el('div', '', 'ia-price-row');
    row.setAttribute('role', 'group');
    const who = control('select', 'IA', {name: 'proveedor'});
    for (const [value, label] of PRICE_PROVIDERS) who.input.add(new Option(label, value));
    who.input.value = provider;
    const name = control('input', 'Modelo', {name: 'modelo', required: true, maxlength: '120', pattern: '[A-Za-z0-9_.:\\/\\-]+',
      autocomplete: 'off', spellcheck: 'false'});
    name.input.value = model;
    const input = control('input', 'Entrada', {name: 'entrada', type: 'number', min: '0', step: 'any', required: true,
      inputmode: 'decimal'});
    input.input.value = price.entrada ?? '';
    const output = control('input', 'Salida', {name: 'salida', type: 'number', min: '0', step: 'any', required: true,
      inputmode: 'decimal'});
    output.input.value = price.salida ?? '';
    const remove = button('Quitar', 'plain');
    remove.setAttribute('aria-label', 'Quitar este precio');
    remove.onclick = () => row.remove();
    row.append(who.wrap, name.wrap, input.wrap, output.wrap, remove);
    return row;
  }

  function renderPrices(data) {
    prices.elements.moneda.value = data.moneda || 'USD';
    const rows = [];
    for (const [provider, models] of Object.entries(data.precios || {})) {
      for (const [model, price] of Object.entries(models)) rows.push(priceRow(provider, model, price));
    }
    $('ia-price-rows').replaceChildren(...rows);
    $('ia-price-empty').hidden = rows.length > 0;
  }

  $('ia-price-add').onclick = () => {
    const row = priceRow();
    $('ia-price-rows').append(row);
    $('ia-price-empty').hidden = true;
    row.querySelector('input').focus();
  };
  prices.onsubmit = event => {
    event.preventDefault();
    if (!prices.reportValidity()) return;
    const table = {moneda: prices.elements.moneda.value.trim().toUpperCase(), precios: {}};
    for (const row of $('ia-price-rows').querySelectorAll('.ia-price-row')) {
      const value = name => row.querySelector(`[name="${name}"]`).value;
      (table.precios[value('proveedor')] ||= {})[value('modelo').trim()] = {entrada: Number(value('entrada')), salida: Number(value('salida'))};
    }
    run(async () => {
      renderPrices(await api('/api/ia/precios', 'PUT', table));
      await loadUsage();
      return 'Precios guardados.';
    });
  };

  // ---------------------------------------------------------------- usage
  async function loadUsage() {
    const period = $('ia-usage-period').value;
    const target = $('ia-usage');
    try {
      const data = await api('/api/ia/uso?periodo=' + encodeURIComponent(period));
      if (!data.grupos.length) {
        target.replaceChildren(el('p', 'Sin llamadas a la IA en este periodo.', 'muted'));
        return;
      }
      const table = el('table', '', 'number-table');
      const head = el('tr');
      for (const [label, numeric] of [['Tarea'], ['IA'], ['Modelo'], ['Llamadas', 1], ['Fallidas', 1], ['Tokens de entrada', 1],
        ['Tokens de salida', 1], ['Costo', 1]]) {
        const cell = el('th', label);
        cell.scope = 'col';
        if (numeric) cell.className = 'num';
        head.append(cell);
      }
      const thead = el('thead');
      thead.append(head);
      const body = el('tbody');
      const names = Object.fromEntries(PRICE_PROVIDERS);
      for (const group of data.grupos) {
        const row = el('tr');
        row.append(el('td', TASK_LABELS[group.tarea] || group.tarea), el('td', names[group.proveedor] || group.proveedor),
          el('td', group.modelo || NO_DATA));
        for (const value of [integer(group.llamadas), integer(group.fallidas), integer(group.tokens_entrada),
          integer(group.tokens_salida), money(group.costo, data.moneda)]) row.append(el('td', value, 'num'));
        body.append(row);
      }
      table.append(thead, body);
      const wrap = el('div', '', 'table-wrap');
      wrap.append(table);
      target.replaceChildren(wrap);
    } catch (error) {
      target.replaceChildren(el('p', error.message, 'muted'));
    }
  }
  $('ia-usage-period').onchange = loadUsage;

  async function refresh() {
    try {
      render(await api('/api/ia'));
      renderPrices(await api('/api/ia/precios'));
      await loadUsage();
    } catch (error) {
      say(error.message, true);
    }
  }

  return {refresh, status: () => status};
}
