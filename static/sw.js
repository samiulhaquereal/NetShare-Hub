// NetShare Hub service worker.
// Network-first for pages so updates are picked up immediately; the cache is
// only an offline fallback. (The previous cache-first strategy served a stale
// "/" forever after the first visit.) API, streams and uploads are never cached.
const CACHE_NAME = 'netshare-v2';
const SHELL = ['/', '/static/style.css', '/static/icon.png', '/static/js/pin-lock.js', '/manifest.json'];

self.addEventListener('install', event => {
  event.waitUntil(caches.open(CACHE_NAME).then(cache => cache.addAll(SHELL)).then(() => self.skipWaiting()));
});

self.addEventListener('activate', event => {
  event.waitUntil(
    caches.keys()
      .then(keys => Promise.all(keys.filter(k => k !== CACHE_NAME).map(k => caches.delete(k))))
      .then(() => self.clients.claim())
  );
});

self.addEventListener('fetch', event => {
  const req = event.request;
  const url = new URL(req.url);
  const cacheable = req.method === 'GET' && url.origin === self.location.origin &&
    (req.mode === 'navigate' || url.pathname.startsWith('/static/') || url.pathname === '/manifest.json');
  if (!cacheable) return; // let the browser handle it normally

  event.respondWith(
    fetch(req)
      .then(res => {
        if (res.ok) {
          const copy = res.clone();
          caches.open(CACHE_NAME).then(cache => cache.put(req, copy));
        }
        return res;
      })
      .catch(() => caches.match(req).then(hit => hit || caches.match('/')))
  );
});
