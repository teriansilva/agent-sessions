/** HQ shows it on screen (#2102): the directive, the ONE table that turns it into a route, and the
 *  words for the line under an answer. Pure (no React, no DOM), so every rule here is pinned
 *  without a browser.
 *
 *  A directive is a closed union, one member per operation, minted by the server from a closed
 *  catalog (`src/agent_sessions/ask_screen.py`). It never carries a path or a URL: the route of
 *  each target lives HERE, built from `lib/routes.ts` and `settingsPath()`, and
 *  `tests/test_ask_screen.py` pins the keys of the three tables below against the server's
 *  catalog. A member this build does not know is not executed and reads as a link-less note. */
import { matchRoute } from "../components/ask/askMatches";
import { missionLink } from "../lib/missionLink";
import {
  ARTIFACTS_PATH,
  AUTOMATIONS_PATH,
  DASHBOARD_PATH,
  GALLERY_PLAYBOOKS_PATH,
  GALLERY_SKILLS_PATH,
  HQ_DISPATCHES_PATH,
  HQ_PATH,
  MAP_PATH,
  MEMORY_PATH,
  MISSION_PATH,
  MISSION_PLANS_PATH,
  PLAYBOOKS_PATH,
  SESSIONS_PATH,
  SKILLS_PATH,
  SPECIALISTS_PATH,
  TEMPLATES_PATH,
  VARIABLES_PATH,
  artifactPath,
  hqConversationPath,
} from "../lib/routes";
import { isSettingsSection, settingsPath, settingsSection } from "../routes/settingsTabs";

export type ScreenTargetType = "session" | "mission" | "artifact" | "conversation";

export type ScreenDirective =
  | {
      id: string;
      op: "open";
      target: { type: ScreenTargetType; id: string };
      where: "full" | "map";
    }
  | { id: string; op: "go"; section: string }
  | { id: string; op: "map_focus" | "map_minimize" | "map_close"; key: string }
  | { id: string; op: "map_close_all" | "map_arrange" };

export type ScreenOp = ScreenDirective["op"];

/** Every operation a directive can be, and what the workspace does for it (`null`: it navigates). */
export const SCREEN_OPS: Record<ScreenOp, { command: string | null }> = {
  open: { command: null },
  go: { command: null },
  map_focus: { command: "focus" },
  map_minimize: { command: "minimize" },
  map_close: { command: "close" },
  map_close_all: { command: "closeAll" },
  map_arrange: { command: "arrange" },
};

/** The full-screen route of each thing an `open` directive names, by its id. */
export const TARGET_ROUTES: Record<ScreenTargetType, (id: string) => string> = {
  session: (id) => matchRoute(id),
  mission: (id) => missionLink(id),
  artifact: (id) => artifactPath(id),
  conversation: (id) => hqConversationPath(id),
};

export interface ScreenSection {
  label: string;
  /** Where the page lives, when its label alone would not say. */
  group?: string;
  to: string;
}

/** The app's sections outside Settings, by the server's name for each. */
export const SCREEN_SECTIONS: Record<string, ScreenSection> = {
  dashboard: { label: "Dashboard", to: DASHBOARD_PATH },
  hq: { label: "HQ", to: HQ_PATH },
  missions: { label: "Missions", to: MISSION_PATH },
  dispatches: { label: "Notifications", group: "HQ", to: HQ_DISPATCHES_PATH },
  sessions: { label: "New session", group: "Sessions", to: SESSIONS_PATH },
  map: { label: "Sessions map", to: MAP_PATH },
  templates: { label: "Prompt Templates", group: "Library", to: TEMPLATES_PATH },
  variables: { label: "Variables", group: "Library", to: VARIABLES_PATH },
  memory: { label: "Memory", group: "Library", to: MEMORY_PATH },
  artifacts: { label: "Artifacts", group: "Library", to: ARTIFACTS_PATH },
  automations: { label: "Automations", group: "Library", to: AUTOMATIONS_PATH },
  skills: { label: "Skills", group: "Library", to: SKILLS_PATH },
  specialists: { label: "Specialists", group: "Library", to: SPECIALISTS_PATH },
  playbooks: { label: "Playbooks", group: "Library", to: PLAYBOOKS_PATH },
  mission_plans: { label: "Mission plans", group: "Library", to: MISSION_PLANS_PATH },
  gallery_playbooks: { label: "Playbook gallery", group: "Library", to: GALLERY_PLAYBOOKS_PATH },
  gallery_skills: { label: "Skill gallery", group: "Library", to: GALLERY_SKILLS_PATH },
  settings: { label: "Settings", to: settingsPath() },
};

