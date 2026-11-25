import { beforeEach, describe, expect, it } from "vitest";
import type { WorkspaceWindow } from "./useWorkspace";
import {
  CAP_KEY,
  decodeWorkspace,
  encodeWorkspace,
  isPersistableKey,
  loadWindowCap,
  loadWorkspace,
  saveWindowCap,
  saveWorkspace,
  WORKSPACE_KEY,
} from "./windowStore";
import { MIN_SIZE, WINDOW_CAP, WINDOW_CAP_MAX, WINDOW_CAP_MIN } from "./workspace";

/** The device-local workspace codec (#936, delivering #872).
 *
 *  These are the READ-side rules, and they are the ones worth pinning: the payload comes back out
 *  of `localStorage`, which the operator can edit, and both of the things it carries — a session
 *  key and four numbers — end up somewhere that matters (a WebSocket URL, an inline style). The
 *  contract is "never throw, never return something the workspace cannot use", so every case
 *  below is a rejection that has to degrade to a smaller layout rather than to an exception. */

const win = (over: Partial<WorkspaceWindow> = {}): WorkspaceWindow => ({
  key: "claude:s1",
  actionKey: "claude:s1",
  engine: "claude",
  id: "s1",
  title: "One",
  rect: { x: 10, y: 20, w: 720, h: 480 },
  z: 1,
  role: "owner",
  ...over,
});

describe("isPersistableKey", () => {
  it("accepts an engine-qualified key", () => {
    expect(isPersistableKey("claude:9f2b1c")).toBe(true);
    expect(isPersistableKey("opencode:ses_9f2b")).toBe(true);
  });

  it("REFUSES a new-session placeholder — restoring one would re-run a launch", () => {
    // The whole point of the rule: a `new-<uuid>` window is mid-LAUNCH. Persisting it means a
    // page reload starts a session nobody asked for. The server draws the same line
    // (`canonical_key` rejects the shape; only the ws launch path accepts it).
    expect(
      isPersistableKey("opencode:new-2f1c9a4e-0000-4000-8000-abcdefabcdef"),
    ).toBe(false);
  });

  it("refuses shapes that are not an engine-qualified id at all", () => {
    for (const bad of [
      "",
      "claude",
      ":s1",
      "claude:",
      "claude:s 1",
      "claude:../../etc/passwd",
      "cl aude:s1",
      "claude:s1?new=1",
      null,
      42,
      { key: "claude:s1" },
    ])
      expect(isPersistableKey(bad)).toBe(false);
  });
});

describe("decodeWorkspace", () => {
  it("returns an empty layout for anything that is not an array of entries", () => {
    for (const raw of [null, "", "not json", "{}", '"a string"', "42", "[1,2,3]"])
      expect(decodeWorkspace(raw)).toEqual([]);
  });

  it("drops a bad entry and KEEPS the rest — one corrupt window never costs the layout", () => {
    const out = decodeWorkspace(
      JSON.stringify([
        { key: "claude:s1", engine: "claude", id: "s1", title: "One", x: 1, y: 2, w: 700, h: 500, z: 1 },
        { key: "opencode:new-2f1c9a4e-0000-4000-8000-abcdefabcdef", engine: "opencode", id: "new-x", title: "L", x: 0, y: 0, w: 700, h: 500, z: 2 },
        { key: "claude:s3", engine: "claude", id: "s3", title: "Three", x: "NaN", y: 0, w: 700, h: 500, z: 3 },
        { key: "claude:s4", engine: "claude", id: "s4", title: "Four", x: 5, y: 6, w: 700, h: 500, z: 4 },
      ]),
    );
    expect(out.map((w) => w.key)).toEqual(["claude:s1", "claude:s4"]);
  });

  it("collapses duplicate keys to the first — one window per session, even in storage", () => {
    const out = decodeWorkspace(
      JSON.stringify([
        { key: "claude:s1", engine: "claude", id: "s1", title: "First", x: 0, y: 0, w: 700, h: 500, z: 1 },
        { key: "claude:s1", engine: "claude", id: "s1", title: "Second", x: 9, y: 9, w: 700, h: 500, z: 2 },
      ]),
    );
    expect(out).toHaveLength(1);
    expect(out[0].title).toBe("First");
  });

  it("clamps geometry rather than rejecting it — a negative or sub-floor value is survivable", () => {
    const [w] = decodeWorkspace(
      JSON.stringify([
        { key: "claude:s1", engine: "claude", id: "s1", title: "One", x: -400, y: -1, w: 10, h: 4, z: 1 },
      ]),
    );
    expect(w.x).toBe(0);
    expect(w.y).toBe(0);
    expect(w.w).toBe(MIN_SIZE.w);
    expect(w.h).toBe(MIN_SIZE.h);
  });

  it("restores in stacking order, so the window that was on top opens last", () => {
    const out = decodeWorkspace(
      JSON.stringify([
        { key: "claude:s1", engine: "claude", id: "s1", title: "1", x: 0, y: 0, w: 700, h: 500, z: 9 },
        { key: "claude:s2", engine: "claude", id: "s2", title: "2", x: 0, y: 0, w: 700, h: 500, z: 3 },
      ]),
    );
    expect(out.map((w) => w.key)).toEqual(["claude:s2", "claude:s1"]);
  });

  it("bounds a hand-edited payload so the read cannot become the expensive part of a load", () => {
    const many = Array.from({ length: 500 }, (_, i) => ({
      key: `claude:s${i}`,
      engine: "claude",
      id: `s${i}`,
      title: `${i}`,
      x: 0,
      y: 0,
      w: 700,
      h: 500,
      z: i,
    }));
    expect(decodeWorkspace(JSON.stringify(many)).length).toBeLessThanOrEqual(64);
  });
});

