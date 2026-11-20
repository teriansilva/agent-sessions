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
 *  Dashboard leads, followed by HQ, Sessions and Library (#2037). Missions is a view of HQ.
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
  Bell,
  BookMarked,
  Brain,
  Braces,
  CalendarClock,
  Crosshair,
  LayoutDashboard,
  Library,
  Package,
  Plug,
  TerminalSquare,
  Network,
  Workflow,
  FileText,
  UsersRound,
  Route,
  Webhook,
  type LucideIcon,
} from "lucide-react";

import { MISSION_ID_RE } from "../../lib/missionLink";
import {
  ARTIFACTS_PATH,
  AUTOMATIONS_PATH,
  DASHBOARD_PATH,
  LEGACY_MISSION_PATH,
  MAP_PATH,
  MCP_PATH,
  MEMORY_PATH,
  MISSION_PATH,
  MISSION_PLANS_PATH,
  PLAYBOOKS_PATH,
  GALLERY_PATH,
  GALLERY_SKILLS_PATH,
  HQ_PATH,
  HQ_DISPATCHES_PATH,
  SKILLS_PATH,
  SPECIALISTS_PATH,
  SESSIONS_PATH,
  TEMPLATES_PATH,
  VARIABLES_PATH,
  WEBHOOKS_PATH,
} from "../../lib/routes";

export type SectionId = "ask" | "sessions" | "mission" | "library";

/** An entry inside a section's sub-menu: Sessions has the session view and its map; Library has
 *  templates, variables, memory, artifacts, automations, webhooks, skills and playbooks (#1294, #1932,
 *  #2221). Skills and
 *  Playbooks each hold their own Browse gallery view (#1440). */
export type SubsectionId =
  | "hq"
  | "missions"
  | "dispatches"
  | "sessions"
  | "map"
  | "templates"
  | "variables"
  | "mcp"
  | "memory"
  | "artifacts"
  | "automations"
  | "webhooks"
  | "playbooks"
  | "skills"
  | "specialists"
  | "mission-plans";

