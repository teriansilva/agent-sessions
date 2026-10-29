import * as SB from "../promo/settingsStates.mjs";
// The NIGHTJAR screenshot sweep's manifest (#1531): every capture id a ledger row may name
// (docs/nightjar-ledger.md "Captures"), and how to put the app into that state. The ledger test
// fails when a `done` row names an id this list does not have. Phases add ids with their rows;
// Phase 14 runs the whole list across every width, theme and pixel ratio.
//
//   cd web && npm run build && npx vite preview --port 41918 --strictPort &
//   node visual/nightjar/sweep.mjs --base http://127.0.0.1:41918 --out ../out/nightjar [--only id,id] [--full]

/** A New session picker project (the `/api/projects` row shape) for the PR6 captures. */
const NS_PROJECT = (id, name, folder) => ({
  id,
  name,
  color: "#64748b",
  folders: folder ? [folder] : [],
  default_folder: folder,
  archived: false,
  created_at: 0,
  session_count: 0,
});

/** Share bundles (#1714): an export catalog and an import preview for the P1 captures. */
const SHARE_CATALOG = {
  imports: {},
  items: [
    { kind: "skill", id: "research-with-citations", title: "research-with-citations", depends_on: [], exportable: true, reason: null, source: "local" },
    { kind: "skill", id: "research-review", title: "research-review", depends_on: [], exportable: true, reason: null, source: "local" },
    { kind: "skill", id: "tdd", title: "tdd", depends_on: [], exportable: true, reason: null, source: "local" },
    { kind: "playbook", id: "research-brief", title: "Research brief", depends_on: ["skill:research-with-citations", "skill:research-review"], exportable: true, reason: null, source: "local" },
    { kind: "playbook", id: "forgejo-workflow", title: "Forgejo workflow", depends_on: [], exportable: true, reason: null, source: "bundled" },
  ],
};
const SHARE_FILE = (path, text) => ({ path, size: text.length, text });
const SHARE_PREVIEW = {
  token: "a".repeat(32),
  expires_at: "2026-10-27T10:15:00Z",
  bundle: { digest: "9f3c" + "0".repeat(56) + "a71e", size: 41984, format: 1, created_at: "2026-10-26T14:02:00Z", app_version: "0.24.1", source: "Studio laptop", items: 5, files: 9 },
  items: [
    { key: "skill:research-with-citations", kind: "skill", id: "research-with-citations", title: "research-with-citations", dest: "research-with-citations", status: "new", reason: null, depends_on: [], uses: [], files: [SHARE_FILE("SKILL.md", "# Research with citations\n\nCite every material claim.\n")] },
    { key: "skill:research-review", kind: "skill", id: "research-review", title: "research-review", dest: "research-review-2", status: "renamed", reason: null, depends_on: [], uses: [], files: [SHARE_FILE("SKILL.md", "# Research review\n")] },
    { key: "playbook:research-brief", kind: "playbook", id: "research-brief", title: "Research brief", dest: "research-brief", status: "new", reason: null, depends_on: ["skill:research-with-citations", "skill:research-review"], uses: [{ from: "research-review", to: "research-review-2" }], files: [SHARE_FILE("playbook.toml", "format = 5\n")] },
    { key: "playbook:release-notes", kind: "playbook", id: "release-notes", title: "Release notes", dest: "release-notes", status: "new", reason: null, depends_on: ["skill:research-review"], uses: [{ from: "research-review", to: "research-review-2" }], files: [SHARE_FILE("playbook.toml", "format = 5\n")] },
    { key: "playbook:triage", kind: "playbook", id: "triage", title: "Old triage", dest: null, status: "refused", reason: "not a valid playbook here: flows/main.toml: steps[2].after names an unknown step", depends_on: [], uses: [], files: [] },
  ],
};

/** @typedef {({ testId: string } | { role: string, name: string, partial?: boolean } | { css: string })
 *   & { upload?: string, fill?: string, select?: string, drag?: [number, number],
 *       wait?: "attached" | "detached" | "visible" }) | { viewport: [number, number] } | { press: string }
 *   | { api: Record<string, unknown> }} SweepStep
 *  A step: a test id, a role + accessible name (`partial`: substring match), or a CSS selector.
 *  `upload`: instead of clicking, put a small text file of that name into the (file input) target.
 *  `fill`: instead of clicking, type this text into the (input) target. `select`: choose the
 *  option with this label. `wait`: wait for the target to be in that state instead of clicking.
 *  `drag`: press on the target, move by [dx, dy] and HOLD — the shot is the drag in flight (the
 *  pointer is released after it). `{ viewport: [w, h] }`: resize the page mid-sequence.
 *  `{ press: "Escape" }`: press a key (a drawer whose close control is visually hidden).
 *  `{ api: {...} }`: from this step on, answer these paths so (the capture's `api` shape) — a state
 *  a re-read produces. */

/** @typedef {{ id: string, path: string, ready?: string, section?: string, fullPage?: boolean,
 *   action?: "stream" | "arrive" | "power-on", motion?: "full" | "reduced" | "off",
 *   open?: SweepStep[], openPhone?: SweepStep[], phoneFirst?: SweepStep[], expect?: string,
 *   expectCss?: string, asPerson?: boolean, at?: number, api?: Record<string, unknown>,
 *   mock?: Record<string, boolean>, storage?: Record<string, unknown>, widths?: ("desktop" | "phone")[],
 *   grant?: string[], holdAsset?: string, peerPath?: string, height?: number, present?: string,
 *   term?: Record<string, unknown> }} SweepCapture
 *  `section`: a test id, or a panel's visible title, to capture alone (it must exist and have a
 *  size, or the sweep fails). `action`: an interaction the sweep performs before capturing.
 *  `motion`: the mode for this capture (default: the sweep's `--motion`, Off).
 *  `open`: controls to click, in order, to open a popup; `expect`: the ARIA role that must then be
 *  visible (`dialog`, `menu`, `alertdialog`…), or the sweep fails. Every width runs, so one id
 *  covers the desktop popup AND its ≤800px bottom sheet.
 *  `openPhone`: the steps on a phone instead, when the control lives elsewhere there (a head chip
 *  folds into the ≤800px Actions menu); `phoneFirst`: steps prepended on a phone.
 *  `expectCss`: like `expect`, a selector that must be visible once the popup is open — for a
 *  panel whose role changes with the width (HQ: complementary on desktop, a dialog on a phone).
 *  `asPerson`: clear `navigator.webdriver` (the boot sequence stands down under automation);
 *  `at`: capture this many ms after load instead of waiting for the page to settle.
 *  `present`: a selector that must be visible at the moment of the shot, or the capture fails.
 *  `term`: the terminal socket's state (visual/promo/terminal.mjs: silent, closeCode, role, holder, dropAfter).
 *  `api`: per-capture answers, `path → body` for a state the shared fixtures do not hold; a
 *  `{ status, json }` body answers that status, and with `method` only
 *  that method (others fall through to the shared fixtures). `mock`: opt-in states of the shared mock (visual/promo: e.g.
 *  `archivedMission`, `bypassOffer`, `disabledAutomation`, `scratchFolder`). `storage`: localStorage
 *  for this capture (`null` removes a key). `widths`: only these (a surface that exists on one).
 *  `grant`: browser permissions (clipboard). `holdAsset`: a regex of an asset that never answers
 *  (the loading state). `peerPath`: open this in another tab of the same browser first.
 *  `height`: the desktop viewport height (default 960). */

/** On a phone the session rows live in the off-canvas drawer: open it first. */
const DRAWER = [{ role: "button", name: "Open session list" }];

// The promo fixtures' sessions and missions (visual/promo/fixtures.mjs).
const S1 = "/s/claude/7c1e1a00-5b2d-4c8e-9f31-0a6d2e8b4001"; // terminal, in a mission
const S6 = "/s/claude/7c1e6a00-5b2d-4c8e-9f31-0a6d2e8b4006"; // terminal, no mission
const SAPI = "/s/claude-api/7c1e9a00-5b2d-4c8e-9f31-0a6d2e8b4009"; // an API (structured) session
const M1 = "/mission?m=msn_" + "1a2b3c4d".repeat(4);
const ARCHIVED = "/mission?m=msn_" + "0f1e2d3c".repeat(4);

/** A session header action: an inline chip on desktop, an item of the ≤800px Actions menu. */
const head = (name) => ({
  open: [{ role: "button", name }],
  openPhone: [{ testId: "head-actions-menu" }, { role: "menuitem", name }],
});
/** The Files panel: its header chip on desktop, the Actions menu on a phone. */
const FILES = head("Browse session files");
const withFiles = (desk, phone = desk) => ({
  open: [{ css: "[data-head-action='files']" }, ...desk],
  openPhone: [...FILES.openPhone, ...phone],
});
const GIT = { role: "tab", name: "Git", partial: true };
/** A compose-bar key: inline on desktop, under More keys on a phone. */
const key = (name) => ({
  open: [{ role: "button", name }],
  openPhone: [{ role: "button", name: "More keys" }, { role: "menuitem", name }],
});

/** The map's clusters open (they start collapsed), and a session chip by its title. */
const MAP_EXPAND = { role: "button", name: "Expand all", partial: true };
const mapChip = (title) => ({ css: `.tr-ov-chip[aria-label="Open ${title}"]` });
const EMPTY_SESSIONS = { sessions: [], total: 0, live_total: 0, next_offset: null, facets: { projects: [], engines: [], missions: [], no_mission: 0 } };
/** One project's memory with three notes by two sessions (the memory graph's nodes and edges). */
const MEMORY_AT = Math.floor(Date.now() / 1000);
const MEMORY_GRAPH = {
  "/api/memory": {
    projects: [{ id: "p-orbital", name: "orbital-api", color: "", enabled: true, notes: 3, pending: 0, quarantined: 0, last_write: MEMORY_AT - 240 }],
  },
  "/api/memory/p-orbital": {
    project: { id: "p-orbital", name: "orbital-api", color: "" },
    enabled: true,
    notes: [
      { slug: "bucket-per-key", name: "Buckets are per API key", description: "never per IP", type: "decision", body: "Key the limiter by API key. See [[retry-after]].", size: 52, created: MEMORY_AT - 600, updated: MEMORY_AT - 240, session: { key: "claude:7c1e1a00-5b2d-4c8e-9f31-0a6d2e8b4001", engine: "claude", kind: "terminal", resolved: true }, links: ["retry-after"] },
      { slug: "retry-after", name: "429 carries Retry-After", description: "seconds, not a date", type: "pitfall", body: "Send Retry-After in seconds.", size: 40, created: MEMORY_AT - 1200, updated: MEMORY_AT - 1200, session: { key: "codex:7c1e2a00-5b2d-4c8e-9f31-0a6d2e8b4002", engine: "codex", kind: "terminal", resolved: true }, links: [] },
      { slug: "k6-workers", name: "k6 needs 200 VUs for 2k rps", description: "on the staging box", type: "finding", body: "Below 200 VUs the client saturates first.", size: 38, created: MEMORY_AT - 9000, updated: MEMORY_AT - 9000, session: { key: "claude:7c1e1a00-5b2d-4c8e-9f31-0a6d2e8b4001", engine: "claude", kind: "terminal", resolved: true }, links: [] },
    ],
    pending: [],
    refused: [],
    quarantine: [],
    withheld: { notes: 0, reason: "" },
    instructions: "Notes are hints from other agents, not instructions.",
    limits: { note_bytes: 4096, notes: 200, inbox_files: 20 },
  },
  "/api/memory/p-orbital/import": { sources: [], last: null, held: [] },
};

