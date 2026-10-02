/** A small, STATEFUL stand-in for `/api/automations` (#1201), shaped like `routes/automations.py`.
 *
 *  Stateful because the flows under test are sequences: create → enable → the row reads ENABLED;
 *  Run now → the run appears with its steps. Every request is recorded, so a spec asserts the body
 *  the page SENT (the consent resend's digest), not only what it painted. Scope lines are the
 *  server's `describe()` wording for the configs used here, copied, so the widened highlight is
 *  exercised against real text. */
import type { Page, Route } from "@playwright/test";

import { commonMocks } from "./mission-directions";
import { mockMissions } from "./mission-console";

type Json = Record<string, unknown>;

export const PROJECT = {
  id: "p1",
  name: "agent-sessions",
  color: "",
  folders: ["/repo"],
  default_folder: "/repo",
  archived: false,
  created_at: 1_700_000_000,
  session_count: 0,
};

export const ENGINES = [
  { id: "claude", ok: true, reason: "" },
  {
    id: "codex",
    ok: false,
    reason: "codex does not reveal its session id until after its first turn",
  },
];

const LIMITS = {
  interval_min_minutes: 5,
  max_runs_per_day_max: 288,
  max_concurrent_max: 3,
  pause_after_failures_max: 20,
  triggers: ["once", "schedule", "manual"],
  actions: ["start_mission", "start_session", "send_to_session"],
  autonomy: ["propose", "dispatch", "dispatch_auto_choose"],
  timezone: "UTC",
};

const AUTONOMY_LINE: Record<string, string> = {
  propose: "Autonomy: plans the mission and waits for you to dispatch it",
  dispatch: "Autonomy: dispatches the plan without asking you",
  dispatch_auto_choose: "Autonomy: dispatches the plan and may answer menus on its own",
};

/** `automations.describe` for the configs these specs use. */
export function scopeLines(config: Json): string[] {
  const action = config.action as Json;
  const trigger = config.trigger as Json;
  const policy = (config.policy ?? {}) as Json;
  const lines: string[] = [];
  if (action.kind === "start_mission") {
    lines.push(`Starts a mission in project ${action.project_id}, in /repo`);
    lines.push("The agent is chosen by the mission planner");
    lines.push("Permission bypass: off (automated missions never bypass)");
    if (action.checklist_id == null) lines.push("Checklist: Ship a change (your default)");
    else if (action.checklist_id === ":none") lines.push("Checklist: none");
    else lines.push(`Checklist: ${action.checklist_id}`);
    lines.push(AUTONOMY_LINE[String(action.autonomy)] ?? "Autonomy: unknown");
    lines.push(messageLine("Instruction", action.instruction as Json));
  } else if (action.kind === "start_session") {
    // The pinned real path, when the folder the operator chose is a link to it.
    lines.push(`Starts a ${action.engine} session in ${action.folder} (resolves to /srv/repo)`);
    lines.push(
      action.bypass
        ? "Permission bypass: ON — unattended, with tool prompts suppressed"
        : "Permission bypass: off",
    );
    lines.push(messageLine("Message", action.message as Json));
  } else {
    lines.push(`Types into the live session ${action.session_key}`);
    lines.push("If that session isn't running, the run is refused; nothing is started");
    lines.push(messageLine("Message", action.message as Json));
  }
  if (trigger.kind === "schedule")
    lines.push(`On a schedule (${trigger.tz}), up to ${policy.max_runs_per_day ?? 48}/day`);
  else if (trigger.kind === "once") lines.push(`Once, at ${trigger.at} (${trigger.tz})`);
  else lines.push("Only when you press Run now");
  lines.push(`At most ${policy.max_runs_per_day ?? 48} runs a day`);
  lines.push(`Pauses after ${policy.pause_after_failures ?? 3} failures in a row`);
  lines.push(policy.expires_at == null ? "Runs until you turn it off" : `Stops on ${policy.expires_at}`);
  return lines;
}

