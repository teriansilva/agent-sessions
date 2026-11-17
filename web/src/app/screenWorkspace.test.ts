/** HQ's screen executor against the REAL workspace reducer: the named regressions from the
 *  reviews of PR #2109 (#2102). The general statement — any order, any start — is
 *  `screenSequences.test.ts`; these are the cases a reviewer reproduced, each by name.
 *
 *  `test/screenHarness.ts` makes React's part explicit: the executor reads the last RENDER, which
 *  lags its own dispatches until `commit`; `canvas` is the mounted map's effects. */
import { describe, expect, it } from "vitest";
import { matchRoute } from "../components/ask/askMatches";
import { MAP_PATH } from "../lib/routes";
import { screenLines, showOf } from "./screenDirective";
import { BOUNDS, harness, id, key, stored } from "../test/screenHarness";

const open = (n: number, k: number | string = n) => ({
  id: id(n),
  op: "open",
  target: { type: "session", id: key(k) },
  where: "map",
});
const typed = { source: "typed" as const, turnId: "t1" };
const SHOWN = { state: "shown", where: "map" };
const text = (l: { lead: string; strong: string; tail: string }) => l.lead + l.strong + l.tail;

describe("after a reload away from the map: the stored layout is the workspace", () => {
  it("close removes the window from the layout, and the map does not bring it back", async () => {
    const h = harness({ layout: [stored(1), stored(2)] });
    h.ex.execute({ id: id(1), op: "map_close", key: key(1) }, typed);
    await h.flush();
    expect(h.outcome(1)).toEqual({ state: "done" });
    expect(h.io.path).toBe("/dashboard"); // closing a window is no reason to leave the page
    expect(h.windows()).toEqual([[key(2), false]]);
    h.io.path = MAP_PATH; // the Commander opens the map
    h.canvas();
    expect(h.state().windows.map((w) => w.key)).toEqual([key(2)]);
  });

  it("minimize parks the stored window: it comes back in the tray, not on screen", async () => {
    const h = harness({ layout: [stored(1), stored(2)] });
    h.ex.execute({ id: id(1), op: "map_minimize", key: key(2) }, typed);
    await h.flush();
    expect(h.outcome(1)).toEqual({ state: "done" });
    h.io.path = MAP_PATH;
    h.canvas();
    expect(h.windows()).toEqual([
      [key(1), false],
      [key(2), true],
    ]);
  });

  it("close all empties the layout instead of saying there are no windows", async () => {
    const h = harness({ layout: [stored(1), stored(2)] });
    h.ex.execute({ id: id(1), op: "map_close_all" }, typed);
    await h.flush();
    expect(h.outcome(1)).toEqual({ state: "done" });
    h.io.path = MAP_PATH;
    h.canvas();
    expect(h.windows()).toEqual([]);
  });

  it("arrange goes to the map, tiles the restored windows, and is done only once it ran", async () => {
    const h = harness({ layout: [stored(1), stored(2)] });
    h.ex.execute({ id: id(1), op: "map_arrange" }, typed);
    await h.ex.idle();
    expect(h.io.path).toBe(MAP_PATH);
    h.commit();
    expect(h.outcome(1)).toEqual({ state: "pending" }); // queued for the canvas: not a result
    h.canvas();
    expect(h.outcome(1)).toEqual({ state: "done" });
    const [a, b] = h.state().windows.map((w) => w.rect);
    expect(a.x + a.w <= b.x || b.x + b.w <= a.x || a.y + a.h <= b.y || b.y + b.h <= a.y).toBe(true);
  });

  it("says so when a session has no window anywhere", async () => {
    const h = harness({ layout: [stored(1)] });
    h.ex.execute({ id: id(1), op: "map_close", key: key(5) }, typed);
    h.ex.execute({ id: id(2), op: "map_minimize", key: key(5) }, typed);
    await h.flush();
    expect([h.outcome(1), h.outcome(2)]).toEqual([{ state: "none" }, { state: "none" }]);
    expect(h.windows()).toEqual([[key(1), false]]);
  });
});

