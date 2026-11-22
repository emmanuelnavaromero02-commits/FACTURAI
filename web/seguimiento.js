const $ = id => document.getElementById(id);
const statesSAT = {
  sin_consultar: 'Sin consultar',
  vigente: 'Vigente',
  cancelado: 'Cancelado',
  no_encontrado: 'CFDI no encontrado',
  no_disponible: 'Servicio no disponible',
};

function dateLabel(value) {
  if (!value) return '';
  const date = new Date(value);
  return Number.isNaN(date.getTime()) ? '' : date.toLocaleString('es-MX');
}

function ticketLabel(row, currency) {
  return [row.receipt.merchant, row.receipt.ticket_number && 'Folio ' + row.receipt.ticket_number,
    currency(row.receipt.amount)].filter(Boolean).join(' · ');
}

export function installSAT({api, currency}) {
  const dialog = $('sat-dialog');
  const query = $('sat-query');
  let job = null;

  function display(data) {
    $('sat-state').textContent = statesSAT[data.status] || 'Estado desconocido';
    $('sat-detail').textContent = data.message || '';
    $('sat-date').hidden = !data.checked_at;
    $('sat-date').textContent = data.checked_at ? 'Consulta del ' + dateLabel(data.checked_at) : '';
    const previous = data.status === 'no_disponible' && data.last_confirmed;
    $('sat-last').hidden = !previous;
    $('sat-last').textContent = previous ?
      'Última respuesta confirmada · ' + (statesSAT[previous.status] || previous.status) + ' · ' + dateLabel(previous.checked_at) : '';
    query.textContent = data.checked_at ? 'Volver a consultar al SAT' : 'Consultar al SAT';
  }

  async function read(method) {
    const active = job;
    if (!active) return;
    query.disabled = true;
    $('sat-detail').textContent = method === 'POST' ? 'Consultando al SAT…' : 'Leyendo la consulta guardada…';
    try {
      const data = await api('/api/jobs/' + encodeURIComponent(active.id) + '/sat', method);
      if (job === active) display(data);
    } catch (error) {
      if (job === active) $('sat-detail').textContent = error.message;
    } finally {
      if (job === active) query.disabled = false;
    }
  }
  query.onclick = () => read('POST');
  dialog.addEventListener('close', () => { job = null; });
  return row => {
    job = {id: row.id};
    $('sat-ticket').textContent = ticketLabel(row, currency);
    $('sat-uuid').textContent = row.uuid ? 'UUID ' + row.uuid : '';
    display({status: 'sin_consultar'});
    dialog.showModal();
    read('GET');
  };
}

const statesWA = {
  new: 'Sin solicitud enviada',
  sending: 'Envío en curso',
  sent: 'Solicitud enviada',
  uncertain: 'Envío sin confirmar',
  verified: 'PDF y XML cotejados',
};

