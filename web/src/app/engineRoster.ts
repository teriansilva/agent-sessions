/**
 * The engine roster — the ONE place the SPA learns which agents exist (#853 P4).
 *
 * Every surface that names, colours, offers or gates an agent reads it from here, and here reads
 * it from `/api/engines`, which the server builds from the plugin manifests. The client keeps no
 * list of its own: adding or removing an agent is a manifest, never an edit in `web/src` (the
 * roster ratchet test fails CI on an engine-id literal outside fixtures).
 *
 * Two things are kept apart on purpose, because conflating them is how a guess becomes an action:
 *
 * - **Appearance** degrades gracefully. Before the roster has loaded — or for an id it does not
 *   list, such as a session of a removed agent — `engineBadge` / `engineName` / `engineColor`
 *   answer the raw id with the neutral slate accent. Nothing blocks on the roster.
 * - **Eligibility** does not. `rosterReady()` is false until a roster has actually loaded, and
 *   every control that mints, launches or hands off waits for it; an unknown id has no
 *   capabilities at all.
 *
 * A module store rather than only a context: `overviewGraph.ts` and other non-React helpers
 * format engines too. Components subscribe with `useEngineRoster()` so they re-render when the
 * roster lands.
 */
import { useSyncExternalStore } from "react";
import type { EngineFamily, EngineInfo, EngineProblem, OfferedModel } from "../types/api";

export type RosterStatus = "loading" | "ready" | "failed";

export interface RosterSnapshot {
  status: RosterStatus;
  /** True once ANY roster has loaded — stays true through a failed refresh (stale kept). */
  loaded: boolean;
  engines: readonly EngineInfo[];
  problems: readonly EngineProblem[];
  /** The server's family decision (#1728); empty from an older server (no API-first default). */
  families: readonly EngineFamily[];
}

const EMPTY: RosterSnapshot = {
  status: "loading",
  loaded: false,
  engines: [],
  problems: [],
  families: [],
};

let snapshot: RosterSnapshot = EMPTY;
let byId = new Map<string, EngineInfo>();
/** Client id → the family row that lists it (#1728). Built from the server's block only. */
let familyByClient = new Map<string, EngineFamily>();
const listeners = new Set<() => void>();

function publish(next: RosterSnapshot): void {
  snapshot = next;
  byId = new Map(next.engines.map((e) => [e.id, e]));
  familyByClient = new Map(next.families.flatMap((f) => f.clients.map((c) => [c, f] as const)));
  for (const fn of listeners) fn();
}

// --- the persisted runtime map (#1502) -------------------------------------------------------------
//
// Which PANE a session draws (terminal, structured, chat) depends on its engine's runtime, and the
// roster that says so lands asynchronously. A cold page load (a notification click, a link from
// another tab) used to mount a terminal for an API engine first and show its refusal as "session
// not found". So every loaded roster also writes `{engineId: runtime}` to `localStorage`, and the
// map is read back at module init: a returning browser picks the right pane on its FIRST render.
//
// This is APPEARANCE-class data — which pane to draw — never eligibility: nothing mints, launches
// or hands off on it, and `rosterReady()` keeps its meaning. It is written only from a loaded
// roster (never authored here, so the no-engine-literal ratchet holds), and the loaded roster
// always wins over it, so a stale entry corrects itself the moment the roster lands.

/** The storage key. Versioned: a future shape gets a new key and this one is simply ignored. */
export const RUNTIME_HINTS_KEY = "battlelab.engineRuntimes.v1";
/** Bounds on what is read back: a corrupt, hostile or oversized value is ignored as a whole. */
const HINTS_MAX_BYTES = 8_192;
const HINTS_MAX_ENGINES = 64;
const HINT_ID_RE = /^[a-z0-9][a-z0-9_-]{0,63}$/;
const HINT_RUNTIME_RE = /^[a-z]{1,16}$/;

