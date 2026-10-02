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
