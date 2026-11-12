// Per-surface mocks beyond the shell: HQ conversations, missions, library, files, structured.
import { readFileSync } from "node:fs";

import * as F from "./fixtures.mjs";
import * as MS from "./missionStates.mjs";

const json = (body, status = 200) => ({ status, body });

export function askSummary(c) {
  return {
    id: c.id,
    title: c.turns[0]?.question ?? "",
    created_at: c.created_at,
    updated_at: c.updated_at,
    unread: false,
  };
}

export function askBody(c) {
  return {
    ...askSummary(c),
    turns: c.turns,
    running: false,
    updates: [],
    following: [],
    follows_exhausted: false,
    follows: [],
    unread: false,
  };
}

export function extraSession() {
  return null;
}

export async function answerExtra(path, q, method, req, state) {
  if (path === "/api/playbooks" && method === "GET") return json(PLAYBOOKS);
  // The editor's schema (#1574 sweep): the vocabulary the step inspector offers.
  if (path === "/api/playbooks/authoring/schema" && method === "GET") return json(AUTHORING_SCHEMA);
  const pbm = /^\/api\/playbooks\/([a-z0-9-]+)(\/projects)?$/.exec(path);
  if (pbm && method === "GET") {
    const book = PLAYBOOKS.playbooks.find((b) => b.id === pbm[1]);
    if (!book) return undefined;
    if (pbm[2]) return json({ playbook_id: book.id, revision: book.revision, projects: [] });
    return json({ ...book, ...playbookDocuments(book), recovery_total: 0 });
  }
  const nyd = /^\/api\/pulse\/needs-you\/([^/]+)\/details$/.exec(path);
  if (nyd && method === "GET") {
    const id = decodeURIComponent(nyd[1]);
    const row = F.needsYou().rows.find((r) => r.id === id);
    if (!row) return undefined;
    return json({
      id,
      title: row.title,
      engine: row.engine,
      project: row.project,
      reason: row.reason,
      last_words: "Dry run is clean (4 statements). Run it on staging?",
      screen: "▲ Allow command?\n  npm run db:migrate -- --env staging\n› 1. Yes\n  2. No",
      prompt_class: "confirm",
      menu: row.menu,
      action: row.action,
    });
  }
  if (path.startsWith("/api/files/") || path.startsWith("/api/git/")) {
    const r = filesAnswer(path, q);
    if (r) return r;
  }
  if (/^\/api\/missions\/msn_[0-9a-f]{32}\/screen\/[^/]+$/.test(path)) return json(MS.missionScreen());
  // The mission shelf (#1740): `shelf` (a sweep state) fills it; otherwise it is empty.
  const sr = /^\/api\/missions\/msn_[0-9a-f]{32}\/shelf\/(shf_[0-9a-f]{32})\/raw$/.exec(path);
  if (sr) return shelfRaw(sr[1]);
  const sd = /^\/api\/missions\/msn_[0-9a-f]{32}\/shelf\/(shf_[0-9a-f]{32})\/diff$/.exec(path);
  if (sd) return json(shelfDiff(sd[1]));
  const mi = /^\/api\/missions\/(msn_[0-9a-f]{32})(\/[a-z_]+)?$/.exec(path);
  if (mi && method === "GET") {
    const [, id, sub] = mi;
    // `mission` (a sweep state, visual/promo/missionStates.mjs): another state of the same mission.
    const shown = id === ARCHIVED_MISSION ? archivedMissionDetail() : MS.missionVariant(missionDetail(id), state?.mission);
    if (!sub) return json(shown);
    if (sub === "/objectives") return json({ objectives: shown.objectives });
    if (sub === "/context") return json(MS.contextVariant(missionContext(id), state?.missionContext));
    if (sub === "/now") return json(missionNow());
    if (sub === "/debrief") return json(debriefShowcase(id, shown, state));
    if (sub === "/shelf") {
      const m = shelfManifest(id, state?.shelf ? shelfShowcase() : []);
      // A session still running has its found-in-git items to come (the dashed placeholder).
      if (state?.shelf) m.found_pending = [SHELF_B];
      return json(m);
    }
    return undefined;
  }
  const st = /^\/api\/structured\/sessions\/([^/]+?)(\/[a-z]+)?$/.exec(path);
  if (st) {
    const key = decodeURIComponent(st[1]);
    const snap = structuredSnapshot(key);
    // `bypassOffer` (a sweep state): the session may switch Skip permissions on, which asks first.
    if (state?.bypassOffer) Object.assign(snap, { can_enable_bypass: true, bypass: false, permission_change: {} });
    if (st[2] === "/events")
      return json({ session_key: key, revision: snap.revision, next_cursor: snap.event_cursor, events: [] });
    if (!st[2]) return json(snap);
    return undefined;
  }
  return undefined;
}

// ---- API (structured) session ---------------------------------------------------------------
const API_REPLY = [
  "## Review: `src/auth/middleware.ts`",
  "",
  "Two findings, one of them worth fixing today.",
  "",
  "1. **Timing leak in the token check (high).** `token === expected` returns as soon as a byte differs, so response time leaks how much of a guessed token is right. Use a constant-time compare.",
  "2. **Missing `Vary: Authorization` (low).** A shared cache could serve one caller's 401 to another.",
  "",
  "```ts",
  "import { timingSafeEqual } from \"node:crypto\";",
  "",
  "const a = Buffer.from(token);",
  "const b = Buffer.from(expected);",
  "if (a.length !== b.length || !timingSafeEqual(a, b)) {",
  "  return res.status(401).set(\"Vary\", \"Authorization\").end();",
  "}",
  "```",
  "",
  "| Check | Before | After |",
  "|---|---|---|",
  "| `npm test -- auth` | 41 passed | 43 passed |",
  "| timing spread (10k req) | 0.21 ms | 0.004 ms |",
  "",
  "Want me to open this as a patch on a branch?",
].join("\n");

export function structuredSnapshot(key) {
  const T = F.NOW - 900;
  return {
    session_key: key,
    revision: 6,
    event_cursor: 6,
    cwd: `${F.HOME}/orbital-api`,
    state: "idle",
    active_turn: null,
    model_requested: "claude-opus-5",
    model_effective: "claude-opus-5",
    turns: [
      {
        turn_id: "21111111-2222-4333-8444-555555555555",
        operation_id: "21111111-2222-4333-8444-555555555555",
        state: "completed",
        text: "Review src/auth/middleware.ts for security problems. Be specific and propose a fix.",
        reply: API_REPLY,
        text_truncated: false,
        reply_truncated: false,
        reason: null,
        tools: [
          { id: "t1", name: "Read", outcome: "ok", summary: "src/auth/middleware.ts" },
          { id: "t2", name: "Grep", outcome: "ok", summary: "\"=== expected\" in src/" },
          { id: "t3", name: "Bash", outcome: "ok", summary: "npm test -- auth" },
        ],
        tools_truncated: false,
        created_at: T,
        completed_at: T + 74,
      },
    ],
    omitted_turns: 0,
    pending_requests: [],
    native: { native_id: "n1", worker: "w1", background_active: false },
    read_only: null,
  };
}

