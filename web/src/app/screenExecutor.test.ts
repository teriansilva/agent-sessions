import { afterEach, describe, expect, it, vi } from "vitest";
import { matchRoute } from "../components/ask/askMatches";
import { AUTOMATIONS_PATH, DASHBOARD_PATH, MAP_PATH } from "../lib/routes";
import { screenLines, showOf } from "./screenDirective";
import { MAP_ANSWER_MS } from "./screenExecutor";
import { harness, id, key, stored } from "../test/screenHarness";

const win = (n: number, k: number | string = n) => ({
  id: id(n),
  op: "open",
  target: { type: "session", id: key(k) },
  where: "map",
});
const typed = { source: "typed" as const, turnId: "t1" };
const SHOWN = { state: "shown", where: "map" };

afterEach(() => vi.useRealTimers());

describe("apply once", () => {
  it("handles each directive id once, whatever became of it", async () => {
    const { ex, io } = harness();
    const go = { id: id(1), op: "go", section: "automations" };
    expect(ex.execute(go, typed)).toBe(true);
    expect(ex.execute(go, typed)).toBe(false); // a duplicate live event
    await ex.idle();
    expect(ex.execute(go, typed)).toBe(false); // a replay after a reconnect
    await ex.idle();
    expect(io.navigations).toEqual([AUTOMATIONS_PATH]);
  });

  it("does nothing for a duplicate after a cancelled navigation", async () => {
    const { ex, io, outcome } = harness({ path: "/hq" });
    io.held = true;
    const go = { id: id(1), op: "go", section: "automations" };
    ex.execute(go, typed);
    await ex.idle();
    expect(outcome(1)).toEqual({ state: "held" });
    io.held = false;
    expect(ex.execute(go, typed)).toBe(false);
    await ex.idle();
    expect(io.navigations).toEqual([AUTOMATIONS_PATH]);
    expect(io.path).toBe("/hq");
    expect(outcome(1)).toEqual({ state: "held" });
  });

  it("remembers what it handled across a reload of the tab", async () => {
    const first = harness();
    first.ex.execute({ id: id(1), op: "go", section: "map" }, typed);
    await first.ex.idle();
    const second = harness({ sameTab: true }); // the page reloaded: a new executor, the same tab
    expect(second.ex.execute({ id: id(1), op: "go", section: "map" }, typed)).toBe(false);
    expect(second.io.navigations).toEqual([]);
    expect(second.ex.snapshot().size).toBe(0); // no outcome: the record reads "Asked to show"
  });

  it("ignores a member it does not know", async () => {
    const { ex, io } = harness();
    expect(ex.execute({ id: id(1), op: "teleport", to: "/x" }, typed)).toBe(false);
    expect(ex.execute({ id: id(2), op: "go", section: "/settings" }, typed)).toBe(false);
    await ex.idle();
    expect(io.navigations).toEqual([]);
  });
});

describe("navigation", () => {
  it("opens a session full screen and reports it shown", async () => {
    const { ex, io, outcome } = harness();
    ex.execute(
      { id: id(1), op: "open", target: { type: "session", id: key(1) }, where: "full" },
      { ...typed, title: "api-rebase" },
    );
    expect(outcome(1)).toEqual({ state: "pending" });
    await ex.idle();
    expect(io.path).toBe(matchRoute(key(1)));
    expect(outcome(1)).toEqual({ state: "shown", where: "full" });
    expect(ex.snapshot().get(id(1))?.title).toBe("api-rebase");
    expect(io.shown).toEqual([null]);
  });

  it("keeps the directives queued behind a navigation, in order", async () => {
    const { ex, io, outcome } = harness();
    ex.execute({ id: id(1), op: "go", section: "automations" }, typed);
    ex.execute({ id: id(2), op: "go", section: "dashboard" }, typed);
    await ex.idle();
    expect(io.navigations).toEqual([AUTOMATIONS_PATH, DASHBOARD_PATH]);
    expect(io.path).toBe(DASHBOARD_PATH);
    expect([outcome(1), outcome(2)]).toEqual([
      { state: "shown", where: "full" },
      { state: "shown", where: "full" },
    ]);
  });
});

