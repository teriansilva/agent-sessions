/** The mission shelf's data (#1740): the whole manifest, kept fresh while the page is visible.
 *
 *  The server pages at 100 items and caps a shelf at 2,000, so the hook reads every page in order
 *  (`next_cursor`) and holds the list; the view filters and groups locally, which keeps every
 *  chip's count and every group consistent with what is on screen. It re-reads on a short timer
 *  while the document is visible — file states (changed, missing) are re-checked by the server on
 *  each read, and an agent's delivery raises the revision — and drops any answer that a newer read
 *  has overtaken, so a slow response can never paint an older shelf over a newer one. */
import { useCallback, useEffect, useRef, useState } from "react";

import { api, ApiError } from "../../../lib/api";
import type { ShelfCounts, ShelfItem } from "../../../types/shelf";
import { withItem } from "./shelfLogic";

export const SHELF_POLL_MS = 10_000;
/** A read that has not settled by now is aborted, so one hung request (a stalled tunnel, a socket
 *  left half-open by a sleeping phone) can never stop the polling for good (review on #1904). */
export const SHELF_READ_TIMEOUT_MS = 45_000;
/** The server's own cap (2,000 items) over its page size (100). */
const MAX_PAGES = 20;

export interface ShelfState {
  revision: number;
  counts: ShelfCounts;
  items: ShelfItem[];
}

export interface MissionShelf {
  shelf: ShelfState | null;
  error: string | null;
  /** Re-read the whole shelf now. Resolves with what it read (or null when it failed). */
  reload: () => Promise<ShelfState | null>;
  /** Fold a verdict answer in place: the item replaced, the revision moved. */
  applyVerdict: (revision: number, item: ShelfItem) => void;
}

export function useMissionShelf(missionId: string): MissionShelf {
  const [shelf, setShelf] = useState<ShelfState | null>(null);
  const [error, setError] = useState<string | null>(null);
  const gen = useRef(0);
  /** Reads in flight. The timer never starts another on top of one: a large shelf on a loaded box
   *  can take longer than the period, and stacking reads that each invalidate the last would mean
   *  nothing ever landed (review on #1904). */
  const inflight = useRef(0);

  const reload = useCallback(async (): Promise<ShelfState | null> => {
    const mine = ++gen.current;
    inflight.current += 1;
    const ctl = new AbortController();
    const deadline = window.setTimeout(() => ctl.abort(), SHELF_READ_TIMEOUT_MS);
    try {
      let cursor: string | null = null;
      let first: ShelfState | null = null;
      const items: ShelfItem[] = [];
      for (let page = 0; page < MAX_PAGES; page++) {
        const r = await api.missionShelf(missionId, cursor, ctl.signal);
        // A server that predates the shelf (or a proxy page) answers something else: unreadable.
        if (!r || typeof r.revision !== "number" || !Array.isArray(r.items) || !r.counts) {
          throw new Error("not a shelf manifest");
        }
        if (first === null) first = { revision: r.revision, counts: r.counts, items };
        items.push(...r.items);
        cursor = r.next_cursor;
        if (!cursor) break;
      }
      if (mine !== gen.current || first === null) return null;
      const next = { ...first, items };
      setShelf(next);
      setError(null);
      return next;
    } catch (e) {
      if (mine !== gen.current) return null;
      setError(
        e instanceof ApiError && e.status === 404
          ? "This mission is gone."
          : "The shelf could not be read. It will try again.",
      );
      return null;
    } finally {
      window.clearTimeout(deadline);
      inflight.current -= 1;
    }
  }, [missionId]);

  const applyVerdict = useCallback((revision: number, item: ShelfItem) => {
    // A verdict answer is newer than any read already in flight: overtake them.
    gen.current++;
    setShelf((s) => {
      if (!s) return s;
      const items = withItem(s.items, item);
      const unreviewed = items.filter((i) => i.verdict === null).length;
      return { revision, items, counts: { ...s.counts, unreviewed } };
    });
  }, []);

  useEffect(() => {
    // The first read is an external fetch whose answer sets state after it arrives.
    // eslint-disable-next-line react-hooks/set-state-in-effect
    void reload();
    const tick = () => {
      if (document.visibilityState === "visible" && inflight.current === 0) void reload();
    };
    const id = window.setInterval(tick, SHELF_POLL_MS);
    const onVisible = () => tick();
    document.addEventListener("visibilitychange", onVisible);
    const generation = gen;
    return () => {
      generation.current++; // an answer still in flight lands on nothing
      window.clearInterval(id);
      document.removeEventListener("visibilitychange", onVisible);
    };
  }, [reload]);

  return { shelf, error, reload, applyVerdict };
}
