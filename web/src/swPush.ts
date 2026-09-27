/**
 * What the service worker does with a push, and how the app retracts needs-you notifications
 * (#726, #1086 Phase 4). Pure over a minimal slice of `ServiceWorkerRegistration` and an injected
 * "is this retracted?" question, so both are unit-tested without a worker.
 *
 * **One payload shape, and every push SHOWS its own notification.** `{title, body, url, tag?,
 * close?}` — titles, a project, a link and tags this server minted; never session content. A
 * needs-you episode carries its own `tag` (`needs-you:<session>:<episode>`); anything else
 * coalesces per link as before. `close` lists owed retractions, applied before showing. There is
 * no retraction-only push: the subscription is `userVisibleOnly`, and a push that shows nothing
 * spends the browser's silent-push budget until Chrome shows its own generic notification.
 *
 * **The SERVER is the one authority on what is retracted** (its closed-episode ledger, the bell's
 * `close_tags`). There is no device-side tombstone store: a cache that fails, fills up or is shared
 * with a reused tag cannot decide anything (Hermes 5265, findings 2, 6 and 7). The worker asks the
 * server TWICE for a needs-you notification:
 *  - before showing it: one already retracted — a delayed or reordered delivery — is still shown
 *    (every push must show), silently, and closed at once;
 *  - AFTER showing it: an app reconciliation that ran while the platform show was in flight found
 *    nothing to close yet, so the worker closes it itself if the server now lists it (Hermes 5275,
 *    finding 3). Once shown, any later reconciliation sees it.
 * A question the server cannot answer (offline, logged out, slow) fails OPEN: the notification is
 * shown and kept, and the app closes it later if it turns out to be retracted. Mission and other
 * notifications are never asked about.
 */
export interface PushPayload {
  title?: string;
  body?: string;
  url?: string;
  tag?: string;
  close?: string[];
}

export interface NotificationSurface {
  showNotification(title: string, options?: NotificationOptions): Promise<void>;
  getNotifications(filter?: GetNotificationOptions): Promise<Notification[]>;
}

/** "Has the server retracted this tag?" — `false` whenever that cannot be established. */
export type IsRetracted = (tag: string) => Promise<boolean>;

export const NEEDS_YOU_PREFIX = "needs-you:";

async function closeTags(
  reg: Pick<NotificationSurface, "getNotifications">,
  tags: string[],
) {
  for (const tag of tags) {
    if (!tag) continue;
    // Filtered by tag already; checked again so a platform that ignores the filter still cannot
    // close a notification this retraction does not name.
    for (const n of await reg.getNotifications({ tag }))
      if (n.tag === tag) n.close();
  }
}

export function createPushHandler(
  reg: NotificationSurface,
  fallbackUrl: string,
  isRetracted: IsRetracted,
) {
  let chain: Promise<void> = Promise.resolve();

  const ask = async (tag: string) => {
    if (!tag.startsWith(NEEDS_YOU_PREFIX)) return false;
    try {
      return await isRetracted(tag);
    } catch {
      return false; // fail open: showing a notification is never the harm here
    }
  };

  const handle = async (data: PushPayload) => {
    const close = (data.close ?? []).filter(Boolean);
    // A failure applying retractions must never stop the new notification being shown.
    await closeTags(reg, close).catch(() => undefined);
    const tag = data.tag || data.url || "mission";
    const retracted = await ask(tag);
    await reg.showNotification(data.title || "Mission control", {
      body: data.body || "Needs your attention",
      icon: "/icon-192.png",
      badge: "/icon-192.png",
      // Coalesce per session: a second escalation for the same session replaces the first rather
      // than stacking. A needs-you episode uses its own tag, which a retraction closes exactly.
      tag,
      silent: retracted,
      data: { url: data.url || fallbackUrl },
    });
    // Re-ask after the show: the episode may have ended while the platform was showing it.
    if (retracted || (await ask(tag)))
      await closeTags(reg, [tag]).catch(() => undefined);
  };

  /** Enqueue one push; resolves when it (and everything before it) has been applied. */
  return (data: PushPayload): Promise<void> => {
    const run = chain.then(() => handle(data));
    chain = run.catch(() => undefined);
    return run;
  };
}

/** The server's retraction list, read over the same authenticated route the bell uses. */
export function serverRetractions(
  fetcher: (input: string, init?: RequestInit) => Promise<Response>,
  timeoutMs = 3000,
): IsRetracted {
  return async (tag) => {
    const ctrl = new AbortController();
    const timer = setTimeout(() => ctrl.abort(), timeoutMs);
    try {
      const res = await fetcher("/api/pulse/notifications", {
        credentials: "same-origin",
        signal: ctrl.signal,
      });
      if (!res.ok) return false;
      const body = (await res.json()) as { close_tags?: unknown };
      return Array.isArray(body.close_tags) && body.close_tags.includes(tag);
    } finally {
      clearTimeout(timer);
    }
  };
}

/** The app-open retraction (#1086 Phase 4): with the bell's read in hand, close on this device
 *  EXACTLY the tags the server lists as retracted — needs-you episode tags only, never reused; an
 *  escalation's URL tag never. Never inferred from "not currently open": a read taken just before an
 *  episode opened would close the brand-new notification. */
export async function reconcileDeviceNotifications(
  reg: Pick<NotificationSurface, "getNotifications">,
  closeTagsList: string[],
): Promise<number> {
  const retracted = new Set(closeTagsList.filter(Boolean));
  if (!retracted.size) return 0;
  let closed = 0;
  for (const n of await reg.getNotifications()) {
    if (retracted.has(n.tag)) {
      n.close();
      closed += 1;
    }
  }
  return closed;
}
