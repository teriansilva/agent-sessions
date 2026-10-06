/** The work sections, as DATA (#1058).
 *
 *  Its own module so `SectionNav.tsx` exports a component and nothing else — the fast-refresh rule
 *  the repo lints for, and a real constraint rather than a style one: a file that mixes a component
 *  with the constants it renders loses hot-reload for the whole file.
 *
 *  Before this, two sections were `<Link>`s in `<nav aria-label="Main sections">` and two were
 *  unlabelled `.gear` icons in the action cluster beside Help and the bell — which is to say the
 *  app's own chrome did not agree on what a destination was. All of them live here now, and the top
 *  bar and the drawer render from the same array, so the next section is one entry rather than
 *  three edits that can disagree.
 *
 *  **The map is a view OF sessions (#1069)**: it lives in `children` of Sessions, which the bar
 *  paints as a sub-menu behind a chevron. The Sessions label itself still goes to the last session
 *  in one click — a sub-menu must not tax the common case to host the rare one. Ask is not here at
 *  all (#1294): it is the sidebar the corner icon beside the bell opens, on every route.
 *
 *  Settings is deliberately absent. It is a route, but it is not a work section — it is a utility
 *  surface, and it keeps its place in the corner cluster (and in the operator menu, which is what
 *  keeps it one tap away on a phone once the cluster collapses into the drawer).
 */
import {
  BookMarked,
  CalendarClock,
  Crosshair,
  LayoutDashboard,
  Library,
  ListChecks,
  TerminalSquare,
  Network,
  Workflow,
  type LucideIcon,
} from "lucide-react";

import {
  AUTOMATIONS_PATH,
  CHECKLISTS_PATH,
  DASHBOARD_PATH,
  LEGACY_MISSION_PATH,
  MAP_PATH,
  MISSION_PATH,
  PLAYBOOKS_PATH,
  SESSIONS_PATH,
  TEMPLATES_PATH,
} from "../../lib/routes";

export type SectionId = "ask" | "sessions" | "mission" | "library";

/** An entry inside a section's sub-menu: Sessions has the session view and its map; Library has
 *  the templates, automations, checklists and playbooks a session or mission is started from
 *  (#1294). */
export type SubsectionId =
  | "sessions"
  | "map"
  | "templates"
  | "automations"
  | "checklists"
  | "playbooks";

export interface Subsection {
  id: SubsectionId;
  label: string;
  Icon: LucideIcon;
  /** `null` for the session view, whose target is whichever session path was last on screen. */
  to: string | null;
}

export interface Section {
  id: SectionId;
  label: string;
  Icon: LucideIcon;
  /** `null` for Sessions, whose target is whichever session path was last on screen. */
  to: string | null;
  /** The sub-menu. The FIRST child is the section's own destination, so the menu also names it. */
  children?: Subsection[];
  /** Still settling (#1085): the entry carries a small BETA tag. */
  beta?: boolean;
}

export const SECTIONS: Section[] = [
  // The dashboard (#1123). The id stays `ask` so nothing keyed on it churns; Ask itself is no
  // longer a destination but the right-hand sidebar the corner icon opens (#1294).
  { id: "ask", label: "Dashboard", Icon: LayoutDashboard, to: DASHBOARD_PATH },
  {
    id: "sessions",
    label: "Sessions",
    Icon: TerminalSquare,
    to: null,
    children: [
      { id: "sessions", label: "Sessions", Icon: TerminalSquare, to: null },
      { id: "map", label: "Sessions map", Icon: Network, to: MAP_PATH },
    ],
  },
  { id: "mission", label: "Missions", Icon: Crosshair, to: MISSION_PATH, beta: true },
  // LIBRARY (#1294): what work is started FROM — templates, automations, the checklists that say
  // what "done" means, and playbooks (#1096). Checklists and automations keep their
  // `/mission/...` URLs: the server links automation-failure notifications to `automationPath()`.
  {
    id: "library",
    label: "Library",
    Icon: Library,
    to: TEMPLATES_PATH,
    children: [
      { id: "templates", label: "Templates", Icon: BookMarked, to: TEMPLATES_PATH },
      { id: "automations", label: "Automations", Icon: CalendarClock, to: AUTOMATIONS_PATH },
      { id: "checklists", label: "Checklists", Icon: ListChecks, to: CHECKLISTS_PATH },
      { id: "playbooks", label: "Playbooks", Icon: Workflow, to: PLAYBOOKS_PATH },
    ],
  },
];

/** Which section a pathname belongs to. `null` for Settings and anything else that is not a work
 *  section — the nav then highlights nothing, which is the honest answer.
 *
 *  Matching is EXACT except where a section genuinely owns a subtree (`/s/…` is a session,
 *  `/templates/…` is a template, `/mission/automations/…` is an automation). A blanket
 *  `startsWith` would claim `/dashboardx` for Dashboard and paint "you are here" on the wrong
 *  entry the first time someone adds a route sharing a prefix. */
export function activeSection(pathname: string): SectionId | null {
  const sub = activeSubsection(pathname);
  if (sub === "sessions" || sub === "map") return "sessions";
  if (sub !== null) return "library";
  if (pathname === DASHBOARD_PATH) return "ask";
  if (pathname === MISSION_PATH || pathname === LEGACY_MISSION_PATH)
    return "mission";
  return null;
}

/** Which sub-menu entry a pathname belongs to — `null` elsewhere.
 *  Same exact-match rule as `activeSection`, which derives the parents from this. */
export function activeSubsection(pathname: string): SubsectionId | null {
  if (pathname === SESSIONS_PATH || pathname.startsWith("/s/"))
    return "sessions";
  if (pathname === MAP_PATH) return "map";
  if (pathname === TEMPLATES_PATH || pathname.startsWith(`${TEMPLATES_PATH}/`))
    return "templates";
  // Automations OWNS its subtree: one automation's run history and editor live under it.
  if (pathname === AUTOMATIONS_PATH || pathname.startsWith(`${AUTOMATIONS_PATH}/`))
    return "automations";
  if (pathname === CHECKLISTS_PATH) return "checklists";
  if (pathname === PLAYBOOKS_PATH) return "playbooks";
  return null;
}
