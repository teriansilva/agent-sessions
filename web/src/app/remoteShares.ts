/** What peers share with this instance (#2055): `GET /api/federation/remote`, held for the
 *  surfaces that list it (the session list, the map, Settings → Peers).
 *
 *  **This is deliberately not the session list.** A remote entry is never a `SessionEntry`, never
 *  enters `SessionsContext` or the map's retained list (`OverviewSessionsProvider`), and is never
 *  filtered, paged, selected or bulk-acted on with local sessions. It has its own read, its own
 *  rows and its own route.
 *
 *  **Memory only.** The list (titles and names a peer chose) is never written to local or session
 *  storage, and no turn of a remote session ever reaches this module. It is read while something
 *  shows it and re-read on a slow poll; `refreshRemoteShares()` asks again at once (the pane
 *  calls it when its share ended, so the row leaves with the next read). */
import { useEffect, useSyncExternalStore } from "react";
import { api } from "../lib/api";
import { isEpochS, type RemoteShare } from "../lib/federation";

/** The mirror behind the read refreshes every 60 s; half of that is as fresh as a row gets. */
export const REMOTE_LIST_POLL_MS = 30_000;

const EMPTY: readonly RemoteShare[] = Object.freeze([]);
let rows: readonly RemoteShare[] = EMPTY;
let signature = "";
let watchers = 0;
let timer: ReturnType<typeof setTimeout> | null = null;
let inflight: AbortController | null = null;
const listeners = new Set<() => void>();

/** Whether a listed entry is one this build can show.
 *
 *  Besides the shape, the `kind`: the server lists a peer's terminal shares too (#2056), and this
 *  build only has the conversation pane. A terminal share drawn as a conversation row would open
 *  a pane whose every read the owner refuses, and read as "no longer shared" when it is shared.
 *  So only `api` entries (and entries from an owner that predates kinds, which are all API
 *  shares) are listed; the terminal screen pane (#2060) widens this. The expiry must be a date
 *  (`isEpochS`): finite, after 0, within what a `Date` holds. */
export function isRow(r: unknown): r is RemoteShare {
  if (!r || typeof r !== "object") return false;
  const x = r as Record<string, unknown>;
  return (
    (x.kind === undefined || x.kind === "api") &&
    typeof x.peer_id === "string" &&
    typeof x.grant_id === "string" &&
    typeof x.title === "string" &&
    typeof x.peer_name === "string" &&
    typeof x.level === "string" &&
    // An expiry this browser cannot make a date of is not a row (#2055 review): the server
    // refuses such a listing since, but a value must never reach a formatter that could throw.
    isEpochS(x.expires_at)
  );
}

function commit(next: readonly RemoteShare[]) {
  // Same content keeps the same array: the map must not rebuild its nodes on an idle poll
  // (docs/invariants/map-window-layer.md, the #936 rule).
  const sig = JSON.stringify(next);
  if (sig === signature) return;
  signature = sig;
  rows = next.length ? next : EMPTY;
  for (const fn of listeners) fn();
}

async function read() {
  inflight?.abort();
  const ctl = new AbortController();
  inflight = ctl;
  try {
    const answer = await api.federationRemote(ctl.signal);
    if (ctl.signal.aborted) return;
    const list: unknown = (answer as { remote?: unknown } | null)?.remote;
    commit(Array.isArray(list) ? list.filter(isRow) : []);
  } catch {
    // A failed read lists nothing new and removes nothing: the rows open a pane that asks the
    // owner anyway, and the next read corrects the list.
  } finally {
    if (inflight === ctl) inflight = null;
  }
}

function schedule() {
  if (timer) clearTimeout(timer);
  timer = null;
  if (!watchers) return;
  timer = setTimeout(() => {
    const visible = typeof document === "undefined" || document.visibilityState !== "hidden";
    void (visible ? read() : Promise.resolve()).finally(schedule);
  }, REMOTE_LIST_POLL_MS);
}

/** Ask for the list again now. */
export function refreshRemoteShares(): Promise<void> {
  return read().finally(schedule);
}

function subscribe(fn: () => void) {
  listeners.add(fn);
  return () => {
    listeners.delete(fn);
  };
}
const snapshot = () => rows;

/** The sessions peers share with this instance. Empty until the first read, and on an instance
 *  with no peers. */
export function useRemoteShares(): readonly RemoteShare[] {
  useEffect(() => {
    watchers += 1;
    if (watchers === 1) void refreshRemoteShares();
    return () => {
      watchers -= 1;
      if (!watchers) {
        if (timer) clearTimeout(timer);
        timer = null;
        inflight?.abort();
      }
    };
  }, []);
  return useSyncExternalStore(subscribe, snapshot, snapshot);
}

/** Test seam: forget everything (the store is module state). */
export function resetRemoteSharesForTest() {
  rows = EMPTY;
  signature = "";
  watchers = 0;
  if (timer) clearTimeout(timer);
  timer = null;
  inflight?.abort();
  inflight = null;
  listeners.clear();
}
