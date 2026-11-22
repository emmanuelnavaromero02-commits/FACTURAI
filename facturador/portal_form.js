({origin, action, profile, password}) => {
  const filled = [];
  const done = (reason = '', submit = null) => ({filled, reason, submit});
  if (location.origin !== origin || location.protocol !== 'https:') return done('El dominio cambió.');
  const norm = s => String(s || '').normalize('NFD').replace(/[\u0300-\u036f]/g, '').toLowerCase().trim();
  const visible = el => !el.disabled && !el.readOnly && el.getClientRects().length > 0 &&
    getComputedStyle(el).visibility === 'visible';
  const caption = el => norm([el.name, el.id, el.placeholder, el.getAttribute('aria-label'),
    ...Array.from(el.labels || [], label => label.textContent)].filter(Boolean).join(' '));
  const sameOrigin = url => { try { const u = new URL(url, document.baseURI); return u.origin === origin && !u.username && !u.password; }
    catch { return false; } };
  const sameForm = form => !form || (sameOrigin(form.action) &&
    Array.from(form.elements).every(el => !el.hasAttribute('formaction') || sameOrigin(el.formAction)));
  const inputs = Array.from(document.querySelectorAll('input,select')).filter(el => visible(el) && sameForm(el.form));
  const textInput = el => el instanceof HTMLInputElement && ['text','email','tel','number','search'].includes(el.type);
  const emailInput = el => textInput(el) && (el.type === 'email' || el.autocomplete === 'email' ||
    /e[-_ ]?mail|correo/.test(caption(el)) || (action !== 'fiscal' && el.autocomplete === 'username'));
  const passInput = el => el instanceof HTMLInputElement && el.type === 'password';
  const brandNew = el => el.autocomplete === 'new-password' || /confirm|repet|nuev|new.?pass/.test(caption(el));
  const current = el => el.autocomplete === 'current-password' || /actual|tempor|current|anterior/.test(caption(el));
  const fill = (el, value, label) => {
    if (!value || !visible(el) || !sameForm(el.form) || location.origin !== origin) return false;
    let desired = value;
    if (el instanceof HTMLSelectElement) {
      const option = Array.from(el.options).filter(opt => !opt.disabled &&
        (opt.value === value || norm(opt.textContent).split(/[^a-z0-9]+/)[0] === norm(value)));
      if (option.length !== 1) return false;
      desired = option[0].value;
    } else if (el.maxLength >= 0 && desired.length > el.maxLength) return false;
    if (el.value && el.value !== desired) return false;
    if (el.value !== desired) {
      const proto = el instanceof HTMLSelectElement ? HTMLSelectElement.prototype : HTMLInputElement.prototype;
      Object.getOwnPropertyDescriptor(proto, 'value').set.call(el, desired);
      el.dispatchEvent(new Event('input', {bubbles: true}));
      el.dispatchEvent(new Event('change', {bubbles: true}));
    }
    if (el.value !== desired) return false;
    if (!filled.includes(label)) filled.push(label);
    return true;
  };

  if (action === 'fiscal') {
    const kinds = [
      ['rfc', 'RFC', x => /rfc|registro federal de contribuyentes/.test(x)],
      ['name', 'razón social', x => /razon.?social|nombre.?fiscal|nombre.*receptor|denominacion/.test(x)],
      ['postal_code', 'código postal', x => /codigo.?postal|codpostal|domicilio.?fiscal.?receptor|(^|\W)cp($|\W)/.test(x)],
      ['regime', 'régimen fiscal', x => /regimen/.test(x)],
      ['cfdi_use', 'uso del CFDI', x => /uso.*cfdi|cfdi.*uso/.test(x)],
      ['email', 'correo', () => false]
    ];
    for (const [key, label, matches] of kinds) {
      const options = inputs.filter(el => (textInput(el) || el instanceof HTMLSelectElement) &&
        !/emisor|proveedor|envio|tarjeta|entrega de mercancia/.test(caption(el)) &&
        (key === 'email' ? emailInput(el) : matches(caption(el))));
      if (options.length === 1) fill(options[0], profile[key], label);
    }
    return done(filled.length ? 'Revisa los datos fiscales en el portal.' : 'No encontré campos fiscales vacíos con etiquetas reconocibles.');
  }

  const relevant = inputs.filter(el => passInput(el) || emailInput(el));
  const groups = [...new Set(relevant.map(el => el.form))];
  const focused = document.activeElement;
  const form = relevant.includes(focused) ? focused.form : groups[0];
  if (!relevant.length || (groups.length > 1 && !relevant.includes(focused))) {
    return done('Haz clic en el campo de correo o contraseña del formulario correcto en el visor.');
  }
  const pass = relevant.filter(el => el.form === form && passInput(el));
  const emails = relevant.filter(el => el.form === form && emailInput(el));
  const olds = pass.filter(current);
  const news = pass.filter(brandNew);
  if (['register', 'create'].includes(action) && form && profile.whatsapp_phone) {
    const phones = inputs.filter(el => el.form === form && textInput(el) && (el.type === 'tel' ||
      /telefono|celular|whatsapp|movil/.test(caption(el))) && !/comercio|establecimiento|opcional/.test(caption(el)) &&
      (el.required || el.getAttribute('aria-required') === 'true' ||
       (/\b(?:requerido|obligatorio)\b/.test(caption(el)) && !/\bno (?:es )?(?:requerido|obligatorio)\b/.test(caption(el)))));
    if (phones.length === 1) {
      const input = phones[0];
      const mexicanNational = /\+52\b/.test(caption(input)) && (input.maxLength === 10 || /10 digitos/.test(caption(input)));
      const phone = mexicanNational && /^\+52\d{10}$/.test(profile.whatsapp_phone) ?
        profile.whatsapp_phone.slice(3) : profile.whatsapp_phone;
      fill(input, phone, 'teléfono');
    }
  }
  let targets = [];
  if (action === 'temporary' || action === 'login') {
    if (olds.length === 1) targets = olds;
    else if (pass.length === 1 && !brandNew(pass[0])) targets = pass;
  } else if (action === 'change') {
    targets = news.filter(el => !current(el));
  } else if (!olds.length && pass.length <= 2) {
    targets = pass;
  }
  if (pass.length && (targets.length === 0 || targets.length > 2)) {
    return done('El portal no distingue una contraseña actual y una nueva. Usa los campos del portal para este paso.');
  }
  if (action !== 'temporary' && action !== 'change' && emails.length === 1) fill(emails[0], profile.email, 'correo');
  const complete = targets.map(el => fill(el, password, action === 'temporary' ? 'contraseña temporal' : 'contraseña')).every(Boolean);
  if (!filled.length) return done('No encontré campos compatibles vacíos. Comprueba el correo, las etiquetas y los límites de longitud.');
  if (action !== 'create') return done('Campos preparados. Revisa el formulario antes de enviarlo.');
  const submitButtons = form && Array.from(form.elements).filter(el =>
    (el instanceof HTMLButtonElement || el instanceof HTMLInputElement) &&
    ['submit','image'].includes(el.type) && visible(el));
  const button = submitButtons && submitButtons.length === 1 ? submitButtons[0] : null;
  const registrationLabel = /^(crear( mi| una)? cuenta|registrarme|registrar(me)?( ahora)?|crear usuario|create( an| my)? account|sign up)$/;
  const eligible = () => {
    if (!complete || !button || !registrationLabel.test(norm(button.innerText || button.value)) ||
        !button.isConnected || !visible(button) || !form.isConnected ||
        !sameForm(form) || location.origin !== origin || emails.length !== 1 || emails[0].value !== profile.email ||
        button.form !== form || button.formNoValidate || form.noValidate ||
        (button.getAttribute('formmethod') || form.method).toLowerCase() !== 'post' ||
        !['', '_self'].includes(button.getAttribute('formtarget') ?? (form.target || document.querySelector('base[target]')?.target || '')) ||
        !form.checkValidity()) return false;
    const text = norm(form.innerText);
    if (/captcha|verifica que eres humano|robot|pagar|comprar|tarjeta|emitir factura|timb(rar|rado)/.test(text) ||
        document.querySelector('iframe[src*="captcha" i], iframe[src*="challenges.cloudflare" i], [data-sitekey], [name*="captcha" i]')) return false;
    const terms = Array.from(form.querySelectorAll('input[type=checkbox]')).filter(el =>
      /terminos|condiciones|privacidad|terms|consent|acept/.test(caption(el)));
    if (terms.some(el => !el.checked) || (/terminos|condiciones|terms of|privacidad/.test(text) && !terms.length)) return false;
    return !Array.from(form.elements).some(el => el.type === 'file' || /^(cc-|one-time-code)/.test(el.autocomplete || ''));
  };
  if (!eligible()) return done('Revisa el registro y complétalo en el portal. Puede requerir verificación, condiciones u otros campos.');
  return done('', () => {
    if (!eligible()) return false;
    HTMLFormElement.prototype.requestSubmit.call(form, button);
    return true;
  });
}