// ---- HQ: the General's briefing ------------------------------------------------------------
export const HQ_ANSWER = [
  "Two things, Commander, and the first one can't wait.",
  "",
  "**Codex is parked on the billing migration.** Dry run's clean — it just wants your say-so to run it on staging, and I'd give it. **Gemini cornered that flaky cart test**; tell it to keep the `transitionend` fix and we're home by supper.",
  "",
  "The other four are marching in step.",
].join("\n");

export function askStream(cid, req, state) {
  const body = req.postDataJSON();
  const c = (state.ask ?? []).find((x) => x.id === cid);
  const codex = F.SESSIONS[1];
  const gemini = F.SESSIONS[3];
  const turn = {
    turn_id: body.turn_id,
    question: body.text,
    asked_at: F.NOW - 20,
    status: "done",
    answer: HQ_ANSWER,
    detail: null,
    code: null,
    // `hqMatches`: false = none; "several" = three terminal sessions (the map's "Open all" batch,
    // #1573's refused-windows capture); otherwise the one session waiting on an approval.
    matches:
      state.hqMatches === false
        ? []
        : state.hqMatches === "several"
          ? F.SESSIONS.slice(0, 3).map((s) => ({ id: s.id, title: s.title, why: "Working on the ingest launch." }))
          : [{ id: codex.id, title: codex.title, why: "Waiting on your approval to run the staging migration." }],
    mission_matches: [],
    tools: [],
    proposals: [
      {
        action_id: "b".repeat(32),
        kind: "nudge",
        key: codex.id,
        title: codex.title,
        text: "Approved. Run the migration on staging, then report the row counts before and after.",
        state: "proposed",
        projection: "actionable",
        can_approve: true,
        can_reject: true,
        detail: "",
      },
    ],
    answers: null,
    contact: body.contact ?? "us-man",
  };
  if (c) {
    c.turns = [...c.turns, turn];
    c.updated_at = F.NOW - 20;
  }
  const lines = [{ type: "turn", status: 200, turn }];
  return {
    raw: {
      status: 200,
      contentType: "application/x-ndjson",
      body: lines.map((l) => JSON.stringify(l)).join("\n") + "\n",
    },
  };
}

// ---- Missions ---------------------------------------------------------------------------------
const MT = F.NOW - 3 * 3600;
const OBJ = [
  ["limit", "Over-quota keys get 429 + Retry-After", "supervisor_judged", "met", 5400],
  ["load", "Load test holds 2k rps with p95 under 50 ms", "supervisor_judged", "met", 1500],
  ["webhooks", "Billing webhooks write v2 events on staging", "supervisor_judged", "pending", null],
  ["checks", "CI checks are green on the pull request", "forge_checks", "pending", null],
  ["docs", "Limits documented for partners", "supervisor_judged", "pending", null],
];

export function missionObjectives(id) {
  return OBJ.map(([key, title, probe, st, ago], i) => ({
    mission_id: id,
    key,
    ord: i,
    title,
    probe,
    probe_args: null,
    gate: true,
    state: st,
    met_at: ago ? F.NOW - ago : null,
    observed: ago
      ? {
          at: F.NOW - ago,
          detail: key === "load" ? "k6: 2,031 rps, p95 41.7 ms over 60 s" : "429 + Retry-After verified in limiter.test.ts",
          value: true,
          judged: {
            met: true,
            confidence: 0.94,
            threshold: 0.9,
            evidence: [
              {
                source: `transcript:${F.SESSIONS[0].id}`,
                quote: key === "load" ? "http_req_duration: avg=18.2ms p(95)=41.7ms" : "✓ answers 429 with Retry-After when empty",
              },
            ],
            fingerprint: "f",
            checked_at: F.NOW - ago,
          },
        }
      : key === "checks"
        ? { at: F.NOW - 240, detail: "2 of 3 checks green · e2e running", value: false }
        : null,
    source: "playbook",
  }));
}

function ev(seq, ago, kind, text, extra = {}) {
  return { seq, mission_id: F.M1, at: F.NOW - ago, kind, session_key: null, action_id: null, text, meta: null, settlement: null, ...extra };
}

export function missionDetail(id) {
  const row = F.MISSIONS.find((m) => m.id === id) ?? F.MISSIONS[0];
  return {
    ...row,
    instruction:
      "Harden the public ingest API before the partner launch: per-key rate limits, billing webhooks on the v2 schema, and p95 under 50 ms at 2k rps.",
    brief: "Partner launch readiness for the ingest API.",
    engine: "claude",
    engine_source: "plan",
    playbook_id: "ship-it",
    archiving_at: null,
    unarchiving_at: null,
    objectives_state: "done",
    objectives_at: MT + 30,
    plan_state: "ready",
    plan_generation: 1,
    plan_detail: null,
    plan_at: MT + 40,
    sessions: row.session_keys.map((k, i) => ({ mission_id: id, session_key: k, role: i === 0 ? "lead" : null, added_at: MT + 60 + i * 30, removed_at: null })),
    objectives: missionObjectives(id),
    events: [
      ev(1, 3 * 3600, "operator_msg", "Harden the public ingest API before the partner launch: per-key rate limits, billing webhooks on the v2 schema, and p95 under 50 ms at 2k rps."),
      ev(2, 3 * 3600 - 40, "assistant_msg", "Plan ready. Claude takes the rate limiter and the load test in orbital-api; Codex migrates the billing webhooks to v2. Five objectives, all gating. I'll keep watch and only call you when a decision is yours."),
      ev(3, 3 * 3600 - 60, "state", "Mission launched — 2 sessions", { meta: { from: "dispatching", to: "running" } }),
      ev(4, 5400, "assistant_msg", "Limiter is in and its tests are green. Objective 1 met — quoted from the session: “✓ answers 429 with Retry-After when empty”."),
      ev(5, 1500, "assistant_msg", "Load test held 2,031 rps with p95 at 41.7 ms. Objective 2 met."),
      ev(6, 600, "operator_msg", "Good. Codex can run the migration on staging once the dry run is clean."),
      ev(7, 240, "assistant_msg", "Codex's dry run is clean (4 statements). It is asking to run the migration on staging — approved per your last message, sending now."),
    ],
    events_next_seq: null,
  };
}

