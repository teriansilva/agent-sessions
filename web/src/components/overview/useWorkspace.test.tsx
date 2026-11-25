import { act, renderHook } from "@testing-library/react";
import { StrictMode } from "react";
import { beforeEach, describe, expect, it } from "vitest";
import { useWorkspace, type WindowSeed } from "./useWorkspace";
import { CAP_KEY, WORKSPACE_KEY } from "./windowStore";
import {
  clampRect,
  DEFAULT_SIZE,
  WINDOW_CAP,
  WINDOW_CAP_MAX,
  WINDOW_CAP_MIN,
} from "./workspace";

// The workspace transition (#208). Rendered under StrictMode ON PURPOSE: the app mounts in
// StrictMode, which double-invokes reducers and effects, and the first cut of `open()` decided
// its outcome by mutating a holder object inside a functional state updater and reading it
// afterwards. That is not something React promises — the read could see a decision not yet made,
// or made twice — so a capped open could skip its notice and focus a key with no window behind
// it. These cases are the ones that shape could get wrong.

const BOUNDS = { w: 1400, h: 900 };

const session = (n: number): WindowSeed => ({
  key: `claude:s${n}`,
  engine: "claude",
  id: `s${n}`,
  title: `Session ${n}`,
});

const setup = () =>
  renderHook(() => useWorkspace(), { wrapper: StrictMode });

// Each case gets a clean device: the workspace now READS storage at construction, so a layout
// left behind by the previous test would leak into the next one's initial state.
beforeEach(() => localStorage.clear());

