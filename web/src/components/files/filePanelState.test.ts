import { beforeEach, describe, expect, it } from "vitest";
import {
  MAX_EXPANDED,
  MAX_SESSIONS,
  STATE_KEY,
  clearPanelState,
  loadPanelState,
  migratePanelState,
  savePanelState,
} from "./filePanelState";

describe("per-session panel state (#783)", () => {
  beforeEach(() => {
    localStorage.clear();
    clearPanelState();
  });

  it("remembers a session's root so switching away and back keeps your place", () => {
    savePanelState("claude:a", { open: true, root: "/home/u/proj/src", expanded: ["/home/u/proj"] });
    expect(loadPanelState("claude:a")).toMatchObject({
      open: true,
      root: "/home/u/proj/src",
      expanded: ["/home/u/proj"],
    });
    expect(loadPanelState("claude:unknown")).toBeNull();
  });

  it("evicts the least-recently-touched session — the store cannot grow forever", () => {
    for (let i = 0; i < MAX_SESSIONS + 5; i++) {
      savePanelState(`claude:${i}`, { open: true, root: `/r/${i}`, expanded: [] });
    }
    const kept = Object.keys(JSON.parse(localStorage.getItem(STATE_KEY)!).sessions);
    expect(kept).toHaveLength(MAX_SESSIONS);
    // The oldest are gone, the newest survive.
    expect(loadPanelState("claude:0")).toBeNull();
    expect(loadPanelState(`claude:${MAX_SESSIONS + 4}`)).not.toBeNull();
  });

  it("touching an old session keeps it alive (it is an LRU, not a FIFO)", () => {
    savePanelState("claude:keep", { open: true, root: "/keep", expanded: [] });
    for (let i = 0; i < MAX_SESSIONS - 1; i++) {
      savePanelState(`claude:f${i}`, { open: true, root: `/f/${i}`, expanded: [] });
    }
    savePanelState("claude:keep", { open: true, root: "/keep", expanded: [] }); // touch
    savePanelState("claude:new", { open: true, root: "/new", expanded: [] }); // forces eviction
    expect(loadPanelState("claude:keep")).not.toBeNull();
  });

  it("caps the expanded set — a deep tree must not accumulate unbounded paths", () => {
    const many = Array.from({ length: MAX_EXPANDED + 50 }, (_, i) => `/p/${i}`);
    savePanelState("claude:a", { open: true, root: "/p", expanded: many });
    const saved = loadPanelState("claude:a")!;
    expect(saved.expanded).toHaveLength(MAX_EXPANDED);
    // Keeps the TAIL — the most recently expanded, which is what the user is looking at.
    expect(saved.expanded.at(-1)).toBe(`/p/${MAX_EXPANDED + 49}`);
  });

  it("survives corrupt storage rather than throwing", () => {
    localStorage.setItem(STATE_KEY, "{not json");
    expect(loadPanelState("claude:a")).toBeNull();
    expect(() => savePanelState("claude:a", { open: true, root: "/x", expanded: [] })).not.toThrow();
  });
});

describe("placeholder → real identity migration (#127)", () => {
  beforeEach(() => {
    localStorage.clear();
    clearPanelState();
  });

  it("carries state across the converge so a reload at the real id still finds it", () => {
    savePanelState("opencode:new-abc", { open: true, root: "/r", expanded: ["/r/x"] });
    migratePanelState("opencode:new-abc", "opencode:ses_real");
    expect(loadPanelState("opencode:new-abc")).toBeNull();
    expect(loadPanelState("opencode:ses_real")).toMatchObject({
      open: true,
      root: "/r",
      expanded: ["/r/x"],
    });
  });

  it("never clobbers an existing target, and is a no-op when there is nothing to move", () => {
    savePanelState("a", { open: true, root: "/from", expanded: [] });
    savePanelState("b", { open: false, root: "/keep", expanded: [] });
    migratePanelState("a", "b");
    expect(loadPanelState("b")?.root).toBe("/keep");
    expect(() => migratePanelState("missing", "also-missing")).not.toThrow();
    expect(() => migratePanelState("a", "a")).not.toThrow();
  });
});