export function missionContext(id) {
  return {
    id,
    project_id: "p-orbital",
    cwd: `${F.HOME}/orbital-api`,
    sessions: F.SESSIONS.slice(0, 2).map((s) => ({ mission_id: id, session_key: s.id, role: null, added_at: F.NOW - 3 * 3600, removed_at: null })),
    git: GIT_STATUS,
    git_error: null,
  };
}

export function missionNow() {
  return {
    sessions: [
      { session_key: F.SESSIONS[0].id, status: "producing", seconds_since_output: 2, prompt_class: null, recap_age_s: 60, recap_older_than_output: false },
      { session_key: F.SESSIONS[1].id, status: "at_prompt", seconds_since_output: 210, prompt_class: "confirm", recap_age_s: 120, recap_older_than_output: false },
    ],
    checked_at: F.NOW,
  };
}

// ---- Library: playbooks ------------------------------------------------------------------------
const MODEL = { claude: "opus", codex: "gpt-5-codex", gemini: "gemini-2.5-pro" };
const step = (id, title, kind, engine, after = []) => ({
  id, title, actor: kind === "agent" ? { kind, engine, ...(MODEL[engine] ? { model: MODEL[engine] } : {}) } : { kind, label: kind === "operator" ? "Commander" : undefined }, after, note: false,
});
function pb(id, name, domain, summary, steps, over = {}) {
  return {
    gallery_origin: null,
    id, ok: true, error: null, source: "local", editable: true, revision: id.padEnd(64, "0").slice(0, 64),
    default: false, name, publisher: "Command staff", version: "1.2.0", domain, summary,
    default_flow: "main",
    flows: [{ id: "main", title: "Main", steps }],
    ships: { materials: 2, runbooks: 1, templates: 3, variables: 4 },
    requires: { binaries: [], connections: [] },
    connections: [],
    capabilities: [],
    ...over,
  };
}
export const PLAYBOOKS = {
  playbooks: [
    pb("ship-it", "Ship it", "development", "Plan, build, review and land a change behind green CI — the default for feature work.", [
      step("plan", "Plan the change", "agent", "claude"),
      step("build", "Build and test", "agent", "codex", ["plan"]),
      step("review", "Independent review", "agent", "claude", ["build"]),
      step("land", "Approve and merge", "operator", null, ["review"]),
    ], { default: true }),
    pb("bug-hunt", "Bug hunt", "development", "Reproduce first, fix second: a failing test before any change, then the fix and a regression guard.", [
      step("repro", "Reproduce with a failing test", "agent", "gemini"),
      step("fix", "Fix the cause", "agent", "claude", ["repro"]),
      step("guard", "Add the regression guard", "agent", "codex", ["fix"]),
    ]),
    pb("perf-sweep", "Performance sweep", "operations", "Profile the hot path, change one thing at a time, and prove every gain with a before/after number.", [
      step("profile", "Profile under load", "agent", "codex"),
      step("tune", "Tune one change at a time", "agent", "claude", ["profile"]),
      step("prove", "Before / after report", "agent", "kimi", ["tune"]),
    ]),
    pb("a11y-pass", "Accessibility pass", "design", "Keyboard, screen reader and contrast audit against WCAG AA, with real-browser proof for every fix.", [
      step("audit", "Audit every screen", "agent", "opencode"),
      step("fix", "Fix by severity", "agent", "claude", ["audit"]),
      step("verify", "Verify in a real browser", "agent", "gemini", ["fix"]),
    ]),
    pb("release-train", "Release train", "operations", "Changelog from merged work, version bump, tagged build and release notes for the Commander to sign off.", [
      step("notes", "Draft release notes", "agent", "claude"),
      step("bump", "Bump and tag", "agent", "codex", ["notes"]),
      step("signoff", "Commander signs off", "operator", null, ["bump"]),
    ], { source: "bundled", editable: false }),
    pb("dep-bumps", "Dependency bumps", "maintenance", "Batch the week's dependency updates, read every changelog, and split anything risky into its own PR.", [
      step("collect", "Collect updates", "agent", "kimi"),
      step("upgrade", "Upgrade and test", "agent", "codex", ["collect"]),
    ], { source: "bundled", editable: false }),
  ],
  default: "ship-it",
  recovery_total: 0,
};

// ---- Files / Git -------------------------------------------------------------------------------
const REPO = `${F.HOME}/orbital-api`;
export const GIT_STATUS = {
  repo: REPO,
  branch: "feat/ingest-limits",
  head: "4f1c2e9".padEnd(40, "0"),
  upstream: "origin/feat/ingest-limits",
  ahead: 2,
  behind: 0,
  truncated: false,
  entries: [
    { path: "src/ingest/limiter.ts", index: "A", worktree: ".", kind: "staged", oid: "a1" },
    { path: "src/ingest/router.ts", index: ".", worktree: "M", kind: "changed", oid: "b2" },
    { path: "src/ingest/limiter.test.ts", index: ".", worktree: "M", kind: "changed", oid: "c3" },
    { path: "docs/ingest-limits.md", index: "?", worktree: "?", kind: "untracked", oid: null },
    { path: "load/ingest.js", index: "?", worktree: "?", kind: "untracked", oid: null },
  ],
};
const ROUTER_DIFF = [
  "@@ -1,9 +1,10 @@",
  ' import { Router } from "express";',
  ' import { validate } from "./schema";',
  '+import { limiter } from "./limiter";',
  ' import { ingest } from "./handler";',
  " ",
  " export const router = Router();",
  " ",
  '-router.post("/v1/ingest", validate, ingest);',
  '+router.post(',
  '+  "/v1/ingest",',
  "+  limiter({ rate: 50, burst: 100, key: (req) => req.apiKey }),",
  "+  validate,",
  "+  ingest,",
  "+);",
  ' router.get("/v1/health", (_req, res) => res.json({ ok: true }));',
  "@@ -24,7 +29,9 @@ export function mount(app) {",
  '   app.use("/", router);',
  "-  app.set(\"trust proxy\", true);",
  "+  // Only the edge proxy may set X-Forwarded-For; the limiter keys on the API key,",
  "+  // never on the client address.",
  "+  app.set(\"trust proxy\", 1);",
  " }",
].join("\n");

const DIRS = {
  [REPO]: [
    ["docs", "dir"], ["load", "dir"], ["src", "dir"], ["test", "dir"],
    [".gitignore", "file", 120], ["package.json", "file", 1840], ["README.md", "file", 4210], ["tsconfig.json", "file", 512],
  ],
};

