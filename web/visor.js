import {installPortalAccounts} from './cuentas-portal.js';
import {installNavigation} from './navegacion.js';

export function installViewer({api, token, showError, perform, accountEmail = () => '', captchaId = () => null}) {
  const byId = id => document.getElementById(id);
  const image = byId('browser-image');
  const take = byId('take-control');
  let browser = {open: false, revision: 0};
  let channel = '';
  let challengeOwner = '';
  let frameUrl = '';
  let frameHash = '';
  let frameWidth = 0;
  let unchangedFrames = 0;
  let commandTail = Promise.resolve();
  let batch = null;
  let dragging = false;
  let lastMove = 0;
  let updating = false;
  let jobActive = false;
  let acquiring = false;
  let uploadId = '';
  const syncPortalAccount = installPortalAccounts({api, accountEmail, control: () => ({channel, browser})});
  const navigation = installNavigation({api, perform, current: () => ({browser, channel, active: jobActive})});

  function releaseFrame() {
    if (frameUrl) URL.revokeObjectURL(frameUrl);
    frameUrl = '';
    frameHash = '';
    image.removeAttribute('src');
  }

  function syncControls() {
    syncPortalAccount();
    navigation.update();
    take.disabled = !browser.open || !browser.manual || acquiring || Boolean(browser.dialog);
    take.textContent = channel ? 'Devolver control' : 'Tomar control';
    image.classList.toggle('controlling', Boolean(channel));
    byId('paste-form').hidden = !channel;
    byId('type-form').hidden = !channel;
    byId('browser-tools').hidden = !channel;
    const currentUpload = channel ? (browser.upload?.id || '') : '';
    if (uploadId !== currentUpload) byId('portal-file').value = '';
    uploadId = currentUpload;
    byId('portal-upload').hidden = !uploadId;
    byId('upload-origin').textContent = uploadId ? browser.upload.origin : '';
    byId('upload-send').disabled = !uploadId || !byId('portal-file').files.length;
    byId('browser-hint').textContent = channel ? 'Haz clic en la página y usa el teclado.' :
      browser.mode === 'personal' ? (browser.url ? 'Haz clic en la página para usarla.' : '') :
      browser.manual ? 'Toma el control para completar el paso.' : browser.open ? 'Vista en vivo del trámite.' : '';
  }

  function update(value, active) {
    jobActive = active;
    if (value.revision !== browser.revision || value.mode !== browser.mode || value.job_id !== browser.job_id) {
      channel = '';
      challengeOwner = '';
      dragging = false;
      releaseFrame();
    }
    browser = value;
    byId('browser-address').textContent = 'Sin página abierta';
    if (value.url) {
      const url = new URL(value.url);
      byId('browser-address').textContent = url.origin === 'null' ? value.label : url.origin + url.pathname;
    } else if (value.open) {
      byId('browser-address').textContent = value.label;
    }
    if (value.mode === 'personal') byId('browser-address').textContent = 'Navegación libre';
    const label = value.mode === 'personal' ? 'Perfil personal' : value.manual ? 'Intervención' : value.open ? 'En vivo' : 'En espera';
    byId('browser-state').textContent = value.open && value.engine ? label + ' · ' + value.engine : label;
    const blankTab = value.mode === 'personal' && !value.url;
    byId('browser-empty').hidden = value.open && !blankTab;
    byId('browser-loading').hidden = !value.open || blankTab || Boolean(frameUrl) || Boolean(value.dialog);
    image.hidden = !value.open || blankTab || !frameUrl;
    byId('native-dialog').hidden = !value.dialog;
    if (value.dialog) {
      byId('native-text').textContent = value.dialog.text;
      byId('native-yes').hidden = value.dialog.type === 'prompt' || value.dialog.type === 'beforeunload';
    }
    byId('wa-capture').hidden = value.mode !== 'whatsapp' && value.label !== 'WhatsApp Web';
    byId('wa-capture').disabled = active;
    syncControls();
  }

  // Ask for frames as wide as the viewer is drawn (in steps), so phones and slow tunnels get smaller images.
  function wantedWidth() {
    const box = byId('browser-image').parentElement?.getBoundingClientRect();
    const pixels = Math.ceil((box?.width || 0) * Math.min(window.devicePixelRatio || 1, 2));
    if (!pixels || pixels >= 1200) return 0;
    return Math.max(320, Math.ceil(pixels / 160) * 160);
  }

  async function frame() {
    if (!browser.open || (browser.mode === 'personal' && !browser.url) || browser.dialog ||
        document.hidden || byId('screen-navegador').hidden || updating) return;
    updating = true;
    const revision = browser.revision;
    const width = wantedWidth();
    if (width !== frameWidth) { frameWidth = width; frameHash = ''; }
    try {
      const query = new URLSearchParams({revision: String(revision)});
      if (frameHash && frameUrl) query.set('have', frameHash);
      if (width) query.set('w', String(width));
      const response = await fetch(`/api/browser/frame?${query}`, {headers: {'x-app-token': token()}});
      if (response.status === 204) unchangedFrames += 1;
      if (response.status === 200 && revision === browser.revision) {
        const blob = await response.blob();
        if (revision !== browser.revision || !browser.open) return;
        unchangedFrames = 0;
        frameHash = response.headers.get('x-frame-hash') || '';
        const previous = frameUrl;
        frameUrl = URL.createObjectURL(blob);
        image.src = frameUrl;
        image.hidden = false;
        byId('browser-loading').hidden = true;
        if (previous) URL.revokeObjectURL(previous);
      }
    } catch { /* The next frame reflects a temporary connection interruption. */ }
    finally { updating = false; }
  }

  function frameLoop() {
    frame().finally(() => {
      // A still page is checked less often unless the person is operating it.
      setTimeout(frameLoop, channel || unchangedFrames < 8 ? 360 : 900);
    });
  }

  function post(current) {
    const events = current.events;
    return api('/api/browser/input', 'POST', events.length === 1 ? {...events[0], channel: current.lease} :
      {channel: current.lease, events});
  }

  function flush() {
    const current = batch;
    if (!current) return commandTail;
    batch = null;
    clearTimeout(current.timer);
    commandTail = commandTail.then(async () => {
      if (channel === current.lease) await post(current);
    }).catch(error => {
      if (channel === current.lease) {
        channel = '';
        dragging = false;
        syncControls();
      }
      showError(error.message);
    }).then(current.resolve);
    return commandTail;
  }

  // Pointer and keyboard events are grouped in small ordered batches of the same lease.
  function send(command) {
    const lease = channel;
    if (!lease) return Promise.resolve();
    if (batch && batch.lease !== lease) flush();
    if (!batch) {
      let resolve;
      const done = new Promise(value => { resolve = value; });
      batch = {lease, events: [], done, resolve, timer: setTimeout(flush, 35)};
    }
    const last = batch.events.at(-1);
    if (command.kind === 'text' && last?.kind === 'text' && last.value.length + command.value.length <= 2000) {
      last.value += command.value;
    } else if (command.kind === 'move' && last?.kind === 'move') {
      last.x = command.x;
      last.y = command.y;
    } else batch.events.push({...command});
    const done = batch.done;
    if (batch.events.length >= 30) flush();
    return done;
  }

  // Map a pointer to the page even when the frame is letterboxed by object-fit: contain.
  function coords(event) {
    const box = image.getBoundingClientRect();
    const naturalWidth = image.naturalWidth || box.width;
    const naturalHeight = image.naturalHeight || box.height;
    const scale = Math.min(box.width / naturalWidth, box.height / naturalHeight) || 1;
    const width = naturalWidth * scale;
    const height = naturalHeight * scale;
    const left = box.left + (box.width - width) / 2;
    const top = box.top + (box.height - height) / 2;
    return {x: Math.min(1, Math.max(0, (event.clientX - left) / width)),
            y: Math.min(1, Math.max(0, (event.clientY - top) / height))};
  }

  function releasePersonal() {
    if (browser.mode !== 'personal' || !channel) return;
    const previous = channel;
    channel = '';
    dragging = false;
    syncControls();
    fetch('/api/browser/release', {method: 'POST', keepalive: true,
      headers: {'x-app-token': token(), 'content-type': 'application/json'},
      body: JSON.stringify({channel: previous})}).catch(() => {});
  }
  window.addEventListener('pagehide', () => {
    if (!channel || !challengeOwner) return releasePersonal();
    const previous = channel;
    channel = '';
    challengeOwner = '';
    fetch('/api/browser/release', {method:'POST', keepalive:true,
      headers:{'x-app-token':token(), 'content-type':'application/json'},
      body:JSON.stringify({channel:previous, abandon:true})}).catch(() => {});
  });
  setInterval(async () => {
    if (!channel || !challengeOwner || document.hidden || byId('screen-navegador').hidden) return;
    const held = channel;
    try {
      const response = await fetch('/api/browser/control/alive', {method:'POST',
        headers:{'x-app-token':token(), 'content-type':'application/json'},
        body:JSON.stringify({channel:held, job_id:challengeOwner})});
      if (response.status === 422 && channel === held) {
        channel = '';
        challengeOwner = '';
        syncControls();
      }
    } catch { /* A disconnected device can reacquire the ticket after its inactive lease expires. */ }
  }, 25000);
  window.addEventListener('hashchange', () => { if (location.hash !== '#navegador') releasePersonal(); });
  document.addEventListener('visibilitychange', () => { if (document.hidden) releasePersonal(); });

  function matchesTicket(key) {
    return browser.mode === 'job' && browser.job_id === key && browser.open && browser.manual;
  }
  async function acquireControl(jobId) {
    if (acquiring) return;
    if (jobId && !matchesTicket(jobId)) throw Error('Espera al visor de este ticket.');
    if (jobId && channel) return;
    acquiring = true;
    const revision = browser.revision;
    const mode = browser.mode;
    try {
      const result = await api('/api/browser/control', 'POST', jobId ? {revision, job_id: jobId} : {revision});
      if (browser.revision !== revision || browser.mode !== mode || (jobId && !matchesTicket(jobId)) ||
          document.hidden || byId('screen-navegador').hidden) {
        await api('/api/browser/release', 'POST', {channel: result.channel});
        return;
      }
      channel = result.channel;
      challengeOwner = jobId || '';
      image.focus();
    } finally {
      acquiring = false;
      syncControls();
    }
  }
  async function releaseControl() {
    const previous = channel;
    await flush();
    if (!previous || previous !== channel) return;
    await api('/api/browser/release', 'POST', {channel: previous});
    if (channel === previous) { channel = ''; challengeOwner = ''; }
    syncControls();
  }
  image.addEventListener('click', event => {
    if (channel || acquiring || browser.mode !== 'personal' || !browser.manual) return;
    const point = coords(event);
    perform(async () => {
      await acquireControl();
      if (channel) {
        await send({kind: 'down', ...point});
        await send({kind: 'up', ...point});
      }
    });
  });
  image.addEventListener('pointerdown', event => {
    if (!channel || event.button !== 0) return;
    event.preventDefault();
    image.focus();
    image.setPointerCapture(event.pointerId);
    dragging = true;
    send({kind: 'down', ...coords(event)});
  });
  image.addEventListener('pointermove', event => {
    if (!channel || !dragging || performance.now() - lastMove < 50) return;
    lastMove = performance.now();
    send({kind: 'move', ...coords(event)});
  });
  function lift(event) {
    if (!dragging) return;
    dragging = false;
    send({kind: 'up', ...coords(event)});
  }
  image.addEventListener('pointerup', lift);
  image.addEventListener('pointercancel', lift);
  image.addEventListener('wheel', event => {
    if (!channel) return;
    event.preventDefault();
    send({kind: 'wheel', delta: Math.round(Math.max(-1200, Math.min(1200, event.deltaY))), ...coords(event)});
  }, {passive: false});
  image.addEventListener('keydown', event => {
    if (!channel || event.isComposing) return;
    if ((event.ctrlKey || event.metaKey) && ['a','z','y'].includes(event.key.toLowerCase())) {
      event.preventDefault();
      send({kind: 'key', value: 'ControlOrMeta+' + event.key.toLowerCase()});
    } else if (!(event.ctrlKey || event.metaKey || event.altKey)) {
      const special = /^(Backspace|Delete|Tab|Enter|Escape|Home|End|PageUp|PageDown|ArrowUp|ArrowDown|ArrowLeft|ArrowRight)$/;
      if (special.test(event.key) || [...event.key].length === 1) {
        event.preventDefault();
        send({kind: special.test(event.key) ? 'key' : 'text', value: event.key});
      }
    }
  });
  image.addEventListener('paste', event => {
    if (!channel) return;
    event.preventDefault();
    send({kind: 'text', value: event.clipboardData.getData('text').slice(0, 2000)});
  });
  byId('paste-form').addEventListener('submit', async event => {
    event.preventDefault();
    const field = byId('remote-text');
    await send({kind: 'text', value: field.value});
    field.value = '';
    image.focus();
  });
  // Phones do not open their keyboard for an image: this field forwards what is typed to the portal.
  const keys = byId('remote-keys');
  function forwardTyped() {
    if (!channel || !keys.value) return;
    const value = keys.value.slice(0, 2000);
    keys.value = '';
    send({kind: 'text', value});
  }
  keys.addEventListener('input', event => { if (!event.isComposing) forwardTyped(); });
  keys.addEventListener('compositionend', forwardTyped);
  keys.addEventListener('beforeinput', event => {
    if (!channel || keys.value) return;
    if (event.inputType === 'deleteContentBackward') {
      event.preventDefault();
      send({kind: 'key', value: 'Backspace'});
    }
  });
  keys.addEventListener('keydown', event => {
    if (!channel || event.isComposing || keys.value) return;
    if (event.key === 'Backspace' && !event.repeat) {
      event.preventDefault();
      send({kind: 'key', value: 'Backspace'});
    }
  });
  byId('remote-backspace').addEventListener('click', () => { forwardTyped(); send({kind: 'key', value: 'Backspace'}); keys.focus(); });
  byId('type-form').addEventListener('submit', event => {
    event.preventDefault();
    forwardTyped();
    send({kind: 'key', value: 'Enter'});
    keys.focus();
  });
  take.addEventListener('click', () => perform(async () => {
    if (channel) await releaseControl();
    else {
      const key = captchaId();
      await acquireControl(key && matchesTicket(key) ? key : undefined);
    }
    syncControls();
  }));
  byId('page-png').onclick = () => perform(async () => {
    const blob = await api('/api/browser/screenshot', 'POST', {channel});
    const url = URL.createObjectURL(blob);
    const save = document.createElement('a');
    save.href = url;
    save.download = 'captura-portal.png';
    save.click();
    setTimeout(() => URL.revokeObjectURL(url), 10_000);
  });
  byId('page-text').onclick = () => perform(async () => {
    const {text} = await api('/api/browser/text', 'POST', {channel});
    byId('page-content').value = text;
    byId('page-content').setSelectionRange(0, 0);
    byId('find-query').value = '';
    byId('find-status').textContent = text ? '' : 'La página no contiene texto visible.';
    byId('page-dialog').showModal();
  });
  byId('find-text').onsubmit = event => {
    event.preventDefault();
    const field = byId('page-content');
    const query = byId('find-query').value.toLocaleLowerCase();
    const haystack = field.value.toLocaleLowerCase();
    let index = haystack.indexOf(query, field.selectionEnd);
    if (index < 0) index = haystack.indexOf(query);
    byId('find-status').textContent = index < 0 ? 'Sin coincidencias.' : 'Coincidencia seleccionada.';
    if (index >= 0) {
      field.focus();
      field.setSelectionRange(index, index + query.length);
    }
  };
  byId('portal-file').onchange = syncControls;
  for (const [id, attach] of [['upload-send', true], ['upload-dismiss', false]]) {
    byId(id).onclick = () => perform(async () => {
      const form = new FormData();
      form.set('channel', channel);
      form.set('request_id', uploadId);
      if (attach) {
        const file = byId('portal-file').files[0];
        if (!file) return;
        form.set('file', file);
      }
      await api('/api/browser/upload', 'POST', form);
      return attach ? 'Archivo adjuntado en el portal.' : '';
    });
  }
  for (const [id, accept] of [['native-yes', true], ['native-no', false]]) {
    byId(id).addEventListener('click', () => perform(() => api('/api/browser/dialog', 'POST', {dialog_id: browser.dialog.id, accept})));
  }
  frameLoop();
  return {update, hasControl: () => Boolean(channel), openPersonal: navigation.openPersonal,
    returnToJob: navigation.returnToJob, gmail: navigation.gmail, acquireControl, releaseControl,
    returnToCaptcha: id => api('/api/browser/return', 'POST', {channel, job_id: id})};
}
