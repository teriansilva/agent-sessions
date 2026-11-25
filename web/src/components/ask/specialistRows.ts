/** What a specialist card shows (#1938), as plain data: the seven rows of a new specialist, the
 *  per-field diff of an edit and its counts, the states of the specialists a playbook relies on,
 *  and which cards of a turn fold. Pure: the components only lay these out. */
import { GRADE_LABEL } from "../specialists/grades";
import type {
  AskOperationCard,
  AskProposalCard,
  AskReliesRow,
  AskSpecialistShown,
} from "../../types/api";

export type SpecialistField =
  | "title"
  | "summary"
  | "agent"
  | "model"
  | "effort"
  | "grade"
  | "skills"
  | "capacity";

/** The fixed order of a specialist's rows. A new specialist shows the seven after the title (the
 *  title is the card's Target row); an edit lists all eight, changed ones first. */
export const FIELD_ORDER: readonly SpecialistField[] = [
  "title",
  "summary",
  "agent",
  "model",
  "effort",
  "grade",
  "skills",
  "capacity",
];
export const CREATE_ROWS: readonly SpecialistField[] = FIELD_ORDER.slice(1);

export const FIELD_LABEL: Record<SpecialistField, string> = {
  title: "Title",
  summary: "Summary",
  agent: "Agent",
  model: "Model",
  effort: "Effort",
  grade: "Grade",
  skills: "Skills",
  capacity: "Capacity",
};

export const GRADE_HINT = "Follows the model, from your model grades. Never chosen here.";

/** A skill revision as the card says it. The Library's revision is a content digest, not a
 *  counter, so its first eight characters stand for it (any `algo:` prefix dropped). */
export function shortRev(revision: string): string {
  return revision.slice(revision.indexOf(":") + 1).slice(0, 8);
}

export const capacityLine = (n: number) => `Up to ${n} at once`;
export const agentText = (s: AskSpecialistShown) => s.agent || s.engine || "Unknown agent";
export const modelText = (s: AskSpecialistShown) =>
  s.model === "default" ? "The agent's default" : s.model;
export const effortText = (s: AskSpecialistShown) =>
  s.effort ? s.effort.charAt(0).toUpperCase() + s.effort.slice(1) : "The model's own";
export const gradeText = (s: AskSpecialistShown) => (s.grade ? GRADE_LABEL[s.grade] : "Ungraded");

/** One field as one line of text (skills are their own list: `skillLines`). */
export function fieldText(s: AskSpecialistShown, field: SpecialistField): string {
  switch (field) {
    case "title":
      return s.title;
    case "summary":
      return s.summary || "No summary";
    case "agent":
      return agentText(s);
    case "model":
      return modelText(s);
    case "effort":
      return effortText(s);
    case "grade":
      return gradeText(s);
    case "capacity":
      return capacityLine(s.capacity);
    case "skills":
      return s.skills.map((k) => k.id).join(", ") || "No skills";
  }
}

/** A pinned skill on the card. `now`: the Library's revision at this read when it is no longer
 *  the pinned one (`null`: the skill is gone); absent while the pin is current. */
export type SkillLine = {
  id: string;
  revision: string;
  sign: " " | "+" | "−";
  now?: string | null;
};

const moved = (
  k: { id: string; revision: string },
  now: Record<string, string | null> | undefined,
): { now?: string | null } =>
  now && k.id in now && now[k.id] !== k.revision ? { now: now[k.id] } : {};

/** A new specialist's skills, each marked when its pin has moved since the card was made. */
export function skillLines(
  s: AskSpecialistShown,
  now?: Record<string, string | null>,
): SkillLine[] {
  return s.skills.map((k) => ({ ...k, sign: " " as const, ...moved(k, now) }));
}

/** An edit's skills: kept ones unsigned, removed `−`, added `+`; a kept skill whose pin changes
 *  reads as its old revision removed and its new one added. */
export function skillDiff(
  before: AskSpecialistShown,
  after: AskSpecialistShown,
  now?: Record<string, string | null>,
): SkillLine[] {
  const was = new Map(before.skills.map((k) => [k.id, k.revision]));
  const is = new Map(after.skills.map((k) => [k.id, k.revision]));
  const out: SkillLine[] = [];
  for (const k of before.skills) {
    if (is.get(k.id) === k.revision) out.push({ ...k, sign: " ", ...moved(k, now) });
    else out.push({ ...k, sign: "−" });
  }
  for (const k of after.skills) {
    if (was.get(k.id) !== k.revision) out.push({ ...k, sign: "+", ...moved(k, now) });
  }
  return out;
}

