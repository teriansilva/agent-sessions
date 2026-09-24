import { useCallback, useEffect, useRef, useState } from "react";
import { api } from "../../lib/api";
import type { NeedsYouPayload } from "../../types/api";

type Facets = NeedsYouPayload["facets"];

/** How often the list re-reads while the page is open. Sessions move on by themselves. */
export const NEEDS_YOU_POLL_MS = 30_000;

export type NeedsYouState =
  | { status: "loading"; data: NeedsYouPayload | null }
  | { status: "ok"; data: NeedsYouPayload }
  | { status: "error"; data: NeedsYouPayload | null; message: string };

/** The NEEDS YOU list for the given window and filters (#1086).
 *
 *  **A read is committed only if it is still the newest one asked for.** Changing a filter starts a
 *  new generation, so a slower answer for the previous filters can never paint over the current
 *  ones. "Nothing needs you" is only a claim after a SUCCESSFUL read: loading and failure are their
 *  own states, and a failed refresh keeps the last good list rather than blanking it. */
export function useNeedsYou(windowDays: number, engine: string, project: string) {
  const [state, setState] = useState<NeedsYouState>({ status: "loading", data: null });
  // The FILTER OPTIONS outlive a filter change. Facets are computed over the unfiltered set, so
  // they do not depend on the filter; dropping them while a filtered read loads left the dropdown
  // with nothing but "all" and the current choice — the operator could not change their mind until
  // a slow answer came back (found by the late-answer race test).
  const [facets, setFacets] = useState<Facets | null>(null);
  // MEMBERSHIP outlives a filter change too (review 5184): which sessions need you, unfiltered, so
  // Ask answers keep their markers and the pinned count is never a filtered subset.
  const [membership, setMembership] = useState<{ ids: Set<string>; total: number } | null>(null);
  const gen = useRef(0);

  const load = useCallback(async () => {
    const mine = ++gen.current;
    try {
      const data = await api.needsYou({ window_days: windowDays, engine, project });
      if (mine === gen.current) {
        setState({ status: "ok", data });
        if (data?.facets) setFacets(data.facets);
        if (Array.isArray(data?.needs_you_ids)) {
          setMembership({ ids: new Set(data.needs_you_ids), total: data.total_unfiltered });
        }
      }
    } catch (e) {
      if (mine !== gen.current) return;
      setState((prev) => ({
        status: "error",
        data: prev.data,
        message: e instanceof Error ? e.message : "Couldn’t read sessions.",
      }));
    }
  }, [windowDays, engine, project]);

  useEffect(() => {
    // A new window or filter is a NEW QUESTION: its rows are not the old ones, so it starts from
    // "reading" with nothing painted. (A poll within the same filters refreshes in place.)
    setState({ status: "loading", data: null });
    void load();
    const t = setInterval(() => void load(), NEEDS_YOU_POLL_MS);
    return () => {
      clearInterval(t);
      gen.current++; // anything still in flight belongs to a question nobody is asking now
    };
  }, [load]);

  // A STABLE refresh that always reads the CURRENT scope (review 5184). A decision settled from a
  // row calls this after its request returns — possibly after the operator changed the filter. A
  // captured `load` would re-read the OLD filter and, as the newest generation, paint its rows
  // under the new selector.
  const latest = useRef(load);
  useEffect(() => {
    latest.current = load;
  }, [load]);
  const refresh = useCallback(() => latest.current(), []);

  return { state, facets, membership, refresh };
}
