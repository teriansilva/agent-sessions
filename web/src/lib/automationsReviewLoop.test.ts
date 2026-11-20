/** #2202 — the Forgejo review loop's pure half: the webhook trigger and the review-loop action come
 *  only together, the form round-trips to exactly the body the server's strict validator accepts,
 *  the reviewer picker offers only API-client specialists at their current version, Run now is
 *  refused with its reason, and a run's leg and round read from the server's recorded inputs.
 *  Engine ids come from the roster fixture, never a literal. */
import { afterEach, describe, expect, test } from "vitest";

import { setRoster } from "../app/engineRoster";
import fixture from "../test/roster.fixture.json";
import type { EngineInfo } from "../types/api";
import type { Automation, AutomationRun, ReviewLoopAction } from "../types/automations";
import type { Specialist } from "../types/specialists";
import {
  actionLabel,
  blankForm,
  bodyFromForm,
  formFromAutomation,
  formProblems,
  loopFacts,
  nextRunWords,
  outcomeWord,
  reviewerOptions,
  runNowBlock,
  runTriggerWords,
  shortRev,
  triggerWords,
  withAction,
  withTrigger,
} from "./automations";

const ENGINES = fixture.engines as EngineInfo[];
const API = ENGINES.find((e) => e.runtime === "api" && (e.status ?? "active") === "active")!;
const API2 = ENGINES.find((e) => e.runtime === "api" && e.id !== API.id)!;
const PTY = ENGINES.find((e) => e.runtime === "pty")!;
const CHAT = ENGINES.find((e) => e.runtime === "chat");

afterEach(() => setRoster(ENGINES));

const LOOP: ReviewLoopAction = {
  kind: "forge_review_loop",
  project_id: "p1",
  repo: "example-org/shop",
  reviewer: { specialist_id: "shop-reviewer", version: 3 },
  reviewer_login: "shop-reviewer-bot",
  legs: { issues: true, pulls: false },
  max_rounds: { issue: 2, pr: 7 },
  return_to_origin: false,
};

function loopAutomation(over: Partial<Automation> = {}): Automation {
  return {
    id: "rv1",
    name: "Review loop · example-org/shop",
    trigger: { kind: "webhook", scheme: "forgejo" },
    action: LOOP,
    policy: { concurrency: "allow", max_concurrent: 3, max_runs_per_day: 48, pause_after_failures: 3, expires_at: null },
    revision: 2,
    state: "enabled",
    enabled: true,
    paused: false,
    paused_reason: "",
    needs_reapproval: false,
    reapproval_reason: "",
    consented_at: 1_700_000_000,
    consented_scope: {},
    scope: {},
    scope_lines: [],
    scope_digest: "d",
    consecutive_failures: 0,
    next_run: null,
    last_run: null,
    stats: { ok: 0, failed: 0, skipped: 0, pending: 0, runs: 0, success_rate: null },
    strip: [],
    webhook: null,
    created_at: 1,
    updated_at: 1,
    ...over,
  };
}

function run(over: Partial<AutomationRun>): AutomationRun {
  return {
    id: "r1",
    automation_id: "rv1",
    trigger: "webhook",
    slot: "review:pr:41:9c1e2f0",
    catch_up: false,
    covered: 1,
    state: "done",
    outcome: "ok",
    result_class: "ok",
    reason: "",
    mission_id: null,
    session_key: null,
    created_at: 1,
    finished_at: 2,
    fire_at: null,
    inputs: {},
    scope: null,
    ...over,
  };
}

describe("the trigger and the action come together (#2202)", () => {
  test("choosing the webhook trigger chooses the review loop, and runs deliveries side by side", () => {
    const f = withTrigger(blankForm("UTC"), "webhook", 3);
    expect(f.triggerKind).toBe("webhook");
    expect(f.actionKind).toBe("forge_review_loop");
    expect(f.concurrency).toBe("allow");
    expect(f.maxConcurrent).toBe(3);
  });

  test("choosing the review loop action chooses the webhook trigger", () => {
    const f = withAction(blankForm("UTC"), "forge_review_loop", 3);
    expect(f.triggerKind).toBe("webhook");
    expect(f.actionKind).toBe("forge_review_loop");
  });

  test("leaving either side leaves both, back to the blank form's defaults", () => {
    const loop = withAction(blankForm("UTC"), "forge_review_loop", 3);
    const byTrigger = withTrigger(loop, "manual", 3);
    expect(byTrigger.triggerKind).toBe("manual");
    expect(byTrigger.actionKind).toBe("start_mission");
    expect(byTrigger.concurrency).toBe("skip");
    const byAction = withAction(loop, "send_to_session", 3);
    expect(byAction.actionKind).toBe("send_to_session");
    expect(byAction.triggerKind).toBe("schedule");
  });

  test("other choices are untouched by the pairing", () => {
    const f = { ...blankForm("UTC"), concurrency: "allow" as const, maxConcurrent: 2 };
    expect(withTrigger(f, "once", 3)).toEqual({ ...f, triggerKind: "once" });
    expect(withAction(f, "start_session", 3)).toEqual({ ...f, actionKind: "start_session" });
    expect(withTrigger(f, f.triggerKind, 3)).toBe(f);
  });
});