/** Parse a stored map, or `null` when it is anything but a well-formed, bounded v1 map. */
export function parseRuntimeHints(raw: string | null): Map<string, string> | null {
  if (typeof raw !== "string" || raw.length === 0 || raw.length > HINTS_MAX_BYTES) return null;
  let doc: unknown;
  try {
    doc = JSON.parse(raw);
  } catch {
    return null;
  }
  if (!doc || typeof doc !== "object" || (doc as { v?: unknown }).v !== 1) return null;
  const runtimes = (doc as { runtimes?: unknown }).runtimes;
  if (!runtimes || typeof runtimes !== "object" || Array.isArray(runtimes)) return null;
  const entries = Object.entries(runtimes as Record<string, unknown>);
  if (entries.length > HINTS_MAX_ENGINES) return null;
  const out = new Map<string, string>();
  for (const [id, runtime] of entries) {
    if (!HINT_ID_RE.test(id) || typeof runtime !== "string" || !HINT_RUNTIME_RE.test(runtime))
      return null;
    out.set(id, runtime);
  }
  return out;
}

function readStoredHints(): Map<string, string> {
  try {
    return parseRuntimeHints(globalThis.localStorage?.getItem(RUNTIME_HINTS_KEY) ?? null) ?? new Map();
  } catch {
    return new Map(); // storage unavailable (private mode, a sandboxed frame): no hints
  }
}

let hints: Map<string, string> = readStoredHints();

/** Record the loaded roster's runtimes — the WHOLE map, so an engine that left the roster leaves
 *  the map too. Best-effort: a full or unavailable storage only costs the next cold load a wait. */
function writeHints(engines: readonly EngineInfo[]): void {
  const runtimes: Record<string, string> = {};
  let n = 0;
  for (const e of engines) {
    if (n >= HINTS_MAX_ENGINES) break;
    if (!HINT_ID_RE.test(e.id) || !HINT_RUNTIME_RE.test(e.runtime) || e.runtime === "unknown")
      continue;
    runtimes[e.id] = e.runtime;
    n += 1;
  }
  hints = new Map(Object.entries(runtimes));
  try {
    const raw = JSON.stringify({ v: 1, runtimes });
    if (raw.length <= HINTS_MAX_BYTES) globalThis.localStorage?.setItem(RUNTIME_HINTS_KEY, raw);
  } catch {
    /* quota / private mode: degrade to the no-hint path */
  }
}

/** Re-read the stored map, as module init does — the test seam for "a returning browser". */
export function loadRuntimeHints(): void {
  hints = readStoredHints();
}

const NO_CAPABILITIES: EngineInfo["capabilities"] = {
  resume: false,
  new: false,
  archive: false,
  handoff_target: false,
  seed_start: false,
  orchestrator_input: false,
  raw_tty: false,
  owns_transcript: false,
};

/** One roster row made SAFE to read (#853 P4). The server always sends the full shape, but a row
 *  can arrive without it — a cached SPA against an older server, a partial test mock — and a
 *  missing `display` must degrade to the neutral fallback, never throw inside every badge. What
 *  cannot be known stays unknown: no `session_id` means the id mode is unknown (launches wait),
 *  and capabilities default to NONE. A row that is not even an object is dropped. */
function normalize(raw: unknown): EngineInfo | null {
  if (!raw || typeof raw !== "object" || typeof (raw as EngineInfo).id !== "string") return null;
  const e = raw as Partial<EngineInfo> & { id: string };
  const d = (e.display ?? {}) as Partial<EngineInfo["display"]>;
  return {
    ...(e as EngineInfo),
    label: typeof e.label === "string" ? e.label : e.id,
    kind: typeof e.kind === "string" ? e.kind : "unknown",
    runtime: typeof e.runtime === "string" ? e.runtime : "unknown",
    display: {
      name: typeof d.name === "string" ? d.name : e.id,
      badge: typeof d.badge === "string" ? d.badge : e.id.slice(0, 2),
      accent: typeof d.accent === "string" ? d.accent : "slate",
      id_prefix: typeof d.id_prefix === "string" ? d.id_prefix : null,
      order: typeof d.order === "number" ? d.order : 500,
    },
    capabilities: { ...NO_CAPABILITIES, ...(e.capabilities ?? {}) },
    session_id: e.session_id ?? { mint: "unknown" },
    models: Array.isArray(e.models) ? e.models : [],
    usage: e.usage ?? { source: "none" },
    terminal: e.terminal ?? { repaint: "none" },
  };
}

