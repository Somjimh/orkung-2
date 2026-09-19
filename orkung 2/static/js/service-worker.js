// Minimal app-shell cache so the interface (not live data) can open when
// offline or on a flaky farm connection. Data itself always goes through
// the network (see README for the offline scope of this version).
const CACHE = "orkung-shell-v1";
const SHELL = [
  "/static/css/style.css",
  "/static/js/app.js",
  "/static/manifest.json",
  "/static/icons/icon-192.svg",
  "/static/icons/icon-512.svg",
];

self.addEventListener("install", (event) => {
  event.waitUntil(caches.open(CACHE).then((c) => c.addAll(SHELL)));
  self.skipWaiting();
});

self.addEventListener("activate", (event) => {
  event.waitUntil(
    caches.keys().then((keys) => Promise.all(keys.filter((k) => k !== CACHE).map((k) => caches.delete(k))))
  );
  self.clients.claim();
});

self.addEventListener("fetch", (event) => {
  const req = event.request;
  if (req.method !== "GET") return; // never intercept data-changing requests
  const url = new URL(req.url);
  if (!SHELL.includes(url.pathname)) return; // only serve the static shell from cache
  event.respondWith(
    caches.match(req).then((cached) => cached || fetch(req))
  );
});
