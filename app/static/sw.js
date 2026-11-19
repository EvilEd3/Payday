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
