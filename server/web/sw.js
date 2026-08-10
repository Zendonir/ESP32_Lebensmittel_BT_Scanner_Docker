// Minimaler Service Worker: nur der App-Rahmen wird gecacht, niemals Daten.
// Ein Cache mit Inventardaten waere schlimmer als gar keiner - man wuerde
// Artikel auslagern, die es laengst nicht mehr gibt.
const CACHE = 'ls-shell-v2';
const SHELL = ['/mobile', '/static/css/app.css', '/static/js/api.js', '/manifest.json'];

self.addEventListener('install', (event) => {
  event.waitUntil(caches.open(CACHE).then((c) => c.addAll(SHELL)).then(() => self.skipWaiting()));
});

self.addEventListener('activate', (event) => {
  event.waitUntil(
    caches.keys()
      .then((keys) => Promise.all(keys.filter((k) => k !== CACHE).map((k) => caches.delete(k))))
      .then(() => self.clients.claim())
  );
});

self.addEventListener('fetch', (event) => {
  const url = new URL(event.request.url);
  if (event.request.method !== 'GET' || url.pathname.startsWith('/api/') || url.pathname.startsWith('/ws/')) {
    return; // Netz-only: Daten kommen immer frisch vom Server.
  }
  event.respondWith(
    fetch(event.request)
      .then((res) => {
        const copy = res.clone();
        caches.open(CACHE).then((c) => c.put(event.request, copy)).catch(() => {});
        return res;
      })
      .catch(() => caches.match(event.request).then((hit) => hit || caches.match('/mobile')))
  );
});
