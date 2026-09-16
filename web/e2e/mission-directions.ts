/** Shared mocks for mission control directions (#983 P2): the playbook editor, the mission console
 *  with an objective list and a supervisor nudge, and the thread's nudge records.
 *
 * Producer-shaped, like `mission-console.ts`. The placeholder table is
 * `tests/fixtures/direction_placeholders.json`, which pytest pins to
 * `mission_directions.placeholder_table()`. A supervisor nudge carries the `render` its proposal
 * persisted and the `render_status` / `can_approve` / `objective_title` that
 * `routes/pulse._operator_projection` adds. Thread events carry the metas `ensure_action_event` and
 * `escalate_once` write. The preview route is a stand-in: what the real one renders is pinned by
 * pytest, and a browser test asserts the body the page sends and that it paints the answer as given.
 */
import { readFileSync } from "node:fs";
import { resolve } from "node:path";
import { expect, type Page } from "@playwright/test";

import { DIRECTION_PREVIEW_PATH } from "../src/lib/apiPaths";
import { settingsPath } from "../src/routes/settingsTabs";
import {
  MISSION,
  missionList,
  missionRow,
  mockMissions,
  openMissionRail,
  openObjectiveMenu,
} from "./mission-console";

export const T = 1_700_000_000;
export const PLACEHOLDERS = JSON.parse(
  readFileSync(resolve(process.cwd(), "../tests/fixtures/direction_placeholders.json"), "utf8"),
);

// --- what the server would say ---------------------------------------------------------------------

const EXAMPLES: Record<string, string> = {
  pr: "412",
  pr_state: "open",
  checks: "failure",
  review: "APPROVED",
  repo: "acme/upload-service",
  branch: "fix/upload-retry",
};
/** `mission_directions.validate`'s words for an unknown placeholder. */
export const UNKNOWN =
  "unknown placeholder {nope}; the placeholders are {pr}, {pr_state}, {checks}, {review}, {repo}, {branch}";
/** …and `prefs` wrapping them for a playbook save. */
export const SAVE_REFUSAL = `objective 'checks_green' has an invalid direction — ${UNKNOWN}`;

export const NUDGE_TEMPLATE =
  "Please continue with the task you were working on. If you finished it, say so and stop.";
export const ORCH = {
  enabled: true,
  autonomy: "yolo",
  allowed_verbs: ["continue"],
  auto_verbs_ceiling: ["continue"],
  confidence_min: 0.9,
  interval_minutes: 10,
  max_actions_per_pass: 4,
  proposal_ttl_minutes: 30,
  stale_hours: 24,
  nudge_template: NUDGE_TEMPLATE,
  notify: "escalations",
  configured: true,
  default_nudge_template: NUDGE_TEMPLATE,
};

export const PLAYBOOKS = {
  default_id: "ship",
  revision: 3,
  playbooks: [
    {
      id: "ship",
      label: "Ship a change",
      objectives: [
        {
          key: "checks_green",
          title: "Checks are green on the PR",
          probe: "forge_checks",
          probe_args: { branch: "fix/upload-retry" },
          gate: true,
        },
      ],
    },
  ],
};

const PROBES = {
  kinds: ["none", "forge_checks", "forge_pr", "forge_review"],
  non_gating: [],
  args: {
    none: { required: [], optional: [] },
    forge_checks: { required: [], optional: ["branch", "repo"] },
    forge_pr: { required: [], optional: ["branch", "repo"] },
    forge_review: { required: [], optional: ["branch", "repo"] },
  },
  types: {
    none: {},
    forge_checks: { branch: "text", repo: "text" },
    forge_pr: { branch: "text", repo: "text" },
    forge_review: { branch: "text", repo: "text" },
  },
  placeholders: PLACEHOLDERS,
};

const CONFIG = {
  csrf: "x",
  new_session_engines: ["claude"],
  terminal_backend: "ws",
  auth_mode: "none",
  overview_expanded: [],
  projects_hidden: [],
  onboarded: true,
  pulse: { configured: true },
  mission_playbooks: PLAYBOOKS,
  mission_probes: PROBES,
  orchestrator: ORCH,
};

/** A stand-in for `POST /api/mission-directions/preview`. */
function previewAnswer(direction: string) {
  if (direction.includes("{nope}")) return { status: 422, json: { detail: UNKNOWN } };
  if (!direction.trim()) return { status: 200, json: { text: null, facts: [] } };
  return {
    status: 200,
    json: {
      text: direction.replace(/\{(\w+)\}/g, (m, n: string) => EXAMPLES[n] ?? m),
      facts: [],
    },
  };
}

