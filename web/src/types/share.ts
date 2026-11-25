/** Share bundles (#1714): the shapes `/api/share/*` answers. */

export type ShareKind = "skill" | "playbook" | "specialist" | "template" | "variable" | "memory" | "mission-plan";

/** What a project's memory (or a specialist's notebook in it) would send (#1714 P4): counts only. */
export interface ShareMemoryCounts {
  notes: number;
  withheld: number;
  quarantined: number;
}

/** One exportable local item and the items it needs (`"<kind>:<id>"` keys). */
export interface ShareCatalogItem {
  kind: string;
  id: string;
  title: string;
  depends_on: string[];
  exportable: boolean;
  reason: string | null;
  source: string;
  /** What travels, in words, when it is less than the item (#1714 P3: a secret's definition). */
  note?: string | null;
  /** A variable whose value never leaves the install (a secret or a secret file, #1714 P3). */
  secret?: boolean;
  /** A `memory` or `notebook` row's project (#1714 P4): local only, never in a file. */
  project?: { id: string; name: string };
  /** How many notes would travel and how many stay home; `null` when it can't be told. */
  memory?: ShareMemoryCounts | null;
}

/** Copies created from a file: `"<kind>:<id>" → {bundle digest, revision}`. */
export type ShareImports = Record<string, { bundle: string | null; revision: string | null }>;

export interface ShareCatalog {
  items: ShareCatalogItem[];
  imports: ShareImports;
}

export interface SharePreviewFile {
  path: string;
  size: number;
  text?: string;
  binary?: boolean;
  omitted?: boolean;
  /** A refused item's file: its text is never sent (#1714 P3 review), only its path and size. */
  withheld?: boolean;
}

export interface SharePreviewItem {
  key: string;
  kind: string;
  id: string;
  title: string;
  dest: string | null;
  /** `reused`: a local copy imported earlier with exactly this content, unchanged since — used
   *  as it is, never copied again, and needing no selection (#1714 P2). */
  status: "new" | "renamed" | "refused" | "reused";
  reason: string | null;
  depends_on: string[];
  uses: { from: string; to: string }[];
  files: SharePreviewFile[];
  /** A specialist's job and what runs it here (#1714 P2). */
  specialist?: SharePreviewSpecialist;
  /** A variable definition (#1714 P3): `arrives_empty` when the file carries no value for it. */
  variable?: { kind: string; value_type: string; arrives_empty: boolean };
  /** A template (#1714 P3): its field and image counts and the library variables it uses. */
  template?: { fields: number; images: number; variables: string[] };
  /** A project's notes (#1714 P4), each judged here: what its import would do. */
  memory?: ShareNotesView;
  /** A specialist's notes in that project (#1714 P4), judged the same way. */
  notebook?: ShareNotesView;
  /** A mission plan (#1924): its counts, never a project. */
  "mission-plan"?: { objectives: number; slots: number; steps: number };
}

/** One incoming note as the preview judged it. A held or refused note's own text is not sent. */
export interface ShareNoteVerdict {
  file: string;
  verdict: "new" | "held" | "refused";
  reason: string | null;
  name?: string;
  type?: string;
  description?: string;
}

export interface ShareNotesView {
  notes: ShareNoteVerdict[];
  counts: { new: number; held: number; refused: number };
}

/** What happened to each note of a container at confirm (#1714 P4). */
export interface ShareNoteOutcome {
  file: string;
  outcome: "created" | "already" | "held" | "refused" | "failed" | "retry" | "pending";
  slug: string | null;
  reason: string | null;
}

export interface ShareNotesResult {
  /** The memory scope the notes went into: a project id, or `<project>~<specialist>`. */
  scope: string | null;
  notes: ShareNoteOutcome[];
  counts: Record<ShareNoteOutcome["outcome"], number>;
}

export interface ShareChoice {
  engine: string;
  model: string;
  effort: string | null;
}

export interface SharePreviewSpecialist {
  title: string;
  summary: string;
  capacity: number;
  skills: string[];
  /** The file's suggestion, never trusted: only `settled` is checked against this install. */
  suggests: { engine: string | null; model: string | null; effort: string | null };
  settled: ShareChoice | null;
  settle_reason: string | null;
  /** Derived from THIS install's model grades for `settled`, never read from the file. */
  grade: string | null;
}

export interface SharePreview {
  token: string;
  expires_at: string;
  bundle: {
    digest: string;
    size: number;
    format: number;
    created_at: string;
    app_version: string;
    source: string | null;
    items: number;
    files: number;
  };
  items: SharePreviewItem[];
}

export type ShareOutcome =
  | "created"
  | "reused"
  | "refused"
  | "failed"
  | "skipped"
  | "not_selected"
  | "stale";

export interface ShareResultItem {
  key: string;
  kind: string;
  id: string;
  title: string;
  status: ShareOutcome;
  dest: string | null;
  reason: string | null;
  /** A failed item a confirm of this same preview may still create. `false` = recorded for good
   *  (a destination taken, a pinned skill that moved): only a new preview gets past it. Absent
   *  from an older server's answer, which is treated as retriable. */
  retry?: boolean;
  /** A memory container's notes, one outcome each (#1714 P4). */
  memory?: ShareNotesResult;
}

export interface ShareResult {
  token: string;
  bundle: string;
  repreview: "expired" | "changed" | "evicted" | null;
  items: ShareResultItem[];
}
