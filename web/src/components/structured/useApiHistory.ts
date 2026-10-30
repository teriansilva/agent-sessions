/** Scrolling up past an API session's snapshot window (#1504).
 *
 *  The snapshot carries the newest turns and `history_before`, the oldest turn of its window. An
 *  operator gesture upward near the top asks for the page just older than the oldest turn held;
 *  pages are prepended and the reading position is kept. What was loaded survives every snapshot
 *  refresh: when the window slides on (new turns), the turns it let go are read back so nothing
 *  between the loaded pages and the window goes missing. Merged by `turn_id`; the snapshot's
 *  copy of a turn wins. Nothing is fetched before a gesture, and one request runs at a time.
 *
 *  Worker ends (#1724) travel with the turn they follow: each page carries the ends anchored to
 *  its own turns (and the first page those before the first turn), and they are held with them. */
import { useCallback, useEffect, useLayoutEffect, useMemo, useRef, useState } from "react";
import type { KeyboardEvent, PointerEvent, RefObject, TouchEvent, WheelEvent } from "react";

import { ApiError, api } from "../../lib/api";
import type {
  StructuredAgentTurn,
  StructuredHistoryPage,
  StructuredSnapshot,
  StructuredTurn,
  StructuredWorkerEnd,
} from "../../types/api";

type Turn = StructuredTurn | StructuredAgentTurn;
/** Read-back progress after a window slide: turns read so far (contiguous, older than
 *  `boundary`) and the cursor to read on from. */
type Chain = { boundary: string; cursor: string; read: Turn[]; ends: StructuredWorkerEnd[] };

/** What one pane keeps, at most. Past it the top says so and nothing older loads. */
export const HISTORY_CAP_TURNS = 1_000;
export const HISTORY_CAP_CHARS = 4_000_000;
export const HISTORY_PAGE = 50;
/** How many held turns a lost cursor (409) is retried from before the pane says it failed. */
const RECOVERY_TRIES = 5;
/** Most pages one window slide is read back with; a burst bigger than this re-reads next time. */
const GAP_PAGES = 10;
/** A scroll counts as the operator's for this long after their last input (wheel, touch, key,
 *  pointer). A scroll with no input behind it — a smooth-scroll animation still running after a
 *  page landed — never asks for another page. */
const INPUT_WINDOW_MS = 600;

export type HistoryStatus = "idle" | "loading" | "error" | "end" | "capped";

export function turnChars(t: Turn): number {
  return ("text" in t ? (t.text ?? "").length : 0) + (t.reply ?? "").length;
}

/** Older turns placed above what is held, within the cap: the newest of the page are kept when
 *  only part of it fits, so the held turns stay contiguous. */
export function prependPage(
  held: Turn[],
  page: Turn[],
  cap = { turns: HISTORY_CAP_TURNS, chars: HISTORY_CAP_CHARS },
): { held: Turn[]; capped: boolean } {
  const known = new Set(held.map((t) => t.turn_id));
  const fresh = page.filter((t) => !known.has(t.turn_id));
  let turns = held.length;
  let chars = held.reduce((n, t) => n + turnChars(t), 0);
  let start = fresh.length;
  while (start > 0) {
    const t = fresh[start - 1];
    if (turns + 1 > cap.turns || chars + turnChars(t) > cap.chars) break;
    turns += 1;
    chars += turnChars(t);
    start -= 1;
  }
  return { held: [...fresh.slice(start), ...held], capped: start > 0 };
}

/** Turns read back after the window slid on, placed below what is held. The oldest held turns
 *  give way if that passes the cap, and the pane then says it stopped. */
export function appendGap(
  held: Turn[],
  page: Turn[],
  cap = { turns: HISTORY_CAP_TURNS, chars: HISTORY_CAP_CHARS },
): { held: Turn[]; capped: boolean } {
  const known = new Set(held.map((t) => t.turn_id));
  const next = [...held, ...page.filter((t) => !known.has(t.turn_id))];
  let chars = next.reduce((n, t) => n + turnChars(t), 0);
  let drop = 0;
  while (drop < next.length && (next.length - drop > cap.turns || chars > cap.chars)) {
    chars -= turnChars(next[drop]);
    drop += 1;
  }
  return { held: next.slice(drop), capped: drop > 0 };
}

