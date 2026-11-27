// Minimal service worker: shows the panel's notices and opens the panel on tap.
// It caches nothing: every page and every API answer always comes from the panel itself.
'use strict';

self.addEventListener('install', () => self.skipWaiting());
self.addEventListener('activate', event => event.waitUntil(self.clients.claim()));

function sameOriginLink(value) {
  try {
    const url = new URL(value || '/', self.location.origin);
    return url.origin === self.location.origin ? url.href : new URL('/', self.location.origin).href;
  } catch {
    return new URL('/', self.location.origin).href;
  }
}

self.addEventListener('push', event => {
  let data = {};
  try { data = event.data ? event.data.json() : {}; } catch { data = {}; }
  const title = typeof data.titulo === 'string' && data.titulo ? data.titulo.slice(0, 80) : 'FACTURAI te necesita';
  const options = {
    body: typeof data.cuerpo === 'string' ? data.cuerpo.slice(0, 200) : 'Abre el panel para ver el aviso.',
    tag: typeof data.etiqueta === 'string' && data.etiqueta ? 'facturai-' + data.etiqueta.slice(0, 64) : undefined,
    renotify: true,
    icon: '/icono-192.png',
    badge: '/icono-192.png',
    lang: 'es',
    data: {liga: sameOriginLink(data.liga)}
  };
  event.waitUntil((async () => {
    await self.registration.showNotification(title, options);
    const windows = await self.clients.matchAll({type: 'window', includeUncontrolled: true});
    for (const client of windows) client.postMessage({tipo: 'facturai-aviso-nuevo'});
  })());
});

self.addEventListener('notificationclick', event => {
  event.notification.close();
  const target = sameOriginLink(event.notification.data && event.notification.data.liga);
  event.waitUntil((async () => {
    const windows = await self.clients.matchAll({type: 'window', includeUncontrolled: true});
    const panel = windows.find(client => new URL(client.url).origin === self.location.origin);
    if (panel) {
      await panel.focus();
      panel.postMessage({tipo: 'facturai-abrir', liga: target});
      return;
    }
    await self.clients.openWindow(target);
  })());
});
