// The mission console's states for the NIGHTJAR sweep (#1573). The shared fixtures hold ONE running
// mission (M1); a capture opts into another state of that same mission with `mock: { mission: … }`
// (and `missionDecisions: true` for the decisions waiting on it). Invented data only.
import * as F from "./fixtures.mjs";

const at = (ago) => F.NOW - ago;
const ev = (seq, ago, kind, text, extra = {}) => ({
  seq,
  mission_id: F.M1,
  at: at(ago),
  kind,
  session_key: null,
  action_id: null,
  text,
  meta: null,
  settlement: null,
  ...extra,
});
const KEYS = F.SESSIONS.map((s) => s.id);
const roster = (keys) =>
  keys.map((k, i) => ({ mission_id: F.M1, session_key: k, role: i === 0 ? "lead" : null, added_at: at(3 * 3600 - 60), removed_at: null }));

const PLAN = {
  plan_id: "pln_" + "a1".repeat(16),
  mission_id: F.M1,
  project_id: "p-orbital",
  cwd: `${F.HOME}/orbital-api`,
  engine: "claude",
  engine_reason: "The limiter and the load test live in one repository; one agent keeps the branch coherent.",
  brief:
    "Add a token-bucket limiter keyed by API key to the ingest router, answer 429 with Retry-After, and prove 2k rps with p95 under 50 ms.",
  created_at: at(3 * 3600 - 30),
  project_options: F.PROJECTS.map((p) => ({ id: p.id, name: p.name, cwd: p.folders[0] })),
  engine_options: [
    { id: "claude", label: "Claude Code" },
    { id: "codex", label: "Codex" },
    { id: "opencode", label: "opencode" },
  ],
};

/** Every row kind the thread draws, oldest first (the board's T8). */
function showcaseEvents() {
  return [
    ev(1, 3 * 3600, "operator_msg", "Harden the public ingest API before the partner launch: per-key rate limits, billing webhooks on the v2 schema, and p95 under 50 ms at 2k rps."),
    ev(2, 3 * 3600 - 20, "plan", PLAN.brief, { meta: { project_id: "p-orbital", engine: "claude" } }),
    ev(3, 3 * 3600 - 25, "objective", null, { meta: { by: "instantiation", ops: [{ op: "add", key: "limit" }, { op: "add", key: "load" }, { op: "add", key: "webhooks" }, { op: "add", key: "checks" }, { op: "add", key: "docs" }] } }),
    ev(4, 3 * 3600 - 28, "plan_edit", null, { meta: { plan_id: PLAN.plan_id, changed: ["brief", "engine"] } }),
    ev(5, 3 * 3600 - 60, "state", "Mission launched — 2 sessions", { meta: { from: "planned", to: "running" } }),
    ev(6, 3 * 3600 - 70, "session", null, { session_key: KEYS[1], meta: { adopted: "worker" } }),
    ev(7, 5400, "assistant_msg", "Limiter is in and its tests are green. Objective 1 met — quoted from the session: “✓ answers 429 with Retry-After when empty”."),
    ev(8, 3600, "action", "Re-run the e2e suite on the webhook branch and paste the failing spec names.", {
      meta: { source: "supervisor", stage: "delivered", objective_key: "checks", text_source: "ai_auto", confidence: 0.97 },
    }),
    ev(9, 2900, "action", "An AI-drafted direction was not sent: the agent was mid-command.", {
      meta: { source: "supervisor", stage: "held", objective_key: "docs", draft: true },
    }),
    ev(10, 2400, "error", "Couldn't reach the AI endpoint (502)."),
    ev(11, 1500, "assistant_msg", "Load test held 2,031 rps with p95 at 41.7 ms. Objective 2 met."),
    ev(12, 900, "completion", "All required objectives are met. Move to review?", {
      meta: {
        proposal: true,
        gates: 4,
        unmet_gates: 0,
        held: {
          observed: [{ key: "checks", title: "CI checks are green on the pull request" }],
          judged: [
            { key: "limit", title: "Over-quota keys get 429 + Retry-After", confidence: 0.94 },
            { key: "load", title: "Load test holds 2k rps with p95 under 50 ms", confidence: 0.91 },
          ],
          waived: [],
        },
        objectives: [{ key: "docs", title: "Limits documented for partners", gate: false, current: "outstanding" }],
        outstanding_goals: 1,
      },
    }),
    ev(13, 600, "operator_msg", "Good. Codex can run the migration on staging once the dry run is clean."),
    ev(14, 400, "restart", "Mission control restarted."),
    ev(15, 240, "assistant_msg", "Codex's dry run is clean (4 statements). It is asking to run the migration on staging — approved per your last message, sending now."),
    ev(16, 120, "session", null, { session_key: KEYS[1], meta: { detached: true } }),
  ];
}

