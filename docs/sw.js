// Lets phones install the page as an app. Fares always come fresh from the network;
// nothing is cached, so the page never shows stale prices.
self.addEventListener("install", () => self.skipWaiting());
self.addEventListener("activate", event => event.waitUntil(self.clients.claim()));
self.addEventListener("fetch", () => {});