/** The whole conversation in journal order: held history merged with the snapshot.
 *
 *  The snapshot is its WINDOW (from `history_before` on) plus any older turn it pulls forward
 *  because it is still live (a queued or running message, `structured_runtime._snapshot`). A
 *  pulled-forward turn sits at its own place in history: among the held turns when a loaded page
 *  covers it, otherwise above them all, since the held turns are a contiguous run ending at the
 *  window and anything older lies above it (Hermes on #1516). Every turn the snapshot shows is
 *  rendered as the snapshot's copy, once. `seam` is the first window turn, where loaded history
 *  meets it, or undefined when nothing is held. */
export function mergeHistory(
  held: Turn[],
  snapTurns: Turn[],
  historyBefore: string | null | undefined,
): { turns: Turn[]; seam: string | undefined } {
  if (!held.length) return { turns: snapTurns, seam: undefined };
  const at = historyBefore ? snapTurns.findIndex((t) => t.turn_id === historyBefore) : -1;
  const pulled = at > 0 ? snapTurns.slice(0, at) : [];
  const window = at >= 0 ? snapTurns.slice(at) : snapTurns;
  const windowIds = new Set(window.map((t) => t.turn_id));
  const snapById = new Map(snapTurns.map((t) => [t.turn_id, t] as const));
  const heldIds = new Set(held.map((t) => t.turn_id));
  const history = held
    .filter((t) => !windowIds.has(t.turn_id))
    .map((t) => snapById.get(t.turn_id) ?? t);
  const above = pulled.filter((t) => !heldIds.has(t.turn_id));
  return { turns: [...above, ...history, ...window], seam: window[0]?.turn_id };
}

/** Ends a page brought, kept only where they render: after a turn now held, or (`after_turn`
 *  null) when the page reached the first turn with nothing cut. Merged by worker, the newer copy
 *  winning; the order is the server's. */
export function holdEnds(
  held: StructuredWorkerEnd[],
  page: StructuredWorkerEnd[] | undefined,
  turns: Turn[],
  reachedFirst: boolean,
): StructuredWorkerEnd[] {
  const ids = new Set(turns.map((t) => t.turn_id));
  const kept = (page ?? []).filter((e) =>
    e.after_turn === null ? reachedFirst : ids.has(e.after_turn),
  );
  if (!kept.length) return held;
  const fresh = new Set(kept.map((e) => e.worker));
  return [...kept, ...held.filter((e) => !fresh.has(e.worker))];
}

/** Every end to render: the held pages' and the snapshot's (whose copy of a worker wins). */
export function mergeEnds(
  held: StructuredWorkerEnd[],
  snapEnds: StructuredWorkerEnd[] | undefined,
): StructuredWorkerEnd[] {
  const own = new Set((snapEnds ?? []).map((e) => e.worker));
  return [...held.filter((e) => !own.has(e.worker)), ...(snapEnds ?? [])];
}

export interface ApiHistory {
  /** The conversation to render: held history merged with the snapshot, in journal order. */
  turns: Turn[];
  /** Work that ended with a worker (#1724): the held pages' and the snapshot's, each placed by
   *  its `after_turn`. History's never animate (they are not in any snapshot). */
  ends: StructuredWorkerEnd[];
  /** The first window turn when history is held: where the "earlier turns" seam goes. */
  seam: string | undefined;
  /** The window slid on and the turns between it and the held history are still being read. */
  catchingUp: boolean;
  status: HistoryStatus;
  retry: () => void;
  /** Handlers for the scrolling log: they arm a page on an upward gesture near the top. */
  handlers: {
    onScroll: () => void;
    onWheel: (e: WheelEvent<HTMLElement>) => void;
    onTouchStart: (e: TouchEvent<HTMLElement>) => void;
    onTouchMove: (e: TouchEvent<HTMLElement>) => void;
    onKeyDown: (e: KeyboardEvent<HTMLElement>) => void;
    onPointerDown: (e: PointerEvent<HTMLElement>) => void;
    onPointerMove: (e: PointerEvent<HTMLElement>) => void;
  };
}

