// Add-on for the panel when it is used behind the entrance for several customers.
// It runs only if the page includes /acceso/panel.js: a way to the account page, and back to
// the sign-in page when the session ends.
const link = document.createElement('a');
link.href = '/cuenta';
link.className = 'inline-link';
link.textContent = 'Mi cuenta: contraseña y cerrar sesión →';
const settings = document.querySelector('#screen-ajustes .screen-header div');
if (settings) settings.append(link);
else document.querySelector('.chat-header')?.append(link);

// Behind the entrance these two personal connections are not offered: the product's own AI
// and a mailbox by IMAP take their place, so their cards stay out of sight.
for (const id of ['pod-connect', 'gmail-configure']) {
  const card = document.getElementById(id)?.closest('section.settings-card');
  if (card) card.hidden = true;
}

const plainFetch = window.fetch.bind(window);
window.fetch = async (...request) => {
  const response = await plainFetch(...request);
  // Only the entrance answers 401, and only when there is no session any more.
  if (response.status === 401 && new URL(response.url).origin === location.origin) location.assign('/entrar' + location.hash);
  return response;
};
