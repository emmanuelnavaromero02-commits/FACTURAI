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

const conversationStates = {
  active: 'Contestando al comercio', waiting: 'Esperando al comercio', delivered: 'El comercio mandó archivos',
  done: 'Terminada', needs_person: 'Necesita tu ayuda', limit: 'Llegó al tope de respuestas', off: 'Sin contestar por ti',
};
const linkStates = {
  listo: 'Vinculado', qr: 'Falta vincular', sesion_cerrada: 'Sesión cerrada', cargando: 'Cargando',
  sin_conexion: 'Sin conexión', otra_ventana: 'Abierto en otra ventana', sin_abrir: 'Sin abrir',
};

export function linkSummary(link) {
  if (!link) return '';
  return (linkStates[link.state] || 'Estado desconocido') + '. ' + (link.message || '') +
    (link.checked ? ' Revisado el ' + dateLabel(link.checked) + '.' : '');
}

// WhatsApp card in Settings: link state with «Comprobar» and the «my photos» reading.
export function installWhatsAppLink({api, showBrowser}) {
  const check = $('wa-link-check');
  function render(link) {
    $('wa-link-pill').textContent = linkStates[link.state] || 'Estado desconocido';
    $('wa-link-pill').classList.toggle('ready', Boolean(link.ready));
    $('wa-link-message').textContent = link.message || '';
    $('wa-link-checked').textContent = link.checked ? 'Revisado el ' + dateLabel(link.checked) + '.' : 'Aún no se ha revisado en esta sesión.';
  }
  async function read(method) {
    check.disabled = true;
    if (method === 'POST') $('wa-link-message').textContent = 'Comprobando WhatsApp Web…';
    try { render(await api(method === 'POST' ? '/api/whatsapp/link/check' : '/api/whatsapp/link', method)); }
    catch (error) { $('wa-link-message').textContent = error.message; }
    finally { check.disabled = false; }
  }
  async function photos() {
    try {
      const data = await api('/api/whatsapp/photos');
      $('wa-photos').textContent = (data.enabled
        ? 'Mis fotos: activo. Importo los tickets que te mandes a tu propio chat de WhatsApp.'
        : 'Mis fotos: apagado. Para usarlo, guarda tu WhatsApp en Datos fiscales y activa el permiso de leer tus fotos.') +
        (data.message ? ' ' + data.message : '');
    } catch (error) { $('wa-photos').textContent = error.message; }
  }
  check.onclick = () => read('POST');
  $('wa-link-open').onclick = async () => {
    try {
      await api('/api/whatsapp/open', 'POST');
      showBrowser();
    } catch (error) { $('wa-link-message').textContent = error.message; }
  };
  return {refresh: () => Promise.all([read('GET'), photos()]), read: () => api('/api/whatsapp/link')};
}