export function useApiHistory(
  key: string,
  snap: StructuredSnapshot | null,
  logRef: RefObject<HTMLDivElement | null>,
  reload: () => Promise<StructuredSnapshot | null>,
): ApiHistory {
  const [held, setHeld] = useState<Turn[]>([]);
  const [heldEnds, setHeldEnds] = useState<StructuredWorkerEnd[]>([]);
  const heldEndsRef = useRef<StructuredWorkerEnd[]>([]);
  const [status, setStatus] = useState<HistoryStatus>("idle");
  const heldRef = useRef<Turn[]>([]);
  const statusRef = useRef<HistoryStatus>("idle");
  const snapRef = useRef(snap);
  // Where the next older page starts; undefined until the first page (then the snapshot's).
  const top = useRef<string | null | undefined>(undefined);
  // The window boundary the held turns reach down to.
  const joined = useRef<string | null | undefined>(undefined);
  const busy = useRef(false);
  const generation = useRef(0);
  const anchor = useRef<{ el: Element; top: number } | null>(null);
  const lastTop = useRef(0);
  const touchY = useRef<number | null>(null);
  const lastInput = useRef(0);
  const chain = useRef<Chain | null>(null);
  const [catchingUp, setCatchingUp] = useState(false);

  const setBoth = useCallback((next: Turn[]) => {
    heldRef.current = next;
    setHeld(next);
  }, []);
  const keepEnds = useCallback(
    (page: StructuredWorkerEnd[] | undefined, turns: Turn[], reachedFirst: boolean) => {
      const next = holdEnds(heldEndsRef.current, page, turns, reachedFirst);
      if (next === heldEndsRef.current) return;
      heldEndsRef.current = next;
      setHeldEnds(next);
    },
    [],
  );
  const setState = useCallback((next: HistoryStatus) => {
    statusRef.current = next;
    setStatus(next);
  }, []);

  useLayoutEffect(() => {
    snapRef.current = snap;
  }, [snap]);
  // The pane is keyed by session, so a new session is a new hook. A read still in flight when
  // this one goes must not land anywhere.
  useEffect(
    () => () => {
      generation.current += 1;
    },
    [],
  );

  /** The first turn on screen and where it sits, so a change above it can keep it there. */
  const holdPlace = useCallback(() => {
    const log = logRef.current;
    if (!log) return;
    const edge = log.getBoundingClientRect().top;
    for (const child of log.children) {
      // A turn, never a history row: those are replaced by the very change being anchored.
      if (child instanceof HTMLElement && child.dataset.historyRow !== undefined) continue;
      const rect = child.getBoundingClientRect();
      if (rect.bottom > edge) {
        anchor.current = { el: child, top: rect.top };
        return;
      }
    }
  }, [logRef]);

  // Put the anchored turn back where it was. A browser that anchors by itself already did, and
  // then this moves nothing; one that does not (WebKit) is corrected here.
  useLayoutEffect(() => {
    const log = logRef.current;
    const a = anchor.current;
    anchor.current = null;
    if (!log || !a || !a.el.isConnected) return;
    const moved = a.el.getBoundingClientRect().top - a.top;
    if (moved) log.scrollTop += moved;
    lastTop.current = log.scrollTop;
  }, [held, logRef]);

  const page = useCallback(
    (before: string) => api.structuredHistory(key, before, HISTORY_PAGE),
    [key],
  );

  /** Read a page, and on a lost cursor (409) try again from the next newer held turn, then the
   *  window's own boundary, after re-reading the snapshot. Never drops what is held. */
  const readOlder = useCallback(
    async (before: string): Promise<StructuredHistoryPage> => {
      try {
        return await page(before);
      } catch (e) {
        if (!(e instanceof ApiError && e.status === 409)) throw e;
      }
      const fresh = (await reload()) ?? snapRef.current;
      const tries = [
        ...heldRef.current.map((t) => t.turn_id).filter((id) => id !== before),
        ...(fresh?.history_before ? [fresh.history_before] : []),
      ].slice(0, RECOVERY_TRIES);
      let last: unknown = null;
      for (const id of tries) {
        try {
          return await page(id);
        } catch (e) {
          if (!(e instanceof ApiError && e.status === 409)) throw e;
          last = e;
        }
      }
      throw last ?? new Error("history cursor not found");
    },
    [page, reload],
  );

  // A failed read waits for Retry: a scroll alone never repeats it behind the operator's back.
  const loadOlder = useCallback(async (retrying = false) => {
    const s = snapRef.current;
    const st = statusRef.current;
    if (busy.current || !s || st === "end" || st === "capped" || (st === "error" && !retrying))
      return;
    const start = top.current === undefined ? s.history_before : top.current;
    if (!start) return;
    const gen = generation.current;
    busy.current = true;
    setState("loading");
    try {
      const result = await readOlder(start);
      if (gen !== generation.current) return;
      const { held: next, capped } = prependPage(heldRef.current, result.turns);
      if (joined.current === undefined) joined.current = s.history_before ?? null;
      top.current = result.before;
      // What scrolls after this point is the old gesture finishing, not a new one.
      lastInput.current = 0;
      holdPlace();
      setBoth(next);
      keepEnds(result.worker_ends, next, !capped && !result.has_more);
      setState(capped ? "capped" : result.has_more ? "idle" : "end");
    } catch {
      if (gen === generation.current) setState("error");
    } finally {
      if (gen === generation.current) busy.current = false;
    }
  }, [readOlder, holdPlace, setBoth, keepEnds, setState]);

  // The window slid on while pages are held: read back what it let go, down to a held turn,
  // in bounded batches (GAP_PAGES per snapshot) that RESUME across snapshots, so the held
  // history is never dropped and the two spans always join in the end (Hermes on #1516).
  // `chain` is the progress: the turns read so far, contiguous and older than `boundary`, and the
  // cursor to read on from. Every snapshot re-checks, so a read skipped (busy) or failed resumes.
  useEffect(() => {
    const boundary = snap?.history_before;
    if (!heldRef.current.length || joined.current === undefined || boundary === joined.current) {
      if (chain.current) setCatchingUp(false);
      chain.current = null;
      return;
    }
    if (!boundary || heldRef.current.some((t) => t.turn_id === boundary)) {
      joined.current = boundary ?? null;
      if (chain.current) setCatchingUp(false);
      chain.current = null;
      return;
    }
    if (busy.current) return; // the next snapshot tries again
    const gen = generation.current;
    busy.current = true;
    void (async () => {
      try {
        let budget = GAP_PAGES;
        const heldIds = new Set(heldRef.current.map((t) => t.turn_id));
        let c: Chain | null = chain.current;
        if (c && c.boundary !== boundary) {
          // The window moved on again mid-way: read the newer stretch down to the chain first.
          const chainIds = new Set(c.read.map((t) => t.turn_id));
          const newer: Turn[] = [];
          const newerEnds: StructuredWorkerEnd[] = [];
          let cursor = boundary;
          let met = false;
          while (budget > 0) {
            const result = await page(cursor);
            if (gen !== generation.current) return;
            budget -= 1;
            newer.unshift(...result.turns);
            newerEnds.unshift(...(result.worker_ends ?? []));
            if (
              !result.has_more ||
              !result.before ||
              result.turns.some((t) => chainIds.has(t.turn_id) || heldIds.has(t.turn_id))
            ) {
              met = true;
              break;
            }
            cursor = result.before;
          }
          c = met
            ? {
                boundary,
                cursor: c.cursor,
                read: [...c.read, ...newer.filter((t) => !chainIds.has(t.turn_id))],
                ends: [...c.ends, ...newerEnds],
              }
            : // A slide wider than a batch on top of a pending one: this stretch becomes the
              // chain (the held turns stay; only the older partial read is read again).
              { boundary, cursor, read: newer, ends: newerEnds };
        }
        if (!c) c = { boundary, cursor: boundary, read: [], ends: [] };
        let joins = c.read.some((t) => heldIds.has(t.turn_id));
        while (!joins && budget > 0) {
          // The saved cursor may have been removed from the journal since the last batch
          // (#1447): retry from the next surviving turn already read, as scrolling up does
          // (Hermes on #1516). If none answers, start the read-back again from the window; the
          // held turns are kept either way.
          const tries = [c.cursor, ...c.read.map((t) => t.turn_id).filter((id) => id !== c?.cursor)];
          let result: StructuredHistoryPage | null = null;
          for (const id of tries.slice(0, RECOVERY_TRIES)) {
            try {
              result = await page(id);
              break;
            } catch (e) {
              if (!(e instanceof ApiError && e.status === 409)) throw e;
            }
          }
          if (gen !== generation.current) return;
          budget -= 1;
          if (!result) {
            c = { boundary, cursor: boundary, read: [], ends: [] };
            break;
          }
          const have: Set<string> = new Set(c.read.map((t: Turn) => t.turn_id));
          c = {
            ...c,
            read: [...result.turns.filter((t) => !have.has(t.turn_id)), ...c.read],
            ends: [...(result.worker_ends ?? []), ...c.ends],
          };
          if (
            !result.has_more ||
            !result.before ||
            result.turns.some((t) => heldIds.has(t.turn_id))
          ) {
            joins = true;
            break;
          }
          c = { ...c, cursor: result.before };
        }
        if (!joins) {
          chain.current = c; // resumes on the next snapshot
          setCatchingUp(true);
          return;
        }
        chain.current = null;
        setCatchingUp(false);
        const { held: next, capped } = appendGap(heldRef.current, c.read);
        holdPlace();
        setBoth(next);
        keepEnds(c.ends, next, false);
        joined.current = boundary;
        if (capped) setState("capped");
      } catch {
        // The chain stays as it was; the next snapshot resumes it.
      } finally {
        if (gen === generation.current) busy.current = false;
      }
    })();
  }, [snap, page, holdPlace, setBoth, keepEnds, setState]);

  const nearTop = () => {
    const log = logRef.current;
    return !!log && log.scrollTop <= log.clientHeight;
  };
  const input = () => {
    lastInput.current = performance.now();
  };
  const handlers = useMemo(
    () => ({
      onScroll: () => {
        const log = logRef.current;
        if (!log) return;
        const up = log.scrollTop < lastTop.current;
        lastTop.current = log.scrollTop;
        const operator = performance.now() - lastInput.current < INPUT_WINDOW_MS;
        if (up && operator && nearTop()) void loadOlder();
      },
      // A log too short to scroll still answers a wheel or a swipe down at its top.
      onWheel: (e: WheelEvent<HTMLElement>) => {
        input();
        if (e.deltaY < 0 && nearTop()) void loadOlder();
      },
      onTouchStart: (e: TouchEvent<HTMLElement>) => {
        input();
        touchY.current = e.touches[0]?.clientY ?? null;
      },
      onTouchMove: (e: TouchEvent<HTMLElement>) => {
        input();
        const y = e.touches[0]?.clientY;
        if (touchY.current === null || y === undefined) return;
        if (y - touchY.current > 12 && nearTop()) {
          touchY.current = null;
          void loadOlder();
        }
      },
      // Keys (PageUp, Home, arrows) and a dragged scrollbar scroll the log too.
      onKeyDown: () => input(),
      onPointerDown: () => input(),
      onPointerMove: (e: PointerEvent<HTMLElement>) => {
        if (e.buttons) input();
      },
    }),
    // eslint-disable-next-line react-hooks/exhaustive-deps
    [loadOlder, logRef],
  );

  const retry = useCallback(() => void loadOlder(true), [loadOlder]);

  const merged = useMemo(
    () => mergeHistory(held, snap?.turns ?? [], snap?.history_before),
    [held, snap?.turns, snap?.history_before],
  );
  const ends = useMemo(
    () => mergeEnds(heldEnds, snap?.native?.worker_ends),
    [heldEnds, snap?.native?.worker_ends],
  );
  return { turns: merged.turns, seam: merged.seam, ends, catchingUp, status, retry, handlers };
}
