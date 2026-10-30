/** Share bundles (#1714): the shapes `/api/share/*` answers. */

export type ShareKind = "skill" | "playbook" | "specialist";

/** One exportable local item and the items it needs (`"<kind>:<id>"` keys). */
export interface ShareCatalogItem {
  kind: string;
  id: string;
  title: string;
  depends_on: string[];
  exportable: boolean;
  reason: string | null;
  source: string;
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
}

export interface ShareResult {
  token: string;
  bundle: string;
  repreview: "expired" | "changed" | "evicted" | null;
  items: ShareResultItem[];
}
