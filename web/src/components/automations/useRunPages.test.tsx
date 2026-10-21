/** Run-history paging (#1252 review): the raw server cursor, head insertion, and the in-flight
 *  guard — each tested on its own layer, below the button. */
import { act, renderHook } from "@testing-library/react";
import { afterEach, expect, test, vi } from "vitest";

import { api } from "../../lib/api";
import type { AutomationRun } from "../../types/automations";
import { useRunPages } from "./useRunPages";

function run(n: number): AutomationRun {
  return {
    id: `r${String(n).padStart(4, "0")}`,
    automation_id: "a1",
    trigger: "schedule",
    slot: "",
    fire_at: null,
    catch_up: false,
    covered: 1,
    state: "done",
    outcome: "ok",
    result_class: "ok",
    reason: "",
    mission_id: null,
    session_key: null,
    created_at: 1_000_000 + n,
    finished_at: null,
    inputs: {},
    scope: null,
  };
}

/** A server list, newest first, that a test can grow at the head between pages. */
function server(n: number) {
  const list = Array.from({ length: n }, (_, i) => run(n - i));
  const calls: number[] = [];
  const spy = vi.spyOn(api, "automationRuns").mockImplementation(async (_id, limit = 50, offset = 0) => {
    calls.push(offset);
    return { runs: list.slice(offset, offset + limit), total: list.length };
  });
  return { list, calls, spy, insertNewest: () => list.unshift(run(1000 + list.length)) };
}

afterEach(() => vi.restoreAllMocks());

test("a run inserted between pages: all 101 are reachable, once each, and paging ends", async () => {
  const s = server(100);
  const { result } = renderHook(() => useRunPages("a1", 50));
  await act(() => result.current.loadFirst());
  expect(result.current.runs).toHaveLength(50);
  s.insertNewest();
  await act(() => result.current.loadMore());
  await act(() => result.current.loadMore());
  const ids = result.current.runs!.map((r) => r.id);
  expect(new Set(ids).size).toBe(ids.length);
  expect(ids.sort()).toEqual(s.list.map((r) => r.id).sort());
  expect(result.current.total).toBe(101);
  expect(result.current.runs!.length < result.current.total).toBe(false); // the button is gone
});

test("the cursor is the server's position: a page the display dedupes still advances it", async () => {
  const s = server(120);
  const { result } = renderHook(() => useRunPages("a1", 50));
  await act(() => result.current.loadFirst());
  // A Run now shown at once: the display grows by one, the server's positions shift by one.
  s.insertNewest();
  act(() => result.current.prepend(s.list[0]));
  await act(() => result.current.loadMore());
  await act(() => result.current.loadMore());
  expect(new Set(result.current.runs!.map((r) => r.id)).size).toBe(121);
  expect(s.calls.filter((o) => o > 0)).toEqual([50, 100]);
});

test("two loads started in the same tick fetch ONE page", async () => {
  const s = server(120);
  const { result } = renderHook(() => useRunPages("a1", 50));
  await act(() => result.current.loadFirst());
  await act(async () => {
    await Promise.all([result.current.loadMore(), result.current.loadMore()]);
  });
  expect(s.calls.filter((o) => o > 0)).toEqual([50]);
  expect(result.current.runs).toHaveLength(100);
});

test("more runs than a page arrive at the head: every one is still reachable, once", async () => {
  const s = server(20);
  const { result } = renderHook(() => useRunPages("a1", 5));
  await act(() => result.current.loadFirst());
  for (let i = 0; i < 7; i++) s.insertNewest();
  for (let i = 0; i < 10 && result.current.runs!.length < result.current.total; i++)
    await act(() => result.current.loadMore());
  const ids = result.current.runs!.map((r) => r.id);
  expect(new Set(ids).size).toBe(27);
  expect(ids.sort()).toEqual(s.list.map((r) => r.id).sort());
});

test("a Retry while an older page is on the wire discards that page", async () => {
  const s = server(120);
  const { result } = renderHook(() => useRunPages("a1", 50));
  await act(() => result.current.loadFirst());
  // Hold the next older page.
  let release: () => void = () => {};
  const held = new Promise<void>((r) => (release = r));
  s.spy.mockImplementationOnce(async (_id, limit = 50, offset = 0) => {
    await held;
    return { runs: s.list.slice(offset, offset + limit), total: s.list.length };
  });
  let more: Promise<void> = Promise.resolve();
  act(() => {
    more = result.current.loadMore();
  });
  await act(() => result.current.loadFirst()); // Retry, while the page is held
  release();
  await act(() => more);
  // The late page added nothing and did not move the cursor: the next page starts after page 1.
  expect(result.current.runs).toHaveLength(50);
  s.calls.length = 0;
  await act(() => result.current.loadMore());
  expect(s.calls.filter((o) => o > 0)).toEqual([50]); // (plus the head read every load makes)
  expect(result.current.runs).toHaveLength(100);
});

test("more new runs than a page, after the cursor has passed them: still every one, once", async () => {
  const s = server(40);
  const { result } = renderHook(() => useRunPages("a1", 5));
  await act(() => result.current.loadFirst());
  await act(() => result.current.loadMore());
  await act(() => result.current.loadMore()); // the cursor is at 15
  for (let i = 0; i < 7; i++) s.insertNewest(); // 7 > one page, all ahead of the cursor
  for (let i = 0; i < 20 && result.current.runs!.length < result.current.total; i++)
    await act(() => result.current.loadMore());
  const ids = result.current.runs!.map((r) => r.id);
  expect(new Set(ids).size).toBe(47);
  expect(ids.sort()).toEqual(s.list.map((r) => r.id).sort());
});


