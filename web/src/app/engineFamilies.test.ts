/** #1728 — the family decision is the server's; the SPA only reads it and applies the device's
 *  explicit choice on top. Engine ids come from the generated fixture, never a literal. */
import { afterEach, expect, test } from "vitest";
import fixture from "../test/roster.fixture.json";
import type { EngineFamily, EngineInfo } from "../types/api";
import { familyMark, familyOf, getRoster, pickInFamily, setRoster } from "./engineRoster";

const ENGINES = fixture.engines as EngineInfo[];
const API = ENGINES.find((e) => e.runtime === "api" && e.api?.source)!;
const CONSOLE = ENGINES.find((e) => e.id === API.api!.source)!;

function row(engine: string | null, reason: string, reasons: Record<string, string> = {}): EngineFamily {
  return {
    family: API.family!,
    clients: [CONSOLE.id, API.id],
    interactive_default: { engine, reason },
    unattended_admitted: [],
    reasons,
  };
}

afterEach(() => setRoster(ENGINES));

test("the fixture puts an API client in its source agent's family", () => {
  expect(API.family).toBeTruthy();
  expect(CONSOLE.family).toBe(API.family);
});

test("an older server sends no families: nothing is derived from ids or `api.source`", () => {
  setRoster(ENGINES);
  expect(getRoster().families).toEqual([]);
  expect(familyOf(API.id)).toBeUndefined();
  expect(familyOf(CONSOLE.id)).toBeUndefined();
});

test("the server's default applies with its typed reason", () => {
  setRoster(ENGINES, [], [row(API.id, "api_ready")]);
  const fam = familyOf(CONSOLE.id)!;
  expect(fam).toBe(familyOf(API.id));
  const ready = pickInFamily(fam, [CONSOLE.id, API.id], null);
  expect(ready).toMatchObject({ engine: API.id, reason: "api_ready", choice: "automatic", stale: null });
  expect(familyMark(ready)).toEqual({ word: "default" });

  setRoster(ENGINES, [], [row(CONSOLE.id, "api_unready", { [API.id]: "signed out" })]);
  const unready = pickInFamily(familyOf(API.id)!, [CONSOLE.id], null);
  expect(unready).toMatchObject({ engine: CONSOLE.id, reason: "api_unready", choice: "automatic" });
  expect(familyMark(unready)?.word).toBe("default here");
});

test("a remembered explicit choice wins while it can start, and is ignored with its reason otherwise", () => {
  setRoster(ENGINES, [], [row(API.id, "api_ready")]);
  const fam = familyOf(API.id)!;
  const kept = pickInFamily(fam, [CONSOLE.id, API.id], CONSOLE.id);
  expect(kept).toMatchObject({ engine: CONSOLE.id, reason: "remembered_explicit", choice: "explicit" });
  expect(familyMark(kept)?.word).toBe("remembered");

  // Unready (not offered): ignored, the default applies, the reason says why.
  setRoster(ENGINES, [], [row(CONSOLE.id, "api_unready", { [API.id]: "signed out" })]);
  const unready = pickInFamily(familyOf(API.id)!, [CONSOLE.id], API.id);
  expect(unready).toMatchObject({ engine: CONSOLE.id, reason: "remembered_stale", choice: "automatic" });
  expect(unready.stale).toEqual({ engine: API.id, why: "signed out" });

  // Uninstalled (no longer in the family): ignored.
  setRoster(ENGINES, [], [row(API.id, "api_ready")]);
  const gone = pickInFamily(familyOf(API.id)!, [CONSOLE.id, API.id], "some-removed-id");
  expect(gone.reason).toBe("remembered_stale");
  expect(gone.stale?.why).toBe("it is no longer installed");

  // Retired (listed, but retiring): ignored.
  const retiring = ENGINES.map((e) => (e.id === CONSOLE.id ? { ...e, status: "retiring" } : e));
  setRoster(retiring, [], [row(API.id, "api_ready", { [CONSOLE.id]: "agent removed" })]);
  const retired = pickInFamily(familyOf(API.id)!, [CONSOLE.id, API.id], CONSOLE.id);
  expect(retired).toMatchObject({ engine: API.id, reason: "remembered_stale" });
});

test("a retired family has no default and no mark", () => {
  setRoster(ENGINES, [], [row(null, "family_retired")]);
  const pick = pickInFamily(familyOf(API.id)!, [CONSOLE.id, API.id], null);
  expect(pick).toMatchObject({ engine: null, reason: "family_retired" });
  expect(familyMark(pick)).toBeNull();
});

test("a malformed families row is dropped as a whole, never half-read", () => {
  setRoster(ENGINES, [], [
    { family: "x", clients: "nope" } as unknown as EngineFamily,
    { ...row(API.id, "api_ready"), interactive_default: { engine: 3, reason: "api_ready" } } as unknown as EngineFamily,
    row(API.id, "api_ready"),
  ]);
  expect(getRoster().families).toHaveLength(1);
});