export function filesAnswer(path, q) {
  if (path === "/api/files/capabilities") return json({ ok: true, reason: "" });
  if (path === "/api/files/list") {
    const dir = q.get("path") || REPO;
    const items = DIRS[dir] ?? DIRS[REPO];
    return json({
      path: dir, parent: F.HOME, root: F.HOME, total: items.length, complete: true, truncated: false,
      entries: items.map(([name, kind, size]) => ({ name, path: `${dir}/${name}`, kind, size: size ?? 4096, mtime: F.NOW - 3600 })),
    });
  }
  if (path === "/api/files/read")
    return json({ path: q.get("path"), size: 200, binary: false, content: "export {};\n", truncated: false });
  if (path === "/api/git/status") return json(GIT_STATUS);
  if (path === "/api/git/diff")
    return json({ path: q.get("path") ?? "src/ingest/router.ts", repo: REPO, diff: ROUTER_DIFF, added: 10, removed: 2, truncated: false, binary: false, too_large: false, conflict: false });
  if (path === "/api/git/log")
    return json({
      repo: REPO, branch: "feat/ingest-limits", head: "4f1c2e9".padEnd(40, "0"),
      commits: [
        ["4f1c2e9", "feat(ingest): k6 load test for /v1/ingest", 1800, false],
        ["9b07d31", "feat(ingest): token-bucket limiter keyed by API key", 4200, false],
        ["e21a5c4", "test(ingest): limiter refill and 429 cases", 4500, true],
        ["77c0b18", "chore: bump express to 5.1", 86400, true],
      ].map(([short, subject, ago, pushed]) => ({ sha: short.padEnd(40, "0"), short, subject, author: "commander", time: F.NOW - ago, parents: 1, pushed })),
    });
  if (path === "/api/git/branches")
    return json({ repo: REPO, current: "feat/ingest-limits", local: ["main", "feat/ingest-limits"], remote: ["origin/main", "origin/feat/ingest-limits"] });
  if (path === "/api/git/push-target")
    return json({ ok: true, reason: null, branch: "feat/ingest-limits", remote: "origin", target: "origin/feat/ingest-limits", expect: "x", candidates: [], set_upstream: false });
  return undefined;
}

// ---- Sweep states (#1531): data the NIGHTJAR sweep needs to reach a popup, opt-in per capture ----
export const ARCHIVED_MISSION = "msn_" + "0f1e2d3c".repeat(4);

function archivedRow() {
  return F.missionRow({
    id: ARCHIVED_MISSION,
    title: "Legacy ingest v1 sunset",
    state: "done",
    closed_at: F.NOW - 7200,
    archived_at: F.NOW - 900,
  });
}

export function archivedMissionsList() {
  const row = archivedRow();
  return { ...F.missionsList(new URLSearchParams()), missions: [row], total: 1 };
}

export function archivedMissionDetail() {
  return { ...missionDetail(F.MISSIONS[0].id), ...archivedRow(), sessions: [], events: [] };
}

export function disabledAutomation() {
  const now = F.NOW;
  return {
    id: "a-nightly-audit",
    name: "Nightly dependency audit",
    trigger: { kind: "schedule", cadence: { kind: "daily", time: "03:00" }, tz: "UTC" },
    action: { kind: "start_mission", project_id: F.PROJECTS[0].id, instruction: { text: "Audit the dependencies and open one PR per risky bump." }, checklist_id: null, autonomy: "propose" },
    policy: { concurrency: "skip", max_concurrent: 1, max_runs_per_day: 48, pause_after_failures: 3, expires_at: null },
    revision: 1,
    state: "off",
    enabled: false,
    paused: false,
    paused_reason: "",
    needs_reapproval: false,
    reapproval_reason: "",
    consented_at: null,
    consented_scope: null,
    scope: { version: 1 },
    scope_digest: "digest-1",
    scope_lines: [
      "Starts a mission in orbital-api every day at 03:00 UTC.",
      "The mission proposes its plan; nothing runs until you approve it.",
      "At most 48 runs a day, one at a time; pauses after 3 failures.",
    ],
    consecutive_failures: 0,
    check_note: "",
    next_run: null,
    last_run: null,
    stats: { ok: 0, failed: 0, skipped: 0, pending: 0, runs: 0, success_rate: null },
    strip: Array.from({ length: 14 }, (_, i) => ({ date: new Date((now - (13 - i) * 86400) * 1000).toISOString().slice(0, 10), worst: null })),
    created_at: now - 3 * 86400,
    updated_at: now - 86400,
  };
}

export const TEMPLATES = {
  templates: [
    {
      id: "tpl_release",
      name: "Release checklist",
      description: "Cut and verify a release",
      tags: ["release"],
      body: "Cut the release, run the smoke tests, post the notes.",
      fields: [],
      images: [],
      created_at: F.NOW - 86400,
      updated_at: F.NOW - 3600,
      used_count: 4,
      last_used_at: F.NOW - 7200,
    },
    {
      id: "tpl_review",
      name: "Security review",
      description: "Review a file for security problems and propose a fix",
      tags: ["review"],
      body: "Review the changed files for security problems. Be specific and propose a fix.",
      fields: [],
      images: [],
      created_at: F.NOW - 5 * 86400,
      updated_at: F.NOW - 2 * 86400,
      used_count: 9,
      last_used_at: F.NOW - 3 * 3600,
    },
  ],
  limits: { templates_max: 200, name_max: 80, description_max: 280, tags_max: 8, body_max: 20000, fields_max: 20, label_max: 60, default_max: 2000, images_max: 4, image_suffixes: [".png", ".jpg"] },
};

export function fsDirs(q) {
  const path = q.get("path") || `${F.HOME}`;
  const names = path === F.HOME ? ["orbital-api", "harbor-web", "atlas-cli"] : ["src", "docs", "tests"];
  return { path, home: "/home/commander", dirs: names.map((n) => ({ name: n, path: `${path}/${n}` })) };
}

