/** #2221 P2b — the automation wizard's pure half: which events (`trigger.when`), values from the
 *  delivery (`message.from_delivery`), the pairing the server allows, each step's problems, and what
 *  the last delivery's shape says about them. Shapes as `webhook_events` validates them. */
import { afterEach, describe, expect, test } from "vitest";

import { setRoster } from "../app/engineRoster";
import fixture from "../test/roster.fixture.json";
import type { EngineInfo, Template } from "../types/api";
import type { Automation } from "../types/automations";
import type { WebhookPreset } from "../types/webhooks";
import {
  blankForm,
  bodyFromForm,
  chosenPreset,
  conditionBody,
  conditionDraft,
  deliveryChecks,
  eventsSkipped,
  eventsWords,
  formFromAutomation,
  formProblems,
  legacyTrigger,
  limitsWords,
  mappableFields,
  stepProblems,
  withFieldSource,
  withAction,
  withPreset,
  withTrigger,
  type EditorForm,
} from "./automations";

const ENGINES = fixture.engines as EngineInfo[];
const API = ENGINES.find((e) => e.runtime === "api")!;
const CONSOLE = ENGINES.find((e) => e.runtime === "pty")!;
const HOOK = "0a1b2c3d4e5f6a7b";

afterEach(() => setRoster(ENGINES));

const PR_PRESET: WebhookPreset = {
  id: "pr_opened_or_pushed",
  label: "PR opened or pushed (drafts skipped)",
  when: [
    { path: "action", op: "one_of", value: ["opened", "reopened", "synchronized"] },
    { path: "pull_request.draft", op: "is_false" },
    { path: "review", op: "missing" },
  ],
};

const SHAPE = [
  { path: "action", type: "text" },
  { path: "number", type: "number" },
  { path: "pull_request.title", type: "text" },
  { path: "pull_request.draft", type: "yes/no" },
  { path: "pull_request.merged_by", type: "empty" },
  { path: "labels[].name", type: "text" },
];

/** A webhook-started session with a template, everything else filled in. */
function hookForm(over: Partial<EditorForm> = {}): EditorForm {
  return {
    ...withTrigger(blankForm("UTC"), "webhook", 3),
    name: "Review every PR",
    webhookId: HOOK,
    engine: API.id,
    folder: "/repo",
    messageMode: "template",
    templateId: "t1",
    ...over,
  };
}