/** What follows a skill's id: its pinned revision and, when the pin moved, what holds now. */
export function skillRev(k: SkillLine): string {
  const base = `rev ${shortRev(k.revision)}`;
  if (k.now === undefined) return base;
  return k.now === null
    ? `${base} · no longer in your Library`
    : `${base} · now rev ${shortRev(k.now)}`;
}

export const skillText = (k: SkillLine) => `${k.id} · ${skillRev(k)}`;

/** One row of an edit. `follows`: the grade, when it moves because the model does — it is
 *  derived, so it is never counted as a change HQ proposed. */
export type EditRow = {
  field: SpecialistField;
  state: "changed" | "follows" | "same";
  before: string;
  after: string;
  skills?: SkillLine[];
};

const sameSkills = (a: AskSpecialistShown, b: AskSpecialistShown) =>
  a.skills.length === b.skills.length &&
  a.skills.every((k, i) => k.id === b.skills[i].id && k.revision === b.skills[i].revision);

/** An edit as rows: every changed field first (in the fixed order), then the unchanged ones. */
export function editRows(
  before: AskSpecialistShown,
  after: AskSpecialistShown,
  now?: Record<string, string | null>,
): EditRow[] {
  const rows = FIELD_ORDER.map((field): EditRow => {
    if (field === "skills") {
      return {
        field,
        state: sameSkills(before, after) ? "same" : "changed",
        before: fieldText(before, field),
        after: fieldText(after, field),
        skills: skillDiff(before, after, now),
      };
    }
    const was = field === "agent" ? before.engine : fieldText(before, field);
    const is = field === "agent" ? after.engine : fieldText(after, field);
    const state = was === is ? "same" : field === "grade" ? "follows" : "changed";
    return { field, state, before: fieldText(before, field), after: fieldText(after, field) };
  });
  return [...rows.filter((r) => r.state !== "same"), ...rows.filter((r) => r.state === "same")];
}

export function editCounts(rows: EditRow[]): {
  changed: number;
  follows: number;
  unchanged: number;
} {
  const n = (s: EditRow["state"]) => rows.filter((r) => r.state === s).length;
  return { changed: n("changed"), follows: n("follows"), unchanged: n("same") };
}

/** A playbook card's dependency list, counted by state. */
export function reliesCounts(rows: AskReliesRow[]) {
  const n = (s: AskReliesRow["state"]) => rows.filter((r) => r.state === s).length;
  return { named: rows.length, exists: n("exists"), proposed: n("proposed"), missing: n("missing") };
}

export const RELIES_WORD: Record<AskReliesRow["state"], string> = {
  proposed: "Proposed",
  exists: "Exists",
  missing: "Missing",
};

export function reliesWord(r: AskReliesRow): string {
  return r.state === "proposed" ? `Proposed ${r.where}` : RELIES_WORD[r.state];
}

export function reliesDetail(r: AskReliesRow): string {
  if (r.state === "proposed")
    return `Its card is ${r.where} in this conversation. Not created yet.`;
  if (r.state === "missing") return "No specialist has this job yet, and none is proposed.";
  const model = r.model === "default" ? "the agent's default" : r.model;
  return `${r.agent || r.engine} · ${model}`;
}

/** The specialist cards that carry a whole specialist (a create, an edit). */
const FOLDS = new Set(["specialist.create", "specialist.edit"]);
const isFolding = (p: AskProposalCard): p is AskOperationCard =>
  p.kind === "operation" && FOLDS.has(p.op);

/** The cards of ONE turn that fold to a row: its specialist cards, when there are several. A
 *  single one is shown whole, and so is every other card. Each is given its ordinal. */
export function foldable(proposals: AskProposalCard[]): Map<string, number> {
  const cards = proposals.filter(isFolding);
  return new Map(cards.length > 1 ? cards.map((p, i) => [p.action_id, i + 1]) : []);
}

/** The line above a turn's folded cards: how many still wait and how many are decided. */
export function foldTally(proposals: AskProposalCard[]): { waiting: number; decided: number } {
  const cards = proposals.filter(isFolding);
  const waiting = cards.filter((p) => p.state === "pending").length;
  return { waiting, decided: cards.length - waiting };
}

/** What changes a playbook card's dependency list (#1938): every specialist card of the
 *  conversation with its state. The list is read again whenever this changes. */
export function specialistWatch(proposals: AskProposalCard[]): string {
  return proposals
    .filter((p): p is AskOperationCard => p.kind === "operation" && p.op.startsWith("specialist."))
    .map((p) => `${p.action_id}:${p.state}`)
    .join(",");
}

/** The message "Ask HQ again" sends for an out-of-date card: an ordinary follow-up. */
export function askAgainText(p: Pick<AskOperationCard, "label" | "title">): string {
  return `The card “${p.label}: ${p.title}” is out of date. Propose it again as it stands now.`;
}
