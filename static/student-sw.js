// Elimu Hub Learner Portal service worker — scoped specifically to
// /student/ so it coexists independently from the main staff/admin PWA
// registered elsewhere on the same site.
//
// Same deliberately conservative caching strategy as the main app's
// service worker: this portal shows a learner's actual marks and fee
// balances. Caching those and showing them stale later would be
// actively harmful — worse than the page simply failing visibly when
// there's no connection. Only the manifest and icons are cached; every
// real page (login, dashboard, and anything added to it later) always
// goes straight to the network.

const CACHE_NAME = 'elimu-learner-static-v1';
const STATIC_ASSETS = [
    '/static/student-manifest.json',
    '/static/icon-192.png',
    '/static/icon-512.png',
    '/static/icon-512-maskable.png',
];

self.addEventListener('install', (event) => {
    event.waitUntil(
        caches.open(CACHE_NAME).then((cache) => cache.addAll(STATIC_ASSETS))
    );
    self.skipWaiting();
});

self.addEventListener('activate', (event) => {
    event.waitUntil(
        caches.keys().then((names) =>
            Promise.all(
                names.filter((name) => name !== CACHE_NAME).map((name) => caches.delete(name))
            )
        ).then(() => self.clients.claim())
    );
});

self.addEventListener('fetch', (event) => {
    const url = new URL(event.request.url);
    if (STATIC_ASSETS.includes(url.pathname)) {
        event.respondWith(
            caches.match(event.request).then((cached) => cached || fetch(event.request))
        );
    }
    // Everything else (every real learner-portal page) passes straight
    // through untouched, exactly as if there were no service worker.
});
