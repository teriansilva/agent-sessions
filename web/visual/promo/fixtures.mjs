// Invented, public-safe fixture data for the promo captures (#1544).
// Every name, path and project here is made up. Nothing refers to a real host or account.
import { readFileSync } from "node:fs";
import { fileURLToPath } from "node:url";

export const NOW = Math.floor(Date.now() / 1000);

export const ROSTER = JSON.parse(
  readFileSync(fileURLToPath(new URL("../../src/test/roster.fixture.json", import.meta.url)), "utf8"),
);
// The API clients are installed in this make-believe fleet.
for (const e of ROSTER.engines) {
  if (e.id === "claude-api" || e.id === "codex-api") {
    e.present = true;
    e.supports_new = true;
    e.api = { ...e.api, unavailable_reason: null };
  }
}

export const HOME = "/home/commander/code";

export const M1 = "msn_" + "1a2b3c4d".repeat(4);
export const M2 = "msn_" + "5e6f7a8b".repeat(4);
export const M3 = "msn_" + "9c0d1e2f".repeat(4);
export const M4 = "msn_" + "3a4b5c6d".repeat(4);
export const M5 = "msn_" + "7e8f9a0b".repeat(4);

export const PROJECTS = [
  { id: "p-orbital", name: "orbital-api", color: "#f59e0b", folders: [`${HOME}/orbital-api`] },
  { id: "p-harbor", name: "harbor-web", color: "#22d3ee", folders: [`${HOME}/harbor-web`] },
  { id: "p-atlas", name: "atlas-cli", color: "#a78bfa", folders: [`${HOME}/atlas-cli`] },
];
const proj = (id) => PROJECTS.find((p) => p.id === id);
export const projRef = (id) => {
  const p = proj(id);
  return { kind: "project", id: p.id, name: p.name, color: p.color };
};

const uid = (n) => `7c1e${n}a00-5b2d-4c8e-9f31-0a6d2e8b4${String(n).padStart(3, "0")}`;

/** The fleet. `state`: working | needs | idle */
export const FLEET = [
  {
    engine: "claude", n: 1, project: "p-orbital", state: "working",
    title: "Rate-limit the public ingest API",
    summary: "Token-bucket limiter wired into the ingest router; load test running.",
    state_line: "running the k6 load test against /v1/ingest",
  },
  {
    engine: "codex", n: 2, project: "p-orbital", state: "needs",
    title: "Migrate billing webhooks to v2",
    summary: "Schema migration written; waiting for approval to run it on staging.",
    state_line: "waiting for approval to run the staging migration",
    reason: "Wants approval to run the staging migration",
    kind: "approval",
  },
  {
    engine: "opencode", n: 3, project: "p-harbor", state: "working",
    title: "Dark-mode tokens for the checkout flow",
    summary: "Replaced 41 hard-coded colours with design tokens; visual diff pending.",
    state_line: "regenerating the visual snapshots",
  },
  {
    engine: "gemini", n: 4, project: "p-harbor", state: "needs",
    title: "Flaky e2e: cart drawer focus trap",
    summary: "Found the race: focus moves before the drawer transition ends.",
    state_line: "asking which fix to keep",
    reason: "Asks which of two fixes to keep",
    kind: "choice",
  },
  {
    engine: "kimi", n: 5, project: "p-atlas", state: "working",
    title: "Shell completions for zsh and fish",
    summary: "zsh done; fish completions generated from the clap spec.",
    state_line: "testing fish completions in a clean shell",
  },
  {
    engine: "claude", n: 6, project: "p-harbor", state: "working",
    title: "Accessibility pass on the onboarding wizard",
    summary: "Labels, focus order and contrast fixed on 5 of 7 steps.",
    state_line: "fixing focus order on step 6",
  },
  {
    engine: "claude-api", n: 9, project: "p-orbital", state: "idle",
    title: "Review the auth middleware",
    summary: "Found a timing leak in the token compare; patch proposed.",
    state_line: "review posted",
  },
  {
    engine: "codex", n: 7, project: "p-orbital", state: "idle",
    title: "Postgres index audit",
    summary: "Dropped 3 unused indexes, added 2 partial ones; p95 down 38%.",
    state_line: "done — report written to docs/perf/index-audit.md",
  },
  {
    engine: "claude", n: 8, project: "p-atlas", state: "idle",
    title: "Release notes for v2.4",
    summary: "Drafted release notes from 46 merged PRs.",
    state_line: "draft ready for review",
  },
];

