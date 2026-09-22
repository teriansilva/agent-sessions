/** The five work sections, as DATA (#1058).
 *
 *  Its own module so `SectionNav.tsx` exports a component and nothing else — the fast-refresh rule
 *  the repo lints for, and a real constraint rather than a style one: a file that mixes a component
 *  with the constants it renders loses hot-reload for the whole file.
 *
 *  Before this, two sections were `<Link>`s in `<nav aria-label="Main sections">` and two were
 *  unlabelled `.gear` icons in the action cluster beside Help and the bell — which is to say the
 *  app's own chrome did not agree on what a destination was. All five live here now, and the top
 *  bar and the drawer render from the same array, so the next section is one entry rather than
 *  three edits that can disagree.
 *
 *  Settings is deliberately absent. It is a route, but it is not a work section — it is a utility
 *  surface, and it keeps its place in the corner cluster (and in the operator menu, which is what
 *  keeps it one tap away on a phone once the cluster collapses into the drawer).
 */
import {
  BookMarked,
  Crosshair,
  Network,
  Sparkles,
  TerminalSquare,
  type LucideIcon,
} from "lucide-react";

import {
  ASK_PATH,
  LEGACY_MISSION_PATH,
  MAP_PATH,
  MISSION_PATH,
  SESSIONS_PATH,
  TEMPLATES_PATH,
} from "../../lib/routes";

export type SectionId = "sessions" | "mission" | "ask" | "map" | "templates";

export interface Section {
  id: SectionId;
  label: string;
  Icon: LucideIcon;
  /** `null` for Sessions, whose target is whichever session path was last on screen. */
  to: string | null;
}

export const SECTIONS: Section[] = [
  { id: "sessions", label: "Sessions", Icon: TerminalSquare, to: null },
  { id: "mission", label: "Missions", Icon: Crosshair, to: MISSION_PATH },
  { id: "ask", label: "Ask", Icon: Sparkles, to: ASK_PATH },
  { id: "map", label: "Map", Icon: Network, to: MAP_PATH },
  { id: "templates", label: "Templates", Icon: BookMarked, to: TEMPLATES_PATH },
];

/** Which section a pathname belongs to. `null` for Settings and anything else that is not a work
 *  section — the nav then highlights nothing, which is the honest answer.
 *
 *  Matching is EXACT except where a section genuinely owns a subtree (`/s/…` is a session,
 *  `/templates/…` is a template). A blanket `startsWith` would claim `/askew` for Ask and paint
 *  "you are here" on the wrong entry the first time someone adds a route sharing a prefix. */
export function activeSection(pathname: string): SectionId | null {
  if (pathname === SESSIONS_PATH || pathname.startsWith("/s/"))
    return "sessions";
  if (pathname === MISSION_PATH || pathname === LEGACY_MISSION_PATH)
    return "mission";
  if (pathname === ASK_PATH) return "ask";
  if (pathname === MAP_PATH) return "map";
  if (pathname === TEMPLATES_PATH || pathname.startsWith(`${TEMPLATES_PATH}/`))
    return "templates";
  return null;
}
