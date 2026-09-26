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
import type { EngineInfo, EngineProblem } from "../types/api";

export type RosterStatus = "loading" | "ready" | "failed";

export interface RosterSnapshot {
  status: RosterStatus;
  /** True once ANY roster has loaded — stays true through a failed refresh (stale kept). */
  loaded: boolean;
  engines: readonly EngineInfo[];
  problems: readonly EngineProblem[];
}

const EMPTY: RosterSnapshot = {
  status: "loading",
  loaded: false,
  engines: [],
  problems: [],
};

let snapshot: RosterSnapshot = EMPTY;
let byId = new Map<string, EngineInfo>();
const listeners = new Set<() => void>();

function publish(next: RosterSnapshot): void {
  snapshot = next;
  byId = new Map(next.engines.map((e) => [e.id, e]));
  for (const fn of listeners) fn();
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

/** Install a roster (the provider does this; tests do it directly). */
export function setRoster(
  engines: readonly EngineInfo[],
  problems: readonly EngineProblem[] = [],
): void {
  const ordered = (engines as unknown[])
    .map(normalize)
    .filter((e): e is EngineInfo => e !== null)
    .sort(
    (a, b) => (a.display?.order ?? 500) - (b.display?.order ?? 500),
  );
  publish({ status: "ready", loaded: true, engines: ordered, problems });
}

/** A refresh failed. The last good roster is KEPT — a stale roster is never replaced by none. */
export function markRosterFailed(): void {
  // A retry that fails AGAIN changes nothing, so it publishes nothing: a fresh snapshot every 15 s
  // would rebuild every subscriber's memo — on the map, the whole node array, which React Flow
  // then hides until re-measured (the #936 flicker).
  if (snapshot.status === "failed") return;
  publish({ ...snapshot, status: "failed" });
}

/** Back to "nothing loaded" — tests only. */
export function resetRoster(): void {
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
export function wipesOnRepaint(id: string): boolean {
  return byId.get(id)?.terminal.repaint === "wipe";
}

/** The native-id prefix to strip for display (`ses_`, `session_`), or "". */
export function idPrefix(id: string): string {
  return byId.get(id)?.display.id_prefix ?? "";
}

/** `retiring` engines (#1126 PR B) are attach-only; a missing status is active. */
export function isActive(e: EngineInfo): boolean {
  return (e.status ?? "active") === "active";
}

/** Does a session of this engine run in a terminal (#853 §7)? Unknown ⇒ undefined (loading). */
export function runsInTerminal(id: string): boolean | undefined {
  const runtime = byId.get(id)?.runtime;
  return runtime === undefined || runtime === "unknown" ? undefined : runtime === "pty";
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
