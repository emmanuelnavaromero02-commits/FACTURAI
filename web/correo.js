// Mailbox settings (IMAP/SMTP account, Outlook / Hotmail with Microsoft's sign-in, Gmail send permission)
// and the per-ticket mail request.
const $ = id => document.getElementById(id);
const MICROSOFT = 'https://login.microsoftonline.com';

const requestStates = {
  new: 'Sin solicitud enviada', sending: 'Envío en curso', sent: 'Solicitud enviada',
  uncertain: 'Envío sin confirmar', verified: 'PDF y XML cotejados',
};
const searchStates = {
  verified: 'Factura encontrada', half: 'Falta una parte', ambiguous: 'Hay más de una factura posible',
  unreadable: 'No pude leer el archivo', pending: 'Aún no llega',
};
const waitingFiles = new Set(['issued_waiting_files', 'uncertain', 'external']);
const outlookPills = {sin_aplicacion: 'Sin configurar', lista: 'Lista para conectar', conectado: 'Conectado',
  reconectar: 'Requiere volver a conectar', no_disponible: 'No disponible'};
const outlookIntro = {
  sin_aplicacion: 'Para correos de Outlook.com, Hotmail, Live y Microsoft 365. Microsoft ya no acepta contraseña de ' +
    'aplicación en estos buzones: se entra con tu cuenta de Microsoft. Antes hay que registrar una aplicación, una sola vez.',
  lista: 'Entra con tu cuenta de Microsoft para que FACTURAI lea ahí los correos de los portales y tus facturas. ' +
    'Tu contraseña no pasa por FACTURAI.',
  no_disponible: 'Este servicio todavía no ofrece conectar correos de Outlook y Hotmail.',
};
const imapSwitch = 'Antes de conectar, en tu correo de Outlook entra a Configuración → Correo → Reenvío e IMAP y activa ' +
  '«Permitir que los dispositivos y las aplicaciones usen IMAP». Viene apagado.';

function el(tag, text = '', className = '') {
  const node = document.createElement(tag);
  if (text) node.textContent = text;
  if (className) node.className = className;
  return node;
}

function actionButton(text, className) {
  const node = el('button', text, className);
  node.type = 'button';
  return node;
}

// Microsoft returns to the panel with its answer after «#». Anything else there is the name of a screen.
function microsoftAnswer() {
  const values = new URLSearchParams(location.hash.slice(1));
  const state = values.get('state') || '';
  if (!state.startsWith('outlook-') || !(values.has('code') || values.has('error'))) return null;
  return {estado: state, codigo: values.get('code') || '', error: values.get('error') || ''};
}

