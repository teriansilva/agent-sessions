/**
 * #853 P4 — the SPA reads the engine roster, and keeps no list of its own.
 *
 * Two guarantees, each with a negative control that proves it can fail:
 *
 * - **The ratchet** (static): no engine-id string literal in `web/src` outside tests and fixtures.
 *   An accident guard, like the server's: it catches the `engine === "<id>"` / `new Set([...])`
 *   shape every roster regression so far has had (#454).
 * - **Conformance** (behavioural): a fixture EIGHTH engine installed in the roster is answered by
 *   every consumer from its own manifest data — badge, colour, name, handoff eligibility, id mode,
 *   repaint, id prefix, runtime — and every consumer falls back when it is removed.
 */
import { readdirSync, readFileSync, statSync } from "node:fs";
import { join, relative, resolve } from "node:path";
import { act, render, screen } from "@testing-library/react";
import { describe, expect, test } from "vitest";

import fixture from "../test/roster.fixture.json";
import type { EngineInfo } from "../types/api";
import { sessionLabel } from "../components/pulse/missionNow";
import { mintNewSessionId } from "../lib/newSession";
import { createScrollEraseStripper } from "../lib/scrollErase";
import {
  canBeHandoffTarget,
  engineBadge,
  engineColor,
  engineLabel,
  engineName,
  getRoster,
  idPrefix,
  isAgent,
  markRosterFailed,
  mintsOwnId,
  resetRoster,
  resolveDefault,
  subscribeRoster,
  rosterReady,
  runsInTerminal,
  setRoster,
  useEngineRoster,
  wipesOnRepaint,
} from "./engineRoster";

const SEVEN = (fixture.engines as EngineInfo[]).map((e) => e.id);

// --- the ratchet ---------------------------------------------------------------------------------

const SRC = resolve(process.cwd(), "src");

function files(dir: string): string[] {
  return readdirSync(dir).flatMap((n) => {
    const p = join(dir, n);
    return statSync(p).isDirectory() ? files(p) : [p];
  });
}