describe("encodeWorkspace", () => {
  it("stores the ACTION key, not the frozen transport key", () => {
    // After a converge the window still transports on its placeholder — but that id will not
    // exist on the next load, and the reconciled one will.
    const out = encodeWorkspace([
      win({
        key: "opencode:new-2f1c9a4e-0000-4000-8000-abcdefabcdef",
        actionKey: "opencode:ses_9f2b",
        engine: "opencode",
        id: "new-2f1c9a4e-0000-4000-8000-abcdefabcdef",
      }),
    ]);
    expect(out).toHaveLength(1);
    expect(out[0].key).toBe("opencode:ses_9f2b");
    // ...and the ws-facing pieces are re-derived from it, never carried over from the launch.
    expect(out[0].engine).toBe("opencode");
    expect(out[0].id).toBe("ses_9f2b");
  });

  it("drops a window still mid-launch", () => {
    expect(
      encodeWorkspace([
        win({
          key: "opencode:new-2f1c9a4e-0000-4000-8000-abcdefabcdef",
          actionKey: "opencode:new-2f1c9a4e-0000-4000-8000-abcdefabcdef",
        }),
      ]),
    ).toEqual([]);
  });

  it("never carries the launch params or the live role into storage", () => {
    const [stored] = encodeWorkspace([
      win({ role: "secondary", fresh: { cwd: "/w", bypass: true } }),
    ]);
    expect(stored).not.toHaveProperty("fresh");
    expect(stored).not.toHaveProperty("role");
  });

  it("round-trips a layout through storage", () => {
    saveWorkspace([win(), win({ key: "claude:s2", actionKey: "claude:s2", id: "s2", z: 2 })]);
    expect(loadWorkspace().map((w) => w.key)).toEqual(["claude:s1", "claude:s2"]);
  });
});

describe("a parked (minimized) window", () => {
  it("round-trips parked, and an ordinary window stores no flag at all", () => {
    saveWorkspace([win({ minimized: true }), win({ key: "claude:s2", actionKey: "claude:s2", id: "s2", z: 2 })]);
    const [a, b] = loadWorkspace();
    expect(a.minimized).toBe(true);
    expect(b).not.toHaveProperty("minimized");
  });

  it("reads only a literal true — a hand-edited truthy value does not hide a window", () => {
    const [w] = decodeWorkspace(
      JSON.stringify([{ key: "claude:s1", engine: "claude", id: "s1", x: 0, y: 0, w: 720, h: 480, z: 1, minimized: "yes" }]),
    );
    expect(w).not.toHaveProperty("minimized");
  });
});

describe("the stored cap", () => {
  beforeEach(() => localStorage.clear());

  it("defaults when absent", () => {
    expect(loadWindowCap()).toBe(WINDOW_CAP);
  });

  it("clamps a stored value into range instead of trusting it", () => {
    saveWindowCap(9999);
    expect(loadWindowCap()).toBe(WINDOW_CAP_MAX);
    saveWindowCap(-3);
    expect(loadWindowCap()).toBe(WINDOW_CAP_MIN);
  });

  it("falls back to the default for a value that is not a number at all", () => {
    // Read-lenient on purpose: a hand-edited value must not be able to disable the workspace.
    localStorage.setItem(CAP_KEY, "lots");
    expect(loadWindowCap()).toBe(WINDOW_CAP);
  });
});

describe("an empty workspace", () => {
  beforeEach(() => localStorage.clear());

  it("REMOVES the key rather than storing an empty array", () => {
    saveWorkspace([win()]);
    expect(localStorage.getItem(WORKSPACE_KEY)).not.toBeNull();
    saveWorkspace([]);
    expect(localStorage.getItem(WORKSPACE_KEY)).toBeNull();
  });
});
