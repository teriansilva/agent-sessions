/** One status for every agent surface (#1408): the roster row, its mode chips, the counts, the
 *  filters and the agent page header all read THIS presenter, so they cannot disagree.
 *
 *  Status comes from three sources — the live roster (`/api/engines`), the installer catalog
 *  (`/api/plugins`: install rows, catalog entries, pending operations) and the roster's
 *  `problems` (invalid manifests) — ranked by the precedence matrix in #1408, worst first:
 *
 *    1 Invalid · 2 Retiring · 3 Setting up · 4 Disabled · 5 Needs setup ·
 *    6 Not installed (console) / Unavailable (API) · 7 Update available · 8 Ready
 *
 *  Nothing here names an engine: grouping reads the manifest's `api.source`. */
import { isActive } from "../../app/engineRoster";
import type { EngineInfo, EngineProblem } from "../../types/api";
import type { PluginCatalog, PluginInstallation } from "../../types/plugins";
import { pendingOperation, setupPath } from "../plugins/model";

/** An LED tone (docs/design.md §3): up = working, deg = needs the operator, down = broken,
 *  idle = neither (disabled, retiring). */
export type Tone = "up" | "idle" | "deg" | "down";

export interface StatusAction {
  label: string;
  to: string;
}

export interface Status {
  /** 1 (worst) … 8 (Ready): the matrix order. */
  rank: number;
  word: string;
  tone: Tone;
  /** The one inline action a row or header offers, when the operator is needed. */
  action: StatusAction | null;
  /** Why, when the word alone is not enough (retiring / unavailable reason). */
  reason: string | null;
}

export const READY_RANK = 8;

const status = (
  rank: number,
  word: string,
  tone: Tone,
  action: StatusAction | null = null,
  reason: string | null = null,
): Status => ({ rank, word, tone, action, reason });

/** The installer's view of one id, shared by live and saved-only agents. */
function installer(id: string, data: PluginCatalog | null) {
  const row: PluginInstallation | undefined = data?.plugins.find((p) => p.id === id);
  const candidate = row?.generations?.find((g) => g.id === (row.candidate ?? row.active));
  const active = row?.generations?.find((g) => g.id === row.active);
  const entry = data?.catalog.find((c) => c.manifest.identity.id === id);
  const operation = data?.operations
    .filter((o) => o.plugin_id === id && pendingOperation(o))
    .sort((x, y) => y.created_at - x.created_at)[0];
  return { row, candidate, active, entry, operation };
}

/** The installer half of the matrix (3–5, 7), or null when it has nothing to say. */
function installStatus(id: string, data: PluginCatalog | null): Status | null {
  const { row, candidate, active, entry, operation } = installer(id, data);
  if (operation) {
    return status(3, "Setting up", "deg", {
      label: "View operation",
      to: setupPath({ operation: operation.id }),
    });
  }
  if (row && row.enabled === false && (!candidate || row.candidate === row.active)) {
    // The way back is setup's Enable step for that generation (#1408 matrix row 4).
    return status(
      4,
      "Disabled",
      "idle",
      candidate ? { label: "Enable", to: setupPath({ plugin: id, generation: candidate.id }) } : null,
    );
  }
  if (row && candidate && (!row.enabled || row.candidate !== row.active)) {
    return status(5, "Needs setup", "deg", {
      label: "Continue setup",
      to: setupPath({ plugin: id, generation: candidate.id }),
    });
  }
  if (row && !candidate && !row.enabled) {
    return status(5, "Needs setup", "deg", { label: "Set up", to: setupPath({ plugin: id }) });
  }
  if (entry && entry.installable !== false && active && entry.digest !== active.review.recipe_digest) {
    return status(7, "Update available", "deg", {
      label: "Review update",
      to: setupPath({ plugin: id }),
    });
  }
  return null;
}

/** One LIVE engine's status. */
export function engineStatus(e: EngineInfo, data: PluginCatalog | null): Status {
  if (!isActive(e)) {
    return status(
      2,
      "Retiring",
      "idle",
      null,
      e.status_reason ||
        "This agent is being removed; its running sessions stay attachable until they exit.",
    );
  }
  const install = installStatus(e.id, data);
  if (install && install.rank < 6) return install;
  if (!e.present) {
    return e.runtime === "api"
      ? status(6, "Unavailable", "deg", null, e.api?.unavailable_reason ?? "not ready")
      : status(6, "Not installed", "deg", null, "No binary found on this host.");
  }
  return install ?? status(READY_RANK, "Ready", "up");
}

