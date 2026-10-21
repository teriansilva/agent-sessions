import { useCallback, useEffect, useRef, useState } from "react";
import { useDashboardRetention } from "../../app/dashboardRetentionStore";
import { api } from "../../lib/api";
import { useScopeEpoch } from "../dashboard/useScopeEpoch";
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
type Membership = { ids: Set<string>; total: number };
/** One read, whole: the list AND the facets + unfiltered membership that came WITH it. */
type Snapshot = {
  data: NeedsYouPayload;
  facets: Facets | null;
  membership: Membership | null;
};

function membershipOf(data: NeedsYouPayload): Membership | null {
  return Array.isArray(data?.needs_you_ids)
    ? { ids: new Set(data.needs_you_ids), total: data.total_unfiltered }
    : null;
}

/** **Retained, then revalidated (#1223).** Each `(window, engine, project)` question retains ONE
 *  snapshot — its list together with the facets and unfiltered membership (the dashboard's KPI)
 *  from the SAME read — and restores all three together. Keeping the list per question but the
 *  membership per window paired a restored list with a newer read's count (review 5407). A mount or
 *  a filter change paints the retained answer to THAT question, if there is one, and reads behind
 *  it; `refreshing` covers that read (and `refresh()`), never the interval poll. */
export function useNeedsYou(windowDays: number, engine: string, project: string) {
  const store = useDashboardRetention();
  const storeRef = useRef(store);
  storeRef.current = store;
  const listKey = `needsYou:${windowDays}:${engine}:${project}`;
  const [state, setState] = useState<NeedsYouState>(() => {
    const hit = store.read<Snapshot>(listKey);
    return hit ? { status: "ok", data: hit.data } : { status: "loading", data: null };
  });
  const [refreshing, setRefreshing] = useState(false);
  // The FILTER OPTIONS outlive a filter change. Facets are computed over the unfiltered set, so
  // they do not depend on the filter; dropping them while a filtered read loads left the dropdown
  // with nothing but "all" and the current choice — the operator could not change their mind until
  // a slow answer came back (found by the late-answer race test).
  const [facets, setFacets] = useState<Facets | null>(
    () => store.read<Snapshot>(listKey)?.facets ?? null,
  );
  // MEMBERSHIP outlives a filter change too (review 5184): which sessions need you, unfiltered, so
  // Ask answers keep their markers and the pinned count is never a filtered subset.
  const [membership, setMembership] = useState<Membership | null>(
    () => store.read<Snapshot>(listKey)?.membership ?? null,
  );
  const gen = useRef(0);

  const load = useCallback(async (visible: boolean) => {
    const mine = ++gen.current;
    const forScope = storeRef.current.scopeKey;
    if (visible) setRefreshing(true);
    try {
      const data = await api.needsYou({ window_days: windowDays, engine, project });
      if (mine === gen.current) {
        setState({ status: "ok", data });
        const snap: Snapshot = {
          data,
          facets: data?.facets ?? null,
          membership: membershipOf(data),
        };
        if (snap.facets) setFacets(snap.facets);
        if (snap.membership) setMembership(snap.membership);
        storeRef.current.write(`needsYou:${windowDays}:${engine}:${project}`, snap, forScope);
      }
    } catch (e) {
      if (mine !== gen.current) return;
      setState((prev) => ({
        status: "error",
        data: prev.data,
        message: e instanceof Error ? e.message : "Couldn’t read sessions.",
      }));
    } finally {
      if (mine === gen.current) setRefreshing(false);
    }
  }, [windowDays, engine, project]);

  // Another scope's list, facets and count are wrong data: all of it goes, and the list re-reads.
  const scopeEpoch = useScopeEpoch(store.scopeKey, gen, () => {
    setState({ status: "loading", data: null });
    setFacets(null);
    setMembership(null);
  });

  useEffect(() => {
    // A new window or filter is a NEW QUESTION: its rows are not the old ones, so it starts from
    // "reading" with nothing painted — unless THIS question was answered before, in which case its
    // own retained answer paints while it re-reads (#1223). (A poll refreshes in place.)
    const hit = storeRef.current.read<Snapshot>(listKey);
    if (hit) {
      // The list, its facets and its count, from ONE read — never a list beside another's count.
      setState({ status: "ok", data: hit.data });
      if (hit.facets) setFacets(hit.facets);
      if (hit.membership) setMembership(hit.membership);
    } else {
      setState({ status: "loading", data: null });
    }
    void load(true);
    const t = setInterval(() => void load(false), NEEDS_YOU_POLL_MS);
    return () => {
      clearInterval(t);
      gen.current++; // anything still in flight belongs to a question nobody is asking now
      setRefreshing(false);
    };
  }, [load, listKey, scopeEpoch]);

  // A STABLE refresh that always reads the CURRENT scope (review 5184). A decision settled from a
  // row calls this after its request returns — possibly after the operator changed the filter. A
  // captured `load` would re-read the OLD filter and, as the newest generation, paint its rows
  // under the new selector.
  const latest = useRef(load);
  useEffect(() => {
    latest.current = load;
  }, [load]);
  const refresh = useCallback(() => latest.current(true), []);

  return { state, facets, membership, refresh, refreshing };
}
