export function installViewer({api, token, showError, perform}) {
  const byId = id => document.getElementById(id);
  const image = byId('browser-image');
  const take = byId('take-control');
  let browser = {open: false, revision: 0};
  let channel = '';
  let frameUrl = '';
  let commandTail = Promise.resolve();
  let dragging = false;
  let lastMove = 0;
  let updating = false;

  function releaseFrame() {
    if (frameUrl) URL.revokeObjectURL(frameUrl);
    frameUrl = '';
    image.removeAttribute('src');
  }

  function syncControls() {
    take.disabled = !browser.open || !browser.manual || Boolean(browser.dialog);
    take.textContent = channel ? 'Devolver control' : 'Tomar control';
    image.classList.toggle('controlling', Boolean(channel));
    byId('paste-form').hidden = !channel;
    byId('browser-hint').textContent = channel ? 'Haz clic en la página y usa el teclado.' :
      browser.manual ? 'Toma el control para completar el paso.' : 'Vista en vivo de la sesión del facturador.';
  }

  function update(value, active) {
    if (value.revision !== browser.revision) {
      channel = '';
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
    byId('browser-state').textContent = value.manual ? 'Intervención' : value.open ? 'En vivo' : 'En espera';
    byId('browser-empty').hidden = value.open;
    byId('browser-loading').hidden = !value.open || Boolean(frameUrl) || Boolean(value.dialog);
    image.hidden = !value.open || !frameUrl;
    byId('native-dialog').hidden = !value.dialog;
    if (value.dialog) {
      byId('native-text').textContent = value.dialog.text;
      byId('native-yes').hidden = value.dialog.type === 'prompt' || value.dialog.type === 'beforeunload';
    }
    byId('wa-capture').disabled = active || value.label !== 'WhatsApp Web';
    syncControls();
  }

  async function frame() {
    if (!browser.open || browser.dialog || document.hidden || byId('screen-navegador').hidden || updating) return;
    updating = true;
    const revision = browser.revision;
    try {
      const response = await fetch(`/api/browser/frame?revision=${revision}`, {headers: {'x-app-token': token()}});
      if (response.status === 200 && revision === browser.revision) {
        const blob = await response.blob();
        if (revision !== browser.revision) return;
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

  function send(command) {
    const lease = channel;
    if (!lease) return;
    commandTail = commandTail.then(async () => {
      if (channel === lease) await api('/api/browser/input', 'POST', {...command, channel: lease});
    }).catch(error => {
      channel = '';
      dragging = false;
      syncControls();
      showError(error.message);
    });
    return commandTail;
  }

  function coords(event) {
    const box = image.getBoundingClientRect();
    return {x: Math.min(1, Math.max(0, (event.clientX - box.left) / box.width)),
            y: Math.min(1, Math.max(0, (event.clientY - box.top) / box.height))};
  }

  image.addEventListener('pointerdown', event => {
    if (!channel || event.button !== 0) return;
    event.preventDefault();
    image.focus();
    image.setPointerCapture(event.pointerId);
    dragging = true;
    send({kind: 'down', ...coords(event)});
  });
  image.addEventListener('pointermove', event => {
    if (!channel || !dragging || performance.now() - lastMove < 75) return;
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
  take.addEventListener('click', () => perform(async () => {
    if (channel) {
      const previous = channel;
      channel = '';
      await api('/api/browser/release', 'POST', {channel: previous});
    } else {
      const result = await api('/api/browser/control', 'POST', {revision: browser.revision});
      channel = result.channel;
      image.focus();
    }
    syncControls();
  }));
  for (const [id, accept] of [['native-yes', true], ['native-no', false]]) {
    byId(id).addEventListener('click', () => perform(() => api('/api/browser/dialog', 'POST', {dialog_id: browser.dialog.id, accept})));
  }
  setInterval(frame, 360);
  return {update};
}