const SETTINGS_PREFIX = "settings.";

/** A section by the server's name: one of the table's, or `settings.<page>` for a page the
 *  Settings list holds. `null` for a name this build does not know. */
export function sectionOf(name: string): ScreenSection | null {
  if (name.startsWith(SETTINGS_PREFIX)) {
    const page = name.slice(SETTINGS_PREFIX.length);
    if (!isSettingsSection(page)) return null;
    return { label: settingsSection(page).label, group: "Settings", to: settingsPath(page) };
  }
  return Object.hasOwn(SCREEN_SECTIONS, name) ? SCREEN_SECTIONS[name] : null;
}

const DIRECTIVE_ID_RE = /^sd_[0-9a-f]{16}$/;
const isObject = (v: unknown): v is Record<string, unknown> =>
  typeof v === "object" && v !== null && !Array.isArray(v);

/** A directive as this build can execute it, or `null`: an unknown operation, target or section,
 *  or a malformed field. Field by field, so nothing a message carried is used unread. */
export function parseDirective(raw: unknown): ScreenDirective | null {
  if (!isObject(raw) || typeof raw.id !== "string" || !DIRECTIVE_ID_RE.test(raw.id)) return null;
  const id = raw.id;
  const op = raw.op;
  if (typeof op !== "string" || !Object.hasOwn(SCREEN_OPS, op)) return null;
  if (op === "open") {
    const target = raw.target;
    if (!isObject(target) || typeof target.id !== "string" || !target.id) return null;
    const type = target.type;
    if (typeof type !== "string" || !Object.hasOwn(TARGET_ROUTES, type)) return null;
    const where = raw.where;
    if (where !== "full" && where !== "map") return null;
    if (where === "map" && type !== "session") return null;
    return { id, op, target: { type: type as ScreenTargetType, id: target.id }, where };
  }
  if (op === "go") {
    if (typeof raw.section !== "string" || !sectionOf(raw.section)) return null;
    return { id, op, section: raw.section };
  }
  if (op === "map_focus" || op === "map_minimize" || op === "map_close") {
    if (typeof raw.key !== "string" || !raw.key.includes(":")) return null;
    return { id, op, key: raw.key };
  }
  return { id, op: op as "map_close_all" | "map_arrange" };
}

/** The session a directive names, if it names one. */
export function sessionOf(d: ScreenDirective): string | null {
  if (d.op === "open") return d.target.type === "session" ? d.target.id : null;
  return "key" in d ? d.key : null;
}

/** Does it ask for a session as a window in the map? */
export const asksMapWindow = (d: ScreenDirective): boolean =>
  (d.op === "open" && d.where === "map") || d.op === "map_focus";

/** The route a directive's target lives at: what a full-screen open navigates to, and what the
 *  line's Open link falls back on. The map's commands live on the map. */
export function directiveRoute(d: ScreenDirective): string {
  if (d.op === "open") return TARGET_ROUTES[d.target.type](d.target.id);
  if (d.op === "go") return sectionOf(d.section)?.to ?? DASHBOARD_PATH;
  return MAP_PATH;
}

/** Why a map window was not opened as one: the map cannot host a window here (a phone, a small
 *  window), or it is at its window limit. */
export type MapRefusal = "host" | "cap";

/** What the asking device did with one directive. Its own, in memory: never reported back, and
 *  gone with the page — a directive read back from history has none and claims nothing. */
export type ScreenOutcome =
  /** Queued for the map: not a result yet. */
  | { state: "pending" }
  | { state: "shown"; where: "full" | "map" }
  /** A map window was asked for; it opened full screen instead. */
  | { state: "fallback"; reason: MapRefusal }
  /** Beyond the map's room, and not opened: one full-screen navigation after another would
   *  only leave the last on screen. Listed, with a way in. */
  | { state: "left"; reason: MapRefusal }
  /** An unsaved-changes guard held the navigation and the Commander stayed. */
  | { state: "held" }
  /** A map command ran. */
  | { state: "done" }
  /** A map command with nothing to act on: no window for it, every window minimised
   *  (`parked`, Arrange), or a map that cannot host one here (`host`, Arrange). */
  | { state: "none"; reason?: "parked" | "host" }
  /** The map never answered (it could not be shown in time): the command was withdrawn. */
  | { state: "unanswered" }
  /** Superseded by a later directive of the same turn about the same window (opened, then
   *  closed; minimised, then brought back). No longer how the map is, so it is not reported:
   *  the later directive has its own line. */
  | { state: "undone" };