/** A judged objective that did NOT hold, beside the base set's two that did. */
function showcaseObjectives(base) {
  return base.map((o) =>
    o.key === "docs"
      ? {
          ...o,
          gate: false,
          state: "pending",
          direction: "Document the per-key limits for {project} partners: the 429 body, Retry-After and the burst size.",
          direction_source: "operator",
          observed: {
            at: at(700),
            value: false,
            judged: {
              met: false,
              confidence: 0.32,
              threshold: 0.9,
              evidence: [{ source: `transcript:${KEYS[0]}`, quote: "I updated the README headings." }],
              fingerprint: "g",
              checked_at: at(700),
            },
          },
        }
      : o,
  );
}

function supervisor(objectives) {
  return {
    objectives: objectives.map((o) => ({
      key: o.key,
      title: o.title,
      gate: o.gate,
      state: o.state,
      met: o.state === "met",
      episode: 1,
      stood_down: false,
      awaiting_answer: false,
      spent: o.key === "checks" ? 2 : o.key === "webhooks" ? 3 : 0,
      remaining: o.key === "webhooks" ? 0 : 3,
      may_nudge: o.state !== "met" && o.key !== "webhooks",
      unreadable: false,
      indeterminate: false,
      live: 0,
      terminal: o.key === "webhooks",
      why_not:
        o.state === "met"
          ? "it is met"
          : o.key === "webhooks"
            ? "the nudge budget for this episode is spent"
            : "the session produced output in the last 5 minutes",
    })),
    likely_done: false,
    unmet_gates: objectives.filter((o) => o.gate && o.state !== "met").length,
    gates: objectives.filter((o) => o.gate).length,
    held_sessions: 2,
    no_session: false,
    sessions_unreadable: false,
    checked_at: at(60),
  };
}

const QUESTION = {
  seq: 40,
  question: "Rotate the old API key now, or after the release?",
  objective: "webhooks",
  episode: 1,
  options: [
    { label: "After the release — open a follow-up issue for the rotation.", action: "note", consequence: "Records your answer in the timeline; nothing else changes." },
    { label: "Now — the release waits for the new key.", action: "hold", consequence: "Holds the mission until the key is rotated.", settling: true },
  ],
};

/** The detail for one variant. `base` is the shared running mission (extras.missionDetail). */
export function missionVariant(base, v) {
  if (!v) return base;
  const objectives = base.objectives;
  switch (v) {
    case "showcase": {
      const obs = showcaseObjectives(objectives);
      return { ...base, objectives: obs, supervisor: supervisor(obs), events: showcaseEvents(), events_next_seq: 1 };
    }
    case "question":
      return { ...base, question: QUESTION, needs_you: true, needs_you_why: ["question"] };
    case "decisions":
      return { ...base, session_keys: KEYS.slice(0, 4), sessions: roster(KEYS.slice(0, 4)), supervisor: supervisor(objectives) };
    case "planning":
      return {
        ...base,
        state: "draft",
        plan_state: "pending",
        plan: null,
        sessions: [],
        session_keys: [],
        objectives: [],
        objectives_state: "pending",
        events: [ev(1, 40, "operator_msg", base.instruction)],
      };
    case "noplan":
      return {
        ...base,
        state: "draft",
        plan_state: "failed",
        plan_detail: "The AI endpoint timed out twice.",
        plan: null,
        plan_options: { project_options: PLAN.project_options, engine_options: PLAN.engine_options },
        sessions: [],
        session_keys: [],
        objectives: [],
        objectives_state: "failed",
        events: [
          ev(1, 300, "operator_msg", base.instruction),
          ev(2, 120, "planning", "Could not plan: the AI endpoint timed out twice.", { meta: { outcome: "failed", generation: 1 } }),
        ],
      };
    case "ready":
      return {
        ...base,
        state: "planned",
        plan_state: "ready",
        plan: PLAN,
        sessions: [],
        session_keys: [],
        objectives_fit: { dropped: 1, parameterised: 1 },
        events: [ev(1, 300, "operator_msg", base.instruction), ev(2, 240, "plan", PLAN.brief, { meta: { project_id: "p-orbital", engine: "claude" } })],
      };
    case "failed":
      return {
        ...base,
        state: "failed",
        retry_eligible: true,
        seed_outcome: "not_attempted",
        events: [
          ev(1, 900, "operator_msg", base.instruction),
          ev(2, 600, "state", "Start failed", {
            session_key: KEYS[0],
            meta: { from: "dispatching", to: "failed", message: "The agent exited before it read its brief.", detail: "claude exited with status 1 after 2.4 s", launch_session_key: KEYS[0] },
          }),
        ],
      };
    case "spawn":
      return { ...base, spawn_cap: 3, spawn_live: 1, spawn_cwd: `${F.HOME}/orbital-api`, spawn_engine: "claude" };
    // One objective whose title runs past the row's two-line clamp (its more / less disclosure).
    case "longobjective":
      return {
        ...base,
        objectives: objectives.map((o, i) =>
          i === 1
            ? {
                ...o,
                title:
                  "Load test holds 2k rps with p95 under 50 ms on the staging cluster, measured over a full ten-minute run with the partner's real key mix, and only after the rate limiter is deployed behind the ingest router",
              }
            : o,
        ),
      };
    case "quiet":
      return { ...base, events: [], objectives: [], objectives_state: "done", supervisor: supervisor([]) };
    case "allmet": {
      const met = objectives.map((o) => ({ ...o, state: "met", met_at: at(300) }));
      return { ...base, objectives: met, supervisor: { ...supervisor(met), likely_done: true } };
    }
    default:
      return base;
  }
}

