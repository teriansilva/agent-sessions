import { useCallback, useEffect, useRef, useState } from "react";
import { useSessionsStore } from "../../app/sessionsStore";
import { api } from "../../lib/api";
import type { Session } from "../../types/api";

/** Where an Unarchive pressed in the pane stands (#2064). */
export type UnarchiveState =
  | { phase: "idle" }
  | { phase: "busy" }
  | { phase: "failed"; message: string };

/** The server's own words for a failed unarchive, trimmed of a trailing full stop (the strip adds
 *  its own sentence after it). */
function failure(e: unknown): string {
  const raw = e instanceof Error && e.message ? e.message : "the request failed";
  return raw.trim().replace(/[.。]+$/, "") || "the request failed";
}

/** Is the session a pane has open archived — frozen, read-only (#2064)? And the pane's Unarchive.
 *
 *  `row` is the pane's session row (`useSessionLookup`), `key` the key it was looked up under.
 *  The row's `archived` is the source; two things this pane learns sooner override it:
 *
 *  - **The server refused this pane as archived** (a terminal 4410 close, or a 409 archived from a
 *    send — another device archived it). That is sticky until an Unarchive here succeeds: the row
 *    may lag (the list polls every 15 s), and a pane that flipped back on a stale row would only
 *    reconnect into the same refusal. The row is re-read at once so the header catches up.
 *  - **An Unarchive here succeeded and the re-read row says active.** The re-read row is published
 *    to the store, but while the sidebar's list still carries the old (archived) copy the list
 *    wins, so the confirmation holds over exactly that row object; the list's next poll replaces
 *    it and the row speaks for itself again.
 *
 *  Unarchive itself starts nothing (the server only clears the flag). The composer comes back only
 *  once the re-read row says `archived: false`; any failure leaves the pane read-only with the
 *  reason. */
export function useArchivedSession(key: string, row: Session | undefined) {
  const { remember } = useSessionsStore();
  const [refused, setRefused] = useState(false);
  const [confirmed, setConfirmed] = useState<{ over: Session | undefined } | null>(null);
  const [state, setState] = useState<UnarchiveState>({ phase: "idle" });
  const rowRef = useRef(row);
  useEffect(() => {
    rowRef.current = row;
  });
  const alive = useRef(true);
  useEffect(() => {
    alive.current = true;
    return () => {
      alive.current = false;
    };
  }, []);
  const busy = useRef(false);

  const confirmedActive = confirmed !== null && confirmed.over === row;
  const archived = refused || (row?.archived === true && !confirmedActive);

  /** The server refused this pane because the session is archived: freeze now, re-read the row. */
  const markArchived = useCallback(() => {
    setRefused(true);
    setConfirmed(null);
    api
      .session(key)
      .then((fresh) => {
        if (alive.current && fresh && fresh.id === key) remember(key, fresh);
      })
      .catch(() => {
        /* the refusal already froze the pane; the header catches up on the next read */
      });
  }, [key, remember]);

  const unarchive = useCallback(async () => {
    if (busy.current) return;
    busy.current = true;
    setState({ phase: "busy" });
    try {
      await api.unarchive(key);
      const fresh = await api.session(key);
      if (!alive.current) return;
      if (!fresh || fresh.archived) throw new Error("the session still reads archived");
      remember(key, fresh);
      setConfirmed({ over: rowRef.current });
      setRefused(false);
      setState({ phase: "idle" });
    } catch (e) {
      if (alive.current) setState({ phase: "failed", message: failure(e) });
    } finally {
      busy.current = false;
    }
  }, [key, remember]);

  return { archived, state, unarchive, markArchived };
}