describe("useWorkspace", () => {
  it("opens one window per session", () => {
    const { result } = setup();
    act(() => result.current.open(session(1), { x: 10, y: 10 }, BOUNDS));
    expect(result.current.windows).toHaveLength(1);
    expect(result.current.focusedKey).toBe("claude:s1");
    expect(result.current.notice).toBeNull();
  });

  it("refuses a duplicate IN THE SAME TURN, and flashes the window that already exists", () => {
    const { result } = setup();
    // Both calls land before React re-renders — the second one cannot see the first through
    // state, only through the reducer. A double-click on a chip is exactly this.
    act(() => {
      result.current.open(session(1), { x: 10, y: 10 }, BOUNDS);
      result.current.open(session(1), { x: 10, y: 10 }, BOUNDS);
    });
    expect(result.current.windows).toHaveLength(1);
    expect(result.current.flashKey).toBe("claude:s1");
    expect(result.current.focusedKey).toBe("claude:s1");
  });

  it("caps at exactly WINDOW_CAP and says so, even when the overflow arrives in one turn", () => {
    const { result } = setup();
    act(() => {
      for (let n = 1; n <= WINDOW_CAP + 1; n++)
        result.current.open(session(n), { x: 0, y: 0 }, BOUNDS);
    });
    expect(result.current.windows).toHaveLength(WINDOW_CAP);
    expect(result.current.notice).toMatch(/limit/i);
    // The refused open must not leave the workspace focused on a window that was never created.
    expect(result.current.windows.some((w) => w.key === result.current.focusedKey)).toBe(true);
    expect(result.current.focusedKey).toBe(`claude:s${WINDOW_CAP}`);
  });

  it("raising an already-focused window changes nothing (the mounted pane keeps its props)", () => {
    const { result } = setup();
    act(() => result.current.open(session(1), null, BOUNDS));
    const before = result.current.windows;
    act(() => result.current.focus("claude:s1"));
    expect(result.current.windows).toBe(before);
  });

  it("closing clears the focus and the flash it owned", () => {
    const { result } = setup();
    act(() => {
      result.current.open(session(1), null, BOUNDS);
      result.current.open(session(1), null, BOUNDS); // flashes s1
    });
    expect(result.current.flashKey).toBe("claude:s1");
    act(() => result.current.close("claude:s1"));
    expect(result.current.windows).toHaveLength(0);
    expect(result.current.focusedKey).toBeNull();
    expect(result.current.flashKey).toBeNull();
  });

  it("adopts a rename, and keeps the last name when the session leaves the map", () => {
    const { result } = setup();
    act(() => result.current.open(session(1), null, BOUNDS));
    act(() => result.current.syncTitles(new Map([["claude:s1", "Renamed"]])));
    expect(result.current.windows[0].title).toBe("Renamed");
    act(() => result.current.syncTitles(new Map()));
    expect(result.current.windows[0].title).toBe("Renamed");
  });

  it("keeps the operator's rect as INTENT — a shrinking box never rewrites it", () => {
    const { result } = setup();
    act(() => result.current.open(session(1), { x: 1200, y: 700 }, BOUNDS));
    const intent = result.current.windows[0].rect;
    expect(intent.w).toBe(DEFAULT_SIZE.w);
    // Nothing in the workspace reacts to the box changing: the layer fits the intent to the
    // box at render time, so a map that shrinks and grows returns the window to this layout
    // rather than to whatever the smallest box along the way allowed.
    expect(clampRect(intent, { w: 800, h: 600 }).w).toBeLessThanOrEqual(800);
    expect(result.current.windows[0].rect).toEqual(intent);
  });

  // ---- #936: hoisted, persisted, and reachable from outside the map ------------------------

  it("a request from outside the map is QUEUED, not opened — only the map knows the anchor", () => {
    const { result } = setup();
    act(() => result.current.requestOpen(session(1)));
    expect(result.current.windows).toHaveLength(0);
    expect(result.current.pending.map((p) => p.seed.key)).toEqual(["claude:s1"]);
    // ...and the map draining it is an ordinary open.
    act(() => result.current.drain(new Map(), BOUNDS, true));
    expect(result.current.windows).toHaveLength(1);
    expect(result.current.pending).toEqual([]);
    expect(result.current.rejected).toEqual([]);
  });

  it("queues one request per session — a double-click on a row is not two windows", () => {
    const { result } = setup();
    act(() => {
      result.current.requestOpen(session(1));
      result.current.requestOpen(session(1));
    });
    expect(result.current.pending).toHaveLength(1);
  });

  it("restore opens the stored layout AT ITS GEOMETRY, and takes no focus on arrival", () => {
    localStorage.setItem(
      WORKSPACE_KEY,
      JSON.stringify([
        { key: "claude:s1", engine: "claude", id: "s1", title: "One", x: 40, y: 60, w: 700, h: 500, z: 1 },
        { key: "claude:s2", engine: "claude", id: "s2", title: "Two", x: 90, y: 20, w: 640, h: 420, z: 2 },
      ]),
    );
    const { result } = setup();
    expect(result.current.hydrated).toBe(false);
    act(() => result.current.restore(BOUNDS));
    expect(result.current.windows.map((w) => w.key)).toEqual([
      "claude:s1",
      "claude:s2",
    ]);
    expect(result.current.windows[0].rect).toEqual({ x: 40, y: 60, w: 700, h: 500 });
    // A restore is an arrival, not a gesture: nothing steals the focus ring.
    expect(result.current.focusedKey).toBeNull();
    expect(result.current.hydrated).toBe(true);
  });

  it("restore CONSUMES the stored layout — a second one cannot resurrect a closed window", () => {
    // Two calls, and they must fail for two different reasons. The first pair is StrictMode's
    // double effect landing in one commit, where neither call has seen the other's state — the
    // dedupe inside `open` already covers that. The one that needs the layout to be *consumed*
    // is the second pair: once the operator has closed a restored window, a later restore (a
    // remount, a resize crossing the host threshold) must not bring it back from storage.
    localStorage.setItem(
      WORKSPACE_KEY,
      JSON.stringify([
        { key: "claude:s1", engine: "claude", id: "s1", title: "One", x: 0, y: 0, w: 700, h: 500, z: 1 },
      ]),
    );
    const { result } = setup();
    act(() => {
      result.current.restore(BOUNDS);
      result.current.restore(BOUNDS);
    });
    expect(result.current.windows).toHaveLength(1);
    act(() => result.current.close("claude:s1"));
    act(() => result.current.restore(BOUNDS));
    expect(result.current.windows).toEqual([]);
  });

  it("restore is NOT truncated by a lowered cap — that would silently delete the layout", () => {
    // The trap this pins: restore opens `cap` windows, hydration flips on, and the debounced
    // persist effect then writes the TRUNCATED list back over the operator's saved layout. The
    // loss is silent and only shows up on the load after next. So a restore is bounded by the
    // hard maximum instead — the soft cap gates what may be OPENED, and lowering it is
    // documented never to close a window.
    localStorage.setItem(
      WORKSPACE_KEY,
      JSON.stringify(
        [1, 2, 3, 4].map((n) => ({
          key: `claude:s${n}`,
          engine: "claude",
          id: `s${n}`,
          title: `S${n}`,
          x: 0,
          y: 0,
          w: 700,
          h: 500,
          z: n,
        })),
      ),
    );
    localStorage.setItem(CAP_KEY, "2");
    const { result } = setup();
    act(() => result.current.restore(BOUNDS));
    expect(result.current.windows).toHaveLength(4);
    expect(result.current.cap).toBe(2);
    // A restore raises no notice either way: it is not something the operator just did, and the
    // toast would be gone before they reached the map.
    expect(result.current.notice).toBeNull();
  });

  it("restore still stops at the HARD maximum, so a hand-edited store cannot open 60 windows", () => {
    localStorage.setItem(
      WORKSPACE_KEY,
      JSON.stringify(
        Array.from({ length: 40 }, (_, i) => ({
          key: `claude:h${i}`,
          engine: "claude",
          id: `h${i}`,
          title: `H${i}`,
          x: 0,
          y: 0,
          w: 700,
          h: 500,
          z: i,
        })),
      ),
    );
    const { result } = setup();
    act(() => result.current.restore(BOUNDS));
    expect(result.current.windows).toHaveLength(WINDOW_CAP_MAX);
  });

  it("detach unfreezes the transport identity, so a remount cannot relaunch the session", () => {
    // The frozen `key` protects a LIVE mount. Past it, coming back to the map would re-mount the
    // `new-<uuid>` placeholder still carrying `fresh` — and send `new=1` again, starting a second
    // session on a map visit.
    const { result } = setup();
    const placeholder = {
      key: "opencode:new-2f1c9a4e-0000-4000-8000-abcdefabcdef",
      engine: "opencode",
      id: "new-2f1c9a4e-0000-4000-8000-abcdefabcdef",
      title: "New session",
    };
    act(() =>
      result.current.open(placeholder, null, BOUNDS, { cwd: "/w", bypass: true }),
    );
    act(() => result.current.reconcile(placeholder.key, "opencode:ses_9f2b"));
    expect(result.current.windows[0].fresh).toBeTruthy();

    act(() => result.current.detach());
    const [w] = result.current.windows;
    expect(w.key).toBe("opencode:ses_9f2b");
    expect(w.engine).toBe("opencode");
    expect(w.id).toBe("ses_9f2b"); // the NATIVE id the ws url is built from
    expect(w.fresh).toBeUndefined();
  });

  it("detach DROPS a window that never learned its real id — it can neither attach nor relaunch", () => {
    const { result } = setup();
    const placeholder = {
      key: "opencode:new-2f1c9a4e-0000-4000-8000-abcdefabcdef",
      engine: "opencode",
      id: "new-2f1c9a4e-0000-4000-8000-abcdefabcdef",
      title: "New session",
    };
    act(() => {
      result.current.open(session(1), null, BOUNDS);
      result.current.open(placeholder, null, BOUNDS, { cwd: "/w", bypass: true });
    });
    act(() => result.current.detach());
    expect(result.current.windows.map((w) => w.key)).toEqual(["claude:s1"]);
  });

  it("detach leaves an ordinary window completely alone", () => {
    const { result } = setup();
    act(() => result.current.open(session(1), null, BOUNDS));
    const before = result.current.windows;
    act(() => result.current.detach());
    // Same ARRAY, not just equal contents: a new identity here would re-render the map on every
    // navigation for no reason.
    expect(result.current.windows).toBe(before);
  });

  it("a capped request is REJECTED, not dropped — the launch it carries must survive", () => {
    // Hermes on #939: clearing the queue after a refused open threw away the cwd and bypass the
    // operator had just chosen on the new-session form. The drain now reports the refusal as an
    // outcome, and the canvas hands it back to the full-screen route.
    const { result } = setup();
    act(() => result.current.setCap(1));
    act(() => result.current.open(session(1), null, BOUNDS));
    const fresh = { cwd: "/w", bypass: true };
    act(() =>
      result.current.requestOpen(
        { key: "claude:brand-new", engine: "claude", id: "brand-new", title: "New session" },
        fresh,
      ),
    );
    act(() => result.current.drain(new Map(), BOUNDS, true));
    expect(result.current.windows).toHaveLength(1);
    expect(result.current.pending).toEqual([]);
    expect(result.current.rejected.map((r) => r.seed.key)).toEqual(["claude:brand-new"]);
    // The launch parameters travel with it — that is the whole point of not dropping it.
    expect(result.current.rejected[0].fresh).toEqual(fresh);
    expect(result.current.rejected[0].reason).toBe("cap"); // #1648: the hand-back says "full"
    act(() => result.current.clearRejected());
    expect(result.current.rejected).toEqual([]);
  });

  it("a map that cannot host rejects the WHOLE queue rather than dropping it", () => {
    const { result } = setup();
    act(() => {
      result.current.requestOpen(session(1));
      result.current.requestOpen(session(2));
    });
    act(() => result.current.drain(new Map(), BOUNDS, false));
    expect(result.current.windows).toEqual([]);
    expect(result.current.pending).toEqual([]);
    expect(result.current.rejected.map((r) => r.seed.key)).toEqual([
      "claude:s1",
      "claude:s2",
    ]);
    // Not "full" (#1648): the map could not host a window at all.
    expect(result.current.rejected.map((r) => r.reason)).toEqual(["host", "host"]);
  });

  it("room lost between the request and the drain: refused as a CAP refusal, launch intact (#1648)", () => {
    // The New session modal checks room at Start, then queues; a window opened in between (another
    // entry point, a restore) fills the cap before the canvas drains. Deterministic here.
    const { result } = setup();
    act(() => result.current.setCap(2));
    act(() => result.current.open(session(1), null, BOUNDS));
    expect(result.current.windows.length).toBeLessThan(2); // room at the moment of the request
    const fresh = { cwd: "/w", bypass: false };
    act(() =>
      result.current.requestOpen(
        { key: "claude:from-modal", engine: "claude", id: "from-modal", title: "New session" },
        fresh,
      ),
    );
    act(() => result.current.open(session(2), null, BOUNDS)); // the cap fills before the drain
    act(() => result.current.drain(new Map(), BOUNDS, true));
    expect(result.current.windows.map((w) => w.key)).toEqual(["claude:s1", "claude:s2"]);
    expect(result.current.rejected).toHaveLength(1);
    expect(result.current.rejected[0]).toMatchObject({ reason: "cap", fresh });
  });

  it("a request for a session that is ALREADY open counts as honoured, not refused", () => {
    // It focuses and flashes the existing window, which is exactly what the caller asked for.
    // Reporting it as rejected would bounce the operator to the full-screen route instead.
    const { result } = setup();
    act(() => result.current.open(session(1), null, BOUNDS));
    act(() => result.current.requestOpen(session(1)));
    act(() => result.current.drain(new Map(), BOUNDS, true));
    expect(result.current.windows).toHaveLength(1);
    expect(result.current.rejected).toEqual([]);
    expect(result.current.flashKey).toBe("claude:s1");
  });

  it("raising the cap admits the window the old one refused", () => {
    const { result } = setup();
    act(() => result.current.setCap(1));
    act(() => {
      result.current.open(session(1), null, BOUNDS);
      result.current.open(session(2), null, BOUNDS);
    });
    expect(result.current.windows).toHaveLength(1);
    expect(result.current.notice).toMatch(/limit/i);
    act(() => result.current.setCap(2));
    act(() => result.current.open(session(2), null, BOUNDS));
    expect(result.current.windows).toHaveLength(2);
  });

  it("LOWERING the cap never closes a window — a settings change is not a destructive act", () => {
    const { result } = setup();
    act(() => {
      result.current.open(session(1), null, BOUNDS);
      result.current.open(session(2), null, BOUNDS);
      result.current.open(session(3), null, BOUNDS);
    });
    act(() => result.current.setCap(1));
    expect(result.current.windows).toHaveLength(3);
    expect(result.current.cap).toBe(1);
    // It refuses the NEXT one, and nothing else.
    act(() => result.current.open(session(4), null, BOUNDS));
    expect(result.current.windows).toHaveLength(3);
  });

  it("the cap is clamped into range, whatever it is asked for", () => {
    const { result } = setup();
    act(() => result.current.setCap(9999));
    expect(result.current.cap).toBe(WINDOW_CAP_MAX);
    act(() => result.current.setCap(0));
    expect(result.current.cap).toBe(WINDOW_CAP_MIN);
  });
});

