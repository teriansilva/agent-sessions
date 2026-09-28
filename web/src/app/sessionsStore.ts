import { createContext, useContext } from "react";
import type { Session } from "../types/api";

/** The sidebar footer's counts (#1085), published by the list that fetched them. They describe
 *  its FILTERED set, never the loaded page. */
export interface SessionCounts {
  total: number;
  live: number;
}

/** Shared store of the sessions the sidebar has loaded, so the compact desktop/mobile
 *  header can resolve the *current* session's title (matched by engine+uuid from the URL)
 *  without re-fetching. The sidebar (the single owner of the list data) publishes its rows
 *  here; the header is a read-only consumer. Empty until the sidebar's first page lands.
 *
 *  It is ALSO the request owner for single-session lookups (#867). The sidebar's list is one
 *  filtered, scope-stripped 20-row page, so it can only ever name a small, recently-active
 *  slice of what the URL can address — a deep link, a reload after the row fell off page 0,
 *  an archived session, or one hidden from the list by `projects_mode` / `projects_hidden`
 *  all left the pane with no row at all. `lookup(key)` fills that gap from
 *  `GET /api/sessions/{sid}`.
 *
 *  The owner lives HERE rather than in a per-consumer hook because `SessionView` and
 *  `Terminal` are two consumers of the SAME pane: with local state each they would issue two
 *  "once per key" requests for one session. Sharing the owner makes that structurally
 *  impossible instead of a convention. */
export interface SessionsStore {
  sessions: Session[];
  setSessions: (s: Session[]) => void;
  /** Footer counts (#1085); `null` until the first page carrying them lands. */
  counts: SessionCounts | null;
  setCounts: (c: SessionCounts | null) => void;
  /** The looked-up row for `key`, or null when nothing has been fetched (yet, or ever —
   *  a 404 is a permanent null for that key). Never consulted when the list already has
   *  the row: the list is the fresher source and a poll supersedes a fetched copy. */
  looked: Record<string, Session | null>;
  /** Ask for `key`. Idempotent per key: a second caller joins the in-flight request rather
   *  than starting another, and a key that already resolved (row OR 404) is not re-asked until
   *  its revalidation window elapses. */
  lookup: (key: string) => void;
  /** Publish a row the LIST is currently authoritative for, so the fallback snapshot under that
   *  key never goes staler than the last list sighting. */
  remember: (key: string, row: Session) => void;
  /** Re-open a key for a real lookup — used when the list stops carrying a row it used to. */
  forget: (key: string) => void;
  /** Bumped when a settled-negative key is released for another attempt. Consumers put it in
   *  their effect deps so a MOUNTED pane re-asks — a ref release alone changes no state, so
   *  nothing would re-run and the pane would stay nameless through an outage. */
  retryGen: number;
}

export const SessionsCtx = createContext<SessionsStore>({
  sessions: [],
  setSessions: () => {},
  counts: null,
  setCounts: () => {},
  looked: {},
  lookup: () => {},
  retryGen: 0,
  remember: () => {},
  forget: () => {},
});

/** True for an `<engine>:new-<uuid>` new-session placeholder (#127/#315).
 *
 *  These are NEVER looked up. The server's `canonical_key` rejects the shape — `parse_key`
 *  accepts it only on the ws launch path — so asking would cache a 404 under a key that is
 *  about to be replaced by the real id, and that 404 would then stand as the pane's answer
 *  for the session. A fresh launch already carries its `cwd` in router state, so skipping
 *  costs nothing; the real key is fetched once after the converge. */
export function isNewSessionPlaceholder(key: string): boolean {
  return /^[a-z0-9_-]+:new-[0-9a-f-]{36}$/i.test(key);
}

export function useSessionsStore(): SessionsStore {
  return useContext(SessionsCtx);
}
