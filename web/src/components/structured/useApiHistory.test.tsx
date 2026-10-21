import { act, renderHook, waitFor } from "@testing-library/react";
import { afterEach, describe, expect, it, vi } from "vitest";

import { ApiError, api } from "../../lib/api";
import type { StructuredHistoryPage, StructuredSnapshot, StructuredTurn } from "../../types/api";
import { appendGap, mergeHistory, prependPage, turnChars, useApiHistory } from "./useApiHistory";

const turn = (id: string, text = id): StructuredTurn =>
  ({ kind: "operator", turn_id: id, operation_id: id, state: "completed", text, reply: "" }) as StructuredTurn;
const ids = (ts: { turn_id: string }[]) => ts.map((t) => t.turn_id);
/** The held history the pane shows above the window: every turn before the seam. */
const above = (h: { turns: { turn_id: string }[]; seam: string | undefined }) =>
  h.seam ? h.turns.slice(0, h.turns.findIndex((t) => t.turn_id === h.seam)) : [];
const range = (from: number, to: number) =>
  Array.from({ length: to - from }, (_, i) => turn(`t${from + i}`));

function snapshot(window: StructuredTurn[], before: string | null): StructuredSnapshot {
  return {
    session_key: "codex-api:s",
    revision: 1,
    event_cursor: 1,
    cwd: "/w",
    state: "idle",
    active_turn: null,
    model_requested: null,
    turns: window,
    omitted_turns: 0,
    history_before: before,
    pending_requests: [],
  } as StructuredSnapshot;
}

/** A server holding turns t0…t(n-1), answering pages as the route does. */
function server(n: number) {
  const all = range(0, n);
  return (_key: string, before: string, limit = 50): Promise<StructuredHistoryPage> => {
    const at = all.findIndex((t) => t.turn_id === before);
    if (at < 0) return Promise.reject(new ApiError(409, "history cursor not found"));
    const turns = all.slice(Math.max(0, at - limit), at);
    const start = Math.max(0, at - limit);
    return Promise.resolve({
      session_key: "codex-api:s",
      turns,
      before: start > 0 ? turns[0].turn_id : null,
      has_more: start > 0,
    });
  };
}

function setup(snap: StructuredSnapshot, reload = vi.fn(async () => null)) {
  const log = document.createElement("div");
  const ref = { current: log };
  const hook = renderHook(({ s }) => useApiHistory("codex-api:s", s, ref, reload), {
    initialProps: { s: snap },
  });
  const up = () =>
    act(() => hook.result.current.handlers.onWheel({ deltaY: -10 } as never));
  return { ...hook, up, reload };
}

afterEach(() => vi.restoreAllMocks());

describe("the pure merge", () => {
  it("prepends a page above what is held, once per turn", () => {
    const { held, capped } = prependPage(range(3, 6), [...range(0, 3), turn("t3")]);
    expect(ids(held)).toEqual(["t0", "t1", "t2", "t3", "t4", "t5"]);
    expect(capped).toBe(false);
  });

  it("keeps the newest of a page that only partly fits, and says it stopped", () => {
    const { held, capped } = prependPage(range(3, 5), range(0, 3), { turns: 4, chars: 1e9 });
    expect(ids(held)).toEqual(["t1", "t2", "t3", "t4"]);
    expect(capped).toBe(true);
  });

  it("counts characters toward the cap as well as turns", () => {
    const big = turn("big", "x".repeat(10));
    expect(turnChars(big)).toBe(10);
    const { held, capped } = prependPage([turn("a", "12345")], [big], { turns: 99, chars: 12 });
    expect(ids(held)).toEqual(["a"]);
    expect(capped).toBe(true);
  });

  it("reads turns back below what is held, the oldest giving way past the cap", () => {
    const { held, capped } = appendGap(range(0, 3), [turn("t2"), ...range(3, 5)], {
      turns: 4,
      chars: 1e9,
    });
    expect(ids(held)).toEqual(["t1", "t2", "t3", "t4"]);
    expect(capped).toBe(true);
  });

  it("never shows a held turn twice, and renders the snapshot's copy", () => {
    const fresh = turn("t4", "fresh");
    const { turns, seam } = mergeHistory(range(0, 5), [fresh, ...range(5, 7)], "t4");
    expect(ids(turns)).toEqual(["t0", "t1", "t2", "t3", "t4", "t5", "t6"]);
    expect(turns[4]).toBe(fresh);
    expect(seam).toBe("t4");
  });

  it("a live turn the snapshot pulled forward stays above newer history (Hermes on #1516)", () => {
    // Queued turn 10 is shown by the snapshot ahead of its window 70–119; page 20–69 is held.
    const queued = turn("t10", "still queued");
    const snap = [queued, ...range(70, 120)];
    const { turns, seam } = mergeHistory(range(20, 70), snap, "t70");
    expect(ids(turns)).toEqual(["t10", ...ids(range(20, 120))]);
    expect(seam).toBe("t70");
    // Once a page covers it, it sits at its own place: between 9 and 11, as the snapshot's copy.
    const deeper = mergeHistory(range(0, 70), snap, "t70");
    expect(ids(deeper.turns)).toEqual(ids(range(0, 120)));
    expect(deeper.turns[10]).toBe(queued);
  });

  it("with nothing held, the snapshot renders exactly as it came", () => {
    const snap = [turn("t10"), ...range(70, 72)];
    expect(mergeHistory([], snap, "t70")).toEqual({ turns: snap, seam: undefined });
  });
});