// The «Outlook / Hotmail» option is built here: it goes first in the mail card, above the form for other providers.
function outlookOption(form) {
  const root = el('section', '', 'mail-option');
  root.id = 'outlook-box';
  root.setAttribute('aria-labelledby', 'outlook-title');
  const heading = el('div', '', 'mail-option-heading');
  const title = el('h3', 'Outlook / Hotmail');
  title.id = 'outlook-title';
  const pill = el('span', '', 'pill');
  pill.id = 'outlook-pill';
  heading.append(title, pill);
  const summary = el('p', '', 'muted');
  summary.id = 'outlook-summary';
  const note = el('p', imapSwitch, 'muted');
  note.id = 'outlook-imap';

  const application = el('form', '', 'mail-option-form');
  application.id = 'outlook-app-form';
  const label = el('label', 'Identificador de tu aplicación de Microsoft');
  const input = el('input');
  for (const [name, value] of Object.entries({name: 'identificador', required: '', maxlength: '64', autocomplete: 'off',
    autocapitalize: 'none', spellcheck: 'false', placeholder: '00000000-0000-0000-0000-000000000000'})) {
    input.setAttribute(name, value);
  }
  label.append(input);
  const save = el('button', 'Guardar identificador', 'secondary');
  save.type = 'submit';
  const saveRow = el('div', '', 'buttons');
  saveRow.append(save);
  application.append(label, saveRow);

  const help = el('details', '', 'help-note');
  help.id = 'outlook-help';
  help.append(el('summary', '¿Cómo obtengo el identificador?'));
  const steps = el('ol', '', 'mail-steps');
  const address = el('code');
  address.id = 'outlook-redirect';
  const fourth = el('li', 'Abre «Autenticación» → «Agregar URI de redirección» → «Aplicaciones móviles y de escritorio», ' +
    'escribe esta dirección y pulsa «Configurar»: ');
  fourth.append(address);
  steps.append(
    el('li', 'Entra a entra.microsoft.com con tu cuenta de Microsoft. Si te pide una cuenta de Azure, crearla es gratis.'),
    el('li', 'Abre «Entra ID» → «Registros de aplicaciones» → «Nuevo registro» y ponle un nombre, por ejemplo FACTURAI.'),
    el('li', 'En «Tipos de cuenta admitidos» elige «Cualquier inquilino de Entra ID + cuentas personales de Microsoft» ' +
      'y pulsa «Registrar».'),
    fourth,
    el('li', 'En «Información general» copia el «Identificador de aplicación (cliente)», pégalo aquí y guárdalo.'));
  help.append(steps);

  const actions = el('div', '', 'buttons');
  const connect = actionButton('Conectar Outlook', 'primary');
  connect.id = 'outlook-connect';
  const send = actionButton('Permitir que también envíe', 'secondary');
  send.id = 'outlook-send';
  const test = actionButton('Probar Outlook', 'secondary');
  test.id = 'outlook-test';
  const disconnect = actionButton('Desconectar Outlook', 'plain');
  disconnect.id = 'outlook-disconnect';
  actions.append(connect, send, test, disconnect);
  const feedback = el('p');
  feedback.id = 'outlook-feedback';
  feedback.setAttribute('role', 'status');
  root.append(heading, summary, application, help, note, actions, feedback);
  // Nothing is offered until the state arrives and says what applies here.
  for (const node of [application, help, note, connect, send, test, disconnect]) node.hidden = true;

  const other = el('h3', 'Otro proveedor de correo', 'mail-option-title');
  other.id = 'mail-other-title';
  const otherNote = el('p', 'Con servidor IMAP y contraseña de aplicación. Los correos de Outlook, Hotmail y Microsoft 365 ' +
    'se conectan arriba.', 'muted');
  form.before(root, other, otherNote, $('mail-summary'));
  return {root, pill, summary, note, application, input, help, address, connect, send, test, disconnect, feedback};
}

function dateLabel(value) {
  if (!value) return '';
  const date = new Date(value);
  return Number.isNaN(date.getTime()) ? '' : date.toLocaleString('es-MX');
}

function ticketLabel(row, currency) {
  return [row.receipt.merchant, row.receipt.ticket_number && 'Folio ' + row.receipt.ticket_number,
    currency(row.receipt.amount)].filter(Boolean).join(' · ');
}