describe("a command behind an open, delivered together before React commits", () => {
  it("open → close leaves no window after the drain, and the open is not called shown", async () => {
    const h = harness({ path: MAP_PATH });
    h.canvas();
    h.ex.execute(open(1), typed);
    h.ex.execute({ id: id(2), op: "map_close", key: key(1) }, typed);
    await h.ex.idle(); // both applied; nothing rendered in between
    h.canvas();
    expect(h.windows()).toEqual([]);
    expect(h.outcome(2)).toEqual({ state: "done" });
    expect(h.outcome(1)).toEqual({ state: "undone" });
    const shows = [
      showOf({ ...open(1), title: "Session 1" })!,
      showOf({ id: id(2), op: "map_close", key: key(1), title: "Session 1" })!,
    ];
    const lines = screenLines(shows, (i) => h.ex.snapshot().get(i));
    expect(lines.map(text)).toEqual(["Shown: Session 1 · window closed"]);
  });

  it("open → minimize opens the window parked", async () => {
    const h = harness({ path: MAP_PATH });
    h.canvas();
    h.ex.execute(open(1), typed);
    h.ex.execute({ id: id(2), op: "map_minimize", key: key(1) }, typed);
    await h.ex.idle();
    h.canvas();
    expect(h.windows()).toEqual([[key(1), true]]);
    expect(h.state().focusedKey).toBeNull();
    expect([h.outcome(1), h.outcome(2)]).toEqual([{ state: "undone" }, { state: "done" }]);
  });

  it("open → close all leaves no window, the open's and the ones already there", async () => {
    const h = harness({ layout: [stored(3)], path: MAP_PATH });
    h.canvas();
    h.ex.execute(open(1), typed);
    h.ex.execute(open(2), typed);
    h.ex.execute({ id: id(9), op: "map_close_all" }, typed);
    await h.ex.idle();
    h.canvas();
    expect(h.windows()).toEqual([]);
    expect(h.outcome(9)).toEqual({ state: "done" });
    expect([h.outcome(1), h.outcome(2)]).toEqual([{ state: "undone" }, { state: "undone" }]);
  });

  it("close all with nothing but a queued open still closes it", async () => {
    const h = harness({ path: MAP_PATH });
    h.canvas();
    h.ex.execute(open(1), typed);
    h.ex.execute({ id: id(2), op: "map_close_all" }, typed);
    await h.ex.idle();
    h.canvas();
    expect(h.windows()).toEqual([]);
    expect(h.outcome(2)).toEqual({ state: "done" });
  });

  it("open → arrange tiles the new window with the others", async () => {
    const h = harness({ layout: [stored(3)], path: MAP_PATH });
    h.canvas();
    h.ex.execute(open(1), typed);
    h.ex.execute({ id: id(2), op: "map_arrange" }, typed);
    await h.ex.idle();
    h.canvas();
    expect(h.windows().map(([k]) => k)).toEqual([key(1), key(3)]);
    expect([h.outcome(1), h.outcome(2)]).toEqual([SHOWN, { state: "done" }]);
    const [a, b] = h.state().windows.map((w) => w.rect);
    expect([a.w, a.h]).toEqual([b.w, b.h]); // placed by the SAME tiling
    expect(a.x + a.w <= b.x || b.x + b.w <= a.x || a.y + a.h <= b.y || b.y + b.h <= a.y).toBe(true);
  });

  it("an open the same batch closed can be asked for again", async () => {
    const h = harness({ path: MAP_PATH });
    h.canvas();
    h.ex.execute(open(1), typed);
    h.ex.execute({ id: id(2), op: "map_close", key: key(1) }, typed);
    h.ex.execute(open(3, 1), typed);
    await h.ex.idle();
    h.canvas();
    expect(h.windows()).toEqual([[key(1), false]]);
    expect([1, 2, 3].map(h.outcome)).toEqual([{ state: "undone" }, { state: "undone" }, SHOWN]);
  });
});

