/** One lazily paged lineage read — a session's children or its attention descendants (#2325) —
 *  that re-reads while it is mounted. Split from `Subagents.tsx` so it can be tested alone. */
import { useCallback, useEffect, useRef, useState } from "react";
import { api } from "../../lib/api";
import type { Session, SessionLineagePage } from "../../types/api";

/** Children and attention rows fetched per request. */
export const SUBAGENT_PAGE = 50;
/** The server's page ceiling on the children and attention routes. */
export const MAX_LIMIT = 200;
/** How often an open group re-reads its subagents (the list's own poll cadence). */
const POLL_MS = 15_000;

export interface PagedState {
  items: Session[];
  total: number;
  next: number | null;
  loading: boolean;
  error: boolean;
}

const EMPTY: PagedState = { items: [], total: 0, next: null, loading: false, error: false };

/** One lazily paged lineage read (children or attention) that re-reads while it is mounted. */
export function useLineagePages(
  id: string,
  kind: "children" | "attention",
  enabled: boolean,
  archived: boolean,
  version: string,
): PagedState & { more: () => void; retry: () => void } {
  const [st, setSt] = useState<PagedState>(EMPTY);
  const gen = useRef(0);
  const loaded = useRef(0);
  /** Read `count` rows from `offset`, in pages the server accepts (≤ MAX_LIMIT each), and either
   *  append them or replace what is shown. A refresh of a window larger than one page is several
   *  bounded requests, never one oversized one (#2325 review). */
  const read = useCallback(
    async (offset: number, count: number, replace: boolean) => {
      const g = ++gen.current;
      setSt((p) => ({ ...p, loading: true, error: false }));
      try {
        const fn = kind === "children" ? api.sessionChildren : api.sessionAttention;
        const got: Session[] = [];
        let at: number | null = offset;
        let page: SessionLineagePage | null = null;
        while (at != null && got.length < count) {
          const limit = Math.min(MAX_LIMIT, count - got.length);
          page = await fn(id, { offset: at, limit, archived });
          if (g !== gen.current) return;
          got.push(...page.sessions);
          at = page.sessions.length ? page.next_offset : null;
          // A short page is the server's own boundary: what it did not send is "more", not a
          // reason to keep reading.
          if (page.sessions.length < limit) break;
        }
        if (!page) return;
        const last = page;
        setSt((p) => {
          const items = replace ? got : [...p.items, ...got];
          loaded.current = items.length;
          return { items, total: last.total, next: at, loading: false, error: false };
        });
      } catch {
        if (g !== gen.current) return;
        setSt((p) => ({ ...p, loading: false, error: true }));
      }
    },
    [id, kind, archived],
  );
  const retained = () => Math.max(SUBAGENT_PAGE, loaded.current);
  useEffect(() => {
    if (!enabled) {
      // A closed group keeps its last read (shown again, then refreshed, when it reopens) and
      // drops any read still in flight.
      gen.current += 1;
      return;
    }
    void read(0, Math.max(SUBAGENT_PAGE, loaded.current), true);
    const t = setInterval(
      () => void read(0, Math.max(SUBAGENT_PAGE, loaded.current), true),
      POLL_MS,
    );
    return () => clearInterval(t);
    // `version` re-reads when the main row's lineage facts move on the list's poll.
  }, [enabled, read, version]);
  return {
    ...st,
    more: () => {
      if (st.next != null) void read(st.next, SUBAGENT_PAGE, false);
    },
    retry: () => void read(0, retained(), true),
  };
}

