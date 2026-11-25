// Unit tests for the scroll-up lazy-load pagination state machine (#348 Phase 3):
// cursor advance, single-inflight, error → retry-same-cursor, the latched end state,
// the exact-attach-boundary seed (Hermes #365 r2 finding 1) and the terminal depth-cap
// latch (finding 2 — no refetch loop at the PagesBuffer cap).
import { describe, expect, it, vi } from "vitest";
import type { HistoryPage } from "../types/api";
import { HistoryLoader, type HistoryState } from "./historyLoader";

function deferred<T>() {
  let resolve!: (v: T) => void;
  let reject!: (e: unknown) => void;
  const promise = new Promise<T>((res, rej) => {
    resolve = res;
    reject = rej;
  });
  return { promise, resolve, reject };
}

const page = (
  ansi: string,
  cursor: number | null,
  has_more: boolean,
): HistoryPage => ({
  ansi,
  cursor,
  has_more,
});

describe("HistoryLoader", () => {
  it("first request omits `before`, then advances the cursor from the response", async () => {
    const calls: Array<number | undefined> = [];
    const fetchPage = vi.fn(async (q: { before?: number }) => {
      calls.push(q.before);
      return q.before === undefined
        ? page("p1", 40, true)
        : page("p2", 20, true);
    });
    const loader = new HistoryLoader(fetchPage);

    expect((await loader.requestOlder(80))?.ansi).toBe("p1");
    expect((await loader.requestOlder(80))?.ansi).toBe("p2");
    expect(calls).toEqual([undefined, 40]);
    expect(loader.state).toBe("idle");
  });

  it("single in-flight: a second request while one is pending is a no-op", async () => {
    const d = deferred<HistoryPage>();
    const fetchPage = vi.fn(() => d.promise);
    const loader = new HistoryLoader(fetchPage);

    const first = loader.requestOlder(80);
    expect(loader.state).toBe("loading");
    // Second call while pending: no new fetch, resolves null immediately.
    expect(await loader.requestOlder(80)).toBeNull();
    expect(fetchPage).toHaveBeenCalledTimes(1);

    d.resolve(page("p1", 10, true));
    expect((await first)?.ansi).toBe("p1");
  });

  it("has_more=false latches the end state; further requests are no-ops", async () => {
    const fetchPage = vi.fn(async () => page("last", null, false));
    const states: HistoryState[] = [];
    const loader = new HistoryLoader(fetchPage, (s) => states.push(s));

    expect((await loader.requestOlder(80))?.ansi).toBe("last");
    expect(loader.state).toBe("end");
    expect(await loader.requestOlder(80)).toBeNull();
    expect(fetchPage).toHaveBeenCalledTimes(1);
    expect(states).toEqual(["loading", "end"]);
  });

  it("an empty no-adapter page is a clean end, not an error", async () => {
    const loader = new HistoryLoader(async () => page("", null, false));
    const p = await loader.requestOlder(80);
    expect(p?.ansi).toBe("");
    expect(loader.state).toBe("end");
  });

  it("error latches until retry; retry re-issues the SAME cursor", async () => {
    const calls: Array<number | undefined> = [];
    let fail = false;
    const fetchPage = vi.fn(async (q: { before?: number }) => {
      calls.push(q.before);
      if (fail) throw new Error("boom");
      return page("p", 30, true);
    });
    const states: HistoryState[] = [];
    const loader = new HistoryLoader(fetchPage, (s) => states.push(s));

    await loader.requestOlder(80); // cursor → 30
    fail = true;
    expect(await loader.requestOlder(80)).toBeNull();
    expect(loader.state).toBe("error");
    // While errored, scroll-to-top events must NOT re-fetch.
    expect(await loader.requestOlder(80)).toBeNull();
    expect(fetchPage).toHaveBeenCalledTimes(2);

    fail = false;
    expect((await loader.retry(80))?.ansi).toBe("p");
    // The failed request and the retry used the same cursor (no advance on error).
    expect(calls).toEqual([undefined, 30, 30]);
    expect(states).toEqual([
      "loading",
      "idle",
      "loading",
      "error",
      "loading",
      "idle",
    ]);
  });

  it("seed() makes the first request carry the exact attach boundary as `before`", async () => {
    // The {"t":"hist","cursor":N} attach frame (Hermes #365 r2 finding 1): a seeded loader
    // ALWAYS sends before= — the server's width-dependent first-page guess is never used.
    const calls: Array<number | undefined> = [];
    const fetchPage = vi.fn(async (q: { before?: number }) => {
      calls.push(q.before);
      return page("p1", 3, true);
    });
    const loader = new HistoryLoader(fetchPage);
    loader.seed(7);
    await loader.requestOlder(80);
    await loader.requestOlder(80);
    expect(calls).toEqual([7, 3]); // exact boundary first, then the returned cursor
  });

  it("seed(0) leads straight to a clean end (nothing older than the attach payload)", async () => {
    const fetchPage = vi.fn(async () => page("", null, false));
    const loader = new HistoryLoader(fetchPage);
    loader.seed(0);
    await loader.requestOlder(80);
    expect(fetchPage).toHaveBeenCalledWith({ before: 0, cols: 80 });
    expect(loader.state).toBe("end");
  });

  it("latchCap() is terminal: no refetch loop after the buffer refuses a page", async () => {
    // Hermes #365 r2 finding 2: once the depth cap latches, further scroll-top events
    // must NOT refetch the same page — the fetch count stays at 1.
    const fetchPage = vi.fn(async () => page("deep page", 10, true));
    const states: HistoryState[] = [];
    const loader = new HistoryLoader(fetchPage, (s) => states.push(s));
    await loader.requestOlder(80); // fetched once; Terminal's prepend then hits the cap…
    loader.latchCap();
    expect(loader.state).toBe("capped");
    expect(await loader.requestOlder(80)).toBeNull(); // scroll-top again → no fetch
    expect(await loader.requestOlder(80)).toBeNull();
    expect(await loader.retry(80)).toBeNull(); // retry() is for errors, not the cap
    expect(fetchPage).toHaveBeenCalledTimes(1);
    expect(states).toEqual(["loading", "idle", "capped"]);
  });

  it("reset() (a server scrollback wipe) lifts a latched cap", async () => {
    const fetchPage = vi.fn(async () => page("p", null, false));
    const loader = new HistoryLoader(fetchPage);
    loader.latchCap();
    loader.reset();
    expect(loader.state).toBe("idle");
    expect((await loader.requestOlder(80))?.ansi).toBe("p");
  });

  it("passes the requested cols through to the fetch", async () => {
    const fetchPage = vi.fn(async (q: { before?: number; cols: number }) =>
      page(`w${q.cols}`, null, false),
    );
    const loader = new HistoryLoader(fetchPage);
    expect((await loader.requestOlder(123))?.ansi).toBe("w123");
    expect(fetchPage).toHaveBeenCalledWith({ before: undefined, cols: 123 });
  });
});