/** One `families` row made safe to read (#1728): a malformed row is dropped as a whole, so a
 *  partial mock or a newer shape can only REMOVE the API-first default, never invent one. */
function normalizeFamily(raw: unknown): EngineFamily | null {
  if (!raw || typeof raw !== "object") return null;
  const f = raw as Partial<EngineFamily>;
  const d = f.interactive_default as EngineFamily["interactive_default"] | undefined;
  if (
    typeof f.family !== "string" ||
    !Array.isArray(f.clients) ||
    !f.clients.every((c) => typeof c === "string") ||
    !d ||
    typeof d !== "object" ||
    (d.engine !== null && typeof d.engine !== "string") ||
    typeof d.reason !== "string"
  )
    return null;
  const reasons: Record<string, string> = {};
  if (f.reasons && typeof f.reasons === "object" && !Array.isArray(f.reasons)) {
    for (const [k, v] of Object.entries(f.reasons)) if (typeof v === "string") reasons[k] = v;
  }
  return {
    family: f.family,
    clients: [...f.clients],
    interactive_default: { engine: d.engine, reason: d.reason },
    unattended_admitted: Array.isArray(f.unattended_admitted)
      ? f.unattended_admitted.filter((c): c is string => typeof c === "string")
      : [],
    reasons,
  };
}

/** Install a roster (the provider does this; tests do it directly). `families` is the server's
 *  family decision (#1728) — omitted by an older server, which then has no API-first default. */
export function setRoster(
  engines: readonly EngineInfo[],
  problems: readonly EngineProblem[] = [],
  families: readonly EngineFamily[] = [],
): void {
  const ordered = (engines as unknown[])
    .map(normalize)
    .filter((e): e is EngineInfo => e !== null)
    .sort(
    (a, b) => (a.display?.order ?? 500) - (b.display?.order ?? 500),
  );
  writeHints(ordered);
  const fams = (Array.isArray(families) ? (families as unknown[]) : [])
    .map(normalizeFamily)
    .filter((f): f is EngineFamily => f !== null);
  publish({ status: "ready", loaded: true, engines: ordered, problems, families: fams });
}

/** A refresh failed. The last good roster is KEPT — a stale roster is never replaced by none. */
export function markRosterFailed(): void {
  // A retry that fails AGAIN changes nothing, so it publishes nothing: a fresh snapshot every 15 s
  // would rebuild every subscriber's memo — on the map, the whole node array, which React Flow
  // then hides until re-measured (the #936 flicker).
  if (snapshot.status === "failed") return;
  publish({ ...snapshot, status: "failed" });
}

/** Back to "nothing loaded" — tests only. The in-memory runtime hints go too (a never-visited
 *  browser); `loadRuntimeHints()` re-reads whatever storage still holds. */
export function resetRoster(): void {
  hints = new Map();
  publish(EMPTY);
}

export function getRoster(): RosterSnapshot {
  return snapshot;
}

export function subscribeRoster(fn: () => void): () => void {
  listeners.add(fn);
  return () => listeners.delete(fn);
}

/** Subscribe a component: it re-renders when the roster loads or changes. */
export function useEngineRoster(): RosterSnapshot {
  return useSyncExternalStore(subscribeRoster, getRoster, getRoster);
}

export function rosterReady(): boolean {
  return snapshot.loaded;
}

/** The roster row for `id`, or `undefined` — for an unknown id, or before the roster loads. */
export function engineInfo(id: string): EngineInfo | undefined {
  return byId.get(id);
}

// --- appearance (degrades to the raw id; never blocks) --------------------------------------------

/** The short badge (`cc`, `oc`, …); the first two characters of an unknown id. */
export function engineBadge(id: string): string {
  // Some rows reach a renderer with no engine at all (a mission session record); that renders
  // blank, never throws — the pre-roster code tolerated it, and an error boundary is not a badge.
  return byId.get(id)?.display.badge ?? (id ?? "").slice(0, 2);
}

