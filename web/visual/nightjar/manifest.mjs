// The NIGHTJAR screenshot sweep's manifest (#1531): every capture id a ledger row may name
// (docs/nightjar-ledger.md "Captures"), and how to put the app into that state. The ledger test
// fails when a `done` row names an id this list does not have. Phases add ids with their rows;
// Phase 14 runs the whole list across every width, theme and pixel ratio.
//
//   cd web && npm run build && npx vite preview --port 41918 --strictPort &
//   node visual/nightjar/sweep.mjs --base http://127.0.0.1:41918 --out ../out/nightjar [--only id,id] [--full]

/** @typedef {{ id: string, path: string, ready?: string, section?: string, fullPage?: boolean,
 *   action?: "stream" | "arrive" | "power-on", motion?: "full" | "reduced" | "off" }} SweepCapture
 *  `section`: a test id, or a panel's visible title, to capture alone (it must exist and have a
 *  size, or the sweep fails). `action`: an interaction the sweep performs before capturing.
 *  `motion`: the mode for this capture (default: the sweep's `--motion`, Off). */

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
