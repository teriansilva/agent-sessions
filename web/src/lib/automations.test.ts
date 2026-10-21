/** The pure half of the Automations pages (#1201): words, the form ↔ API mapping and the consent
 *  state. The scope lines used here are the server's own (`automations.describe`), copied verbatim,
 *  so a highlight that stops matching the server's wording fails here rather than silently. */
import { describe, expect, test } from "vitest";

import { ApiError } from "./api";
import {
  blankForm,
  bodyFromForm,
  cadenceWords,
  consentForEnable,
  consentFromError,
  dayWord,
  duplicateBody,
  formFromAutomation,
  formProblems,
  nextRunWords,
  onceWords,
  outcomeWord,
  runNowBlock,
  stateWord,
  stripWords,
  triggerWords,
  widenedLine,
} from "./automations";
import type { Automation } from "../types/automations";

function auto(over: Partial<Automation> = {}): Automation {
  return {
    id: "a1",
    name: "Nightly audit",
    trigger: { kind: "schedule", cadence: { kind: "daily", time: "03:00" }, tz: "Europe/Berlin" },
    action: {
      kind: "start_mission",
      project_id: "p1",
      instruction: { text: "audit the deps" },
      checklist_id: null,
      autonomy: "propose",
    },
    policy: {
      concurrency: "skip",
      max_concurrent: 1,
      max_runs_per_day: 4,
      pause_after_failures: 3,
      expires_at: null,
    },
    revision: 3,
    state: "enabled",
    enabled: true,
    paused: false,
    paused_reason: "",
    needs_reapproval: false,
    reapproval_reason: "",
    consented_at: 1_700_000_000,
    consented_scope: {},
    scope: {},
    scope_lines: ["Starts a mission in project p1, in /repo"],
    scope_digest: "d1",
    consecutive_failures: 0,
    next_run: null,
    last_run: null,
    stats: { ok: 0, failed: 0, skipped: 0, pending: 0, runs: 0, success_rate: null },
    strip: [],
    created_at: 1,
    updated_at: 1,
    ...over,
  };
}

describe("words", () => {
  test("a state's word and tone: red is only an active failure", () => {
    expect(stateWord("enabled")).toEqual({ word: "Enabled", tone: "up" });
    expect(stateWord("paused").tone).toBe("degraded");
    expect(stateWord("needs_reapproval")).toEqual({ word: "Needs re-approval", tone: "degraded" });
    expect(stateWord("erroring").tone).toBe("down");
    expect(stateWord("off").tone).toBe("idle");
  });

  test("an interrupted run is amber (unknown), never green and never red", () => {
    expect(outcomeWord("interrupted").tone).toBe("degraded");
    expect(outcomeWord("refused").tone).toBe("down");
    expect(outcomeWord("skipped").tone).toBe("idle");
    expect(outcomeWord("", "dispatching").word).toBe("Starting");
  });

  test("the strip's words count RUNS, and a day's square names its worst", () => {
    expect(stripWords({ ok: 12, failed: 0, skipped: 1, pending: 0, runs: 13, success_rate: 1 })).toBe(
      "12 ok · 1 skipped",
    );
    expect(stripWords({ ok: 0, failed: 3, skipped: 0, pending: 1, runs: 4, success_rate: 0 })).toBe(
      "3 failed · 1 in progress",
    );
    expect(stripWords({ ok: 0, failed: 0, skipped: 0, pending: 0, runs: 0, success_rate: null })).toBe(
      "no runs",
    );
    expect(dayWord({ date: "2026-09-29", worst: "failed" })).toBe("2026-09-29: failed");
    expect(dayWord({ date: "2026-09-29", worst: null })).toBe("2026-09-29: nothing ran");
  });

  test("a cadence in words", () => {
    expect(cadenceWords({ kind: "interval", every: 15, unit: "minutes" })).toBe("Every 15 min");
    expect(cadenceWords({ kind: "interval", every: 2, unit: "hours" })).toBe("Every 2 h");
    expect(cadenceWords({ kind: "daily", time: "03:00" })).toBe("Daily 03:00");
    expect(
      cadenceWords({ kind: "weekly", days: ["mon", "tue", "wed", "thu", "fri"], time: "07:30" }),
    ).toBe("Weekdays 07:30");
    expect(cadenceWords({ kind: "weekly", days: ["wed", "mon"], time: "06:00" })).toBe(
      "Mon, Wed 06:00",
    );
    expect(cadenceWords({ kind: "monthly", day: 1, time: "09:00" })).toBe("Monthly on the 1st 09:00");
    expect(cadenceWords({ kind: "monthly", day: 22, time: "09:00" })).toBe("Monthly on the 22nd 09:00");
  });

  test("a once is read in ITS zone, never re-interpreted in the viewer's", () => {
    expect(onceWords("2026-10-01T09:00")).toBe("Thu 01 Oct 2026 09:00");
    expect(triggerWords({ kind: "once", at: "2026-10-01T09:00", tz: "Europe/Berlin" })).toEqual({
      label: "Once",
      detail: "Thu 01 Oct 2026 09:00 · Europe/Berlin",
    });
    expect(triggerWords({ kind: "manual" }).label).toBe("Manual");
  });

  test("with no next run, the cell says why", () => {
    const now = 1_700_000_000;
    expect(nextRunWords(auto({ state: "off", enabled: false, consented_at: null }), now).sub).toBe(
      "never enabled",
    );
    expect(
      nextRunWords(auto({ state: "paused", paused_reason: "paused after 3 failed runs in a row" }), now)
        .sub,
    ).toBe("paused after 3 failed runs in a row");
    expect(nextRunWords(auto({ next_run: { slot: "x", at: now + 3600 + 720 } }), now).sub).toBe(
      "in 1 h 12 min",
    );
  });

  test("Run now is offered only where the server would accept it", () => {
    expect(runNowBlock(auto(), true)).toBe("");
    expect(runNowBlock(auto(), false)).toMatch(/switched off/);
    expect(runNowBlock(auto({ consented_at: null, enabled: false }), true)).toMatch(/never been approved/);
    expect(runNowBlock(auto({ needs_reapproval: true }), true)).toMatch(/approval again/);
    expect(runNowBlock(auto({ enabled: false }), true)).toMatch(/turned off/);
    // A paused automation runs ONCE on Run now, without resuming — the server allows it.
    expect(runNowBlock(auto({ paused: true, state: "paused" }), true)).toBe("");
  });
});

