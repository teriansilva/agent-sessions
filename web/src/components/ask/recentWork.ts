import type { RecentWorkEntry } from "../../types/api";

/** How many entries the Ask page previews: the LATEST ones, shown oldest first (review 72959). */
export const PREVIEW = 4;

/** The latest `n` entries, in chronological order. */
export function previewEntries(entries: RecentWorkEntry[], n = PREVIEW): RecentWorkEntry[] {
  return [...entries].sort((a, b) => a.ts - b.ts).slice(-n);
}
