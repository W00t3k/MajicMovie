const CACHE = "majic-v1";
const OFFLINE_URLS = ["/", "/static/styles.css", "/static/app.js", "/static/logo.svg", "/static/manifest.json"];

self.addEventListener("install", e => {
  e.waitUntil(caches.open(CACHE).then(c => c.addAll(OFFLINE_URLS)).then(() => self.skipWaiting()));
});

self.addEventListener("activate", e => {
  e.waitUntil(
    caches.keys().then(keys => Promise.all(keys.filter(k => k !== CACHE).map(k => caches.delete(k))))
      .then(() => self.clients.claim())
  );
});

self.addEventListener("fetch", e => {
  if (e.request.method !== "GET") return;
  const url = new URL(e.request.url);
  if (url.pathname.startsWith("/api/")) return;
  e.respondWith(
    caches.match(e.request).then(cached => {
      const network = fetch(e.request).then(res => {
        if (res.ok) caches.open(CACHE).then(c => c.put(e.request, res.clone()));
        return res;
      }).catch(() => cached);
      return cached || network;
    })
  );
});

self.addEventListener("push", e => {
  if (!e.data) return;
  const data = e.data.json().catch(() => ({ title: "Majic", body: e.data.text() }));
  e.waitUntil(
    data.then(d => self.registration.showNotification(d.title || "Majic Movie Selector", {
      body: d.body || d.message || "",
      icon: "/static/logo.svg",
      badge: "/static/logo.svg",
      data: { url: d.url || "/" },
    }))
  );
});

self.addEventListener("notificationclick", e => {
  e.notification.close();
  const target = e.notification.data?.url || "/";
  e.waitUntil(clients.openWindow(target));
});