describe("the editor's form ↔ the API body", () => {
  test("a schedule + mission round-trips unchanged", () => {
    const a = auto();
    const body = bodyFromForm(formFromAutomation(a, "UTC"));
    expect(body).toEqual({
      name: "Nightly audit",
      trigger: a.trigger,
      action: a.action,
      policy: { concurrency: "skip", max_runs_per_day: 4, pause_after_failures: 3, expires_at: null },
    });
  });

  test("a weekly cadence sends its days in week order, and max_concurrent only with allow", () => {
    const f = { ...blankForm("Europe/Berlin"), name: " Stand-up ", cadenceKind: "weekly" as const };
    f.days = ["fri", "mon"];
    f.time = "07:30";
    f.concurrency = "allow";
    f.maxConcurrent = 3;
    const body = bodyFromForm(f);
    expect(body.name).toBe("Stand-up");
    expect(body.trigger).toEqual({
      kind: "schedule",
      cadence: { kind: "weekly", days: ["mon", "fri"], time: "07:30" },
      tz: "Europe/Berlin",
    });
    expect(body.policy.max_concurrent).toBe(3);
    expect(bodyFromForm(blankForm("UTC")).policy).not.toHaveProperty("max_concurrent");
  });

  test("a new session never names a model, and a template sends only typed values", () => {
    const f = {
      ...blankForm("UTC"),
      actionKind: "start_session" as const,
      engine: "agentx",
      folder: "/home/u/work",
      bypass: true,
      messageMode: "template" as const,
      templateId: "tpl_1",
      values: { repo: "acme/x", empty: "" },
    };
    expect(bodyFromForm(f).action).toEqual({
      kind: "start_session",
      engine: "agentx",
      model: null,
      folder: "/home/u/work",
      bypass: true,
      message: { template_id: "tpl_1", values: { repo: "acme/x" } },
    });
  });

  test("a send targets the engine-qualified session id it was given", () => {
    const f = {
      ...blankForm("UTC"),
      actionKind: "send_to_session" as const,
      sessionKey: "agentx:1111",
      text: "go",
    };
    expect(bodyFromForm(f).action).toEqual({
      kind: "send_to_session",
      session_key: "agentx:1111",
      message: { text: "go" },
    });
  });

  test("what the form still needs is named before Save", () => {
    const f = blankForm("UTC");
    expect(formProblems(f)).toEqual(["Give it a name", "Choose a project", "Write the instruction"]);
    expect(formProblems({ ...f, name: "x", projectId: "p", text: "go" })).toEqual([]);
    expect(formProblems({ ...f, triggerKind: "once", name: "x", projectId: "p", text: "t" })).toEqual([
      "Choose when it runs",
    ]);
  });

  test("a duplicate is a copy under a new name; a spent once becomes manual", () => {
    expect(duplicateBody(auto())?.name).toBe("Nightly audit (copy)");
    const spent = auto({ trigger: { kind: "once", at: "2020-01-01T00:00", tz: "UTC" }, next_run: null });
    expect(duplicateBody(spent)?.trigger).toEqual({ kind: "manual" });
    expect(duplicateBody(auto({ action: null }))).toBeNull();
  });
});