export function installMail({api, currency, profile, showSettings, changed}) {
  const form = $('mail-form');
  const feedback = $('mail-feedback');
  // Read before anything else looks at the address, and taken out of it at once.
  const returned = microsoftAnswer();
  if (returned) history.replaceState(null, '', location.pathname + location.search + '#ajustes');
  const outlook = outlookOption(form);
  let status = null;
  let microsoft = {};   // What /api/correo/outlook says about the mailbox of Microsoft's sign-in.
  let saving = false;
  let outlookBusy = false;
  let awaitingMicrosoft = false;

  function say(message, failed = false) {
    feedback.textContent = message;
    feedback.classList.toggle('error', failed);
  }

  function sayOutlook(message, failed = false) {
    outlook.feedback.textContent = message;
    outlook.feedback.classList.toggle('error', failed);
  }

  function renderOutlook(link) {
    const state = Object.hasOwn(outlookPills, link.estado) ? link.estado : 'no_disponible';
    const application = link.aplicacion || {};
    const linked = state === 'conectado';
    outlook.pill.textContent = outlookPills[state];
    outlook.pill.classList.toggle('ready', linked);
    outlook.summary.textContent = linked
      ? 'Conectado como ' + link.cuenta + '. ' + (link.envio ? 'Lee tu buzón y puede pedir facturas por correo.'
        : 'Lee tu buzón; no envía correos.') + ' El permiso se renueva solo.'
      : state === 'reconectar' ? 'Microsoft dejó de renovar el permiso de ' + (link.cuenta || 'tu correo') +
        ': se retiró o caducó. Pulsa «Volver a conectar Outlook» para darlo otra vez.'
      : outlookIntro[state];
    outlook.note.hidden = linked || state === 'no_disponible';
    // The identifier is typed only where the owner registers the application: never behind the entrance.
    outlook.application.hidden = !application.editable || linked || state === 'reconectar';
    const saved = application.identificador || '';
    if (outlook.input.dataset.saved !== saved) {
      // Only what the server holds replaces what is in the box; a text being typed is left alone.
      outlook.input.dataset.saved = saved;
      outlook.input.value = saved;
    }
    outlook.help.hidden = outlook.application.hidden;
    outlook.address.textContent = link.redireccion || '';
    outlook.connect.hidden = !(state === 'lista' || state === 'reconectar');
    outlook.connect.textContent = state === 'reconectar' ? 'Volver a conectar Outlook' : 'Conectar Outlook';
    outlook.send.hidden = !linked || link.envio;
    outlook.test.hidden = !linked;
    outlook.disconnect.hidden = !(linked || state === 'reconectar');
    for (const control of [outlook.connect, outlook.send, outlook.test, outlook.disconnect]) control.disabled = outlookBusy;
  }

  // The card's own pill counts both mailboxes: the one with a password and the one of Microsoft.
  function renderPill() {
    const account = status?.imap;
    const connected = Boolean(account) || microsoft.estado === 'conectado';
    const sends = Boolean(account?.envio) || Boolean(microsoft.envio);
    $('mail-pill').textContent = connected ? (sends ? 'Conectado' : 'Solo lectura') : 'Por conectar';
    $('mail-pill').classList.toggle('ready', connected);
  }

  function renderMicrosoft(link) {
    microsoft = link || {};
    renderOutlook(microsoft);
    renderPill();
  }

  function renderSettings(data) {
    status = data;
    const account = data.imap;
    renderPill();
    form.hidden = Boolean(account);
    $('mail-actions').hidden = !account;
    $('mail-summary').textContent = account ? [
      'Cuenta ' + (account.address || account.user) + '.',
      'Entrada ' + account.imap_host + ':' + account.imap_port + '.',
      account.envio ? 'Salida ' + account.smtp_host + ':' + account.smtp_port + ': puede pedir facturas por correo.'
        : 'Sin servidor de salida: solo busca facturas, no envía.',
    ].join(' ') : 'Conecta tu buzón para encontrar las facturas que te lleguen y, con servidor de salida, pedirlas por correo.';
    const gmail = data.gmail || {};
    $('gmail-send').hidden = !gmail.conectado || gmail.envio;
    $('gmail-send-status').hidden = !gmail.conectado;
    $('gmail-send-status').textContent = !gmail.conectado ? '' : gmail.envio
      ? 'Gmail también puede enviar tus solicitudes de factura.'
      : 'Gmail solo lee tu buzón. Si quieres, permite que también envíe las solicitudes de factura.';
  }

  async function refresh() {
    try {
      // Without an answer about Outlook the rest of the card still shows; that option says it is not available.
      const [mail, link] = await Promise.all([api('/api/correo'), api('/api/correo/outlook').catch(() => ({}))]);
      renderMicrosoft(link);
      renderSettings(mail);
    } catch (error) { say(error.message, true); }
  }

  function busy(value) {
    saving = value;
    $('mail-fields').disabled = value;
    for (const id of ['mail-test', 'mail-disconnect']) $(id).disabled = value;
  }

  form.onsubmit = async event => {
    event.preventDefault();
    if (saving || !form.reportValidity()) return;
    const fields = form.elements;
    const payload = {
      imap_host: fields.imap_host.value.trim(), imap_port: Number(fields.imap_port.value || 993),
      smtp_host: fields.smtp_host.value.trim(), smtp_port: Number(fields.smtp_port.value || 465),
      smtp_tls: fields.smtp_tls.value, user: fields.user.value.trim(), password: fields.password.value,
      address: fields.address.value.trim(),
    };
    // The password leaves the page with this request and is never shown again.
    fields.password.value = '';
    busy(true);
    say('Probando la cuenta…');
    try {
      renderSettings(await api('/api/correo/cuenta', 'POST', payload));
      say('Correo conectado. La cuenta respondió bien.');
      changed?.();
    } catch (error) {
      say(error.message, true);
    } finally {
      payload.password = '';
      busy(false);
    }
  };
  $('mail-test').onclick = async () => {
    busy(true);
    say('Probando la conexión…');
    try {
      renderSettings(await api('/api/correo/probar', 'POST'));
      say('La conexión con tu correo funciona.');
    } catch (error) { say(error.message, true); }
    finally { busy(false); }
  };
  $('mail-disconnect').onclick = async () => {
    if (!window.confirm('¿Desconectar tu correo de FACTURAI? Para volver a usarlo tendrás que escribir otra vez la contraseña de aplicación.')) return;
    busy(true);
    try {
      renderSettings(await api('/api/correo/cuenta', 'DELETE'));
      form.reset();
      say('Correo desconectado.');
      changed?.();
    } catch (error) { say(error.message, true); }
    finally { busy(false); }
  };

  // ---------------------------------------------------------------- Outlook / Hotmail
  async function outlookAction(work) {
    if (outlookBusy) return;
    outlookBusy = true;
    renderOutlook(microsoft);
    try {
      await work();
    } catch (error) {
      sayOutlook(error.message, true);
      // What failed may have changed the connection (a permission taken away, for instance).
      await refresh();
    } finally {
      outlookBusy = false;
      renderOutlook(microsoft);
    }
  }

  outlook.application.onsubmit = event => {
    event.preventDefault();
    if (!outlook.application.reportValidity()) return;
    outlookAction(async () => {
      renderMicrosoft(await api('/api/correo/outlook/aplicacion', 'POST', {identificador: outlook.input.value.trim()}));
      sayOutlook('Identificador guardado. Ahora pulsa Conectar Outlook.');
    });
  };

  // Microsoft's page opens in another tab; when the person comes back, the state is read again.
  async function authorize(send) {
    const tab = window.open('about:blank', '_blank');
    if (tab) tab.opener = null;
    try {
      const {url} = await api('/api/correo/outlook/conectar', 'POST', {enviar: send});
      const destination = new URL(url);
      if (destination.origin !== MICROSOFT || destination.username || destination.password) {
        throw Error('FACTURAI no recibió una dirección de Microsoft válida.');
      }
      awaitingMicrosoft = true;
      if (tab && !tab.closed) {
        tab.location.replace(destination.href);
        sayOutlook('Autoriza en la pestaña de Microsoft y vuelve aquí.');
      } else {
        const link = el('a', 'Continuar en Microsoft', 'inline-link');
        link.href = destination.href;
        link.target = '_blank';
        link.rel = 'noopener noreferrer';
        outlook.feedback.classList.remove('error');
        outlook.feedback.replaceChildren('El navegador bloqueó la pestaña de Microsoft. ', link);
      }
    } catch (error) {
      if (tab && !tab.closed) tab.close();
      throw error;
    }
  }
  outlook.connect.onclick = () => outlookAction(() => authorize(false));
  outlook.send.onclick = () => outlookAction(() => authorize(true));
  outlook.test.onclick = () => outlookAction(async () => {
    sayOutlook('Probando la conexión…');
    renderMicrosoft(await api('/api/correo/outlook/probar', 'POST'));
    sayOutlook('La conexión con tu correo de Outlook funciona.');
  });
  outlook.disconnect.onclick = () => {
    if (!window.confirm('¿Desconectar tu correo de Outlook de FACTURAI? Para volver a usarlo tendrás que autorizarlo otra vez en Microsoft.')) return;
    outlookAction(async () => {
      renderMicrosoft(await api('/api/correo/outlook', 'DELETE'));
      sayOutlook('Outlook desconectado.');
      await refresh();
      changed?.();
    });
  };

  async function backFromMicrosoft() {
    if (!awaitingMicrosoft || document.hidden) return;
    const before = microsoft;
    await refresh();
    const now = microsoft;
    if (now.estado === 'conectado' && (before.estado !== 'conectado' || before.envio !== now.envio || before.cuenta !== now.cuenta)) {
      awaitingMicrosoft = false;
      sayOutlook('Outlook conectado como ' + now.cuenta + '.');
      changed?.();
    }
  }
  document.addEventListener('visibilitychange', backFromMicrosoft);
  window.addEventListener('focus', backFromMicrosoft);

  // This page is the one Microsoft returned to: hand its answer over and show how it went.
  if (returned) {
    outlookAction(async () => {
      sayOutlook('Terminando la conexión con Microsoft…');
      try {
        const data = await api('/api/correo/outlook/terminar', 'POST', returned);
        renderMicrosoft(data);
        sayOutlook(data.aviso || 'Outlook conectado como ' + data.cuenta +
          '. Si llegaste aquí desde otra pestaña de FACTURAI, ya puedes cerrar esta.');
        await refresh();
        changed?.();
      } finally {
        outlook.root.scrollIntoView({block: 'center'});
      }
    });
  }

  // Gmail may also send, only when the person chooses it. Same flow as connecting Gmail.
  $('gmail-send').onclick = async () => {
    const tab = window.open('about:blank', '_blank');
    if (tab) tab.opener = null;
    try {
      const {url} = await api('/api/gmail/connect', 'POST', {send: true});
      const destination = new URL(url);
      if (destination.origin !== 'https://accounts.google.com' || destination.username || destination.password) {
        throw Error('FACTURAI no recibió una dirección de autorización de Google válida.');
      }
      if (tab && !tab.closed) {
        tab.location.replace(destination.href);
        $('gmail-send-status').textContent = 'Autoriza el envío en la pestaña de Google y vuelve aquí.';
      } else {
        const link = document.createElement('a');
        link.className = 'inline-link';
        link.textContent = 'Continuar en Google';
        link.href = destination.href;
        link.target = '_blank';
        link.rel = 'noopener noreferrer';
        $('gmail-send-status').replaceChildren('El navegador bloqueó la pestaña de Google. ', link);
      }
    } catch (error) {
      if (tab && !tab.closed) tab.close();
      $('gmail-send-status').textContent = error.message;
    }
  };

  // ---------------------------------------------------------------- per ticket
  const dialog = $('mail-job-dialog');
  const request = $('mail-job-form');
  let session = null;

  function canAsk(row, data) {
    return data?.status === 'new' && ['ready', 'external'].includes(row.status) && !row.issue_attempted &&
      !row.historical && !String(row.receipt.source || '').startsWith('Ejemplo:');
  }

  function renderRequest(active) {
    const {row, data} = active;
    const fiscal = profile();
    const open = canAsk(row, data);
    $('mail-job-state').textContent = active.title || (data ? requestStates[data.status] || 'Estado desconocido' : '');
    $('mail-job-message').textContent = active.feedback || data?.message || '';
    const record = data && data.status !== 'new' ? [data.to && 'Enviada a ' + data.to, data.reference,
      dateLabel(data.updated)].filter(Boolean).join(' · ') : '';
    $('mail-job-record').hidden = !record;
    $('mail-job-record').textContent = record;
    request.hidden = !open || !data.puede_enviar || !fiscal;
    $('mail-job-fields').disabled = active.busy;
    const consent = request.elements.consent;
    consent.required = !data?.permiso;
    $('mail-job-consent-row').hidden = Boolean(data?.permiso);
    $('mail-job-permission').hidden = !data?.permiso;
    $('mail-job-profile').textContent = fiscal ? 'Pediré el PDF y el XML a nombre de tus datos fiscales, para ' + fiscal.email + '.' : '';
    const hint = !data ? '' : open && !data.puede_enviar ?
      'Para pedir por correo, conecta en Ajustes una cuenta con servidor de salida o permite que Gmail u Outlook envíen.' :
      open && !fiscal ? 'Guarda primero tus datos fiscales.' :
      data.status === 'new' && !open ? 'Este ticket ya no admite una solicitud nueva por correo.' :
      data.status === 'uncertain' ? 'No repetiré la solicitud. Revisa tus enviados; la respuesta se buscará sola.' : '';
    $('mail-job-hint').hidden = !hint;
    $('mail-job-hint').textContent = hint;
    $('mail-job-settings').hidden = !(open && (!data.puede_enviar || !fiscal));
    $('mail-job-search').hidden = !data || !(['sent', 'uncertain'].includes(data.status) || waitingFiles.has(row.status) && row.issue_attempted);
    for (const id of ['mail-job-search', 'mail-job-refresh', 'mail-job-settings']) $(id).disabled = active.busy;
  }

  async function act(kind, payload) {
    const active = session;
    if (!active || active.busy) return;
    const base = '/api/jobs/' + encodeURIComponent(active.row.id) + '/correo';
    active.busy = true;
    active.title = '';
    active.feedback = {read: 'Leyendo el expediente…', ask: 'Enviando la solicitud…', search: 'Buscando en tu correo…'}[kind];
    renderRequest(active);
    try {
      if (kind === 'search') {
        const result = await api(base + '/buscar', 'POST');
        active.title = searchStates[result.status] || 'Búsqueda terminada';
        active.feedback = result.message || '';
        if (session === active) active.data = await api(base).catch(() => active.data);
        changed?.();
        return;
      }
      active.data = kind === 'ask' ? await api(base + '/pedir', 'POST', payload) : await api(base);
      active.feedback = '';
      if (kind === 'ask') changed?.();
    } catch (error) {
      active.feedback = error.message;
      if (kind === 'ask') {
        // The server may have recorded the request even if its answer was lost.
        try { active.data = await api(base); } catch { active.feedback += ' Actualiza el estado antes de continuar.'; }
      }
    } finally {
      active.busy = false;
      if (session === active) renderRequest(active);
    }
  }

  request.onsubmit = event => {
    event.preventDefault();
    if (request.hidden || !session || session.busy || !request.reportValidity()) return;
    act('ask', {to: request.elements.to.value.trim(), consent: request.elements.consent.checked});
  };
  $('mail-job-search').onclick = () => act('search');
  $('mail-job-refresh').onclick = () => act('read');
  $('mail-job-settings').onclick = () => { dialog.close(); showSettings(); };
  dialog.addEventListener('close', () => { session = null; request.reset(); });

  function openRequest(row) {
    session = {row, data: null, feedback: '', title: '', busy: false};
    $('mail-job-ticket').textContent = ticketLabel(row, currency);
    request.reset();
    request.elements.to.value = row.receipt.merchant_email || '';
    $('mail-job-to-hint').hidden = !row.receipt.merchant_email;
    renderRequest(session);
    dialog.showModal();
    act('read');
  }

  return {refresh, openRequest, status: () => status};
}
