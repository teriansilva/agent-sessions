/** The mission shelf (#1740): what a mission produced, as the server's manifest describes it.
 *  Every field is server-owned; agent-written strings (`title`, `why`, `text`) are untrusted and
 *  are only ever rendered as text. */

export type ShelfKind = "image" | "video" | "audio" | "document" | "code" | "link" | "text";
export type ShelfSource = "delivered" | "found";
export type ShelfVerdict = "good" | "needs_work";
/** A file item's state, re-checked on every read. */
export type ShelfFileState = "ok" | "changed" | "missing" | "inaccessible";

export interface ShelfFile {
  state: ShelfFileState;
  /** `changed` only: the sha256 and mtime (epoch seconds) of what is on disk now. */
  disk_sha256?: string | null;
  disk_mtime?: number;
}

export interface ShelfItem {
  id: string;
  seq: number;
  source: ShelfSource;
  kind: ShelfKind;
  title: string;
  why: string;
  session_key: string | null;
  step_id?: string | null;
  ref_type: "path" | "url" | "text" | "code";
  size: number | null;
  sha256: string | null;
  created_at: number;
  verdict: ShelfVerdict | null;
  note: string;
  verdict_at: number | null;
  // path items
  path?: string;
  name?: string;
  /** The type the server serves it inline as; `null` = download only. */
  mime?: string | null;
  viewable?: boolean;
  file?: ShelfFile;
  // url items
  url?: string;
  host?: string;
  url_path?: string;
  // text items
  text?: string;
  /** Every session whose window covered the item (a found item can name several). */
  sessions?: string[];
  // code items (found in git)
  meta?: ShelfCodeMeta | null;
}

export interface ShelfCodeFile {
  path: string;
  /** A added, M modified, D deleted. */
  status: string;
  add: number | null;
  del: number | null;
  from: "commit" | "worktree";
  sha?: string;
  worktree_too?: boolean;
}

export interface ShelfCodeMeta {
  repo: string;
  repo_name: string;
  branch: string | null;
  base: string | null;
  head: string | null;
  commits: { sha: string; subject: string }[];
  commits_more: boolean;
  files: ShelfCodeFile[];
  files_more: number;
  add: number;
  del: number;
  pr: { number: number; state: string; url: string; merged: boolean } | null;
  late: boolean;
  sessions: string[];
}

export interface ShelfDiffFile extends ShelfCodeFile {
  patch: string;
  truncated: boolean;
  unavailable: boolean;
}

export interface ShelfCounts {
  total: number;
  by_kind: Record<ShelfKind, number>;
  by_source: Record<ShelfSource, number>;
  unreviewed: number;
  file_checks: { changed: number; missing: number; inaccessible: number };
}

export interface ShelfManifest {
  mission_id: string;
  revision: number;
  counts: ShelfCounts;
  items: ShelfItem[];
  next_cursor: string | null;
  /** Sessions whose found-in-git items are still to come (collected when the session ends). */
  found_pending?: string[];
}

/** The mission debrief (#1740 P2): the facts read now from the records, and the stored summary
 *  with the records revision it describes. */
export interface DebriefEvidence {
  source: string;
  quote: string;
}

export interface DebriefObjective {
  key: string;
  title: string;
  state: string;
  met: boolean;
  judged: {
    met: boolean;
    confidence: number | null;
    evidence: DebriefEvidence[];
    checked_at: number | null;
  } | null;
}

export interface DebriefItem {
  id: string;
  kind: ShelfKind;
  source: ShelfSource;
  title: string;
  session_key: string | null;
  note?: string;
  commits?: number;
  files?: number;
  pr?: number;
  repo_name?: string;
}

export interface DebriefFacts {
  mission_id: string;
  title: string;
  state: string;
  asked: string;
  sessions: {
    key: string;
    role: string | null;
    added_at: number | null;
    removed_at: number | null;
  }[];
  objectives: DebriefObjective[];
  shelf: {
    counts: ShelfCounts;
    groups: {
      session_key: string | null;
      total: number;
      by_kind: Partial<Record<ShelfKind, number>>;
    }[];
  };
  open: {
    objectives: Pick<DebriefObjective, "key" | "title" | "state" | "judged">[];
    objectives_total: number;
    needs_work: DebriefItem[];
    needs_work_total: number;
    found_not_delivered: DebriefItem[];
    found_not_delivered_total: number;
  };
  integrity: {
    changed: { id: string; title: string }[];
    missing: { id: string; title: string }[];
  };
  revision: string;
}

export interface Debrief {
  mission_id: string;
  state: string;
  configured: boolean;
  facts: DebriefFacts;
  summary: { text: string; revision: string; written_at: number } | null;
  /** The summary describes the records as they are now. */
  fresh: boolean;
  generating: boolean;
  error: string | null;
}