/** The reads every surface here makes, and the preview route. Returns the preview bodies sent. */
export async function commonMocks(page: Page) {
  const previews: { direction: string; probe: string }[] = [];
  await page.route("**/api/config", (r) => r.fulfill({ json: CONFIG }));
  await page.route("**/api/version", (r) => r.fulfill({ json: { version: "test" } }));
  await page.route(/\/api\/folders(\?.*)?$/, (r) => r.fulfill({ json: { folders: [] } }));
  await page.route("**/api/engines", (r) => r.fulfill({ json: { engines: [] } }));
  await page.route("**/api/system", (r) =>
    r.fulfill({ json: { auto_update: true, current: "test", channel: "stable" } }),
  );
  await page.route("**/api/projects**", (r) => r.fulfill({ json: { projects: [] } }));
  await page.route("**/api/ai-review/models**", (r) => r.fulfill({ json: { models: [] } }));
  await page.route("**/api/prompts**", (r) => r.fulfill({ json: { prompts: [] } }));
  await page.route("**/api/sessions**", (r) =>
    r.fulfill({
      json: { sessions: [], next_offset: null, total: 0, facets: { projects: [], engines: [] } },
    }),
  );
  await page.route("**/api/pulse/notifications", (r) =>
    r.fulfill({ json: { notifications: [], unread: 0, uncertain: 0, settled: [] } }),
  );
  await page.route(`**${DIRECTION_PREVIEW_PATH}`, (r) => {
    const body = r.request().postDataJSON() as { direction: string; probe: string };
    previews.push(body);
    const a = previewAnswer(body.direction);
    return r.fulfill({ status: a.status, json: a.json });
  });
  return { previews };
}

// --- the playbook editor ---------------------------------------------------------------------------

/** Settings → AI → Playbooks, with a prefs route that records each save and refuses `{nope}`. */
export async function playbookEditor(page: Page, opts: { refuse?: boolean } = {}) {
  const common = await commonMocks(page);
  const saves: { mission_playbooks: typeof PLAYBOOKS }[] = [];
  await page.route("**/api/pulse/orchestrator", (r) =>
    r.fulfill({ status: 500, json: { detail: "off" } }),
  );
  await page.route("**/api/prefs", (r) => {
    const body = r.request().postDataJSON() as { mission_playbooks: typeof PLAYBOOKS };
    saves.push(body);
    const sent = JSON.stringify(body.mission_playbooks);
    if (opts.refuse || sent.includes("{nope}")) {
      return r.fulfill({ status: 422, json: { detail: SAVE_REFUSAL } });
    }
    return r.fulfill({
      json: { mission_playbooks: { ...body.mission_playbooks, revision: PLAYBOOKS.revision + 1 } },
    });
  });
  await page.goto(settingsPath("ai-playbooks"));
  const field = page.getByTestId("objective-direction").first();
  await expect(field).toBeVisible();
  return { ...common, saves, field, text: field.getByTestId("objective-direction-text") };
}

// --- the mission console ---------------------------------------------------------------------------

export const COPIED =
  "PR #{pr}'s checks are {checks} on {branch}. Open the failing check, fix the cause, push.";
export const PLAYBOOK_NOW = "PR #{pr} checks are {checks}. Open the failing check first.";
export const OWN = "Ask a reviewer on PR #{pr}.";

type ObjectiveRow = Record<string, unknown> & { key: string };

function objective(
  key: string,
  ord: number,
  title: string,
  probe: string,
  direction: string | null,
  source: "template" | "operator" | null,
): ObjectiveRow {
  return {
    mission_id: "msn_1",
    key,
    ord,
    title,
    probe,
    probe_args: { branch: "fix/upload-retry" },
    gate: true,
    state: "open",
    met_at: null,
    observed: null,
    source: "playbook",
    direction,
    direction_source: source,
  };
}

const OBJECTIVES = () => [
  objective("checks", 0, "Checks are green on the PR", "forge_checks", COPIED, "template"),
  objective("pr", 1, "A PR is open for the branch", "forge_pr", null, null),
  objective("review", 2, "A reviewer approved the PR", "forge_review", OWN, "operator"),
];

/** The exact text a proposal persisted. A doubled space and a line break, so a page that collapsed
 *  whitespace would fail rather than pass by looking close enough. */
export const TYPED =
  "PR #412's checks are failing on fix/upload-retry.\nOpen the failing check,  fix the cause, push.";
export const WHY = "The last push broke the lint step and the agent moved on to the docs.";
export const SESSION = "claude:aaa";

