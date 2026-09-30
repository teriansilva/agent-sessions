/** Hand a link that landed in a NEW tab to a BattleLab tab that is already open (#1232).
 *
 *  A plain browser tab cannot stop a link from opening a new tab, raise another tab, or close a
 *  tab it did not open by script. What it can do is ask: the new tab posts the link on a
 *  same-origin `BroadcastChannel`, one existing tab takes it and opens it there, and the new tab
 *  says so instead of starting a second copy of the app. The installed app does not need any of
 *  this — `launch_handler: navigate-existing` routes the link into its window.
 *
 *      new tab                               existing tab(s)
 *        claim {id, path}          ─────►     first to win the Web Lock `battlelab-link:<id>`
 *        wait ≤ HANDOFF_WAIT_MS    ◄─────     taken {id}, then opens `path` itself
 *
 *  Exactly one taker: every listener tries the SAME lock with `ifAvailable`, and only the holder
 *  answers. And no wait at all when nobody could answer: every listening tab holds the shared
 *  `TAB_LOCK`, so a new tab that finds it unheld boots at once instead of sitting blank for the
 *  wait. Without Web Locks nobody answers, and the link opens where it landed — today's
 *  behaviour, never worse.
 *
 *  The channel is trusted for nothing beyond "which link": a receiver re-classifies the path with
 *  `classifyLink` against its own origin and drops anything that is not a session or mission link. */
import { classifyLink, type LinkEntry } from "./linkEntry";

export const HANDOFF_CHANNEL = "battlelab-links";
export const HANDOFF_WAIT_MS = 400;
/** Held (shared) by every tab that takes links, for as long as it does. */
export const TAB_LOCK = "battlelab-tab";

type Msg =
  | { t: "claim"; id: string; path: string }
  | { t: "taken"; id: string };

function channel(): BroadcastChannel | null {
  return typeof BroadcastChannel === "function" ? new BroadcastChannel(HANDOFF_CHANNEL) : null;
}

/** New tab: offer `entry` to an existing tab. Resolves `true` when one took it. */
export async function offerLink(entry: LinkEntry, waitMs = HANDOFF_WAIT_MS): Promise<boolean> {
  const ch = channel();
  if (!ch || !navigator.locks) {
    ch?.close();
    return false;
  }
  try {
    const state = await navigator.locks.query();
    if (!state.held?.some((l) => l.name === TAB_LOCK)) {
      ch.close();
      return false;
    }
  } catch {
    // No answer about other tabs: fall through and ask them.
  }
  const id = crypto.randomUUID();
  try {
    return await new Promise<boolean>((resolve) => {
      // On timeout, CLOSE the claim before booting here: take its lock ourselves. A tab that answers
      // late then finds the lock held and stands down, so the link never opens twice. If a tab
      // already holds it, that tab won before we gave up and is opening the link.
      const timer = setTimeout(() => {
        void navigator.locks.request(
          `battlelab-link:${id}`,
          { ifAvailable: true },
          async (lock) => {
            resolve(lock === null);
            // Held for the rest of this page's life: a tab frozen in the background can deliver
            // the claim seconds later, and must still find it closed.
            if (lock) await new Promise(() => {});
          },
        );
      }, waitMs);
      ch.onmessage = (e: MessageEvent<Msg>) => {
        if (e.data?.t === "taken" && e.data.id === id) {
          clearTimeout(timer);
          resolve(true);
        }
      };
      ch.postMessage({ t: "claim", id, path: entry.path } satisfies Msg);
    });
  } finally {
    ch.close();
  }
}

/** Existing tab: take offered links. `open` runs only in the one tab that won the claim. Returns
 *  the unsubscribe. */
export function acceptLinks(open: (entry: LinkEntry) => void): () => void {
  const ch = channel();
  if (!ch || !navigator.locks) {
    ch?.close();
    return () => {};
  }
  let release: () => void = () => {};
  const held = new Promise<void>((r) => {
    release = r;
  });
  void navigator.locks.request(TAB_LOCK, { mode: "shared" }, () => held);
  ch.onmessage = (e: MessageEvent<Msg>) => {
    const msg = e.data;
    if (msg?.t !== "claim" || typeof msg.id !== "string" || typeof msg.path !== "string") return;
    const entry = classifyLink(msg.path, window.location.origin);
    if (!entry) return;
    void navigator.locks.request(
      `battlelab-link:${msg.id}`,
      { ifAvailable: true },
      async (lock) => {
        if (!lock) return;
        ch.postMessage({ t: "taken", id: msg.id } satisfies Msg);
        open(entry);
        // Hold the lock past the offerer's wait, so a slower tab cannot win it after we released.
        await new Promise((r) => setTimeout(r, HANDOFF_WAIT_MS * 5));
      },
    );
  };
  return () => {
    release();
    ch.close();
  };
}
