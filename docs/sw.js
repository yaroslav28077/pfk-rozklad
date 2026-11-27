/* Service worker: працює офлайн (останній завантажений розклад), дані й код інтерфейсу — спершу з мережі */
const CACHE = "rozklad-v12";
const SHELL = [
  "./",
  "index.html",
  "manifest.webmanifest",
  "icon.svg",
  "logo.png",
  "icon-192.png",
  "fonts/manrope-cyrillic.woff2",
  "fonts/manrope-latin.woff2",
];

self.addEventListener("install", (e) => {
  e.waitUntil(caches.open(CACHE).then((c) => c.addAll(SHELL)).then(() => self.skipWaiting()));
});
self.addEventListener("activate", (e) => {
  e.waitUntil(caches.keys().then((ks) => Promise.all(ks.filter((k) => k !== CACHE).map((k) => caches.delete(k)))).then(() => self.clients.claim()));
});
self.addEventListener("fetch", (e) => {
  const url = new URL(e.request.url);
  if (e.request.method !== "GET" || url.origin !== location.origin) return;
  const isFresh = url.pathname.includes("/data/") || url.pathname.endsWith(".ics")
    || url.pathname.endsWith("/") || url.pathname.endsWith(".html")
    || url.pathname.endsWith(".js") || url.pathname.endsWith(".css");
  if (isFresh) {
    // network-first
    e.respondWith(fetch(e.request).then((r) => {
      const copy = r.clone();
      const key = url.origin + url.pathname; // без ?параметрів — щоб офлайн знаходився останній збережений
      caches.open(CACHE).then((c) => c.put(key, copy));
      return r;
    }).catch(() => caches.match(url.origin + url.pathname).then((r) => r || caches.match("index.html"))));
    return;
  }
  // cache-first для іконок, шрифтів і маніфесту
  e.respondWith(caches.match(e.request).then((r) => r || fetch(e.request).then((resp) => {
    const copy = resp.clone(); caches.open(CACHE).then((c) => c.put(e.request, copy)); return resp;
  })));
});
