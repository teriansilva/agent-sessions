/** Grades (#1613 D18): a model tier, shown under job-like names. Derived, never typed. */
import { useSyncExternalStore } from "react";
import { useConfig } from "../../app/config";
import type { Grade } from "../../types/specialists";

export const GRADES: readonly Grade[] = ["fast", "strong", "frontier"];
export const GRADE_LABEL: Record<Grade, string> = {
  fast: "Intern",
  strong: "Specialist",
  frontier: "Senior",
};
export const GRADE_HINT: Record<Grade, string> = {
  fast: "Fast, low-cost models: sorting, filing, renaming.",
  strong: "Strong models: implementing, matching, writing.",
  frontier: "Frontier models: planning, review, security-sensitive work.",
};

export type ModelGrades = Record<string, Record<string, Grade>>;

/** The grade of `model` on `engine` in the operator's map; `null` = ungraded (incl. `default`). */
export function gradeOf(
  map: ModelGrades | undefined,
  engine: string,
  model: string,
): Grade | null {
  if (!engine || !model || model === "default") return null;
  return map?.[engine]?.[model] ?? null;
}

// ---- the newest committed map -------------------------------------------------------------------
//
// The server bumps a revision on every grade commit (`model_grades_rev`), and both `/api/config`
// and every save's answer carry it. The app's config is refreshed by a reload, which can fail; a
// save's answer can arrive after a later save's; two saves of one row can commit in either order.
// One rule is right for all of those: show the map with the HIGHEST revision seen. A save's answer
// is kept here; the config wins as soon as it is at least as new. Every consumer of grades in the
// SPA (the grade table, the editor's grade line) reads through `useModelGrades`.

let newest: { rev: number; map: ModelGrades } | null = null;
const listeners = new Set<() => void>();

/** A save's answer: the map the server committed at revision `rev`. Older answers are ignored. */
export function confirmGrades(map: ModelGrades, rev: number): void {
  if (newest && newest.rev >= rev) return;
  newest = { rev, map };
  for (const l of listeners) l();
}

function subscribe(l: () => void): () => void {
  listeners.add(l);
  return () => listeners.delete(l);
}

export function useModelGrades(): ModelGrades | undefined {
  const config = useConfig();
  const snap = useSyncExternalStore(subscribe, () => newest);
  if (snap && snap.rev > (config?.model_grades_rev ?? 0)) return snap.map;
  return config?.model_grades;
}
