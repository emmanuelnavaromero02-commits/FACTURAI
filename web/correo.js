// Mailbox settings (IMAP/SMTP account, Gmail send permission) and the per-ticket mail request.
const $ = id => document.getElementById(id);

const requestStates = {
  new: 'Sin solicitud enviada', sending: 'Envío en curso', sent: 'Solicitud enviada',
  uncertain: 'Envío sin confirmar', verified: 'PDF y XML cotejados',
};
const searchStates = {
  verified: 'Factura encontrada', half: 'Falta una parte', ambiguous: 'Hay más de una factura posible',
  unreadable: 'No pude leer el archivo', pending: 'Aún no llega',
};
const waitingFiles = new Set(['issued_waiting_files', 'uncertain', 'external']);

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
  let status = null;
  let saving = false;

  function say(message, failed = false) {
    feedback.textContent = message;
    feedback.classList.toggle('error', failed);
  }

  function renderSettings(data) {
    status = data;
    const account = data.imap;
    $('mail-pill').textContent = account ? (account.envio ? 'Conectado' : 'Solo lectura') : 'Por conectar';
    $('mail-pill').classList.toggle('ready', Boolean(account));
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
    try { renderSettings(await api('/api/correo')); }
    catch (error) { say(error.message, true); }
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
      'Para pedir por correo, conecta en Ajustes una cuenta con servidor de salida o permite que Gmail envíe.' :
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