/** String literals equal to an engine id, outside comments. `'…'`, `"…"` and whole `` `…` ``. */
export function engineLiterals(source: string, ids: readonly string[]): string[] {
  const code = source
    .replace(/\/\*[\s\S]*?\*\//g, "")
    .split("\n")
    .map((l) => l.replace(/(^|[^:"'`])\/\/.*$/, "$1"))
    .join("\n");
  const hits: string[] = [];
  for (const m of code.matchAll(/(["'`])([a-z][a-z0-9-]*)\1/g)) {
    if (ids.includes(m[2])) hits.push(m[0]);
  }
  return hits;
}

describe("the web roster ratchet", () => {
  test("no engine-id literal in web/src outside tests and fixtures", () => {
    const found: string[] = [];
    for (const f of files(SRC)) {
      const rel = relative(SRC, f);
      if (!/\.(ts|tsx)$/.test(rel)) continue;
      if (/\.test\.(ts|tsx)$/.test(rel) || rel.startsWith("test/") || rel.includes("fixture"))
        continue;
      for (const hit of engineLiterals(readFileSync(f, "utf8"), SEVEN)) found.push(`${rel}: ${hit}`);
    }
    expect(found, "an engine is named in the SPA instead of read from the roster").toEqual([]);
  });

  test.each([
    'if (engine === "claude") {}',
    "const RECONCILE = new Set(['opencode', 'codex']);",
    "const label = `kimi`;",
  ])("NEGATIVE CONTROL: a planted literal turns it red — %s", (planted) => {
    expect(engineLiterals(planted, SEVEN).length).toBeGreaterThan(0);
  });

  test("comments are not code", () => {
    expect(engineLiterals('// the "claude" engine\n/* "codex" */ const x = 1;', SEVEN)).toEqual(
      [],
    );
  });
});

// --- conformance: a fixture eighth engine --------------------------------------------------------

const ZETA: EngineInfo = {
  id: "zeta",
  present: true,
  supports_new: true,
  supports_seed_start: false,
  seed_reason: "no seed-capable start yet",
  bin: "/fixture/bin/zeta",
  label: "Zeta Agent",
  kind: "agent",
  runtime: "pty",
  display: { name: "zeta", badge: "zt", accent: "magenta", id_prefix: "zt_", order: 70 },
  capabilities: {
    resume: true,
    new: true,
    archive: true,
    handoff_target: false, // declared DENIED: must be refused, not defaulted
    seed_start: false,
    orchestrator_input: false,
    raw_tty: true,
    owns_transcript: false,
  },
  session_id: { mint: "adopt" },
  models: [],
  usage: { source: "manual" },
  terminal: { repaint: "wipe" },
  status: "active",
  status_reason: null,
};

type Consumer = (id: string) => unknown;

/** What each consumer answers for `id` — exercised through the same functions the SPA calls. */
function consumers(id: string): Record<string, unknown> {
  const wipe = new TextEncoder().encode("\x1b[3Jx");
  return {
    badge: engineBadge(id),
    color: engineColor(id),
    name: engineName(id),
    label: engineLabel(id),
    agent: isAgent(id),
    handoffTarget: canBeHandoffTarget(id),
    mintsOwn: mintsOwnId(id),
    placeholder: (mintNewSessionId(id) ?? "").startsWith("new-"),
    repaint: wipesOnRepaint(id),
    strips: createScrollEraseStripper().strip(id, true, wipe).length < wipe.length,
    prefix: idPrefix(id),
    label4: sessionLabel(`${id}:zt_abcd1234`),
    terminal: runsInTerminal(id),
  };
}

const ZETA_ANSWERS = {
  badge: "zt",
  color: "var(--engine-magenta)",
  name: "zeta",
  label: "Zeta Agent",
  agent: true,
  handoffTarget: false,
  mintsOwn: true,
  placeholder: true,
  repaint: true,
  strips: true,
  prefix: "zt_",
  label4: "zeta · abcd",
  terminal: true,
};

const FALLBACK = {
  badge: "ze",
  color: "var(--engine-slate)",
  name: "zeta",
  label: "zeta",
  agent: false,
  handoffTarget: false,
  mintsOwn: undefined,
  placeholder: false,
  repaint: false,
  strips: false,
  prefix: "",
  label4: "zeta · zt_a",
  terminal: undefined,
};

describe("an eighth engine reaches every consumer through its manifest data", () => {
  test("added: every consumer answers from ITS data", () => {
    setRoster([...(fixture.engines as EngineInfo[]), ZETA]);
    expect(consumers("zeta")).toEqual(ZETA_ANSWERS);
    expect(getRoster().engines.map((e) => e.id)).toContain("zeta");
  });

  test("removed: every consumer falls back — renders, offers nothing", () => {
    setRoster([...(fixture.engines as EngineInfo[]), ZETA]);
    setRoster(fixture.engines as EngineInfo[]);
    expect(consumers("zeta")).toEqual(FALLBACK);
  });

  test("NEGATIVE CONTROL: a consumer with its OWN list is caught", () => {
    setRoster([...(fixture.engines as EngineInfo[]), ZETA]);
    const hardcoded: Consumer = (id) => ({ claude: "cc", codex: "cx" })[id] ?? id.slice(0, 2);
    expect(hardcoded("zeta")).not.toEqual(ZETA_ANSWERS.badge);
  });

  test("NEGATIVE CONTROL: a consumer that snapshotted the roster before it changed is caught", () => {
    const snapshot = new Map(getRoster().engines.map((e) => [e.id, e])); // taken too early
    setRoster([...(fixture.engines as EngineInfo[]), ZETA]);
    const stale: Consumer = (id) => snapshot.get(id)?.display.badge ?? id.slice(0, 2);
    expect(stale("zeta")).not.toEqual(ZETA_ANSWERS.badge);
  });

  test("the seven answer exactly what the old client tables did", () => {
    // Recorded from the pre-P4 format.ts ladders / RECONCILE_ENGINES / WIPE_REPAINT_ENGINES.
    const old = {
      badge: { claude: "cc", opencode: "oc", codex: "cx", gemini: "gm", antigravity: "ag", kimi: "ki", shell: "sh" },
      reconcile: ["opencode", "codex", "antigravity", "kimi"],
      wipe: ["codex", "kimi"],
      handoffSource: SEVEN.filter((e) => e !== "shell"),
    } as const;
    for (const e of SEVEN) {
      expect(engineBadge(e), e).toBe(old.badge[e as keyof typeof old.badge]);
      expect(engineName(e), e).toBe(e);
      expect(mintsOwnId(e), e).toBe((old.reconcile as readonly string[]).includes(e));
      expect(wipesOnRepaint(e), e).toBe((old.wipe as readonly string[]).includes(e));
      expect(isAgent(e), e).toBe(old.handoffSource.includes(e));
      expect(engineColor(e), e).toMatch(/^var\(--engine-[a-z]+\)$/);
    }
  });
});

// --- readiness: appearance degrades, eligibility waits ---------------------------------------------

describe("before the roster loads", () => {
  test("appearance falls back and nothing is minted on a guess", () => {
    resetRoster();
    expect(rosterReady()).toBe(false);
    expect(engineBadge("codex")).toBe("co");
    expect(mintNewSessionId("codex")).toBeNull();
    expect(canBeHandoffTarget("claude")).toBe(false);
  });

  test("a failed refresh KEEPS the last good roster; an empty success is distinct", () => {
    setRoster(fixture.engines as EngineInfo[]);
    markRosterFailed();
    expect(getRoster().status).toBe("failed");
    expect(getRoster().loaded).toBe(true);
    expect(engineBadge("claude")).toBe("cc");
    setRoster([]);
    expect(getRoster()).toMatchObject({ status: "ready", loaded: true, engines: [] });
  });

  test("a row with NO engine renders blank instead of throwing (mission session records)", () => {
    const none = undefined as unknown as string;
    for (const loaded of [false, true]) {
      if (loaded) setRoster(fixture.engines as EngineInfo[]);
      else resetRoster();
      expect(engineBadge(none)).toBe("");
      expect(engineName(none)).toBe("");
      expect(engineLabel(none)).toBe("");
    }
  });

  test("a retry that fails AGAIN publishes nothing — subscribers' memos stay put (#936)", () => {
    resetRoster();
    markRosterFailed();
    const first = getRoster();
    let calls = 0;
    const off = subscribeRoster(() => calls++);
    markRosterFailed();
    markRosterFailed();
    off();
    expect(calls).toBe(0);
    expect(getRoster()).toBe(first);
  });

  test("a subscribed component re-renders when the roster lands", () => {
    resetRoster();
    function Chip() {
      useEngineRoster();
      return <span>{engineBadge("claude")}</span>;
    }
    render(<Chip />);
    expect(screen.getByText("cl")).toBeInTheDocument();
    act(() => setRoster(fixture.engines as EngineInfo[]));
    expect(screen.getByText("cc")).toBeInTheDocument();
  });
});

// --- the eligible-default resolver -------------------------------------------------------------

describe("resolveDefault", () => {
  const all = () => getRoster().engines;

  test("an eligible stored default wins", () => {
    expect(resolveDefault(all(), "codex", "new")).toEqual({
      engine: "codex",
      unavailableDefault: null,
    });
  });

  test("an ABSENT default falls back visibly to the first ELIGIBLE engine, unchanged", () => {
    const engines = all().map((e) => (e.id === "gemini" ? { ...e, present: false } : e));
    expect(resolveDefault(engines, "gemini", "new")).toEqual({
      engine: "claude",
      unavailableDefault: "gemini",
    });
  });

  test("shell may be the default for new sessions but is never a handoff fallback", () => {
    expect(resolveDefault(all(), "shell", "new").engine).toBe("shell");
    const h = resolveDefault(all(), "shell", "handoff");
    expect(h.engine).not.toBe("shell");
    expect(h.unavailableDefault).toBe("shell");
  });

  test("a retiring engine is never a fallback", () => {
    const engines = all().map((e) => (e.id === "claude" ? { ...e, status: "retiring" } : e));
    expect(resolveDefault(engines, null, "new").engine).not.toBe("claude");
    expect(resolveDefault(engines, "claude", "new").unavailableDefault).toBe("claude");
  });

  test("nothing eligible is null, never a guess", () => {
    expect(resolveDefault([], "claude", "new")).toEqual({
      engine: null,
      unavailableDefault: "claude",
    });
  });
});

describe("a roster row without the manifest fields degrades, never throws (#853 P4)", () => {
  test("an old-shape row renders with the fallback and can launch nothing; junk rows are dropped", () => {
    setRoster([
      { id: "zeta", present: true } as unknown as EngineInfo,
      "claude" as unknown as EngineInfo,
      null as unknown as EngineInfo,
    ]);
    expect(getRoster().engines.map((e) => e.id)).toEqual(["zeta"]);
    expect(engineBadge("zeta")).toBe("ze");
    expect(engineColor("zeta")).toBe("var(--engine-slate)");
    expect(engineName("zeta")).toBe("zeta");
    expect(isAgent("zeta")).toBe(false);
    expect(canBeHandoffTarget("zeta")).toBe(false);
    expect(mintsOwnId("zeta")).toBeUndefined(); // unknown id mode: launches wait
    expect(mintNewSessionId("zeta")).toBeNull();
    expect(runsInTerminal("zeta")).toBeUndefined();
    expect(wipesOnRepaint("zeta")).toBe(false);
  });
});
