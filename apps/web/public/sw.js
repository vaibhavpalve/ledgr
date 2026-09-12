/**
 * Minimal app-shell service worker — §5.2/MOB-002's "installable PWA".
 *
 * This is NOT `@ledgr/offline-queue`'s job duplicated. That package already
 * persists captured documents and their metadata (IndexedDB, encrypted) and
 * drains them when a connection returns — a data-level offline story this
 * file has nothing to add to. What this file does is the one thing that
 * package cannot: let the installed app's HTML/JS/CSS *open* with no network
 * at all, which is a precondition for installability and for the capture
 * screen existing at all offline.
 *
 * Cache-first for the app shell, network passthrough for everything else
 * (in particular every `/v1/...` API call): a stale cached API response would
 * be actively wrong for financial data, where `@ledgr/offline-queue` and the
 * screens built on it already own the real offline behaviour.
 */

const CACHE_NAME = "ledgr-shell-v1";
const APP_SHELL = ["/", "/manifest.webmanifest"];

self.addEventListener("install", (event) => {
  event.waitUntil(
    caches
      .open(CACHE_NAME)
      .then((cache) => cache.addAll(APP_SHELL))
      .then(() => self.skipWaiting()),
  );
});

self.addEventListener("activate", (event) => {
  event.waitUntil(
    caches
      .keys()
      .then((names) =>
        Promise.all(names.filter((name) => name !== CACHE_NAME).map((name) => caches.delete(name))),
      )
      .then(() => self.clients.claim()),
  );
});

self.addEventListener("fetch", (event) => {
  const { request } = event;

  // Only ever the app shell's own navigations and static assets. Never an
  // API call: `/v1/...` must always reach the network or fail explicitly, not
  // be answered from a cache that could be describing yesterday's books.
  if (request.method !== "GET" || new URL(request.url).pathname.startsWith("/v1/")) {
    return;
  }

  event.respondWith(
    caches.match(request).then((cached) => {
      if (cached) return cached;
      return fetch(request).catch(() => caches.match("/"));
    }),
  );
});