describe("minimize and arrange", () => {
  it("minimize parks a window — still open, still under the cap, focus released", () => {
    const { result } = setup();
    act(() => {
      result.current.open(session(1), null, BOUNDS);
      result.current.open(session(2), null, BOUNDS);
    });
    act(() => result.current.minimize("claude:s2"));
    expect(result.current.windows).toHaveLength(2);
    expect(result.current.windows.find((w) => w.key === "claude:s2")?.minimized).toBe(true);
    expect(result.current.focusedKey).toBeNull();
  });

  it("any raise brings a parked window back on top — the tray chip and a re-open alike", () => {
    const { result } = setup();
    act(() => {
      result.current.open(session(1), null, BOUNDS);
      result.current.open(session(2), null, BOUNDS);
    });
    act(() => result.current.minimize("claude:s1"));
    act(() => result.current.focus("claude:s1"));
    const w1 = result.current.windows.find((w) => w.key === "claude:s1")!;
    expect(w1.minimized).toBe(false);
    expect(result.current.focusedKey).toBe("claude:s1");
    expect(w1.z).toBeGreaterThan(result.current.windows.find((w) => w.key === "claude:s2")!.z);

    // A re-open of a parked session (sidebar, Ask "Open in map") restores it rather than refusing.
    act(() => result.current.minimize("claude:s1"));
    act(() => result.current.open(session(1), null, BOUNDS));
    expect(result.current.windows).toHaveLength(2);
    expect(result.current.windows.find((w) => w.key === "claude:s1")?.minimized).toBe(false);
  });

  it("arrange tiles the shown windows sorted by name, stacking in that order, parked ones untouched", () => {
    const { result } = setup();
    const named = (n: number, title: string): WindowSeed => ({ ...session(n), title });
    act(() => {
      result.current.open(named(1, "zeta"), null, BOUNDS);
      result.current.open(named(2, "Alpha"), null, BOUNDS);
      result.current.open(named(3, "mid"), null, BOUNDS);
      result.current.open(named(4, "parked"), null, BOUNDS);
    });
    act(() => result.current.minimize("claude:s4"));
    const parkedBefore = result.current.windows.find((w) => w.key === "claude:s4")!.rect;
    act(() => result.current.arrange(BOUNDS));
    const by = (k: string) => result.current.windows.find((w) => w.key === k)!;
    // 3 windows in 1400×900 → a 2×2 grid at 700×450; name order Alpha, mid, zeta.
    expect(by("claude:s2").rect).toEqual({ x: 0, y: 0, w: 700, h: 450 });
    expect(by("claude:s3").rect).toEqual({ x: 700, y: 0, w: 700, h: 450 });
    expect(by("claude:s1").rect).toEqual({ x: 0, y: 450, w: 700, h: 450 });
    expect(by("claude:s2").z).toBeLessThan(by("claude:s3").z);
    expect(by("claude:s3").z).toBeLessThan(by("claude:s1").z);
    expect(by("claude:s4").rect).toEqual(parkedBefore);
    expect(by("claude:s4").minimized).toBe(true);
  });

  it("arrange parks what does not fit at the floor — every window stays reachable (Hermes on #1320)", () => {
    const { result } = setup();
    act(() => {
      for (let n = 1; n <= 8; n++) result.current.open(session(n), null, BOUNDS);
    });
    const small = { w: 600, h: 330 }; // hostable, but holds one window at the floor
    act(() => result.current.arrange(small));
    const shown = result.current.windows.filter((w) => !w.minimized);
    expect(shown.map((w) => w.title)).toEqual(["Session 1"]);
    expect(shown[0].rect).toEqual({ x: 0, y: 0, w: 600, h: 330 });
    expect(result.current.windows.filter((w) => w.minimized)).toHaveLength(7);
    expect(result.current.windows).toHaveLength(8); // parked, never closed
  });

  it("a refused BATCH is kept whole for the map to list, never trimmed to the first (Hermes on #1320)", () => {
    const { result } = setup();
    act(() => {
      for (let n = 1; n <= 3; n++) result.current.requestOpen(session(n));
    });
    act(() => result.current.drain(new Map(), BOUNDS, false));
    expect(result.current.rejected).toHaveLength(3);
    act(() => result.current.refuseRejected());
    expect(result.current.rejected).toHaveLength(0);
    expect(result.current.refused.map((r) => r.seed.key)).toEqual([
      "claude:s1",
      "claude:s2",
      "claude:s3",
    ]);
    act(() => result.current.dismissRefused());
    expect(result.current.refused).toHaveLength(0);
  });

  it("a stored parked window restores parked", () => {
    localStorage.setItem(
      WORKSPACE_KEY,
      JSON.stringify([
        { key: "claude:s1", engine: "claude", id: "s1", title: "Session 1", x: 0, y: 0, w: 720, h: 480, z: 1, minimized: true },
        { key: "claude:s2", engine: "claude", id: "s2", title: "Session 2", x: 0, y: 0, w: 720, h: 480, z: 2 },
      ]),
    );
    const { result } = setup();
    act(() => result.current.restore(BOUNDS));
    expect(result.current.windows.map((w) => [w.key, Boolean(w.minimized)])).toEqual([
      ["claude:s1", true],
      ["claude:s2", false],
    ]);
  });
});