export function nudgeAction(over: Record<string, unknown> = {}) {
  return {
    id: "act_nudge",
    state: "proposed",
    projection: "actionable",
    can_approve: true,
    can_reject: true,
    ts: T - 120,
    expires_at: T + 1800,
    tier: "suggest",
    session_id: SESSION,
    engine: "claude",
    title: WHY,
    project: "agent-sessions",
    project_id: "p1",
    verb: "continue",
    confidence: 1,
    rationale: "",
    evidence: "none",
    source: "supervisor",
    mission_id: "msn_1",
    objective_key: "checks",
    objective_episode: 1,
    objective_title: "Checks are green on the PR",
    render: {
      text: TYPED,
      source: "direction",
      digest: "d1",
      facts: [
        {
          name: "pr",
          value: 412,
          target: { forge_target: "t", repo: "", branch: "fix/upload-retry", pr: 412, head: "4b7e0d9c1f" },
          observed_at: T - 300,
        },
        {
          name: "checks",
          value: "failure",
          target: { forge_target: "t", repo: "", branch: "fix/upload-retry", pr: 412, head: "4b7e0d9c1f" },
          observed_at: T - 300,
        },
        { name: "branch", value: "fix/upload-retry", target: { probe_args: "a1" }, observed_at: null },
      ],
    },
    render_status: { sendable: true, reason: "" },
    announced: false,
    ...over,
  };
}

export const STALE_REASON =
  "the facts behind this nudge changed since it was proposed (its objective, target, head or a fact's value)";
export const staleNudge = () =>
  nudgeAction({ can_approve: false, render_status: { sendable: false, reason: STALE_REASON } });

export interface ConsoleServer {
  rows: ObjectiveRow[];
  patches: { ops: Record<string, unknown>[] }[];
  refuse: string | null;
  /** While set, a PATCH is recorded and then held until this resolves, so a pending save can be
   *  observed and interacted with (#997 review 4880). An immediately fulfilled save cannot. */
  hold: Promise<void> | null;
  approvals: string[];
  rejects: string[];
}

/** A promise to hold a route with, and the hand that releases it. */
export function deferred(): { promise: Promise<void>; release: () => void } {
  let release: () => void = () => {};
  const promise = new Promise<void>((resolve) => {
    release = resolve;
  });
  return { promise, release };
}

/** A running mission (or `state`) holding `claude:aaa`, opened in the console. PATCHes to its
 *  objectives are recorded and applied the way `missions._op_*` would; `refuse` answers the next one
 *  422 with that detail instead, and `hold` keeps one pending until it is released. */