describe("which events: conditions (#2221 P2)", () => {
  test("a preset is copied into the trigger; no conditions omits the key", () => {
    const f = withPreset(hookForm(), PR_PRESET);
    expect(bodyFromForm(f).trigger).toEqual({ kind: "webhook", webhook_id: HOOK, when: PR_PRESET.when });
    expect(chosenPreset(f, [PR_PRESET])).toBe(PR_PRESET.id);
    const every = withPreset(f, null);
    expect(bodyFromForm(every).trigger).toEqual({ kind: "webhook", webhook_id: HOOK });
    expect(chosenPreset(every, [PR_PRESET])).toBe("");
  });

  test("an edited preset is the operator's own conditions", () => {
    const f = withPreset(hookForm(), PR_PRESET);
    const g = { ...f, when: f.when.slice(0, 2) };
    expect(chosenPreset(g, [PR_PRESET])).toBeNull();
    expect(eventsWords(g, [PR_PRESET])).toBe(
      "action is one of opened, reopened, synchronized · pull_request.draft is no",
    );
    expect(eventsWords(f, [PR_PRESET])).toBe(PR_PRESET.label);
  });

  test("values are typed: a number, yes/no, a list; a value-less operator sends no value", () => {
    expect(conditionBody({ path: "number", op: "is", values: [{ text: "42", type: "number" }] })).toEqual({
      path: "number",
      op: "is",
      value: 42,
    });
    expect(conditionBody({ path: "pull_request.draft", op: "is", values: [{ text: "no", type: "yes_no" }] })).toEqual({
      path: "pull_request.draft",
      op: "is",
      value: false,
    });
    // A list: each element typed by its OWN type, its text kept as typed (commas, spaces and all).
    expect(
      conditionBody({
        path: "action",
        op: "one_of",
        values: [
          { text: "opened, synchronized", type: "text" },
          { text: "4", type: "number" },
          { text: "no", type: "yes_no" },
          { text: " spaced ", type: "text" },
        ],
      }),
    ).toEqual({ path: "action", op: "one_of", value: ["opened, synchronized", 4, false, " spaced "] });
    expect(conditionBody({ path: "review", op: "missing", values: [{ text: "left over", type: "text" }] })).toEqual({
      path: "review",
      op: "missing",
    });
  });

  test("a stored condition round-trips through the form unchanged", () => {
    for (const c of [
      ...PR_PRESET.when,
      { path: "number", op: "is_not" as const, value: 7 },
      { path: "labels[].name", op: "contains" as const, value: "needs-review" },
      { path: "pull_request.draft", op: "is" as const, value: true },
    ])
      expect(conditionBody(conditionDraft(c))).toEqual(c);
  });

  test("the events step's problems: a path, a value, its type, at most 8", () => {
    const f = hookForm({
      when: [
        { path: "pull request", op: "exists", values: [{ text: "", type: "text" }] },
        { path: "action", op: "is", values: [{ text: "", type: "text", unset: true }] },
        { path: "number", op: "is", values: [{ text: "four", type: "number" }] },
        { path: "pull_request.draft", op: "is", values: [{ text: "maybe", type: "yes_no" }] },
      ],
    });
    expect(stepProblems(f).events).toEqual([
      "Condition 1: pick a field of the delivery (like pull_request.draft)",
      "Condition 2: give it a value",
      "Condition 3: “four” is not a number; give a number",
      "Condition 4: “maybe” is not yes or no; answer yes or no",
    ]);
    const many = hookForm({
      when: Array.from({ length: 9 }, () => ({ path: "action", op: "exists" as const, values: [{ text: "", type: "text" as const }] })),
    });
    expect(stepProblems(many).events).toEqual(["At most 8 conditions"]);
  });

  test("a review loop takes no conditions: none are sent, and none hold its step", () => {
    const f = {
      ...hookForm({ when: [{ path: "", op: "is", values: [{ text: "", type: "text" }] }] }),
      actionKind: "forge_review_loop" as const,
    };
    expect(stepProblems(f).events).toEqual([]);
    expect(bodyFromForm(f).trigger).toEqual({ kind: "webhook", webhook_id: HOOK });
  });

  test("a schedule or Run now skips step 2, and never sends conditions", () => {
    expect(eventsSkipped(blankForm("UTC"))).toBe(true);
    expect(eventsSkipped({ ...blankForm("UTC"), triggerKind: "manual" })).toBe(true);
    expect(eventsSkipped(hookForm())).toBe(false);
  });
});