describe("form ↔ config (#2202)", () => {
  test("a stored review loop round-trips to the same trigger and action", () => {
    const a = loopAutomation();
    const body = bodyFromForm(formFromAutomation(a, "UTC"));
    expect(body.trigger).toEqual({ kind: "webhook", scheme: "forgejo" });
    expect(body.action).toEqual(LOOP);
    expect(body.policy.concurrency).toBe("allow");
    expect(body.policy.max_concurrent).toBe(3);
  });

  test("a new loop sends the defaults: both legs, 4 PR / 5 issue reviews, verdicts returned", () => {
    let f = withAction(blankForm("UTC"), "forge_review_loop", 3);
    f = {
      ...f,
      name: "Loop",
      projectId: "p1",
      repo: " example-org/shop ",
      reviewerId: "shop-reviewer",
      reviewerVersion: 3,
      reviewerLogin: "shop-reviewer-bot",
    };
    expect(formProblems(f)).toEqual([]);
    expect(bodyFromForm(f).action).toEqual({
      kind: "forge_review_loop",
      project_id: "p1",
      repo: "example-org/shop",
      reviewer: { specialist_id: "shop-reviewer", version: 3 },
      reviewer_login: "shop-reviewer-bot",
      legs: { issues: true, pulls: true },
      max_rounds: { issue: 5, pr: 4 },
      return_to_origin: true,
    });
    // A review loop has no message: nothing about one is ever sent.
    expect(JSON.stringify(bodyFromForm(f).action)).not.toMatch(/message|instruction/);
  });

  test("the problems name what the server would refuse", () => {
    const f = { ...withAction(blankForm("UTC"), "forge_review_loop", 3), name: "x" };
    expect(formProblems(f)).toEqual([
      "Choose a project",
      "Name the repository as owner/name",
      "Choose a reviewer specialist",
      "Name the reviewer’s forge account",
    ]);
    const g = {
      ...f,
      projectId: "p1",
      repo: "no-slash",
      reviewerId: "s",
      reviewerVersion: 1,
      reviewerLogin: "bad login",
      reviewIssues: false,
      reviewPulls: false,
      roundsPr: 11,
    };
    expect(formProblems(g)).toEqual([
      "Name the repository as owner/name",
      "Name the reviewer’s forge account",
      "Review issues, pull requests or both",
      "Reviews per item: 1 to 10",
    ]);
  });
});

describe("words (#2202)", () => {
  test("trigger, action, next run", () => {
    expect(triggerWords({ kind: "webhook", scheme: "forgejo" }).label).toBe("Forgejo webhook");
    expect(actionLabel("forge_review_loop")).toBe("Review loop");
    expect(nextRunWords(loopAutomation(), 0)).toEqual({ main: "On delivery", sub: "when Forgejo sends an event" });
  });

  test("Run now is refused for a review loop, with its reason", () => {
    expect(runNowBlock(loopAutomation(), true)).toBe("It runs only when its webhook delivers an event");
    expect(runNowBlock(loopAutomation(), false)).toBe("It runs only when its webhook delivers an event");
  });

  test("the new outcomes: ignored is grey, capped amber, undelivered red", () => {
    expect(outcomeWord("ignored")).toEqual({ word: "Ignored", tone: "idle" });
    expect(outcomeWord("capped")).toEqual({ word: "Capped", tone: "degraded" });
    expect(outcomeWord("undelivered")).toEqual({ word: "Not delivered", tone: "down" });
  });
});

describe("a run's leg and round (#2202)", () => {
  test("a review run: leg review, review k / N, its item at its revision", () => {
    const r = run({
      outcome: "ok",
      inputs: { item: "pr#41", revision: "9c1e2f0aa11bb22cc33dd44ee55ff66778899aab", round: 2, cap: 4, record: "x", event: "pull_request", delivery: "8d2f", headers: "recorded, not trusted" },
    });
    expect(loopFacts(r)).toEqual({
      leg: "review",
      round: "review 2 / 4",
      item: "pr#41",
      revision: "9c1e2f0aa11bb22cc33dd44ee55ff66778899aab",
      verdict: "",
      event: "pull_request",
      delivery: "8d2f",
    });
    expect(runTriggerWords(r)).toBe("pr#41 at 9c1e2f0");
  });

  test("a capped run is the review leg, and counts its reviews", () => {
    const r = run({ outcome: "capped", inputs: { item: "pr#37", revision: "abc1234", reviews: 4, cap: 4 } });
    expect(loopFacts(r)!.leg).toBe("review");
    expect(loopFacts(r)!.round).toBe("4 / 4 reviews");
  });

  test("a return run, accepted or not, is the return leg", () => {
    const sent = run({ outcome: "undelivered", inputs: { leg: "return", item: "issue#39", round: 1, verdict: "adjust" } });
    expect(loopFacts(sent)).toMatchObject({ leg: "return", round: "review 1", verdict: "adjust" });
    const stale = run({ outcome: "ignored", inputs: { leg: "return", record: "r" } });
    expect(loopFacts(stale)).toMatchObject({ leg: "return", round: null });
    // A malformed marker never reached the acceptance, so it carries no leg — its event says it.
    const bad = run({ outcome: "ignored", inputs: { kind: "pr_review", item: "pr#41" } });
    expect(loopFacts(bad)!.leg).toBe("return");
  });

  test("not a webhook run → no loop facts; a delivery with no item reads as one", () => {
    expect(loopFacts(run({ trigger: "schedule" }))).toBeNull();
    expect(runTriggerWords(run({ inputs: { event: "push" } }))).toBe("Forgejo delivery");
  });

  test("short revisions: a sha shortens, anything else is kept", () => {
    expect(shortRev("9c1e2f0aa11bb22cc33dd44ee55ff66778899aab")).toBe("9c1e2f0");
    expect(shortRev("0123456789abcdef")).toBe("0123456");
    expect(shortRev("v1")).toBe("v1");
  });
});

describe("the reviewer picker (#2202)", () => {
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
