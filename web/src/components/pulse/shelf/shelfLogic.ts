/** Pure helpers for the mission shelf (#1740), split from the components so they stay
 *  unit-testable without a DOM and the component files export components only. */
import type { ShelfItem, ShelfKind, ShelfSource, ShelfVerdict } from "../../../types/shelf";

export const KIND_ORDER: readonly ShelfKind[] = [
  "image",
  "video",
  "audio",
  "document",
  "code",
  "link",
  "text",
];

export const KIND_LABEL: Record<ShelfKind, string> = {
  image: "Image",
  video: "Video",
  audio: "Audio",
  document: "Document",
  code: "Code",
  link: "Link",
  text: "Text",
};

export type GroupBy = "session" | "none";
export interface ShelfFilters {
  kind: ShelfKind | "all";
  source: ShelfSource | "all";
  unreviewedOnly: boolean;
  group: GroupBy;
}
export const NO_FILTERS: ShelfFilters = {
  kind: "all",
  source: "all",
  unreviewedOnly: false,
  group: "session",
};

export function applyFilters(items: readonly ShelfItem[], f: ShelfFilters): ShelfItem[] {
  return items.filter(
    (i) =>
      (f.kind === "all" || i.kind === f.kind) &&
      (f.source === "all" || i.source === f.source) &&
      (!f.unreviewedOnly || i.verdict === null),
  );
}

export interface ShelfGroup {
  /** The session key, or `null` for items no session delivered. */
  key: string | null;
  items: ShelfItem[];
}

/** Items grouped by the session that produced them, groups in the order their first item
 *  arrived, items keeping the shelf's own order. */
export function groupBySession(items: readonly ShelfItem[]): ShelfGroup[] {
  const groups = new Map<string | null, ShelfItem[]>();
  for (const it of items) {
    const k = it.session_key ?? null;
    const g = groups.get(k);
    if (g) g.push(it);
    else groups.set(k, [it]);
  }
  return Array.from(groups, ([key, its]) => ({ key, items: its }));
}

/** A file item whose bytes cannot be shown at all now (gone, or outside the boundary). */
export function isGone(item: ShelfItem): boolean {
  const s = item.file?.state;
  return s === "missing" || s === "inaccessible";
}

export function isChanged(item: ShelfItem): boolean {
  return item.file?.state === "changed";
}

/** Can the browser be handed this item's bytes inline? (A media or text document, still there.) */
export function isInlineMedia(item: ShelfItem): boolean {
  return (
    item.ref_type === "path" &&
    !!item.mime &&
    !isGone(item) &&
    (item.kind === "image" || item.kind === "video" || item.kind === "audio")
  );
}

export function isTextDocument(item: ShelfItem): boolean {
  return item.ref_type === "path" && item.kind === "document" && !!item.mime && !isGone(item);
}

export function engineOf(key: string | null): string {
  if (!key) return "";
  const i = key.indexOf(":");
  return i > 0 ? key.slice(0, i) : "";
}

export function fmtBytes(n: number | null | undefined): string {
  if (n === null || n === undefined || !Number.isFinite(n)) return "—";
  if (n < 1024) return `${n} B`;
  if (n < 1024 * 1024) return `${(n / 1024).toFixed(n < 10 * 1024 ? 1 : 0)} KB`;
  if (n < 1024 * 1024 * 1024) return `${(n / (1024 * 1024)).toFixed(1)} MB`;
  return `${(n / (1024 * 1024 * 1024)).toFixed(1)} GB`;
}

/** `mm:ss` (or `h:mm:ss`) for a media duration in seconds; `""` when unknown. */
export function fmtDuration(s: number | null | undefined): string {
  if (s === null || s === undefined || !Number.isFinite(s) || s < 0) return "";
  const total = Math.round(s);
  const h = Math.floor(total / 3600);
  const m = Math.floor((total % 3600) / 60);
  const sec = total % 60;
  const mm = String(m).padStart(2, "0");
  const ss = String(sec).padStart(2, "0");
  return h ? `${h}:${mm}:${ss}` : `${mm}:${ss}`;
}

/** Local wall-clock `HH:MM` for an epoch-seconds timestamp. */
export function fmtClock(epochS: number | null | undefined): string {
  if (!epochS) return "";
  const d = new Date(epochS * 1000);
  return `${String(d.getHours()).padStart(2, "0")}:${String(d.getMinutes()).padStart(2, "0")}`;
}

export function shortSha(sha: string | null | undefined): string {
  if (!sha) return "—";
  return sha.length > 12 ? `${sha.slice(0, 4)}…${sha.slice(-4)}` : sha;
}

const DOC_TYPES: Record<string, string> = {
  md: "Markdown",
  markdown: "Markdown",
  txt: "Text",
  text: "Text",
  log: "Log",
  csv: "CSV",
  tsv: "TSV",
  json: "JSON",
  jsonl: "JSON lines",
  ndjson: "JSON lines",
  yaml: "YAML",
  yml: "YAML",
  toml: "TOML",
  pdf: "PDF",
  svg: "SVG",
  diff: "Diff",
  patch: "Patch",
};

export function extOf(name: string | undefined): string {
  const n = name ?? "";
  const i = n.lastIndexOf(".");
  return i > 0 && i < n.length - 1 ? n.slice(i + 1).toLowerCase() : "";
}

export function docType(name: string | undefined): string {
  const e = extOf(name);
  return DOC_TYPES[e] ?? (e ? e.toUpperCase() : "File");
}

/** What the tile's foot says about the item: a verdict (or Unreviewed), or the file state when
 *  the file cannot be reviewed at all. The word is always there; the colour only repeats it. */
export function footState(item: ShelfItem): {
  word: string;
  led: "up" | "attention" | "idle";
  tone: "ok" | "warn" | "idle";
} {
  if (item.file?.state === "missing") return { word: "Missing", led: "idle", tone: "idle" };
  if (item.file?.state === "inaccessible")
    return { word: "Not accessible", led: "idle", tone: "idle" };
  return verdictState(item.verdict);
}

export function verdictState(v: ShelfVerdict | null): {
  word: string;
  led: "up" | "attention" | "idle";
  tone: "ok" | "warn" | "idle";
} {
  if (v === "good") return { word: "Looks good", led: "up", tone: "ok" };
  if (v === "needs_work") return { word: "Needs work", led: "attention", tone: "warn" };
  return { word: "Unreviewed", led: "idle", tone: "idle" };
}

/** The `revision` + item list the page holds, after a verdict answer: the item replaced in place
 *  and the revision moved — never a reload's worth of state for one change. */
export function withItem(items: readonly ShelfItem[], next: ShelfItem): ShelfItem[] {
  return items.map((i) => (i.id === next.id ? { ...i, ...next, file: i.file } : i));
}

/** The shelf tab's own label numbers. */
export function tabCounts(items: readonly ShelfItem[]): { total: number; toReview: number } {
  return { total: items.length, toReview: items.filter((i) => i.verdict === null).length };
}

/** Default view for a mission: review and closed open on the shelf, everything else on the
 *  thread (#1740 design decision 1). */
export function defaultView(state: string | undefined): "thread" | "shelf" {
  return state === "review" || state === "done" || state === "failed" || state === "abandoned"
    ? "shelf"
    : "thread";
}