describe("map windows: admitted against the room that is left", () => {
  it("queues a window, goes to the map, and is Shown only once the map opened it", async () => {
    const h = harness();
    h.ex.execute(win(1), typed);
    await h.ex.idle();
    expect(h.io.path).toBe(MAP_PATH);
    expect(h.state().pending.map((p) => p.seed.key)).toEqual([key(1)]);
    expect(h.outcome(1)).toEqual({ state: "pending" }); // queued is not shown
    h.canvas();
    expect(h.outcome(1)).toEqual(SHOWN);
    expect(h.io.shown).toEqual([key(1)]);
  });

  it("opens four windows asked for in one turn", async () => {
    const h = harness();
    for (const n of [1, 2, 3, 4]) h.ex.execute(win(n), typed);
    await h.ex.idle();
    expect(h.io.navigations).toEqual([MAP_PATH]); // one trip to the map, not four
    h.canvas();
    expect(h.windows().map(([k]) => k)).toEqual([1, 2, 3, 4].map(key));
    expect([1, 2, 3, 4].map(h.outcome)).toEqual(Array(4).fill(SHOWN));
  });

  it("falls back to full screen at a full cap, decided up front", async () => {
    const h = harness({ cap: 1, layout: [stored("other")], path: MAP_PATH });
    h.canvas();
    h.ex.execute(win(1), typed);
    await h.ex.idle();
    expect(h.state().queue).toEqual([]);
    expect(h.io.path).toBe(matchRoute(key(1)));
    expect(h.outcome(1)).toEqual({ state: "fallback", reason: "cap" });
  });

  it("counts a saved layout the map has not restored yet against the room", async () => {
    const h = harness({ cap: 1, layout: [stored("other")] }); // loaded on the dashboard
    h.ex.execute(win(1), typed);
    await h.ex.idle();
    expect(h.outcome(1)).toEqual({ state: "fallback", reason: "cap" });
  });

  it("falls back to full screen where the map can't host a window (a phone)", async () => {
    const h = harness({ capable: false });
    h.ex.execute(win(1), typed);
    await h.ex.idle();
    expect(h.io.path).toBe(matchRoute(key(1)));
    expect(h.outcome(1)).toEqual({ state: "fallback", reason: "host" });
  });

  it("with room for two of four opens two and lists the rest, never four navigations", async () => {
    const h = harness({ cap: 2 });
    for (const n of [1, 2, 3, 4]) h.ex.execute(win(n), typed);
    await h.ex.idle();
    expect(h.io.navigations).toEqual([MAP_PATH]);
    h.canvas();
    expect([1, 2, 3, 4].map(h.outcome)).toEqual([
      SHOWN,
      SHOWN,
      { state: "left", reason: "cap" },
      { state: "left", reason: "cap" },
    ]);
  });

  it("re-opening a session that has a window costs no room", async () => {
    const h = harness({ cap: 1, layout: [stored(1)], path: MAP_PATH });
    h.canvas();
    h.ex.execute(win(1), typed);
    await h.ex.idle();
    h.canvas();
    expect(h.outcome(1)).toEqual(SHOWN);
  });

  it("takes the drain's refusal over its own earlier room: one refused goes full screen", async () => {
    const h = harness({ cap: 2, path: MAP_PATH });
    h.canvas();
    h.ex.execute(win(1), typed);
    await h.ex.idle();
    // The Commander filled the map by hand before the queue drained.
    for (const n of ["a", "b"])
      h.dispatch({
        type: "open",
        seed: { key: key(n), engine: "claude", id: `s${n}`, title: "" },
        anchor: null,
        bounds: { w: 1600, h: 1000 },
      });
    h.canvas();
    expect(h.outcome(1)).toEqual({ state: "fallback", reason: "cap" });
    expect(h.io.path).toBe(matchRoute(key(1))); // the map handed it back
  });

  it("…and several refused stay listed on the map", async () => {
    const h = harness({ cap: 2, path: MAP_PATH });
    h.canvas();
    h.ex.execute(win(1), typed);
    h.ex.execute(win(2), typed);
    await h.ex.idle();
    for (const n of ["a", "b"])
      h.dispatch({
        type: "open",
        seed: { key: key(n), engine: "claude", id: `s${n}`, title: "" },
        anchor: null,
        bounds: { w: 1600, h: 1000 },
      });
    h.canvas();
    expect([h.outcome(1), h.outcome(2)]).toEqual([
      { state: "left", reason: "cap" },
      { state: "left", reason: "cap" },
    ]);
    expect(h.io.path).toBe(MAP_PATH);
  });

  it("admits each turn on its own: a later turn may fall back again", async () => {
    const h = harness({ cap: 1, layout: [stored("other")], path: MAP_PATH });
    h.canvas();
    h.ex.execute(win(1), { source: "typed", turnId: "t1" });
    h.ex.execute(win(2), { source: "typed", turnId: "t1" });
    h.ex.execute(win(3), { source: "typed", turnId: "t2" });
    await h.ex.idle();
    expect([h.outcome(1), h.outcome(2), h.outcome(3)]).toEqual([
      { state: "fallback", reason: "cap" },
      { state: "left", reason: "cap" },
      { state: "fallback", reason: "cap" },
    ]);
    expect(h.io.navigations).toEqual([matchRoute(key(1)), matchRoute(key(3))]);
  });

  it("a guarded page holds the trip to the map and nothing is queued", async () => {
    const h = harness();
    h.io.held = true;
    h.ex.execute(win(1), typed);
    await h.ex.idle();
    expect(h.state().queue).toEqual([]);
    expect(h.outcome(1)).toEqual({ state: "held" });
  });
});

