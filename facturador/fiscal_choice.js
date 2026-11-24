control => {
  const clean = s => (s || '').normalize('NFD').replace(/[\u0300-\u036f]/g, '')
    .replace(/([a-z])([A-Z])/g, '$1 $2').toLowerCase().replace(/[^a-z0-9]+/g,' ').trim();
  if (control.closest('.p-dropdown') && !control.matches('.p-dropdown')) return null;
  const named = (control.getAttribute('aria-labelledby') || '').split(/\s+/)
    .map(id => document.getElementById(id)?.innerText || '').join(' ');
  function labelText(label) {
    const copy = label.cloneNode(true);
    copy.querySelectorAll('input,select,textarea,[role=combobox],[role=listbox]').forEach(child => child.remove());
    return copy.textContent;
  }
  const labels = [...(control.labels || [])].map(labelText);
  if (control.id) labels.push(...[...document.querySelectorAll('label[for]')]
    .filter(label => label.htmlFor === control.id).map(labelText));
  const native = control.matches('select') ? control : control.querySelector('select');
  const name = clean([control.id, native?.name, control.getAttribute('name'), named,
    control.getAttribute('aria-label'), ...labels].filter(Boolean).join(' '));
  if (/\b(emisor|aduanero|proveedor)\b/.test(name)) return null;
  let kind;
  if (/\bregimen(?: fiscal| receptor| del receptor)?\b/.test(name)) kind = 'regime';
  else if (/\buso (?:del? )?(?:cfdi|factura)\b/.test(name)) kind = 'use';
  else if (/\b(?:forma|tipo|medio) (?:de )?pago\b|\bpayment (?:method|type)\b/.test(name) || name === 'pago') kind = 'payment';
  if (!kind) return null;
  const primeLabel = control.querySelector('.p-dropdown-label');
  const current = native?.selectedOptions[0];
  return {kind, native: native === control, hasBacking: Boolean(native),
    disabled: control.matches(':disabled,[aria-disabled=true],.p-disabled') || Boolean(native?.disabled),
    value: current?.value || '', label: current?.label || '',
    display: primeLabel?.textContent || (control.matches('select') ? '' :
      control.getAttribute('aria-valuetext') || control.value || control.innerText),
    expanded: control.getAttribute('aria-expanded') === 'true'};
}
