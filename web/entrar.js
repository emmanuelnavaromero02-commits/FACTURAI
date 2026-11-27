// Sign-in and invitation pages of the entrance for several customers.
const $ = id => document.getElementById(id);

function notify(message, good = false) {
  $('aviso').textContent = message;
  $('aviso').hidden = !message;
  $('aviso').classList.toggle('good', good);
}

async function send(path, payload) {
  const response = await fetch(path, {method: 'POST', credentials: 'same-origin',
    headers: {'content-type': 'application/json'}, body: JSON.stringify(payload)});
  let data = {};
  try { data = await response.json(); } catch { /* A refusal may come without a body. */ }
  if (!response.ok) throw Error(typeof data.detail === 'string' ? data.detail : 'No se pudo completar. Intenta de nuevo.');
  return data;
}

async function submit(form, work) {
  const button = form.querySelector('button[type="submit"]');
  button.disabled = true;
  notify('');
  try {
    await work(new FormData(form));
  } catch (error) {
    notify(error.message);
    button.disabled = false;
  }
}

for (const box of document.querySelectorAll('[data-ver]')) {
  box.addEventListener('change', () => {
    for (const name of box.dataset.ver.split(' ')) box.form.elements[name].type = box.checked ? 'text' : 'password';
  });
}

// The part after «#» says which screen of the panel the person was going to.
const enterPanel = () => location.replace('/' + location.hash);

async function showLogin() {
  // The session cookie does not travel when someone arrives from a link in another site or app.
  // This page is ours, so it can ask: with a session still open, the person goes straight in.
  try {
    if ((await fetch('/acceso/yo', {credentials: 'same-origin'})).ok) return enterPanel();
  } catch { /* Without an answer the form is shown. */ }
  $('vista-entrar').hidden = false;
  const form = $('form-entrar');
  form.addEventListener('submit', event => {
    event.preventDefault();
    submit(form, async data => {
      await send('/acceso/entrar', {usuario: data.get('usuario'), contrasena: data.get('contrasena')});
      enterPanel();
    });
  });
  form.elements.usuario.focus();
}

async function showInvitation() {
  document.title = 'Crear tu cuenta · FACTURAI';
  $('vista-invitacion').hidden = false;
  // The code travels after «#», so it is never sent in the address of a request.
  const code = location.hash.slice(1);
  history.replaceState(null, '', '/invitacion');
  const form = $('form-invitacion');
  if (!code) {
    $('texto-invitacion').textContent = 'Abre el enlace completo de tu invitación.';
    $('ir-a-entrar').hidden = false;
    return;
  }
  try {
    const found = await send('/acceso/invitacion/consultar', {codigo: code});
    $('texto-invitacion').textContent = 'Elige tu contraseña para entrar a FACTURAI.';
    if (found.usuario) {
      form.elements.usuario.value = found.usuario;
      form.elements.usuario.readOnly = true;
    }
    form.hidden = false;
    (found.usuario ? form.elements.contrasena : form.elements.usuario).focus();
  } catch (error) {
    $('texto-invitacion').textContent = error.message;
    $('ir-a-entrar').hidden = false;
    return;
  }
  form.addEventListener('submit', event => {
    event.preventDefault();
    submit(form, async data => {
      if (data.get('contrasena') !== data.get('repetir')) throw Error('Las dos contraseñas no coinciden.');
      await send('/acceso/invitacion', {codigo: code, usuario: data.get('usuario'), contrasena: data.get('contrasena')});
      location.replace('/');
    });
  });
}

if (location.pathname === '/invitacion') showInvitation();
else showLogin();
