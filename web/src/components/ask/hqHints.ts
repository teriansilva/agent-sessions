/** Hints in HQ's empty thread (#2265): example questions that teach what HQ can do.
 *
 *  - **A static catalog.** Every hint is a question HQ answers today: `tool` names the HQ tool
 *    that answers it, and `tests/test_hq_hints.py` fails when that tool is gone. A `manual` hint
 *    also names the manual page its answer comes from; the same test fails when the installed
 *    manual's search no longer finds that page for the question.
 *  - **Something new each open, per pool.** The manual slot cycles every manual hint before one
 *    repeats; the other two slots take the least recently shown non-manual hints, each of a kind
 *    not already in the set. With 14 non-manual hints, the 8th open repeats one of them while the
 *    manual slot is still on its first cycle: intended.
 *  - **Device-local, best effort.** What was shown lives in `localStorage` under
 *    {@link STORAGE_KEY}. A read or a write that throws, or a value that does not parse, falls
 *    back to an in-memory map for the page's lifetime: rotation keeps working, HQ never breaks.
 *    Only catalog ids with a finite, non-negative time are kept.
 *
 *  Nothing here sends anything: a hint only ever fills the composer. */

export type HintKind =
  | "manual"
  | "sessions"
  | "missions"
  | "start"
  | "screen"
  | "memory"
  | "library"
  | "artifacts"
  | "usage"
  | "automations";

export interface Hint {
  id: string;
  kind: HintKind;
  /** The HQ tool that answers it (an `ask_orchestrator.TOOLS` name). */
  tool: string;
  /** `manual` hints only: the `docs/site` page the answer comes from. */
  page?: string;
  text: string;
}

/** The micro-label each kind shows above its question. */
export const KIND_LABELS: Record<HintKind, string> = {
  manual: "Manual",
  sessions: "Sessions",
  missions: "Missions",
  start: "Start work",
  screen: "On screen",
  memory: "Memory",
  library: "Library",
  artifacts: "Artifacts",
  usage: "Usage",
  automations: "Automations",
};

// One hint per line: `tests/test_hq_hints.py` reads this array line by line.
// prettier-ignore
export const HINTS: readonly Hint[] = [
  { id: "manual-home-free", kind: "manual", tool: "docs_search", page: "guide/home-free", text: "How do I pair Home Free with my phone?" },
  { id: "manual-playbook-flow", kind: "manual", tool: "docs_search", page: "guide/playbooks", text: "What is a playbook flow, and when would I use one?" },
  { id: "manual-session-limit", kind: "manual", tool: "docs_search", page: "guide/settings", text: "How do I set the session limit?" },
  { id: "manual-mcp-server", kind: "manual", tool: "docs_search", page: "guide/mcp-servers", text: "How do I connect an MCP server to a project?" },
  { id: "manual-handoff", kind: "manual", tool: "docs_search", page: "guide/handoff", text: "How do I hand a session over to another agent?" },
  { id: "manual-automations", kind: "manual", tool: "docs_search", page: "guide/automations", text: "How do automations decide when to run?" },
  { id: "manual-secrets", kind: "manual", tool: "docs_search", page: "guide/secrets", text: "How do I give agents a secret without pasting it into the chat?" },
  { id: "manual-calls", kind: "manual", tool: "docs_search", page: "guide/calls", text: "How do I call HQ by voice?" },
  { id: "sessions-needs-me", kind: "sessions", tool: "needs_you", text: "Which sessions need me right now?" },
  { id: "sessions-yesterday", kind: "sessions", tool: "list_sessions", text: "What did I work on yesterday?" },
  { id: "sessions-busy", kind: "sessions", tool: "fleet_status", text: "What are my agents busy with right now?" },
  { id: "missions-progress", kind: "missions", tool: "list_missions", text: "How far along are my missions, and what are they waiting on?" },
  { id: "start-session", kind: "start", tool: "propose_session", text: "Start a Claude session in my main project to update the dependencies" },
  { id: "start-steps", kind: "start", tool: "propose_steps", text: "Set up a new project for the docs site, then start a session in it" },
  { id: "screen-map", kind: "screen", tool: "show", text: "Show me the sessions map" },
  { id: "memory-remember", kind: "memory", tool: "remember_hq_memory", text: "Remember that we only deploy on Fridays before 14:00" },
  { id: "memory-recall", kind: "memory", tool: "search_memory", text: "What have we decided about deploys?" },
  { id: "library-playbooks", kind: "library", tool: "list_playbooks", text: "Which playbooks do I have for code review?" },
  { id: "library-specialists", kind: "library", tool: "list_specialists", text: "Which specialists could review a pull request?" },
  { id: "artifacts-report", kind: "artifacts", tool: "artifacts_find", text: "Give me the link to the report an agent published yesterday" },
  { id: "usage-plan", kind: "usage", tool: "agent_usage", text: "How much of my Claude plan have I used this week?" },
  { id: "automations-today", kind: "automations", tool: "list_automations", text: "Which automations ran today?" },
];