/** A turn's stored request as the conversation carries it (`shows`), or a live event's. */
export interface ScreenShow {
  directive: ScreenDirective | null;
  /** The directive's id, also for one this build cannot parse. */
  id: string;
  title: string;
}

/** One stored `shows` row → a show. The server sends the directive's fields beside its title. */
export function showOf(raw: unknown): ScreenShow | null {
  if (!isObject(raw) || typeof raw.id !== "string") return null;
  return {
    directive: parseDirective(raw),
    id: raw.id,
    title: typeof raw.title === "string" ? raw.title : "",
  };
}

/** What the line's Open does. `sessions`: as map windows where the map has room for them,
 *  otherwise the FIRST one full screen — never several full-screen navigations in a row. */
export type ScreenOpen =
  | { type: "route"; to: string }
  | { type: "sessions"; sessions: { key: string; title: string }[] };

export interface ScreenLine {
  /** `shown`: applied here. `asked`: a request that claims nothing. `held`: not opened. */
  kind: "shown" | "asked" | "held";
  lead: string;
  /** The target, in the stronger weight. Empty for a line with no target. */
  strong: string;
  tail: string;
  open: ScreenOpen | null;
}

const REASON: Record<MapRefusal, string> = {
  host: "the map can't host a window here",
  cap: "the map is full",
};
const NOUN: Record<ScreenTargetType, string> = {
  session: "a session",
  mission: "a mission",
  artifact: "an artifact",
  conversation: "this conversation",
};
const count = (n: number, one: string, many: string) => `${n} ${n === 1 ? one : many}`;

interface Entry {
  d: ScreenDirective;
  title: string;
  outcome: ScreenOutcome | null;
}

/** The target's words: the server's title, else what kind of thing it is. */
function nameOf(e: Entry): string {
  const { d } = e;
  if (d.op === "go") return sectionOf(d.section)?.label ?? "a section";
  if (d.op === "map_close_all" || d.op === "map_arrange") return "the map";
  if (d.op === "open") return e.title || NOUN[d.target.type];
  return e.title || NOUN.session;
}

/** Where it is (or was asked to be), after the "·". */
function placeOf(e: Entry): string {
  const { d, outcome } = e;
  if (d.op === "go") return sectionOf(d.section)?.group ?? "";
  if (d.op !== "open") {
    if (d.op === "map_minimize") return "minimised in the map";
    if (d.op === "map_close") return "window closed";
    if (d.op === "map_close_all") return "all windows closed";
    if (d.op === "map_arrange") return "windows arranged";
    return outcome?.state === "fallback" ? `full screen (${REASON[outcome.reason]})` : "in the map";
  }
  if (d.target.type !== "session") {
    return d.target.type === "conversation" ? "HQ page" : d.target.type;
  }
  if (outcome?.state === "fallback") return `full screen (${REASON[outcome.reason]})`;
  if (outcome?.state === "shown") return outcome.where === "map" ? "in the map" : "full screen";
  return d.where === "map" ? "in the map" : "full screen";
}

const tailOf = (place: string) => (place ? ` · ${place}` : "");

function openOf(entries: Entry[]): ScreenOpen {
  const sessions = entries.map((e) => {
    const key = asksMapWindow(e.d) ? sessionOf(e.d) : null;
    return key ? { key, title: e.title } : null;
  });
  if (sessions.every((s) => s !== null)) return { type: "sessions", sessions };
  return { type: "route", to: directiveRoute(entries[entries.length - 1].d) };
}

/** Are they all sessions put on screen? A window minimised or closed is a session too, but
 *  "2 sessions" would then read as two that were shown. */
const allSessions = (entries: Entry[]) =>
  entries.every((e) => (e.d.op === "open" || e.d.op === "map_focus") && sessionOf(e.d) !== null);

/** The lines under one turn's answer: at most one for what was shown here, one for what was only
 *  asked for (history, another device, a closed page, the part of a batch the map had no room
 *  for), and one for what a guard held. `outcomeOf` is this device's memory of a directive. */
