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
  // code items (found in git)
  meta?: Record<string, unknown> | null;
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
}
