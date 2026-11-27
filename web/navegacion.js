export function installGoogleAccount({api, perform}) {
  const byId = id => document.getElementById(id);
  const account = byId('browser-google');
  let webmail = byId('browser-webmail');
  if (!webmail) {
    webmail = document.createElement('a');
    webmail.id = 'browser-webmail';
    webmail.className = 'secondary button-link';
    webmail.textContent = 'Abrir Gmail en mi Chrome';
    account.after(webmail);
  }
  webmail.href = 'https://mail.google.com/';
  webmail.target = '_blank';
  webmail.rel = 'noopener noreferrer';
  const channel = typeof BroadcastChannel === 'function' ? new BroadcastChannel('facturai-gmail') : null;
  let pending = null;
  let starting = false;

  function message(text, failed = false) {
    const notice = byId('notice');
    notice.textContent = text;
    notice.hidden = false;
    notice.classList.toggle('error', failed);
    return notice;
  }
  function settings(control) {
    if (location.hash === '#ajustes') control.focus();
    else {
      window.addEventListener('hashchange', () => requestAnimationFrame(() => control.focus()), {once: true});
      location.hash = 'ajustes';
    }
  }
  async function checkConnection() {
    const request = pending;
    if (!request) return;
    try {
      const {gmail} = await api('/api/state');
      if (pending !== request) return;
      if (gmail === 'connected' && (!request.alreadyConnected || request.returned)) {
        pending = null;
        await perform(async () => 'Gmail vinculado. FACTURAI puede leer los correos de verificación y las facturas.');
      }
    } catch { /* El siguiente regreso al panel vuelve a consultar el estado. */ }
  }
  window.addEventListener('focus', checkConnection);
  document.addEventListener('visibilitychange', () => { if (!document.hidden) checkConnection(); });
  if (channel) channel.onmessage = ({data}) => {
    if (!pending || !['gmail-ok', 'gmail-error'].includes(data)) return;
    if (data === 'gmail-error') {
      pending = null;
      message('La autorización de Gmail no se completó. Revisa el detalle en la pestaña de Google y vuelve a intentarlo.', true);
    } else {
      pending.returned = true;
      checkConnection();
    }
  };

  async function connect() {
    if (starting) return;
    starting = true;
    // Open synchronously from the click, then sever the opener before visiting Google.
    const tab = window.open('about:blank', '_blank');
    if (tab) tab.opener = null;
    try {
      if (!pending || Date.now() >= pending.expires) {
        const {url} = await api('/api/gmail/connect', 'POST');
        const destination = new URL(url);
        if (destination.origin !== 'https://accounts.google.com' || destination.username || destination.password) {
          throw Error('FACTURAI no recibió una dirección de autorización de Google válida.');
        }
        pending = {url: destination.href, expires: Date.now() + 9 * 60 * 1000,
          alreadyConnected: !byId('gmail-disconnect').hidden};
      }
      if (tab && !tab.closed) {
        tab.location.replace(pending.url);
        message('Autoriza la lectura de Gmail en la otra pestaña y vuelve a FACTURAI para continuar.');
      } else {
        const link = document.createElement('a');
        link.textContent = 'Continuar en Google';
        link.href = pending.url;
        link.target = '_blank';
        link.rel = 'noopener noreferrer';
        message('Chrome bloqueó la pestaña de autorización. ').append(link);
        link.focus();
      }
    } catch (error) {
      if (tab && !tab.closed) tab.close();
      pending = null;
      message(error.message, true);
    } finally {
      starting = false;
    }
  }
  account.onclick = () => {
    if (byId('gmail-connect').disabled && byId('gmail-configure').hidden) {
      // The installation brings its own Google client and it cannot be used: the card says why.
      location.hash = 'ajustes';
      message(byId('gmail-help').textContent, true);
    } else if (byId('gmail-connect').disabled) {
      settings(byId('gmail-configure'));
      message('Para que FACTURAI lea códigos y facturas, importa el cliente OAuth de escritorio y pulsa Conectar Gmail.');
    } else if (!byId('gmail-disconnect').hidden) {
      settings(byId('gmail-connect'));
      message('Gmail ya está vinculado a FACTURAI. Puedes consultar tu buzón con Abrir Gmail en mi Chrome.');
    } else connect();
  };
  return {connect, notifyReturn: outcome => channel?.postMessage(outcome), reset: () => { pending = null; }};
}

