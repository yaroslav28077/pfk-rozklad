/* Service worker: працює офлайн (останній завантажений розклад), дані — спершу з мережі */
const CACHE = "rozklad-v2";
const SHELL = ["./", "index.html", "style.css", "app.js", "manifest.webmanifest", "icon.svg"];

self.addEventListener("install", (e) => {
  e.waitUntil(caches.open(CACHE).then((c) => c.addAll(SHELL)).then(() => self.skipWaiting()));
});
self.addEventListener("activate", (e) => {
  e.waitUntil(caches.keys().then((ks) => Promise.all(ks.filter((k) => k !== CACHE).map((k) => caches.delete(k)))).then(() => self.clients.claim()));
});
self.addEventListener("fetch", (e) => {
  const url = new URL(e.request.url);
  if (e.request.method !== "GET" || url.origin !== location.origin) return;
  const isData = url.pathname.endsWith("site.json") || url.pathname.endsWith(".ics") || url.pathname.endsWith(".png");
  if (isData || url.pathname.endsWith("/") || url.pathname.endsWith(".html")) {
    // network-first
    e.respondWith(fetch(e.request).then((r) => {
      const copy = r.clone();
      caches.open(CACHE).then((c) => c.put(url.pathname.endsWith("site.json") ? "site.json" : e.request, copy));
      return r;
    }).catch(() => caches.match(url.pathname.endsWith("site.json") ? "site.json" : e.request).then((r) => r || caches.match("index.html"))));
    return;
  }
  // cache-first для статики
  e.respondWith(caches.match(e.request).then((r) => r || fetch(e.request).then((resp) => {
    const copy = resp.clone(); caches.open(CACHE).then((c) => c.put(e.request, copy)); return resp;
  })));
});