/** The display name (`claude`, `kimi`, …); the raw id when unknown. */
export function engineName(id: string): string {
  return byId.get(id)?.display.name ?? id ?? "";
}

/** The human label (`Claude Code`, `Kimi Code`, …); the raw id when unknown. */
export function engineLabel(id: string): string {
  return byId.get(id)?.label ?? id ?? "";
}

/** The accent TOKENS a manifest may name (`kinds.ACCENT_TOKENS` server-side), each spelled out as
 *  its whole `var(...)` so the CSS guard can see every token the app reads. This is the design
 *  system's colour vocabulary, not a list of engines: an agent picks one, it never adds one. */
const ACCENT_VAR: Readonly<Record<string, string>> = {
  amber: "var(--engine-amber)",
  teal: "var(--engine-teal)",
  green: "var(--engine-green)",
  blue: "var(--engine-blue)",
  lime: "var(--engine-lime)",
  magenta: "var(--engine-magenta)",
  slate: "var(--engine-slate)",
};

/** The engine's accent as a CSS value — always a token, never a hex (#853 P4). An unknown id (or
 *  an accent this build has no token for) is the neutral slate, deliberately not an agent hue. */
export function engineColor(id: string): string {
  const accent = byId.get(id)?.display.accent;
  return accent && Object.hasOwn(ACCENT_VAR, accent)
    ? ACCENT_VAR[accent]
    : ACCENT_VAR.slate;
}

// --- capabilities (default-deny; an unknown id can do nothing) ------------------------------------

/** Is there an agent behind this engine? The plain shell has none. */
export function isAgent(id: string): boolean {
  return byId.get(id)?.kind === "agent";
}

/** May this engine be offered as a handoff target? (The server re-checks.) */
export function canBeHandoffTarget(id: string): boolean {
  const e = byId.get(id);
  return (
    !!e && e.capabilities.handoff_target && e.supports_seed_start && isActive(e)
  );
}

/** Does this engine mint its own id, so a new session launches under a `new-<uuid>` placeholder
 *  and reconciles afterwards? Unknown ⇒ undefined: the caller must WAIT, never guess (#454). */
export function mintsOwnId(id: string): boolean | undefined {
  const mint = byId.get(id)?.session_id.mint;
  return mint === "adopt" ? true : mint === "pinned" ? false : undefined;
}

/** Does this engine's TUI wipe and repaint scrollback (codex, kimi — #969)? */
/** The models a new session of `id` may request (#1189), `default` excluded — the server's own
 *  list (manifest + operator-added). Empty ⇒ the picker offers `default` only. */
export function offeredModels(id: string): OfferedModel[] {
  const sel = byId.get(id)?.model_select;
  return sel?.supported ? sel.offered : [];
}

export function wipesOnRepaint(id: string): boolean {
  return byId.get(id)?.terminal.repaint === "wipe";
}

/** Does this engine repaint its active viewport on a same-size SIGWINCH and re-stream the full
 *  conversation on any geometry change? Until a roster has loaded (`snapshot.loaded`), returns
 *  `undefined` so automatic resize-jiggle nudges wait or fail closed rather than guessing `false`. */
export function repaintsOnWinch(id: string): boolean | undefined {
  if (!snapshot.loaded) return undefined;
  return byId.get(id)?.terminal.repaint === "winch";
}

/** The native-id prefix to strip for display (`ses_`, `session_`), or "". */
export function idPrefix(id: string): string {
  return byId.get(id)?.display.id_prefix ?? "";
}

/** `retiring` engines (#1126 PR B) are attach-only; a missing status is active. */
export function isActive(e: EngineInfo): boolean {
  return (e.status ?? "active") === "active";
}

/** Which runtime to DRAW a session of `id` with (#1502): the loaded roster's answer when there
 *  is one; before any roster has loaded, the runtime the last loaded roster recorded; otherwise
 *  `undefined`. Appearance only — never consult it to launch, mint or hand off. Once a roster is
 *  loaded it alone answers, so an id it does not list has no hint even if storage remembers one. */