describe("the map's own commands", () => {
  it("runs minimize, close, arrange and close all through the workspace, each answered", async () => {
    const h = harness({ layout: [stored(1), stored(2), stored(3)], path: MAP_PATH });
    h.canvas();
    h.ex.execute({ id: id(1), op: "map_minimize", key: key(1) }, typed);
    h.ex.execute({ id: id(2), op: "map_close", key: key(2) }, typed);
    h.ex.execute({ id: id(3), op: "map_arrange" }, typed);
    await h.ex.idle();
    expect([1, 2, 3].map(h.outcome)).toEqual(Array(3).fill({ state: "pending" }));
    h.canvas();
    expect(h.windows()).toEqual([
      [key(1), true],
      [key(3), false],
    ]);
    expect([1, 2, 3].map(h.outcome)).toEqual(Array(3).fill({ state: "done" }));
    h.ex.execute({ id: id(4), op: "map_close_all" }, typed);
    await h.ex.idle();
    h.canvas();
    expect(h.windows()).toEqual([]);
    expect(h.outcome(4)).toEqual({ state: "done" });
  });

  it("says so when there is no window to act on", async () => {
    const h = harness({ path: MAP_PATH });
    h.canvas();
    h.ex.execute({ id: id(1), op: "map_close", key: key(1) }, typed);
    h.ex.execute({ id: id(2), op: "map_close_all" }, typed);
    h.ex.execute({ id: id(3), op: "map_arrange" }, typed);
    await h.ex.idle();
    expect(h.io.dispatched.filter((a) => a.type !== "restore")).toEqual([]);
    expect([1, 2, 3].map(h.outcome)).toEqual(Array(3).fill({ state: "none" }));
  });
});

describe("a command the map never answers", () => {
  it("is withdrawn after the wait, says so, and can no longer run", async () => {
    vi.useFakeTimers();
    const h = harness();
    h.ex.execute(win(1), typed);
    await vi.advanceTimersByTimeAsync(0);
    h.commit(); // the map route is up, but its canvas never mounts (its sessions do not load)
    expect(h.outcome(1)).toEqual({ state: "pending" });
    await vi.advanceTimersByTimeAsync(MAP_ANSWER_MS);
    expect(h.outcome(1)).toEqual({ state: "unanswered" });
    const [line] = screenLines([showOf({ ...win(1), title: "Session 1" })!], (i) =>
      h.ex.snapshot().get(i),
    );
    expect(line.lead + line.strong + line.tail).toBe("Not opened: the map did not open in time");
    h.canvas(); // the map finally mounts: the withdrawn request opens nothing
    expect(h.windows()).toEqual([]);
    expect(h.outcome(1)).toEqual({ state: "unanswered" });
  });

  it("an answered command is not withdrawn later", async () => {
    vi.useFakeTimers();
    const h = harness();
    h.ex.execute(win(1), typed);
    await vi.advanceTimersByTimeAsync(0);
    h.canvas();
    await vi.advanceTimersByTimeAsync(MAP_ANSWER_MS * 2);
    h.canvas();
    expect(h.outcome(1)).toEqual(SHOWN);
    expect(h.windows()).toEqual([[key(1), false]]);
  });
});

describe("the line's Open, tapped", () => {
  const sessions = [1, 2].map((n) => ({ key: key(n), title: "" }));
  const tick = async () => {
    for (let i = 0; i < 4; i += 1) await Promise.resolve();
  };

  it("opens the remaining sessions as map windows when there is room", async () => {
    const h = harness({ cap: 4 });
    h.ex.open({ type: "sessions", sessions });
    await tick();
    expect(h.io.navigations).toEqual([MAP_PATH]);
    h.canvas();
    expect(h.windows().map(([k]) => k)).toEqual([key(1), key(2)]);
  });

  it("opens ONE session full screen when there is still no room", async () => {
    const h = harness({ cap: 1, layout: [stored("other")] });
    h.ex.open({ type: "sessions", sessions });
    await tick();
    expect(h.io.navigations).toEqual([matchRoute(key(1))]);
    expect(h.state().queue).toEqual([]);
  });

  it("goes to a route for anything else", () => {
    const h = harness();
    h.ex.open({ type: "route", to: AUTOMATIONS_PATH });
    expect(h.io.navigations).toEqual([AUTOMATIONS_PATH]);
  });
});