export const sid = (f) => `${f.engine}:${uid(f.n)}`;

export function sessionRow(f, i) {
  const p = proj(f.project);
  const t = NOW - (f.state === "idle" ? 3600 * (i + 1) : 20 + i * 45);
  return {
    id: sid(f),
    engine: f.engine,
    uuid: uid(f.n),
    short_uuid: uid(f.n).slice(0, 8),
    cwd: p.folders[0],
    project: projRef(f.project),
    last_mtime: t,
    created_at: t - 7200,
    last_output_at: f.state === "working" ? NOW - 2 : t,
    working: f.state === "working",
    running: f.state !== "idle",
    first_user_message: f.title,
    title: f.title,
    sticky: false,
    tag: "",
    archived: false,
    ai_summary: f.summary,
    ai_title: f.title,
    intervention_required: f.state === "needs",
    intervention_reason: f.reason ?? "",
    reviewed_at: NOW - 300,
    review_excluded: false,
    orchestrator_excluded: false,
    ai_recap: f.summary,
    has_draft: false,
    handoff_from: "",
    handoff_to: "",
    model_requested: null,
    mission: f.n === 1 || f.n === 2 ? { id: M1, title: "Harden the public ingest API", state: "running" } : null,
  };
}

export const SESSIONS = FLEET.map(sessionRow);

export const SESSIONS_FACETS = {
  projects: PROJECTS.map((p) => ({ ...projRef(p.id), count: FLEET.filter((f) => f.project === p.id).length })),
  engines: [...new Set(FLEET.map((f) => f.engine))],
  missions: [{ id: M1, title: "Harden the public ingest API", state: "running", count: 2 }],
  no_mission: FLEET.length - 2,
};

export function sessionsPage(rows = SESSIONS) {
  return {
    sessions: rows,
    total: rows.length,
    live_total: rows.filter((r) => r.running).length,
    next_offset: null,
    facets: SESSIONS_FACETS,
  };
}

export const CONFIG = {
  csrf: "promo",
  hostname: "command-post",
  auth_mode: "single-user",
  username: "commander",
  terminal_backend: "ws",
  onboarded: true,
  must_change_password: false,
  new_session_engines: ["claude", "codex", "opencode", "gemini", "kimi"],
  hq_contact: "us-man",
  hq_contact_rev: 1,
  theme: "dark",
  ai_review: {
    enabled: true,
    base_url: "https://api.example.com/v1",
    model: "frontier-large",
    interval_minutes: 10,
    max_input_chars: 60000,
    request_timeout: null,
    api_key_set: true,
    configured: true,
  },
  pulse: { auto_enabled: true, interval_minutes: 15, window_days: 1, scan_depth: "slow", configured: true },
  auto_sort: { enabled: false, interval_minutes: 30, confidence_min: 0.7, max_per_pass: 8, configured: true },
  analytics: { decided: true, enabled: false, available: true },
  agent_defaults: { default_engine: "claude", bypass: false, models: {} },
  default_project_id: "p-orbital",
  project_roots: [HOME],
  projects_hidden: [],
  projects_mode: "all",
  two_factor_enabled: true,
};

export function projectsList() {
  return {
    projects: PROJECTS.map((p) => ({
      ...p,
      default_folder: p.folders[0],
      archived: false,
      created_at: NOW - 86400 * 30,
      session_count: FLEET.filter((f) => f.project === p.id).length,
    })),
  };
}