describe("values from the delivery (#2221 P2)", () => {
  const TEMPLATE = {
    id: "t1",
    name: "Review a PR",
    fields: [
      { name: "pr_number", label: "", default: "", required: true, source: "typed", kind: "text" },
      { name: "reviewer", label: "", default: "", required: false, source: "typed", kind: "text" },
      { name: "token", label: "", default: "", required: false, source: "typed", kind: "secret" },
      { name: "team", label: "", default: "", required: false, source: "library", kind: "text" },
    ],
  } as unknown as Template;

  test("only the template's own typed fields can be mapped — never a library field or a secret", () => {
    expect(mappableFields(TEMPLATE)).toEqual(["pr_number", "reviewer"]);
    expect(mappableFields(null)).toEqual([]);
  });

  test("a mapped field is never also fixed; choices are sent for a choice only", () => {
    let f = hookForm({ values: { pr_number: "12", reviewer: "bot" } });
    f = withFieldSource(f, "pr_number", "delivery");
    f = { ...f, fromDelivery: { pr_number: { path: "number", type: "number", choices: ["a", "b"] } } };
    f = withFieldSource(f, "reviewer", "delivery");
    f = {
      ...f,
      fromDelivery: { ...f.fromDelivery, reviewer: { path: "action", type: "choice", choices: ["opened", "synchronized"] } },
    };
    const action = bodyFromForm(f).action as { message: unknown };
    expect(action.message).toEqual({
      template_id: "t1",
      values: {},
      from_delivery: {
        pr_number: { path: "number", type: "number" },
        reviewer: { path: "action", type: "choice", choices: ["opened", "synchronized"] },
      },
    });
    // Back to a fixed value: the mapping goes, the field is typed again.
    const g = withFieldSource(f, "reviewer", "fixed");
    expect(g.fromDelivery).not.toHaveProperty("reviewer");
  });

  test("no mapping, no key; a schedule never sends one even if the form held it", () => {
    expect(bodyFromForm(hookForm({ values: { reviewer: "bot" } })).action).toMatchObject({
      message: { template_id: "t1", values: { reviewer: "bot" } },
    });
    expect(JSON.stringify(bodyFromForm(hookForm()).action)).not.toMatch(/from_delivery/);
    const sched = {
      ...hookForm({ fromDelivery: { pr_number: { path: "number", type: "number", choices: [] } } }),
      triggerKind: "schedule" as const,
    };
    expect(JSON.stringify(bodyFromForm(sched))).not.toMatch(/from_delivery|when/);
  });

  test("a stored mapping round-trips, choices sorted as the server stores them", () => {
    const a = {
      id: "a1",
      name: "x",
      trigger: { kind: "webhook", webhook_id: HOOK, when: PR_PRESET.when },
      action: {
        kind: "start_session",
        engine: API.id,
        model: null,
        folder: "/repo",
        bypass: false,
        message: {
          template_id: "t1",
          values: { reviewer: "bot" },
          from_delivery: { pr_number: { path: "action", type: "choice", choices: ["opened", "synchronized"] } },
        },
      },
      policy: null,
    } as unknown as Automation;
    const f = formFromAutomation(a, "UTC");
    expect(f.fromDelivery.pr_number).toEqual({ path: "action", type: "choice", choices: ["opened", "synchronized"] });
    const body = bodyFromForm(f);
    expect(body.trigger).toEqual(a.trigger);
    expect(body.action).toEqual(a.action);
  });

  test("the what step's problems: a field, not a list, a choice's values", () => {
    const f = hookForm({
      fromDelivery: {
        a: { path: "", type: "text", choices: [] },
        b: { path: "labels[].name", type: "text", choices: [] },
        c: { path: "action", type: "choice", choices: ["", " "] },
        d: { path: "action", type: "choice", choices: ["ok", "not<ok>"] },
      },
    });
    expect(stepProblems(f).what).toEqual([
      "a: pick a field of the delivery",
      "b: a value comes from one field, not a list",
      "c: list the allowed values, none empty",
      "d: each allowed value is a short plain text (letters, digits, spaces and _.,:;/#@()+-)",
    ]);
  });
});

