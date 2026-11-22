({origin, operation = 'inspect', value = ''}) => {
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
