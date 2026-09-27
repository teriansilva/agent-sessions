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
  Crosshair,
  LayoutDashboard,
  Network,
  TerminalSquare,
  type LucideIcon,
} from "lucide-react";

import {
  ASK_PATH,
  DASHBOARD_PATH,
  LEGACY_MISSION_PATH,
  MAP_PATH,
  MISSION_PATH,
  SESSIONS_PATH,
  TEMPLATES_PATH,
} from "../../lib/routes";

export type SectionId = "ask" | "sessions" | "mission" | "templates";

/** An entry inside a section's sub-menu. Only Sessions has one today: the session view and its map. */
export type SubsectionId = "sessions" | "map";

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
  // The dashboard (#1123) — Ask lives on it, so the section keeps its id and its first place.
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
  { id: "templates", label: "Templates", Icon: BookMarked, to: TEMPLATES_PATH },
];

/** Which section a pathname belongs to. `null` for Settings and anything else that is not a work
 *  section — the nav then highlights nothing, which is the honest answer.
 *
 *  Matching is EXACT except where a section genuinely owns a subtree (`/s/…` is a session,
 *  `/templates/…` is a template). A blanket `startsWith` would claim `/askew` for Ask and paint
 *  "you are here" on the wrong entry the first time someone adds a route sharing a prefix. */
export function activeSection(pathname: string): SectionId | null {
  const sub = activeSubsection(pathname);
  if (sub) return "sessions";
  if (pathname === MISSION_PATH || pathname === LEGACY_MISSION_PATH)
    return "mission";
  if (pathname === DASHBOARD_PATH || pathname === ASK_PATH) return "ask";
  if (pathname === TEMPLATES_PATH || pathname.startsWith(`${TEMPLATES_PATH}/`))
    return "templates";
  return null;
}

/** Which sub-menu entry a pathname belongs to — the map, or the session view — `null` elsewhere.
 *  Same exact-match rule as `activeSection`, which derives the Sessions parent from this. */
export function activeSubsection(pathname: string): SubsectionId | null {
  if (pathname === SESSIONS_PATH || pathname.startsWith("/s/"))
    return "sessions";
  if (pathname === MAP_PATH) return "map";
  return null;
}
