({origin, action, profile, password, plan}) => {
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
  // What keeps a form away from an unattended send: a verification, a payment or an emission,
  // terms nobody accepted and controls that carry files, cards or one-time codes.
  // The rule-based sign-up and the assisted one share these exclusions.
  const blockedText = text => /captcha|verifica que eres humano|robot|pagar|comprar|tarjeta|emitir factura|timb(rar|rado)/.test(text);
  const challengeWidget = () => document.querySelector(
    'iframe[src*="captcha" i], iframe[src*="challenges.cloudflare" i], [data-sitekey], [name*="captcha" i]');
  const termsCaption = text => /terminos|condiciones|privacidad|terms|consent|acept/.test(text);
  const termsText = text => /terminos|condiciones|terms of|privacidad/.test(text);
  const privateControl = el => el.type === 'file' || /^(cc-|one-time-code)/.test(el.autocomplete || '');
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

  if (action === 'describe' || action === 'assist') {
    // Assisted sign-up for the registration form of any portal. «describe» reports that form
    // without a single value; «assist» fills it by a plan the program already checked and hands
    // back its submit. No value of the page is ever reported: only whether a field has one.
    const ask = plan || {};
    const fold = s => norm(s).replace(/\s+/g, ' ');
    const tidy = (s, max) => String(s || '').replace(/\s+/g, ' ').trim().slice(0, max);
    const shown = el => el.getClientRects().length > 0 && getComputedStyle(el).visibility === 'visible';
    const reachable = el => shown(el) ||
      (['checkbox', 'radio'].includes(el.type) && Array.from(el.labels || []).some(shown));
    // Form properties read past any control named «action» or «elements».
    const formProp = (form, name) => Object.getOwnPropertyDescriptor(HTMLFormElement.prototype, name).get.call(form);
    const sameTarget = form => sameOrigin(formProp(form, 'action')) && ['', '_self'].includes(formProp(form, 'target')) &&
      Array.from(formProp(form, 'elements')).every(el => !el.hasAttribute('formaction') || sameOrigin(el.formAction));
    const STRONG = /^(crear?( mi| una| tu)? cuenta( nueva| ahora| gratis)?|registrarme( ahora| gratis)?|registrarse|registrate( ahora| aqui| gratis)?|registrar (mi )?(cuenta|usuario)|crear (mi )?usuario|darme de alta|date de alta|abrir (mi )?cuenta|(completar|finalizar|terminar|confirmar) (mi |el )?registro|create( an| my| your)? account|sign ?up( now| free)?|join( now)?)$/;
    const UNSURE = /^(registrar( ahora)?|registro|register( now)?|crear|enviar registro)$/;
    const WEAK = /^(continuar|siguiente|enviar|aceptar|guardar( y continuar)?|finalizar|confirmar|terminar|listo|next|continue|submit|save|finish|done)$/;
    const SIGN_IN = /^(iniciar sesion|inicia sesion|ingresar|acceder|entrar|login|log in|sign in)$/;
    const leadsAway = el => el.matches('a') && !/^(#.*|javascript:.*)?$/i.test(el.getAttribute('href') || '');
    const ACCOUNT = /crear? (tu |una |mi |su )?cuenta|registro de (usuarios?|cuenta|clientes?)|nuevo usuario|nueva cuenta|alta de (usuarios?|cuenta|clientes?)|registrate|registrarse|registrarme|crear usuario|sign ?up|create (an |your |my )?account|registration/;
    const ACCOUNT_TITLE = /^(registro|nuevo registro|crear cuenta)$/;
    const ANOTHER_PURPOSE = /restablec|recupera|olvid|cambi(a|ar|o) (de |tu |la |mi )?contrasena|reset|forgot|doble factor|dos (pasos|factores)|segundo factor|two.factor|multi.factor/;
    const REG_PATH = /regist|signup|sign[-_]up|crear[-_]?cuenta|create[-_]?account|nuevo[-_]?usuario|new[-_]?user/i;
    const TICKET = /folio|ticket|nota de (venta|compra|consumo)|numero de (transaccion|operacion|venta|orden|pedido)|importe|\bmonto\b|\btotal\b|fecha de (compra|consumo|venta|operacion)|codigo de facturacion|\bweb ?id\b|(forma|metodo) de pago/;
    const CARD = /tarjeta|\bcvv\b|\bcvc\b|vencimiento|expiracion|card ?number|clabe|cuenta bancaria|numero de cuenta/;
    const ONE_TIME = /\botp\b|one.time|codigo de (verificacion|seguridad|acceso|autenticacion)|\btoken\b|\bnip\b|\bpin\b/;
    const TERMS = /terminos|condiciones|privacidad|terms|privacy|aviso legal|politica|contrato/;
    const EXTERNAL = /\b(google|facebook|apple|microsoft|twitter|linkedin|github|outlook|yahoo|amazon)\b/;
    const NOT_OURS = /pag(ar|o|ue)\b|compr(ar|a)\b|factur|emit|timbr|cfdi|donar|suscrib/;
    const NATIVE = 'button, input[type=submit], input[type=button], input[type=image], [role=button], a';
    const rawText = el => tidy(el.getAttribute('aria-label') || el.innerText || el.value || el.title ||
      el.querySelector('img[alt]')?.alt, 60);
    const labelOfButton = el => fold(rawText(el)).replace(/^[^a-z0-9]+|[^a-z0-9]+$/g, '');
    const stripped = node => {
      const copy = node.cloneNode(true);
      copy.querySelectorAll('input, select, textarea, script, style').forEach(child => child.remove());
      return copy.textContent;
    };
    const before = el => {
      for (let node = el, depth = 0; node && depth < 3; depth++, node = node.parentElement) {
        for (let prev = node.previousElementSibling; prev; prev = prev.previousElementSibling) {
          const text = shown(prev) ? tidy(prev.innerText, 400) : '';
          if (text) return text.slice(-80);
        }
      }
      return '';
    };
    const labelOf = el => {
      const own = Array.from(el.labels || [], stripped).join(' ');
      const named = (el.getAttribute('aria-labelledby') || '').split(/\s+/).filter(Boolean)
        .map(id => document.getElementById(id)?.textContent || '').join(' ');
      const around = ['checkbox', 'radio'].includes(el.type) && el.parentElement ? stripped(el.parentElement) : '';
      return tidy(own || el.getAttribute('aria-label') || named || around || el.placeholder || el.title ||
        before(el) || el.name || el.id, 90);
    };
    const groupLabel = el => {
      const group = el.closest('fieldset, [role=radiogroup]');
      return tidy(group?.querySelector('legend')?.textContent || group?.getAttribute('aria-label') ||
        before(group || el.parentElement || el) || el.name, 90);
    };
    const mustFill = (el, label) => el.required || el.getAttribute('aria-required') === 'true' || /^\*|\*$/.test(label.trim()) ||
      (/\b(requerido|obligatorio)\b/.test(fold(label)) && !/\bno (es )?(requerido|obligatorio)\b/.test(fold(label)));
    const hintOf = (el, label) => tidy([
      el.placeholder && el.placeholder !== label ? 'ejemplo o formato: ' + el.placeholder : '',
      el.maxLength > 0 ? 'máximo ' + el.maxLength + ' caracteres' : '',
      el.autocomplete && !['on', 'off'].includes(el.autocomplete) ? 'autocomplete ' + el.autocomplete : '',
      (el.getAttribute('aria-describedby') || '').split(/\s+/).filter(Boolean)
        .map(id => document.getElementById(id)?.textContent || '').join(' '),
      el.title && el.title !== label ? el.title : ''].filter(Boolean).join('; '), 140);
    const kindOf = el => el instanceof HTMLSelectElement ? (el.multiple ? 'otro' : 'lista') :
      el instanceof HTMLTextAreaElement ? 'texto_largo' :
      ({text: 'texto', search: 'texto', url: 'texto', email: 'correo', tel: 'telefono', number: 'numero', date: 'fecha',
        password: 'contrasena', checkbox: 'casilla', radio: 'opciones'})[el.type] || 'otro';
    const hash = (text, seed) => {
      let value = seed;
      for (let i = 0; i < text.length; i++) value = Math.imul(value ^ text.charCodeAt(i), 0x01000193) >>> 0;
      return value.toString(16).padStart(8, '0');
    };

    const scan = () => {
      const all = Array.from(document.querySelectorAll('input, select, textarea')).filter(el =>
        !['hidden', 'submit', 'button', 'reset', 'image', 'file'].includes(el.type) && !el.disabled && !el.readOnly &&
        reachable(el));
      let clickables = Array.from(document.querySelectorAll(NATIVE)).filter(el => shown(el) && rawText(el) && !el.closest('label'));
      // Blocks dressed as buttons: short, without controls inside, and marked as pressable.
      const dressed = Array.from(document.querySelectorAll('div, span')).filter(el =>
        el.childElementCount <= 2 && (el.textContent || '').length <= 80 && shown(el) && rawText(el) &&
        !el.closest(NATIVE + ', label') && !el.querySelector('input, select, textarea, ' + NATIVE) &&
        (/(^|[\s_-])(btn|button|boton|submit)($|[\s_-])/i.test(typeof el.className === 'string' ? el.className : '') ||
         getComputedStyle(el).cursor === 'pointer'));
      clickables = clickables.concat(dressed.filter(el => !dressed.some(other => other !== el && el.contains(other))));
      const scopes = [...new Set(all.map(el => el.form).filter(Boolean))].map(form => ({form, root: form}));
      const loose = all.filter(el => !el.form);
      if (loose.length) {
        // Controls outside any <form>: the smallest block around them that also holds their button.
        const acts = node => clickables.some(b => node.contains(b) && !leadsAway(b) && !b.closest('nav, header, footer, aside') &&
          (STRONG.test(labelOfButton(b)) || UNSURE.test(labelOfButton(b)) || WEAK.test(labelOfButton(b))));
        let root = (loose.find(el => el.type === 'password') || loose[0]).parentElement;
        while (root && root !== document.body && !acts(root)) root = root.parentElement;
        if (root) scopes.push({form: null, root});
      }
      const heads = Array.from(document.querySelectorAll('h1, h2, h3, h4, legend, [role=heading]'))
        .filter(h => shown(h) && !h.closest('nav, footer, aside'));
      const found = [];
      for (const scope of scopes) {
        const {form, root} = scope;
        scope.fields = all.filter(el => form ? el.form === form : !el.form && root.contains(el));
        scope.buttons = clickables.filter(el => form ? (form.contains(el) || el.form === form) : root.contains(el) && !el.form);
        const above = heads.filter(h => !root.contains(h) &&
          (h.compareDocumentPosition(root) & Node.DOCUMENT_POSITION_FOLLOWING)).slice(-1);
        const wording = [...above, ...heads.filter(h => root.contains(h))].map(h => fold(h.innerText));
        if (form) wording.push(fold(['aria-label', 'id', 'name', 'action'].map(name => form.getAttribute(name) || '').join(' ')));
        const pass = scope.fields.filter(el => el.type === 'password');
        // Links that lead to another page and the menus around the form say nothing about this form.
        scope.pressable = scope.buttons.filter(el => !EXTERNAL.test(labelOfButton(el)) && !leadsAway(el) &&
          !el.closest('nav, header, footer, aside'));
        const labels = scope.pressable.map(labelOfButton);
        const strong = labels.some(label => STRONG.test(label));
        const soft = labels.some(label => WEAK.test(label) || UNSURE.test(label));
        const account = wording.some(text => ACCOUNT.test(text) || ACCOUNT_TITLE.test(text));
        const following = Boolean(ask.continuing) && (REG_PATH.test(location.pathname) || location.pathname === ask.previousPath);
        // One password that is not new next to a sign-in button is a sign-in, whatever else the form offers.
        const signIn = pass.length === 1 && !pass.some(brandNew) && labels.some(label => SIGN_IN.test(label));
        let evidence = '';
        if (pass.length > 2 || signIn || wording.some(text => ANOTHER_PURPOSE.test(text))) evidence = '';
        else if (pass.length && pass.length === scope.fields.length) evidence = strong || (soft && following) ? 'clave' : '';
        else if (pass.length && (pass.length === 2 || pass.some(brandNew) || strong || account)) evidence = 'clave';
        else if (strong) evidence = 'boton';
        else if (soft && account) evidence = 'titulo';
        else if ((soft || pass.length) && following) evidence = 'continua';
        if (evidence) found.push({...scope, evidence});
      }
      if (found.length !== 1) return {view: {estado: 'no'}};
      const scope = found[0];
      const {form, root} = scope;
      const inner = form ? Array.from(formProp(form, 'elements')) : Array.from(root.querySelectorAll('input, select, textarea'));
      const words = el => fold(labelOf(el) + ' ' + caption(el));
      // The form that looks up or bills a ticket is never a registration, whatever its heading says.
      if (scope.fields.some(el => TICKET.test(words(el)))) return {view: {estado: 'no'}};
      const page = fold(document.body.innerText);
      const carried = inner.filter(el => el.type !== 'password').map(el => fold(el.value)).join(' | ');
      const marked = (ask.marks || []).some(mark => {
        const exact = new RegExp('(?<![\\w-])' + fold(mark).replace(/[.*+?^${}()|[\]\\]/g, '\\$&') + '(?![\\w-])');
        return exact.test(page) || exact.test(carried);
      });
      if (marked) return {view: {estado: 'no'}};
      const text = fold(root.innerText);
      const blocks = [];
      if (blockedText(text) || blockedText(scope.buttons.map(labelOfButton).join(' | ')) || challengeWidget()) blocks.push('excluido');
      if (inner.some(privateControl) || root.querySelector('input[type=file]') ||
          scope.fields.some(el => CARD.test(words(el)) || ONE_TIME.test(words(el)))) blocks.push('campos');
      if (form && !sameTarget(form)) blocks.push('destino');
      if (scope.fields.length > 60) blocks.push('tamano');
      const fields = [], elements = {}, radios = new Set();
      for (const el of scope.fields) {
        const id = 'c' + (fields.length + 1);
        if (el.type === 'radio') {
          const key = el.name || labelOf(el);
          if (radios.has(key)) continue;
          radios.add(key);
          const group = scope.fields.filter(other => other.type === 'radio' && (other.name || labelOf(other)) === key);
          const label = groupLabel(el);
          fields.push({id, tipo: 'opciones', etiqueta: label, obligatorio: group.some(radio => mustFill(radio, label)),
            pista: '', opciones: group.map(radio => tidy(labelOf(radio), 60)), valores: group.map(radio => radio.value),
            lleno: group.some(radio => radio.checked), nombre: el.name || ''});
          elements[id] = group;
          continue;
        }
        const label = labelOf(el), kind = kindOf(el);
        const field = {id, tipo: kind, etiqueta: label, obligatorio: mustFill(el, label), pista: hintOf(el, label),
          lleno: kind === 'casilla' ? el.checked : el.value !== '', nombre: el.name || ''};
        if (kind === 'lista') {
          const options = Array.from(el.options).filter(opt => !opt.disabled && opt.value !== '' && tidy(opt.textContent, 60));
          field.opciones = options.slice(0, 300).map(opt => tidy(opt.textContent, 60));
          field.valores = options.slice(0, 300).map(opt => opt.value);
          field.recortada = options.length > 300;
          elements[id + ':opciones'] = options.slice(0, 300);
        }
        if (kind === 'casilla') field.terminos = TERMS.test(words(el));
        if (privateControl(el) || CARD.test(words(el)) || ONE_TIME.test(words(el))) field.prohibido = true;
        if (el.maxLength > 0) field.maximo = el.maxLength;
        fields.push(field);
        elements[id] = el;
      }
      const outside = clickables.filter(el => !scope.buttons.includes(el) &&
        el.matches('button, input[type=submit], input[type=button], input[type=image], [role=button]'));
      // A link to another page never sends a form: it is not offered as its button.
      const inside = scope.buttons.filter(el => !leadsAway(el));
      const listed = [...inside, ...outside].slice(0, 40);
      const buttons = listed.map((el, index) => ({id: 'b' + (index + 1), texto: rawText(el),
        en_formulario: inside.includes(el), externo: EXTERNAL.test(labelOfButton(el)),
        prohibido: NOT_OURS.test(labelOfButton(el)) || blockedText(labelOfButton(el))}));
      const pressers = Object.fromEntries(listed.map((el, index) => ['b' + (index + 1), el]));
      const shape = location.pathname + '\n' + fields.map(field =>
        [field.tipo, field.etiqueta, field.obligatorio, (field.opciones || []).join('¦')].join('|')).join('\n');
      const optionTexts = new Set(fields.flatMap(field => field.tipo === 'lista' ? field.opciones : []));
      const terms = scope.fields.filter(el => el.type === 'checkbox' && (TERMS.test(words(el)) || termsCaption(words(el))));
      return {scope, elements, pressers, view: {
        estado: 'formulario', evidencia: scope.evidence, bloqueos: blocks, ruta: location.pathname,
        firma: fields.length + ':' + hash(shape, 0x811c9dc5) + hash(shape, 0x01234567),
        // Loose identity of the screen: the same fields shown again, under another address or with
        // an error message on top, are still the screen that was already sent.
        huella: hash(fields.map(field => field.tipo + '|' + (field.nombre || field.etiqueta)).join('\n'), 0x811c9dc5),
        // Sending the form accepts terms that no checkbox asks about.
        terminos_implicitos: termsText(text) && !terms.length,
        campos: fields, botones: buttons, titulo: tidy(document.title, 120),
        encabezados: heads.slice(0, 8).map(h => tidy(h.innerText, 100)),
        texto: document.body.innerText.split('\n').map(line => tidy(line, 300))
          .filter(line => line && !optionTexts.has(line)).join('\n').slice(0, 5000)}};
    };

    const first = scan();
    if (action === 'describe') return first.view;
    if (first.view.estado !== 'formulario' || first.view.bloqueos.length || first.view.firma !== ask.firma) return {estado: 'cambio'};
    const wanted = ask.campos || [];
    for (const item of wanted) {
      const described = first.view.campos.find(field => field.id === item.id);
      if (!described || described.tipo !== item.tipo || !first.elements[item.id]) return {estado: 'cambio'};
    }
    const alnum = value => fold(value).replace(/[^a-z0-9]/g, '');
    const same = (a, b) => a === b || (alnum(a) !== '' && alnum(a) === alnum(b));
    const write = (el, value) => {
      const proto = el instanceof HTMLTextAreaElement ? HTMLTextAreaElement.prototype : HTMLInputElement.prototype;
      Object.getOwnPropertyDescriptor(proto, 'value').set.call(el, value);
      el.dispatchEvent(new Event('input', {bubbles: true}));
      el.dispatchEvent(new Event('change', {bubbles: true}));
    };
    const outcome = {};
    for (const item of wanted) {
      const target = first.elements[item.id];
      if (location.origin !== origin) return {estado: 'cambio'};
      if (item.modo === 'clave') {
        // Only the generated password may stand in a password field that is sent.
        if (target.value && target.value !== password) { outcome[item.id] = 'ajeno'; continue; }
        if (target.maxLength >= 0 && password.length > target.maxLength) { outcome[item.id] = 'fallo'; continue; }
        if (target.value !== password) write(target, password);
        outcome[item.id] = target.value === password ? 'escrito' : 'fallo';
      } else if (item.modo === 'marcar') {
        if (!target.checked) HTMLElement.prototype.click.call(target);
        outcome[item.id] = target.checked ? 'marcado' : 'fallo';
      } else if (item.modo === 'opcion' && item.tipo === 'opciones') {
        const radio = target[item.indice];
        if (radio && !radio.checked) HTMLElement.prototype.click.call(radio);
        outcome[item.id] = radio && radio.checked ? 'elegido' : 'fallo';
      } else if (item.modo === 'opcion') {
        const option = (first.elements[item.id + ':opciones'] || [])[item.indice];
        if (option && target.value !== option.value) {
          Object.getOwnPropertyDescriptor(HTMLSelectElement.prototype, 'value').set.call(target, option.value);
          target.dispatchEvent(new Event('input', {bubbles: true}));
          target.dispatchEvent(new Event('change', {bubbles: true}));
        }
        outcome[item.id] = option && target.selectedOptions[0] === option ? 'elegido' : 'fallo';
      } else {
        const candidates = item.candidatos || [];
        if (target.value) {
          // A value somebody already typed is kept, never replaced.
          outcome[item.id] = candidates.some(candidate => same(target.value, candidate)) ? 'igual' : 'ajeno';
          continue;
        }
        outcome[item.id] = 'fallo';
        for (const candidate of candidates) {
          if (target.maxLength >= 0 && candidate.length > target.maxLength) continue;
          write(target, candidate);
          const validity = target.validity;
          if (same(target.value, candidate) && !validity.patternMismatch && !validity.typeMismatch && !validity.badInput &&
              !validity.tooLong && !validity.rangeOverflow && !validity.rangeUnderflow) {
            outcome[item.id] = 'escrito';
            break;
          }
          write(target, '');
        }
      }
    }
    // Everything is read again from the page before any send.
    const state = () => {
      if (location.origin !== origin || location.protocol !== 'https:') return {estado: 'cambio'};
      const now = scan();
      if (now.view.estado !== 'formulario' || now.view.bloqueos.length) return {estado: 'cambio'};
      if (now.view.firma !== ask.firma) return {estado: 'ampliado'};
      const pending = now.view.campos.filter(field => field.obligatorio && !field.lleno).map(field => field.etiqueta);
      const form = now.scope.form;
      if (form && !HTMLFormElement.prototype.checkValidity.call(form)) {
        const invalid = Array.from(formProp(form, 'elements')).find(el => el.willValidate && !el.validity.valid);
        const label = invalid ? labelOf(invalid) : '';
        if (!pending.includes(label)) pending.push(label);
      }
      for (const item of wanted) {
        const target = now.elements[item.id];
        if (item.modo === 'clave' && target.value !== password) pending.push(now.view.campos.find(field => field.id === item.id).etiqueta);
        if (item.exacto && target.value !== item.candidatos[0]) pending.push(now.view.campos.find(field => field.id === item.id).etiqueta);
      }
      // No password field may travel with something the program did not put there, and the
      // account is only ever opened for the e-mail it will be saved under.
      if (now.scope.fields.some(el => el.type === 'password' && el.value !== (password || ''))) pending.push('contraseña');
      if (now.scope.fields.some(el => el.type === 'email' && el.value && el.value !== ask.email)) pending.push('correo');
      const button = now.pressers[ask.boton];
      const described = now.view.botones.find(entry => entry.id === ask.boton);
      // A real submit button of the form sends it natively; anything else is pressed like a person would.
      const owned = Boolean(button && form && (button instanceof HTMLButtonElement || button instanceof HTMLInputElement) &&
        ['submit', 'image'].includes(button.type) && button.form === form);
      const usable = Boolean(button && described && described.en_formulario && !described.externo && !described.prohibido &&
        button.isConnected && shown(button) && !button.disabled && button.getAttribute('aria-disabled') !== 'true' &&
        !leadsAway(button) && (!button.matches('a') || (['', '_self'].includes(button.target) && !button.hasAttribute('download'))) &&
        (!owned || ['', '_self'].includes(button.getAttribute('formtarget') ??
                                         (formProp(form, 'target') || document.querySelector('base[target]')?.target || ''))));
      if (!usable) return {estado: 'pendiente', pendientes: pending, boton: false};
      if (pending.length) return {estado: 'pendiente', pendientes: pending, boton: true};
      return {estado: 'listo', pendientes: [], boton: true, button, form, owned, fields: now.scope.fields};
    };
    const settled = state();
    return {estado: settled.estado, campos: outcome, pendientes: (settled.pendientes || []).slice(0, 5),
      boton: settled.boton !== false, submit: () => {
        const ready = state();
        if (ready.estado !== 'listo') return 'cambio';
        const {button, form, owned} = ready;
        if (!owned) {
          HTMLElement.prototype.click.call(button);
          return 'enviado';
        }
        const method = (button.getAttribute('formmethod') || form.getAttribute('method') || 'get').toLowerCase();
        if (method === 'post') {
          HTMLFormElement.prototype.requestSubmit.call(form, button);
          return 'enviado';
        }
        // Without POST the browser would put the password and the person's data in the address.
        // Only a script of the page may take such a form: the button is pressed and, if no
        // script took the send, the browser's own is called off.
        let fired = false, taken = false;
        const guard = event => {
          if (event.target !== form) return;
          fired = true;
          taken = event.defaultPrevented;
          event.preventDefault();
        };
        window.addEventListener('submit', guard);
        try {
          HTMLElement.prototype.click.call(button);
        } finally {
          window.removeEventListener('submit', guard);
        }
        return fired && !taken ? 'sin_envio' : 'enviado';
      }};
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
    if (blockedText(text) || challengeWidget()) return false;
    const terms = Array.from(form.querySelectorAll('input[type=checkbox]')).filter(el => termsCaption(caption(el)));
    if (terms.some(el => !el.checked) || (termsText(text) && !terms.length)) return false;
    return !Array.from(form.elements).some(privateControl);
  };
  if (!eligible()) return done('Revisa el registro y complétalo en el portal. Puede requerir verificación, condiciones u otros campos.');
  return done('', () => {
    if (!eligible()) return false;
    HTMLFormElement.prototype.requestSubmit.call(form, button);
    return true;
  });
}