describe("consent", () => {
  const WIDEN = {
    detail: "this change widens what the automation may do; confirm it again",
    widened: ["higher mission autonomy", "permission bypass turned on"],
    scope: {},
    scope_lines: [
      "Starts a claude session in /home/u/work",
      "Permission bypass: ON — unattended, with tool prompts suppressed",
      "On a schedule (UTC), up to 48/day",
      "Pauses after 3 failures in a row",
    ],
    scope_digest: "abc",
  };

  test("a widening 422 carries the consent the dialog shows", () => {
    const c = consentFromError(new ApiError(422, WIDEN.detail, WIDEN));
    expect(c?.scope_digest).toBe("abc");
    expect(c?.widened).toEqual(WIDEN.widened);
    expect(c?.scope_lines).toEqual(WIDEN.scope_lines);
  });

  test("a 409 whose scope moved re-opens it; a stale revision and a plain 422 do not", () => {
    // Field presence is kept: absent stays absent, an explicit [] stays [].
    expect(
      consentFromError(new ApiError(409, "changed", { ...WIDEN, widened: undefined }))?.widened,
    ).toBeUndefined();
    expect(consentFromError(new ApiError(409, "changed", { ...WIDEN, widened: [] }))?.widened).toEqual(
      [],
    );
    expect(consentFromError(new ApiError(409, "revision", { detail: "revision" }))).toBeNull();
    expect(consentFromError(new ApiError(422, "bad", { detail: "time must be HH:MM" }))).toBeNull();
    expect(consentFromError(new Error("network"))).toBeNull();
  });

  test("the widened items highlight the lines they are about, and only those", () => {
    const marked = WIDEN.scope_lines.filter((l) => widenedLine(l, WIDEN.widened));
    expect(marked).toEqual(["Permission bypass: ON — unattended, with tool prompts suppressed"]);
    expect(widenedLine("Autonomy: dispatches the plan without asking you", ["higher mission autonomy"])).toBe(
      true,
    );
    expect(widenedLine("On a schedule (UTC), up to 48/day", ["a higher daily cap"])).toBe(true);
    expect(widenedLine("Pauses after 3 failures in a row", ["a higher daily cap"])).toBe(false);
    expect(widenedLine("Anything", [])).toBe(false);
  });

  test("an enable asks about the scope as read, with its digest", () => {
    expect(consentForEnable(auto())).toMatchObject({ scope_digest: "d1", widened: [] });
    expect(consentForEnable(auto({ scope_digest: null }))).toBeNull();
  });
});

describe("a scope that moved under an open dialog", () => {
  test("keeps naming what widened when the 409 does not say", async () => {
    const { reconsent } = await import("./automations");
    const prev = { detail: "", widened: ["higher mission autonomy"], scope_lines: ["a"], scope_digest: "d1" };
    // The server OMITTED `widened` (an older server): carry the previous list forward.
    const missing = { detail: "", widened: undefined, scope_lines: ["a", "b"], scope_digest: "d2" };
    expect(reconsent(missing, prev)).toEqual({ ...missing, widened: ["higher mission autonomy"] });
    expect(reconsent(missing, null).widened).toEqual([]);
    // The server SAID nothing widens now: an explicit [] wins over the stale labels.
    const empty = { ...missing, widened: [] };
    expect(reconsent(empty, prev).widened).toEqual([]);
    const own = { ...missing, widened: ["a new target"] };
    expect(reconsent(own, prev).widened).toEqual(["a new target"]);
  });
});

describe("the merged API's extra words", () => {
  test("in_flight, status codes, check_note, stopped and partial", async () => {
    const m = await import("./automations");
    expect(m.inFlightNote({ in_flight: true, in_flight_detail: "a run already in progress may still complete" })).toBe(
      "Note: a run already in progress may still complete.",
    );
    expect(m.inFlightNote({ in_flight: false })).toBe("");
    expect(m.errorWords(new ApiError(404, "not found"))).toMatch(/no longer exists/);
    expect(m.errorWords(new ApiError(413, "request body is too large"))).toMatch(/too large/);
    expect(m.errorWords(new ApiError(503, "could not check the automation's inputs right now"))).toBe(
      "could not check the automation's inputs right now — try again in a moment.",
    );
    expect(m.errorWords(new ApiError(409, "this automation is off; enable it first"))).toBe(
      "this automation is off; enable it first",
    );
    const noted = auto({ check_note: "not checked: the store is busy" });
    expect(m.needsYou(noted)).toBe(true);
    expect(m.whyNotRunning(noted)).toBe("Not checked: the store is busy.");
    expect(outcomeWord("stopped")).toEqual({ word: "Stopped", tone: "idle" });
    expect(outcomeWord("partial").tone).toBe("down");
  });
});