describe("review round 2: three more orders, one mechanism", () => {
  it("open → minimize → focus: the later focus brings the queued window back", async () => {
    const h = harness({ path: MAP_PATH });
    h.canvas();
    h.ex.execute(open(1), typed);
    h.ex.execute({ id: id(2), op: "map_minimize", key: key(1) }, typed);
    h.ex.execute({ id: id(3), op: "map_focus", key: key(1) }, typed);
    await h.ex.idle(); // all three before any render
    h.canvas();
    expect(h.windows()).toEqual([[key(1), false]]); // on screen, not in the tray
    expect(h.outcome(3)).toEqual(SHOWN);
    // The minimize ran and was then undone by the focus: neither it nor the open is reported.
    expect([h.outcome(1), h.outcome(2)]).toEqual([{ state: "undone" }, { state: "undone" }]);
  });

  it("minimize → focus on a window that is open does the same", async () => {
    const h = harness({ layout: [stored(1)], path: MAP_PATH });
    h.canvas();
    h.ex.execute({ id: id(1), op: "map_minimize", key: key(1) }, typed);
    h.ex.execute({ id: id(2), op: "map_focus", key: key(1) }, typed);
    await h.ex.idle();
    h.canvas();
    expect(h.windows()).toEqual([[key(1), false]]);
    expect([h.outcome(1), h.outcome(2)]).toEqual([{ state: "undone" }, SHOWN]);
  });

  it("at a full cap, close A → open B: the close frees the room B is admitted against", async () => {
    const h = harness({ cap: 2, layout: [stored("a"), stored("x")], path: MAP_PATH });
    h.canvas();
    h.ex.execute({ id: id(1), op: "map_close", key: key("a") }, typed);
    h.ex.execute(open(2, "b"), typed);
    await h.ex.idle(); // before the next render: the rendered workspace still shows two windows
    expect(h.io.navigations).toEqual([]); // B did NOT fall back to its full-screen route
    h.canvas();
    expect(h.windows()).toEqual([
      [key("b"), false],
      [key("x"), false],
    ]);
    expect([h.outcome(1), h.outcome(2)]).toEqual([{ state: "done" }, SHOWN]);
    expect(h.io.path).toBe(MAP_PATH);
  });

  it("at a full cap, close all → open B likewise", async () => {
    const h = harness({ cap: 2, layout: [stored("a"), stored("x")], path: MAP_PATH });
    h.canvas();
    h.ex.execute({ id: id(1), op: "map_close_all" }, typed);
    h.ex.execute(open(2, "b"), typed);
    await h.ex.idle();
    expect(h.io.navigations).toEqual([]);
    h.canvas();
    expect(h.windows()).toEqual([[key("b"), false]]);
    expect([h.outcome(1), h.outcome(2)]).toEqual([{ state: "done" }, SHOWN]);
  });

  it("…and without a close the same open still falls back: the room is real, not assumed", async () => {
    const h = harness({ cap: 2, layout: [stored("a"), stored("x")], path: MAP_PATH });
    h.canvas();
    h.ex.execute(open(1, "b"), typed);
    await h.flush();
    expect(h.outcome(1)).toEqual({ state: "fallback", reason: "cap" });
    expect(h.io.path).toBe(matchRoute(key("b")));
  });

  it("arrange with every window minimised settles at once, and says why", async () => {
    const h = harness({ layout: [stored(1, true), stored(2, true)] }); // saved, map not mounted
    h.ex.execute({ id: id(1), op: "map_arrange" }, typed);
    await h.flush();
    expect(h.outcome(1)).toEqual({ state: "none", reason: "parked" });
    expect(h.io.navigations).toEqual([]); // nothing to arrange is no reason to leave the page
    const [line] = screenLines([showOf({ id: id(1), op: "map_arrange" })!], (i) =>
      h.ex.snapshot().get(i),
    );
    expect(text(line)).toBe("Not opened: every window in the map is minimised");
    expect(line.kind).toBe("held");
  });

  it("an arrange the map could not run is answered by ITS id; a later arrange settles only itself", async () => {
    const h = harness({ layout: [stored(1)], path: MAP_PATH });
    h.canvas();
    // The Commander parks the only window by hand; no render has shown the executor that yet.
    h.dispatch({ type: "minimize", key: key(1) });
    h.ex.execute({ id: id(1), op: "map_arrange" }, typed);
    await h.ex.idle();
    h.canvas();
    expect(h.outcome(1)).toEqual({ state: "none", reason: "parked" }); // answered, not pending
    // A later arrange that does run…
    h.ex.execute({ id: id(2), op: "map_focus", key: key(1) }, typed);
    h.ex.execute({ id: id(3), op: "map_arrange" }, typed);
    await h.flush();
    expect(h.outcome(3)).toEqual({ state: "done" });
    // …leaves the earlier one exactly as it was answered.
    expect(h.outcome(1)).toEqual({ state: "none", reason: "parked" });
    expect(h.state().acks.filter((a) => a.id === id(1))).toEqual([
      { id: id(1), result: "noop", reason: "parked" },
    ]);
  });

  it("an arrange on a map that turns out unable to host one is refused, not left pending", async () => {
    const h = harness({ layout: [stored(1)], path: MAP_PATH });
    h.canvas();
    h.ex.execute({ id: id(1), op: "map_arrange" }, typed);
    await h.ex.idle();
    h.dispatch({ type: "drain", anchors: new Map(), bounds: BOUNDS, canHost: false });
    h.commit();
    expect(h.outcome(1)).toEqual({ state: "none", reason: "host" });
  });

  it("a window the Commander closes by hand mid-batch wins over the executor's own picture", async () => {
    const h = harness({ cap: 2, layout: [stored("a"), stored("x")], path: MAP_PATH });
    h.canvas();
    h.dispatch({ type: "close", key: key("x") }); // by hand, not yet rendered…
    h.commit(); // …and now it is: the executor's next decision starts from it
    h.ex.execute(open(1, "b"), typed);
    await h.flush();
    expect(h.outcome(1)).toEqual(SHOWN);
    expect(h.windows().map(([k]) => k)).toEqual([key("a"), key("b")]);
  });
});