export function runtimeHint(id: string): string | undefined {
  if (snapshot.loaded) {
    const runtime = byId.get(id)?.runtime;
    return runtime === undefined || runtime === "unknown" ? undefined : runtime;
  }
  return hints.get(id);
}

/** Has the loaded roster itself stated this engine's runtime (as opposed to a stored hint)? */
export function runtimeConfirmed(id: string): boolean {
  return snapshot.loaded && runtimeHint(id) !== undefined;
}

/** Does a session of this engine run in a terminal (#853 §7)? Unknown ⇒ undefined (loading). */
export function runsInTerminal(id: string): boolean | undefined {
  const runtime = byId.get(id)?.runtime;
  return runtime === undefined || runtime === "unknown" ? undefined : runtime === "pty";
}

// --- client families: API first (#1728) ---------------------------------------------------------
//
// Which client of an agent a new session defaults to is the SERVER's decision (`families`, from
// `engines.default_client`): the SPA cannot mirror a live readiness probe, so it never derives one.
// What the SPA adds is the operator's per-device explicit choice (`remembered`, kept by
// `lib/familyChoice.ts`), applied on top with its own two reasons. An older server sends no block,
// and then nothing here applies: the pickers behave exactly as before.

/** Why a picker preselects the client it does: the server's base reasons, plus the device's. */
export type FamilyReason =
  | "api_ready"
  | "api_unready"
  | "api_absent"
  | "family_retired"
  | "remembered_explicit"
  | "remembered_stale"
  | string;

/** The family row listing client `id`, or `undefined` (an older server, or an unknown id). */
export function familyOf(id: string): EngineFamily | undefined {
  return familyByClient.get(id);
}

export interface FamilyPick {
  family: EngineFamily;
  /** The client to preselect, or null when none of the family's clients is offered. */
  engine: string | null;
  reason: FamilyReason;
  /** A remembered client that no longer applies, and why — shown, never silently dropped. */
  stale: { engine: string; why: string } | null;
  /** What the create sends: the server's default (`automatic`) or the operator's own choice. */
  choice: "automatic" | "explicit";
}

/** THE family resolver every New session picker uses (#1728). `offered` is what the picker can
 *  start right now (the server's answer); `remembered` the operator's last explicit client for
 *  this family on this device. A remembered client wins while the family still lists it and it
 *  can start (`remembered_explicit`); otherwise it is ignored with its reason
 *  (`remembered_stale`) and the server's base default applies. Nothing here is a readiness check:
 *  the create re-checks, and refuses a stale `automatic` choice. */
export function pickInFamily(
  family: EngineFamily,
  offered: readonly string[],
  remembered: string | null | undefined,
): FamilyPick {
  let stale: FamilyPick["stale"] = null;
  if (remembered) {
    const listed = family.clients.includes(remembered);
    const info = byId.get(remembered);
    if (listed && offered.includes(remembered) && info && isActive(info)) {
      return {
        family,
        engine: remembered,
        reason: "remembered_explicit",
        stale: null,
        choice: "explicit",
      };
    }
    stale = {
      engine: remembered,
      why: !listed
        ? "it is no longer installed"
        : family.reasons[remembered] || "it can’t start a session here right now",
    };
  }
  const base = family.interactive_default.engine;
  return {
    family,
    engine: base && offered.includes(base) ? base : null,
    reason: stale ? "remembered_stale" : family.interactive_default.reason,
    stale,
    choice: "automatic",
  };
}

/** The tile mark a family's preselected client carries (#1728 mockup): `default` when its API
 *  client is ready, `default here` when the console stands in for an unavailable API client, and
 *  `remembered` for the device's own choice. A one-client family carries none of these. */
export function familyMark(pick: FamilyPick): { word: string; sub?: string } | null {
  if (!pick.engine || pick.family.clients.length < 2) return null;
  switch (pick.reason) {
    case "remembered_explicit":
      return { word: "remembered", sub: "your last choice on this device" };
    case "api_ready":
      return { word: "default" };
    case "api_unready":
    case "remembered_stale":
      return pick.family.interactive_default.reason === "api_unready"
        ? { word: "default here", sub: "used because the API client is unavailable" }
        : { word: "default" };
    default:
      return null;
  }
}

