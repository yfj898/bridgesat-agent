const CACHE_NAME = "bridgesat-shell-v4";
const PACK_CACHE_NAME = "bridgesat-packs-v1";
const APP_SHELL = ["/", "/styles.css", "/app.js", "/offline.js", "/offline-core.js", "/manifest.webmanifest"];
const PACK_URL_PREFIX = "/v1/content-packs";

self.addEventListener("install", (event) => {
  event.waitUntil(
    Promise.all([
      caches.open(CACHE_NAME).then((cache) => cache.addAll(APP_SHELL)),
      caches.open(PACK_CACHE_NAME),
    ])
  );
  self.skipWaiting();
});

self.addEventListener("activate", (event) => {
  event.waitUntil(
    caches
      .keys()
      .then((keys) =>
        Promise.all(
          keys
            .filter((key) => key !== CACHE_NAME && key !== PACK_CACHE_NAME)
            .map((key) => caches.delete(key))
        )
      )
  );
  self.clients.claim();
});

async function fetchWithPackCache(request) {
  const cached = await caches.match(request, { cacheName: PACK_CACHE_NAME });
  if (cached) return cached;
  try {
    const response = await fetch(request);
    if (response.ok) {
      const copy = response.clone();
      caches.open(PACK_CACHE_NAME).then((cache) => cache.put(request, copy));
    }
    return response;
  } catch (error) {
    return caches.match("/", { cacheName: CACHE_NAME });
  }
}

async function fetchPackListing(request) {
  const cache = await caches.open(PACK_CACHE_NAME);
  try {
    const response = await fetch(request);
    if (response.ok) await cache.put(request, response.clone());
    return response;
  } catch (_error) {
    return (await cache.match(request)) || caches.match("/", { cacheName: CACHE_NAME });
  }
}

self.addEventListener("fetch", (event) => {
  if (event.request.method !== "GET") return;
  if (event.request.url.includes(PACK_URL_PREFIX)) {
    const path = new URL(event.request.url).pathname;
    event.respondWith(
      path === PACK_URL_PREFIX
        ? fetchPackListing(event.request)
        : fetchWithPackCache(event.request)
    );
    return;
  }
  event.respondWith(
    fetch(event.request)
      .then((response) => {
        const copy = response.clone();
        caches.open(CACHE_NAME).then((cache) => cache.put(event.request, copy));
        return response;
      })
      .catch(() => caches.match(event.request).then((cached) => cached || caches.match("/")))
  );
});
