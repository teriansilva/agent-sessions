import { afterEach, describe, expect, test, vi } from "vitest";

import fixture from "../test/roster.fixture.json";
import type { EngineInfo } from "../types/api";
import {
  loadRuntimeHints,
  parseRuntimeHints,
  resetRoster,
  rosterReady,
  RUNTIME_HINTS_KEY,
  runtimeConfirmed,
  runtimeHint,
  setRoster,
} from "./engineRoster";
import { reloadRoster } from "./reloadRoster";
import { api } from "../lib/api";

// #1502: the persisted runtime map. Which pane to DRAW, recorded from every loaded roster and read
// back at init so a returning browser decides on its first render. Appearance only: it never
// makes the roster "ready", and the loaded roster always wins over it.

const ENGINES = fixture.engines as unknown as EngineInfo[];
const API = ENGINES.find((e) => e.runtime === "api")!.id;
const PTY = ENGINES.find((e) => e.runtime === "pty")!.id;

afterEach(() => {
  vi.restoreAllMocks();
  localStorage.clear();
});

/** A cold page load: the roster is not loaded, the module re-reads storage as at init. */
function coldLoad() {
  resetRoster();
  loadRuntimeHints();
}

describe("the persisted runtime map", () => {
  test("every loaded roster writes the whole {engine: runtime} map", () => {
    setRoster(ENGINES);
    const doc = JSON.parse(localStorage.getItem(RUNTIME_HINTS_KEY)!);
    expect(doc.v).toBe(1);
    expect(doc.runtimes).toEqual(Object.fromEntries(ENGINES.map((e) => [e.id, e.runtime])));
    // An engine that left the roster leaves the map too.
    setRoster(ENGINES.filter((e) => e.id !== API));
    expect(JSON.parse(localStorage.getItem(RUNTIME_HINTS_KEY)!).runtimes[API]).toBeUndefined();
  });

  test("it is read back at init: a cold load answers before any roster, without being ready", () => {
    setRoster(ENGINES);
    coldLoad();
    expect(rosterReady()).toBe(false);
    expect(runtimeHint(API)).toBe("api");
    expect(runtimeHint(PTY)).toBe("pty");
    expect(runtimeConfirmed(API)).toBe(false);
  });

  test("a never-visited browser has no hint", () => {
    localStorage.clear();
    coldLoad();
    expect(runtimeHint(API)).toBeUndefined();
  });

  test("the loaded roster wins over the hint, and confirms it", () => {
    localStorage.setItem(RUNTIME_HINTS_KEY, JSON.stringify({ v: 1, runtimes: { [PTY]: "api" } }));
    coldLoad();
    expect(runtimeHint(PTY)).toBe("api");
    setRoster(ENGINES);
    expect(runtimeHint(PTY)).toBe("pty");
    expect(runtimeConfirmed(PTY)).toBe(true);
  });

  test("once loaded, an id the roster does not list has no hint even if storage remembers one", () => {
    localStorage.setItem(RUNTIME_HINTS_KEY, JSON.stringify({ v: 1, runtimes: { gone: "api" } }));
    coldLoad();
    expect(runtimeHint("gone")).toBe("api");
    setRoster(ENGINES);
    expect(runtimeHint("gone")).toBeUndefined();
  });

  test.each([
    ["not JSON", "{oops"],
    ["another version", JSON.stringify({ v: 2, runtimes: { a: "api" } })],
    ["no map", JSON.stringify({ v: 1 })],
    ["an array", JSON.stringify({ v: 1, runtimes: ["api"] })],
    ["a non-string runtime", JSON.stringify({ v: 1, runtimes: { a: 1 } })],
    ["a hostile id", JSON.stringify({ v: 1, runtimes: { "__proto__x/../": "api" } })],
    ["a hostile runtime", JSON.stringify({ v: 1, runtimes: { a: "<img src=x>" } })],
    [
      "too many engines",
      JSON.stringify({
        v: 1,
        runtimes: Object.fromEntries(Array.from({ length: 65 }, (_, i) => [`e${i}`, "pty"])),
      }),
    ],
    ["oversized", JSON.stringify({ v: 1, runtimes: { a: "api" }, pad: "x".repeat(9000) })],
  ])("a corrupt or oversized value is ignored as a whole — %s", (_name, raw) => {
    expect(parseRuntimeHints(raw)).toBeNull();
    localStorage.setItem(RUNTIME_HINTS_KEY, raw);
    coldLoad();
    expect(runtimeHint("a")).toBeUndefined();
    expect(runtimeHint("e0")).toBeUndefined();
  });

  test("unavailable storage degrades to the no-hint path and never throws", () => {
    vi.spyOn(Storage.prototype, "getItem").mockImplementation(() => {
      throw new Error("SecurityError");
    });
    vi.spyOn(Storage.prototype, "setItem").mockImplementation(() => {
      throw new Error("QuotaExceededError");
    });
    expect(() => setRoster(ENGINES)).not.toThrow();
    expect(rosterReady()).toBe(true);
    coldLoad();
    expect(runtimeHint(API)).toBeUndefined();
  });
});

describe("reloadRoster reports its outcome (#1502)", () => {
  test("ok — the roster is installed", async () => {
    resetRoster();
    vi.spyOn(api, "engines").mockResolvedValue({ engines: ENGINES, problems: [] } as never);
    await expect(reloadRoster()).resolves.toBe("ok");
    expect(rosterReady()).toBe(true);
  });

  test("failed — a thrown read keeps the last roster and never rejects", async () => {
    setRoster(ENGINES);
    vi.spyOn(api, "engines").mockRejectedValue(new Error("offline"));
    await expect(reloadRoster()).resolves.toBe("failed");
    expect(rosterReady()).toBe(true);
  });

  test("failed — a response that is not a roster", async () => {
    resetRoster();
    vi.spyOn(api, "engines").mockResolvedValue({} as never);
    await expect(reloadRoster()).resolves.toBe("failed");
    expect(rosterReady()).toBe(false);
  });
});