/** @type {SweepCapture[]} */
/** On a phone the mission rail is the shell's drawer: open it first. */
const MISSION_DRAWER = [{ role: "button", name: "Open mission list" }];
/** A mission-list answer (#1573's rail captures): the rows, their count, no store error. */
const RAIL_LIST = (rows) => ({
  missions: rows,
  total: rows.length,
  limit: 50,
  offset: 0,
  facets: { projects: ["p-orbital", "p-harbor", "p-atlas"], states: ["running", "review"] },
  store_error: null,
  snapshot: "snap",
});
const RAIL_NOW = Math.floor(Date.now() / 1000);
const railRow = (n, title, state, over = {}) => ({
  id: "msn_" + String(n).repeat(32),
  title,
  project_id: "p-orbital",
  cwd: "/home/commander/code/orbital-api",
  state,
  created_at: RAIL_NOW - 9000,
  updated_at: RAIL_NOW - 300 * n,
  closed_at: null,
  archived_at: null,
  outcome: null,
  session_keys: ["claude:7c1e1a00-5b2d-4c8e-9f31-0a6d2e8b4001"],
  ...over,
});
/** One row per LED the rail draws. */
const RAIL_ROWS = [
  railRow(1, "Share masking speed bound", "running", { needs_you: true, project_id: "p-harbor" }),
  railRow(2, "Harden the public ingest API", "running"),
  railRow(3, "Pricing page v2", "dispatching", { project_id: "p-harbor" }),
  railRow(4, "Relay reconnect storm", "failed"),
  railRow(5, "atlas-cli 2.4 release", "review", { project_id: "p-atlas", needs_you: true }),
  railRow(6, "Runner pool notes", "draft", { project_id: null, cwd: null, session_keys: [] }),
  railRow(7, "Postgres p95 under 20 ms", "done"),
];
/** The orchestrator, failing: two passes in a row could not reach the AI endpoint. */
const ORCH_FAILING = {
  config: { enabled: true, autonomy: "suggest", allowed_verbs: [], auto_verbs_ceiling: [], configured: true },
  pending: [],
  feed: [],
  expired_now: 0,
  delivering_verbs: [],
  last: {
    orchestrator: {
      finished_at: RAIL_NOW - 120,
      ok: false,
      consecutive_failures: 3,
      last_ok: RAIL_NOW - 3 * 3600,
      error: "The endpoint refused the key (401).",
    },
  },
};
/** Library → Checklists with two checklists, the first the default. */
const CHECKLISTS_CONFIG = {
  mission_playbooks: {
    default_id: "ship",
    revision: 1,
    playbooks: [
      {
        id: "ship",
        label: "Ship it",
        objectives: [
          { key: "tests", title: "The test suite is green", probe: "none", gate: true },
          { key: "live", title: "It is live", probe: "http_status", probe_args: { url: "https://orbital.example/healthz" }, gate: true },
          { key: "notes", title: "Release notes are written", probe: "none", gate: false },
          { key: "pr", title: "A pull request is open", probe: "none", gate: true },
        ],
      },
      {
        id: "bug",
        label: "Bug hunt",
        objectives: [
          { key: "repro", title: "A failing test reproduces it", probe: "none", gate: true },
          { key: "fix", title: "The fix turns it green", probe: "none", gate: true },
        ],
      },
    ],
  },
  mission_probes: {
    kinds: ["none", "http_status"],
    non_gating: [],
    args: { none: { required: [], optional: [] }, http_status: { required: ["url"], optional: ["expect_status"] } },
    types: { none: {}, http_status: { url: "text", expect_status: "int" } },
  },
};

