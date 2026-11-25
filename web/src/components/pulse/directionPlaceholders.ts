/** Reading the server's placeholder table, and inserting from it (#983 P2).
 *
 *  The table itself is never kept here. `GET /api/config` ships it as `mission_probes.placeholders`,
 *  generated from `mission_directions.PLACEHOLDERS`, the same table the renderer and the save
 *  validation use; these helpers only read it. `tests/fixtures/direction_placeholders.json` is pinned
 *  to the server's output by pytest and drives the unit tests here, so the two cannot drift. */
import type { DirectionPlaceholder } from "../../types/api";

/** The placeholders an objective checked by `probe` can fill, in the server's table order. */
export function placeholdersFor(
  table: readonly DirectionPlaceholder[] | null | undefined,
  probe: string,
): DirectionPlaceholder[] {
  if (!Array.isArray(table) || !probe) return [];
  return table.filter(
    (p) =>
      p &&
      typeof p.name === "string" &&
      Array.isArray(p.probes) &&
      p.probes.includes(probe),
  );
}

/** `{name}` typed over the selection `[start, end)` of `text`, and where the caret lands after it.
 *  Out-of-range offsets are clamped, so a stale selection can never throw or splice past the end. */
export function insertPlaceholder(
  text: string,
  start: number,
  end: number,
  name: string,
): { text: string; caret: number } {
  const token = `{${name}}`;
  const s = Math.max(0, Math.min(Number.isFinite(start) ? start : text.length, text.length));
  const e = Math.max(s, Math.min(Number.isFinite(end) ? end : s, text.length));
  return { text: text.slice(0, s) + token + text.slice(e), caret: s + token.length };
}

/** A checked fact as its chip reads: `PR #412`, `checks failure`, `fix/upload-retry`. */
export function factLabel(name: string, value: unknown): string {
  const v = String(value);
  switch (name) {
    case "pr":
      return `PR #${v}`;
    case "pr_state":
      return `PR ${v}`;
    case "checks":
      return `checks ${v}`;
    case "review":
      return `review ${v}`;
    default:
      return v;
  }
}
