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
