/** Unified-diff parser (#784).
 *
 * ~80 lines instead of a dependency: syntax highlighting is the thing that would need one, and it
 * is out of scope. The output is the shape the viewer renders — hunks of typed lines carrying the
 * OLD and NEW numbers, because a diff without both gutters is much harder to read against the file
 * it came from.
 */

export type DiffLineKind = "context" | "add" | "del" | "meta" | "nonewline";

export interface DiffLine {
  kind: DiffLineKind;
  text: string;
  /** Line number on the old side, or null for an addition. */
  oldNo: number | null;
  /** Line number on the new side, or null for a deletion. */
  newNo: number | null;
}

export interface DiffHunk {
  header: string;
  lines: DiffLine[];
}

const HUNK = /^@@ -(\d+)(?:,(\d+))? \+(\d+)(?:,(\d+))? @@(.*)$/;

/** Parse a unified diff into hunks. Unknown/absent input yields an empty list rather than throwing
 *  — the viewer has an honest empty state and a parser is not the place to decide there is a bug. */
export function parseDiff(text: string): DiffHunk[] {
  if (!text) return [];
  const hunks: DiffHunk[] = [];
  let cur: DiffHunk | null = null;
  let oldNo = 0;
  let newNo = 0;

  for (const raw of text.split("\n")) {
    const m = HUNK.exec(raw);
    if (m) {
      oldNo = Number(m[1]);
      newNo = Number(m[3]);
      cur = { header: raw, lines: [] };
      hunks.push(cur);
      continue;
    }
    if (!cur) {
      // `---` / `+++` file headers before the first hunk carry no line information.
      continue;
    }
    if (raw.startsWith("\\")) {
      // "\ No newline at end of file" belongs to the preceding line and numbers nothing.
      cur.lines.push({ kind: "nonewline", text: raw, oldNo: null, newNo: null });
      continue;
    }
    const marker = raw[0];
    const body = raw.slice(1);
    if (marker === "+") {
      cur.lines.push({ kind: "add", text: body, oldNo: null, newNo: newNo++ });
    } else if (marker === "-") {
      cur.lines.push({ kind: "del", text: body, oldNo: oldNo++, newNo: null });
    } else if (marker === " " || raw === "") {
      // A context line whose content is empty arrives as "" rather than " ".
      cur.lines.push({ kind: "context", text: body, oldNo: oldNo++, newNo: newNo++ });
    } else {
      cur.lines.push({ kind: "meta", text: raw, oldNo: null, newNo: null });
    }
  }
  return hunks;
}

/** Added/removed counts for a diff we parsed ourselves. Only meaningful for a COMPLETE diff — a
 *  truncated one has its counts withheld by the server precisely because a prefix count is not a
 *  total, so callers should prefer the server's numbers and treat null as "unknown". */
export function countChanges(hunks: DiffHunk[]): { added: number; removed: number } {
  let added = 0;
  let removed = 0;
  for (const h of hunks) {
    for (const l of h.lines) {
      if (l.kind === "add") added++;
      else if (l.kind === "del") removed++;
    }
  }
  return { added, removed };
}
