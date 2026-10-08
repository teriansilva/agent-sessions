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
  /** An interaction to run after `goto`, before `waitFor` (#1109): the consolidated window
   *  chrome only exists on an OPEN window, and a capture that only visits a route cannot show
   *  it. The one shipped step opens the first session chip as a window. */
  prepare?:
    | "open-first-window"
    | "resource-controls"
    | "resource-usage"
    | "playbook-flow";
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
    path: "/settings/system",
    name: "settings-resources",
    description:
      "Settings — finite next-launch resource budgets and environment overrides",
    requireAuth: "admin",
    prepare: "resource-controls",
    waitFor: { selector: "#resources-heading", timeoutMs: 12000 },
  },
  {
    group: "authed",
    path: "/settings/system",
    name: "settings-resource-usage",
    description:
      "Settings — observed running task budgets and inherited pressure",
    requireAuth: "admin",
    prepare: "resource-usage",
    waitFor: { selector: "#resource-usage-heading", timeoutMs: 12000 },
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
    description: "Playbook editor — canvas/list flow and shared step inspector",
    prepare: "playbook-flow",
    requireAuth: "admin",
    waitFor: { selector: '[aria-label="Step inspector"]', timeoutMs: 12000 },
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
    description: "Session list sidebar + new-session panel (all engine badges)",
    requireAuth: "admin",
    waitFor: SPA_MOUNTED,
  },
  {
    group: "authed",
    path: "/?new=1",
    name: "new-session",
    description: "New-session landing — agent + project picker",
    requireAuth: "admin",
    waitFor: SPA_MOUNTED,
  },
  {
    group: "authed",
    path: "/settings",
    name: "settings",
    description:
      "Settings — tabbed shell, Appearance tab (theme + accent + compose) (#109/#211/#357)",
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