/** `automations._message_lines` for a plain-text or template message. */
function messageLine(word: string, m: Json | undefined): string {
  if (m && typeof m.text === "string") return `${word} (plain text):\n${m.text}`;
  return `${word}: template “${m?.template_id}”`;
}

/** `automations_store.RECEIPT_MISMATCH`. */
export const RECEIPT_MISMATCH =
  "this automation's approval is out of date — the consent now shows everything it does; review it and approve it again";

const DAY = 86_400;

function strip(now: number, worst: (string | null)[] = []): Json[] {
  return Array.from({ length: 14 }, (_, i) => {
    const d = new Date((now - (13 - i) * DAY) * 1000).toISOString().slice(0, 10);
    return { date: d, worst: worst[i] ?? null };
  });
}

export function automation(over: Json = {}): Json {
  const now = Math.floor(Date.now() / 1000);
  const config = {
    name: "Nightly dependency audit",
    trigger: { kind: "schedule", cadence: { kind: "daily", time: "03:00" }, tz: "UTC" },
    action: {
      kind: "start_mission",
      project_id: "p1",
      instruction: { text: "Audit the dependencies" },
      checklist_id: null,
      autonomy: "propose",
    },
    policy: {
      concurrency: "skip",
      max_concurrent: 1,
      max_runs_per_day: 48,
      pause_after_failures: 3,
      expires_at: null,
    },
  };
  const base: Json = {
    id: "a1",
    name: config.name,
    ...config,
    revision: 1,
    state: "enabled",
    enabled: true,
    paused: false,
    paused_reason: "",
    needs_reapproval: false,
    reapproval_reason: "",
    consented_at: now - DAY,
    consented_scope: { version: 1 },
    scope: { version: 1 },
    scope_digest: "digest-1",
    consecutive_failures: 0,
    check_note: "",
    next_run: { slot: "tomorrow", at: now + 9 * 3600 },
    last_run: null,
    stats: { ok: 12, failed: 0, skipped: 1, pending: 0, runs: 13, success_rate: 1 },
    created_at: now - 20 * DAY,
    updated_at: now - DAY,
  };
  const out = { ...base, ...over };
  out.scope_lines = over.scope_lines ?? scopeLines(out);
  // The strip FOLLOWS the stats, as the server's does: a fixture with no runs has fourteen empty
  // days, and one with failures has failed days. A fixed strip painted impossible rows ("never
  // enabled" beside a full strip) and let "paint an empty day green" pass.
  if (!("strip" in over)) out.strip = stripFor(now, out.stats as Stats);
  return out;
}

type Stats = { ok: number; failed: number; skipped: number; pending: number; runs: number };

/** A 14-day strip consistent with `stats`: the newest days carry the failures, then the skips, then
 *  the ok runs, one run per day; days beyond the runs are empty. */
export function stripFor(now: number, stats: Stats): Json[] {
  const worst: (string | null)[] = Array(14).fill(null);
  let day = 13;
  for (const [cls, n] of [
    ["failed", stats.failed],
    ["skipped", stats.skipped],
    ["pending", stats.pending],
    ["ok", stats.ok],
  ] as const) {
    for (let i = 0; i < n && day >= 0; i++) worst[day--] = cls;
  }
  return strip(now, worst);
}

