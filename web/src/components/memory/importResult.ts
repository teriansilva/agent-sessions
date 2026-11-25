import type { ImportHeld, ImportOutcome, ImportResult } from "../../types/memory";

/** What each outcome is called, in the order a result reads. A held or refused file is NOT a tray
 *  item: nothing of it is stored, and the fix is made in the agent's own memory (#1471). */
const PHRASES: [ImportOutcome, (n: number) => string][] = [
  ["imported", n => `${n} taken in`],
  ["updated", n => `${n} updated`],
  ["kept_edits", n => `${n} kept your edits`],
  ["held", n => `${n} held back`],
  ["refused", n => `${n} refused`],
  ["left_out", n => `${n} left out — memory is full`],
  ["skipped_deleted", n => `${n} you deleted stay deleted`],
  ["unchanged", n => `${n} unchanged`],
];

/** The most sources one import takes (the server refuses more). The rest go in a second run. */
export const MAX_SOURCES = 16;

export const plural = (n: number, one: string, many = `${one}s`) => `${n} ${n === 1 ? one : many}`;
export const message = (e: unknown) => (e instanceof Error ? e.message : "The request failed. Try again.");
export const labelsOf = (items: { label: string }[]) => [...new Set(items.map(s => s.label).filter(Boolean))].join(" / ") || "the agent";

/** How many of the held-back files belong to the sources THIS import read — never another
 *  source's, which the project-wide list also holds (Hermes on #1484). */
export function heldFor(r: ImportResult, held: ImportHeld[]): number {
  const read = new Set(r.sources.filter(s => s.state === "ok").map(s => s.path));
  return held.filter(h => read.has(h.source)).length;
}

/** One sentence for an import's result: every outcome that happened, never "all moved" when some
 *  did not. ``stillHeld`` is how many files of the sources it read are held back or refused NOW
 *  (`heldFor`): a file that stays held is `unchanged` to a repeat import, but it is still not in
 *  shared memory, so the line says so whenever any remain. */
export function resultLine(r: ImportResult, stillHeld = 0): string {
  const read = r.sources.filter(s => s.state === "ok");
  const files = read.reduce((a, s) => a + (s.files ?? 0), 0);
  const parts = PHRASES.filter(([k]) => r.totals[k]).map(([k, say]) => say(r.totals[k]));
  let text = `${plural(files, "note")} from ${plural(read.length, "place")}: ${parts.join(", ") || "nothing to take in"}.`;
  if (r.unavailable) {
    const why = r.sources.filter(s => s.state !== "ok").map(s => s.reason).filter(Boolean);
    text += ` ${plural(r.unavailable, "place")} couldn't be read${why.length ? ` (${why.join("; ")})` : ""}.`;
  }
  const held = Math.max(stillHeld, r.totals.held + r.totals.refused);
  if (held) {
    text += ` ${plural(held, "file")} ${held === 1 ? "is" : "are"} held back — fix ${held === 1 ? "it" : "them"} in ${labelsOf(read)}'s memory, then import again.`;
  }
  return text;
}
