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
 *  **Ask leads, and the map is a view OF sessions (#1069).** The operator asked for Ask first and
 *  for the map to stop being a destination of its own: it lives in `children` of Sessions, which
 *  the bar paints as a sub-menu behind a chevron and the drawer as an indented entry. The Sessions
 *  label itself still goes to the last session in one click — a sub-menu must not tax the common
 *  case to host the rare one.
 *
 *  Settings is deliberately absent. It is a route, but it is not a work section — it is a utility
 *  surface, and it keeps its place in the corner cluster (and in the operator menu, which is what
 *  keeps it one tap away on a phone once the cluster collapses into the drawer).
 */
import {
  BookMarked,
  CalendarClock,
  Crosshair,
  ListChecks,
  LayoutDashboard,
  MessageSquare,
  Network,
  TerminalSquare,
  type LucideIcon,
} from "lucide-react";

import {
  ASK_PATH,
  AUTOMATIONS_PATH,
  CHECKLISTS_PATH,
  DASHBOARD_PATH,
  LEGACY_MISSION_PATH,
  MAP_PATH,
  MISSION_PATH,
  SESSIONS_PATH,
  TEMPLATES_PATH,
} from "../../lib/routes";

export type SectionId = "ask" | "sessions" | "mission" | "templates";

/** An entry inside a section's sub-menu: Sessions has the session view and its map; Dashboard
 *  has the dashboard and Ask's conversation page (#1171); Missions has the console, its
 *  checklists and its automations (#1201). */
export type SubsectionId =
  | "sessions"
  | "map"
  | "dashboard"
  | "ask"
  | "mission"
  | "checklists"
  | "automations";

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
  // The dashboard (#1123) — Ask starts on it, so the section keeps its id and its first place.
  // A conversation has its own page under it (#1171), the way the map sits under Sessions.
  {
    id: "ask",
    label: "Dashboard",
    Icon: LayoutDashboard,
    to: DASHBOARD_PATH,
    children: [
      { id: "dashboard", label: "Dashboard", Icon: LayoutDashboard, to: DASHBOARD_PATH },
      { id: "ask", label: "Ask", Icon: MessageSquare, to: ASK_PATH },
    ],
  },
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
  // The checklists a mission starts with live under Missions, beside the console that uses them —
  // they were a Settings tab, which is not where anyone looks for what "done" means for a mission.
  {
    id: "mission",
    label: "Missions",
    Icon: Crosshair,
    to: MISSION_PATH,
    beta: true,
    children: [
      { id: "mission", label: "Missions", Icon: Crosshair, to: MISSION_PATH },
      { id: "checklists", label: "Checklists", Icon: ListChecks, to: CHECKLISTS_PATH },
      // Work that runs while the operator is away (#1201): everything it starts is a mission or a
      // session, so it lives beside the console rather than as a section of its own.
      { id: "automations", label: "Automations", Icon: CalendarClock, to: AUTOMATIONS_PATH },
    ],
  },
  { id: "templates", label: "Templates", Icon: BookMarked, to: TEMPLATES_PATH },
];

/** Which section a pathname belongs to. `null` for Settings and anything else that is not a work
 *  section — the nav then highlights nothing, which is the honest answer.
 *
 *  Matching is EXACT except where a section genuinely owns a subtree (`/s/…` is a session,
 *  `/templates/…` is a template, `/mission/automations/…` is an automation). A blanket
 *  `startsWith` would claim `/askew` for Ask and paint "you are here" on the wrong entry the first
 *  time someone adds a route sharing a prefix. */
export function activeSection(pathname: string): SectionId | null {
  const sub = activeSubsection(pathname);
  if (sub === "sessions" || sub === "map") return "sessions";
  if (sub === "dashboard" || sub === "ask") return "ask";
  if (sub === "mission" || sub === "checklists" || sub === "automations") return "mission";
  if (pathname === TEMPLATES_PATH || pathname.startsWith(`${TEMPLATES_PATH}/`))
    return "templates";
  return null;
}

/** Which sub-menu entry a pathname belongs to — the session view, the map, the dashboard or Ask —
 *  `null` elsewhere.
 *  Same exact-match rule as `activeSection`, which derives the Sessions parent from this. */
export function activeSubsection(pathname: string): SubsectionId | null {
  if (pathname === SESSIONS_PATH || pathname.startsWith("/s/"))
    return "sessions";
  if (pathname === MAP_PATH) return "map";
  if (pathname === DASHBOARD_PATH) return "dashboard";
  if (pathname === ASK_PATH) return "ask";
  if (pathname === MISSION_PATH || pathname === LEGACY_MISSION_PATH)
    return "mission";
  if (pathname === CHECKLISTS_PATH) return "checklists";
  // Automations OWNS its subtree: one automation's run history and editor live under it.
  if (pathname === AUTOMATIONS_PATH || pathname.startsWith(`${AUTOMATIONS_PATH}/`))
    return "automations";
  return null;
}