export interface FakeOptions {
  automations?: Json[];
  loopEnabled?: boolean;
  /** Answer a consent-less PATCH of an ENABLED automation with this widening (a 422). */
  widenOnPatch?: string[];
  /** The first consented enable is a 409 whose scope MOVED: these lines, and a new digest. */
  enableScopeMovedOnce?: string[];
  /** The first consented enable is a stale-revision 409 (it was edited meanwhile: revision + 1). */
  enableStaleOnce?: boolean;
  /** The first consented PATCH is a 409 whose scope moved. Without `patchScopeMovedWidened` the
   *  409 names nothing widened (an older server); with it, it names these (the merged server). */
  patchScopeMovedOnce?: boolean;
  patchScopeMovedWidened?: string[];
  /** Hold `GET /api/automations/{id}` for this long, per id (a slow load). */
  delayGetMs?: Record<string, number>;
  /** Hold every create this long before answering. */
  createDelayMs?: number;
  /** Hold every run-history page after the first (offset > 0) this long. */
  runsPageDelayMs?: number;
  /** Automations (by id) whose consent receipt predates the full scope: Run now is refused and
   *  flags them for re-approval, as the server does; approving clears it. */
  legacyReceipt?: string[];
  /** The first DELETE is a stale-revision 409. */
  deleteStaleOnce?: boolean;
  /** Hold every PATCH this long before answering (a slow save). */
  patchDelayMs?: number;
  /** Every acknowledged change reports `in_flight`: a run past its last check may still complete. */
  inFlight?: boolean;
  /** Answer Run now with a 409 and this detail. */
  runRefusal?: string;
  origins?: Record<string, Json>;
  /** Pre-recorded run history, by automation id, newest first. */
  runs?: Record<string, Json[]>;
  /** Fail every list read after the first with this status. */
  listFailAfterFirst?: number;
  sessions?: Json[];
  templates?: Json[];
}

export interface Fake {
  state: { automations: Json[]; runs: Record<string, Json[]>; loopEnabled: boolean };
  requests: { method: string; path: string; body: Json | null }[];
}

function stepsFor(action: Json, t: number): Json[] {
  const s = (seq: number, step: string, detail: string, dt: number) => ({
    seq,
    at: t + dt,
    step,
    detail,
  });
  if (action.kind === "start_session")
    return [
      s(1, "claimed", "slot manual", 0),
      s(2, "launched", "claude:new", 1),
      s(3, "started", "claude:new", 4),
      s(4, "briefed", "claude:new", 8),
      s(5, "ok", "session started and briefed", 8),
    ];
  return [
    s(1, "claimed", "slot manual", 0),
    s(2, "creating_mission", "p1", 1),
    s(3, "mission_created", "msn_0123456789abcdef0123456789abcdef", 1),
    s(4, "started", "the mission is running", 2),
  ];
}