export interface Subsection {
  id: SubsectionId;
  label: string;
  Icon: LucideIcon;
  /** `null` for the session view, whose target is whichever session path was last on screen. */
  to: string | null;
  /** A live count the sub-menu draws beside the label (#2146): `notifications` is the bell's
   *  unread count, from the same store the badge reads. */
  count?: "notifications";
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

/** HQ's views (#2037), switched from the top bar's HQ sub-menu (a bottom sheet on a phone) — the
 *  only switcher since the in-page strip went (#2146). `dispatches` keeps its id (a wire key shared
 *  with the server's screen directives); the view it names is Notifications, at the same URL. */
export const HQ_VIEWS: Subsection[] = [
  { id: "hq", label: "Conversations", Icon: Crosshair, to: HQ_PATH },
  { id: "missions", label: "Missions", Icon: Crosshair, to: MISSION_PATH },
  { id: "dispatches", label: "Notifications", Icon: Bell, to: HQ_DISPATCHES_PATH, count: "notifications" },
];

export const SECTIONS: Section[] = [
  // The dashboard (#1123). The id stays `ask` so nothing keyed on it churns; Ask itself is no
  // longer a destination but the right-hand sidebar the corner icon opens (#1294).
  { id: "ask", label: "Dashboard", Icon: LayoutDashboard, to: DASHBOARD_PATH },
  // Keep the historical section id for retained shell state. HQ groups its existing views.
  {
    id: "mission", label: "HQ", Icon: Crosshair, to: HQ_PATH,
    children: HQ_VIEWS,
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
  // LIBRARY (#1294): what work is started FROM — templates, automations and playbooks (#1096).
  // Automations keep their `/mission/...` URL: the server links automation-failure notifications
  // to `automationPath()`. (Checklists were here until they were removed, #1653.)
  {
    id: "library",
    label: "Library",
    Icon: Library,
    to: TEMPLATES_PATH,
    children: [
      { id: "templates", label: "Prompt Templates", Icon: BookMarked, to: TEMPLATES_PATH },
      { id: "variables", label: "Variables", Icon: Braces, to: VARIABLES_PATH },
      // #2163: third-party MCP servers; their credentials live in Variables, just above.
      { id: "mcp", label: "MCP servers", Icon: Plug, to: MCP_PATH },
      { id: "memory", label: "Memory", Icon: Brain, to: MEMORY_PATH },
      // What agents PUBLISHED (#1932): files and static sites at a stable link.
      { id: "artifacts", label: "Artifacts", Icon: Package, to: ARTIFACTS_PATH },
      { id: "automations", label: "Automations", Icon: CalendarClock, to: AUTOMATIONS_PATH },
      // #2221: the signed addresses services deliver to, which automations pick as a trigger.
      { id: "webhooks", label: "Webhooks", Icon: Webhook, to: WEBHOOKS_PATH },
      { id: "skills", label: "Skills", Icon: FileText, to: SKILLS_PATH },
      { id: "specialists", label: "Specialists", Icon: UsersRound, to: SPECIALISTS_PATH },
      { id: "playbooks", label: "Playbooks", Icon: Workflow, to: PLAYBOOKS_PATH },
      // #1924 (#1613 P7a): reusable, slot-only, inert mission plans.
      { id: "mission-plans", label: "Mission plans", Icon: Route, to: MISSION_PLANS_PATH },
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
  if (sub === "hq" || sub === "missions" || sub === "dispatches") return "mission";
  if (sub !== null) return "library";
  // `/` redirects to the dashboard since #1525: it lights Dashboard for the render before it lands.
  if (pathname === DASHBOARD_PATH || pathname === "/") return "ask";
  return null;
}

/** Which sub-menu entry a pathname belongs to — `null` elsewhere.
 *  Same exact-match rule as `activeSection`, which derives the parents from this. */
export function activeSubsection(pathname: string): SubsectionId | null {
  // `/remote/…` (#2055) is a session another instance shares: it sits in the Sessions section,
  // beside the list its row is in.
  if (pathname === SESSIONS_PATH || pathname.startsWith("/s/") || pathname.startsWith("/remote/"))
    return "sessions";
  if (pathname === MAP_PATH) return "map";
  if (pathname === TEMPLATES_PATH || pathname.startsWith(`${TEMPLATES_PATH}/`))
    return "templates";
  // Automations OWNS its subtree: one automation's run history and editor live under it.
  if (pathname === AUTOMATIONS_PATH || pathname.startsWith(`${AUTOMATIONS_PATH}/`))
    return "automations";
  if (pathname === VARIABLES_PATH) return "variables";
  // Webhooks OWNS its subtree: a new one is `/library/webhooks/new`, one webhook `/library/webhooks/<id>`.
  if (pathname === WEBHOOKS_PATH || pathname.startsWith(`${WEBHOOKS_PATH}/`)) return "webhooks";
  // MCP servers OWNS its subtree: the wizard is `/library/mcp/new`, a server `/library/mcp/<id>`.
  if (pathname === MCP_PATH || pathname.startsWith(`${MCP_PATH}/`)) return "mcp";
  if (pathname === MEMORY_PATH) return "memory";
  // Artifacts OWNS its subtree: one artifact's page is `/library/artifacts/<id>`.
  if (pathname === ARTIFACTS_PATH || pathname.startsWith(`${ARTIFACTS_PATH}/`)) return "artifacts";
  // The public gallery is a view of Skills / Playbooks (#1440); its old bare and digest links
  // land on Playbooks, so they light that entry while they redirect.
  if (pathname === SKILLS_PATH || pathname.startsWith(`${SKILLS_PATH}/`)) return "skills";
  if (pathname === SPECIALISTS_PATH || pathname.startsWith(`${SPECIALISTS_PATH}/`))
    return "specialists";
  if (pathname === MISSION_PLANS_PATH || pathname.startsWith(`${MISSION_PLANS_PATH}/`))
    return "mission-plans";
  if (pathname === GALLERY_SKILLS_PATH || pathname.startsWith(`${GALLERY_SKILLS_PATH}/`))
    return "skills";
  if (pathname === GALLERY_PATH || pathname.startsWith(`${GALLERY_PATH}/`)) return "playbooks";
  if (pathname === PLAYBOOKS_PATH || pathname.startsWith(`${PLAYBOOKS_PATH}/`)) return "playbooks";
  if (pathname === HQ_DISPATCHES_PATH || pathname.startsWith(`${HQ_DISPATCHES_PATH}/`)) return "dispatches";
  if (pathname === HQ_PATH || pathname.startsWith(`${HQ_PATH}/`)) return "hq";
  if (pathname === MISSION_PATH || pathname === LEGACY_MISSION_PATH || (pathname.startsWith(`${MISSION_PATH}/`) && MISSION_ID_RE.test(pathname.slice(MISSION_PATH.length + 1)))) return "missions";
  return null;
}
