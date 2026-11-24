/** What is left in `lib/automations` of the retired review loop (#2202, removed in #2222): the
 *  specialist picker it used (kept for an automation that starts a specialist), and the words for
 *  the outcomes only its runs were written with, so an old run still reads. Moved here from
 *  `automationsReviewLoop.test.ts`. Engine ids come from the roster fixture, never a literal. */
import { afterEach, describe, expect, test } from "vitest";

import { setRoster } from "../app/engineRoster";
import fixture from "../test/roster.fixture.json";
import type { EngineInfo } from "../types/api";
import type { Specialist } from "../types/specialists";
import { outcomeWord, reviewerOptions } from "./automations";

const ENGINES = fixture.engines as EngineInfo[];
const API = ENGINES.find((e) => e.runtime === "api" && (e.status ?? "active") === "active")!;
const API2 = ENGINES.find((e) => e.runtime === "api" && e.id !== API.id)!;
const PTY = ENGINES.find((e) => e.runtime === "pty")!;
const CHAT = ENGINES.find((e) => e.runtime === "chat");

afterEach(() => setRoster(ENGINES));

describe("an old review-loop run still reads", () => {
  test("its outcomes: ignored is grey, capped amber, undelivered red", () => {
    expect(outcomeWord("ignored")).toEqual({ word: "Ignored", tone: "idle" });
    expect(outcomeWord("capped")).toEqual({ word: "Capped", tone: "degraded" });
    expect(outcomeWord("undelivered")).toEqual({ word: "Not delivered", tone: "down" });
  });
});

describe("the specialist picker", () => {
  const spec = (id: string, engine: string, version: number, archived = false): Specialist =>
    ({
      id,
      revision: 1,
      archived,
      current: { version, title: id.toUpperCase(), summary: "", skills: [], engine, model: "m1", effort: null, capacity: 1, created_at: 1 },
      grade: null,
      live: 0,
      skills_missing: [],
      skills_outdated: [],
    }) as Specialist;

  test("only API-client specialists, never archived, pinned at their CURRENT version", () => {
    setRoster(ENGINES);
    const list = [
      spec("api-one", API.id, 3),
      spec("term", PTY.id, 1),
      spec("gone", API.id, 2, true),
      ...(CHAT ? [spec("chat", CHAT.id, 1)] : []),
    ];
    const out = reviewerOptions(list, [], (id) => `label:${id}`);
    expect(out).toEqual([{ id: "api-one", version: 3, label: `API-ONE v3 · label:${API.id} · m1`, blocked: "" }]);
  });

  test("one the server can't start unattended is offered disabled, with its reason", () => {
    setRoster(ENGINES);
    const out = reviewerOptions(
      [spec("a", API.id, 1), spec("b", API2.id, 1)],
      [
        { id: API.id, ok: true, reason: "" },
        { id: API2.id, ok: false, reason: "not mission-ready" },
      ],
      (id) => id,
    );
    expect(out.map((o) => [o.id, o.blocked])).toEqual([
      ["a", ""],
      ["b", "not mission-ready"],
    ]);
  });

  test("no pool, or no roster yet: nothing is offered on a guess", () => {
    setRoster(ENGINES);
    expect(reviewerOptions(null, [], (id) => id)).toEqual([]);
    setRoster([]);
    expect(reviewerOptions([spec("a", API.id, 1)], [], (id) => id)).toEqual([]);
  });
});
