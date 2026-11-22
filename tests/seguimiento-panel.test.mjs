import assert from 'node:assert/strict';
import {readFileSync} from 'node:fs';
import {test} from 'node:test';
import {installSAT, installWhatsApp} from '../web/seguimiento.js';

const html = readFileSync(new URL('../web/index.html', import.meta.url), 'utf8');
const tick = () => new Promise(resolve => setImmediate(resolve));
const row = {id: 'prueba', status: 'external', issue_attempted: false,
  receipt: {merchant: 'Comercio de prueba', amount: '160', ticket_number: '123'}};
const currency = () => '$160.00';

function mountPanel(t) {
  const nodes = new Map();
  for (const [, id] of html.matchAll(/\bid="([^"]+)"/g)) {
    const node = new EventTarget();
    node.close = () => node.dispatchEvent(new Event('close'));
    node.showModal = () => {};
    nodes.set(id, node);
  }
  const byId = id => {
    assert.ok(nodes.has(id), 'Control presente en index.html: ' + id);
    return nodes.get(id);
  };
  const original = globalThis.document;
  globalThis.document = {getElementById: byId};
  t.after(() => { if (original === undefined) delete globalThis.document; else globalThis.document = original; });
  return byId;
}

test('SAT abre el resultado local y solo consulta remotamente al pulsar el botón', async t => {
  const byId = mountPanel(t);
  const calls = [];
  const open = installSAT({currency, api: async (path, method) => {
    calls.push([path, method]);
    if (method === 'GET') return {status: 'vigente', checked_at: '2026-11-22T12:00:00Z', message: 'Vigente'};
    return {status: 'no_disponible', checked_at: '2026-11-22T12:01:00Z', message: 'Servicio no disponible',
      last_confirmed: {status: 'vigente', checked_at: '2026-11-22T12:00:00Z'}};
  }});
  open({...row, uuid: 'uuid-de-prueba'});
  await tick();
  assert.deepEqual(calls, [['/api/jobs/prueba/sat', 'GET']]);
  assert.equal(byId('sat-state').textContent, 'Vigente');
  await byId('sat-query').onclick();
  assert.deepEqual(calls[1], ['/api/jobs/prueba/sat', 'POST']);
  assert.equal(byId('sat-state').textContent, 'Servicio no disponible');
  assert.equal(byId('sat-last').hidden, false);
  assert.match(byId('sat-last').textContent, /Vigente/);
});

test('WhatsApp requiere destinatario y consentimiento, y un envío incierto permite recuperación sin reenvío', async t => {
  const byId = mountPanel(t);
  const form = byId('wa-job-form');
  form.elements = {phone: {value: ''}, consent: {checked: false}, watch: {checked: false}, csf: {files: []}, voucher: {files: []}};
  const fields = form.elements;
  form.reset = () => {fields.phone.value = ''; fields.consent.checked = false;};
  form.reportValidity = () => Boolean(fields.phone.value && fields.consent.checked);
  const calls = [];
  let saved = {status: 'new'};
  const open = installWhatsApp({currency, profile: () => ({email: 'prueba@example.test'}),
    api: async (path, method = 'GET', data) => {
      calls.push([path, method]);
      if (path.endsWith('/send')) {
        assert.equal(data.get('phone'), '+525500000000');
        assert.equal(data.get('consent'), 'true');
        assert.equal(data.get('watch'), 'false');
        assert.equal(data.has('csf'), false);
        saved = {status: 'uncertain', reference: 'solicitud-de-prueba', phone: '+525500000000'};
        throw Error('Se perdió la conexión');
      }
      if (path.endsWith('/recover')) saved = {status: 'verified'};
      return saved;
    }});
  open(row);
  await tick();
  const submit = () => form.onsubmit({preventDefault() {}});
  fields.phone.value = '+525500000000';
  await submit();
  assert.deepEqual(calls, [['/api/jobs/prueba/whatsapp', 'GET']]);
  fields.consent.checked = true;
  // Node's FormData has no HTMLFormElement overload. Its mutation semantics are sufficient here.
  const NativeFormData = globalThis.FormData;
  globalThis.FormData = class extends NativeFormData { constructor() {super();} };
  t.after(() => { globalThis.FormData = NativeFormData; });
  await submit();
  assert.equal(form.hidden, true);
  assert.equal(byId('wa-job-recover').hidden, false);
  assert.match(byId('wa-job-hint').textContent, /Comprueba la conversación/);
  await submit();
  await byId('wa-job-recover').onclick();
  assert.equal(byId('wa-job-state').textContent, 'PDF y XML cotejados');
  assert.deepEqual(calls.map(([p,m]) => [p.split('/').at(-1),m]),
    [['whatsapp','GET'], ['send','POST'], ['whatsapp','GET'], ['recover','POST']]);
});

test('WhatsApp permite pausar y reactivar el seguimiento y refleja la recuperación automática mediante lectura local', async t => {
  const byId = mountPanel(t);
  byId('wa-job-form').reset = () => {};
  t.mock.timers.enable({apis: ['setTimeout']});
  const calls = [];
  let saved = {status: 'sent', watch: true, next_check: '2026-11-22T12:10:00Z'};
  const open = installWhatsApp({currency, profile: () => ({email: 'prueba@example.test'}),
    api: async (path, method = 'GET', data) => {
      calls.push([path, method]);
      if (method === 'PUT') saved = {...saved, watch: data.watch};
      return {...saved};
    }});
  open(row);
  await tick();
  const watch = byId('wa-job-auto');
  assert.equal(watch.checked, true);
  assert.match(byId('wa-job-tracking-detail').textContent, /Próxima revisión/);
  watch.checked = false;
  await watch.onchange();
  t.mock.timers.tick(10000);
  await tick();
  assert.equal(calls.length, 2);
  assert.equal(byId('wa-job-recover').hidden, false);
  watch.checked = true;
  await watch.onchange();
  saved = {status: 'verified', watch: false};
  t.mock.timers.tick(5000);
  await tick();
  assert.equal(byId('wa-job-state').textContent, 'PDF y XML cotejados');
  assert.equal(byId('wa-job-tracking').hidden, true);
  t.mock.timers.tick(10000);
  await tick();
  assert.deepEqual(calls, [['/api/jobs/prueba/whatsapp','GET'], ['/api/jobs/prueba/whatsapp/watch','PUT'],
    ['/api/jobs/prueba/whatsapp/watch','PUT'], ['/api/jobs/prueba/whatsapp','GET']]);
});
