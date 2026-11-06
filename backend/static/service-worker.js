const CACHE_NAME = "fluir-shell-v3";
const OFFLINE_PAGE = "/static/offline.html";
const APP_ASSETS = [
    OFFLINE_PAGE,
    "/static/css/styles.css?v=20261106-1",
    "/static/js/app.js?v=20261106-1",
];

self.addEventListener("install", (event) => {
    event.waitUntil(
        caches.open(CACHE_NAME)
            .then((cache) => cache.addAll(APP_ASSETS))
            .then(() => self.skipWaiting()),
    );
});

self.addEventListener("activate", (event) => {
    event.waitUntil((async () => {
        const keys = await caches.keys();
        await Promise.all(keys.filter((key) => key.startsWith("fluir-shell-") && key !== CACHE_NAME).map((key) => caches.delete(key)));
        await self.clients.claim();
    })());
});

self.addEventListener("fetch", (event) => {
    const request = event.request;
    const url = new URL(request.url);
    if (request.method !== "GET" || url.origin !== self.location.origin) return;

    if (request.mode === "navigate") {
        event.respondWith(fetch(request).catch(async () => {
            const cachedOfflinePage = await caches.match(OFFLINE_PAGE);
            return cachedOfflinePage || new Response("Você está sem conexão. Reconecte-se e tente novamente.", {
                status: 503,
                headers: { "Content-Type": "text/plain; charset=utf-8" },
            });
        }));
        return;
    }

    if (url.pathname.startsWith("/static/")) {
        event.respondWith((async () => {
            const cache = await caches.open(CACHE_NAME);
            const cached = await cache.match(request);
            const network = fetch(request).then((response) => {
                if (response.ok && response.type === "basic") cache.put(request, response.clone());
                return response;
            });
            return cached || network.catch(() => new Response("", { status: 503 }));
        })());
    }
});
