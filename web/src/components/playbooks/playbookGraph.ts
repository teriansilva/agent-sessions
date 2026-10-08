/** Presentation-only layout; dependency edits preserve the complete shared flow draft. */
import type { Step } from "./playbookDraft";

export const isNote = (step: Step) =>
  step.actor.kind === "none" && !step.checklist?.length;

export function dependencyError(
  steps: Step[],
  source: string,
  target: string,
): string {
  const from = steps.find((s) => s.id === source);
  const to = steps.find((s) => s.id === target);
  if (!from || !to) return "Both steps must still exist.";
  if (source === target) return "A step cannot depend on itself.";
  if (isNote(from)) return "A note cannot be a prerequisite.";
  if (to.after?.includes(source)) return "That dependency already exists.";
  const seen = new Set<string>();
  const pending = [source];
  while (pending.length) {
    const id = pending.pop()!;
    if (id === target)
      return "This would create a dependency cycle. Configure a bounded rework path in the step inspector instead.";
    if (seen.has(id)) continue;
    seen.add(id);
    pending.push(...(steps.find((s) => s.id === id)?.after ?? []));
  }
  return "";
}

export function graphLayout(steps: Step[]) {
  const remaining = new Set(steps.map((s) => s.id));
  const levels = new Map<string, number>();
  const ids = new Set(remaining);
  const missing = steps.some(
    (s) =>
      (s.after ?? []).some((id) => !ids.has(id)) ||
      (s.rework && !ids.has(s.rework.to)),
  );
  // Bounded by the flow's 30-step contract. Invalid drafts still render every node.
  for (let pass = 0; pass < steps.length; pass++) {
    let changed = false;
    for (const s of steps) {
      if (!remaining.has(s.id)) continue;
      const parents = (s.after ?? []).filter((id) => ids.has(id));
      if (parents.some((id) => !levels.has(id))) continue;
      levels.set(
        s.id,
        Math.max(-1, ...parents.map((id) => levels.get(id)!)) + 1,
      );
      remaining.delete(s.id);
      changed = true;
    }
    if (!changed) break;
  }
  const cyclic = remaining.size > 0;
  const fallback = Math.max(-1, ...levels.values()) + 1;
  for (const id of remaining) levels.set(id, fallback);
  const rows = new Map<number, number>();
  const positions = new Map<string, { x: number; y: number }>();
  for (const s of steps) {
    const level = levels.get(s.id)!;
    const row = rows.get(level) ?? 0;
    positions.set(s.id, { x: level * 300, y: row * 220 + 80 });
    rows.set(level, row + 1);
  }
  return { positions, cyclic, missing };
}