export function installNavigation({api, perform, current}) {
  const byId = id => document.getElementById(id);
  const address = byId('browser-url');
  const tabList = byId('browser-tab-list');
  let tabSignature = '';
  let modifying = false;
  let draft = false;
  let lastTab = null;
  let typedGoogle = null;
  let hintSignature = '';
  const portalOrigins = new Map();
  const googleHint = document.createElement('div');
  googleHint.className = 'browser-bar buttons';
  googleHint.id = 'browser-google-hint';
  googleHint.hidden = true;
  const hintText = document.createElement('span');
  const external = document.createElement('a');
  external.className = 'secondary button-link';
  external.target = '_blank';
  external.rel = 'noopener noreferrer';
  const portalLink = external.cloneNode();
  portalLink.hidden = true;
  googleHint.append(hintText, external, portalLink);
  byId('browser-url-form').closest('.browser-bar').after(googleHint);

  function googlePage(value) {
    let url;
    try { url = new URL(value); } catch { return null; }
    if (url.protocol !== 'https:' || url.username || url.password) return null;
    if (['mail.google.com', 'gmail.com', 'www.gmail.com'].includes(url.hostname)) return 'mail';
    if (['accounts.google.com', 'myaccount.google.com'].includes(url.hostname) ||
        (['www.google.com', 'google.com'].includes(url.hostname) && /^\/accounts(?:\/|$)/i.test(url.pathname))) return 'account';
    if (url.searchParams.has('redirect_uri') && url.searchParams.has('client_id') ||
        url.searchParams.has('state') && url.searchParams.has('code') ||
        /\/(?:oauth2?|oidc|openid-connect)\/(?:v\d+\/)?(?:auth|authorize|callback)(?:\/|$)/i.test(url.pathname)) return url.origin;
    return null;
  }
  function showGoogleHint(kind) {
    googleHint.hidden = !kind;
    if (!kind) return;
    const mail = kind === 'mail';
    const merchant = portalOrigins.get(lastTab);
    const generic = kind !== 'account' && !mail;
    const signature = JSON.stringify([kind, merchant]);
    if (signature === hintSignature) return;
    hintSignature = signature;
    hintText.textContent = generic ? 'Para identificarte en tu Chrome, comienza desde la página principal y elige Iniciar sesión.' :
      mail ? 'Abre el buzón en una pestaña de tu Chrome.' :
      'Abre tu cuenta en Chrome. Si llegaste desde un comercio, inicia el acceso desde ese comercio en tu Chrome.';
    // Transfer only a public homepage. The redirect, path, tokens and ticket stay at their origin.
    external.href = generic ? (merchant || kind) + '/' : mail ? 'https://mail.google.com/' : 'https://accounts.google.com/';
    external.textContent = generic ? 'Abrir ' + new URL(external.href).host + ' en Chrome' :
      mail ? 'Abrir Gmail en Chrome' : 'Abrir mi cuenta en Chrome';
    portalLink.hidden = !(kind === 'account' && merchant);
    if (!portalLink.hidden) {
      portalLink.href = merchant + '/';
      portalLink.textContent = 'Abrir ' + new URL(merchant).host + ' en Chrome';
    } else portalLink.removeAttribute('href');
  }

  function update() {
    const {browser, channel, active} = current();
    const personal = browser.mode === 'personal';
    const tabs = personal ? browser.tabs || [] : [];
    const selected = tabs.find(tab => tab.selected);
    const blocked = modifying || Boolean(browser.dialog);
    const canNavigate = personal || (!active && channel && browser.manual);
    for (const id of ['page-back', 'page-forward', 'page-reload']) {
      byId(id).disabled = blocked || !browser.open || !canNavigate;
    }
    for (const id of ['browser-open', 'browser-go', 'browser-return']) {
      byId(id).disabled = blocked;
    }
    byId('wa-open').disabled = blocked || active;
    byId('browser-return').hidden = !(personal && active);
    address.disabled = blocked;
    if (lastTab !== (selected?.id || null)) { draft = false; typedGoogle = null; }
    lastTab = selected?.id || null;
    const google = personal && googlePage(browser.url);
    if (lastTab && !google && /^https:\/\//i.test(browser.url)) {
      const visited = new URL(browser.url);
      if (!['google.com', 'www.google.com'].includes(visited.hostname)) portalOrigins.set(lastTab, visited.origin);
    }
    if (personal) {
      for (const id of portalOrigins.keys()) if (!tabs.some(tab => tab.id === id)) portalOrigins.delete(id);
    }
    showGoogleHint(typedGoogle || google);
    if (!draft && document.activeElement !== address) {
      address.value = google ? external.href : personal && /^https:\/\//i.test(browser.url || '') ? browser.url : '';
    }
    byId('browser-tabs').hidden = !personal;
    byId('browser-new-tab').disabled = blocked || tabs.length >= 12;
    byId('browser-close-tab').disabled = blocked || !selected;
    const signature = JSON.stringify([tabs, blocked]);
    if (signature === tabSignature) return;
    tabSignature = signature;
    const buttons = tabs.map(tab => {
      const button = document.createElement('button');
      button.type = 'button';
      button.className = 'secondary';
      button.textContent = tab.title || (tab.url?.startsWith('https://') ? new URL(tab.url).host : 'Nueva pestaña');
      button.title = button.textContent;
      button.disabled = blocked;
      if (tab.selected) button.setAttribute('aria-current', 'page');
      button.addEventListener('click', () => changeTab('select', tab.id));
      return button;
    });
    tabList.replaceChildren(...buttons);
  }

  function navigate(path, body, focusAddress = false) {
    return perform(async () => {
      if (modifying) return;
      modifying = true;
      const channel = current().channel;
      update();
      try {
        await api(path, 'POST', {...body, channel});
        draft = false;
        address.blur();
      } finally {
        modifying = false;
        update();
      }
      if (focusAddress) {
        address.value = '';
        address.focus();
        address.select();
      }
    });
  }

  function openPersonal(url = '') {
    return navigate('/api/browser/personal', url ? {url} : {});
  }

  function changeTab(action, tabId) {
    return navigate('/api/browser/personal/tabs', tabId ? {action, tab_id: tabId} : {action}, action === 'new');
  }

  address.addEventListener('input', () => { draft = true; typedGoogle = null; address.setCustomValidity(''); });
  address.addEventListener('keydown', event => {
    if (event.key === 'Escape') {
      draft = false;
      address.blur();
      update();
    }
  });
  byId('browser-url-form').onsubmit = event => {
    event.preventDefault();
    const value = address.value.trim();
    if (!value) return;
    function suggestGoogle(kind) {
      typedGoogle = kind;
      showGoogleHint(kind);
      address.value = external.href;
      external.focus();
    }
    if (/^gmail$/i.test(value)) return suggestGoogle('mail');
    if (/^(?:mi )?cuenta de google$/i.test(value)) return suggestGoogle('account');
    const scheme = /^[a-z][a-z\d+.\-]*:/i.test(value) && !/^[^:/\s]+:\d+(?:\/|$)/.test(value);
    const looksLikeAddress = scheme || /[\/\\@]/.test(value) || /^\S+\.\S+(?:\s|$)/.test(value) ||
      /^(?:localhost|\[|\d+\.\d+)/i.test(value);
    if (!looksLikeAddress) {
      const search = new URL('https://www.google.com/search');
      search.searchParams.set('q', value);
      openPersonal(search.href);
      return;
    }
    let url;
    try {
      url = new URL(scheme ? value : 'https://' + value);
      if (url.protocol !== 'https:' || url.username || url.password || !url.hostname) throw Error();
    } catch {
      address.setCustomValidity('Escribe una dirección web HTTPS válida.');
      address.reportValidity();
      return;
    }
    const google = googlePage(url.href);
    if (google) return suggestGoogle(google);
    openPersonal(url.href);
  };
  function returnToJob() {
    return navigate('/api/browser/return', {});
  }
  byId('browser-open').onclick = () => openPersonal();
  byId('browser-return').onclick = returnToJob;
  const gmail = installGoogleAccount({api, perform});
  byId('browser-new-tab').onclick = () => changeTab('new');
  byId('browser-close-tab').onclick = () => {
    const tab = current().browser.tabs?.find(item => item.selected);
    if (tab) changeTab('close', tab.id);
  };
  for (const [id, direction] of [['page-back', 'back'], ['page-forward', 'forward'], ['page-reload', 'reload']]) {
    byId(id).onclick = () => navigate('/api/browser/history', {direction});
  }
  return {update, openPersonal, returnToJob, gmail};
}
