import { useCallback, useEffect, useMemo, useRef, useState } from "react";
import type { ReactNode } from "react";
import { api, ApiError } from "../lib/api";
import type { Session } from "../types/api";
import { isNewSessionPlaceholder, SessionsCtx } from "./sessionsStore";

/** Backoff for TRANSIENT lookup failures (#867 review). A 404 is an answer; a dropped
 *  connection or a 500 is not, and settling one as "this session has no row" left a
 *  hidden/archived pane nameless until the whole provider remounted — with no way back, since
 *  the filtered list is exactly what cannot rescue those rows. Module scope so the retry policy
 *  is not re-created per render and cannot go stale inside the callback. */
const RETRY_MS = [800, 2400];

/** How long a settled-negative key waits before it is allowed to be asked again. Covers both
 *  failure shapes the fast budget cannot: an outage that outlasts it, and a 404 the server
 *  answered from a snapshot that has since been re-walked. */
const REVALIDATE_MS = 15_000;

/** A 2xx that carried someone else's row (or no row at all): a proxy or SPA-shell fallback
 *  answering a path it should not, a mock whose pattern is too broad. Its own class so the catch
 *  can tell it apart from a real `ApiError` and treat it as a retryable attempt. */
class IdentityMismatch extends Error {
  constructor(key: string) {
    super(`lookup for ${key} returned a different row`);
    this.name = "IdentityMismatch";
  }
}

