import { useEffect, useRef } from "react";
import type { Session } from "../types/api";
import { useSessionsStore } from "./sessionsStore";

/** The row for the session a pane has open (#867) — the ONE accessor `SessionView` and
 *  `Terminal` both read, so a pane resolves its own identity instead of hoping the sidebar
 *  happens to have loaded it.
 *
 *  The sidebar's list holds a single filtered, scope-stripped 20-row page, so it can only name
 *  a small, recently-active slice of what a URL can address. Everything else — a deep link, a
 *  reload after the row fell off page 0, an archived session, one hidden from the list by
 *  `projects_mode` / `projects_hidden` — used to leave the pane with no row at all: no project,
 *  no update time, a `<uuid[:8]>…` title in the brief, and a Files trigger disabled behind
 *  "this session has not reported a folder yet" while that session's terminal was on screen.
 *
 *  **The list still wins whenever it has the row.** It is the fresher source — the 15 s poll
 *  keeps `working`, `last_mtime` and the review fields moving on it — so a fetched copy is a
 *  fallback that a later poll supersedes, never a snapshot frozen over live data.
 *
 *  `fallbackKey` is the id the URL has settled on, for the opencode/codex converge (#127): the
 *  terminal's identity stays frozen on the placeholder so its socket survives, but after
 *  `onReconcileId` the row exists only under the real id. That is also the key we ASK for — the
 *  placeholder itself is never requested (see `isNewSessionPlaceholder`).
 *
 *  The request itself is owned by `SessionsProvider`, not by this hook: two calls for one pane
 *  must be one request, and a per-hook `useRef` guard could not see the sibling's.
 */
export function useSessionRow(
  key: string,
  fallbackKey?: string,
): Session | undefined {
  const { sessions, looked, lookup, retryGen, remember, forget } =
    useSessionsStore();
  const listed =
    sessions.find((s) => s.id === key) ??
    (fallbackKey ? sessions.find((s) => s.id === fallbackKey) : undefined);
  const want = fallbackKey || key;
  // Boolean, not the row object: the poll hands back a fresh object every 15 s, and depending on
  // its identity would re-run this effect on every tick for no reason.
  const have = Boolean(listed);
  // `retryGen` is a dep on purpose: it is the only thing that re-runs this effect for a pane
  // that never navigates, which is what lets a nameless pane heal once the provider releases a
  // settled-negative key (#867 review round 4).
  useEffect(() => {
    if (!have) lookup(want);
  }, [have, want, lookup, retryGen]);

  // While the list IS authoritative, keep the fallback snapshot in step with it; when it stops
  // being authoritative for a key it used to carry, re-open that key instead of falling back to
  // whatever was cached (#867 review round 6). Without this the pane could show arbitrarily old
  // project/review metadata after a filter or visibility change dropped the row — and for a
  // hidden or archived session, forever, since list polling can never refresh it.
  // The KEY the list was last authoritative for, not a boolean (#867 review round 7). A bare
  // flag is not tied to the identity that set it: navigating from a listed A to an unlisted B
  // left it `true`, so B's own effect read "the row left the list" and called `forget(B)` —
  // releasing a key whose first lookup was still in flight, which then ran again on the
  // retry-generation render. Three requests for one pane, and their outcomes could race (a late
  // 404 replacing a good row). Keyed, the transition can only ever fire for the key that was
  // actually listed.
  const listedKeyRef = useRef<string | null>(null);
  useEffect(() => {
    if (listed) {
      listedKeyRef.current = want;
      remember(want, listed);
    } else if (listedKeyRef.current === want) {
      listedKeyRef.current = null;
      forget(want);
    } else if (listedKeyRef.current !== null) {
      // We navigated away from the key that was listed. Nothing "left the list" for THIS key —
      // just stop tracking the old one.
      listedKeyRef.current = null;
    }
  }, [listed, want, remember, forget]);
  // `looked[want]` is `null` once a 404 has settled the key — a permanent answer, not a
  // pending one, and read by key so a response that landed after a navigation cannot surface
  // on the pane that replaced it.
  return listed ?? looked[want] ?? undefined;
}