/** An agent the installer knows but the live roster does not (saved, disabled, mid-setup). */
export function savedStatus(id: string, data: PluginCatalog | null): Status {
  return (
    installStatus(id, data) ??
    status(5, "Needs setup", "deg", { label: "Set up", to: setupPath({ plugin: id }) })
  );
}

export const PROBLEM_STATUS = status(1, "Invalid", "down");

export interface Mode {
  engine: EngineInfo;
  /** TERMINAL / API / CHAT — what kind of session this mode starts. */
  label: string;
  status: Status;
}

export type RosterRow =
  | { kind: "agent"; id: string; label: string; modes: Mode[]; status: Status }
  | { kind: "saved"; id: string; label: string; status: Status }
  | { kind: "problem"; id: string; label: string; problem: EngineProblem; status: Status };

const modeLabel = (e: EngineInfo) =>
  e.runtime === "pty" ? "Terminal" : e.runtime === "api" ? "API" : e.runtime === "chat" ? "Chat" : e.runtime;

/** The directory name of an invalid manifest, from the path the loader reported. */
export function problemName(source: string): string {
  const parts = source.split("/").filter(Boolean);
  const i = parts.lastIndexOf("plugin.toml");
  return i > 0 ? parts[i - 1] : source;
}

/** The roster as rows: one per CLI. An API client whose source is in the roster becomes a mode
 *  of the source's row; one whose source is not keeps a row of its own, so a removed source can
 *  never hide it. A row's status is its WORST mode's (never an aggregate word). */
export function buildRows(
  engines: readonly EngineInfo[],
  problems: readonly EngineProblem[],
  data: PluginCatalog | null,
): RosterRow[] {
  const ids = new Set(engines.map((e) => e.id));
  const grouped = (e: EngineInfo) =>
    e.runtime === "api" && !!e.api?.source && e.api.source !== e.id && ids.has(e.api.source);
  const rows: RosterRow[] = [];
  const byId = new Map<string, Extract<RosterRow, { kind: "agent" }>>();
  for (const e of engines) {
    if (grouped(e)) continue;
    const row = {
      kind: "agent" as const,
      id: e.id,
      label: e.label,
      modes: [{ engine: e, label: modeLabel(e), status: engineStatus(e, data) }],
      status: engineStatus(e, data),
    };
    byId.set(e.id, row);
    rows.push(row);
  }
  for (const e of engines) {
    if (!grouped(e)) continue;
    const host = byId.get(e.api!.source)!;
    const mode = { engine: e, label: modeLabel(e), status: engineStatus(e, data) };
    host.modes.push(mode);
    if (mode.status.rank < host.status.rank) host.status = mode.status;
  }
  const saved = new Set(data?.plugins.map((p) => p.id) ?? []);
  for (const id of saved) {
    if (ids.has(id)) continue;
    const { row, candidate, entry } = installer(id, data);
    const manifest = candidate?.review.entry.manifest ?? entry?.manifest;
    rows.push({
      kind: "saved",
      id,
      label: manifest?.identity.label ?? id,
      status: row?.error ? status(5, "Needs setup", "deg", null, row.error) : savedStatus(id, data),
    });
  }
  for (const p of problems) {
    rows.push({ kind: "problem", id: `problem:${p.source}`, label: problemName(p.source), problem: p, status: PROBLEM_STATUS });
  }
  return rows;
}

/** Catalog agents that are neither live nor saved: the "Also in the catalog" line, never rows. */
export function catalogOnly(engines: readonly EngineInfo[], data: PluginCatalog | null) {
  const taken = new Set([...engines.map((e) => e.id), ...(data?.plugins.map((p) => p.id) ?? [])]);
  return (data?.catalog ?? []).filter(
    (c) => !taken.has(c.manifest.identity.id) && c.installable !== false && !c.manifest.api,
  );
}

export type RosterFilter = "All" | "Ready" | "Needs attention";

export const needsAttention = (row: RosterRow) => row.status.rank < READY_RANK;

export function rowMatches(row: RosterRow, query: string, filter: RosterFilter): boolean {
  const q = query.trim().toLowerCase();
  const hay = [row.label, row.id, ...(row.kind === "agent" ? row.modes.map((m) => `${m.engine.id} ${m.engine.label}`) : [])]
    .join(" ")
    .toLowerCase();
  if (q && !hay.includes(q)) return false;
  if (filter === "Ready") return row.status.rank === READY_RANK;
  if (filter === "Needs attention") return needsAttention(row);
  return true;
}