export const DASH = {
  live: {
    health: "ok",
    total: FLEET.filter((f) => f.state !== "idle").length,
    working: FLEET.filter((f) => f.state === "working").length,
    by_engine: Object.fromEntries(
      [...new Set(FLEET.filter((f) => f.state !== "idle").map((f) => f.engine))].map((e) => [
        e,
        {
          live: FLEET.filter((f) => f.engine === e && f.state !== "idle").length,
          working: FLEET.filter((f) => f.engine === e && f.state === "working").length,
        },
      ]),
    ),
    rows: FLEET.filter((f) => f.state !== "idle").map((f, i) => ({
      id: sid(f),
      title: f.title,
      engine: f.engine,
      project: { id: f.project, name: proj(f.project).name },
      working: f.state === "working",
      last_activity: NOW - 30 - i * 50,
      state_line: f.state_line,
    })),
  },
  recent: {
    total: FLEET.length,
    rows: FLEET.map((f, i) => ({
      id: sid(f),
      title: f.title,
      engine: f.engine,
      project: { id: f.project, name: proj(f.project).name },
      band: f.state === "needs" ? "needs_you" : f.state === "working" ? "in_flight" : "recently_active",
      running: f.state !== "idle",
      last_activity: NOW - 60 - i * 400,
    })),
  },
};

const MENU_GEMINI = {
  engine: "gemini",
  question: "Two fixes pass locally. Which one should I keep?",
  options: [
    { n: 1, label: "Wait for transitionend before moving focus", selected: true },
    { n: 2, label: "Move focus in a requestAnimationFrame", selected: false },
  ],
};

export function needsYou() {
  const rows = FLEET.filter((f) => f.state === "needs").map((f, i) => ({
    id: sid(f),
    engine: f.engine,
    title: f.title,
    project: { id: f.project, name: proj(f.project).name },
    last_activity: NOW - 90 - i * 200,
    since: NOW - 240 - i * 600,
    reason: f.reason,
    summary: f.summary,
    flagged: true,
    kind: f.kind,
    menu: f.kind === "choice" ? MENU_GEMINI : null,
    action:
      f.kind === "choice"
        ? { id: "act-choose-1", verb: "choose", state: "proposed", option: 1, can_approve: true, can_reject: true, menu: MENU_GEMINI, confidence: 0.86, rationale: "Option 1 fixes the race at its cause." }
        : { id: "act-approve-2", verb: "answer", state: "proposed", can_approve: true, can_reject: true, confidence: 0.8, answer: "Yes — run it on staging, then report the row counts.", rationale: "Staging only; reversible." },
  }));
  return {
    rows,
    total: rows.length,
    total_unfiltered: rows.length,
    needs_you_ids: rows.map((r) => r.id),
    truncated: false,
    facets: { engines: [...new Set(rows.map((r) => r.engine))], projects: PROJECTS.map((p) => ({ id: p.id, name: p.name })) },
    window_days: 1,
  };
}

export const RECAP = {
  window_days: 1,
  source: "ai",
  generated_at: NOW - 240,
  stale: false,
  configured: true,
  error: null,
  entries: FLEET.slice(0, 5).map((f, i) => ({
    session_key: sid(f),
    ts: NOW - 600 - i * 1800,
    text: f.summary,
    engine: f.engine,
    title: f.title,
    project: { id: f.project, name: proj(f.project).name },
    session_recap: f.summary,
  })),
};