export const SET_SIZE = 3;
export const STORAGE_KEY = "battlelab.hqHints.v1";

/** When each hint was last shown, by id (epoch ms). */
export type Shown = Record<string, number>;

/** Least recently shown first: never shown before any shown one, ties in catalog order. */
function byAge(pool: readonly Hint[], shown: Shown): Hint[] {
  const order = new Map(pool.map((h, i) => [h.id, i]));
  const at = (h: Hint) =>
    Object.prototype.hasOwnProperty.call(shown, h.id) ? shown[h.id] : -1;
  return [...pool].sort(
    (x, y) => at(x) - at(y) || order.get(x.id)! - order.get(y.id)!,
  );
}

/** The next set: the least recently shown manual hint, then the least recently shown non-manual
 *  hints of kinds not yet in the set. `avoid` (the set on screen) is skipped while anything else
 *  of an eligible kind remains, so ↻ Other ideas never shows the same hint twice in a row. */
export function pickHints(
  catalog: readonly Hint[],
  shown: Shown,
  avoid: ReadonlySet<string> = new Set(),
): Hint[] {
  const fresh = (pool: Hint[]) => {
    const other = pool.filter((h) => !avoid.has(h.id));
    return other.length
      ? [...other, ...pool.filter((h) => avoid.has(h.id))]
      : pool;
  };
  const out: Hint[] = [];
  const manual = fresh(
    byAge(
      catalog.filter((h) => h.kind === "manual"),
      shown,
    ),
  );
  if (manual.length) out.push(manual[0]);
  const rest = fresh(
    byAge(
      catalog.filter((h) => h.kind !== "manual"),
      shown,
    ),
  );
  for (const h of rest) {
    if (out.length >= SET_SIZE) break;
    if (!out.some((o) => o.kind === h.kind)) out.push(h);
  }
  return out;
}

/** The set shown last: every id recorded at the latest time. An open avoids it, so two opens in a
 *  row never share a hint while anything else of an eligible kind remains. */
export function lastSet(shown: Shown): Set<string> {
  const times = Object.values(shown);
  if (!times.length) return new Set();
  const latest = Math.max(...times);
  return new Set(Object.keys(shown).filter((id) => shown[id] === latest));
}

/** Only catalog ids with a finite, non-negative time survive. */
export function cleanShown(
  raw: unknown,
  catalog: readonly Hint[] = HINTS,
): Shown {
  const ids = new Set(catalog.map((h) => h.id));
  const out: Shown = {};
  if (!raw || typeof raw !== "object" || Array.isArray(raw)) return out;
  for (const [id, t] of Object.entries(raw as Record<string, unknown>)) {
    if (ids.has(id) && typeof t === "number" && Number.isFinite(t) && t >= 0)
      out[id] = t;
  }
  return out;
}

/** The storage this module talks to: `window.localStorage`, or none. A getter, because reading
 *  `localStorage` can itself throw (storage disabled). */
export interface HintStore {
  get(): string | null;
  set(value: string): void;
}

export function browserStore(): HintStore | null {
  try {
    const ls = window.localStorage;
    return {
      get: () => ls.getItem(STORAGE_KEY),
      set: (v) => ls.setItem(STORAGE_KEY, v),
    };
  } catch {
    return null;
  }
}

/** The in-memory copy: what was shown this page lifetime, whatever storage did. */
let memory: Shown = {};

export function loadShown(
  store: HintStore | null,
  catalog: readonly Hint[] = HINTS,
): Shown {
  let stored: Shown = {};
  try {
    const raw = store?.get();
    if (raw)
      stored = cleanShown(
        (JSON.parse(raw) as { shown?: unknown })?.shown,
        catalog,
      );
  } catch {
    stored = {};
  }
  // Merge with what this page already recorded: a write that failed must not forget it.
  const merged: Shown = { ...stored };
  for (const [id, t] of Object.entries(cleanShown(memory, catalog)))
    if (!(id in merged) || merged[id] < t) merged[id] = t;
  return merged;
}

/** Records `hints` as shown at `now`. Never throws. */
export function recordShown(
  store: HintStore | null,
  hints: readonly Hint[],
  now: number,
  catalog: readonly Hint[] = HINTS,
): Shown {
  const next = loadShown(store, catalog);
  for (const h of hints) next[h.id] = now;
  memory = next;
  try {
    store?.set(JSON.stringify({ shown: next }));
  } catch {
    // Quota, private mode, storage denied: the in-memory copy carries the rotation.
  }
  return next;
}

/** Tests only: forget the in-memory copy. */
export function resetHintMemory(): void {
  memory = {};
}
