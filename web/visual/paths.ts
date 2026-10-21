// Visual-capture registry (#96) — the universe of screens the Playwright capture
// screenshots at every screen format. Mirrors demoapp.io/tests/visual/paths.ts,
// adapted to agent-sessions (single-admin form login, no OIDC/seed-from-Docker).
//
// `web/visual/paths.test.ts` enforces: name uniqueness, non-empty description, every
// entry has a `waitFor`, and every `networkidle` wait carries a non-empty `reason`.

export type WaitForSelector = { selector: string; timeoutMs?: number };
export type WaitForTimeout = { timeoutMs: number };
export type WaitForNetworkIdle = {
  kind: "networkidle";
  timeoutMs?: number;
  reason: string;
};
export type WaitFor = WaitForSelector | WaitForTimeout | WaitForNetworkIdle;

export type VisualPath = {
  group: "public" | "authed";
  /** Route to visit (relative to the base URL). */
  path: string;
  /** Stable key — the screenshot filename + manifest id (kebab-case). */
  name: string;
  description: string;
  /** false = anonymous; "admin" = log in via the /login form first. */
  requireAuth: false | "admin";
  waitFor: WaitFor;
  /** Needs the seeded `must_change` account / fixtures (Phase 2). Skipped until seeded. */
  seeded?: boolean;
  /** Synthetic native activity in the real shared pane; no agent is launched. */
  fixture?: "structured-guidance" | "gallery-listing";
  /** An interaction to run after `goto`, before `waitFor` (#1109): the consolidated window
   *  chrome only exists on an OPEN window, and a capture that only visits a route cannot show
   *  it. The one shipped step opens the first session chip as a window. */
  prepare?:
    | "open-first-window"
    | "resource-controls"
    | "resource-advanced"
    | "api-approval"
    | "api-permissions"
    | "playbook-flow"
    | "project-playbooks"
    | "project-playbook-inputs"
    | "playbook-step"
    | "playbook-prompts"
    | "gallery-confirm";
};

/** Screen formats — every area is captured at each (the operator asked for several). */
export const VIEWPORTS = {
  desktop: { width: 1440, height: 900 },
  laptop: { width: 1280, height: 800 },
  tablet: { width: 768, height: 1024 },
  mobile: { width: 390, height: 844 },
  "mobile-sm": { width: 360, height: 740 },
} as const;

export type ViewportName = keyof typeof VIEWPORTS;
export const VIEWPORT_NAMES = Object.keys(VIEWPORTS) as ViewportName[];

const DEFAULT_SELECTOR_TIMEOUT_MS = 8000;
// The login form's heading + the SPA's mounted shell are the stable readiness signals.
const LOGIN_FORM = {
  selector: 'form[action="/login"]',
  timeoutMs: DEFAULT_SELECTOR_TIMEOUT_MS,
} as const;
const SPA_MOUNTED = {
  selector: "#root *",
  timeoutMs: DEFAULT_SELECTOR_TIMEOUT_MS,
} as const;