// ---- PR4b library A (#1574): memory, variables and suggestions for the sweep ------------------
// Opt-in mock states (`mock: { memory: true }` …). Every value is invented; the one secret is
// stored and never carries a value, as the server's secret rows never do.
const MEM_PID = "p-mem1a2b";
const memSession = (engine, n) => ({
  key: `${engine}:${String(n).repeat(8)}-${String(n).repeat(4)}-4${String(n).repeat(3)}-8${String(n).repeat(3)}-${String(n).repeat(12)}`,
  engine,
  kind: "terminal",
  resolved: true,
});
function memoryView(state) {
  return {
    project: { id: MEM_PID, name: "orbital-api", color: "" },
    enabled: !state.memoryOff,
    notes: state.memoryOff
      ? []
      : [
          { slug: "typed-ids", name: "Scopes are typed ids", description: "never display names", type: "decision", body: "Use the project entity id everywhere. See [[inbox-nofollow]].", size: 58, created: F.NOW - 600, updated: F.NOW - 240, session: memSession("claude", 1), links: ["inbox-nofollow"] },
          { slug: "inbox-nofollow", name: "Inbox files: O_NOFOLLOW", description: "refuse symlinks", type: "pitfall", body: "Open every inbox file with O_NOFOLLOW.", size: 40, created: F.NOW - 1200, updated: F.NOW - 1200, session: memSession("codex", 2), links: [] },
          { slug: "ci-pr-validate", name: "pr-validate takes 20–40 minutes", description: "that is normal", type: "finding", body: "A slow pr-validate run is the shared runner, not a wedge.", size: 56, created: F.NOW - 9000, updated: F.NOW - 9000, session: memSession("claude", 3), links: ["typed-ids"] },
          { slug: "deploy-verify", name: "Deploy, then verify live", description: "the stamp, a deep route, a 404", type: "reference", body: "A green deploy is not a ship: check the stamp is live.", size: 52, created: F.NOW - 20000, updated: F.NOW - 20000, session: memSession("gemini", 4), links: [] },
        ],
    pending: [{ filename: "paste-gotcha.md", size: 1200, at: F.NOW - 5, session: memSession("claude", 1) }],
    refused: [{ filename: "whole-transcript.md", reason: "larger than 4 KiB", size: 61000, at: F.NOW - 60, session: memSession("codex", 2) }],
    quarantine: [{ id: 7, filename: "deploy-notes.md", reason: "looks like a credential", size: 90, at: F.NOW - 30, masked: "████████ (withheld)", session: memSession("claude", 3) }],
    withheld: { notes: 0, reason: "" },
    instructions:
      "If `$BATTLELAB_MEMORY` is set, read `$BATTLELAB_MEMORY/MEMORY.md` before a task and open the notes it needs. To leave a note for later sessions, write one short `.md` file into `$BATTLELAB_MEMORY_INBOX`.",
    limits: { note_bytes: 4096, notes: 200, inbox_files: 20 },
  };
}
const memoryImport = {
  sources: [
    { id: "s-here", engine: "claude", label: "Claude Code", path: "~/.claude/projects/-home-commander-code-orbital-api/memory", relation: "this folder", files: 41, over_agent_size: 2, over_cap: 0, beyond_max_files: 0, state: "ok", reason: "" },
  ],
  last: null,
  held: [],
};
export function memoryAnswer(path, method, state) {
  const v = memoryView(state);
  if (path === "/api/memory" && method === "GET")
    return {
      projects: [
        { id: MEM_PID, name: "orbital-api", color: "", enabled: v.enabled, notes: v.notes.length, pending: v.pending.length, quarantined: v.quarantine.length, last_write: F.NOW - 240 },
        { id: "p-mem9z8y", name: "harbor-web", color: "", enabled: false, notes: 0, pending: 0, quarantined: 0, last_write: null },
      ],
    };
  if (path === `/api/memory/${MEM_PID}` && method === "GET") return v;
  if (path === `/api/memory/${MEM_PID}/import` && method === "GET") return memoryImport;
  if (path === `/api/memory/${MEM_PID}/ingest`) return { notes: 0, quarantined: 0, refused: 0, dropped: 0 };
  return null;
}

export const VARIABLES_LIB = {
  variables: [
    { id: "var_branch", name: "release_branch", kind: "text", definition: { value_type: "text", label: "Release branch", help: "The branch a release is cut from." }, set: true, value: "main", created_at: F.NOW - 86400 * 9, updated_at: F.NOW - 86400 },
    { id: "var_token", name: "deploy_token", kind: "secret", definition: { value_type: "text", label: "Deploy token" }, set: true, created_at: F.NOW - 86400 * 7, updated_at: F.NOW - 86400 * 2 },
    { id: "var_reviewer", name: "reviewer", kind: "text", definition: { value_type: "enum", label: "Reviewer", choices: ["hermes", "operator", "pair"] }, set: false, created_at: F.NOW - 86400 * 4, updated_at: F.NOW - 86400 * 4 },
    { id: "var_region", name: "staging_region", kind: "text", definition: { value_type: "text", label: "Staging region" }, set: true, value: "eu-central", created_at: F.NOW - 86400 * 3, updated_at: F.NOW - 86400 * 3 },
  ],
  collections: [
    { id: "col_release", name: "Release", variable_ids: ["var_branch", "var_token"], created_at: F.NOW - 86400 * 9, updated_at: F.NOW - 86400 },
    { id: "col_review", name: "Review", variable_ids: ["var_reviewer"], created_at: F.NOW - 86400 * 4, updated_at: F.NOW - 86400 * 4 },
  ],
  migration_required: false,
  limits: { variables_max: 200, value_max: 4096, name_max: 64, secret_min: 8, collections_max: 20 },
};