// --- the eligible-default resolver (#1128) --------------------------------------------------------

export type EngineAction = "new" | "handoff";

/** Can `e` be chosen for `action` on this host right now? */
export function eligibleFor(e: EngineInfo, action: EngineAction): boolean {
  if (!isActive(e)) return false;
  if (action === "new") return e.present && e.supports_new;
  return e.capabilities.handoff_target && e.supports_seed_start;
}

export interface DefaultChoice {
  /** The engine to preselect, or null when nothing is eligible. */
  engine: string | null;
  /** The operator's stored default, when it is NOT the one in effect (absent / ineligible). */
  unavailableDefault: string | null;
}

/** THE ONE resolver every picker uses. The stored default wins when it is eligible for `action`;
 *  otherwise the FIRST ELIGIBLE engine in roster order — and `unavailableDefault` says so, so the
 *  fallback is visible. The stored value itself is never rewritten here. */
export function resolveDefault(
  engines: readonly EngineInfo[],
  stored: string | null | undefined,
  action: EngineAction,
): DefaultChoice {
  const eligible = engines.filter((e) => eligibleFor(e, action));
  if (stored && eligible.some((e) => e.id === stored)) {
    return { engine: stored, unavailableDefault: null };
  }
  return {
    engine: eligible[0]?.id ?? null,
    unavailableDefault: stored ? stored : null,
  };
}

// --- the default client: API first (#1739) -------------------------------------------------------
//
// THE ONE rule for which client a new session uses when nothing else has been chosen: a READY API
// client first, otherwise a console (terminal) client. An API client that cannot start is never
// dropped: it comes back with its reason. The pairing of an API client with its console agent is
// the API client's own manifest (`api.source`), never a list kept here. #1728's server resolver
// (`default_client` / `families`) replaces this helper when it lands; there is never a second rule.

/** An API client that cannot start a session right now, and why. */
export interface UnreadyClient {
  id: string;
  label: string;
  reason: string;
}

export interface ClientChoices {
  /** Ready API clients, in roster order. */
  api: string[];
  /** Ready console clients (terminal sessions), in roster order. */
  terminal: string[];
  /** API clients that cannot start, each with its reason (#1311: absent is explained, never silent). */
  unready: UnreadyClient[];
  /** The client to preselect: the first ready API client, else the first console one, else null. */
  preselect: string | null;
}

/** The API client that drives console agent `source`, when the roster has one. */
export function apiClientOf(
  engines: readonly EngineInfo[],
  source: string,
): EngineInfo | undefined {
  return engines.find(
    (e) => e.runtime === "api" && !!e.api?.source && e.api.source === source && e.id !== source,
  );
}

/** Split the startable engines into API-first choices. `startable` is the server's answer
 *  (`/api/config.new_session_engines`); `unavailable` its list of API clients that cannot start
 *  (`unavailable_clients`). Chat clients (an endpoint, not an agent CLI) are neither group. */
export function clientChoices(
  engines: readonly EngineInfo[],
  startable: readonly string[],
  unavailable: readonly UnreadyClient[] = [],
): ClientChoices {
  const can = new Set(startable);
  const ready = (e: EngineInfo) => can.has(e.id) && eligibleFor(e, "new");
  const api = engines.filter((e) => e.runtime === "api" && ready(e)).map((e) => e.id);
  const terminal = engines.filter((e) => e.runtime === "pty" && ready(e)).map((e) => e.id);
  const unready: UnreadyClient[] = [];
  const seen = new Set(api);
  for (const c of unavailable) {
    if (seen.has(c.id)) continue;
    seen.add(c.id);
    unready.push({ id: c.id, label: c.label, reason: c.reason });
  }
  for (const e of engines) {
    if (e.runtime !== "api" || seen.has(e.id) || !isActive(e)) continue;
    seen.add(e.id);
    unready.push({ id: e.id, label: e.label, reason: e.api?.unavailable_reason || "not ready" });
  }
  return { api, terminal, unready, preselect: api[0] ?? terminal[0] ?? null };
}
