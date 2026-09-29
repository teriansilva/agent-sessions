import { useCallback, useEffect, useRef, useState } from "react";

import { useDashboardRetention } from "../../app/dashboardRetentionStore";
import { useScopeEpoch } from "./useScopeEpoch";

/** One dashboard tile's read (#1123). Every tile loads, fails and retries on its OWN, so one
 *  unreadable source never blanks the page.
 *
 *  - `loading` only before the first answer — and only when nothing is retained (#1223).
 *  - A failed first read is `error`, never an empty result: "nothing" is a claim only a successful
 *    read can make.
 *  - A failed REFRESH keeps the last good data and marks it (`refreshFailed`), rather than blanking
 *    a tile that was right a moment ago.
 *  - A generation guard drops an answer that lands after a newer request, so a slow read can never
 *    paint over a fresher one. */
export type Polled<T> =
  | { status: "loading" }
  | { status: "ok"; data: T; refreshFailed: boolean }
  | { status: "error"; message: string };

/** `[state, retry, refreshing]`.
 *
 *  **Retained, then revalidated (#1223).** A source that was read before paints its last
 *  successful answer (`key`, in `DashboardRetentionProvider`) the moment the dashboard mounts, and
 *  the mount read runs behind it — the retained value decides what is drawn first, never whether
 *  to fetch.
 *
 *  `refreshing` is true while the MOUNT read or an operator's Retry is in flight over painted data:
 *  what the dashboard's bar shows. Interval polls stay silent, so the bar does not flash every
 *  30 s. The flag belongs to the generation that set it: a superseded request can neither clear a
 *  newer one's flag nor commit anywhere.
 *
 *  A change of hard scope while mounted is a new question: data painted under the old scope is
 *  dropped (the tile goes cold, not "refreshing") and re-read. */
export function usePolled<T>(
  key: string,
  fetcher: () => Promise<T>,
  everyMs: number,
): [Polled<T>, () => Promise<void>, boolean] {
  const store = useDashboardRetention();
  const { scopeKey } = store;
  const [state, setState] = useState<Polled<T>>(() => {
    const hit = store.read<T>(key);
    return hit === undefined
      ? { status: "loading" }
      : { status: "ok", data: hit, refreshFailed: false };
  });
  const [refreshing, setRefreshing] = useState(false);
  const gen = useRef(0);
  const fetchRef = useRef(fetcher);
  fetchRef.current = fetcher;
  const storeRef = useRef(store);
  storeRef.current = store;
  const load = useCallback(
    async (visible: boolean) => {
      const mine = ++gen.current;
      const forScope = storeRef.current.scopeKey;
      if (visible) setRefreshing(true);
      try {
        const data = await fetchRef.current();
        if (mine !== gen.current) return;
        storeRef.current.write(key, data, forScope);
        setState({ status: "ok", data, refreshFailed: false });
      } catch (e) {
        if (mine !== gen.current) return;
        setState((prev) =>
          prev.status === "ok"
            ? { ...prev, refreshFailed: true }
            : {
                status: "error",
                message: e instanceof Error ? e.message : "Couldn’t read.",
              },
        );
      } finally {
        if (mine === gen.current) setRefreshing(false);
      }
    },
    [key],
  );

  const scopeEpoch = useScopeEpoch(scopeKey, gen, () =>
    setState({ status: "loading" }),
  );

  useEffect(() => {
    const generation = gen; // invalidated on cleanup, so an answer landing after unmount is dropped
    void load(true);
    const t = setInterval(() => void load(false), everyMs);
    return () => {
      clearInterval(t);
      generation.current++;
      setRefreshing(false);
    };
    // A known scope change re-runs the read under the new scope.
  }, [load, everyMs, scopeEpoch]);

  const retry = useCallback(() => load(true), [load]);
  return [state, retry, refreshing];
}