export const SUGGESTIONS = {
  result: {
    generated_at: F.NOW - 600,
    stats: { messages: 212, distinct: 90, sessions: 41, withheld: 2, days: 30 },
    dropped: 1,
    suggestions: [
      { id: "aaaaaaaaaaaaaaaa", kind: "template", name: "Fix review notes", reason: "You sent a variant of this 17 times.", count: 17, body: "Read the latest review on {{pr}}, fix every note, push.", fields: [{ name: "pr", label: "PR", default: "" }] },
      { id: "bbbbbbbbbbbbbbbb", kind: "variable", name: "staging_host", reason: "Appears in 23 messages.", count: 23, value: "staging.orbital.test", secret: false },
      { id: "cccccccccccccccc", kind: "variable", name: "deploy_token", reason: "Looks like a credential you paste.", count: 4, value: "", secret: true },
    ],
  },
  configured: true,
};
// ---- the automations showcase (#1574, sweep state `automationsShowcase`) ----------------------
// Three automations as the Library B board draws them: one enabled with a run history (a failed
// run and an ok one), one that waits for your consent, one that is off — so the list's LED rows,
// the ⋯ menu, the runs page and the editor all have something to show.
function strip14(worst) {
  return Array.from({ length: 14 }, (_, i) => ({
    date: new Date((F.NOW - (13 - i) * 86400) * 1000).toISOString().slice(0, 10),
    worst: worst(i),
  }));
}
function showcaseBase(over) {
  return {
    ...disabledAutomation(),
    consented_at: F.NOW - 5 * 86400,
    consented_scope: { version: 1 },
    ...over,
  };
}
export function automationsShowcase() {
  const audit = showcaseBase({
    id: "a-nightly-audit",
    name: "Nightly dependency audit",
    state: "enabled",
    enabled: true,
    next_run: { slot: "tomorrow", at: F.NOW + 9 * 3600 },
    last_run: automationRuns("a-nightly-audit").runs[1],
    stats: { ok: 24, failed: 2, skipped: 0, pending: 0, runs: 26, success_rate: 0.92 },
    strip: strip14((i) => (i === 9 ? "failed" : i % 3 === 1 ? null : "ok")),
  });
  const notes = showcaseBase({
    id: "a-release-notes",
    name: "Weekly release notes draft",
    trigger: { kind: "schedule", cadence: { kind: "weekly", days: ["mon"], time: "09:00" }, tz: "Europe/Berlin" },
    action: { kind: "start_session", engine: "claude", folder: `${F.PROJECTS[0].folders[0]}`, message: { text: "Draft the release notes for everything merged since the last tag. Do not publish." }, bypass: false },
    state: "needs_reapproval",
    enabled: true,
    paused: true,
    needs_reapproval: true,
    reapproval_reason: "its scope changed since you approved it",
    scope_lines: [
      `Starts a claude session in ${F.PROJECTS[0].folders[0]}`,
      "Permission bypass: off",
      "Message: Draft the release notes for everything merged since the last tag. Do not publish.",
    ],
    strip: strip14(() => null),
  });
  const prune = showcaseBase({
    id: "a-prune-checkouts",
    name: "Prune merged checkouts",
    trigger: { kind: "manual" },
    state: "off",
    enabled: false,
    consented_at: null,
    consented_scope: null,
  });
  return [audit, notes, prune];
}
export function automationRuns(id) {
  const step = (seq, s, detail, dt, t) => ({ seq, at: t + dt, step: s, detail });
  const failedAt = F.NOW - 4 * 3600;
  const okAt = F.NOW - 28 * 3600;
  const runs = [
    {
      id: "run-26",
      automation_id: id,
      trigger: "schedule",
      slot: "2026-10-22T03:00",
      fire_at: failedAt,
      catch_up: false,
      covered: 1,
      state: "done",
      outcome: "failed",
      result_class: "failed",
      reason: "npm audit could not reach the registry",
      mission_id: null,
      session_key: null,
      created_at: failedAt,
      finished_at: failedAt + 240,
      inputs: { instruction: "Audit the dependencies and open one PR per risky bump." },
      scope: {},
      steps: [
        step(1, "claimed", "slot 03:00", 0, failedAt),
        step(2, "creating_mission", F.PROJECTS[0].id, 1, failedAt),
        step(3, "failed", "npm audit could not reach the registry", 240, failedAt),
      ],
    },
    {
      id: "run-25",
      automation_id: id,
      trigger: "schedule",
      slot: "2026-10-21T03:00",
      fire_at: okAt,
      catch_up: false,
      covered: 1,
      state: "done",
      outcome: "ok",
      result_class: "ok",
      reason: "the mission proposed its plan",
      mission_id: "msn_" + "1a2b3c4d".repeat(4),
      session_key: null,
      created_at: okAt,
      finished_at: okAt + 180,
      inputs: { instruction: "Audit the dependencies and open one PR per risky bump." },
      scope: {},
      steps: [
        step(1, "claimed", "slot 03:00", 0, okAt),
        step(2, "creating_mission", F.PROJECTS[0].id, 1, okAt),
        step(3, "mission_created", "msn_" + "1a2b3c4d".repeat(4), 2, okAt),
        step(4, "ok", "the mission proposed its plan", 180, okAt),
      ],
    },
  ];
  return { runs, total: 26 };
}
export const AUTOMATION_ENGINES = [
  { id: "claude", ok: true, reason: "" },
  { id: "codex", ok: true, reason: "" },
];
export const AUTOMATION_LIMITS = {
  interval_min_minutes: 15,
  max_runs_per_day_max: 48,
  max_concurrent_max: 4,
  pause_after_failures_max: 10,
  triggers: ["once", "schedule", "manual"],
  actions: ["start_mission", "start_session", "send_to_session"],
  autonomy: ["propose", "dispatch", "dispatch_auto_choose"],
  timezone: "Europe/Berlin",
};

// ---- the gallery compare (#1574, sweep state `galleryCompare`) --------------------------------
// A local skill copied from the gallery at v1.0.0, and the gallery now publishing v2.0.0: the
// Review update page runs the three-way compare and shows one conflicting file.
const GC_DIGEST = "a".repeat(64);
const GC_REVISION = "b".repeat(64);
const GC_CONTENT = {
  format: 1,
  kind: "skill",
  id: "review-checklist",
  version: "2.0.0",
  title: "Review checklist",
  author: "BattleLab",
  publisher: "BattleLab",
  summary: "A short checklist an agent follows before it calls a change done.",
  source: "https://example.com/review-checklist",
  requirements: ["Read the change first"],
  files: {
    "SKILL.md": "# Review\nRead the change, run the tests, and report concrete findings.",
    "notes/example.md": "Supporting notes",
  },
};
export const GALLERY_COMPARE_DIGEST = GC_DIGEST;
export function galleryListing() {
  return {
    state: "ready",
    entries: [{ content: GC_CONTENT, digest: GC_DIGEST, available: true }],
    sequence: 1,
    digest: "e".repeat(64),
    expires_at: F.NOW + 86400 * 365,
    error: null,
  };
}
// `galleryShowcase` (#1574 G1): the browse page with three skills, as the board draws it.
export function galleryShowcase() {
  const entry = (id, version, title, summary, d) => ({
    content: { ...GC_CONTENT, id, version, title, summary },
    digest: d.repeat(64),
    available: true,
  });
  return {
    ...galleryListing(),
    entries: [
      entry("bug-hunt", "0.5.0", "Bug hunt", "Reproduce first, fix second, add a regression guard.", "1"),
      entry("performance-sweep", "1.0.0", "Performance sweep", "Profile, change one thing at a time, prove it.", "2"),
      entry("docs-pass", "2.0.0", "Docs pass", "Bring the docs in line with what shipped.", "3"),
    ],
  };
}
export function galleryContent() {
  return {
    entry: { content: GC_CONTENT, digest: GC_DIGEST, available: true },
    importable: true,
    reason: null,
    cut_digest: "e".repeat(64),
    preview: null,
  };
}
export function localSkill() {
  return {
    id: "my-skill",
    revision: GC_REVISION,
    files: {
      "SKILL.md": "# Review\nRead the change and report concrete findings. Keep it short.",
      "notes/example.md": "Supporting notes",
    },
    gallery_origin: {
      state: "verified",
      kind: "skill",
      title: GC_CONTENT.title,
      id: GC_CONTENT.id,
      version: "1.0.0",
      publisher: GC_CONTENT.publisher,
      source: GC_CONTENT.source,
      entry_digest: GC_DIGEST,
      base_revision: GC_REVISION,
      pointer: "d".repeat(32),
    },
    editable: true,
  };
}
export function galleryComparison() {
  const local = localSkill();
  return {
    kind: "skill",
    id: local.id,
    revision: local.revision,
    origin: local.gallery_origin.pointer,
    incoming_digest: GC_DIGEST,
    comparison: local.revision,
    base: { "SKILL.md": "# Review\nRead the change and report concrete findings.", "notes/example.md": "Supporting notes" },
    local: local.files,
    incoming: GC_CONTENT.files,
    proposed: local.files,
    conflicts: ["SKILL.md"],
    changed_files: ["SKILL.md"],
    incoming_version: "2.0.0",
    base_version: "1.0.0",
  };
}