// Commands from OUTSIDE the map, before the map has mounted since the page loaded (#2102, review
// of PR #2109): the stored layout is then the workspace's only record of its windows. A command
// has to change that record AND storage — otherwise the next page load restores the window.
describe("before hydration: commands act on the stored layout, and the change is written", () => {
  const entry = (n: number) => ({
    key: `claude:s${n}`,
    engine: "claude",
    id: `s${n}`,
    title: `Session ${n}`,
    x: 40 * n,
    y: 40 * n,
    w: 640,
    h: 420,
    z: n,
  });
  const seed = (...ns: number[]) =>
    localStorage.setItem(WORKSPACE_KEY, JSON.stringify(ns.map(entry)));
  const storedKeys = () =>
    (JSON.parse(localStorage.getItem(WORKSPACE_KEY) ?? "[]") as { key: string }[]).map(
      (w) => w.key,
    );

  it("reading a layout back writes nothing", () => {
    seed(1, 2);
    const raw = localStorage.getItem(WORKSPACE_KEY);
    setup();
    expect(localStorage.getItem(WORKSPACE_KEY)).toBe(raw);
  });

  it("close removes the stored window for good: a reload does not bring it back", () => {
    seed(1, 2);
    const { result, unmount } = setup();
    act(() => result.current.close("claude:s1"));
    expect(result.current.restorable.map((w) => w.key)).toEqual(["claude:s2"]);
    expect(storedKeys()).toEqual(["claude:s2"]);
    unmount();
    const again = setup(); // the page reloads
    act(() => again.result.current.restore(BOUNDS));
    expect(again.result.current.windows.map((w) => w.key)).toEqual(["claude:s2"]);
  });

  it("minimize parks the stored window, and it restores parked", () => {
    seed(1, 2);
    const { result, unmount } = setup();
    act(() => result.current.minimize("claude:s2"));
    unmount();
    const again = setup();
    act(() => again.result.current.restore(BOUNDS));
    expect(again.result.current.windows.map((w) => [w.key, Boolean(w.minimized)])).toEqual([
      ["claude:s1", false],
      ["claude:s2", true],
    ]);
  });

  it("close all empties the stored layout", () => {
    seed(1, 2);
    const { result } = setup();
    act(() => result.current.closeAll());
    expect(result.current.restorable).toEqual([]);
    expect(localStorage.getItem(WORKSPACE_KEY)).toBeNull();
  });

  it("a command behind a queued open queues behind it, and the drain replays them in order", () => {
    const { result } = setup();
    act(() => result.current.restore(BOUNDS));
    act(() => {
      result.current.requestOpen(session(1));
      result.current.requestOpen(session(2));
      result.current.command({ type: "close", key: "claude:s1", ack: "c1" });
      result.current.command({ type: "minimize", key: "claude:s2", ack: "m2" });
    });
    // Nothing ran yet: applied now, the close would have run BEFORE the open it follows.
    expect(result.current.queue.map((c) => c.kind)).toEqual(["open", "open", "close", "minimize"]);
    expect(result.current.pending.map((p) => p.seed.key)).toEqual(["claude:s1", "claude:s2"]);
    expect(result.current.acks).toEqual([]);
    act(() => result.current.drain(new Map(), BOUNDS, true));
    expect(result.current.windows.map((w) => [w.key, Boolean(w.minimized)])).toEqual([
      ["claude:s2", true],
    ]);
    expect(result.current.queue).toEqual([]);
    expect(result.current.acks).toEqual([
      { id: "c1", result: "applied" },
      { id: "m2", result: "applied" },
    ]);
  });

  it("with nothing queued a command applies at once, and is answered at once", () => {
    const { result } = setup();
    act(() => result.current.open(session(1), null, BOUNDS));
    act(() => {
      result.current.command({ type: "minimize", key: "claude:s1", ack: "m" });
      result.current.command({ type: "close", key: "claude:s9", ack: "c" });
      result.current.command({ type: "closeAll", ack: "all" });
      result.current.command({ type: "closeAll", ack: "again" });
    });
    expect(result.current.windows).toEqual([]);
    expect(result.current.acks).toEqual([
      { id: "m", result: "applied" },
      { id: "c", result: "noop", reason: "absent" },
      { id: "all", result: "applied" },
      { id: "again", result: "noop", reason: "empty" },
    ]);
  });

  it("a later request for a queued, minimised session brings it back: the order decides", () => {
    const { result } = setup();
    act(() => result.current.restore(BOUNDS));
    act(() => {
      result.current.command({ type: "request", req: { seed: session(1), acks: ["o"] } });
      result.current.command({ type: "minimize", key: "claude:s1", ack: "m" });
      result.current.command({ type: "request", req: { seed: session(1), acks: ["f"] } });
    });
    act(() => result.current.drain(new Map(), BOUNDS, true));
    expect(result.current.windows.map((w) => [w.key, Boolean(w.minimized)])).toEqual([
      ["claude:s1", false],
    ]);
    expect(result.current.acks.map((a) => [a.id, a.result])).toEqual([
      ["o", "applied"],
      ["m", "applied"],
      ["f", "applied"],
    ]);
  });

  it("every Arrange is answered by its own id: applied, nothing on screen, or refused", () => {
    const { result } = setup();
    act(() => result.current.restore(BOUNDS));
    act(() => result.current.command({ type: "requestArrange", ack: "empty" }));
    act(() => result.current.drain(new Map(), BOUNDS, true));
    act(() => result.current.open(session(1), null, BOUNDS));
    act(() => result.current.minimize("claude:s1"));
    act(() => result.current.command({ type: "requestArrange", ack: "parked" }));
    act(() => result.current.drain(new Map(), BOUNDS, true));
    act(() => result.current.focus("claude:s1"));
    act(() => result.current.command({ type: "requestArrange", ack: "ran" }));
    act(() => result.current.drain(new Map(), BOUNDS, true));
    act(() => result.current.command({ type: "requestArrange", ack: "small" }));
    act(() => result.current.drain(new Map(), BOUNDS, false));
    expect(result.current.acks).toEqual([
      { id: "empty", result: "noop", reason: "empty" },
      { id: "parked", result: "noop", reason: "parked" },
      { id: "ran", result: "applied", parked: [] },
      { id: "small", result: "refused", reason: "host" },
    ]);
  });

  it("a withdrawn command is answered cancelled and never runs", () => {
    const { result } = setup();
    act(() => result.current.restore(BOUNDS));
    act(() => {
      result.current.command({ type: "request", req: { seed: session(1), acks: ["o"] } });
      result.current.command({ type: "requestArrange", ack: "a" });
      result.current.command({ type: "cancel", acks: ["o", "a"] });
    });
    expect(result.current.queue).toEqual([]);
    act(() => result.current.drain(new Map(), BOUNDS, true));
    expect(result.current.windows).toEqual([]);
    expect(result.current.acks.map((a) => [a.id, a.result])).toEqual([
      ["o", "cancelled"],
      ["a", "cancelled"],
    ]);
  });

  it("a numbered action records how far the state has got with its caller's dispatches", () => {
    const { result } = setup();
    expect(result.current.seq).toBe(0);
    act(() => result.current.command({ type: "closeAll", seq: 3 }));
    act(() => result.current.command({ type: "closeAll", seq: 2 })); // never goes back
    expect(result.current.seq).toBe(3);
  });
});
