// The NIGHTJAR screenshot sweep's manifest (#1531): every capture id a ledger row may name
// (docs/nightjar-ledger.md "Captures"), and how to put the app into that state. The ledger test
// fails when a `done` row names an id this list does not have. Phases add ids with their rows;
// Phase 14 runs the whole list across every width, theme and pixel ratio.
//
//   cd web && npm run build && npx vite preview --port 41918 --strictPort &
//   node visual/nightjar/sweep.mjs --base http://127.0.0.1:41918 --out ../out/nightjar [--only id,id] [--full]

/** @typedef {{ id: string, path: string, ready?: string, section?: string, fullPage?: boolean,
 *   action?: "stream" | "arrive" | "power-on", motion?: "full" | "reduced" | "off",
 *   open?: ({ testId: string } | { role: string, name: string })[], expect?: string,
 *   phoneFirst?: ({ testId: string } | { role: string, name: string })[], asPerson?: boolean,
 *   at?: number, expectCss?: string, present?: string }} SweepCapture
 *  `expectCss`: like `expect`, a selector that must be visible once the popup is open — for a
 *  panel whose role changes with the width (HQ: complementary on desktop, a dialog on a phone).
 *  `asPerson`: clear `navigator.webdriver` (the boot sequence stands down under automation);
 *  `at`: capture this many ms after load instead of waiting for the page to settle.
 *  `present`: a selector that must be visible at the moment of the shot, or the capture fails.
 *  `section`: a test id, or a panel's visible title, to capture alone (it must exist and have a
 *  size, or the sweep fails). `action`: an interaction the sweep performs before capturing.
 *  `motion`: the mode for this capture (default: the sweep's `--motion`, Off).
 *  `open`: controls to click, in order, to open a popup; `expect`: the ARIA role that must then be
 *  visible (`dialog`, `menu`, `alertdialog`…), or the sweep fails. Every width runs, so one id
 *  covers the desktop popup AND its ≤800px bottom sheet. */

/** On a phone the session rows live in the off-canvas drawer: open it first. */
const DRAWER = [{ role: "button", name: "Open session list" }];

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
  // The boot sequence (Phase 4), mid-boot: Full, as a person, 450 ms after load.
  { id: "route:boot", path: "/dashboard", motion: "full", asPerson: true, at: 450, present: "[data-testid='boot-sequence']" },
  // Routes (Phases 4–11). The skeleton: the route itself in its default state; each phase adds its
  // states (loading, empty, error, opened popups, focus) as ids of its own.
  { id: "route:dashboard", path: "/dashboard" },
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
];

export const WIDTHS = [1440, 1100, 390];
export const THEMES = ["dark", "light"];
export const PIXEL_RATIOS = [1, 2];
