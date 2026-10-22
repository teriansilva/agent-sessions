// The NIGHTJAR screenshot sweep's manifest (#1531): every capture id a ledger row may name
// (docs/nightjar-ledger.md "Captures"), and how to put the app into that state. The ledger test
// fails when a `done` row names an id this list does not have. Phases add ids with their rows;
// Phase 14 runs the whole list across every width, theme and pixel ratio.
//
//   cd web && npm run build && npx vite preview --port 41918 --strictPort &
//   node visual/nightjar/sweep.mjs --base http://127.0.0.1:41918 --out ../out/nightjar [--only id,id] [--full]

/** @typedef {({ testId: string } | { role: string, name: string, partial?: boolean } | { css: string })
 *   & { upload?: string }} SweepStep
 *  A step: a test id, a role + accessible name (`partial`: substring match), or a CSS selector.
 *  `upload`: instead of clicking, put a small text file of that name into the (file input) target. */

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
 *  `term`: the terminal socket's state (visual/promo/terminal.mjs: silent, closeCode, role, holder).
 *  `api`: per-capture answers, `path → body` (a number answers that HTTP status), for a state the
 *  shared fixtures do not hold. `mock`: opt-in states of the shared mock (visual/promo: e.g.
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

/** @type {SweepCapture[]} */
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
  { id: "popup:folder-picker", path: "/new", open: [{ role: "button", name: "Choose folder…" }], expect: "dialog" },
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
    // A phone keeps them behind the details tab; the dialog there is the same sheet as every other.
    widths: ["desktop"],
    height: 1400,
    // The SECOND objective: the first is met, and a met objective's ⋯ offers no direction.
    open: [{ css: "[data-testid='objective-menu'] >> nth=1" }, { testId: "objective-edit-direction" }],
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
  // Routes (Phases 4–11). The skeleton: the route itself in its default state; each phase adds its
  // states (loading, empty, error, opened popups, focus) as ids of its own.
  { id: "route:dashboard", path: "/dashboard" },
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
  { id: "route:mission", path: "/mission" },
  { id: "route:checklists", path: "/mission/checklists" },
  { id: "route:automations", path: "/mission/automations" },
  { id: "route:map", path: "/overview" },
  { id: "route:templates", path: "/templates" },
  { id: "route:playbooks", path: "/library/playbooks" },
  { id: "route:skills", path: "/library/skills" },
  { id: "route:memory", path: "/library/memory" },
  { id: "route:variables", path: "/library/variables" },
  { id: "route:new-project", path: "/projects/new" },
  { id: "route:settings-interface", path: "/settings/appearance" },
  { id: "route:settings-agents", path: "/settings/agents" },
  { id: "route:settings-security", path: "/settings/security" },
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
];

export const WIDTHS = [1440, 1100, 390];
export const THEMES = ["dark", "light"];
export const PIXEL_RATIOS = [1, 2];