describe("an edit that only renames saves exactly what it loaded (Hermes on #2241)", () => {
  /** A stored automation, loaded into the form, renamed, and built back into a body. */
  function renamed(trigger: unknown, message: unknown) {
    const a = {
      id: "a1",
      name: "before",
      trigger,
      action: { kind: "start_session", engine: API.id, model: null, folder: "/repo", bypass: false, message },
      policy: { concurrency: "skip", max_concurrent: 1, max_runs_per_day: 48, pause_after_failures: 3, expires_at: null },
    } as unknown as Automation;
    const f = { ...formFromAutomation(a, "UTC"), name: "after" };
    return bodyFromForm(f);
  }
  const msg = { template_id: "t1", values: {} };

  for (const [what, value] of [
    ["a string with a comma", ["ops, urgent"]],
    ["strings with spaces, kept as typed", [" ops ", "two  words", "  "]],
    ["a mixed number, string and yes/no list", [1, "2", true, "no", 0, false, "1.50", -3.25]],
    ["strings that read like numbers and yes/no", ["7", "yes", "true", "0"]],
  ] as const)
    test(`one_of: ${what}`, () => {
      const trigger = { kind: "webhook", webhook_id: HOOK, when: [{ path: "labels[].name", op: "one_of", value }] };
      expect(renamed(trigger, msg).trigger).toEqual(trigger);
    });

  test("single values keep their own type, and contains keeps its text", () => {
    const trigger = {
      kind: "webhook",
      webhook_id: HOOK,
      when: [
        { path: "number", op: "is", value: "12" },
        { path: "number", op: "is_not", value: 12 },
        { path: "pull_request.draft", op: "is", value: false },
        { path: "pull_request.title", op: "contains", value: "fix, then ship " },
        { path: "review", op: "missing" },
      ],
    };
    expect(renamed(trigger, msg).trigger).toEqual(trigger);
  });

  for (const [op, value] of [
    ["is", ""],
    ["is_not", ""],
    ["one_of", ["", "opened"]],
  ] as const)
    test(`an explicitly empty text is a value: ${op} ${JSON.stringify(value)} passes and saves back`, () => {
      const trigger = { kind: "webhook", webhook_id: HOOK, when: [{ path: "action", op, value }] };
      const a = {
        id: "a1",
        name: "before",
        trigger,
        action: {
          kind: "start_session",
          engine: API.id,
          model: null,
          folder: "/repo",
          bypass: false,
          message: { text: "go" },
        },
        policy: null,
      } as unknown as Automation;
      const f = { ...formFromAutomation(a, "UTC"), name: "after" };
      expect(stepProblems(f).events).toEqual([]);
      expect(formProblems(f)).toEqual([]);
      expect(bodyFromForm(f).trigger).toEqual(trigger);
    });

  test("still missing: a fresh row never typed into, an empty contains, an empty number or yes/no", () => {
    const one = (values: { text: string; type: "text" | "number" | "yes_no"; unset?: true }[], op = "is" as const) =>
      stepProblems(hookForm({ when: [{ path: "action", op, values }] })).events;
    expect(one([{ text: "", type: "text", unset: true }])).toEqual(["Condition 1: give it a value"]);
    expect(one([{ text: "", type: "number" }])).toEqual(["Condition 1: give it a value"]);
    expect(one([{ text: "", type: "yes_no" }])).toEqual(["Condition 1: give it a value"]);
    expect(
      stepProblems(hookForm({ when: [{ path: "action", op: "contains", values: [{ text: "", type: "text" }] }] })).events,
    ).toEqual(["Condition 1: give it a value"]);
    expect(
      stepProblems(
        hookForm({
          when: [{ path: "action", op: "one_of", values: [{ text: "", type: "text" }, { text: "", type: "text", unset: true }] }],
        }),
      ).events,
    ).toEqual(["Condition 1: give it a value"]);
    // Mapping choices must be non-empty, as the server requires.
    expect(
      stepProblems(hookForm({ fromDelivery: { pr_number: { path: "action", type: "choice", choices: ["", "x"] } } })).what,
    ).toContain("pr_number: list the allowed values, none empty");
  });

  test("mapping choices with commas and spaces come back whole", () => {
    const message = {
      template_id: "t1",
      values: { reviewer: "bot" },
      from_delivery: {
        team: { path: "repository.owner.login", type: "choice", choices: ["ops, urgent", "web team", "x"] },
        pr_number: { path: "number", type: "number" },
      },
    };
    const body = renamed({ kind: "webhook", webhook_id: HOOK }, message);
    expect((body.action as { message: unknown }).message).toEqual(message);
    expect(body.name).toBe("after");
  });
});

describe("what the last delivery's shape says", () => {
  test("a field it did not have warns; a type that can't fit holds the step", () => {
    const f = hookForm({
      when: [
        { path: "pull_request.base.ref", op: "is", values: [{ text: "main", type: "text" }] },
        { path: "number", op: "is_true", values: [{ text: "", type: "text" }] },
        { path: "review", op: "missing", values: [{ text: "", type: "text" }] },
      ],
      fromDelivery: {
        pr_number: { path: "pull_request.title", type: "number", choices: [] },
        reviewer: { path: "pull_request.head.ref", type: "text", choices: [] },
        merged: { path: "pull_request.merged_by", type: "text", choices: [] },
        draft: { path: "pull_request.draft", type: "yes_no", choices: [] },
      },
    });
    const c = deliveryChecks(f, SHAPE);
    expect(c.events.warnings).toEqual(["“pull_request.base.ref” was not in the last delivery — it may never match."]);
    expect(c.events.problems).toEqual([
      "Condition 2: number is a number, not yes or no. Use “is” or pick a yes/no field.",
    ]);
    expect(c.what.problems).toEqual([
      "pr_number is a number; pull_request.title is text. Pick a number field, or change its type.",
    ]);
    expect(c.what.warnings).toEqual([
      "reviewer: “pull_request.head.ref” was not in the last delivery — a delivery without it is refused.",
      "merged: “pull_request.merged_by” was empty in the last delivery, so its type can’t be checked.",
    ]);
  });

  test("no delivery yet: nothing to check against", () => {
    const f = hookForm({ when: [{ path: "anything", op: "exists", values: [{ text: "", type: "text" }] }] });
    expect(deliveryChecks(f, [])).toEqual({
      events: { problems: [], warnings: [] },
      what: { problems: [], warnings: [] },
    });
  });
});