// ---- the playbook editor (#1574 sweep) ----------------------------------------------------------
// What GET /api/playbooks/<id> carries for the editor: the parsed manifest and flow, and the files.
export const AUTHORING_SCHEMA = {
  agents: ["claude", "codex", "gemini", "kimi", "opencode"],
  actors: ["agent", "external", "none", "operator"],
  memory: ["none", "read", "read-write"],
  distinct: ["session", "engine", "model"],
  variable_types: ["text", "url", "enum", "bool", "int", "path"],
  probes: { supervisor_judged: { outputs: [], args: {} }, none: { outputs: [], args: {} } },
  limits: { flows: 20, steps: 30, items: 12, variables: 32, distinct: 8, rework_min: 1, rework_max: 10 },
};
function playbookDocuments(book) {
  const manifest = {
    format: 2,
    identity: { id: book.id, name: book.name, publisher: book.publisher, version: book.version, domain: book.domain },
    flows: { default: "main" },
  };
  const steps = book.flows[0].steps.map((st, i, all) => ({
    id: st.id,
    title: st.title,
    actor: st.actor.kind === "agent" ? { kind: "agent", engine: st.actor.engine, ...(st.actor.model ? { model: st.actor.model } : {}) } : { kind: st.actor.kind },
    ...(st.after.length ? { after: st.after } : {}),
    checklist: [{ key: `${st.id}-done`, title: `${st.title} — done`, probe: "supervisor_judged" }],
    // The review step sends a failed review back to the build: the bounded rework path.
    ...(st.id === "review" && all.some((x) => x.id === "build") ? { rework: { to: "build", when: "failed", max_rounds: 2 } } : {}),
  }));
  const flow = { format: 2, title: book.flows[0].title, steps };
  return {
    documents: { "playbook.toml": manifest, "flows/main.toml": flow },
    files: {
      "playbook.toml": `format = 2\n[identity]\nid = "${book.id}"\nname = "${book.name}"\n`,
      "flows/main.toml": `format = 2\ntitle = "${book.flows[0].title}"\n`,
      "README.md": `# ${book.name}\n\n${book.summary}\n`,
    },
    readme: `# ${book.name}\n\n${book.summary}\n`,
  };
}

// ---- the mission shelf (#1740) ---------------------------------------------------------------
const SHELF_PNG = readFileSync(new URL("../../e2e/fixtures/media/dot.png", import.meta.url));
const SHELF_WEBM = readFileSync(new URL("../../e2e/fixtures/media/clip.webm", import.meta.url));
const SHELF_OGG = readFileSync(new URL("../../e2e/fixtures/media/tone.ogg", import.meta.url));
const shf = (n) => "shf_" + String(n).padStart(32, "0");
const SHELF_A = "claude:5f3c0000-0000-4000-8000-0000000000a1";
const SHELF_B = "codex:019a0000-0000-7000-8000-0000000000b2";

function shelfShowcase() {
  const at = (m) => F.NOW - m * 60;
  const file = (name, mime, extra = {}) => ({
    ref_type: "path",
    name,
    path: `/home/operator/projects/battlelab-site/${name}`,
    mime,
    viewable: mime !== null,
    sha256: "9f2c" + "0".repeat(56) + "41a0",
    file: { state: "ok" },
    ...extra,
  });
  const item = (n, over) => ({
    id: shf(n),
    seq: n,
    source: "delivered",
    why: "",
    session_key: n <= 4 ? SHELF_A : SHELF_B,
    size: 1024,
    sha256: null,
    created_at: at(40 - n),
    verdict: null,
    note: "",
    verdict_at: null,
    ...over,
  });
  return [
    item(1, { kind: "video", title: "Hero video, 1080p", why: "The 30-second launch cut, dark theme for the hero shot as asked.", ...file("out/hero-v021.webm", "video/webm"), size: 19293798, verdict: "good", verdict_at: at(5) }),
    item(2, { kind: "audio", title: "Voice-over, take 3", why: "Second paragraph shortened so the read fits 30 seconds.", ...file("out/vo-take3.ogg", "audio/ogg"), size: 626688, verdict: "needs_work", note: "Keep the full second paragraph; trim the intro instead.", verdict_at: at(4) }),
    item(3, { kind: "image", title: "Poster frame", why: "Frame 00:04 of the hero video, for the release page.", ...file("out/hero-poster.png", "image/png", { file: { state: "changed", disk_sha256: "77ab" + "1".repeat(56) + "0e93", disk_mtime: at(3) } }), size: 1153433 }),
    item(4, { kind: "text", title: "Render settings", why: "So the next render can match this one.", ref_type: "text", text: "Rendered at 1920×1080, 24 fps, VP9 at constant quality 31. The title card holds for 2.5 s; the map sequence runs 00:06–00:19.", size: 1434 }),
    item(5, { kind: "image", title: "Map view, dark", why: "Hero screenshot for the release notes.", ...file("shots/map-dark.png", "image/png"), size: 421888, verdict: "good", verdict_at: at(6) }),
    item(6, { kind: "image", title: "Shelf page, dark", why: "Third screenshot, as the plan asked.", ...file("shots/shelf-dark.png", "image/png", { file: { state: "missing" } }) }),
    item(7, { kind: "document", title: "Changelog for v0.21", why: "Grouped by area; links each item to its issue.", ...file("CHANGELOG.md", "text/plain; charset=utf-8"), size: 6348 }),
    item(8, { kind: "document", title: "Release notes", why: "The PDF for the press page.", ...file("out/release-notes.pdf", null), size: 98304 }),
    item(9, { kind: "link", title: "Staging preview of the release page", why: "The page with the video and screenshots in place.", ref_type: "url", url: "https://staging.example.org/releases/v0-21", host: "staging.example.org", url_path: "/releases/v0-21", size: null }),
    // Found in git (#1740 P1b): a code change two sessions' windows covered, with its PR.
    item(10, {
      kind: "code", source: "found", ref_type: "code", size: null, session_key: SHELF_A,
      title: "render: add the hero timeline",
      sessions: [SHELF_A, SHELF_B],
      meta: {
        repo: "/home/operator/projects/battlelab-site", repo_name: "battlelab-site",
        branch: "devopsagent/launch-v021", base: "1".repeat(40), head: "3e9c1f2" + "0".repeat(33),
        commits: [{ sha: "3e9c1f2" + "0".repeat(33), subject: "render: add the hero timeline" }],
        commits_more: false,
        files: [
          { path: "src/render/timeline.ts", status: "A", add: 96, del: 0, from: "commit" },
          { path: "render.config.json", status: "M", add: 4, del: 2, from: "commit" },
          { path: "src/render/scenes.ts", status: "M", add: 28, del: 12, from: "commit" },
        ],
        files_more: 0, add: 128, del: 14,
        pr: { number: 418, state: "open", url: "https://git.example.org/battlelab/site/pulls/418", merged: false },
        late: false, sessions: [SHELF_A, SHELF_B],
      },
    }),
  ];
}

