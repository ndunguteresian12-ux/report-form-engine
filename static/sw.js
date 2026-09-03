// Elimu Hub service worker.
//
// Deliberately conservative caching strategy: this app tracks real school
// finance, marks, and academic records, so the priority is correctness
// over "offline-first" bells and whistles. A cached page showing a
// student's fee balance from yesterday, or old marks, would be actively
// harmful — worse than the app simply failing clearly when there's no
// connection.
//
// What IS cached: the app icons and the manifest itself — small, truly
// static files that never change per-request, so caching them only
// speeds up install/load with zero risk of showing stale data.
//
// What is NEVER cached: every actual app page (dashboards, marks entry,
// fee pages, timetable, everything). Those always go straight to the
// network, exactly like a normal website — if there's no connection,
// the request fails visibly instead of silently serving old data.

const CACHE_NAME = 'elimu-hub-static-v1';
const STATIC_ASSETS = [
    '/static/manifest.json',
    '/static/icon-192.png',
    '/static/icon-512.png',
    '/static/icon-512-maskable.png',
];

self.addEventListener('install', (event) => {
    event.waitUntil(
        caches.open(CACHE_NAME).then((cache) => cache.addAll(STATIC_ASSETS))
    );
    // Activate this new service worker immediately rather than waiting
    // for every open tab to close first — paired with clients.claim()
    // below, and the update-check script on each page, so a deployed
    // change reaches users promptly rather than sitting invisible until
    // their next full app restart.
    self.skipWaiting();
});

self.addEventListener('activate', (event) => {
    event.waitUntil(
        caches.keys().then((names) =>
            Promise.all(
                names
                    .filter((name) => name !== CACHE_NAME)
                    .map((name) => caches.delete(name))
            )
        ).then(() => self.clients.claim())
    );
});

self.addEventListener('fetch', (event) => {
    const url = new URL(event.request.url);

    // Only ever serve from cache for the exact static assets listed
    // above. Everything else — every real app page — passes straight
    // through to the network, untouched.
    if (STATIC_ASSETS.includes(url.pathname)) {
        event.respondWith(
            caches.match(event.request).then((cached) => cached || fetch(event.request))
        );
    }
    // No else branch and no event.respondWith() call for anything else —
    // leaving the fetch event alone means the browser handles it exactly
    // as if there were no service worker at all for that request.
});