export async function missionConsole(
  page: Page,
  opts: { pending?: unknown; events?: unknown[]; state?: string; context?: unknown } = {},
): Promise<ConsoleServer> {
  await commonMocks(page);
  const server: ConsoleServer = {
    rows: OBJECTIVES(),
    patches: [],
    refuse: null,
    hold: null,
    approvals: [],
    rejects: [],
  };
  await page.route("**/api/prefs", (r) => r.fulfill({ json: {} }));
  await page.route("**/api/pulse/orchestrator", (r) =>
    r.fulfill({ status: 500, json: { detail: "off" } }),
  );
  const card = {
    id: SESSION,
    engine: "claude",
    title: "Fix the flaky upload retry",
    cwd: "/repo",
    project: { kind: "project", id: "p1", name: "agent-sessions" },
    last_activity: T - 120,
    ai_summary: "",
    intervention_required: false,
    intervention_reason: "",
    live: true,
    state: opts.pending ? "needs_you" : "in_flight",
    synthesis: null,
    mission_id: "msn_1",
    ...(opts.pending ? { pending_action: opts.pending } : {}),
  };
  await page.route(/\/api\/pulse$/, (r) =>
    r.fulfill({
      json: {
        cache_version: 1,
        generated_at: T - 60,
        window_days: 3,
        scan_depth: "fast",
        input_fingerprint: "fp",
        synthesis_skipped: false,
        cards: [card],
      },
    }),
  );
  const state = opts.state ?? "running";
  await mockMissions(page, {
    missions: missionList([missionRow({ session_keys: [SESSION], state, playbook_id: "ship" })]),
    mission: {
      ...MISSION,
      state,
      playbook_id: "ship",
      ...(state === "done" ? { outcome: "done", closed_at: T - 60 } : {}),
      sessions: [{ session_key: SESSION, removed_at: null }],
      events: opts.events ?? [],
      events_next_seq: null,
    },
    // #983 P3: a roster, so the Context section renders the session's composer.
    ...(opts.context !== undefined ? { context: opts.context } : {}),
  });
  // Registered AFTER mockMissions, so it wins for the objectives URL (newest route first).
  await page.route("**/api/missions/*/objectives", async (r) => {
    if (r.request().method() === "PATCH") {
      const body = r.request().postDataJSON() as { ops: Record<string, unknown>[] };
      server.patches.push(body);
      if (server.hold) await server.hold;
      if (server.refuse !== null) {
        const detail = server.refuse;
        server.refuse = null;
        return r.fulfill({ status: 422, json: { detail } });
      }
      for (const op of body.ops) {
        const row = server.rows.find((o) => o.key === op.key);
        if (!row) continue;
        if (op.op === "set_direction")
          Object.assign(row, { direction: op.direction, direction_source: "operator" });
        if (op.op === "reset_direction")
          Object.assign(row, { direction: PLAYBOOK_NOW, direction_source: "template" });
        if (op.op === "clear_direction")
          Object.assign(row, { direction: null, direction_source: null });
      }
    }
    return r.fulfill({ json: { objectives: server.rows } });
  });
  await page.route(/\/api\/pulse\/actions\/[^/]+\/approve$/, (r) => {
    server.approvals.push(/actions\/([^/]+)\/approve/.exec(r.request().url())![1]);
    return r.fulfill({ json: { ...nudgeAction(), state: "delivered", delivered_text: TYPED } });
  });
  await page.route(/\/api\/pulse\/actions\/[^/]+\/reject$/, (r) => {
    server.rejects.push(/actions\/([^/]+)\/reject/.exec(r.request().url())![1]);
    return r.fulfill({ json: { ...staleNudge(), state: "rejected" } });
  });

  await page.goto("/mission");
  await expect(page.getByTestId("mission-console")).toBeVisible();
  await openMissionRail(page);
  await page.locator('[data-testid="rail-mission"]:visible').first().click();
  if (await page.getByRole("dialog").count()) {
    await page.keyboard.press("Escape");
    await expect(page.getByRole("dialog")).toHaveCount(0);
  }
  await expect(page.getByTestId("mission-state")).toBeVisible();
  return server;
}

export function objectiveRows(page: Page) {
  return page.getByTestId("objectives").getByTestId("objective");
}

/** Open objective row `index`'s ⋯ → Edit direction, and return the dialog. */
export async function openDirection(page: Page, index: number) {
  const menu = await openObjectiveMenu(page, index);
  await menu.getByTestId("objective-edit-direction").click();
  const dialog = page.getByTestId("direction-dialog");
  await expect(dialog).toBeVisible();
  return dialog;
}

// --- the thread ------------------------------------------------------------------------------------

function event(seq: number, kind: string, text: string, meta: Record<string, unknown>) {
  return {
    seq,
    mission_id: "msn_1",
    at: T + seq * 60,
    kind,
    session_key: SESSION,
    action_id: `act_${seq}`,
    text,
    meta,
    settlement: null,
  };
}

/** Newest first, as `get_mission` orders them: a held direction, a legacy held record, a delivered
 *  default nudge and a delivered direction. */
export const THREAD = [
  event(
    4,
    "escalation",
    "Checks are green on the PR: its direction could not be filled: {pr} is missing from this objective's latest observation",
    { held: "direction", objective_key: "checks", episode: 2 },
  ),
  event(3, "action", "A nudge was prepared but not delivered: session is not live", {
    source: "supervisor",
    objective_key: "pr",
    episode: 1,
    held: true,
    state: "failed",
  }),
  event(2, "action", NUDGE_TEMPLATE, {
    source: "supervisor",
    objective_key: "pr",
    episode: 1,
    delivered: true,
    text_source: "default_nudge",
    digest: "d2",
    stage: "delivered",
  }),
  event(1, "action", TYPED, {
    source: "supervisor",
    objective_key: "checks",
    episode: 1,
    delivered: true,
    text_source: "direction",
    digest: "d1",
    stage: "delivered",
  }),
];

/** Settings → AI → Mission control, with the orchestrator block above. */
export async function missionControlSettings(page: Page) {
  await commonMocks(page);
  await page.route("**/api/prefs", (r) => r.fulfill({ json: {} }));
  await page.route("**/api/pulse/orchestrator", (r) =>
    r.fulfill({ json: { config: ORCH, pending: [], feed: [], expired_now: 0, last: {} } }),
  );
  await mockMissions(page);
  await page.goto(settingsPath("ai-mission-control"));
}
