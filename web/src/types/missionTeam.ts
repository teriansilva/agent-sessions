/** A mission's team (#1950, #1613 P5): the shapes `GET …/plan/team` and `…/plan/team/choices`
 *  answer. Every reason, note and refusal line is SERVER text; the client renders it and decides
 *  nothing (`docs/invariants/mission-team.md`). */
import type { Grade } from "./specialists";

export interface TeamReason {
  code: string;
  text: string;
}

export interface TeamPin {
  id: string;
  version: number;
}

export interface TeamEffective {
  engine: string;
  model: string | null;
  effort: string | null;
}

export interface TeamAssignment {
  specialist: TeamPin;
  override: TeamEffective | null;
  reason: string;
  alternates: { specialist: TeamPin; reason: string }[];
  notes: string[];
}

export interface TeamStep {
  id: string;
  objective: string;
  title: string;
  actor: string;
  role: { skills: string[]; min_grade: Grade | null } | null;
  distinct_from: { step: string; constraint: string }[];
  assignable: boolean;
  assignment: TeamAssignment | null;
  /** Re-derived on THIS read: never stored. */
  derived?: {
    title: string;
    effective: TeamEffective;
    grade: Grade | null;
    ok: boolean;
    reasons: TeamReason[];
    notes: TeamReason[];
  };
  unassigned?: {
    summary: string;
    candidates: { specialist: string; reasons: TeamReason[] }[];
  };
}

export interface MissionTeam {
  revision: number | null;
  author_kind?: string;
  team: {
    steps: TeamStep[];
    peak: number;
    pool_max: number;
    pool_note: string | null;
    pool_size: number;
  } | null;
}

export interface TeamCandidate {
  index: number;
  specialist: TeamPin;
  title: string;
  engine: string;
  model: string | null;
  effort: string | null;
  grade: Grade | null;
  ok: boolean;
  reasons: TeamReason[];
}

export interface TeamModelChoice {
  index: number;
  engine: string;
  label: string;
  model: string;
  effort: string | null;
  grade: Grade | null;
  ok: boolean;
  reasons: TeamReason[];
}

export interface TeamChoices {
  revision: number;
  step: string;
  candidates: { choices: TeamCandidate[]; listing: string };
  overrides: { choices: TeamModelChoice[]; listing: string };
}

export type TeamOp =
  | { op: "assemble" }
  | { op: "assign"; step: string; index: number; listing: string }
  | { op: "set_override"; step: string; index: number; listing: string }
  | { op: "clear_override"; step: string };
