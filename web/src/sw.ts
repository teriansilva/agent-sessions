/// <reference lib="webworker" />
/**
 * BattleLab service worker (#726 Phase 3).
 *
 * Hand-written because Web Push needs a `push` listener, and `generateSW` gives no seam to add
 * one. Everything else is deliberately a like-for-like reproduction of what `generateSW` was
 * emitting — precache the built shell, navigation-fallback to `index.html`, and deny that
 * fallback for every server-rendered route.
 *
 * The migration risk is entirely in that last part. A dropped denylist entry doesn't fail a
 * build or throw at runtime; the SW just starts answering `/login` with the cached React shell
 * and someone is locked out of their own box. So the list lives in `sw-denylist.ts`, shared
 * with a test that walks concrete paths in both directions.
 *
 * Push notifications carry title + project + link ONLY (#726). The payload has already crossed
 * a third-party push service by the time it reaches this file, so there is nothing sensitive in
 * it — evidence is fetched from our own server after the tap, under the session cookie.
 */
import { clientsClaim } from "workbox-core";
import { NavigationRoute, registerRoute } from "workbox-routing";
import { createHandlerBoundToURL, precacheAndRoute } from "workbox-precaching";
import { NAVIGATE_FALLBACK_DENYLIST } from "./sw-denylist";

declare const self: ServiceWorkerGlobalScope & {
  __WB_MANIFEST: Array<{ url: string; revision: string | null }>;
};

// `registerType: "autoUpdate"` + these two = a new SW takes over without the user having to
// close every tab, matching the previous generateSW behaviour (#661 relies on it).
self.skipWaiting();
clientsClaim();

precacheAndRoute(self.__WB_MANIFEST);

// Navigation fallback: serve the cached SPA shell for client-side routes, and ONLY those.
registerRoute(
  new NavigationRoute(createHandlerBoundToURL("index.html"), {
    // The shared list, passed STRAIGHT through — no adapter object, no cast.
    //
    // This previously wrapped `isDenied` in a fake RegExp whose `test` took a `URL`. Workbox
    // calls `regExp.test(url.pathname + url.search)` — a STRING — so `url.pathname` was
    // `undefined`, `isDenied(undefined)` matched nothing, and NOTHING was denied: /login and
    // /change-password were being served the cached SPA shell. The `as unknown as RegExp`
    // cast is what silenced the type error that would have caught it.
    //
    // The list is already `RegExp[]`, so the adapter was never needed. Anything that has to
    // wrap it in future must accept the pathname+search STRING.
    denylist: NAVIGATE_FALLBACK_DENYLIST,
  }),
);

interface PushPayload {
  title?: string;
  body?: string;
  url?: string;
}

self.addEventListener("push", (event: PushEvent) => {
  let data: PushPayload;
  try {
    data = (event.data?.json() ?? {}) as PushPayload;
  } catch {
    // A malformed or unencrypted push must still surface something rather than throwing away
    // the wake-up — the operator being told "Pulse needs you" with no detail beats silence.
    data = {};
  }
  const title = data.title || "Pulse";
  event.waitUntil(
    self.registration.showNotification(title, {
      body: data.body || "Needs your attention",
      icon: "/icon-192.png",
      badge: "/icon-192.png",
      // Coalesce per session: a second escalation for the same session replaces the first
      // rather than stacking, so a chatty agent can't bury the notification shade.
      tag: data.url || "pulse",
      data: { url: data.url || "/pulse" },
    }),
  );
});

self.addEventListener("notificationclick", (event: NotificationEvent) => {
  event.notification.close();
  const target = (event.notification.data?.url as string) || "/pulse";
  event.waitUntil(
    (async () => {
      const all = await self.clients.matchAll({
        type: "window",
        includeUncontrolled: true,
      });
      // Prefer focusing an existing tab and navigating it — opening a second BattleLab window
      // every time a notification is tapped is its own small hell.
      for (const client of all) {
        if ("focus" in client) {
          await client.focus();
          if ("navigate" in client) await client.navigate(target);
          return;
        }
      }
      await self.clients.openWindow(target);
    })(),
  );
});
