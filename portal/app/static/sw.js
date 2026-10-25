// Service worker for the phone app. Scope is the whole site (served from /sw.js), but it only touches /m, its own static files and a few read-only APIs.
// Shell and data are network-first, so a connection always wins; the cache is the "last known" view when the portal is out of reach.
const CACHE = "hb-m1";
const SHELL = ["/static/mobile.css", "/static/mobile.js", "/static/plugs.js", "/static/site/favicon.svg", "/static/site/icon-192.png"];
const API = ["/api/snapshot", "/api/plugs", "/api/presence"];

self.addEventListener("install", (e) => { e.waitUntil(caches.open(CACHE).then((c) => c.addAll(SHELL)).then(() => self.skipWaiting())); });
self.addEventListener("activate", (e) => {
  e.waitUntil(caches.keys().then((ks) => Promise.all(ks.filter((k) => k !== CACHE).map((k) => caches.delete(k)))).then(() => self.clients.claim()));
});
// The page asks for this before sign-out, so the next person on the phone does not see cached data.
self.addEventListener("message", (e) => { if (e.data === "clear") e.waitUntil(caches.delete(CACHE)); });

async function networkFirst(req, key, mark) {
  const cache = await caches.open(CACHE);
  try {
    const res = await fetch(req);
    if (res.ok && !res.redirected) cache.put(key, res.clone());   // a redirect means the login page: never keep that
    return res;
  } catch (err) {
    const hit = await cache.match(key);
    if (!hit) throw err;
    if (!mark) return hit;
    const h = new Headers(hit.headers);
    h.set("X-Hubbah-Cache", "1");
    return new Response(await hit.blob(), { status: 200, headers: h });
  }
}

self.addEventListener("fetch", (e) => {
  const req = e.request, url = new URL(req.url);
  if (req.method !== "GET" || url.origin !== location.origin) return;
  if (url.pathname === "/m") e.respondWith(networkFirst(req, "/m", false));
  else if (API.includes(url.pathname)) e.respondWith(networkFirst(req, url.pathname, true));
  else if (SHELL.includes(url.pathname)) {
    e.respondWith(caches.match(req.url).then((hit) => {
      const fresh = fetch(req).then((res) => { if (res.ok) caches.open(CACHE).then((c) => c.put(req.url, res.clone())); return res; });
      return hit || fresh;
    }));
  }
});

self.addEventListener("push", (e) => {
  let d = {};
  try { d = e.data.json(); } catch (err) { /* plain or empty */ }
  e.waitUntil(self.registration.showNotification(d.title || "HAHBAH", {
    body: d.body || "A Danger alert opened.", tag: d.tag || "danger", icon: "/static/site/icon-192.png", badge: "/static/site/icon-192.png", data: { url: d.url || "/m#alerts" } }));
});
self.addEventListener("notificationclick", (e) => {
  e.notification.close();
  const url = (e.notification.data && e.notification.data.url) || "/m#alerts";
  e.waitUntil(self.clients.matchAll({ type: "window", includeUncontrolled: true }).then((cs) => {
    const c = cs.find((x) => new URL(x.url).pathname === "/m");
    return c ? c.focus().then(() => c.navigate(url)) : self.clients.openWindow(url);
  }));
});
