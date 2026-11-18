import { describe, expect, test } from "vitest";
import rosterFixture from "../test/roster.fixture.json";
import type { EngineFamily, EngineInfo } from "../types/api";
import { pickHandoffTarget } from "./handoffTarget";

// The roster GENERATED from the manifests; which API clients are ready is host state, so each test
// says it. `ready` = handoff targets on this "host".
function roster(ready: string[]): EngineInfo[] {
  return (rosterFixture.engines as EngineInfo[]).map((e) => ({
    ...e,
    supports_seed_start: ready.includes(e.id),
  }));
}

const fam = (family: string, clients: string[], def: string | null): EngineFamily => ({
  family,
  clients,
  interactive_default: { engine: def, reason: "api_ready" },
  unattended_admitted: [],
  reasons: {},
});

const FAMILIES = [
  fam("claude", ["claude", "claude-api"], "claude-api"),
  fam("codex", ["codex", "codex-api"], "codex-api"),
];

describe("pickHandoffTarget (#2141): API first", () => {
  const all = roster(["claude", "claude-api", "codex", "codex-api", "kimi"]);

  test("the stored default resolves to its family's API client", () => {
    expect(pickHandoffTarget(all, FAMILIES, "claude", "codex")).toBe("codex-api");
  });

  test("a terminal source moves to its own agent's API client", () => {
    expect(pickHandoffTarget(all, FAMILIES, "claude", null)).toBe("claude-api");
  });

  test("without the server's families, the API client's own manifest pairs them", () => {
    expect(pickHandoffTarget(all, undefined, "codex", null)).toBe("codex-api");
  });

  test("an API source goes to the first other API client, never itself", () => {
    expect(pickHandoffTarget(all, FAMILIES, "claude-api", null)).toBe("codex-api");
  });

  test("a stored default that is the source is skipped", () => {
    expect(pickHandoffTarget(all, FAMILIES, "claude-api", "claude-api")).toBe("codex-api");
  });

  test("no ready API client: the first target as before", () => {
    const consoles = roster(["claude", "codex", "kimi"]);
    expect(pickHandoffTarget(consoles, FAMILIES, "claude", null)).toBe("codex");
  });

  test("nothing can be a target", () => {
    expect(pickHandoffTarget(roster([]), FAMILIES, "claude", null)).toBeNull();
  });
});