export function installWhatsApp({api, currency, profile, showBrowser, showSettings, linkState, changed}) {
  const dialog = $('wa-job-dialog');
  const form = $('wa-job-form');
  const refresh = $('wa-job-refresh');
  const recover = $('wa-job-recover');
  const chat = $('wa-job-chat');
  const connect = $('wa-job-link');
  const settings = $('wa-job-settings');
  const watch = $('wa-job-auto');
  const reply = $('wa-job-reply');
  const complete = $('wa-job-complete-form');
  let session = null;
  let timer;

  function render(active) {
    const data = active.data;
    const fiscal = profile();
    const canSend = data?.status === 'new' && ['ready', 'external'].includes(active.row.status) && !active.row.issue_attempted;
    const pending = ['uncertain', 'sent'].includes(data?.status);
    $('wa-job-state').textContent = data ? statesWA[data.status] || 'Estado desconocido' : '';
    $('wa-job-message').textContent = active.feedback || data?.message || '';
    const attention = Boolean(data?.attention) && pending;
    $('wa-job-attention').hidden = !attention;
    $('wa-job-attention').textContent = attention ? 'Te necesito en esta conversación. ' +
      (data.conversation_message || 'Revisa el chat del comercio en WhatsApp.') : '';
    const talking = pending && (data.reply || (data.conversation && data.conversation !== 'off'));
    const replies = Number(data?.replies) || 0;
    $('wa-job-conversation').hidden = !talking;
    $('wa-job-conversation').textContent = talking ? [
      'Conversación: ' + (conversationStates[data.conversation] || 'estado desconocido') + '.',
      replies === 1 ? '1 respuesta enviada.' : replies + ' respuestas enviadas.',
      !attention && data.conversation_message,
    ].filter(Boolean).join(' ') : '';
    $('wa-job-reply-row').hidden = !pending;
    reply.checked = active.pendingReply ?? Boolean(data?.reply);
    $('wa-job-link-state').hidden = !active.link;
    $('wa-job-link-state').textContent = active.link ? 'WhatsApp Web: ' + linkSummary(active.link) : '';
    complete.hidden = !(data?.status === 'uncertain' && data.completable);
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
    for (const control of [refresh, recover, chat, settings, connect, watch, reply]) control.disabled = active.busy;
    $('wa-job-complete-fields').disabled = active.busy;
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
    if (kind === 'reply') active.pendingReply = payload.reply;
    active.feedback = {read: 'Leyendo el expediente…', send: 'Enviando la solicitud…', recover: 'Buscando la respuesta del comercio…',
      open: 'Abriendo la conversación…', link: 'Abriendo WhatsApp Web…', watch: 'Guardando el seguimiento…',
      reply: 'Guardando…', complete: 'Completando el envío…'}[kind];
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
      active.data = kind === 'read' ? await api(base) :
        await api(base + '/' + kind, ['watch', 'reply'].includes(kind) ? 'PUT' : 'POST', payload);
      active.feedback = '';
      if (kind === 'send' && session === active && active.data.status !== 'new') form.reset();
      if (kind === 'complete' && session === active) complete.reset?.();
      if (['send', 'recover', 'complete'].includes(kind)) changed?.();
    } catch (error) {
      active.feedback = error.message;
      if (['send', 'recover', 'watch', 'reply', 'complete'].includes(kind)) {
        // The server may have committed the action even though its response was lost.
        active.data = null;
        try { active.data = await api(base); }
        catch { active.feedback += ' Actualiza el estado antes de continuar.'; }
      }
    } finally {
      active.busy = false;
      active.pendingWatch = null;
      active.pendingReply = null;
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
  reply.onchange = () => action('reply', {reply: reply.checked});
  settings.onclick = () => { dialog.close(); showSettings(); };
  form.onsubmit = event => {
    event.preventDefault();
    if (form.hidden || !session || session.busy || !form.reportValidity()) return;
    const data = new FormData(form);
    data.set('phone', form.elements.phone.value.trim());
    data.set('consent', String(form.elements.consent.checked));
    data.set('watch', String(form.elements.watch.checked));
    data.set('reply', String(Boolean(form.elements.reply?.checked)));
    for (const name of ['csf', 'voucher']) if (!form.elements[name].files.length) data.delete(name);
    return action('send', data);
  };
  complete.onsubmit = event => {
    event.preventDefault();
    if (complete.hidden || !session || session.busy) return;
    const data = new FormData(complete);
    for (const name of ['csf', 'voucher']) if (!complete.elements[name].files.length) data.delete(name);
    return action('complete', data);
  };
  dialog.addEventListener('close', () => { clearTimeout(timer); session = null; form.reset(); complete.reset?.(); });
  return row => {
    const active = {row, data: null, feedback: '', busy: false, revision: 0, link: null};
    session = active;
    $('wa-job-ticket').textContent = ticketLabel(row, currency);
    form.reset();
    // The merchant's number printed on the ticket is proposed; typing one is then optional.
    const phone = form.elements?.phone;
    const saved = row.receipt.merchant_whatsapp || '';
    if (phone) {
      phone.value = saved;
      phone.required = !saved;
    }
    $('wa-job-phone-hint').hidden = !saved;
    render(active);
    dialog.showModal();
    action('read');
    if (linkState) linkState().then(link => {
      if (session !== active) return;
      active.link = link;
      render(active);
    }).catch(() => {});
  };
}
