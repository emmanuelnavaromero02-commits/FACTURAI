export function installPortalAccounts({api, control, accountEmail}) {
  const open = document.createElement('button');
  open.type = 'button';
  open.className = 'secondary';
  open.textContent = 'Cuenta y datos fiscales';
  document.getElementById('browser-tools').append(open);
  const dialog = document.createElement('dialog');
  dialog.innerHTML = `
    <div class="dialog-body">
      <div class="dialog-heading"><h2>Cuenta del comercio</h2><button type="button" class="secondary" data-close>Cerrar</button></div>
      <p>Portal que recibirá los datos <strong data-origin></strong></p>
      <label>Correo para la cuenta<input type="email" maxlength="254" autocomplete="off" required data-email></label>
      <div class="buttons"><button type="button" class="primary" data-action="create"></button>
        <button type="button" class="secondary" data-action="register">Solo rellenar registro</button>
        <button type="button" class="secondary" data-action="login">Rellenar acceso guardado</button></div>
      <div><strong>Datos de facturación</strong>
        <p>RFC, nombre o razón social, código postal, régimen, uso del CFDI y correo del perfil guardado.</p>
        <button type="button" class="secondary" data-action="fiscal">Rellenar datos fiscales</button></div>
      <details><summary>Verificación o contraseña temporal</summary>
        <div class="dialog-body">
          <button type="button" class="secondary" data-mail>Revisar correo en FACTURAI</button>
          <a data-gmail target="_blank" rel="noopener noreferrer">Abrir correo de registro en Gmail</a>
          <label>Contraseña temporal del comercio<input data-temporary type="password" maxlength="512" autocomplete="off"></label>
          <button type="button" class="secondary" data-action="temporary">Rellenar contraseña temporal</button>
          <button type="button" class="secondary" data-action="change">Preparar contraseña nueva</button>
          <button type="button" class="secondary" data-action="confirm_change">Confirmar que el portal aceptó la nueva contraseña</button>
        </div>
      </details>
      <p role="status" data-status></p>
    </div>`;
  document.body.append(dialog);
  const node = key => dialog.querySelector('[data-' + key + ']');
  const email = node('email');
  const temporary = node('temporary');
  const status = node('status');
  let opened = null;
  let busy = false;
  const current = () => {
    const {channel, browser} = control();
    if (!channel || !browser.open || !browser.manual || browser.mode === 'personal') return null;
    let url;
    try { url = new URL(browser.url); } catch { return null; }
    return url.protocol === 'https:' ? {channel, revision: browser.revision, origin: url.origin} : null;
  };
  const sameTarget = target => target && opened && target.channel === opened.channel &&
    target.origin === opened.origin && target.revision === opened.revision;
  function gmailLink() {
    const link = node('gmail');
    if (!opened || !email.checkValidity()) {
      link.removeAttribute('href');
      return;
    }
    const gmail = new URL('https://mail.google.com/mail/u/');
    gmail.searchParams.set('authuser', email.value.trim());
    gmail.hash = 'search/' + encodeURIComponent('newer_than:7d from:(' + new URL(opened.origin).hostname + ')');
    link.href = gmail.href;
  }
  open.onclick = () => {
    opened = current();
    if (!opened) return;
    node('origin').textContent = opened.origin;
    dialog.querySelector('[data-action="create"]').textContent = 'Crear mi cuenta en ' + new URL(opened.origin).host;
    email.value = accountEmail();
    temporary.value = '';
    status.textContent = '';
    gmailLink();
    dialog.showModal();
  };
  email.oninput = gmailLink;
  node('close').onclick = () => dialog.close();
  dialog.addEventListener('close', () => { temporary.value = ''; opened = null; });
  node('mail').onclick = () => {
    if (!sameTarget(current())) { dialog.close(); return; }
    const detail = {...opened};
    dialog.close();
    window.dispatchEvent(new CustomEvent('facturai:portal-mail', {detail}));
  };
  for (const button of dialog.querySelectorAll('[data-action]')) {
    button.onclick = async () => {
      const action = button.dataset.action;
      if (busy || !sameTarget(current())) return;
      if (!['temporary', 'fiscal'].includes(action) && !email.reportValidity()) return;
      if (action === 'temporary' && !temporary.value) { status.textContent = 'Escribe la contraseña temporal.'; return; }
      const {origin, channel} = opened;
      busy = true;
      for (const control of dialog.querySelectorAll('button')) control.disabled = true;
      try {
        const payload = {origin, channel, action, email: email.value.trim()};
        if (action === 'temporary') payload.temporary = temporary.value;
        const result = await api('/api/browser/portal-account', 'POST', payload);
        status.textContent = result.message + (result.filled.length ? ' Campos: ' + result.filled.join(', ') + '.' : '');
      } catch (error) { status.textContent = error.message; }
      finally {
        temporary.value = '';
        busy = false;
        for (const control of dialog.querySelectorAll('button')) control.disabled = false;
      }
    };
  }
  return () => {
    const target = current();
    open.hidden = control().browser.mode === 'personal';
    open.disabled = !target;
    if (dialog.open && !sameTarget(target)) dialog.close();
  };
}
