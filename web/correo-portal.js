export function installPortalMail({api, gmailState}) {
  const dialog = document.createElement('dialog');
  dialog.innerHTML = `
    <div class="dialog-body">
      <div class="section-heading"><h2>Correo de registro</h2><button type="button" data-close>Cerrar</button></div>
      <strong data-origin></strong>
      <p>Elige el correo que envió el comercio a tu dirección fiscal.</p>
      <button type="button" class="secondary" data-search>Buscar en Gmail</button>
      <label>Abrir un correo descargado (.eml)<input type="file" accept=".eml,message/rfc822" data-file></label>
      <div data-messages></div><div data-actions></div><p data-status role="status"></p>
    </div>`;
  document.body.append(dialog);
  const get = name => dialog.querySelector('[data-' + name + ']');
  const messages = get('messages');
  const actions = get('actions');
  const state = get('status');
  let current = null;
  let busy = false;

  function actionButton(label, run) {
    const button = document.createElement('button');
    button.type = 'button';
    button.className = 'secondary';
    button.textContent = label;
    button.onclick = () => perform(run);
    return button;
  }
  async function perform(run) {
    if (!current || busy) return;
    busy = true;
    state.textContent = 'Consultando…';
    try { await run({...current}); }
    catch (error) { state.textContent = error.message; }
    finally { busy = false; }
  }
  function choices(target, available) {
    if (!current || current.channel !== target.channel) return;
    messages.replaceChildren();
    actions.replaceChildren();
    state.textContent = 'Elige qué hacer en el portal que confirmaste.';
    for (const choice of available.actions) {
      const label = {activate: 'Abrir la liga de activación en este portal', code: 'Rellenar el código de alta en este portal',
        temporary: 'Rellenar la contraseña temporal en este portal'}[choice];
      actions.append(actionButton(label, async ctx => {
        await api('/api/portal-mail/apply', 'POST', {...ctx, choice});
        dialog.close();
      }));
    }
  }
  get('search').onclick = () => perform(async target => {
    messages.replaceChildren();
    actions.replaceChildren();
    const result = await api('/api/portal-mail/search', 'POST', target);
    if (!current || current.channel !== target.channel) return;
    state.textContent = result.messages.length ? 'Selecciona un mensaje.' : 'No hay mensajes recientes de ese comercio.';
    for (const message of result.messages) {
      messages.append(actionButton([message.date, message.sender, message.subject].filter(Boolean).join(' · '), async ctx => {
        choices(ctx, await api('/api/portal-mail/select', 'POST', {...ctx, choice: message.choice}));
      }));
    }
  });
  get('file').onchange = () => perform(async target => {
    const file = get('file').files[0];
    if (!file) return;
    const payload = new FormData();
    for (const [k, v] of Object.entries(target)) payload.set(k, String(v));
    payload.set('file', file);
    choices(target, await api('/api/portal-mail/file', 'POST', payload));
  });
  get('close').onclick = () => dialog.close();
  dialog.addEventListener('close', () => {
    current = null;
    get('file').value = '';
    messages.replaceChildren();
    actions.replaceChildren();
    state.textContent = '';
  });
  window.addEventListener('facturai:portal-mail', event => {
    current = event.detail;
    get('origin').textContent = current.origin;
    get('search').disabled = gmailState() !== 'connected';
    state.textContent = get('search').disabled ? 'Para buscar aquí, conecta Gmail en Ajustes. También puedes abrir un archivo .eml.' : '';
    dialog.showModal();
  });
}