export const VISUAL_PATHS: VisualPath[] = [
  {
    group: "authed",
    path: "/dashboard",
    name: "dashboard",
    description: "Dashboard — overview and quota retained, Recent work collapsed",
    requireAuth: "admin",
    waitFor: {
      selector:
        '[data-testid="dashboard-page"]:has([data-testid="hq-primary-contact"]):has([data-testid="dash-quota"]):not(:has([data-testid="tile-loading"])):has([data-testid="needs-you-empty"], [data-testid="needs-you-row"], [data-testid="needs-you-error"])',
      timeoutMs: 12000,
    },
  },
  {
    group: "authed",
    path: "/projects/new",
    name: "new-project-playbooks",
    description: "New project — shared playbook gallery and explicit No playbook choice",
    requireAuth: "admin",
    prepare: "project-playbooks",
    waitFor: { selector: 'input[type="search"]', timeoutMs: 12000 },
  },
  {
    group: "authed",
    path: "/projects/new",
    name: "new-project-playbook-inputs",
    description: "New project — transient playbook inputs and saved step assignments",
    requireAuth: "admin",
    prepare: "project-playbook-inputs",
    waitFor: { selector: '#wizard-step-heading', timeoutMs: 12000 },
  },
  {
    group: "authed",
    path: "/library/variables",
    name: "variables",
    description: "Shared variables and collections — definitions and optional global values",
    requireAuth: "admin",
    waitFor: { selector: '[aria-label="Search variables"]', timeoutMs: 12000 },
  },
  {
    group: "authed",
    path: "/s/codex-api/5b0d2c1e-8f3a-4c7d-9e21-6a4b3c2d1e0f",
    name: "structured-approval",
    description: "API approval — prominent Approve always (deterministic browser fixture)",
    requireAuth: "admin",
    prepare: "api-approval",
    waitFor: { selector: '[data-testid="structured-always"]', timeoutMs: 12000 },
  },
  {
    group: "authed",
    path: "/s/codex-api/5b0d2c1e-8f3a-4c7d-9e21-6a4b3c2d1e0f",
    name: "structured-permissions",
    description: "Existing API conversation — Skip permissions confirmation (browser fixture)",
    requireAuth: "admin",
    prepare: "api-permissions",
    waitFor: { selector: '[role="dialog"]', timeoutMs: 12000 },
  },
  {
    group: "authed",
    path: "/s/codex-api/5b0d2c1e-8f3a-4c7d-9e21-6a4b3c2d1e0f",
    name: "api-guidance-codex",
    description: "Codex API — collapsed activity, distinct reply and queued Send now (synthetic activity)",
    requireAuth: "admin",
    fixture: "structured-guidance",
    waitFor: { selector: '[data-testid="structured-queued"] button', timeoutMs: 12000 },
  },
  {
    group: "authed",
    path: "/s/claude-api/5b0d2c1e-8f3a-4c7d-9e21-6a4b3c2d1e0f",
    name: "api-guidance-claude",
    description: "Claude API — collapsed activity, distinct reply and queued Send now (synthetic activity)",
    requireAuth: "admin",
    fixture: "structured-guidance",
    waitFor: { selector: '[data-testid="structured-queued"] button', timeoutMs: 12000 },
  },
  {
    group: "authed",
    path: "/s/opencode-api/5b0d2c1e-8f3a-4c7d-9e21-6a4b3c2d1e0f",
    name: "api-guidance-opencode",
    description: "Opencode API — collapsed activity, distinct reply and queued Send now (synthetic activity)",
    requireAuth: "admin",
    fixture: "structured-guidance",
    waitFor: { selector: '[data-testid="structured-queued"] button', timeoutMs: 12000 },
  },
  {
    group: "authed",
    path: "/settings/resources",
    name: "settings-resources",
    description: "Resources — next-launch API session memory allowance",
    requireAuth: "admin",
    prepare: "resource-controls",
    waitFor: { selector: "#resources-heading", timeoutMs: 12000 },
  },
  {
    group: "authed",
    path: "/settings/resources",
    name: "settings-resources-advanced",
    description:
      "Resources — advanced process/thread budgets and environment overrides",
    requireAuth: "admin",
    prepare: "resource-advanced",
    waitFor: { selector: "#resources-heading", timeoutMs: 12000 },
  },
  {
    group: "authed",
    path: "/library/playbooks/create/new",
    name: "playbook-new",
    description: "New local playbook — identity and accessible flow editor",
    requireAuth: "admin",
    waitFor: {
      selector:
        '[data-testid="playbook-editor"] input[data-field="identity.name"]',
      timeoutMs: 12000,
    },
  },
  {
    group: "authed",
    path: "/library/playbooks/forge-workflow/edit",
    name: "playbook-editor",
    description: "Playbook editor — flowchart and workflow properties",
    prepare: "playbook-flow",
    requireAuth: "admin",
    waitFor: {
      selector: '[aria-label="Workflow properties"]',
      timeoutMs: 12000,
    },
  },
  {
    group: "authed",
    path: "/library/playbooks/forge-workflow/edit",
    name: "playbook-step-properties",
    description:
      "Selected step — performer, instructions, completion rules and next steps",
    prepare: "playbook-step",
    requireAuth: "admin",
    waitFor: { selector: '[aria-label="Step properties"]', timeoutMs: 12000 },
  },
  {
    group: "authed",
    path: "/library/playbooks/forge-workflow/edit",
    name: "playbook-step-prompts",
    description: "Direct step prompts — unsaved manual instructions and source controls",
    prepare: "playbook-prompts",
    requireAuth: "admin",
    waitFor: { selector: '[aria-label="Prompt 1"]', timeoutMs: 12000 },
  },
  {
    group: "authed",
    path: "/library/playbooks",
    name: "playbooks",
    description: "Playbook gallery — flow previews, source and domain filters",
    requireAuth: "admin",
    waitFor: {
      selector: '[data-testid="playbook-card-forge-workflow"]',
      timeoutMs: 12000,
    },
  },
  // #1440: the public gallery's Browse views. The capture host has no signed listing (the trust
  // root is the installed release key, never a seeded one), so these show the state operators see
  // until a publication exists: the plain "can't be reached" / "nothing published" callout. The
  // populated entry, confirmation and update states are pinned by e2e/content-gallery.spec.ts.
  {
    group: "authed",
    path: "/library/gallery/playbooks",
    name: "gallery-playbooks",
    description:
      "Playbooks → Browse gallery — public playbooks, or the plain unreachable / nothing-published state",
    requireAuth: "admin",
    waitFor: {
      selector:
        '[data-testid="gallery-unreachable"], [data-testid="gallery-empty"], [data-testid="gallery-refused"], [data-testid="content-gallery"] article',
      timeoutMs: 45000,
    },
  },
  {
    group: "authed",
    path: "/library/gallery/skills",
    name: "gallery-skills",
    description:
      "Skills → Browse gallery — public skills, or the plain unreachable / nothing-published state",
    requireAuth: "admin",
    waitFor: {
      selector:
        '[data-testid="gallery-unreachable"], [data-testid="gallery-empty"], [data-testid="gallery-refused"], [data-testid="content-gallery"] article',
      timeoutMs: 45000,
    },
  },
  // The populated states, from a browser-side listing (`e2e/galleryFixture.ts`): no trust root is
  // overridden; the server never sees this listing.
  {
    group: "authed",
    path: "/library/gallery/playbooks",
    name: "gallery-playbooks-listed",
    description: "Playbooks → Browse gallery with a listing — cards, Verified, View",
    requireAuth: "admin",
    fixture: "gallery-listing",
    waitFor: { selector: '[data-testid="content-gallery"] article', timeoutMs: 20000 },
  },
  {
    group: "authed",
    path: `/library/gallery/playbooks/${"f".repeat(64)}`,
    name: "gallery-entry",
    description:
      "Gallery entry — requirements, what's inside and every file before Add, Technical details folded",
    requireAuth: "admin",
    fixture: "gallery-listing",
    waitFor: { selector: '[data-testid="gallery-add"]:not([disabled])', timeoutMs: 20000 },
  },
  {
    group: "authed",
    path: `/library/gallery/playbooks/${"f".repeat(64)}`,
    name: "gallery-confirm",
    description: "Gallery entry — the one confirmation: \"I've reviewed it — Add\"",
    requireAuth: "admin",
    fixture: "gallery-listing",
    prepare: "gallery-confirm",
    waitFor: { selector: "dialog[open]", timeoutMs: 20000 },
  },
  {
    group: "authed",
    path: "/library/playbooks/forge-workflow",
    name: "playbook-detail",
    description:
      "Playbook detail — flow actors, materials, variables and project deployments",
    requireAuth: "admin",
    waitFor: {
      selector: '[data-testid="playbooks-page"] h1',
      timeoutMs: 12000,
    },
  },
  {
    group: "public",
    path: "/login",
    name: "login",
    description: "Sign-in form (server-rendered)",
    requireAuth: false,
    waitFor: LOGIN_FORM,
  },
  {
    group: "public",
    path: "/connect.html",
    name: "connect",
    description:
      "Home Free public connect page — centered sign-in + floating connected controls",
    requireAuth: false,
    waitFor: {
      selector: ".connect-card",
      timeoutMs: DEFAULT_SELECTOR_TIMEOUT_MS,
    },
  },
  {
    group: "authed",
    path: "/",
    name: "app-home",
    description:
      "Start page: / redirects to the dashboard — Welcome, Commander. + the primary HQ contact (#1525)",
    requireAuth: "admin",
    waitFor: {
      selector: '[data-testid="dashboard-page"] [data-testid="hq-primary-contact"]',
      timeoutMs: DEFAULT_SELECTOR_TIMEOUT_MS,
    },
  },
  {
    group: "authed",
    path: "/new",
    name: "new-session",
    description:
      "New-session landing (/new since #1525) — session list sidebar + agent and project picker",
    requireAuth: "admin",
    waitFor: {
      selector: 'h1:has-text("Start a new session")',
      timeoutMs: DEFAULT_SELECTOR_TIMEOUT_MS,
    },
  },
  {
    group: "authed",
    path: "/settings",
    name: "settings",
    description:
      "Settings — lands on Appearance → Interface (mode cards + 16 accents) (#109/#211/#357/#1494)",
    requireAuth: "admin",
    waitFor: SPA_MOUNTED,
  },
  {
    group: "authed",
    // Explicit, not just `/settings`: on a phone the bare route is the section INDEX, so only
    // this entry captures the Interface page at the mobile formats (#1494).
    path: "/settings/appearance",
    name: "settings-appearance",
    description: "Settings — Appearance → Interface (mode cards, 16 accents, hex field) (#1494)",
    requireAuth: "admin",
    waitFor: SPA_MOUNTED,
  },
  {
    group: "authed",
    path: "/settings/terminal-appearance",
    name: "settings-terminal-appearance",
    description: "Settings — Appearance → Terminal sessions (font + text size) (#1494)",
    requireAuth: "admin",
    waitFor: SPA_MOUNTED,
  },
  {
    group: "authed",
    path: "/settings/maintenance",
    name: "settings-maintenance",
    description:
      "Maintenance — cache pruning, mission archival and observed-idle OpenCode compaction",
    requireAuth: "admin",
    waitFor: { selector: '[aria-label="OpenCode database"]', timeoutMs: 8000 },
  },
  {
    group: "authed",
    path: "/settings/agents/apichat",
    name: "settings-agent-tools",
    description:
      "API agent endpoint — read and propose edits opt-in with per-file consent",
    requireAuth: "admin",
    waitFor: { selector: 'input[value="write"]', timeoutMs: 8000 },
  },
  {
    group: "authed",
    path: "/s/apichat/019e2ba1-1590-7003-8e4a-51ab62cec905",
    name: "chat-edit-approval",
    description:
      "API agent — pending file change, complete diff and separate approval controls",
    requireAuth: "admin",
    waitFor: { selector: '[data-testid="chat-proposal"]', timeoutMs: 12000 },
  },
  {
    group: "authed",
    path: "/overview",
    name: "overview",
    description:
      "Session Overview map — clustered projects/sessions (#139/#211 HUD)",
    requireAuth: "admin",
    waitFor: {
      kind: "networkidle",
      timeoutMs: 8000,
      reason: "React Flow lays out nodes after the sessions fetch resolves",
    },
  },
  {
    group: "authed",
    path: "/overview",
    name: "overview-window",
    description:
      "Session Overview map with an open window — the ONE consolidated, project-tinted chrome bar carrying the facts run, the chips and the single ⋯ (#1109)",
    requireAuth: "admin",
    // The map is the readiness gate; the prepare step below then opens the window and waits
    // for ITS terminal — a window-scoped waitFor would time out on the narrow formats, where
    // the map (correctly) refuses to host one and the shot keeps the plain map.
    waitFor: { selector: ".tr-overview .tr-ov-chip", timeoutMs: 15000 },
    prepare: "open-first-window",
  },
  {
    group: "authed",
    path: "/mission",
    name: "pulse",
    description:
      "Missions — searchable sidebar, conversation and collapsible mission details (#944)",
    requireAuth: "admin",
    // Readiness belongs to the mission workspace, including the empty-store case.
    waitFor: { selector: '[data-testid="mission-console"]', timeoutMs: 8000 },
  },
  {
    group: "authed",
    // The deterministic seeded Claude session (web/visual/seed.py _CLAUDE[0]); resumed
    // against the fake-agent transcript so the terminal pane renders representative output.
    path: "/s/claude/019e2ba1-1590-7003-8e4a-51ab62cec900",
    name: "session-view",
    description:
      "Open session — terminal chrome (header + scrollback + compose bar) (#211 HUD)",
    requireAuth: "admin",
    // Wait for the xterm canvas to mount + the fake-agent transcript to paint.
    waitFor: { selector: ".xterm-screen", timeoutMs: 12000 },
  },
];

export const KNOWN_AREA_KEYS: ReadonlySet<string> = new Set(
  VISUAL_PATHS.map((p) => p.name),
);
export const SEEDED_AREA_KEYS: ReadonlySet<string> = new Set(
  VISUAL_PATHS.filter((p) => p.seeded).map((p) => p.name),
);
