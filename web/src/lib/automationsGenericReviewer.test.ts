/** "Run as a specialist" and "Run once per" in the wizard's form (#2222): what is sent, what is
 *  read back, and the checks that mirror the server's. Engine ids come from the roster fixture. */
import { afterEach, describe, expect, test } from "vitest";

import { setRoster } from "../app/engineRoster";
import fixture from "../test/roster.fixture.json";
import type { EngineInfo } from "../types/api";
import type { Automation } from "../types/automations";
import {
  ONCE_PER_MAX,
  blankForm,
  formAction,
  formFromAutomation,
  formTrigger,
  oncePerProblems,
  specialistChoices,
  stepProblems,
  withEngine,
  withTrigger,
  type EditorForm,
  type ReviewerOption,
} from "./automations";

const ENGINES = fixture.engines as EngineInfo[];
const API = ENGINES.find((e) => e.runtime === "api" && (e.status ?? "active") === "active")!;
const API2 = ENGINES.find((e) => e.runtime === "api" && e.id !== API.id)!;
const REVISION = ["repository.full_name", "pull_request.number", "pull_request.head.sha"];

afterEach(() => setRoster(ENGINES));

const opt = (id: string, engine: string, version = 3): ReviewerOption => ({
  id,
  engine,
  version,
  label: `${id} v${version}`,
  blocked: "",
});

function sessionForm(over: Partial<EditorForm> = {}): EditorForm {
  return {
    ...blankForm("UTC"),
    triggerKind: "webhook",
    webhookId: "wh_1",
    actionKind: "start_session",
    engine: API.id,
    folder: "/w/shop",
    messageMode: "text",
    text: "review it",
    ...over,
  };
}

describe("what the form sends", () => {
  test("neither key without a choice: a stored automation keeps its exact shape", () => {
    const f = sessionForm();
    expect(formTrigger(f)).toEqual({ kind: "webhook", webhook_id: "wh_1" });
    expect(formAction(f)).not.toHaveProperty("specialist");
  });

  test("the specialist pin and the once-per fields when chosen", () => {
    const f = sessionForm({ specialistId: "org-reviewer", specialistVersion: 3, oncePer: REVISION });
    expect(formTrigger(f)).toEqual({ kind: "webhook", webhook_id: "wh_1", once_per: REVISION });
    expect(formAction(f)).toMatchObject({
      kind: "start_session",
      specialist: { specialist_id: "org-reviewer", version: 3 },
      bypass: false,
    });
  });

  test("a stored automation reads back into the same form", () => {
    const f = sessionForm({ specialistId: "org-reviewer", specialistVersion: 3, oncePer: REVISION });
    const a = {
      name: "Review PRs",
      trigger: formTrigger(f),
      action: formAction(f),
      policy: { concurrency: "skip", max_concurrent: 2, max_runs_per_day: 48, pause_after_failures: 3, expires_at: null },
    } as unknown as Automation;
    const back = formFromAutomation(a, "UTC");
    expect(back.oncePer).toEqual(REVISION);
    expect([back.specialistId, back.specialistVersion]).toEqual(["org-reviewer", 3]);
  });

  test("leaving the webhook trigger drops the once-per fields", () => {
    const f = withTrigger(sessionForm({ oncePer: REVISION }), "manual");
    expect(f.oncePer).toEqual([]);
  });
});

describe("the checks mirror the server's", () => {
  test("once-per: a plain field, never a list, no repeats, at most four", () => {
    expect(oncePerProblems(REVISION)).toEqual([]);
    expect(oncePerProblems(["labels[].name"])[0]).toMatch(/not a list/);
    expect(oncePerProblems(["a", "a"])[0]).toMatch(/already chosen/);
    expect(oncePerProblems(["@x"])[0]).toMatch(/pick a field/);
    expect(oncePerProblems(["a", "b", "c", "d", "e"])[0]).toMatch(`at most ${ONCE_PER_MAX}`);
    expect(stepProblems(sessionForm({ oncePer: ["labels[].name"] })).events).toHaveLength(1);
  });

  test("a specialist's session may skip permission prompts and close when done (#2405)", () => {
    const f = sessionForm({
      specialistId: "org-reviewer",
      specialistVersion: 3,
      bypass: true,
      closeWhenDone: true,
    });
    expect(stepProblems(f).what).toEqual([]);
  });
});

describe("choosing the agent", () => {
  const options = [opt("org-reviewer", API.id), opt("docs-reviewer", API2.id)];

  test("offers only the specialists on that agent", () => {
    expect(specialistChoices(options, API.id).map((o) => o.id)).toEqual(["org-reviewer"]);
    expect(specialistChoices(options, "")).toEqual([]);
  });

  test("keeps a specialist on the new agent, clears one on another", () => {
    const f = sessionForm({ engine: API2.id, specialistId: "org-reviewer", specialistVersion: 3 });
    const kept = withEngine(f, API.id, options);
    expect([kept.engine, kept.specialistId]).toEqual([API.id, "org-reviewer"]);
    const cleared = withEngine(kept, API2.id, options);
    expect([cleared.specialistId, cleared.specialistVersion]).toEqual(["", null]);
  });
});