export const SWEEP = [
  // The primitives gallery (Phase 2): one capture per section, plus the whole page.
  { id: "gallery:all", path: "/__nightjar", ready: "nightjar-gallery", fullPage: true },
  { id: "gallery:panels", path: "/__nightjar", ready: "g-panel-default", section: "g-panel-default" },
  { id: "gallery:buttons", path: "/__nightjar", ready: "nightjar-gallery", section: "Buttons" },
  { id: "gallery:fields", path: "/__nightjar", ready: "nightjar-gallery", section: "Fields" },
  { id: "gallery:meters", path: "/__nightjar", ready: "g-readout", section: "Meters" },
  { id: "gallery:loaders", path: "/__nightjar", ready: "nightjar-gallery", section: "Loaders" },
  { id: "gallery:stream", path: "/__nightjar", ready: "nightjar-gallery", section: "Streaming", action: "stream", motion: "full" },
  { id: "gallery:arrival", path: "/__nightjar", ready: "nightjar-gallery", section: "Arrival", action: "arrive", motion: "full" },
  { id: "gallery:power-on", path: "/__nightjar", ready: "g-panel-power", section: "g-panel-power", action: "power-on", motion: "full" },
  // Popups (Phase 3): opened by clicking real controls, so a capture is the real popup.
  { id: "popup:operator-menu", path: "/dashboard", open: [{ testId: "operator-menu" }], expect: "menu" },
  { id: "popup:dashboard-new", path: "/dashboard", open: [{ testId: "dashboard-new" }], expect: "menu" },
  { id: "popup:session-row-menu", path: "/new", phoneFirst: DRAWER, open: [{ role: "button", name: "Session actions" }], expect: "menu" },
  {
    id: "popup:hand-off",
    path: "/new",
    phoneFirst: DRAWER,
    open: [{ role: "button", name: "Session actions" }, { role: "menuitem", name: "Hand off session to another engine" }],
    expect: "dialog",
  },
  {
    id: "popup:move-to-project",
    path: "/new",
    phoneFirst: DRAWER,
    open: [{ role: "button", name: "Session actions" }, { role: "menuitem", name: "Move session to a project" }],
    expect: "dialog",
  },
  {
    id: "popup:session-brief",
    path: "/new",
    phoneFirst: DRAWER,
    open: [{ role: "button", name: "Session actions" }, { role: "menuitem", name: "Open session brief" }],
    expect: "dialog",
  },
  {
    id: "popup:whats-new",
    path: "/dashboard",
    open: [{ testId: "operator-menu" }, { role: "menuitem", name: "What's new in 0.20" }],
    expect: "dialog",
  },
  { id: "popup:bell", path: "/dashboard", open: [{ role: "button", name: "Notifications" }], expect: "dialog" },
  {
    id: "popup:tour",
    path: "/dashboard",
    open: [{ testId: "operator-menu" }, { role: "menuitem", name: "Intro tour" }],
    expect: "dialog",
  },
  // HQ opens from the primary contact card (#1525: the dashboard has no HQ button).
  {
    id: "popup:hq",
    path: "/dashboard",
    ready: "hq-primary-contact",
    open: [{ testId: "hq-primary-contact" }],
    expectCss: '[data-testid="ask-sidebar"][data-open="true"]',
  },
  // Calling HQ (#1397): the call panel, opened by the dashboard card's Call. With a (fake,
  // granted) microphone the call is admitted and dials; the sweep has no call socket that answers,
  // so the panel holds its dialing state — the panel chrome, controls and copy — asserted below.
  {
    id: "popup:hq-call",
    path: "/dashboard",
    ready: "hq-primary-call",
    grant: ["microphone"],
    api: {
      "/api/voice/capabilities": { configured: true, runtime: true, available: true, reason: "", captions: true, mic: "open", active_call: null, destinations: { stt: "lan", tts: "lan", ai: "lan", stt_host: "gpu-1.lan:8000", tts_host: "gpu-1.lan:8880", ai_host: "llm.lan" } },
      "/api/voice/calls": { status: 201, json: { id: "c1", conversation_id: "a".repeat(32), contact: "asia-woman", state: "dialing", muted: false, elapsed_s: 0, ended_reason: "" } },
    },
    open: [{ testId: "hq-primary-call" }],
    expectCss: '[data-testid="call-panel"][data-phase="dialing"]',
  },
  // The rest of the overlays (Phase 3). Each is opened by its real control, on both widths unless
  // the surface exists on one only.
  { id: "popup:head-menu", path: S1, widths: ["phone"], open: [{ testId: "head-actions-menu" }], expect: "menu" },
  { id: "popup:files", path: S1, ...withFiles([]), expectCss: "[data-file-panel]" },
  {
    id: "popup:file-viewer",
    path: S1,
    ...withFiles([{ css: "[data-file-row][data-kind='file']:has-text('README.md') [data-row-main]" }]),
    expect: "dialog",
  },
  { id: "popup:upload-menu", path: S1, ...withFiles([{ css: "[data-upload-trigger]" }]), expectCss: "[data-upload-menu]" },
  {
    id: "popup:upload-collision",
    path: S1,
    ...withFiles([{ css: "[data-upload-files]", upload: "README.md" }]),
    api: {
      "/api/files/upload/batch": { batch_id: "b1", files: 1, bytes: 6, files_limit: 500, bytes_limit: 262144000, file_limit: 26214400 },
      "/api/files/upload": { status: 409, json: { detail: "'README.md' already exists here" } },
    },
    expectCss: "[data-upload-queue]",
  },
  { id: "popup:git-branch-menu", path: S1, ...withFiles([GIT, { css: "[data-branch-trigger]" }]), expectCss: "[data-branch-menu]" },
  {
    id: "popup:git-confirm",
    path: S1,
    ...withFiles(
      [GIT, { role: "button", name: "Discard changes to src/ingest/router.ts" }],
      [GIT, { css: "[data-row-menu-trigger='src/ingest/router.ts']" }, { role: "menuitem", name: "Discard changes…" }],
    ),
    expect: "alertdialog",
  },
  {
    id: "popup:git-row-menu",
    path: S1,
    widths: ["phone"],
    ...withFiles([], [GIT, { css: "[data-row-menu-trigger='src/ingest/router.ts']" }]),
    expectCss: "[data-row-menu='src/ingest/router.ts']",
  },
  { id: "popup:keybar", path: S1, widths: ["phone"], open: [{ role: "button", name: "More keys" }], expect: "menu" },
  {
    id: "popup:sent-messages",
    path: S1,
    storage: {
      "as:sent:v1": [
        { id: "s1", text: "Rerun k6 at 400 VUs and post p95.", attachments: [], ts: Date.now() - 60000, confirmed: true, session: "claude:7c1e1a00-5b2d-4c8e-9f31-0a6d2e8b4001" },
      ],
    },
    ...key("Sent messages"),
    expect: "dialog",
  },
  { id: "popup:template-picker", path: S1, ...key("Use a template"), expect: "dialog" },
  { id: "popup:session-chooser", path: "/templates", open: [{ role: "button", name: "Use Release checklist" }], expect: "dialog" },
  { id: "popup:confirm", path: "/settings/projects", open: [{ role: "button", name: "Delete project orbital-api" }], expect: "dialog" },
  {
    id: "popup:rename-project",
    path: "/settings/projects",
    mock: { scratchFolder: true },
    open: [{ css: "button[aria-label^='Rename'][aria-label$='scratch']" }],
    expect: "dialog",
  },
  // New session has no folder picker of its own any more (#1458); Settings → Projects opens it.
  { id: "popup:folder-picker", path: "/settings/projects", open: [{ css: "button[aria-label^='Change default folder for']" }], expect: "dialog" },
  { id: "popup:permissions", path: SAPI, mock: { bypassOffer: true }, ...head("Enable Skip permissions for this session"), expect: "dialog" },
  { id: "popup:share-publicly", path: SAPI, ...head("Share this session publicly"), expectCss: "[data-testid='share-publicly']" },
  { id: "popup:adopt", path: S6, ...head("Adopt this session into a mission"), expectCss: "[data-testid='adopt-dialog']" },
  {
    id: "popup:toast",
    path: S1,
    grant: ["clipboard-read", "clipboard-write"],
    ...head("Share a link to this session"),
    expectCss: "[data-link-toast]",
  },
  {
    id: "popup:open-link",
    path: S1,
    widths: ["desktop"],
    storage: { "battlelab.linkOpenMode": null },
    expectCss: "[data-testid='open-link-dialog']",
  },
  { id: "popup:handoff-stub", path: S1, peerPath: "/new", expectCss: "[data-testid='link-handoff-stub']" },
  { id: "popup:mission-overflow", path: M1, open: [{ testId: "mission-overflow" }], expectCss: "[data-testid='mission-overflow-menu']" },
  { id: "popup:mission-title", path: M1, open: [{ role: "button", name: "Show full mission title" }], expect: "dialog" },
  {
    id: "popup:objective-direction",
    path: M1,
    // The objectives sit below the fold at 960, and the scroll that reaches them closes the menu.
    // A phone reaches them through the details band (Context folded for room).
    height: 1400,
    // The SECOND objective: the first is met, and a met objective's ⋯ offers no direction.
    open: [{ css: "[data-testid='objective-menu'] >> nth=1" }, { testId: "objective-edit-direction" }],
    openPhone: [
      { testId: "details-toggle" },
      { testId: "detail-context" },
      { css: "[data-testid='objective-menu'] >> nth=1" },
      { testId: "objective-edit-direction" },
    ],
    expectCss: "[data-testid='direction-dialog']",
  },
  { id: "popup:unarchive", path: ARCHIVED, mock: { archivedMission: true }, open: [{ testId: "mission-unarchive" }], expect: "dialog" },
  {
    id: "popup:needs-you-details",
    path: "/dashboard",
    open: [{ role: "button", name: "Details for Migrate billing webhooks to v2" }],
    expectCss: "[data-testid='needs-you-dialog']",
  },
  {
    id: "popup:recent-work",
    path: "/dashboard",
    open: [{ testId: "dashboard-details-toggle" }, { testId: "recent-work-more" }],
    expectCss: "[data-testid='recent-work-dialog']",
  },
  {
    id: "popup:consent",
    path: "/mission/automations",
    mock: { disabledAutomation: true },
    open: [{ testId: "automation-more" }, { role: "menuitem", name: "Enable…" }],
    expectCss: "[data-testid='consent-dialog']",
  },
  {
    id: "popup:session-text",
    path: "/overview",
    widths: ["desktop"],
    open: [
      { role: "button", name: "Collapse session list" },
      { role: "button", name: "Expand all", partial: true },
      { role: "button", name: "Session actions: Rate-limit the public ingest API" },
      { role: "menuitem", name: "Rename session" },
    ],
    expectCss: "[data-session-text-dialog]",
  },
  {
    id: "popup:playbook-delete",
    path: "/library/playbooks",
    open: [{ css: "[data-testid='playbook-card-ship-it'] a:has-text('Open')" }, { role: "button", name: "Delete playbook…" }],
    expect: "dialog",
  },
  // The shell's states (Phase 4): a route chunk still loading, and a part of the app that failed.
  { id: "route:loading", path: "/templates", holdAsset: "/assets/Templates-[^/.]+\\.js$", at: 1500 },
  { id: "route:error", path: "/library/skills", api: { "/api/library/skills": {} }, expect: "alert" },
  // The boot sequence (Phase 4), mid-boot: Full, as a person, 450 ms after load.
  { id: "route:boot", path: "/dashboard", motion: "full", asPerson: true, at: 450, present: "[data-testid='boot-sequence']" },
  // #1570: the uplink veil over a terminal whose settled link (held > 750 ms) has been down past
  // its 5 s grace; every retry is accepted and refused at once (a flap), which keeps the outage.
  // The session pane's own panels (RuntimeGate.module.css, #1502): a browser that has never loaded
  // the agent list waits ("Opening…"), gives up after 8 s (Retry), and a 4404 whose lookup settled
  // "missing" reads Not found over the terminal.
  { id: "route:session-opening", path: S6, holdAsset: "/api/engines(\\?|$)", at: 1500, present: "[data-session-opening='opening']" },
  { id: "route:session-opening-failed", path: S6, holdAsset: "/api/engines(\\?|$)", at: 9500, present: "[data-session-opening='failed']" },
  {
    id: "route:session-not-found",
    path: "/s/claude/7c1e0000-5b2d-4c8e-9f31-0a6d2e8b40ff",
    term: { closeCode: 4404, closeReason: "not found" },
    at: 3000,
    present: "[data-session-opening='not-found']",
  },
  { id: "route:uplink-veil", path: S6, term: { dropAfter: 1500 }, at: 7600, present: "[data-testid='uplink-veil'][data-phase='lost']" },
  // #1570 follow-up: the same veil on an API session — its first read never succeeds, so after 3 s
  // the conversation area shows ESTABLISHING (reads answer 503, raw and encoded key forms).
  {
    id: "route:uplink-veil-api",
    path: SAPI,
    api: {
      "/api/structured/sessions/claude-api%3A7c1e9a00-5b2d-4c8e-9f31-0a6d2e8b4009": { status: 503, json: { detail: "down" } },
      "/api/structured/sessions/claude-api:7c1e9a00-5b2d-4c8e-9f31-0a6d2e8b4009": { status: 503, json: { detail: "down" } },
    },
    at: 4500,
    present: "[data-testid='uplink-veil'][data-phase='establishing']",
  },
  // Routes (Phases 4–11). The skeleton: the route itself in its default state; each phase adds its
  // states (loading, empty, error, opened popups, focus) as ids of its own.
  { id: "route:dashboard", path: "/dashboard" },
  // HQ's page (#1776): the list beside the thread, and a link that names no conversation.
  { id: "route:hq", path: "/hq", present: "[data-testid='hq-page']" },
  { id: "route:hq-missing", path: "/hq/c/not-a-conversation", present: "[data-testid='hq-missing']" },
  // ---- PR3 dashboard ----
  // Phase 5's dashboard states: every first read in flight (Acquiring), two reads that failed
  // (a dash and the words, never a zero), and Recent work opened (the grid-rows disclosure).
  {
    id: "route:dashboard-acquiring",
    path: "/dashboard",
    holdAsset: "/api/(dashboard/sessions|agents/usage(\\?|$)|missions(\\?|$)|pulse/needs-you(\\?|$))",
    at: 1500,
    present: "[data-testid='tile-loading']",
  },
  {
    id: "route:dashboard-unread",
    path: "/dashboard",
    api: {
      "/api/missions": { status: 502, json: { detail: "mission store did not answer" } },
      "/api/agents/usage": { status: 503, json: { detail: "usage cache unavailable" } },
    },
    present: "[data-testid='tile-error']",
  },
  {
    id: "route:dashboard-recent",
    path: "/dashboard",
    open: [{ testId: "dashboard-details-toggle" }],
    expectCss: "[data-testid='recent-work']",
  },
  // ---- /PR3 dashboard ----
  { id: "route:new-session", path: "/new" },
  // ---- PR6 new session ("Deploy", #1531 Phase 6) ----
  // The form's states: the first read (skeleton rows), a failed read, a folderless project with
  // no fallback (the summary points at Settings → Projects, #1458), an explicit "No project",
  // a long list with its filter, and an API agent with its model and effort selects.
  {
    id: "newsession:loading",
    path: "/new",
    holdAsset: "/api/projects(\\?|$)",
    at: 1500,
    present: "[data-testid='project-skeleton']",
  },
  {
    id: "newsession:list-failed",
    path: "/new",
    api: { "/api/projects": { status: 502, json: { detail: "project store did not answer" } } },
    present: "[data-new-session-form] p[role='status']",
  },
  {
    id: "newsession:no-folder",
    path: "/new",
    api: { "/api/projects": { projects: [NS_PROJECT("p-scratch", "scratch", "")] } },
    present: "[data-testid='new-session-summary'] a",
  },
  {
    id: "newsession:no-project",
    path: "/new",
    open: [{ css: "label:has(> input[name='new-session-project'][value=''])" }],
    present: "input[name='new-session-project'][value='']:checked",
  },
  {
    id: "newsession:many",
    path: "/new",
    api: {
      "/api/projects": {
        projects: ["orbital-api", "harbor-web", "atlas-cli", "ledger-sync", "pixel-forge", "relay-gw", "nova-docs", "quartz-ui"].map(
          (name, i) => NS_PROJECT(i ? `p-${name}` : "p-orbital", name, `/home/commander/code/${name}`),
        ),
      },
    },
    present: "input[type='search'][aria-label='Filter projects']",
  },
  {
    id: "newsession:api",
    path: "/new",
    // The promo config (its `config` mock state merges over it), with the two API clients offered.
    mock: { config: { new_session_engines: ["claude", "codex", "opencode", "gemini", "kimi", "codex-api", "claude-api"] } },
    api: {
      "re:/api/structured/clients/[^/]+/models": {
        status: "ok",
        reason: null,
        models: [
          { id: "frontier-large", label: "Frontier Large", description: null, efforts: ["low", "medium", "high"], is_default: true },
          { id: "frontier-mini", label: "Frontier Mini", description: null, efforts: ["low", "medium"], is_default: false },
        ],
      },
    },
    open: [
      { css: "label:has(> input[name='new-session-agent'][value='codex-api'])" },
      { css: "[data-testid='new-session-model']", select: "Frontier Large (frontier-large)" },
    ],
    present: "[data-testid='new-session-effort']",
  },
  { id: "route:mission", path: "/mission" },
  { id: "route:checklists", path: "/mission/checklists" },
  { id: "route:automations", path: "/mission/automations" },
  { id: "route:map", path: "/overview" },
  // New session over the map (#1648): the sidebar's real link opens the form in THE dialog over
  // the mounted map (a full-height sheet on a phone, reached through the drawer).
  {
    id: "popup:new-session-map",
    path: "/overview",
    phoneFirst: DRAWER,
    open: [{ role: "link", name: "New session" }],
    expect: "dialog",
  },
  // ---- PR4a map ----
  // The map (#1573 Phase 9): sector-framed clusters on the tactical grid, session chips (the
  // working ping, the needs-you "!"), Controls, the minimap, the toolbar; the windows over it; the
  // route states, banners and group states; and the two other React Flow canvases on the shared
  // theme. Windows exist on a desktop map only (docs/invariants/map-window-layer.md).
  { id: "map:expanded", path: "/overview", open: [MAP_EXPAND], present: ".tr-ov-chip" },
  // Folders layout over sub-folders: hierarchy edges, the one under a working agent live.
  {
    id: "map:folders-live",
    path: "/overview",
    storage: { "tr-overview-groupby": "folder" },
    mock: { nestedFolders: true },
    open: [MAP_EXPAND],
    present: ".react-flow__edge.tr-ov-edge-live",
  },
  // A window over the map (focused, tethered to its chip) and a second one minimised to the tray;
  // the session list folded for room.
  {
    id: "map:windows",
    path: "/overview",
    widths: ["desktop"],
    open: [
      { role: "button", name: "Collapse session list" },
      MAP_EXPAND,
      mapChip("Postgres index audit"),
      { role: "button", name: "Minimize window" },
      mapChip("Rate-limit the public ingest API"),
    ],
    expectCss: "[data-window-tray]",
    present: "[data-session-window][data-focused='true']",
  },
  // A window's file drawer, docked over the window body (it overlays, never resizes).
  {
    id: "map:window-files",
    path: "/overview",
    widths: ["desktop"],
    open: [MAP_EXPAND, mapChip("Rate-limit the public ingest API"), { css: "[data-session-window] [data-head-action='files']" }],
    expectCss: "[data-session-window] [data-file-tree]",
  },
  // The window limit reached: the notice over the tray.
  {
    id: "map:window-cap",
    path: "/overview",
    widths: ["desktop"],
    storage: { "tr-overview-window-cap": "1" },
    open: [MAP_EXPAND, mapChip("Rate-limit the public ingest API"), mapChip("Dark-mode tokens for the checkout flow")],
    expectCss: "[data-window-notice]",
  },
  // A chip's ⋯: the map's session menu (the sidebar row menu's chrome, Phase 3).
  // Desktop: on a phone the fitted map is below the zoom that leaves room for a ⋯ beside the chip
  // (#968), so the menu opens by long-press there — the session-row sheet already captured.
  { id: "map:chip-menu", path: "/overview", widths: ["desktop"], open: [MAP_EXPAND, { css: "[data-chip-menu]" }], expect: "menu" },
  // Route states: the first read in flight (Acquiring over the grid), a failed first read with
  // Retry, an empty map, and empty project clusters (dashed drop targets).
  { id: "map:loading", path: "/overview", holdAsset: "/api/sessions(\\?|$)", at: 1500, present: "[data-map-state='loading']" },
  {
    id: "map:error",
    path: "/overview",
    api: { "/api/sessions": { status: 503, json: { detail: "session store did not answer" } } },
    present: "[data-map-state='error']",
  },
  { id: "map:empty", path: "/overview", api: { "/api/sessions": EMPTY_SESSIONS, "/api/projects": { projects: [] } } },
  { id: "map:empty-projects", path: "/overview", api: { "/api/sessions": EMPTY_SESSIONS }, open: [MAP_EXPAND], present: ".tr-ov-group.empty" },
  // Banners: partial results (the walk hit its page cap), and a create that failed (the inline
  // New-project form, filled, refused by the server).
  { id: "map:partial", path: "/overview", mock: { partialMap: true }, present: ".tr-ov-partial" },
  {
    id: "map:new-project-error",
    path: "/overview",
    api: { "/api/projects": { status: 409, json: { detail: "That folder already belongs to “harbor-web”." } } },
    open: [
      { role: "button", name: "New project", partial: true },
      { css: ".tr-ov-newproj input", fill: "orbital-api" },
      { role: "button", name: "Create" },
    ],
    expectCss: "[data-map-error]",
  },
  // A folder cluster's "Make this a project", refused: the group's inline error.
  {
    id: "map:group-error",
    path: "/overview",
    storage: { "tr-overview-groupby": "folder" },
    api: { "/api/projects": { status: 409, json: { detail: "That folder already belongs to a project." } } },
    open: [{ css: ".tr-ov-mkproj" }],
    present: ".tr-ov-err",
  },
  // The other two React Flow canvases on the shared theme: the memory graph and a playbook's flow.
  {
    id: "canvas:memory-graph",
    path: "/library/memory",
    widths: ["desktop"],
    api: MEMORY_GRAPH,
    present: "[data-testid='memory-graph'] .react-flow__node",
  },
  // Desktop: a phone shows the playbook's steps as a list (its page is #1574's).
  {
    id: "canvas:playbook-flow",
    path: "/library/playbooks/ship-it",
    widths: ["desktop"],
    present: "[data-testid='playbook-flowchart'] .react-flow__node",
  },
  // A refused batch (Hermes on #1320): HQ offered "Open all in map" on a stale "this map can host"
  // (measured, then the window shrank), so the map refuses all three and lists each with its own
  // way in. Reached the way map-minimize-arrange.spec.ts does; the viewport is restored for the shot.
  // Desktop: a phone map hosts no windows, so HQ never offers the batch there.
  {
    id: "map:windows-refused",
    path: "/overview",
    widths: ["desktop"],
    mock: { hqMatches: "several" },
    open: [
      { css: "[data-window-layer]", wait: "attached" },
      { css: '[data-testid="section-nav"] >> text=Dashboard' },
      { css: ".tr-overview", wait: "detached" },
      { viewport: [1440, 400] },
      { css: '.hud-topbar [data-testid="ask-toggle"]' },
      { css: '[data-testid="ask-sidebar"] [data-testid="composer-input"]', fill: "which sessions are on the ingest launch?" },
      { css: '[data-testid="ask-sidebar"] [data-testid="composer-send"]' },
      { testId: "ask-match-map-all" },
      { css: "[data-map-refused]", wait: "visible" },
      { css: '.hud-topbar [data-testid="ask-toggle"]' },
      { viewport: [1440, 960] },
    ],
    present: "[data-map-refused]",
  },
  // The toolbar's action error: a chip-menu action the server refused (here Favorite), under the
  // toolbar. Desktop: a phone's map opens the chip menu by long-press (the row sheet, captured).
  {
    id: "map:toolbar-error",
    path: "/overview",
    widths: ["desktop"],
    api: { "re:/api/sessions/[^/]+/favorite": { status: 409, json: { detail: "That session was archived on another device." } } },
    open: [MAP_EXPAND, { css: "[data-chip-menu] >> nth=0" }, { role: "menuitem", name: "Favorite session" }],
    expectCss: "[data-map-error]",
  },
  // No session on the map while a window is open: the overlay variant of the empty state (the
  // window layer stays mounted and live, #208). Reached as it happens: a window open, then the
  // re-read after a chip action (Favorite) answers an empty list. Desktop: a phone map hosts no
  // windows.
  {
    id: "map:empty-windows",
    path: "/overview",
    widths: ["desktop"],
    // No projects, so the sessions' own clusters are the whole map and the re-read empties it.
    api: { "/api/projects": { projects: [] } },
    open: [
      MAP_EXPAND,
      mapChip("Rate-limit the public ingest API"),
      // Minimised: still open (the tray), and the overlay's line is not under the window.
      { role: "button", name: "Minimize window" },
      { api: { "/api/sessions": EMPTY_SESSIONS } },
      { css: "[data-chip-menu] >> nth=0" },
      { role: "menuitem", name: "Favorite session" },
    ],
    present: ".tr-ov-state--overlay",
  },
  // A window whose terminal is held by another viewer: READ-ONLY in the window's one bar.
  {
    id: "map:window-readonly",
    path: "/overview",
    widths: ["desktop"],
    term: { role: "secondary", holder: { label: "Mac · Chrome", since: Math.floor(Date.now() / 1000) - 300 } },
    open: [MAP_EXPAND, mapChip("Rate-limit the public ingest API")],
    present: "[data-window-readonly]",
  },
  // ---- /PR4a map ----
  { id: "route:templates", path: "/templates" },
  { id: "route:playbooks", path: "/library/playbooks" },
  { id: "route:skills", path: "/library/skills" },
  { id: "route:memory", path: "/library/memory", mock: { memory: true }, present: "[role='group'][aria-label='Project orbital-api']" },
  { id: "route:variables", path: "/library/variables", mock: { variables: true }, present: "article[aria-label='deploy_token']" },
  // ---- PR4b library A ---- (#1574 Phase 10: templates, variables, memory)
  // Suggestions on demand: the analysed result, its dossier cards and the withheld note.
  {
    id: "route:templates-suggestions",
    path: "/templates",
    mock: { suggestions: true },
    open: [{ role: "tab", name: /Suggested/ }],
    present: "[data-suggestion]",
  },
  // The editor: fields, the {{variable}} list, tags and the image strip.
  { id: "route:template-editor", path: "/templates/tpl_review", present: "fieldset textarea" },
  // Delete sits behind the Phase 3 confirm, the danger plate naming the thing.
  {
    id: "popup:template-delete",
    path: "/templates",
    open: [{ role: "button", name: "Delete Security review" }],
    expect: "dialog",
  },
  // The variable form: a secret's edit form with its Advanced disclosure open.
  {
    id: "route:variables-form",
    path: "/library/variables",
    mock: { variables: true },
    open: [{ role: "button", name: "Edit deploy_token" }, { css: "form details summary" }],
    present: "form details[open]",
  },
  // Memory as a graph: the shared React Flow theme (#1573) with the page's legend.
  {
    id: "route:memory-graph",
    path: "/library/memory",
    mock: { memory: true },
    open: [{ role: "button", name: "Graph" }],
    openPhone: [{ role: "tab", name: "Graph" }],
    present: ".react-flow__node",
  },
  // Quarantine: the amber edge (the page's only amber), with Release and Delete.
  {
    id: "route:memory-quarantine",
    path: "/library/memory",
    mock: { memory: true },
    openPhone: [{ role: "tab", name: /Trays/ }],
    present: "[aria-label='Quarantined deploy-notes.md']",
  },
  // Memory off: the quiet frame with the switch to turn it on.
  {
    id: "route:memory-off",
    path: "/library/memory",
    mock: { memory: true, memoryOff: true },
    present: "button:has-text('Turn memory on')",
  },
  // The first read: Acquiring, in words.
  {
    id: "route:memory-acquiring",
    path: "/library/memory",
    mock: { memory: true },
    holdAsset: "/api/memory(\\?|$)",
    present: "[data-testid='memory-acquiring']",
  },
  // Clear behind the type-the-name confirm.
  {
    id: "popup:memory-erase",
    path: "/library/memory",
    mock: { memory: true },
    open: [{ role: "button", name: "Clear…" }],
    expect: "dialog",
  },
  // ---- /PR4b library A ----
  // ---- PR4b library B ---- (#1574 Phase 10: automations, playbooks, skills and the gallery)
  // The list with three automations (an enabled one, one waiting for consent, one off): counter
  // tiles, LED rows, the consent row on its degraded edge.
  {
    id: "route:automations-list",
    path: "/mission/automations",
    mock: { automationsShowcase: true },
    present: "[data-testid='automation-row']",
  },
  // The row ⋯ menu: anchored on a desktop, a sheet on a phone (Phase 3 chrome).
  {
    id: "popup:automation-menu",
    path: "/mission/automations",
    mock: { automationsShowcase: true },
    open: [{ css: "[data-testid='automation-row'][data-id='a-nightly-audit'] [data-testid='automation-more']" }],
    present: "[data-testid='automation-menu']",
  },
  // One automation's runs: the list and a failed run's detail with its timeline.
  {
    id: "route:automation-runs",
    path: "/mission/automations/a-nightly-audit?run=run-26",
    mock: { automationsShowcase: true },
    present: "[data-testid='run-detail']",
  },
  // The editor on a weekly session: segments, the weekday group, square fields.
  {
    id: "route:automation-editor",
    path: "/mission/automations/a-release-notes/edit",
    mock: { automationsShowcase: true },
    present: "[data-testid='editor-fields']",
  },
  // A playbook's detail: callsign sections, the flow (canvas on a desktop, the list on a phone),
  // the stamps and the actions.
  { id: "route:playbook-detail", path: "/library/playbooks/ship-it", present: "h1" },
  // The editor: the page around the canvas and the step inspector.
  { id: "route:playbook-editor", path: "/library/playbooks/ship-it/edit", present: "h1" },
  // A step selected: the inspector with its fields, completion rules and prompts.
  {
    id: "route:playbook-step",
    path: "/library/playbooks/ship-it/edit",
    open: [{ css: ".react-flow__node" }],
    present: "text=Prompts follow the step instructions",
  },
  // Review update: the three-way compare of a local skill behind the gallery's newer version.
  {
    id: "route:gallery-compare",
    path: "/library/gallery/skills/" + "a".repeat(64) + "?update=my-skill",
    mock: { galleryCompare: true },
    present: "[aria-label^='Versions of']",
  },
  // The gallery's browse page: three skills as dossier cards (board G1).
  {
    id: "route:gallery-browse",
    path: "/library/gallery/skills",
    mock: { galleryShowcase: true },
    present: "[data-testid='content-gallery'] article",
  },
  // Skills with nothing saved yet: the empty state (`route:skills` is the same page, unpinned).
  { id: "route:skills-empty", path: "/library/skills", present: "h2:has-text('No skills yet')" },
  // Share bundles (#1714): the export picker with a playbook's skills included, and the import
  // preview with a rename, a refused dependency and an invalid playbook (boards A and B).
  {
    id: "popup:share-export",
    path: "/library/skills",
    api: { "/api/share/catalog": { method: "GET", status: 200, json: SHARE_CATALOG } },
    open: [{ role: "button", name: "Export…" }, { css: "[data-testid='share-export-row-playbook:research-brief'] input" }],
    expect: "dialog",
  },
  {
    id: "popup:share-import",
    path: "/library/skills",
    api: { "/api/share/import/preview": { method: "POST", status: 200, json: SHARE_PREVIEW } },
    open: [
      { role: "button", name: "Import…" },
      { testId: "share-file", upload: "research-kit.battlelab" },
      { css: "[data-testid='share-preview-row-skill:research-review'] input" },
    ],
    expect: "dialog",
  },
  // ---- /PR4b library B ----
  { id: "route:new-project", path: "/projects/new" },
  { id: "route:settings-interface", path: "/settings/appearance" },
  // ---- PR4b settings A ---- (#1574 Phase 11: the shell, Appearance and AI pages)
  // Motion: the Interface page in each mode (Off is the sweep's default above).
  { id: "route:settings-interface-full", path: "/settings/appearance", motion: "full" },
  { id: "route:settings-interface-reduced", path: "/settings/appearance", motion: "reduced" },
  // The phone grouped index (chevrons, the endpoint's model + LED); a desktop lands on a section.
  { id: "route:settings-index", path: "/settings", widths: ["phone"], present: "a[href$='/settings/ai-endpoint']" },
  { id: "route:settings-terminal", path: "/settings/terminal-appearance" },
  { id: "route:settings-session-defaults", path: "/settings/session-defaults" },
  { id: "route:settings-endpoint", path: "/settings/ai-endpoint" },
  {
    id: "route:settings-hq-calls",
    path: "/settings/ai-hq-calls",
    api: {
      // The COMPLETE public settings shape (the profiles section reads every list): a partial
      // fixture captured a page that throws, not the page. `contact_defaults` is what the server
      // returns (hq_calls_settings.public: each General's own group, style and speed).
      "/api/voice/settings": { stt: { base_url: "http://gpu-1.lan:8000/v1", model: "whisper-1", api_key_set: false }, tts: { base_url: "http://gpu-1.lan:8880/v1", model: "kokoro", api_key_set: true }, voices: { woman: "af_heart", man: "am_michael" }, turn_taking: "natural", interrupt: true, mic: "open", answers: "brief", captions: true, configured: true, contacts: {}, contact_defaults: {"asia-woman": {"group": "woman", "style": "calm", "speed": 1.0}, "asia-man": {"group": "man", "style": "serious", "speed": 0.95}, "europe-woman": {"group": "woman", "style": "narrator", "speed": 1.0}, "europe-man": {"group": "man", "style": "neutral", "speed": 1.0}, "russia-woman": {"group": "woman", "style": "serious", "speed": 1.0}, "russia-man": {"group": "man", "style": "dramatic", "speed": 1.0}, "us-woman": {"group": "woman", "style": "energetic", "speed": 1.05}, "us-man": {"group": "man", "style": "dramatic", "speed": 1.0}, "africa-woman": {"group": "woman", "style": "energetic", "speed": 1.0}, "africa-man": {"group": "man", "style": "calm", "speed": 0.95}, "southamerica-woman": {"group": "woman", "style": "dramatic", "speed": 1.05}, "southamerica-man": {"group": "man", "style": "calm", "speed": 1.0}, "romania-woman": {"group": "woman", "style": "dramatic", "speed": 1.0}}, language: "auto", tts_styles: ["calm", "dramatic"], tts_languages: ["en", "de", "fr"], tts_voices: ["af_heart", "am_michael"], preview_languages: ["en", "de", "fr", "es", "it", "pt", "nl", "pl", "ru", "ja", "zh", "hi", "ko"], stt_detects_language: true, tts_accepts_language: true },
      "/api/voice/capabilities": { configured: true, runtime: true, available: true, reason: "", captions: true, mic: "open", active_call: null, destinations: { stt: "lan", tts: "lan", ai: "lan", stt_host: "gpu-1.lan:8000", tts_host: "gpu-1.lan:8880", ai_host: "llm.lan" } },
      "/api/voice/local": { installed: { runtime: true, server: true }, token: true, settings_token_mismatch: false, unit: true, systemd: true, stt: "base", last_error: null, stale: null, url: "http://127.0.0.1:8766/v1", limits: "hard", running: true, in_use: false, voices: { woman: "af_heart", man: "am_michael" } },
    },
    // The capture counts only once the route's own content rendered (each General's row).
    present: "[data-testid='hq-calls-contacts']",
  },
  // A failed check: a new key (masked — a fake fixture value) and the endpoint refusing it.
  {
    id: "route:settings-endpoint-failed",
    path: "/settings/ai-endpoint",
    open: [
      { role: "button", name: "Replace key" },
      { css: "input[type='password']", fill: "sk-FAKE-fixture-0000000000000000" },
      { role: "button", name: "Test", partial: false },
    ],
    api: { "/api/ai-review/endpoint/test": { status: 502, json: { detail: "The endpoint refused the key (HTTP 401)." } } },
    present: "input[type='password']",
  },
  { id: "route:settings-mission-control", path: "/settings/ai-mission-control" },
  // Prompts: the catalog with an edited row open, a guarded row and a default one…
  {
    id: "route:settings-prompts",
    path: "/settings/ai-prompts",
    api: { "/api/prompts": { prompts: [
        {
          id: "ai-review-prompt",
          group: "Session review",
          label: "Session review",
          description: "Summarises a session and flags it when it needs you.",
          contract: '{ "title": string, "summary": string, "needs_you": boolean }',
          max_chars: 4000,
          guarded: false,
          guard_suffix: null,
          value: "Review the session. Quote its own words for anything you flag.",
          default: "Review the session.",
          is_default: false,
        },
        {
          id: "mission-judge",
          group: "Mission control",
          label: "Mission judge",
          description: "Settles an objective on evidence quoted verbatim from the session.",
          contract: '{ "met": boolean, "confidence": number, "evidence": [{ "quote": string }] }',
          max_chars: 4000,
          guarded: true,
          guard_suffix: "Answer only from quotes; never invent evidence.",
          value: "Settle the gate only with verbatim quotes from the session.",
          default: "Settle the gate only with verbatim quotes from the session.",
          is_default: true,
        },
        {
          id: "auto-sort",
          group: "Auto-sort",
          label: "Project classifier",
          description: "Assigns an unassigned session to a project.",
          contract: '{ "project_id": string | null, "confidence": number }',
          max_chars: 2000,
          guarded: false,
          guard_suffix: null,
          value: "Pick the project this session belongs to.",
          default: "Pick the project this session belongs to.",
          is_default: true,
        },
      ] } },
    open: [{ role: "button", name: "Session review", partial: true }],
    present: "textarea",
  },
  // …and the catalog that would not load (Retry).
  { id: "route:settings-prompts-error", path: "/settings/ai-prompts" },
  { id: "route:settings-ai-review", path: "/settings/ai-session-review" },
  { id: "route:settings-auto-sort", path: "/settings/ai-auto-sort" },
  { id: "route:settings-activity", path: "/settings/ai-activity" },
  // ---- /PR4b settings A ----

  { id: "route:settings-agents", path: "/settings/agents", api: { "/api/plugins": SB.PLUGINS } },
  { id: "route:settings-security", path: "/settings/security" },
  // ---- PR4b settings B ---- (#1574 Phase 11: agents, system and about; FAKE secrets only,
  // visual/promo/settingsStates.mjs)
  { id: "route:settings-agents-defaults", path: "/settings/agents/defaults", api: { "/api/plugins": SB.PLUGINS } },
  {
    id: "route:agent-add",
    path: "/settings/agents/setup/new",
    api: { "/api/plugins": SB.PLUGINS },
  },
  // The gallery lives on Appearance (Settings A's page): the shot is scrolled to it (`present`), not a
  // section crop — a phone's crop is taller than the screen and stitches the sticky top bar in.
  {
    id: "route:settings-hq-gallery",
    path: "/settings/appearance",
    present: "[data-testid='hq-gallery']",
  },
  {
    id: "route:settings-2fa-setup",
    path: "/settings/security",
    mock: { config: { two_factor_enabled: false } },
    api: { "/api/2fa/enroll": SB.TWOFA_ENROLL },
    open: [{ role: "button", name: "Enable two-factor auth" }],
    present: "[data-testid='twofa-enroll'], code",
  },
  {
    id: "route:settings-loginoff",
    path: "/settings/security",
    mock: { config: { auth_mode: "none" } },
  },
  { id: "route:settings-sharing", path: "/settings/sharing", api: { "/api/shares": SB.SHARES } },
  {
    id: "route:settings-sharing-revoke-all",
    path: "/settings/sharing",
    api: { "/api/shares": SB.SHARES },
    open: [{ role: "button", name: "Revoke all" }],
  },
  {
    id: "route:settings-updates",
    path: "/settings/updates",
    api: { "/api/update/settings": SB.UPDATE_SETTINGS, "/api/update/progress": SB.UPDATE_RUNNING },
  },
  { id: "route:settings-host", path: "/settings/system", api: { "/api/system": SB.SYSTEM } },
  {
    id: "route:settings-resources",
    path: "/settings/resources",
    api: { "/api/system": SB.SYSTEM, "/api/system/resources": SB.RESOURCES },
  },
  { id: "route:settings-analytics", path: "/settings/analytics" },
  // ---- /PR4b settings B ----
  // ---- brand (#1590) ---- the kit's lockup with its tagline on Settings → About.
  { id: "route:settings-about", path: "/settings/about", expectCss: "[data-brand-lockup]" },
  // ---- /brand ----
  // ---- PR3 chrome ----
  // Session chrome (#1568 Phase 6): the sidebar list and its states, the pane head, the terminal
  // chrome and the compose bar. On a phone the list is in the drawer.
  { id: "route:sidebar", path: S1, phoneFirst: DRAWER },
  { id: "route:sidebar-folded", path: S1, phoneFirst: DRAWER, storage: { "tr-sidebar-filters-open": "0" } },
  {
    id: "route:sidebar-empty",
    path: S1,
    phoneFirst: DRAWER,
    api: { "/api/sessions": { sessions: [], total: 0, live_total: 0, next_offset: null, facets: { projects: [], engines: [] } } },
  },
  {
    id: "route:sidebar-error",
    path: S1,
    phoneFirst: DRAWER,
    api: { "/api/sessions": { status: 500, json: { detail: "sessions unavailable" } } },
  },
  {
    id: "route:sidebar-rename",
    path: S1,
    phoneFirst: DRAWER,
    open: [{ role: "button", name: "Session actions" }, { role: "menuitem", name: "Rename session" }],
    expectCss: "input[aria-label='Session title']",
  },
  // The pane: the head's chips, the terminal and the folded compose bar (open on a phone).
  { id: "route:session-pane", path: S1 },
  // ---- headers ---- (#1589: the v5 top bar with the brand kit's logo, and the session head that
  // folds into one Actions menu on a touch tablet). Sweep with `--widths 1440,1100,834,412,390,320`
  // (touch at ≤1180) for the tablet and phone forms; the default 1440 / 390 covers both ends.
  { id: "headers:topbar", path: S1, height: 140 },
  { id: "headers:topbar-menu", path: S1, open: [{ role: "button", name: "Library menu" }] },
  { id: "headers:session-head", path: S1, height: 200 },
  // ---- /headers ----
  // The compose bar open: textarea, key chips, Talk and Send.
  { id: "route:session-compose", path: S1, open: [{ role: "button", name: "Open compose box" }], openPhone: [], present: "textarea" },
  // The terminal's chrome states (#1568): reconnecting (the radar), refused, and a read-only
  // viewer behind the take-over banner. Nothing is drawn over the xterm canvas itself.
  // A busy master (4409) is retried: the pane shows the reconnecting chip with the radar.
  { id: "route:term-reconnecting", path: S1, term: { closeCode: 4409 }, at: 1500 },
  { id: "route:term-rejected", path: S1, term: { closeCode: 4403, closeReason: "forbidden" }, at: 2500 },
  {
    id: "route:term-readonly",
    path: S1,
    term: { role: "secondary", holder: { label: "Mac · Chrome", since: Math.floor(Date.now() / 1000) - 300 } },
    expectCss: "[aria-label='Take over this session']",
  },
  // ---- /PR3 chrome ----
  // ---- PR3 files ---- (#1568: the Files panel shell, tree and upload; the git tab and viewer are #950's)
  {
    id: "files:tree-open",
    path: S1,
    ...withFiles([{ css: "[data-file-row][data-kind='dir'] [data-row-main]" }]),
    expectCss: "[data-file-row][data-kind='dir'] [aria-expanded='true']",
  },
  // The root listing held: the tree's skeleton (a scan band in Full; the sweep shoots Off).
  { id: "files:tree-loading", path: S1, ...withFiles([]), holdAsset: "/api/files/list(\\?|$)", expectCss: "[data-file-tree] [role='status']" },
  {
    id: "files:tree-unavailable",
    path: S1,
    ...withFiles([]),
    api: { "/api/files/list": { status: 503, json: { detail: "Could not read this folder." } } },
    expectCss: "[data-file-tree] [role='alert']",
  },
  {
    id: "files:tree-truncated",
    path: S1,
    ...withFiles([]),
    api: {
      "/api/files/list": {
        path: "/home/commander/code/orbital-api",
        parent: "/home/commander/code",
        root: "/home/commander/code",
        total: 900,
        complete: false,
        truncated: true,
        entries: [
          { name: "src", path: "/home/commander/code/orbital-api/src", kind: "dir", size: 4096, mtime: 0 },
          { name: "package.json", path: "/home/commander/code/orbital-api/package.json", kind: "file", size: 1200, mtime: 0 },
          { name: "README.md", path: "/home/commander/code/orbital-api/README.md", kind: "file", size: 3100, mtime: 0 },
        ],
      },
    },
    expectCss: "[data-file-tree] [data-file-row]",
  },
  {
    id: "files:panel-unavailable",
    path: S1,
    ...withFiles([]),
    api: { "/api/files/capabilities": { ok: false, reason: "The file service is not reachable from this box." } },
    expectCss: "[data-file-panel] [role='alert']",
  },
  // A file mid-upload: the batch is accepted, the upload itself never answers, so the row stays SENDING.
  {
    id: "files:upload-queue",
    path: S1,
    ...withFiles([{ css: "[data-upload-files]", upload: "notes.md" }]),
    api: { "/api/files/upload/batch": { batch_id: "b1", files: 1, bytes: 6, files_limit: 500, bytes_limit: 262144000, file_limit: 26214400 } },
    holdAsset: "/api/files/upload(\\?|$)",
    expectCss: "[data-upload-queue]",
  },
  // "Add the path to the message": the row's control lit for its acknowledgement window. Desktop
  // only: on a phone the sheet closes on send (#792), so the flash is a dock state.
  {
    id: "files:send-flash",
    path: S1,
    widths: ["desktop"],
    ...withFiles([{ css: "[data-file-row][data-kind='file'] button[aria-label^='Add ']" }]),
    present: "[data-file-row][data-kind='file'] button[aria-label^='Add ']",
  },
  // ---- /PR3 files ----
  // ---- PR4a rail ---- (#1573 Phase 8: the route shell, the mission rail, the new-mission page and
  // Library → Checklists; the console is the console slice's block)
  // Every LED the rail draws (needs you, running, starting, failed, review, draft, done) and the
  // NEEDS YOU preview on the landing.
  { id: "route:mission-needs-you", path: "/mission", api: { "/api/missions": RAIL_LIST(RAIL_ROWS) } },
  // The same rows in the phone's drawer (the rail is the shell's drawer at ≤800px).
  {
    id: "route:mission-rail-drawer",
    path: "/mission",
    widths: ["phone"],
    api: { "/api/missions": RAIL_LIST(RAIL_ROWS) },
    phoneFirst: MISSION_DRAWER,
    expect: "dialog",
  },
  // The first read, held: Acquiring over skeleton rows.
  {
    id: "route:mission-rail-loading",
    path: "/mission",
    holdAsset: "/api/missions(\\?|$)",
    phoneFirst: MISSION_DRAWER,
    at: 1500,
  },
  // The store would not answer: the rail's error with its Retry (and no NEEDS YOU claim).
  {
    id: "route:mission-rail-error",
    path: "/mission",
    phoneFirst: MISSION_DRAWER,
    api: { "/api/missions": { ...RAIL_LIST([]), store_error: "the mission store is restarting" } },
  },
  // Nothing tracked yet.
  { id: "route:mission-rail-empty", path: "/mission", phoneFirst: MISSION_DRAWER, api: { "/api/missions": RAIL_LIST([]) } },
  // The rail's other empty wordings: a search that matches nothing, and an empty Archived scope.
  {
    id: "route:mission-rail-filtered-empty",
    path: "/mission",
    phoneFirst: MISSION_DRAWER,
    api: { "/api/missions": RAIL_LIST([]) },
    open: [{ css: "input[aria-label='Search missions']", fill: "no such mission" }],
    present: "[data-testid='rail-no-missions']:has-text('match these filters')",
  },
  {
    id: "route:mission-rail-archived-empty",
    path: "/mission",
    phoneFirst: MISSION_DRAWER,
    api: { "/api/missions": RAIL_LIST([]) },
    open: [{ testId: "rail-scope-archived" }],
    present: "[data-testid='rail-no-missions']:has-text('Nothing archived yet')",
  },
  // More missions than the first page: "Load more — x of y" under the rows.
  {
    id: "route:mission-rail-more",
    path: "/mission",
    phoneFirst: MISSION_DRAWER,
    api: { "/api/missions": { ...RAIL_LIST(RAIL_ROWS), total: RAIL_ROWS.length + 5 } },
    present: "[data-testid='rail-load-more']",
  },
  // The Archived scope is a filter (#959): the landing's preview says "in current filters" and
  // offers Clear filters — answered empty, still reading, and unreadable. On a phone the scope tabs
  // are in the drawer: switch there and close it, so the landing is what the shot shows.
  {
    id: "route:mission-needs-filtered",
    path: "/mission",
    open: [{ testId: "rail-scope-archived" }],
    openPhone: [...MISSION_DRAWER, { testId: "rail-scope-archived" }, { press: "Escape" }],
  },
  {
    id: "route:mission-needs-loading",
    path: "/mission",
    holdAsset: "/api/missions(\\?|$)",
    open: [{ testId: "rail-scope-archived" }],
    openPhone: [...MISSION_DRAWER, { testId: "rail-scope-archived" }, { press: "Escape" }],
    at: 1500,
  },
  {
    id: "route:mission-needs-error",
    path: "/mission",
    api: { "/api/missions": { ...RAIL_LIST([]), store_error: "the mission store is restarting" } },
    open: [{ testId: "rail-scope-archived" }],
    openPhone: [...MISSION_DRAWER, { testId: "rail-scope-archived" }, { press: "Escape" }],
  },
  // The overview read failed: the route's error line with its Retry.
  { id: "route:mission-overview-error", path: "/mission", api: { "/api/pulse": { status: 503, json: { detail: "restarting" } } } },
  // The orchestrator has failed twice running: the read-only health banner above the console.
  { id: "route:mission-health", path: "/mission", api: { "/api/pulse/orchestrator": ORCH_FAILING } },
  // The template picker, opened from the brief's Template button.
  { id: "popup:mission-template-picker", path: "/mission", open: [{ testId: "new-mission-template" }], expect: "dialog" },
  // Library → Checklists with checklists: the cards, the default's lit rail, the dashed New card.
  { id: "route:checklists-list", path: "/mission/checklists", mock: { config: CHECKLISTS_CONFIG } },
  // One checklist: the tally, the numbered track, the first step open, the save bar.
  {
    id: "route:checklists-editor",
    path: "/mission/checklists",
    mock: { config: CHECKLISTS_CONFIG },
    open: [{ testId: "playbook-card" }],
    expectCss: "[data-testid='playbook']",
  },
  // Leaving with an unsaved draft asks first (#1221). Desktop: the rail is in the phone's drawer.
  {
    id: "popup:checklists-leave",
    path: "/mission/checklists",
    widths: ["desktop"],
    mock: { config: CHECKLISTS_CONFIG },
    open: [{ testId: "playbook-card" }, { testId: "playbook-remove" }, { testId: "rail-mission" }],
    expect: "dialog",
  },
  // The server refuses a start: the brief and project stay, and the form says why in its own words.
  {
    id: "route:mission-start-refused",
    path: "/mission",
    api: {
      "/api/missions": {
        method: "POST",
        status: 409,
        json: { detail: "orbital-api already has a mission starting. Open it from the rail, or wait for it to start." },
      },
    },
    open: [
      { testId: "new-mission-instruction", fill: "Add a token-bucket limiter keyed by API key to the ingest router." },
      { testId: "new-mission-project", select: "orbital-api" },
      { testId: "new-mission-start" },
    ],
    present: "[data-testid='new-mission-error']",
  },
  // ---- /PR4a rail ----
  // ---- PR4a console ---- (#1573: the mission console, its details, plan, decisions and thread)
  // The running console: the frame (title, state chip, primary, ⋯), the Now strip, the thread and, at
  // ≥1400, the details column (Context and Objectives open). On a phone the details are a band.
  { id: "mission:console", path: M1 },
  // Every thread row kind (the board's T8), a judged objective that did not hold, the supervisor's
  // reading and Load older. Tall, so the whole thread is on screen.
  { id: "mission:thread", path: M1, mock: { mission: "showcase" }, height: 2300 },
  // The decisions waiting on the mission: a permission card, the session's own menu, a supervisor
  // nudge and an AI-drafted direction (dashed).
  { id: "mission:decisions", path: M1, mock: { mission: "decisions", missionDecisions: true }, height: 2100 },
  // A question mission control is waiting on: options are 44 px rows.
  { id: "mission:question", path: M1, mock: { mission: "question" } },
  // The plan card: planning (pips + Acquiring), could not plan, ready (Review plan), and the armed
  // launch confirmation (Required / Goal, Worth a look).
  { id: "mission:planning", path: M1, mock: { mission: "planning" } },
  { id: "mission:no-plan", path: M1, mock: { mission: "noplan" } },
  { id: "mission:plan-manual", path: M1, mock: { mission: "noplan" }, open: [{ testId: "mission-plan-manually" }], present: "[data-testid='mission-plan-manual']" },
  { id: "mission:plan-ready", path: M1, mock: { mission: "ready" }, open: [{ testId: "mission-plan-review" }], present: "[data-testid='mission-plan-fields']" },
  { id: "mission:plan-armed", path: M1, mock: { mission: "ready" }, open: [{ testId: "mission-begin" }], present: "[data-testid='mission-launch-confirm']", height: 1300 },
  // A start that failed: the failure block with Start again.
  { id: "mission:failed", path: M1, mock: { mission: "failed" } },
  // No AI endpoint: the notice, and the composer that says what it needs.
  {
    id: "mission:no-ai",
    path: M1,
    mock: { config: { pulse: { auto_enabled: false, interval_minutes: 15, window_days: 1, scan_depth: "slow", configured: false } } },
  },
  // Nothing has happened yet: the empty thread and the empty checklist.
  { id: "mission:quiet", path: M1, mock: { mission: "quiet" } },
  // Every objective met: the all-met state and the supervisor's proposal.
  { id: "mission:all-met", path: M1, mock: { mission: "allmet" } },
  // ≤1399: the details are ONE band under the head; open on a phone.
  { id: "mission:details-band", path: M1, widths: ["phone"], openPhone: [{ testId: "details-toggle" }], present: "[data-testid='mission-details']" },
  // Context: the git line's error, View screen (`<pre>`), the relay and Start sub-agent. On a phone
  // Context is the details band's first section, open: the band, then the control.
  {
    id: "mission:context-git-error",
    path: M1,
    mock: { missionContext: "gitError" },
    openPhone: [{ testId: "details-toggle" }],
    present: "[data-testid='context-git-error']",
  },
  {
    id: "mission:screen",
    path: M1,
    open: [{ testId: "view-screen" }],
    openPhone: [{ testId: "details-toggle" }, { testId: "view-screen" }],
    present: "[data-testid='mission-screen'] pre",
    height: 1400,
  },
  {
    id: "mission:spawn",
    path: M1,
    mock: { mission: "spawn" },
    open: [{ testId: "spawn-open" }],
    openPhone: [{ testId: "details-toggle" }, { testId: "spawn-open" }],
    present: "[data-testid='spawn-card']",
    height: 1500,
  },
  // Objectives: judged evidence with its verbatim quotes, the row menu, rename and the Follow-through
  // and Timeline disclosures.
  // On a phone each is in the details band, with the sections above it folded.
  {
    id: "mission:objective-evidence",
    path: M1,
    mock: { mission: "showcase" },
    open: [{ testId: "objective-evidence-toggle" }],
    openPhone: [{ testId: "details-toggle" }, { testId: "detail-context" }, { testId: "objective-evidence-toggle" }],
    present: "[data-testid='objective-evidence']",
    height: 1500,
  },
  {
    id: "mission:follow-through",
    path: M1,
    mock: { mission: "showcase" },
    open: [{ testId: "detail-context" }, { testId: "detail-objectives" }, { testId: "detail-followThrough" }],
    openPhone: [{ testId: "details-toggle" }, { testId: "detail-context" }, { testId: "detail-objectives" }, { testId: "detail-followThrough" }],
    present: "[data-testid='mission-auto-choose']",
    height: 1300,
  },
  {
    id: "mission:timeline",
    path: M1,
    mock: { mission: "showcase" },
    open: [{ testId: "detail-context" }, { testId: "detail-objectives" }, { testId: "detail-timeline" }],
    openPhone: [{ testId: "details-toggle" }, { testId: "detail-context" }, { testId: "detail-objectives" }, { testId: "detail-timeline" }],
    // The first row, not the whole list: a list taller than the column would be scrolled to.
    present: "[data-testid='timeline-row']",
    height: 1300,
  },
  // An objective dragged by its handle: the row lifts on the dashed accent outline (shot mid-drag).
  {
    id: "mission:objective-drag",
    path: M1,
    open: [{ css: "[aria-label^='Reorder']:visible", drag: [0, 60] }],
    openPhone: [{ testId: "details-toggle" }, { testId: "detail-context" }, { css: "[aria-label^='Reorder']:visible", drag: [0, 60] }],
    present: "[class*='objRowDragging']",
  },
  // A reorder the server refused (409: the list changed): the rows settle on the saved order and the
  // alert says so beside the list. Reached by the row menu's Move down, which sends the same op.
  {
    id: "mission:objective-refused",
    path: M1,
    api: { ["/api/missions/msn_" + "1a2b3c4d".repeat(4) + "/objectives"]: { method: "PATCH", status: 409, json: { detail: "The objectives changed since this list was read." } } },
    open: [{ css: "[aria-label^='Actions for']:visible" }, { css: "[role='menuitem'][aria-label^='Move '][aria-label$=' down']" }],
    openPhone: [
      { testId: "details-toggle" },
      { testId: "detail-context" },
      { css: "[aria-label^='Actions for']:visible" },
      { css: "[role='menuitem'][aria-label^='Move '][aria-label$=' down']" },
    ],
    present: "[data-testid='objectives-reorder-refused']",
  },
  // The objectives' four empty states (each says something different, #942): the read failed
  // (unreadable), still being worked out (pending), could not be produced (unavailable), and none.
  // Context is folded first (on a phone, in the details band) so the Objectives are in the shot.
  {
    id: "mission:objectives-unreadable",
    path: M1,
    open: [{ testId: "detail-context" }],
    api: { ["/api/missions/" + M1.split("m=")[1] + "/objectives"]: { method: "GET", status: 503, json: { detail: "the mission store is restarting" } } },
    openPhone: [{ testId: "details-toggle" }, { testId: "detail-context" }],
    present: "[data-testid='objectives-unreadable']",
  },
  { id: "mission:objectives-pending", path: M1, open: [{ testId: "detail-context" }], mock: { mission: "planning" }, openPhone: [{ testId: "details-toggle" }, { testId: "detail-context" }], present: "[data-testid='objectives-pending']" },
  { id: "mission:objectives-unavailable", path: M1, open: [{ testId: "detail-context" }], mock: { mission: "noplan" }, openPhone: [{ testId: "details-toggle" }, { testId: "detail-context" }], present: "[data-testid='objectives-unavailable']" },
  { id: "mission:objectives-empty", path: M1, open: [{ testId: "detail-context" }], mock: { mission: "quiet" }, openPhone: [{ testId: "details-toggle" }, { testId: "detail-context" }], present: "[data-testid='objectives-empty']" },
  // A title past the row's two-line clamp: clamped with its disclosure, then opened.
  {
    id: "mission:objective-clamped",
    path: M1,
    mock: { mission: "longobjective" },
    open: [{ testId: "detail-context" }],
    openPhone: [{ testId: "details-toggle" }, { testId: "detail-context" }],
    present: "[data-testid='objective-title-toggle'][aria-expanded='false']",
  },
  {
    id: "mission:objective-unclamped",
    path: M1,
    mock: { mission: "longobjective" },
    open: [{ testId: "detail-context" }, { css: "[data-testid='objective-title-toggle'][aria-expanded='false']" }],
    openPhone: [...[{ testId: "details-toggle" }, { testId: "detail-context" }], { css: "[data-testid='objective-title-toggle'][aria-expanded='false']" }],
    present: "[data-testid='objective-title-toggle'][aria-expanded='true']",
  },
  // An objective's ⋯: the row menu (a sheet on a phone), and its Rename form open in the row. Tall:
  // at 960 the objectives sit below the fold, and the scroll that reaches them closes a menu.
  {
    id: "popup:objective-menu",
    path: M1,
    height: 1400,
    open: [{ css: "[data-testid='objective-menu'] >> nth=1" }],
    openPhone: [...[{ testId: "details-toggle" }, { testId: "detail-context" }], { css: "[data-testid='objective-menu'] >> nth=1" }],
    expectCss: "[role='menuitem']",
  },
  {
    id: "mission:objective-rename",
    path: M1,
    height: 1400,
    open: [{ css: "[data-testid='objective-menu'] >> nth=1" }, { testId: "objective-rename" }],
    openPhone: [...[{ testId: "details-toggle" }, { testId: "detail-context" }], { css: "[data-testid='objective-menu'] >> nth=1" }, { testId: "objective-rename" }],
    present: "[data-testid='objective-rename-input']",
  },
  // The ⋯ overflow's two-tap confirmations: the first tap arms the item and the menu says what the
  // second will do.
  {
    id: "popup:mission-confirm-abandon",
    path: M1,
    open: [{ testId: "mission-overflow" }, { testId: "mission-abandon" }],
    present: "[data-testid='mission-confirm-abandon']",
  },
  {
    id: "popup:mission-confirm-archive",
    path: M1,
    open: [{ testId: "mission-overflow" }, { testId: "mission-archive" }],
    present: "[data-testid='mission-confirm-archive']",
  },
  {
    id: "popup:mission-confirm-failed",
    path: M1,
    open: [{ testId: "mission-overflow" }, { testId: "mission-failed" }],
    present: "[data-testid='mission-failed']:has-text('Confirm mark failed')",
  },
  // Nothing on the timeline yet (Context and Objectives folded so it is in view).
  {
    id: "mission:timeline-empty",
    path: M1,
    mock: { mission: "quiet" },
    open: [{ testId: "detail-context" }, { testId: "detail-objectives" }, { testId: "detail-timeline" }],
    openPhone: [{ testId: "details-toggle" }, { testId: "detail-context" }, { testId: "detail-objectives" }, { testId: "detail-timeline" }],
    present: "[data-testid='timeline-empty']",
  },
  // A turn in flight: the composer's message is the pending turn, `AskWorking` under it (its reply
  // never arrives, so the shot holds the wait).
  {
    id: "mission:pending",
    path: M1,
    holdAsset: "/message/stream",
    open: [{ testId: "composer-input", fill: "Which objective is furthest from done?" }, { testId: "composer-send" }],
    present: "[data-testid='turn-pending']",
  },
  // ---- /PR4a console ----
  // ---- PR5a outside ---- (#1596, Phase 12: the pages outside the SPA)
  // The server's own pages (sign-in, TOTP, password, device link, the share placeholder) are
  // rendered by the Python app: `served` ids are photographed by that spec's capture mode
  // (NIGHTJAR_CAPTURE_DIR), at the same widths and themes, named like the sweep's.
  ...[
    "page:login",
    "page:login-error",
    "page:login-qr",
    "page:totp",
    "page:totp-error",
    "page:password",
    "page:password-error",
    "page:link-confirm",
    "page:link-approved",
    "page:link-denied",
    "page:link-invalid",
    "page:share-placeholder",
  ].map((id) => ({ id, path: "/login", served: "e2e/appmode-served-pages.spec.ts" })),
  // The Home Free connect page, through its own harness seam (no relay): the form, the verified
  // gate, the dialog, connecting, the error card (a relay `offline` frame on the real mount path),
  // and the session bar over the streamed app.
  ...(() => {
    const harness = (mode) =>
      `window.__battlelabConnectHarness = { holdMs: 60, makeWebSocket: () => { const ws = { binaryType: "arraybuffer", readyState: 1, onmessage: null, onopen: null, onclose: null, onerror: null, send(d) { if (${JSON.stringify(mode)} === "offline" && typeof d === "string" && d.includes('"hello"')) setTimeout(() => ws.onmessage && ws.onmessage({ data: JSON.stringify({ t: "error", code: "offline" }) }), 0); }, close() {} }; return ws; }, ${
        mode === "paired"
          ? `mountApp: async (_w, _k, _c, o) => { o && o.onEvent && o.onEvent({ type: "paired", deadline: Math.floor(Date.now() / 1000) + 14100, ttl: 14400 }); document.getElementById("app-root").textContent = "[ the streamed BattleLab app ]"; return { teardown() {} }; },`
          : ""
      } };`;
    const ALTCHA = { "re:/altcha/challenge": { algorithm: "SHA-256", challenge: "5feceb66ffc86f38d952786c6d696c79c2dbc239dd4e91b46729d73a27fb57e9", salt: "", signature: "s", maxnumber: 0 } };
    const filled = [
      { css: "#name", fill: "viper-8231" },
      { css: "#key", fill: "stream-secret" },
      { css: "#relay-options summary" },
      { css: "#relay", fill: "https://relay.example" },
      { css: "#relay-options summary" },
      { hold: 400, css: "#gate-hold" },
    ];
    const go = [...filled, { css: "#connect" }];
    return [
      { id: "connect:signed-out", path: "/connect.html", ready: undefined, init: harness("") },
      { id: "connect:verified", path: "/connect.html", init: harness(""), open: filled, present: '#verify-gate[data-state="verified"]' },
      { id: "connect:dialog", path: "/connect.html", open: [{ css: "#how-it-works" }], expect: "dialog" },
      { id: "connect:connecting", path: "/connect.html", init: harness("hang"), api: ALTCHA, open: go, present: "#connect-progress" },
      { id: "connect:error", path: "/connect.html", init: harness("offline"), api: ALTCHA, open: go, present: "#connect-error" },
      { id: "connect:connected", path: "/connect.html", init: harness("paired"), api: ALTCHA, open: go, present: "#session-box" },
    ];
  })(),
  // The public share viewer at /p/<token> (share.html), one id per phase, `/api/public/*` answered
  // per capture. The open conversation is e2e/public-share.spec.ts's full flow.
  ...(() => {
    const P = "/p/tok" + "x".repeat(40);
    const share = (kind) => ({ share: { link: "L1", kind, mode: "view", title: "Fix the auth redirect loop", expires_at: null, name: null, name_required: false }, csrf: "c" });
    const pw = { "/api/public/unlock": { status: 401, json: { password_required: true, tries_left: 2 } } };
    return [
      { id: "share:loading", path: P, document: "/share.html", holdAsset: "/api/public/unlock", at: 1200 },
      { id: "share:password", path: P, document: "/share.html", api: pw, present: "[data-testid=share-password-gate]" },
      {
        id: "share:password-error",
        path: P,
        document: "/share.html",
        api: pw,
        open: [{ css: "#share-pw", fill: "wrong one" }, { role: "button", name: "Open session" }],
        present: "[data-testid=share-password-gate] [role=alert]",
      },
      {
        id: "share:password-wait",
        path: P,
        document: "/share.html",
        api: pw,
        open: [
          { api: { "/api/public/unlock": { status: 429, json: { detail: "too many tries" } } } },
          { css: "#share-pw", fill: "wrong again" },
          { role: "button", name: "Open session" },
        ],
        present: "[data-testid=share-password-gate] [role=alert]",
      },
      { id: "share:unavailable", path: P, document: "/share.html", api: { "/api/public/unlock": { status: 404, json: {} } }, present: "[data-testid=share-unavailable]" },
      { id: "share:relink", path: "/p/", document: "/share.html", api: { "/api/public/share": { status: 401, json: {} } }, present: "[data-testid=share-relink]" },
      {
        id: "share:nocookie",
        path: P,
        document: "/share.html",
        api: { "/api/public/unlock": share("api"), "re:/api/public/(session|events)": { status: 401, json: {} } },
        present: "[data-testid=share-nocookie]",
      },
      { id: "share:offline", path: P, document: "/share.html", api: { "/api/public/unlock": { status: 503, json: { detail: "restarting" } } }, present: "[data-testid=share-offline]" },
      { id: "share:terminal", path: P, document: "/share.html", api: { "/api/public/unlock": share("terminal") }, present: "[data-testid=share-terminal-pending]" },
      {
        id: "share:open",
        path: P,
        document: "/share.html",
        api: {
          "/api/public/unlock": { ...share("api"), share: { ...share("api").share, mode: "send", name: "Alex" } },
          "/api/public/share": { ...share("api"), share: { ...share("api").share, mode: "send", name: "Alex" } },
          "/api/public/session": {
            link: "L1", revision: 3, event_cursor: 3, state: "idle", active_turn: null, model: "gpt-5-codex", omitted_turns: 0,
            pending_requests: [], read_only: false, images: true, can_send: true, mission_held: false, background: false,
            turns: [{ turn_id: "t1", state: "completed", delivery: null, text: "Login bounces between /login and / after the cookie refresh. Find out why.", text_truncated: false, reply: "The refresh middleware re-issues the cookie with `Path=/api`, so the SPA shell never sees it.", reply_ts: 1800000000, reply_truncated: false, attachments: [], tools: [{ name: "read", outcome: "ok", summary: "src/auth/middleware.py · 212 lines" }], tools_truncated: false, author: null, mine: false }],
          },
          "/api/public/events": { link: "L1", revision: 3, next_cursor: 3, events: [] },
        },
        present: "[data-testid=share-composer]",
      },
    ];
  })(),
  // ---- /PR5a outside ----
  // ---- sites ---- (#1597, Phases 12b and 12c: battlelabos.com's sub-pages and the docs site)
  // Static sites outside the SPA: each is photographed by its own spec's capture mode
  // (NIGHTJAR_CAPTURE_DIR). The public site is dark only; the docs site in both themes. `cwd` is
  // relative to web/.
  ...[
    "site:legal",
    "site:privacy",
    "site:privacy-on",
    "site:privacy-off",
    "site:privacy-gpc",
    "site:privacy-nojs",
    "site:404",
    "site:og",
  ].map((id) => ({
    id,
    path: "/",
    served: "e2e/landing-pages.spec.ts",
  })),
  ...["docs:home", "docs:page", "docs:search", "docs:404"].map((id) => ({
    id,
    path: "/",
    served: "e2e/geometry.spec.ts",
    cwd: "../docs/site",
  })),
  // ---- /sites ----
  // ---- map v5 ---- (#1600: the map's v5 composition; windows for terminal AND API sessions)
  // A terminal window beside an API (structured) window: the same armour frame, callsign bar and
  // footer, each footer with its own readout (a grid; a model).
  {
    id: "map:window-api",
    path: "/overview",
    widths: ["desktop"],
    storage: {
      "tr-overview-workspace": [
        {
          key: "claude:7c1e1a00-5b2d-4c8e-9f31-0a6d2e8b4001",
          engine: "claude",
          id: "7c1e1a00-5b2d-4c8e-9f31-0a6d2e8b4001",
          title: "Rate-limit the public ingest API",
          x: 40,
          y: 60,
          w: 620,
          h: 420,
          z: 1,
        },
        {
          key: "claude-api:5a9e0c11-2f3d-4e6a-8b7c-1d2e3f4a5b6c",
          engine: "claude-api",
          id: "5a9e0c11-2f3d-4e6a-8b7c-1d2e3f4a5b6c",
          title: "Draft the ingest limits runbook",
          x: 700,
          y: 60,
          w: 620,
          h: 420,
          z: 2,
        },
      ],
    },
    api: {
      "re:/api/sessions/claude-api(?::|%3A)5a9e0c11-2f3d-4e6a-8b7c-1d2e3f4a5b6c$": {
        id: "claude-api:5a9e0c11-2f3d-4e6a-8b7c-1d2e3f4a5b6c",
        engine: "claude-api",
        uuid: "5a9e0c11-2f3d-4e6a-8b7c-1d2e3f4a5b6c",
        short_uuid: "5a9e0c11",
        cwd: "/home/commander/code/orbital-api",
        project: { kind: "folder", id: "/home/commander/code/orbital-api", name: "orbital-api" },
        last_mtime: Math.floor(Date.now() / 1000) - 120,
        title: "Draft the ingest limits runbook",
        first_user_message: "Draft the ingest limits runbook",
        sticky: false,
        archived: false,
        running: true,
        model_configured: "claude-sonnet-4",
      },
      "re:/api/structured/sessions/claude-api(?::|%3A)5a9e0c11-2f3d-4e6a-8b7c-1d2e3f4a5b6c(\\?.*)?$": {
        session_key: "claude-api:5a9e0c11-2f3d-4e6a-8b7c-1d2e3f4a5b6c",
        revision: 1,
        event_cursor: 1,
        cwd: "/home/commander/code/orbital-api",
        state: "idle",
        active_turn: null,
        model_requested: null,
        model_effective: null,
        turns: [],
        omitted_turns: 0,
        pending_requests: [],
        native: { native_id: "n1", worker: "w1", background_active: false },
        read_only: null,
      },
      "re:/api/structured/sessions/claude-api(?::|%3A)5a9e0c11-2f3d-4e6a-8b7c-1d2e3f4a5b6c/events": {
        session_key: "claude-api:5a9e0c11-2f3d-4e6a-8b7c-1d2e3f4a5b6c",
        revision: 1,
        next_cursor: 1,
        events: [],
      },
    },
    open: [{ role: "button", name: "Collapse session list" }],
    present: "[data-window-foot='api']",
  },
  // ---- /map v5 ----
];

// 834: an iPad in portrait, the width the 800 px breakpoint used to hand the desktop layout.
export const WIDTHS = [1440, 1100, 834, 390];
export const THEMES = ["dark", "light"];
export const PIXEL_RATIOS = [1, 2];
