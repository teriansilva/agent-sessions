/** Shared project memory (#1441) — the shapes `/api/memory` returns. */

export type NoteType = "decision" | "finding" | "pitfall" | "reference";

/** Who wrote something — a label, not authority (any same-user process can write any inbox). */
export interface MemorySession {
  /** The session key when the app knows which session it was; "" when unresolved. */
  key: string;
  engine: string;
  /** "terminal" | "api" (the launch path that minted the inbox). */
  kind: string;
  resolved: boolean;
}

export interface MemoryNote {
  slug: string;
  name: string;
  description: string;
  type: NoteType;
  body: string;
  size: number;
  created: number;
  updated: number;
  session: MemorySession;
  /** Slugs of other notes this one links to with `[[slug]]`. */
  links: string[];
}

export interface MemoryPending {
  filename: string;
  size: number;
  at: number;
  session: MemorySession;
}

export interface MemoryRefused extends MemoryPending {
  reason: string;
}

export interface MemoryQuarantined extends MemoryRefused {
  id: number;
  /** Always the whole-item mask: nothing of a quarantined item's content is ever returned. */
  masked: string;
}

export interface MemoryView {
  project: { id: string; name: string; color: string };
  enabled: boolean;
  notes: MemoryNote[];
  pending: MemoryPending[];
  refused: MemoryRefused[];
  quarantine: MemoryQuarantined[];
  /** Notes held back from the page because they hold a value now stored as a secret, or because
   *  the secret check could not run. */
  withheld: { notes: number; reason: string };
  instructions: string;
  limits: { note_bytes: number; notes: number; inbox_files: number };
}

export interface MemoryProject {
  id: string;
  name: string;
  color: string;
  enabled: boolean;
  notes: number;
  pending: number;
  quarantined: number;
  last_write: number | null;
}

export type NoteEdit = Partial<Pick<MemoryNote, "name" | "description" | "type" | "body">>;

/** An agent's own memory store found for a project's folders (#1471) — read only. */
export interface ImportSource {
  /** Opaque: what the import is asked for. A path is never sent back. */
  id: string;
  engine: string;
  label: string;
  /** For display only. */
  path: string;
  relation: "this folder" | "a subfolder session" | "";
  files: number;
  /** Longer than an agent note, still importable (up to the operator ceiling). */
  over_agent_size: number;
  /** Over the import limit: refused. */
  over_cap: number;
  beyond_max_files: number;
  state: "ok" | "unavailable";
  reason: string;
}

export const IMPORT_OUTCOMES = [
  "imported",
  "updated",
  "unchanged",
  "kept_edits",
  "skipped_deleted",
  "held",
  "refused",
  "left_out",
] as const;
export type ImportOutcome = (typeof IMPORT_OUTCOMES)[number];

export interface ImportSourceResult extends Partial<Record<ImportOutcome, number>> {
  id: string;
  label: string;
  path: string;
  relation: string;
  state: "ok" | "unavailable";
  reason?: string;
  files?: number;
}

export interface ImportResult {
  at: number;
  sources: ImportSourceResult[];
  totals: Record<ImportOutcome, number>;
  unavailable: number;
}

/** A source file the import did not take in. Metadata only: its text is never stored, and it is
 *  not a quarantine item — the fix is made in the agent's own memory, then imported again. */
export interface ImportHeld {
  source: string;
  /** Masked like any tray name when the name itself is withheld. */
  file: string;
  reason: string;
  refused: boolean;
  at: number;
}

export interface ImportPreview {
  sources: ImportSource[];
  last: ImportResult | null;
  held: ImportHeld[];
}
