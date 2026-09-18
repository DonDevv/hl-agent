/* Minimal service worker: network first, cached shell as offline fallback. API calls are never cached. */
const CACHE = "hl-agent-v15";
const SHELL = ["/", "/static/app.css", "/static/app.js", "/static/logo.png", "/manifest.webmanifest"];

self.addEventListener("install", (e) => {
  e.waitUntil(caches.open(CACHE).then((c) => c.addAll(SHELL)).then(() => self.skipWaiting()));
});
self.addEventListener("activate", (e) => {
  e.waitUntil(
    caches.keys().then((keys) => Promise.all(keys.filter((k) => k !== CACHE).map((k) => caches.delete(k))))
      .then(() => self.clients.claim())
  );
});
self.addEventListener("fetch", (e) => {
  const url = new URL(e.request.url);
  if (e.request.method !== "GET" || url.pathname.startsWith("/api/")) return;
  e.respondWith(
    fetch(e.request)
      .then((res) => {
        const copy = res.clone();
        caches.open(CACHE).then((c) => c.put(e.request, copy));
        return res;
      })
      .catch(() => caches.match(e.request))
  );
});

/* Web Push: the server sends {title, body, url, tag}; a tap focuses the app on that run. */
self.addEventListener("push", (e) => {
  let d = {};
  try { d = e.data ? e.data.json() : {}; } catch (_) { d = { title: "hl-agent", body: e.data && e.data.text() }; }
  e.waitUntil(
    self.registration.showNotification(d.title || "hl-agent", {
      body: d.body || "",
      tag: d.tag || undefined,
      icon: "/static/icon-180.png",
      badge: "/static/icon-180.png",
      data: { url: d.url || "/" },
    })
  );
});
self.addEventListener("notificationclick", (e) => {
  e.notification.close();
  const url = new URL((e.notification.data && e.notification.data.url) || "/", self.location.origin).href;
  e.waitUntil(
    self.clients.matchAll({ type: "window", includeUncontrolled: true }).then((list) => {
      const c = list[0];
      if (c) return c.navigate(url).then((w) => (w || c).focus());
      return self.clients.openWindow(url);
    })
  );
});