/** A server that can also PRUNE its oldest runs, as retention does. */
function prunable(n: number) {
  const s = server(n);
  return { ...s, pruneOldest: (k: number) => s.list.splice(s.list.length - k, k) };
}

test("inserts and prunes in the same gap: every run on the server is reached", async () => {
  const s = prunable(150);
  const { result } = renderHook(() => useRunPages("a1", 50));
  await act(() => result.current.loadFirst());
  await act(() => result.current.loadMore()); // 100 shown
  for (let i = 0; i < 10; i++) s.insertNewest();
  s.pruneOldest(10); // total is 150 again: its change says nothing
  for (let i = 0; i < 6 && result.current.runs!.length < result.current.total; i++)
    await act(() => result.current.loadMore());
  const shown = new Set(result.current.runs!.map((r) => r.id));
  for (const r of s.list) expect(shown.has(r.id)).toBe(true);
  expect(result.current.total).toBe(150);
});

test("a read that fails mid-load commits nothing, and a Retry of it loses no run", async () => {
  const s = server(120);
  const { result } = renderHook(() => useRunPages("a1", 50));
  await act(() => result.current.loadFirst());
  s.insertNewest();
  // The head read succeeds, the older page fails.
  const real = s.spy.getMockImplementation()!;
  s.spy.mockImplementationOnce(real).mockImplementationOnce(async () => {
    throw new Error("down");
  });
  await act(() => result.current.loadMore());
  expect(result.current.error).toBeTruthy();
  expect(result.current.runs).toHaveLength(50); // nothing half-applied
  // …and the cursor did not move: the next load's older page starts at 50 + the 1 arrival.
  s.calls.length = 0;
  await act(() => result.current.loadMore());
  expect(s.calls.filter((o) => o > 0)[0]).toBe(51);
  for (let i = 0; i < 4 && result.current.runs!.length < result.current.total; i++)
    await act(() => result.current.loadMore());
  const ids = result.current.runs!.map((r) => r.id);
  expect(new Set(ids).size).toBe(121);
  expect(s.calls.filter((o) => o > 0)).not.toContain(52); // never skipped past a row
  expect(ids.sort()).toEqual(s.list.map((r) => r.id).sort());
});

test("of two Retries, the newer one's list stands even if the older answers last", async () => {
  const s = server(120);
  const { result } = renderHook(() => useRunPages("a1", 50));
  let releaseOld: () => void = () => {};
  const oldHeld = new Promise<void>((r) => (releaseOld = r));
  const real = s.spy.getMockImplementation()!;
  s.spy.mockImplementationOnce(async () => {
    await oldHeld;
    return { runs: [], total: 0 }; // the older read would say "no runs"
  });
  let first: Promise<void> = Promise.resolve();
  act(() => {
    first = result.current.loadFirst();
  });
  s.spy.mockImplementation(real);
  await act(() => result.current.loadFirst()); // the newer Retry lands first
  releaseOld();
  await act(() => first);
  expect(result.current.runs).toHaveLength(50);
  expect(result.current.total).toBe(120);
});

test("runs that land BEHIND the cursor are reached: a short page walks again from the top", async () => {
  const s = server(150);
  const { result } = renderHook(() => useRunPages("a1", 50));
  await act(() => result.current.loadFirst());
  await act(() => result.current.loadMore()); // 100 shown, the cursor at 100
  // Five runs whose time sorts among the ones already shown (a peer instance's clock): they sit
  // at positions the cursor has passed, and no head read ever meets them.
  const mid = s.list[70].created_at; // beyond the head page every load re-reads
  for (let i = 0; i < 5; i++) {
    const r = { ...s.list[0], id: `mid${i}`, created_at: mid - 0.1 * (i + 1) };
    s.list.splice(71 + i, 0, r);
  }
  for (let i = 0; i < 6 && result.current.runs!.length < result.current.total; i++)
    await act(() => result.current.loadMore());
  const shown = new Set(result.current.runs!.map((r) => r.id));
  for (const r of s.list) expect(shown.has(r.id)).toBe(true);
});

test("more arrivals than a head page: ONE load walks the head until it meets a held run", async () => {
  // A single head page would count only `page` arrivals: the older read would start inside the
  // arrivals, come back FULL (so no walk from the top is triggered), and the list would show a
  // hole — arrivals the head page did not reach, and the older page it should have read.
  const s = server(10);
  const { result } = renderHook(() => useRunPages("a1", 2));
  await act(() => result.current.loadFirst()); // r0010, r0009
  for (let i = 0; i < 5; i++) s.insertNewest(); // 5 arrivals: more than two head pages
  s.calls.length = 0;
  await act(() => result.current.loadMore());
  // Contiguous: the five arrivals, the two held runs, and the next two older ones — no hole.
  expect(result.current.runs!.map((r) => r.id)).toEqual(s.list.slice(0, 9).map((r) => r.id));
  expect(s.calls).toEqual([0, 2, 4, 7]); // three head pages, then the older page at 2 + 5
});