describe("useApiHistory", () => {
  it("fetches nothing until the operator scrolls up", async () => {
    const spy = vi.spyOn(api, "structuredHistory").mockImplementation(server(120));
    const { result, rerender } = setup(snapshot(range(70, 120), "t70"));
    rerender({ s: snapshot(range(70, 120), "t70") });
    await act(async () => {});
    expect(spy).not.toHaveBeenCalled();
    expect(above(result.current)).toEqual([]);
    expect(result.current.status).toBe("idle");
  });

  it("walks back a page per gesture to the beginning", async () => {
    const spy = vi.spyOn(api, "structuredHistory").mockImplementation(server(120));
    const { result, up } = setup(snapshot(range(70, 120), "t70"));
    await up();
    await waitFor(() => expect(above(result.current)).toHaveLength(50));
    expect(spy).toHaveBeenLastCalledWith("codex-api:s", "t70", 50);
    expect(result.current.status).toBe("idle");
    await up();
    await waitFor(() => expect(result.current.status).toBe("end"));
    expect(ids(above(result.current))).toEqual(ids(range(0, 70)));
    // At the beginning a gesture asks for nothing more.
    await up();
    expect(spy).toHaveBeenCalledTimes(2);
  });

  it("asks once while a page is in flight", async () => {
    let release!: () => void;
    const spy = vi.spyOn(api, "structuredHistory").mockImplementation(
      (...a) => new Promise((r) => (release = () => r(server(120)(...a)))),
    );
    const { result, up } = setup(snapshot(range(70, 120), "t70"));
    await up();
    await up();
    await up();
    expect(spy).toHaveBeenCalledTimes(1);
    expect(result.current.status).toBe("loading");
    await act(async () => release());
    await waitFor(() => expect(above(result.current)).toHaveLength(50));
  });

  it("keeps what it loaded when a refresh slides the window, reading back what it let go", async () => {
    const spy = vi.spyOn(api, "structuredHistory").mockImplementation(server(130));
    const { result, up, rerender } = setup(snapshot(range(70, 120), "t70"));
    await up();
    await waitFor(() => expect(above(result.current)).toHaveLength(50));
    // Three new turns: the window now starts at t73, and t70…t72 left it.
    rerender({ s: snapshot(range(73, 123), "t73") });
    await waitFor(() => expect(ids(above(result.current))).toEqual(ids(range(20, 73))));
    expect(spy).toHaveBeenLastCalledWith("codex-api:s", "t73", 50);
    // Every turn once, in order, from the first held to the window's newest.
    const shown = [...above(result.current), ...range(73, 123)];
    expect(ids(shown)).toEqual(ids(range(20, 123)));
  });

  it("a slide wider than one batch keeps what was loaded and joins it over later snapshots", async () => {
    // Hermes on #1516: held 0–49, then the window slides to 650–699 (12 pages away). Nothing
    // loaded is dropped; the gap is read in bounded batches that resume, then joined.
    vi.spyOn(api, "structuredHistory").mockImplementation(server(700));
    const { result, up, rerender } = setup(snapshot(range(50, 100), "t50"));
    await up();
    await waitFor(() => expect(above(result.current)).toHaveLength(50));
    rerender({ s: snapshot(range(650, 700), "t650") });
    // First batch: still catching up, and the loaded turns are all still there.
    await waitFor(() => expect(result.current.catchingUp).toBe(true));
    expect(ids(result.current.turns.slice(0, 50))).toEqual(ids(range(0, 50)));
    // The next snapshot resumes the read and joins the two: every turn, once, in order.
    rerender({ s: snapshot(range(650, 700), "t650") });
    await waitFor(() => expect(ids(result.current.turns)).toEqual(ids(range(0, 700))));
    expect(result.current.catchingUp).toBe(false);
  });

  it("a read-back cursor removed between batches is recovered from the next surviving turn", async () => {
    // Hermes on #1516: the saved cursor (t150) is removed from the journal (#1447) before the
    // second batch. The read-back resumes from the next turn it holds and still joins.
    const gone = new Set<string>();
    const all = range(0, 700);
    const live = (_key: string, before: string, limit = 50): Promise<StructuredHistoryPage> => {
      const kept = all.filter((t) => !gone.has(t.turn_id));
      const at = kept.findIndex((t) => t.turn_id === before);
      if (at < 0) return Promise.reject(new ApiError(409, "history cursor not found"));
      const start = Math.max(0, at - limit);
      const turns = kept.slice(start, at);
      return Promise.resolve({
        session_key: "codex-api:s",
        turns,
        before: start > 0 ? turns[0].turn_id : null,
        has_more: start > 0,
      });
    };
    const spy = vi.spyOn(api, "structuredHistory").mockImplementation(live);
    const { result, up, rerender } = setup(snapshot(range(50, 100), "t50"));
    await up();
    await waitFor(() => expect(above(result.current)).toHaveLength(50));
    rerender({ s: snapshot(range(650, 700), "t650") });
    await waitFor(() => expect(result.current.catchingUp).toBe(true));
    gone.add("t150");
    const before = spy.mock.calls.length;
    rerender({ s: snapshot(range(650, 700), "t650") });
    // Joined: every turn once, in order. t150 had already been read back in the first batch, and
    // turns already loaded are not pruned afterwards, as with held pages.
    await waitFor(() => expect(ids(result.current.turns)).toEqual(ids(all)));
    expect(result.current.catchingUp).toBe(false);
    // It asked for t150 once, got 409, and moved on: never the same 409 forever.
    const asked = spy.mock.calls.slice(before).map((c) => c[1]);
    expect(asked.filter((b) => b === "t150")).toHaveLength(1);
  });

  it("a window that moves again mid-catch-up is read down to the chain, then joined", async () => {
    vi.spyOn(api, "structuredHistory").mockImplementation(server(710));
    const { result, up, rerender } = setup(snapshot(range(50, 100), "t50"));
    await up();
    await waitFor(() => expect(above(result.current)).toHaveLength(50));
    rerender({ s: snapshot(range(650, 700), "t650") });
    await waitFor(() => expect(result.current.catchingUp).toBe(true));
    // Five more turns arrive before the next snapshot.
    rerender({ s: snapshot(range(655, 705), "t655") });
    await waitFor(() => expect(ids(result.current.turns)).toEqual(ids(range(0, 705))));
    expect(result.current.catchingUp).toBe(false);
  });

  it("a refresh that leaves the boundary where it was reads nothing back", async () => {
    const spy = vi.spyOn(api, "structuredHistory").mockImplementation(server(120));
    const { result, up, rerender } = setup(snapshot(range(70, 120), "t70"));
    await up();
    await waitFor(() => expect(above(result.current)).toHaveLength(50));
    rerender({ s: snapshot(range(70, 120), "t70") });
    await act(async () => {});
    expect(spy).toHaveBeenCalledTimes(1);
  });

  it("on a lost cursor keeps every loaded turn and retries from the next one it holds", async () => {
    const real = server(200);
    const { result, up, reload } = setup(snapshot(range(150, 200), "t150"));
    vi.spyOn(api, "structuredHistory").mockImplementation(real);
    await up();
    await waitFor(() => expect(above(result.current)).toHaveLength(50));
    // t100 (the oldest held, the next cursor) is gone from the server's history.
    const spy = vi
      .spyOn(api, "structuredHistory")
      .mockImplementation((k, before, limit) =>
        before === "t100" ? Promise.reject(new ApiError(409, "gone")) : real(k, before, limit),
      );
    spy.mockClear();
    await up();
    await waitFor(() => expect(result.current.status).toBe("idle"));
    expect(reload).toHaveBeenCalled();
    expect(spy.mock.calls.map((c) => c[1])).toEqual(["t100", "t101"]);
    // Nothing held was dropped, and nothing is shown twice.
    const older = ids(above(result.current));
    expect(older).toEqual([...new Set(older)]);
    expect(older).toEqual(expect.arrayContaining(ids(range(100, 150))));
    expect(older[0]).toBe("t51");
  });

  it("says it failed and loads on Retry", async () => {
    const real = server(120);
    const spy = vi
      .spyOn(api, "structuredHistory")
      .mockRejectedValueOnce(new ApiError(503, "unavailable"))
      .mockImplementation(real);
    const { result, up } = setup(snapshot(range(70, 120), "t70"));
    await up();
    await waitFor(() => expect(result.current.status).toBe("error"));
    expect(above(result.current)).toEqual([]);
    await act(async () => result.current.retry());
    await waitFor(() => expect(above(result.current)).toHaveLength(50));
    expect(spy).toHaveBeenCalledTimes(2);
  });

  it("after a failure only Retry reads again: a scroll never repeats it", async () => {
    const spy = vi
      .spyOn(api, "structuredHistory")
      .mockRejectedValueOnce(new ApiError(503, "unavailable"))
      .mockImplementation(server(120));
    const { result, up } = setup(snapshot(range(70, 120), "t70"));
    await up();
    await waitFor(() => expect(result.current.status).toBe("error"));
    await up();
    await up();
    expect(spy).toHaveBeenCalledTimes(1);
    expect(result.current.status).toBe("error");
  });

  /** A log that scrolls (jsdom's does not), with the hook's handlers driving it as a browser
   *  would: `to(n)` moves it and fires the scroll event. */
  function scrolling(n: number, before: string) {
    let top = 0;
    const log = document.createElement("div");
    Object.defineProperty(log, "scrollTop", { get: () => top, set: (v: number) => (top = v) });
    Object.defineProperty(log, "clientHeight", { get: () => 100 });
    const reload = vi.fn(async () => null);
    const hook = renderHook(() =>
      useApiHistory("codex-api:s", snapshot(range(n - 50, n), before), { current: log }, reload),
    );
    const to = (v: number) => {
      top = v;
      act(() => hook.result.current.handlers.onScroll());
    };
    return { hook, to };
  }

  it("a scroll with no operator input behind it loads nothing", async () => {
    const spy = vi.spyOn(api, "structuredHistory").mockImplementation(server(120));
    const { hook, to } = scrolling(120, "t70");
    to(900);
    to(0); // up, to the top, but no one did it
    await act(async () => {});
    expect(spy).not.toHaveBeenCalled();
    // The same scroll right after a key press is the operator's.
    act(() => hook.result.current.handlers.onKeyDown({} as never));
    to(900);
    to(0);
    await waitFor(() => expect(spy).toHaveBeenCalledTimes(1));
  });

  it("a scroll still running after a page lands asks for nothing more", async () => {
    const spy = vi.spyOn(api, "structuredHistory").mockImplementation(server(200));
    const { hook, to } = scrolling(200, "t150");
    to(900);
    act(() => hook.result.current.handlers.onWheel({ deltaY: -10 } as never));
    to(50); // the wheel's own scroll reaches the top: one page
    await waitFor(() => expect(above(hook.result.current)).toHaveLength(50));
    // The animation goes on moving up with no new input: not a second gesture.
    to(40);
    to(0);
    await act(async () => {});
    expect(spy).toHaveBeenCalledTimes(1);
  });

  it("stops at the pane's cap and never claims the beginning", async () => {
    const spy = vi.spyOn(api, "structuredHistory").mockImplementation(server(1_300));
    const { result, up } = setup(snapshot(range(1_250, 1_300), "t1250"));
    for (let i = 0; i < 25 && result.current.status !== "capped"; i += 1) {
      await up();
      await waitFor(() => expect(result.current.status).not.toBe("loading"));
    }
    expect(result.current.status).toBe("capped");
    expect(above(result.current)).toHaveLength(1_000);
    const calls = spy.mock.calls.length;
    await up();
    expect(spy).toHaveBeenCalledTimes(calls);
  });
});
