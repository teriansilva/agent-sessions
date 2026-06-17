// Visual-capture registry (#96) — the universe of screens the Playwright capture
// screenshots at every screen format. Mirrors demoapp.io/tests/visual/paths.ts,
// adapted to agent-sessions (single-admin form login, no OIDC/seed-from-Docker).
//
// `web/visual/paths.test.ts` enforces: name uniqueness, non-empty description, every
// entry has a `waitFor`, and every `networkidle` wait carries a non-empty `reason`.

export type WaitForSelector = { selector: string; timeoutMs?: number };
export type WaitForTimeout = { timeoutMs: number };
export type WaitForNetworkIdle = { kind: "networkidle"; timeoutMs?: number; reason: string };
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
const LOGIN_FORM = { selector: 'form[action="/login"]', timeoutMs: DEFAULT_SELECTOR_TIMEOUT_MS } as const;
const SPA_MOUNTED = { selector: "#root *", timeoutMs: DEFAULT_SELECTOR_TIMEOUT_MS } as const;

export const VISUAL_PATHS: VisualPath[] = [
  {
    group: "public",
    path: "/login",
    name: "login",
    description: "Sign-in form (server-rendered)",
    requireAuth: false,
    waitFor: LOGIN_FORM,
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
    description: "Settings — tabbed shell, Appearance tab (theme + accent + compose) (#109/#211/#357)",
    requireAuth: "admin",
    waitFor: SPA_MOUNTED,
  },
  {
    group: "authed",
    path: "/overview",
    name: "overview",
    description: "Session Overview map — clustered projects/sessions (#139/#211 HUD)",
    requireAuth: "admin",
    waitFor: {
      kind: "networkidle",
      timeoutMs: 8000,
      reason: "React Flow lays out nodes after the sessions fetch resolves",
    },
  },
  {
    group: "authed",
    path: "/pulse",
    name: "pulse",
    description:
      "Pulse — AI-curated recent-work overview: banner + sessions grouped by state with Jump in (#441 HUD)",
    requireAuth: "admin",
    // The seeded pulse-cache.json (web/visual/seed.py) makes this populated; wait for the
    // first card's Jump-in link (a stable, non-hashed aria-label selector) to paint.
    waitFor: { selector: 'a[aria-label^="Jump into"]', timeoutMs: 8000 },
  },
  {
    group: "authed",
    // The deterministic seeded Claude session (web/visual/seed.py _CLAUDE[0]); resumed
    // against the fake-agent transcript so the terminal pane renders representative output.
    path: "/s/claude/019e2ba1-1590-7003-8e4a-51ab62cec900",
    name: "session-view",
    description: "Open session — terminal chrome (header + scrollback + compose bar) (#211 HUD)",
    requireAuth: "admin",
    // Wait for the xterm canvas to mount + the fake-agent transcript to paint.
    waitFor: { selector: ".xterm-screen", timeoutMs: 12000 },
  },
];

export const KNOWN_AREA_KEYS: ReadonlySet<string> = new Set(VISUAL_PATHS.map((p) => p.name));
export const SEEDED_AREA_KEYS: ReadonlySet<string> = new Set(
  VISUAL_PATHS.filter((p) => p.seeded).map((p) => p.name),
);
