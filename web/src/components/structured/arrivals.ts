/** Which rows of the API transcript ARRIVED while the pane was open (#1727, docs/design.md §8
 *  Arrival). A row is a turn, a tool row or a pending request. One counts as arrived when a
 *  snapshot shows it and no snapshot this pane held before did:
 *
 *  - the first snapshot is the first paint — nothing in it arrives;
 *  - history pages never pass through here (they are older turns, prepended) — they never arrive;
 *  - a streamed text page adds no row, so the Jump to latest count is rows, never flushes;
 *  - the operator's own message arrived already, as its outbox entry at Send: the turn that
 *    replaces that entry is not a second arrival.
 *
 *  Arrival is remembered for the life of the pane, so a row re-rendered later keeps its class
 *  (and its finished animation) instead of animating again. */
import { createContext, useContext, useState } from "react";

import type { StructuredRequest, StructuredSnapshot } from "../../types/api";

export interface Arrivals {
  /** Whether the row with this key arrived (`turn:<id>`, `tool:<turn>:<id>`, `req:<key>`). */
  has: (key: string) => boolean;
  /** Rows the latest snapshot added (0 for the first paint and for text-only changes). */
  added: number;
  /** The snapshot `added` was counted for: an effect keyed on it runs once per snapshot. */
  at: StructuredSnapshot | null;
}

export function rowKeys(
  snap: StructuredSnapshot,
  requestKey: (r: StructuredRequest) => string,
): string[] {
  const keys: string[] = [];
  for (const t of snap.turns) {
    keys.push(`turn:${t.turn_id}`);
    for (const tool of t.tools) keys.push(`tool:${t.turn_id}:${tool.id}`);
  }
  for (const r of snap.pending_requests) keys.push(`req:${requestKey(r)}`);
  return keys;
}

export function useArrivals(
  snap: StructuredSnapshot | null,
  requestKey: (r: StructuredRequest) => string,
  outboxIds: readonly string[],
): Arrivals {
  const [state, setState] = useState<{
    snap: StructuredSnapshot | null;
    known: Set<string> | null;
    fresh: Set<string>;
    added: number;
    /** Every message this pane showed in its outbox: their turns arrived as that entry. */
    sent: Set<string>;
  }>({ snap: null, known: null, fresh: new Set(), added: 0, sent: new Set() });
  // Derived during render (React's pattern for state that follows a prop): the class is on the
  // row in the very render that mounts it.
  let current = state;
  if (outboxIds.some((id) => !current.sent.has(id)))
    current = { ...current, sent: new Set([...current.sent, ...outboxIds]) };
  if (snap && snap !== current.snap) {
    const keys = rowKeys(snap, requestKey);
    if (!current.known) {
      current = { ...current, snap, known: new Set(keys), added: 0 };
    } else {
      const known = new Set(current.known);
      const fresh = new Set(current.fresh);
      let added = 0;
      for (const k of keys) {
        if (known.has(k)) continue;
        known.add(k);
        if (k.startsWith("turn:") && current.sent.has(k.slice(5))) continue;
        fresh.add(k);
        added += 1;
      }
      current = { ...current, snap, known, fresh, added };
    }
  }
  if (current !== state) setState(current);
  const fresh = current.fresh;
  return { has: (key) => fresh.has(key), added: current.added, at: current.snap };
}

/** The arrival motion: a 6 px slide and a HUD-tint flash (500 ms) in Full, a 120 ms fade in
 *  Reduced (`nj-fade-ok`), nothing in Off (theme/motion.css). */
export const ARRIVE_CLASS = "nj-arrive nj-fade-ok";

export interface ArrivalView {
  has: (key: string) => boolean;
  /** The pane painted a snapshot before this render: text mounting now is new, not history. */
  primed: boolean;
}

export const ArrivalContext = createContext<ArrivalView>({ has: () => false, primed: false });

/** ` nj-arrive nj-fade-ok` for a row that arrived, else "" (append it to a className). */
export function useArriveClass(): (key: string) => string {
  const { has } = useContext(ArrivalContext);
  return (key) => (has(key) ? ` ${ARRIVE_CLASS}` : "");
}

/** Whether a live reply mounting in this render is new text (see `ArrivalView.primed`). */
export function usePrimed(): boolean {
  return useContext(ArrivalContext).primed;
}
