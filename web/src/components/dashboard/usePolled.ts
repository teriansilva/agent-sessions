import { useCallback, useEffect, useRef, useState } from "react";

/** One dashboard tile's read (#1123). Every tile loads, fails and retries on its OWN, so one
 *  unreadable source never blanks the page.
 *
 *  - `loading` only before the first answer.
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

export function usePolled<T>(
  fetcher: () => Promise<T>,
  everyMs: number,
): [Polled<T>, () => Promise<void>] {
  const [state, setState] = useState<Polled<T>>({ status: "loading" });
  const gen = useRef(0);
  const fetchRef = useRef(fetcher);
  fetchRef.current = fetcher;

  const load = useCallback(async () => {
    const mine = ++gen.current;
    try {
      const data = await fetchRef.current();
      if (mine === gen.current)
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
    }
  }, []);

  useEffect(() => {
    const generation = gen; // invalidated on cleanup, so an answer landing after unmount is dropped
    void load();
    const t = setInterval(() => void load(), everyMs);
    return () => {
      clearInterval(t);
      generation.current++;
    };
  }, [load, everyMs]);

  return [state, load];
}