export function SessionsProvider({ children }: { children: ReactNode }) {
  const [sessions, setSessions] = useState<Session[]>([]);
  // Resolved lookups, keyed by session id. A key maps to its row, or to null once a 404 has
  // settled it — both are answers, and both are stored under the key they were REQUESTED for.
  // Nothing is ever read out of a "current session" slot, which is what stops a response that
  // lands after a navigation from painting one session's project (or error) onto another's pane.
  const [looked, setLooked] = useState<Record<string, Session | null>>({});
  // Keys already asked for — in a ref, not state, so the guard is effective the moment
  // `lookup` runs rather than one render later (two consumers of the same pane call it in the
  // same commit, and a state-based guard would let both through).
  const asked = useRef<Set<string>>(new Set());
  // Every pending retry / revalidation timer, cleared on unmount (#867 review round 6, non-
  // blocking note). Nothing here survives the provider, so a teardown mid-outage cannot leave
  // timers queued against a store that no longer exists.
  const timers = useRef<Set<number>>(new Set());
  useEffect(
    () => () => {
      for (const t of timers.current) window.clearTimeout(t);
      timers.current.clear();
    },
    [],
  );
  const later = useCallback((fn: () => void, ms: number) => {
    const t = window.setTimeout(() => {
      timers.current.delete(t);
      fn();
    }, ms);
    timers.current.add(t);
  }, []);
  // Bumped whenever a settled-negative key is released for another attempt. It is in the context
  // value (and in `useSessionRow`'s effect deps) purely so a MOUNTED pane re-asks: without a
  // state change nothing re-runs, and the pane would wait for a navigation it may never get.
  const [retryGen, setRetryGen] = useState(0);

  // A NAMED function expression so the retry can call itself. `useCallback([], …)` makes it
  // stable, so `run` is the same closure every time — no ref write during render, and consumers
  // keep one `lookup` identity so their effects do not re-fire.
  // Keep the fallback snapshot as fresh as the list (#867 review round 6). A successful lookup
  // used to leave its row under the key forever while `asked` suppressed any re-ask; the sidebar
  // row would win only for as long as it stayed listed, and the moment a filter/page/visibility
  // change dropped it the pane fell back to the ORIGINAL snapshot — arbitrarily old, and for a
  // hidden or archived session never refreshable at all, because list polling cannot reach it.
  // Publishing the listed row into the same slot means the fallback is never staler than the
  // last time the list saw it, and dropping out of the list re-opens the key for a real lookup.
  // Per-key generation (#867 review round 7). Every list write and every new request bumps it;
  // a completion whose generation is stale is DROPPED. Without this, a lookup that started while
  // the row was absent could land AFTER the list published a fresher one and overwrite it —
  // invisibly, because the list keeps winning while it is authoritative, and then the stale row
  // resurfaces the moment the list drops it. `SessionView` reads the same row for the file
  // panel's cwd, so a resurrected snapshot can move the folder the panel is browsing.
  const gen = useRef<Map<string, number>>(new Map());
  const bump = useCallback((key: string) => {
    const next = (gen.current.get(key) ?? 0) + 1;
    gen.current.set(key, next);
    return next;
  }, []);

  const remember = useCallback(
    (key: string, listedRow: Session) => {
      bump(key); // the list is newer than anything currently in flight
      setLooked((prev) =>
        prev[key] === listedRow ? prev : { ...prev, [key]: listedRow },
      );
    },
    [bump],
  );

  const forget = useCallback((key: string) => {
    asked.current.delete(key);
    setRetryGen((g) => g + 1);
  }, []);

  const lookup = useCallback(function run(key: string, attempt = 0, inherited?: number) {
    if (!key || isNewSessionPlaceholder(key)) return;
    if (attempt === 0) {
      if (asked.current.has(key)) return;
      asked.current.add(key);
    }
    // A RETRY inherits the generation of the attempt that scheduled it; only a first attempt
    // claims a new one (#867 review round 8). Bumping on every retry made a retry "newer than
    // the list" even though it exists solely because of an older request — so a mismatch
    // retried past a `remember()` could overwrite the fresher list row.
    const mine = attempt === 0 || inherited === undefined ? bump(key) : inherited;
    api
      .session(key)
      // A lookup must verify it got the row it ASKED for. Anything else — a list page from a
      // proxy or a test mock whose pattern also matches this path, an SPA-shell redirect, a row
      // for a different id — is a miss, not a row. Without this check the wrong-shaped object
      // reaches `row.project.kind` in the pane header and takes the whole app down with it,
      // which is exactly how a broad `**/api/sessions**` route in the existing e2e suite turned
      // one new request into 218 red specs. Cheap, and it closes the same door on the server.
      .then((row) => {
        if (row?.id !== key) {
          // Not the row we asked for. THROW rather than settle: a mismatch is a failed attempt,
          // not a verdict about the session (#867 review round 5). Settling `null` here left the
          // key in `asked` with all the release/revalidate logic living in the catch, so one
          // misrouted 2xx pinned the pane empty for the provider's whole life — the exact
          // permanent failure the transient-error handling exists to prevent. Falling into the
          // catch gives it the same bounded retry and slow revalidation as a 5xx.
          throw new IdentityMismatch(key);
        }
        // Superseded while in flight — by a list write, or by a newer request for this key.
        if (gen.current.get(key) !== mine) return;
        setLooked((prev) => ({ ...prev, [key]: row }));
      })
      .catch((e: unknown) => {
        // Superseded is superseded — including an identity mismatch. Exempting it (so its retry
        // policy could run) is what let a stale retry outlive a `remember()`: the list has
        // already answered this key, so nothing this attempt produces matters.
        if (gen.current.get(key) !== mine) return;
        // A 404 IS the answer — the id is unknown, hidden behind the hard scope, or gone. Settle
        // it as null so the pane degrades to its pre-#867 behaviour for that key alone, and never
        // ask again.
        if (e instanceof ApiError && e.status === 404) {
          setLooked((prev) => ({ ...prev, [key]: null }));
          // Settled — but not forever. The server re-walks the scan before answering 404, so
          // this is authoritative *now*; a session can still be created under this id later
          // (and a deep link can land before its transcript exists at all). Allow one more ask
          // after a pause rather than teaching the pane "no such session" for its whole life.
          later(() => {
            asked.current.delete(key);
            setRetryGen((g) => g + 1);
          }, REVALIDATE_MS);
          return;
        }
        // Anything else — offline, a 5xx, a proxy hiccup — is a failed attempt, not a verdict.
        const delay = RETRY_MS[attempt];
        if (delay === undefined) {
          // Out of the fast budget. Releasing `asked` is NOT enough on its own: a ref write is
          // invisible to React, so a pane that stays mounted never re-runs its effect and sits
          // nameless until the user navigates away and back (#867 review round 4). Bump a
          // generation after a pause — that is a state change, so mounted consumers re-run and
          // ask again. Slow and open-ended by design: an outage should heal on its own, and one
          // request per ~15 s is not a poll worth bounding away.
          later(() => {
            asked.current.delete(key);
            setRetryGen((g) => g + 1);
          }, REVALIDATE_MS);
          return;
        }
        later(() => run(key, attempt + 1, mine), delay);
      });
  }, [later, bump]);

  const value = useMemo(
    () => ({ sessions, setSessions, looked, lookup, retryGen, remember, forget }),
    [sessions, looked, lookup, retryGen, remember, forget],
  );
  return <SessionsCtx.Provider value={value}>{children}</SessionsCtx.Provider>;
}