export const USAGE = {
  budgets: {},
  agents: [
    {
      engine: "claude", source: "plan",
      windows: [
        { label: "5h", used_pct: 34, resets_at: NOW + 2 * 3600 },
        { label: "weekly", used_pct: 58, resets_at: NOW + 3 * 86400 },
      ],
      at: NOW - 200, checked_at: NOW - 200, stale: false, limit_tokens: 0, manual_used: 0, used_pct: 58,
      forecast: { state: "ok", window: "weekly", resets_at: NOW + 3 * 86400, runs_out_at: null, pct_at_reset: 81, rate_per_h: 0.3 },
    },
    {
      engine: "codex", source: "plan",
      windows: [{ label: "weekly", used_pct: 71, resets_at: NOW + 2 * 86400 }],
      at: NOW - 300, checked_at: NOW - 300, stale: false, limit_tokens: 0, manual_used: 0, used_pct: 71,
      forecast: { state: "ok", window: "weekly", resets_at: NOW + 2 * 86400, runs_out_at: null, pct_at_reset: 92, rate_per_h: 0.4 },
    },
    {
      engine: "gemini", source: "plan",
      windows: [{ label: "day", used_pct: 22, resets_at: NOW + 9 * 3600 }],
      at: NOW - 400, checked_at: NOW - 400, stale: false, limit_tokens: 0, manual_used: 0, used_pct: 22,
      forecast: { state: "ok", window: "day", resets_at: NOW + 9 * 3600, runs_out_at: null, pct_at_reset: 40, rate_per_h: 2 },
    },
    {
      engine: "kimi", source: "plan",
      windows: [{ label: "weekly", used_pct: 12, resets_at: NOW + 5 * 86400 }],
      at: NOW - 500, checked_at: NOW - 500, stale: false, limit_tokens: 0, manual_used: 0, used_pct: 12,
      forecast: { state: "ok", window: "weekly", resets_at: NOW + 5 * 86400, runs_out_at: null, pct_at_reset: 30, rate_per_h: 0.1 },
    },
  ],
};

export function missionRow(over = {}) {
  return {
    id: M1,
    title: "Harden the public ingest API",
    project_id: "p-orbital",
    cwd: `${HOME}/orbital-api`,
    state: "running",
    created_at: NOW - 3 * 3600,
    updated_at: NOW - 120,
    closed_at: null,
    archived_at: null,
    outcome: null,
    session_keys: [sid(FLEET[0]), sid(FLEET[1])],
    ...over,
  };
}

export const MISSIONS = [
  missionRow(),
  missionRow({
    id: M2, title: "Ship dark mode for checkout", project_id: "p-harbor",
    cwd: `${HOME}/harbor-web`, state: "running", created_at: NOW - 5 * 3600, updated_at: NOW - 600,
    session_keys: [sid(FLEET[2])],
  }),
  missionRow({
    id: M3, title: "atlas-cli 2.4 release", project_id: "p-atlas",
    cwd: `${HOME}/atlas-cli`, state: "review", created_at: NOW - 26 * 3600, updated_at: NOW - 1800,
    session_keys: [sid(FLEET[4]), sid(FLEET[8])],
  }),
  missionRow({
    id: M5, title: "Onboarding wizard meets WCAG AA", project_id: "p-harbor",
    cwd: `${HOME}/harbor-web`, state: "running", created_at: NOW - 7 * 3600, updated_at: NOW - 240,
    session_keys: [sid(FLEET[5])],
  }),
  missionRow({
    id: M4, title: "Postgres p95 under 20 ms", project_id: "p-orbital",
    cwd: `${HOME}/orbital-api`, state: "review", created_at: NOW - 30 * 3600, updated_at: NOW - 5400,
    session_keys: [sid(FLEET[7])],
  }),
];

export function missionsList(q) {
  const state = q.get("state");
  const rows = state ? MISSIONS.filter((m) => state.split(",").includes(m.state)) : MISSIONS;
  return {
    missions: rows,
    total: rows.length,
    limit: Number(q.get("limit") ?? 50),
    offset: 0,
    facets: { projects: PROJECTS.map((p) => p.id), states: ["running", "review"] },
    store_error: null,
    snapshot: "snap",
  };
}
