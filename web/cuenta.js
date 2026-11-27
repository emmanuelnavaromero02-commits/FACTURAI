// Account page: password, sessions and, for an administrator, who may enter.
const $ = id => document.getElementById(id);
const dateFormat = new Intl.DateTimeFormat('es-MX', {dateStyle: 'medium', timeStyle: 'short'});
const when = seconds => seconds ? dateFormat.format(new Date(seconds * 1000)) : 'nunca';
let me = null;

function el(tag, text = '', className = '') {
  const node = document.createElement(tag);
  node.textContent = text;
  if (className) node.className = className;
  return node;
}

function notify(message, good = false) {
  $('aviso').textContent = message;
  $('aviso').hidden = !message;
  $('aviso').classList.toggle('good', good);
  if (message) $('aviso').scrollIntoView({block: 'nearest'});
}

async function call(path, payload) {
  const options = {credentials: 'same-origin'};
  if (payload !== undefined) {
    Object.assign(options, {method: 'POST', headers: {'content-type': 'application/json'}, body: JSON.stringify(payload)});
  }
  const response = await fetch(path, options);
  if (response.status === 401) {
    location.replace('/entrar');
    throw Error('Tu sesión terminó. Vuelve a entrar.');
  }
  let data = {};
  try { data = await response.json(); } catch { /* A refusal may come without a body. */ }
  if (!response.ok) throw Error(typeof data.detail === 'string' ? data.detail : 'No se pudo completar. Intenta de nuevo.');
  return data;
}

async function act(control, work) {
  control.disabled = true;
  notify('');
  try {
    const message = await work();
    if (message) notify(message, true);
  } catch (error) {
    notify(error.message);
  } finally {
    control.disabled = false;
  }
}

async function loadMe() {
  me = await call('/acceso/yo');
  const others = me.sesiones - 1;
  $('quien').textContent = `Entraste como ${me.usuario}` + (me.rol === 'admin' ? ' (administrador).' : '.') +
    (others > 0 ? ` Tienes ${others} ${others === 1 ? 'sesión abierta' : 'sesiones abiertas'} en otros dispositivos.` : '');
  $('form-contrasena').elements.usuario.value = me.usuario;
  $('cerrar-otras').hidden = others <= 0;
  $('admin').hidden = me.rol !== 'admin';
  if (me.rol === 'admin') await loadPeople();
}

async function loadPeople() {
  const data = await call('/acceso/admin/usuarios');
  $('usuarios').replaceChildren(...data.usuarios.map(person => {
    const row = el('li');
    const text = el('div');
    const name = el('strong', person.nombre);
    if (person.rol === 'admin') name.append(el('span', 'administrador', 'tag'));
    if (!person.activo) name.append(el('span', 'sin acceso', 'tag off'));
    text.append(name, el('small', 'Último acceso: ' + when(person.ultimo_acceso)));
    row.append(text);
    if (person.nombre !== me.usuario) {
      const button = el('button', person.activo ? 'Quitar acceso' : 'Devolver acceso', person.activo ? 'danger' : 'secondary');
      button.type = 'button';
      button.addEventListener('click', () => {
        if (person.activo && !window.confirm(`¿Quitar el acceso a ${person.nombre}? Se cerrarán sus sesiones. Sus datos se conservan.`)) return;
        act(button, async () => {
          await call('/acceso/admin/usuarios/estado', {id: person.id, activo: !person.activo});
          await loadPeople();
          return person.activo ? 'Acceso retirado.' : 'Acceso devuelto.';
        });
      });
      row.append(button);
    }
    return row;
  }));
  $('invitaciones').replaceChildren(...data.invitaciones.map(invitation => {
    const row = el('li');
    const text = el('div');
    text.append(el('strong', invitation.nombre ? 'Invitación para ' + invitation.nombre : 'Invitación sin usar'),
      el('small', 'Vence: ' + when(invitation.vence)));
    const button = el('button', 'Cancelar', 'secondary');
    button.type = 'button';
    button.addEventListener('click', () => act(button, async () => {
      await call('/acceso/admin/invitaciones/revocar', {id: invitation.id});
      await loadPeople();
      return 'Invitación cancelada.';
    }));
    row.append(text, button);
    return row;
  }));
}

for (const box of document.querySelectorAll('[data-ver]')) {
  box.addEventListener('change', () => {
    for (const name of box.dataset.ver.split(' ')) box.form.elements[name].type = box.checked ? 'text' : 'password';
  });
}

$('salir').addEventListener('click', () => act($('salir'), async () => {
  await call('/acceso/salir', {});
  location.replace('/entrar');
}));

$('cerrar-otras').addEventListener('click', () => act($('cerrar-otras'), async () => {
  const result = await call('/acceso/sesiones/cerrar-otras', {});
  await loadMe();
  return result.cerradas === 1 ? 'Se cerró 1 sesión.' : `Se cerraron ${result.cerradas} sesiones.`;
}));

$('form-contrasena').addEventListener('submit', event => {
  event.preventDefault();
  const form = event.target;
  act(form.querySelector('button[type="submit"]'), async () => {
    const data = new FormData(form);
    if (data.get('nueva') !== data.get('repetir')) throw Error('Las dos contraseñas nuevas no coinciden.');
    await call('/acceso/contrasena', {actual: data.get('actual'), nueva: data.get('nueva')});
    form.reset();
    await loadMe();
    return 'Contraseña cambiada. Tus otras sesiones se cerraron.';
  });
});

$('form-invitar').addEventListener('submit', event => {
  event.preventDefault();
  const form = event.target;
  act(form.querySelector('button[type="submit"]'), async () => {
    const data = new FormData(form);
    const made = await call('/acceso/admin/invitaciones', {usuario: data.get('usuario').trim(), horas: Number(data.get('horas'))});
    $('enlace').value = made.enlace;
    $('invitacion-lista').hidden = false;
    form.reset();
    await loadPeople();
    return 'Invitación creada.';
  });
});

$('copiar').addEventListener('click', async () => {
  try {
    await navigator.clipboard.writeText($('enlace').value);
    notify('Enlace copiado.', true);
  } catch {
    $('enlace').select();
  }
});

loadMe().catch(error => notify(error.message));