export function installWhatsApp({api, currency, profile, showBrowser, showSettings}) {
  const dialog = $('wa-job-dialog');
  const form = $('wa-job-form');
  const refresh = $('wa-job-refresh');
  const recover = $('wa-job-recover');
  const chat = $('wa-job-chat');
  const connect = $('wa-job-link');
  const settings = $('wa-job-settings');
  const watch = $('wa-job-auto');
  let session = null;
  let timer;

  function render(active) {
    const data = active.data;
    const fiscal = profile();
    const canSend = data?.status === 'new' && ['ready', 'external'].includes(active.row.status) && !active.row.issue_attempted;
    const pending = ['uncertain', 'sent'].includes(data?.status);
    $('wa-job-state').textContent = data ? statesWA[data.status] || 'Estado desconocido' : '';
    $('wa-job-message').textContent = active.feedback || data?.message || '';
    const record = data ? [data.phone && 'Destinatario ' + data.phone, data.reference,
      dateLabel(data.updated), data.count > 0 && (data.count === 1 ? '1 archivo recibido' : data.count + ' archivos recibidos')].filter(Boolean).join(' · ') : '';
    $('wa-job-record').hidden = !record;
    $('wa-job-record').textContent = record;
    form.hidden = !canSend || !fiscal;
    $('wa-job-fields').disabled = active.busy;
    $('wa-job-profile').textContent = fiscal ? 'Se solicitarán el PDF y el XML para ' + fiscal.email + '.' : '';
    const hint = data?.status === 'uncertain' ? 'Comprueba la conversación antes de cualquier otra solicitud. Puedes recuperar los archivos si el comercio ya respondió.' :
      data?.status === 'new' && !canSend ? 'Este expediente no admite un envío nuevo. Consulta su actividad en la bandeja.' :
      canSend && !fiscal ? 'Guarda el perfil fiscal que recibirá la factura.' : '';
    $('wa-job-hint').hidden = !hint;
    $('wa-job-hint').textContent = hint;
    recover.hidden = !pending;
    chat.hidden = !(pending || data?.status === 'verified');
    connect.hidden = !canSend || !fiscal;
    settings.hidden = !canSend || Boolean(fiscal);
    $('wa-job-tracking').hidden = !pending;
    watch.checked = active.pendingWatch ?? Boolean(data?.watch);
    const trackingInfo = pending ? [
      data.watch ? 'Docker y WhatsApp Web deben permanecer activos.' : 'Seguimiento automático en pausa.',
      data.last_check && 'Última revisión el ' + dateLabel(data.last_check) + '.',
      data.watch && data.next_check && 'Próxima revisión el ' + dateLabel(data.next_check) + '.',
      data.watch_message,
    ].filter(Boolean).join(' ') : '';
    $('wa-job-tracking-detail').textContent = trackingInfo;
    for (const control of [refresh, recover, chat, settings, connect, watch]) control.disabled = active.busy;
  }

  function scheduleRead(active) {
    clearTimeout(timer);
    const data = active.data;
    if (session !== active || !data || !(data.status === 'sending' || (data.watch && ['sent', 'uncertain'].includes(data.status)))) return;
    const revision = active.revision;
    timer = setTimeout(async () => {
      if (session !== active || active.revision !== revision) return;
      if (!document.hidden) {
        try {
          const latest = await api('/api/jobs/' + encodeURIComponent(active.row.id) + '/whatsapp');
          if (session !== active || active.revision !== revision) return;
          if (JSON.stringify(latest) !== JSON.stringify(active.data)) {
            active.data = latest;
            active.feedback = '';
            render(active);
          }
        } catch { /* A transient failure can be retried by the next local read. */ }
      }
      if (session === active && active.revision === revision) scheduleRead(active);
    }, 5000);
  }

  async function action(kind, payload) {
    const active = session;
    if (!active || active.busy) return;
    const base = '/api/jobs/' + encodeURIComponent(active.row.id) + '/whatsapp';
    active.revision++;
    clearTimeout(timer);
    active.busy = true;
    if (kind === 'watch') active.pendingWatch = payload.watch;
    active.feedback = {read: 'Leyendo el expediente…', send: 'Enviando la solicitud…', recover: 'Buscando la respuesta del comercio…',
      open: 'Abriendo la conversación…', link: 'Abriendo WhatsApp Web…', watch: 'Guardando el seguimiento…'}[kind];
    render(active);
    try {
      if (kind === 'open' || kind === 'link') {
        await api(kind === 'open' ? base + '/open' : '/api/whatsapp/open', 'POST');
        if (session === active) {
          dialog.close();
          showBrowser();
        }
        return;
      }
      active.data = kind === 'read' ? await api(base) : await api(base + '/' + kind, kind === 'watch' ? 'PUT' : 'POST', payload);
      active.feedback = '';
      if (kind === 'send' && session === active && active.data.status !== 'new') form.reset();
    } catch (error) {
      active.feedback = error.message;
      if (kind === 'send' || kind === 'recover' || kind === 'watch') {
        // The server may have committed the action even though its response was lost.
        active.data = null;
        try { active.data = await api(base); }
        catch { active.feedback += ' Actualiza el estado antes de continuar.'; }
      }
    } finally {
      active.busy = false;
      active.pendingWatch = null;
      if (session === active) {
        render(active);
        scheduleRead(active);
      }
    }
  }

  refresh.onclick = () => action('read');
  recover.onclick = () => action('recover');
  chat.onclick = () => action('open');
  connect.onclick = () => action('link');
  watch.onchange = () => action('watch', {watch: watch.checked});
  settings.onclick = () => { dialog.close(); showSettings(); };
  form.onsubmit = event => {
    event.preventDefault();
    if (form.hidden || !session || session.busy || !form.reportValidity()) return;
    const data = new FormData(form);
    data.set('phone', form.elements.phone.value.trim());
    data.set('consent', String(form.elements.consent.checked));
    data.set('watch', String(form.elements.watch.checked));
    for (const name of ['csf', 'voucher']) if (!form.elements[name].files.length) data.delete(name);
    return action('send', data);
  };
  dialog.addEventListener('close', () => { clearTimeout(timer); session = null; form.reset(); });
  return row => {
    session = {row, data: null, feedback: '', busy: false, revision: 0};
    $('wa-job-ticket').textContent = ticketLabel(row, currency);
    form.reset();
    render(session);
    dialog.showModal();
    action('read');
  };
}
