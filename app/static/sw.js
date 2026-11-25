// Payday service worker: always uses the network (your data must be live),
// and shows a short message instead of an error page when the server can't be reached.
self.addEventListener("install", () => self.skipWaiting());
self.addEventListener("activate", (e) => e.waitUntil(self.clients.claim()));
self.addEventListener("fetch", (event) => {
  if (event.request.mode !== "navigate") return;
  event.respondWith(
    fetch(event.request).catch(() => new Response(
      '<!doctype html><meta name="viewport" content="width=device-width,initial-scale=1">' +
      '<body style="font-family:system-ui;padding:2rem;background:#EAEFEC;color:#17262B">' +
      "<h1>Can’t reach Payday</h1><p>Check you’re on your home network or VPN, then try again.</p></body>",
      { headers: { "Content-Type": "text/html; charset=utf-8" } }))
  );
});

// Notifications: show what Payday sends, and open the right page when one is tapped.
self.addEventListener("push", (event) => {
  let data = {};
  try { data = event.data ? event.data.json() : {}; } catch (e) { data = { body: event.data && event.data.text() }; }
  event.waitUntil(self.registration.showNotification(data.title || "Payday", {
    body: data.body || "",
    tag: data.tag || undefined,
    icon: "/static/icon-192.png",
    badge: "/static/icon-192.png",
    data: { url: data.url || "/" },
  }));
});
// Tapping a notification opens its entry in the activity log (or the page it's about).
// Android only lets a window come to the front while the tap is still fresh, so focus first and navigate after.
// A window this worker doesn't control can't be navigated, so if that fails it opens the page in a new window.
self.addEventListener("notificationclick", (event) => {
  event.notification.close();
  const target = new URL(event.notification.data && event.notification.data.url || "/", self.location.origin).href;
  event.waitUntil((async () => {
    const wins = await self.clients.matchAll({ type: "window", includeUncontrolled: true });
    const ours = wins.filter((w) => new URL(w.url).origin === self.location.origin);
    const win = ours.find((w) => w.focused) || ours.find((w) => w.visibilityState === "visible") || ours[0];
    if (win) {
      try {
        const focused = await win.focus();
        if (new URL(focused.url).href === target) return;
        const moved = await focused.navigate(target);
        if (moved) return;
      } catch (e) { /* fall through to a new window */ }
    }
    if (self.clients.openWindow) await self.clients.openWindow(target);
  })());
});