function shelfManifest(missionId, items) {
  const by_kind = { image: 0, video: 0, audio: 0, document: 0, code: 0, link: 0, text: 0 };
  for (const i of items) by_kind[i.kind] += 1;
  return {
    mission_id: missionId,
    revision: items.length,
    counts: {
      total: items.length,
      by_kind,
      by_source: { delivered: items.length, found: 0 },
      unreviewed: items.filter((i) => i.verdict === null).length,
      file_checks: {
        changed: items.filter((i) => i.file?.state === "changed").length,
        missing: items.filter((i) => i.file?.state === "missing").length,
        inaccessible: 0,
      },
    },
    items,
    next_cursor: null,
  };
}

/** The debrief (#1740 P2) for the showcase shelf: the facts from the records, a stored summary
 *  (`debriefStale`, a sweep state: written before the records moved). */
function debriefShowcase(id, mission, state) {
  const m = shelfManifest(id, state?.shelf ? shelfShowcase() : []);
  const needs = m.items.filter((i) => i.verdict === "needs_work");
  const objectives = (mission.objectives ?? []).map((o, n) => ({
    key: o.key,
    title: o.title,
    state: n === 0 ? "met" : "pending",
    met: n === 0,
    judged:
      n === 0
        ? { met: true, confidence: 0.96, checked_at: F.NOW - 3600, evidence: [{ source: `transcript:${SHELF_A}`, quote: "Rendered out/hero-v021.webm — 1920×1080, 00:31, 24 fps." }] }
        : { met: false, confidence: 0.64, checked_at: F.NOW - 3000, evidence: [{ source: `transcript:${SHELF_A}`, quote: "The second paragraph was shortened to fit 30 seconds." }] },
  }));
  const rev = state?.debriefStale ? "r-now" : "r-then";
  return {
    mission_id: id,
    state: "review",
    configured: true,
    facts: {
      mission_id: id,
      title: mission.title,
      state: "review",
      asked: mission.instruction,
      sessions: [
        { key: SHELF_A, role: "primary", added_at: F.NOW - 4 * 3600, removed_at: F.NOW - 3600 },
        { key: SHELF_B, role: "sub", added_at: F.NOW - 4 * 3600, removed_at: F.NOW - 1800 },
      ],
      objectives,
      shelf: {
        counts: m.counts,
        groups: [
          { session_key: SHELF_A, total: m.items.filter((i) => i.session_key === SHELF_A).length, by_kind: {} },
          { session_key: SHELF_B, total: m.items.filter((i) => i.session_key === SHELF_B).length, by_kind: {} },
        ],
      },
      open: {
        objectives: objectives.filter((o) => !o.met).map(({ key, title, state: st, judged }) => ({ key, title, state: st, judged })),
        objectives_total: objectives.filter((o) => !o.met).length,
        needs_work: needs.map((i) => ({ id: i.id, kind: i.kind, source: i.source, title: i.title, session_key: i.session_key, note: i.note })),
        needs_work_total: needs.length,
        found_not_delivered: [
          { id: shf(11), kind: "code", source: "found", title: "render: add the hero timeline", session_key: SHELF_A, commits: 1, files: 3, repo_name: "battlelab-site" },
          { id: shf(12), kind: "code", source: "found", title: "docs: changelog for v0.21", session_key: SHELF_B, commits: 2, files: 1, pr: 418, repo_name: "battlelab-site" },
        ],
        found_not_delivered_total: 2,
      },
      integrity: { changed: [], missing: [] },
      revision: "r-then",
    },
    summary: {
      text: "Two sessions worked for 2 h 51 min. Claude rendered a 31-second hero video in the dark theme and recorded a voice-over, shortening the second paragraph to fit. Codex captured the screenshots, wrote the changelog and opened PR #418 with both.",
      revision: rev,
      written_at: F.NOW - 1700,
    },
    fresh: rev === "r-then",
    generating: false,
    error: null,
  };
}

function shelfDiff(itemId) {
  return {
    id: itemId,
    files_more: 0,
    files: [
      {
        path: "src/render/timeline.ts", status: "A", add: 96, del: 0, from: "commit", truncated: false, unavailable: false,
        patch: "--- /dev/null\n+++ b/src/render/timeline.ts\n@@ -0,0 +1,6 @@ new file\n+export const HERO_TIMELINE = [\n+  { at: 0, scene: \"title\", hold: 2.5 },\n+  { at: 2.5, scene: \"map\", until: 19 },\n+];\n+\n+export const FPS = 24;\n",
      },
      {
        path: "render.config.json", status: "M", add: 4, del: 2, from: "commit", truncated: false, unavailable: false,
        patch: "@@ -3,7 +3,9 @@\n   \"width\": 1920,\n-  \"fps\": 30,\n-  \"theme\": \"light\",\n+  \"fps\": 24,\n+  \"theme\": \"dark\",\n+  \"codec\": \"vp9\",\n+  \"crf\": 31,\n",
      },
      { path: "src/render/scenes.ts", status: "M", add: 28, del: 12, from: "commit", truncated: false, unavailable: false, patch: "@@ -1,3 +1,3 @@\n-export const SCENES = 3;\n+export const SCENES = 4;\n" },
    ],
  };
}

function shelfRaw(itemId) {
  if (itemId === shf(1)) return { raw: { status: 200, contentType: "video/webm", body: SHELF_WEBM } };
  if (itemId === shf(2)) return { raw: { status: 200, contentType: "audio/ogg", body: SHELF_OGG } };
  if (itemId === shf(3) || itemId === shf(5)) {
    return { raw: { status: 200, contentType: "image/png", body: SHELF_PNG } };
  }
  if (itemId === shf(7)) {
    return {
      raw: {
        status: 200,
        contentType: "text/plain; charset=utf-8",
        body: "# v0.21\n\n## Missions\n- The shelf: everything a mission produced, for review.\n",
      },
    };
  }
  return json({ detail: "the file is no longer on disk" }, 404);
}
