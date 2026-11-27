// Per-ticket progress shared by the chat and the inbox: stage, waiting state and time in the stage.
export const STAGES = ['Subiendo', 'Leyendo', 'Abriendo el portal', 'Llenando', 'Descargando', 'Lista'];
const READING = {preparando: 'Preparando la lectura', leyendo: 'Leyendo el ticket', buscando_portal: 'Buscando el portal del comercio'};
const seen = new Map();

function readingJob(state, id) {
  const jobs = state.automation?.jobs;
  const job = jobs && typeof jobs === 'object' ? jobs[id] : null;
  return job && READING[job.phase] ? job : null;
}

function position(row, state) {
  const automation = state.automation || {};
  const automatic = Boolean(automation.enabled || row.batch_id?.startsWith('chat-'));
  const browserHere = Boolean(state.browser?.open && state.browser.job_id === row.id && state.active_job === row.id);
  const job = readingJob(state, row.id);
  if (job && ['new', 'review'].includes(row.status)) {
    return {index: 1, detail: job.message || READING[job.phase], since: Number(job.since) > 0 ? Number(job.since) * 1000 : null};
  }
  if (automation.current_id === row.id && automation.phase === 'reading') return {index: 1, detail: 'Identificando los datos del ticket…'};
  switch (row.status) {
    case 'new': return automatic ? {index: 1, detail: 'En cola de lectura'} : {index: 1, waiting: true, detail: 'Pide leer el ticket.'};
    case 'review': return {index: 1, waiting: true, detail: 'Revisa los datos del ticket.'};
    case 'ready': return automatic ? {index: 2, detail: 'Listo para abrir el portal'} : {index: 2, waiting: true, detail: 'Inicia el trámite cuando quieras.'};
    case 'queued': return {index: 2, detail: 'En espera del navegador'};
    case 'running': return {index: browserHere ? 3 : 2};
    case 'issuing': return {index: 3, detail: 'Emitiendo la factura'};
    case 'needs_user': return {index: browserHere || row.human_exposed ? 3 : 2, waiting: true};
    case 'blocked': return {index: row.issue_attempted || row.issuance_seen ? 4 : 3, waiting: true};
    case 'issued_waiting_files': case 'uncertain': return {index: 4, detail: 'Buscando el PDF y el XML'};
    case 'verified': return {index: 5, done: true};
    default: return null;
  }
}

// Stage of a ticket, or null for rows outside the facturation flow (duplicates, history, WhatsApp).
export function ticketStage(row, state, now = Date.now()) {
  if (!row) return null;
  const stage = position(row, state);
  if (!stage) return null;
  const key = stage.index + '|' + Boolean(stage.waiting);
  const previous = seen.get(row.id);
  let since = stage.since || null;
  if (!since) {
    if (previous?.key === key) since = previous.since;
    else if (!previous) {
      const updated = Date.parse(row.updated || '');
      since = Number.isFinite(updated) ? Math.min(updated, now) : now;
    } else since = now;
  }
  seen.set(row.id, {key, since});
  return {...stage, since, label: stage.waiting ? 'Te necesito' : STAGES[stage.index]};
}

export function uploadStage(fraction, startedAt) {
  const percent = Math.round(Math.max(0, Math.min(1, fraction || 0)) * 100);
  return {index: 0, label: STAGES[0], detail: percent + '%', since: startedAt || Date.now(), percent};
}

export function elapsed(since, now = Date.now()) {
  const seconds = Math.max(0, Math.floor((now - since) / 1000));
  if (seconds >= 86_400) return 'más de un día';
  const hours = Math.floor(seconds / 3600);
  const minutes = Math.floor(seconds / 60) % 60;
  const rest = String(seconds % 60).padStart(2, '0');
  return hours ? `${hours}:${String(minutes).padStart(2, '0')}:${rest}` : `${minutes}:${rest}`;
}

function clock(since) {
  const time = document.createElement('span');
  time.className = 'stage-time';
  time.dataset.stageSince = String(since);
  time.textContent = elapsed(since);
  return time;
}

// Full step list for the chat; ``compact`` draws one line with small segments for the inbox.
export function stageView(stage, {compact = false} = {}) {
  const box = document.createElement('div');
  box.className = compact ? 'stages compact' : 'stages';
  box.dataset.stage = String(stage.index);
  if (stage.waiting) box.classList.add('waiting');
  if (stage.done) box.classList.add('done');
  const steps = document.createElement('ol');
  steps.setAttribute('aria-hidden', compact ? 'true' : 'false');
  STAGES.forEach((name, index) => {
    const step = document.createElement('li');
    if (index < stage.index || stage.done) step.className = 'past';
    else if (index === stage.index) step.className = stage.waiting ? 'current waiting' : 'current';
    if (!compact) step.textContent = name;
    steps.append(step);
  });
  const now = document.createElement('p');
  now.className = 'stage-now';
  now.setAttribute('role', 'status');
  const label = document.createElement('strong');
  label.textContent = stage.label;
  now.append(label);
  if (stage.waiting && !compact) now.append(' · ' + STAGES[stage.index]);
  if (!stage.done) now.append(' · ', clock(stage.since));
  if (stage.percent !== undefined) {
    const bar = document.createElement('progress');
    bar.max = 100;
    bar.value = stage.percent;
    bar.className = 'stage-upload';
    bar.setAttribute('aria-label', 'Avance de la subida');
    box.append(steps, now, bar);
    return box;
  }
  box.append(steps, now);
  return box;
}

export function stageSignature(stage) {
  return stage ? [stage.index, stage.label, Boolean(stage.waiting), Boolean(stage.done), stage.since, stage.percent].join('|') : '';
}

let ticking = 0;
export function startStageClock() {
  if (ticking) return;
  ticking = setInterval(() => {
    if (document.hidden) return;
    const now = Date.now();
    for (const node of document.querySelectorAll('[data-stage-since]')) {
      const text = elapsed(Number(node.dataset.stageSince), now);
      if (node.textContent !== text) node.textContent = text;
    }
  }, 1000);
}
