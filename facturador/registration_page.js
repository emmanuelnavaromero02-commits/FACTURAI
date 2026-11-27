({origin, operation = 'inspect', value = '', marks = []}) => {
  if (location.origin !== origin || location.protocol !== 'https:') return 'other';
  const norm = s => String(s || '').normalize('NFD').replace(/[\u0300-\u036f]/g, '').toLowerCase().replace(/\s+/g, ' ').trim();
  const visible = el => el.getClientRects().length && getComputedStyle(el).visibility === 'visible';
  const name = el => norm(el.getAttribute('aria-label') || el.innerText || el.value);
  const caption = el => norm([el.getAttribute('aria-label'), el.placeholder, el.name, el.id,
    ...Array.from(el.labels || [], label => label.textContent)].join(' '));
  const inputs = Array.from(document.querySelectorAll('input')).filter(visible);
  const buttons = Array.from(document.querySelectorAll('button, input[type=submit]')).filter(visible);
  const same = address => {try {const u = new URL(address, document.baseURI);
    return u.origin === origin && !u.username && !u.password;} catch {return false;}};
  const signup = /^(crear( mi| una)? cuenta|registrarme|registrar(me)?( ahora)?|registro de usuario|crear usuario|create( an| my)? account|sign up)$/;
  const login = /^(iniciar sesion|ingresar|acceder|entrar|login|log in|sign in)$/;
  const change = /^(cambiar contrasena|guardar (?:mi |nueva )?contrasena|actualizar contrasena|change password)$/;
  const verify = /^(verificar(?: (?:mi |el )?(?:correo|cuenta|codigo))?|confirmar (?:mi )?(?:registro|correo|cuenta)|activar (?:mi )?cuenta|validar codigo|verify email)$/;
  const text = norm(document.body.innerText).slice(0, 24000);
  const registration = /activa(?:r|cion)? (?:tu |su |la )?cuenta|activacion de (?:tu |la )?cuenta|confirm(?:a|ar) (?:tu |el )?registro|verifica(?:r)? (?:tu |su )?correo|verificacion de (?:tu |su )?correo|registro de (?:cuenta|usuario)|completa (?:tu |el )?registro|verify your email|codigo de activacion/.test(text);
  const sensitive = /doble factor|dos (?:pasos|factores)|segundo factor|autenticador|codigo de (?:inicio de sesion|acceso|seguridad)|restablecer (?:tu |la )?contrasena|recuperar (?:tu |la )?contrasena|two.factor|multi.factor/.test(text);
  const hasPassword = inputs.some(el => el.type === 'password');
  const hasCode = inputs.some(el => /one-time-code/.test(el.autocomplete) || /otp|codigo|verification code/.test(caption(el)));
  if (['phone_code', 'submit_phone_code', 'access', 'submit_access', 'signup_link', 'open_signup_wide'].includes(operation)) {
    // Screens the strict rules below leave to the person: a code sent to the phone, a sign-in in
    // two steps or behind a scripted button, and a sign-up link under another name. Each
    // operation answers for itself only, so «inspect» keeps classifying pages exactly as before.
    if (/captcha|soy humano|no soy un robot|verifica que eres humano/.test(text) || document.querySelector(
        'iframe[src*="captcha" i], iframe[src*="challenges.cloudflare" i], [data-sitekey], [name*="captcha" i]')) return 'other';
    // A page that shows the ticket being billed is part of its invoice, not of an account.
    if (marks.some(mark => new RegExp('(?<![\\w-])' + norm(mark).replace(/[.*+?^${}()|[\]\\]/g, '\\$&') + '(?![\\w-])').test(text))) return 'other';
    const external = /\b(google|facebook|apple|microsoft|twitter|linkedin|github|outlook|yahoo|amazon)\b/;
    const notOurs = /pag(ar|o|ue)\b|compr(ar|a)\b|factur|emit|timbr|cfdi|donar|suscrib/;
    const label = el => name(el).replace(/^[^a-z0-9]+|[^a-z0-9]+$/g, '');
    // «Entrar con Google» and the like are never candidates, whatever the page offers.
    const pressable = Array.from(document.querySelectorAll('button, input[type=submit], input[type=button], [role=button], a'))
      .filter(el => visible(el) && !el.disabled && el.getAttribute('aria-disabled') !== 'true' && label(el) &&
        !external.test(label(el)) && !notOurs.test(label(el)));
    const typed = inputs.filter(el => !el.disabled && !el.readOnly &&
      ['text', 'email', 'tel', 'number', 'password'].includes(el.type));
    const scopeOf = fields => {
      if (!fields.length || fields.some(field => field.form !== fields[0].form)) return null;
      if (fields[0].form) return fields[0].form;
      let node = fields[0].parentElement;
      while (node && node !== document.body &&
             !(fields.every(field => node.contains(field)) && pressable.some(el => node.contains(el)))) node = node.parentElement;
      return node || document.body;
    };
    const within = scope => pressable.filter(el => scope.contains(el) || (scope instanceof HTMLFormElement && el.form === scope));
    const wording = scope => {
      const heads = Array.from(document.querySelectorAll('h1, h2, h3, h4, legend, [role=heading]'))
        .filter(head => visible(head) && !head.closest('nav, footer, aside'));
      const above = heads.filter(head => !scope.contains(head) &&
        (head.compareDocumentPosition(scope) & Node.DOCUMENT_POSITION_FOLLOWING)).slice(-1);
      return [...above, ...heads.filter(head => scope.contains(head))].map(head => norm(head.innerText))
        .concat(scope instanceof HTMLFormElement
          ? [norm(['aria-label', 'id', 'name', 'action'].map(key => scope.getAttribute(key) || '').join(' '))] : []);
    };
    const goodLink = el => {
      const href = el.getAttribute('href') || '';
      return (href === '' || href.startsWith('#') || /^javascript:/i.test(href) || same(href)) &&
        ['', '_self'].includes(el.target) && !el.hasAttribute('download');
    };
    const write = (el, text) => {
      Object.getOwnPropertyDescriptor(HTMLInputElement.prototype, 'value').set.call(el, text);
      el.dispatchEvent(new Event('input', {bubbles: true}));
      el.dispatchEvent(new Event('change', {bubbles: true}));
    };
    const press = (el, secret) => {
      const form = el.form;
      const owned = form && (el instanceof HTMLButtonElement || el instanceof HTMLInputElement) && ['submit', 'image'].includes(el.type);
      if (!owned) {
        if (el.matches('a') && !goodLink(el)) return false;
        el.click();
        return true;
      }
      if (!same(form.action) || !same(el.getAttribute('formaction') || form.action) || !form.checkValidity() ||
          !['', '_self'].includes(el.getAttribute('formtarget') ?? (form.target || document.querySelector('base[target]')?.target || '')) ||
          Array.from(form.elements).some(other => (other.hasAttribute('formaction') && !same(other.formAction)) ||
            other.type === 'file' || (other.autocomplete || '').startsWith('cc-'))) return false;
      if (secret && (el.getAttribute('formmethod') || form.getAttribute('method') || 'get').toLowerCase() !== 'post') {
        // Without POST the browser would put the secret in the address. Only a script of the page
        // may take such a form: if none takes the send, the browser's own is called off.
        let fired = false, taken = false;
        const guard = event => {
          if (event.target !== form) return;
          fired = true;
          taken = event.defaultPrevented;
          event.preventDefault();
        };
        window.addEventListener('submit', guard);
        try {
          el.click();
        } finally {
          window.removeEventListener('submit', guard);
        }
        return !fired || taken;
      }
      el.click();
      return true;
    };
    const accessStage = () => {
      if (hasCode || /dos pasos|doble factor|segundo factor|two.factor|multi.factor/.test(text)) return null;
      const secrets = typed.filter(el => el.type === 'password');
      const ids = typed.filter(el => el.type !== 'password' && (el.type === 'email' || /username|email/.test(el.autocomplete) ||
        /correo|e-?mail|usuario|username/.test(caption(el))));
      if (!typed.length || typed.length !== secrets.length + ids.length || secrets.length > 1 || ids.length > 1) return null;
      if (secrets.length && (secrets[0].autocomplete === 'new-password' ||
                             /confirm|repet|nuev|new.?pass/.test(caption(secrets[0])))) return null;
      const scope = scopeOf(typed);
      if (!scope) return null;
      const next = within(scope).filter(el =>
        /^(iniciar sesion|inicia sesion|ingresar|acceder|entrar|login|log in|sign in|siguiente|continuar|next|continue)$/.test(label(el)));
      if (next.length !== 1) return null;
      // An e-mail field alone is a sign-in step only where the page itself says so, and a form
      // that only speaks of opening an account is not a sign-in at all.
      const says = /iniciar sesion|inicia sesion|inicio de sesion|ingresa a tu cuenta|accede a tu cuenta|acceso (a|de) (clientes|usuarios|tu cuenta)|\blogin\b|log in|sign in/;
      const opens = /crear? (tu |una |mi |su )?cuenta|registr|nuevo usuario|nueva cuenta|sign ?up|create (an |your |my )?account/;
      const words = wording(scope);
      const signIn = words.some(line => says.test(line)) || /login|signin|sign-in|iniciar-?sesion/i.test(location.pathname);
      if (!signIn && (!secrets.length || words.some(line => opens.test(line)))) return null;
      return {stage: secrets.length ? (ids.length ? 'login' : 'password') : 'identifier', secrets, ids, next: next[0]};
    };
    if (operation === 'phone_code' || operation === 'submit_phone_code') {
      const plain = String(document.body.innerText).normalize('NFD').replace(/[\u0300-\u036f]/g, '').toLowerCase().slice(0, 24000);
      const channel = '(?:sms|whatsapp|mensaje de texto|celular|telefono|movil)';
      const word = '(?:codigo|clave|\\bpin\\b|token)';
      // The code and the phone must be named in the same sentence; a support number elsewhere does not count.
      if (!new RegExp(word + '[^.\\n]{0,140}' + channel + '|' + channel + '[^.\\n]{0,140}' + word).test(plain) ||
          /(?:codigo|clave|\bpin\b)[^.\n]{0,140}(?:correo|e-?mail|bandeja)|(?:correo|e-?mail|bandeja)[^.\n]{0,140}(?:codigo|clave|\bpin\b)/.test(plain) ||
          /autenticador|authenticator|aplicacion de autenticacion|app de autenticacion|codigo de recuperacion|recovery code/.test(plain)) return 'other';
      // A postal, coupon or billing code is another kind of code: it is never taken for this one.
      const codeLike = el => /one-time-code/.test(el.autocomplete) || (/otp|codigo|code|\bpin\b|token|verificacion/.test(caption(el)) &&
        !/postal|\bcp\b|zip|cupon|promo|descuento|factura|ticket|barras|referencia|producto|cliente|sucursal|pais|\blada\b/.test(caption(el)));
      const boxes = typed.filter(el => el.maxLength === 1);
      const single = typed.filter(el => el.maxLength !== 1 && codeLike(el));
      if (typed.length !== boxes.length + single.length) return 'other';
      let targets = null;
      if (single.length === 1 && !boxes.length) targets = single;
      else if (!single.length && boxes.length >= 4 && boxes.length <= 8) targets = boxes;
      const scope = targets && scopeOf(targets);
      if (!scope) return 'other';
      const next = within(scope).filter(el =>
        /^(verificar( (el |mi )?(codigo|cuenta|telefono))?|confirmar( (el |mi )?(codigo|telefono))?|validar( (el )?codigo)?|continuar|enviar|siguiente|aceptar|ingresar|entrar|activar( (mi )?cuenta)?|iniciar sesion|verify|confirm|continue|submit|next)$/.test(label(el)));
      if (next.length > 1 || (!next.length && targets.length === 1)) return 'other';
      if (operation === 'phone_code') return 'phone_code';
      if (!/^[0-9a-z]{4,10}$/i.test(value) || targets.some(el => el.value) ||
          (targets.length > 1 ? targets.length !== value.length : targets[0].maxLength > 0 && value.length > targets[0].maxLength)) return 'hold';
      const parts = targets.length > 1 ? value.split('') : [value];
      targets.forEach((el, index) => write(el, parts[index]));
      if (targets.some((el, index) => el.value !== parts[index])) return 'hold';
      // A row of digit boxes without a button sends itself once the last one is typed.
      if (!next.length) return 'sent';
      return press(next[0], true) ? 'sent' : 'hold';
    }
    if (operation === 'access' || operation === 'submit_access') {
      const found = accessStage();
      if (!found) return 'other';
      if (operation === 'access') return found.stage;
      if (found.ids.length) {
        const field = found.ids[0];
        if (!value || (field.value && field.value !== value)) return 'hold';
        if (field.value !== value) write(field, value);
        if (field.value !== value) return 'hold';
      }
      if (found.secrets.length && !found.secrets[0].value) return 'hold';
      return press(found.next, Boolean(found.secrets.length)) ? 'sent' : 'hold';
    }
    const wideSignup = /^((aun )?no tienes (una )?cuenta\??,? ?)?(crear?( mi| una| tu)? cuenta( nueva| ahora| gratis)?|registrarme( ahora| aqui)?|registrarse|registrate( ahora| aqui| gratis)?|registrar(me)?( ahora)?|registro|crear usuario|nuevo usuario|soy nuevo|quiero registrarme|darme de alta|date de alta|create( an| my| your)? account|sign ?up|register)$/;
    const openers = pressable.filter(el => wideSignup.test(label(el)) &&
      (el.matches('a') ? goodLink(el) : !(el.form && ['submit', 'image'].includes(el.type))));
    // The same destination offered twice (menu and page) counts once.
    const places = new Map(openers.map(el => [el.matches('a[href]') ? el.href : label(el), el]));
    const wall = hasPassword || Boolean(accessStage()) ||
      /(?:para (?:poder )?facturar.{0,55}(?:registra|inici[ae] sesion|necesita.{0,12}cuenta)|registro obligatorio para facturar)/.test(text);
    if (places.size !== 1 || !wall) return 'other';
    if (operation === 'signup_link') return 'signup_link';
    return press([...places.values()][0], false) ? 'sent' : 'hold';
  }
  // Recovery links in a login form are common; a dedicated MFA/reset form stays with its owner.
  if (hasCode && sensitive || /(?:dos pasos|doble factor|two.factor|captcha|soy humano|no soy un robot)/.test(text)
      && (hasCode || hasPassword)) return 'protected';
  const matches = regex => buttons.filter(el => regex.test(name(el)));
  const formButton = (button, ready) => {
    const form = button?.form;
    if (!form || !button.isConnected || button.disabled || !same(form.action) ||
        !same(button.getAttribute('formaction') || form.action) ||
        (button.getAttribute('formmethod') || form.method).toLowerCase() !== 'post' ||
        !['', '_self'].includes(button.getAttribute('formtarget') ?? (form.target || document.querySelector('base[target]')?.target || '')) ||
        form.noValidate || button.formNoValidate || !['submit','image'].includes(button.type)) return false;
    const controls = Array.from(form.elements);
    if (controls.some(el => (el.hasAttribute('formaction') && !same(el.formAction)) ||
        el.type === 'file' || (el.autocomplete || '').startsWith('cc-'))) return false;
    if (/pagar|comprar|emitir factura|timbrar|acepto (los |las )?(terminos|condiciones)|terms of service/.test(norm(form.innerText))) return false;
    return !ready || form.checkValidity();
  };
  const registrationButtons = matches(signup);
  if (registrationButtons.length === 1 && formButton(registrationButtons[0], false) &&
      inputs.some(el => el.form === registrationButtons[0].form && (el.type === 'email' || /correo|email/.test(caption(el))))) return 'signup';
  const signupLinks = Array.from(document.querySelectorAll('a[href]')).filter(el => visible(el) &&
    signup.test(name(el)) && same(el.href) && ['', '_self'].includes(el.target) && !el.hasAttribute('download'));
  if (!hasPassword && signupLinks.length === 1 &&
      /(?:para (?:poder )?facturar.{0,55}(?:registra|inici[ae] sesion|necesita.{0,12}cuenta)|registro obligatorio para facturar)/.test(text)) {
    if (operation === 'open_signup') {signupLinks[0].click(); return 'sent';}
    return 'required_signup';
  }
  const loginButtons = matches(login);
  if (hasPassword && loginButtons.length === 1 && formButton(loginButtons[0], false)) {
    if (operation === 'open_signup') {
      if (signupLinks.length !== 1) return 'hold';
      signupLinks[0].click();
      return 'sent';
    }
    if (operation === 'submit_login') {
      if (!formButton(loginButtons[0], true)) return 'hold';
      loginButtons[0].click();
      return 'sent';
    }
    return signupLinks.length === 1 ? 'login_signup' : 'login';
  }
  const changing = matches(change);
  if (changing.length === 1 && hasPassword && formButton(changing[0], false) && /temporal|provisional|temporary password/.test(text)) {
    if (operation === 'submit_change') {
      if (!formButton(changing[0], true)) return 'hold';
      changing[0].click();
      return 'sent';
    }
    return 'change';
  }
  if (registration && !sensitive) {
    const checking = matches(verify);
    if (checking.length === 1 && formButton(checking[0], false) && hasCode) {
      if (operation === 'submit_code' || operation === 'fill_code') {
        const codes = inputs.filter(el => el.form === checking[0].form && !el.readOnly && !el.disabled &&
          ['number', 'text', 'tel'].includes(el.type) && (/one-time-code/.test(el.autocomplete) || /codigo|verification code/.test(caption(el))));
        if (codes.length !== 1 || !/^[0-9]{4,10}$/.test(value) || (codes[0].value && codes[0].value !== value)) return 'hold';
        Object.getOwnPropertyDescriptor(HTMLInputElement.prototype, 'value').set.call(codes[0], value);
        codes[0].dispatchEvent(new Event('input', {bubbles:true}));
        codes[0].dispatchEvent(new Event('change', {bubbles:true}));
        if (codes[0].value !== value) return 'hold';
        if (operation === 'fill_code') return 'filled';
        if (!formButton(checking[0], true)) return 'hold';
        checking[0].click();
        return 'sent';
      }
      return 'code';
    }
    if (/enviamos|enviado|revisa (?:tu |su )?(?:bandeja|correo)|check your (?:email|inbox)/.test(text)) return 'mail';
  }
  if (hasPassword || hasCode && registration) return 'protected';
  return 'other';
}