export function screenLines(
  shows: ScreenShow[],
  outcomeOf: (id: string) => { outcome: ScreenOutcome; title: string } | undefined,
): ScreenLine[] {
  const entries: Entry[] = [];
  let unknown = 0;
  for (const s of shows) {
    if (!s.directive) {
      unknown += 1;
      continue;
    }
    const mine = outcomeOf(s.id);
    if (mine?.outcome.state === "undone") continue;
    entries.push({ d: s.directive, title: mine?.title || s.title, outcome: mine?.outcome ?? null });
  }
  const applied = entries.filter(
    (e) => e.outcome && ["shown", "fallback", "done"].includes(e.outcome.state),
  );
  const left = entries.filter((e) => e.outcome?.state === "left");
  const asked = entries.filter((e) => !e.outcome || e.outcome.state === "pending");
  const held = entries.filter((e) => e.outcome?.state === "held");
  const none = entries.filter((e) => e.outcome?.state === "none");
  const unanswered = entries.filter((e) => e.outcome?.state === "unanswered");
  const lines: ScreenLine[] = [];

  if (applied.length === 1 && left.length === 0) {
    const [e] = applied;
    lines.push({
      kind: "shown",
      lead: "Shown: ",
      strong: nameOf(e),
      tail: tailOf(placeOf(e)),
      open: { type: "route", to: directiveRoute(e.d) },
    });
  } else if (applied.length > 0) {
    const inMap = applied.filter(
      (e) => asksMapWindow(e.d) && e.outcome?.state === "shown" && e.outcome.where === "map",
    );
    const onlyWindows = inMap.length === applied.length;
    const why = left[0]?.outcome?.state === "left" ? ` (${REASON[left[0].outcome.reason]})` : "";
    let strong: string;
    let tail = "";
    if (onlyWindows) {
      const of = inMap.length + left.length;
      strong = left.length
        ? `${inMap.length} of ${count(of, "session", "sessions")}`
        : count(inMap.length, "session", "sessions");
      tail = ` · in the map${why}`;
    } else if (applied.length === 1) {
      strong = nameOf(applied[0]);
      tail = tailOf(placeOf(applied[0]));
    } else {
      strong = allSessions(applied)
        ? count(applied.length, "session", "sessions")
        : count(applied.length, "item", "items");
    }
    lines.push({
      kind: "shown",
      lead: "Shown: ",
      strong,
      tail,
      open: onlyWindows
        ? { type: "route", to: MAP_PATH }
        : { type: "route", to: directiveRoute(applied[applied.length - 1].d) },
    });
  }

  const rest = [...left, ...asked];
  if (rest.length === 1 && applied.length === 0) {
    const [e] = rest;
    lines.push({
      kind: "asked",
      lead: "Asked to show: ",
      strong: nameOf(e),
      tail: tailOf(placeOf({ ...e, outcome: null })),
      open: openOf(rest),
    });
  } else if (rest.length > 0) {
    const more = applied.length > 0 ? "more " : "";
    const strong = allSessions(rest)
      ? `${rest.length} ${more}${rest.length === 1 ? "session" : "sessions"}`
      : `${rest.length} ${more}${rest.length === 1 ? "item" : "items"}`;
    const windows = applied.length === 0 && rest.every((e) => asksMapWindow(e.d));
    lines.push({
      kind: "asked",
      lead: "Asked to show: ",
      strong,
      tail: windows ? " · in the map" : "",
      open: openOf(rest),
    });
  }
  if (unknown > 0) {
    lines.push({
      kind: "asked",
      lead: "Asked to show: ",
      strong: "",
      tail: "something this version of the app cannot open",
      open: null,
    });
  }

  if (held.length > 0) {
    lines.push({
      kind: "held",
      lead: "Not opened: ",
      strong: "",
      tail: "you have unsaved changes on this page",
      open: openOf(held),
    });
  } else if (unanswered.length > 0) {
    lines.push({
      kind: "held",
      lead: "Not opened: ",
      strong: "",
      tail: "the map did not open in time",
      open: openOf(unanswered),
    });
  } else if (none.length > 0) {
    const why = none[0].outcome?.state === "none" ? none[0].outcome.reason : undefined;
    lines.push({
      kind: "held",
      lead: "Not opened: ",
      strong: "",
      tail:
        why === "parked"
          ? "every window in the map is minimised"
          : why === "host"
            ? REASON.host
            : none.length === 1 && sessionOf(none[0].d)
              ? "that session has no window in the map"
              : "the map has no window for it",
      open: null,
    });
  }
  return lines;
}