describe("the pairing and the steps", () => {
  test("a review loop's own webhook restricts the action only while it IS the trigger", () => {
    const legacy = formFromAutomation(
      {
        id: "rv1",
        name: "Loop",
        trigger: { kind: "webhook", scheme: "forgejo" },
        action: {
          kind: "forge_review_loop",
          project_id: "p1",
          repo: "example-org/shop",
          reviewer: { specialist_id: "s", version: 3 },
          reviewer_login: "bot",
          legs: { issues: true, pulls: true },
          max_rounds: { issue: 5, pr: 4 },
          return_to_origin: true,
        },
        policy: null,
      } as unknown as Automation,
      "UTC",
    );
    expect(legacyTrigger(legacy)).toBe(true);
    for (const kind of ["manual", "schedule"] as const) {
      const moved = withTrigger(legacy, kind, 3);
      // The identity is kept, for a switch back; it restricts nothing now.
      expect(moved.legacyHook).toBe(true);
      expect(legacyTrigger(moved)).toBe(false);
      expect(moved.actionKind).toBe("start_mission");
      const session = withAction(moved, "start_session", 3);
      expect(session.triggerKind).toBe(kind);
      expect(session.actionKind).toBe("start_session");
      expect(stepProblems({ ...session, engine: API.id, folder: "/repo", text: "go" }).what).toEqual([]);
      expect(stepProblems({ ...moved, projectId: "p1", text: "go" }).what).toEqual([]);
      // Back to the webhook: its own again, with its loop.
      const back = withTrigger(session, "webhook", 3);
      expect(legacyTrigger(back)).toBe(true);
      expect(back.actionKind).toBe("forge_review_loop");
      expect(bodyFromForm(back).trigger).toEqual({ kind: "webhook", scheme: "forgejo" });
    }
    // On its own webhook, anything but the loop is still refused.
    expect(stepProblems({ ...legacy, actionKind: "start_session" }).what).toContain(
      "A review loop’s own webhook starts only that review loop",
    );
  });

  test("a webhook starts or messages an API session only", () => {
    setRoster(ENGINES);
    expect(stepProblems(hookForm({ engine: CONSOLE.id })).what).toContain(
      "A webhook starts or messages an API session only; pick an API agent",
    );
    expect(stepProblems(hookForm()).what).not.toContain(
      "A webhook starts or messages an API session only; pick an API agent",
    );
    const msg = hookForm({ actionKind: "send_to_session", sessionKey: `${CONSOLE.id}:abc` });
    expect(stepProblems(msg).what).toContain("A webhook starts or messages an API session only; pick an API agent");
    // A schedule may still start a terminal session.
    const sched = { ...hookForm({ engine: CONSOLE.id }), triggerKind: "schedule" as const };
    expect(stepProblems(sched).what).not.toContain(
      "A webhook starts or messages an API session only; pick an API agent",
    );
  });

  test("any webhook from the list must be picked; the name is asked last but listed first", () => {
    const f = { ...hookForm(), webhookId: "", name: "" };
    expect(stepProblems(f).when).toEqual(["Pick a webhook"]);
    expect(formProblems(f).slice(0, 2)).toEqual(["Give it a name", "Pick a webhook"]);
    // A webhook can't start a mission.
    expect(stepProblems({ ...hookForm(), actionKind: "start_mission", projectId: "p1" }).what).toContain(
      "A webhook can’t start a mission; start or message a session",
    );
  });

  test("the limits in one line", () => {
    expect(limitsWords(blankForm("UTC"))).toBe(
      "one run at a time · at most 48 runs a day · pauses after 3 failures in a row · runs until you turn it off",
    );
    expect(limitsWords({ ...blankForm("UTC"), concurrency: "allow", maxConcurrent: 3 })).toMatch(
      /^up to 3 runs at once/,
    );
  });
});
