import { act, renderHook } from "@testing-library/react";
import { StrictMode } from "react";
import { describe, expect, it } from "vitest";
import type { Session } from "../../types/api";
import { useWorkspace } from "./useWorkspace";
import { clampRect, DEFAULT_SIZE, WINDOW_CAP } from "./workspace";

// The workspace transition (#208). Rendered under StrictMode ON PURPOSE: the app mounts in
// StrictMode, which double-invokes reducers and effects, and the first cut of `open()` decided
// its outcome by mutating a holder object inside a functional state updater and reading it
// afterwards. That is not something React promises — the read could see a decision not yet made,
// or made twice — so a capped open could skip its notice and focus a key with no window behind
// it. These cases are the ones that shape could get wrong.

const BOUNDS = { w: 1400, h: 900 };

const session = (n: number): Session =>
  ({
    id: `claude:s${n}`,
    engine: "claude",
    uuid: `s${n}`,
    short_uuid: `s${n}`,
    cwd: "/w",
    project: { kind: "folder", id: "/w", name: "w" },
    last_mtime: 0,
    first_user_message: "",
    title: `Session ${n}`,
    sticky: false,
    archived: false,
  }) as unknown as Session;

const setup = () =>
  renderHook(() => useWorkspace(), { wrapper: StrictMode });

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
});