export function contextVariant(base, v) {
  if (v === "gitError") return { ...base, git: null, git_error: "Couldn't read git status — the folder is not a repository." };
  return base;
}

/** The decisions waiting on the mission (`missionDecisions`): each rides its session's card, so the
 *  `decisions` variant holds four sessions and each carries one pending action. */
export function decisionCards() {
  const act = (i, over) => ({
    id: "act_" + String(i).repeat(12),
    state: "proposed",
    ts: at(540 + i * 60),
    tier: "suggest",
    session_id: KEYS[i],
    engine: F.SESSIONS[i].engine,
    title: F.SESSIONS[i].title,
    project: "orbital-api",
    project_id: "p-orbital",
    verb: "continue",
    confidence: 0.9,
    rationale: "",
    evidence: "none",
    projection: "actionable",
    can_approve: true,
    can_reject: true,
    mission_id: F.M1,
    ...over,
  });
  const actions = [
    act(1, {
      verb: "escalate",
      state: "escalated",
      escalation_reason: "permission",
      confidence: 0.95,
      rationale: "Codex wants to run the staging migration.",
      observed_prompt: {
        prompt_class: "permission",
        menu: null,
        permission: {
          engine: "codex",
          kind: "permission",
          parser: "codex",
          heading: "Allow command?",
          title: "npm run db:migrate -- --env staging",
          detail: "The agent asks to run this command in orbital-api.",
          question: "Run it?",
          options: [
            { n: 1, label: "Allow once", selected: true, persistent: false },
            { n: 2, label: "Allow for this session", selected: false, persistent: true },
            { n: 3, label: "Deny", selected: false, persistent: false },
          ],
        },
        observed_at: at(500),
      },
      can_approve: false,
    }),
    act(0, {
      verb: "escalate",
      state: "escalated",
      escalation_reason: "model",
      confidence: 0.82,
      rationale: "Which of two fixes should the agent keep?",
      observed_prompt: {
        prompt_class: "menu",
        menu: {
          engine: "claude",
          question: "Which fix should I keep?",
          options: [
            { n: 1, label: "Retry with backoff in the upload hook", selected: true },
            { n: 2, label: "Serialize uploads behind one queue", selected: false },
          ],
        },
        observed_at: at(2400),
      },
      ts: at(2400),
      can_approve: false,
    }),
    act(2, {
      source: "supervisor",
      objective_key: "webhooks",
      objective_title: "Billing webhooks write v2 events on staging",
      confidence: 0.9,
      rationale: "CLAUDE has been idle 6 min on this objective.",
      render: {
        text: "Run the webhook replay against staging and paste the v2 event counts.",
        source: "direction",
        facts: [
          { name: "project", value: "orbital-api" },
          { name: "objective", value: "Billing webhooks write v2 events on staging" },
        ],
      },
      render_status: { sendable: true, reason: "" },
    }),
    act(3, {
      verb: "draft_direction",
      source: "supervisor",
      objective_key: "docs",
      objective_title: "Limits documented for partners",
      confidence: 0.86,
      rationale: "The docs objective has had no progress for 40 minutes.",
      draft: "Document the per-key limits in docs/partners.md: the 429 body, Retry-After and the burst size.",
    }),
  ];
  return actions.map((a) => {
    const s = F.SESSIONS.find((x) => x.id === a.session_id);
    return {
      id: s.id,
      engine: s.engine,
      title: s.title,
      cwd: s.cwd,
      project: s.project,
      last_activity: s.last_mtime,
      ai_summary: s.ai_summary,
      intervention_required: true,
      intervention_reason: a.rationale,
      reviewed_at: s.reviewed_at,
      live: true,
      state: "needs_you",
      synthesis: null,
      mission_id: F.M1,
      pending_action: a,
    };
  });
}

/** A held session's screen (View screen), as the mission route returns it: terminal text. */
export function missionScreen() {
  return {
    kind: "screen",
    available: true,
    text: [
      "● Update(src/ingest/limiter.ts)",
      "  ⎿  Updated src/ingest/limiter.ts with 14 additions and 3 removals",
      "● Bash(npx vitest run limiter)",
      "  ⎿  ✓ 12 passed",
      "✻ Writing the burst-size test… (38s · esc to interrupt)",
    ].join("\n"),
  };
}
