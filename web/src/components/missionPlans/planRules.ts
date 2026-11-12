/** Mission plans (#1924): the rules the Library page and the save dialog share. Pure functions;
 *  the server (`mission_plan_library`) is the authority and refuses anything these let through. */
import type { MissionPlanCounts, MissionPlanRevision } from "../../types/missionPlans";

/** What a mission plan never holds: one list, shown by the detail page AND the save dialog. */
export const NEVER_SAVED: readonly string[] = [
  "which projects, folders or repositories it used",
  "input values: branches, pull requests, numbers, playbook defaults",
  "the mission's instruction and brief",
  "evidence, results and sessions",
];

export const TITLE_MAX = 120;
export const SUMMARY_MAX = 2000;
export const LABEL_MAX = 60;

/** One slot the save will make: `p<n>` numbered by the project's FIRST APPEARANCE in objective
 *  order — the server's own rule (`from_revision`) — with the objectives that use it. */
export interface PlannedSlot {
  id: string;
  projectId: string;
  objectives: string[];
}

export function slotsOf(plan: NonNullable<MissionPlanRevision["plan"]>): PlannedSlot[] {
  const out: PlannedSlot[] = [];
  for (const o of plan.document.objectives) {
    let slot = out.find((s) => s.projectId === o.project_id);
    if (!slot) {
      slot = { id: `p${out.length + 1}`, projectId: o.project_id, objectives: [] };
      out.push(slot);
    }
    slot.objectives.push(o.id);
  }
  return out;
}

// eslint-disable-next-line no-control-regex
const CONTROL = /[\u0000-\u001f\u007f]/;

/** Why a one-line field can't be saved, or `null`. */
export function oneLineProblem(value: string, what: string, max: number): string | null {
  if (!value.trim()) return `${what} is required.`;
  if (value.length > max) return `${what} is at most ${max} characters.`;
  if (CONTROL.test(value)) return `${what} is one line.`;
  return null;
}

/** Why the summary can't be saved, or `null`: newlines and tabs are fine, other control
 *  characters are not. */
export function summaryProblem(value: string): string | null {
  if (value.length > SUMMARY_MAX) return `The summary is at most ${SUMMARY_MAX} characters.`;
  // eslint-disable-next-line no-control-regex
  if (/[\u0000-\u0008\u000b-\u001f\u007f]/.test(value)) return "The summary has a control character.";
  return null;
}

const plural = (n: number, one: string, many = `${one}s`) => `${n} ${n === 1 ? one : many}`;

export function countsText(c: MissionPlanCounts): string {
  return `${plural(c.objectives, "objective")} · ${plural(c.slots, "project slot")} · ${plural(c.steps, "step")}`;
}
