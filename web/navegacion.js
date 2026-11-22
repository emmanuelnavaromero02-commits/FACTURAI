export function installNavigation({api, perform, current}) {
  const byId = id => document.getElementById(id);
  const address = byId('browser-url');
  const tabList = byId('browser-tab-list');
  let tabSignature = '';
  let modifying = false;
  let draft = false;
  let lastTab = null;

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
    for (const id of ['browser-open', 'browser-google', 'browser-go', 'browser-return']) {
      byId(id).disabled = blocked;
    }
    byId('wa-open').disabled = blocked || active;
    byId('browser-return').hidden = !(personal && active);
    address.disabled = blocked;
    if (lastTab !== (selected?.id || null)) draft = false;
    lastTab = selected?.id || null;
    if (!draft && document.activeElement !== address) {
      address.value = personal && /^https:\/\//i.test(browser.url || '') ? browser.url : '';
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

  address.addEventListener('input', () => { draft = true; address.setCustomValidity(''); });
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
    openPersonal(url.href);
  };
  function returnToJob() {
    return navigate('/api/browser/return', {});
  }
  byId('browser-open').onclick = () => openPersonal();
  byId('browser-return').onclick = returnToJob;
  byId('browser-google').onclick = () => openPersonal('https://accounts.google.com/');
  byId('browser-new-tab').onclick = () => changeTab('new');
  byId('browser-close-tab').onclick = () => {
    const tab = current().browser.tabs?.find(item => item.selected);
    if (tab) changeTab('close', tab.id);
  };
  for (const [id, direction] of [['page-back', 'back'], ['page-forward', 'forward'], ['page-reload', 'reload']]) {
    byId(id).onclick = () => navigate('/api/browser/history', {direction});
  }
  return {update, openPersonal, returnToJob};
}