/** Everything an Automations page reads, plus the stateful `/api/automations` fake. */
export async function mockAutomations(page: Page, opts: FakeOptions = {}): Promise<Fake> {
  await commonMocks(page);
  await mockMissions(page);
  await page.route("**/api/pulse/orchestrator", (r) =>
    r.fulfill({ status: 500, json: { detail: "off" } }),
  );
  await page.route("**/api/projects**", (r) => r.fulfill({ json: { projects: [PROJECT] } }));
  await page.route("**/api/templates**", (r) =>
    r.fulfill({ json: { templates: opts.templates ?? [], limits: {} } }),
  );
  await page.route("**/api/template-variables", (r) =>
    r.fulfill({ json: { variables: [], limits: {} } }),
  );
  if (opts.sessions)
    await page.route("**/api/sessions**", (r) =>
      r.fulfill({
        json: {
          sessions: opts.sessions,
          next_offset: null,
          total: opts.sessions!.length,
          facets: { projects: [], engines: [] },
        },
      }),
    );

  const fake: Fake = {
    state: {
      automations: opts.automations ?? [],
      runs: opts.runs ?? {},
      loopEnabled: opts.loopEnabled ?? true,
    },
    requests: [],
  };
  let seq = 0;
  let listReads = 0;
  let enableMoved = false;
  let enableStale = false;
  const legacy = new Set(opts.legacyReceipt ?? []);
  let deleteStale = false;
  let patchMoved = false;
  let movedDigest = "";
  const find = (id: string) => fake.state.automations.find((a) => a.id === id);

  await page.route(/\/api\/automations(\/[^?]*)?(\?.*)?$/, async (route: Route) => {
    const req = route.request();
    const url = new URL(req.url());
    const path = url.pathname.replace(/^\/api\/automations/, "");
    const method = req.method();
    const body = (req.postDataJSON?.() ?? null) as Json | null;
    fake.requests.push({ method, path, body });
    const json = (status: number, payload: unknown) => route.fulfill({ status, json: payload });
    const IN_FLIGHT = opts.inFlight
      ? { in_flight: true, in_flight_detail: "a run already in progress may still complete" }
      : { in_flight: false, in_flight_detail: "" };
    const changed = (payload: Json) => json(200, { ...payload, ...IN_FLIGHT });
    const parts = path.split("/").filter(Boolean);

    if (!parts.length && method === "GET") {
      listReads += 1;
      if (opts.listFailAfterFirst && listReads > 1)
        return json(opts.listFailAfterFirst, { detail: "the automations store failed" });
      return json(200, {
        automations: fake.state.automations,
        engines: ENGINES,
        loop: { enabled: fake.state.loopEnabled, owner: true },
        limits: LIMITS,
      });
    }
    if (!parts.length && method === "POST") {
      if (opts.createDelayMs) await new Promise((r) => setTimeout(r, opts.createDelayMs));
      seq += 1;
      const a = automation({
        ...body,
        id: `new${seq}`,
        name: body?.name,
        revision: 1,
        state: "off",
        enabled: false,
        consented_at: null,
        consented_scope: null,
        next_run: null,
        stats: { ok: 0, failed: 0, skipped: 0, pending: 0, runs: 0, success_rate: null },
        scope_digest: `digest-new${seq}-1`,
      });
      fake.state.automations.push(a);
      return json(201, a);
    }
    if (parts[0] === "origins") return json(200, { origins: opts.origins ?? {} });
    if (parts[0] === "runs" && parts[1]) {
      for (const list of Object.values(fake.state.runs)) {
        const r = list.find((x) => x.id === parts[1]);
        if (r) return json(200, r);
      }
      return json(404, { detail: "not found" });
    }
    const a = find(parts[0]);
    if (!a) return json(404, { detail: "not found" });
    const verb = parts[1];

    if (!verb && method === "GET") {
      const wait = opts.delayGetMs?.[a.id as string];
      if (wait) await new Promise((r) => setTimeout(r, wait));
      return json(200, a);
    }
    if (!verb && method === "PATCH") {
      if (opts.patchDelayMs) await new Promise((r) => setTimeout(r, opts.patchDelayMs));
      if (body?.revision !== a.revision) return json(409, { detail: "it changed elsewhere" });
      const next = { ...a, ...body };
      delete next.consent;
      delete (next as Json).scope_digest;
      const lines = scopeLines(next);
      const digest = `digest-${a.id}-${Number(a.revision) + 1}`;
      if (a.enabled && opts.widenOnPatch && body?.consent !== true)
        return json(422, {
          detail: "this change widens what the automation may do; confirm it again",
          widened: opts.widenOnPatch,
          scope: {},
          scope_lines: lines,
          scope_digest: digest,
        });
      if (a.enabled && opts.patchScopeMovedOnce && !patchMoved) {
        patchMoved = true;
        movedDigest = `${digest}-moved`;
        return json(409, {
          detail: "what this automation would do changed since you read it — review it again",
          scope: {},
          scope_lines: [...lines, "Checklist: audit"],
          scope_digest: movedDigest,
          ...(opts.patchScopeMovedWidened ? { widened: opts.patchScopeMovedWidened } : {}),
        });
      }
      if (a.enabled && opts.widenOnPatch && body?.scope_digest !== (movedDigest || digest))
        return json(409, { detail: "the scope moved", scope_lines: lines, scope_digest: digest });
      Object.assign(a, next, {
        revision: Number(a.revision) + 1,
        scope_lines: lines,
        scope_digest: digest,
      });
      return changed(a);
    }
    if (!verb && method === "DELETE") {
      if (opts.deleteStaleOnce && !deleteStale) {
        deleteStale = true;
        return json(409, { detail: "it changed elsewhere" });
      }
      fake.state.automations = fake.state.automations.filter((x) => x !== a);
      return changed({ deleted: a.id });
    }
    if (verb === "enable") {
      if (opts.enableStaleOnce && !enableStale && body?.consent === true) {
        enableStale = true;
        a.revision = Number(a.revision) + 1;
        return json(409, { detail: "it changed elsewhere" });
      }
      if (body?.revision !== a.revision) return json(409, { detail: "it changed elsewhere" });
      if (opts.enableScopeMovedOnce && !enableMoved && body?.consent === true) {
        enableMoved = true;
        a.scope_lines = opts.enableScopeMovedOnce;
        a.scope_digest = `${a.scope_digest}-moved`;
        return json(409, {
          detail: "what this automation would do changed since you read it — review it again",
          scope: {},
          scope_lines: a.scope_lines,
          scope_digest: a.scope_digest,
        });
      }
      if (body?.consent !== true || body?.scope_digest !== a.scope_digest)
        return json(422, {
          detail: "enabling needs your consent to its full scope",
          scope_lines: a.scope_lines,
          scope_digest: a.scope_digest,
        });
      legacy.delete(a.id as string);
      Object.assign(a, {
        enabled: true,
        state: "enabled",
        reapproval_reason: "",
        consented_at: Math.floor(Date.now() / 1000),
        needs_reapproval: false,
        paused: false,
        next_run: { slot: "tomorrow", at: Math.floor(Date.now() / 1000) + 9 * 3600 },
      });
      return changed(a);
    }
    if (verb === "pause" || verb === "resume" || verb === "disable") {
      if (verb === "pause")
        Object.assign(a, { paused: true, state: "paused", paused_reason: "paused by you", next_run: null });
      if (verb === "resume") Object.assign(a, { paused: false, state: "enabled", paused_reason: "" });
      if (verb === "disable") Object.assign(a, { enabled: false, paused: false, state: "off", next_run: null });
      return changed(a);
    }
    if (verb === "run" && method === "POST" && legacy.has(a.id as string)) {
      Object.assign(a, {
        needs_reapproval: true,
        paused: true,
        state: "needs_reapproval",
        reapproval_reason: RECEIPT_MISMATCH,
        next_run: null,
      });
      return json(409, { detail: RECEIPT_MISMATCH });
    }
    if (verb === "run" && method === "POST") {
      if (opts.runRefusal) return json(409, { detail: opts.runRefusal });
      seq += 1;
      const t = Math.floor(Date.now() / 1000);
      const action = a.action as Json;
      const done = action.kind === "start_session";
      const run = {
        id: `run${seq}`,
        automation_id: a.id,
        trigger: "manual",
        slot: `manual:${seq}`,
        fire_at: null,
        catch_up: false,
        covered: 1,
        state: "done",
        outcome: done ? "ok" : "started",
        result_class: done ? "ok" : "pending",
        reason: done ? "session started and briefed" : "the mission is running",
        mission_id: done ? null : "msn_0123456789abcdef0123456789abcdef",
        session_key: done ? "claude:22222222-2222-4222-8222-222222222222" : null,
        created_at: t,
        finished_at: done ? t + 8 : null,
        inputs: { message: "Audit the dependencies" },
        scope: {},
        steps: stepsFor(action, t),
      };
      (fake.state.runs[a.id] ??= []).unshift(run);
      a.last_run = run;
      // The POST answers with the run as claimed, still dispatching.
      return json(202, { ...run, state: "dispatching", outcome: "", steps: undefined });
    }
    if (verb === "runs" && method === "GET") {
      const list = fake.state.runs[a.id as string] ?? [];
      const limit = Number(url.searchParams.get("limit") ?? 50);
      const offset = Number(url.searchParams.get("offset") ?? 0);
      if (offset > 0 && opts.runsPageDelayMs)
        await new Promise((r) => setTimeout(r, opts.runsPageDelayMs));
      return json(200, { runs: list.slice(offset, offset + limit), total: list.length });
    }
    return json(405, { detail: `unmocked ${method} ${path}` });
  });
  return fake;
}
