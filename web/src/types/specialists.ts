/** The specialist pool (#1693, #1613 P3). A specialist is a job: title, skills, a preselected engine,
 *  model and effort, and a capacity. Versions are immutable; the grade is derived from the model
 *  through the operator's model→grade map, never typed. */
export type Grade = "fast" | "strong" | "frontier";

export interface SpecialistSkill {
  id: string;
  /** The skills library revision the version pins. */
  revision: string;
}

export interface SpecialistVersion {
  version: number;
  title: string;
  summary: string;
  skills: SpecialistSkill[];
  engine: string;
  model: string;
  effort: string | null;
  capacity: number;
  created_at: number;
}

export interface Specialist {
  id: string;
  revision: number;
  archived: boolean;
  current: SpecialistVersion;
  /** Derived server-side from `model_grades`; `null` = ungraded. */
  grade: Grade | null;
  /** Live instances holding a capacity reservation now (0 until reservations land, S4). */
  live: number;
  /** Pinned skill ids the library no longer has. */
  skills_missing: string[];
  /** Pinned skill ids the library has at a different revision: saving a new version re-pins. */
  skills_outdated: string[];
}

export interface SpecialistTemplate {
  id: string;
  title: string;
  summary: string;
  skills: string[];
  grade: Grade;
}

export interface SpecialistList {
  specialists: Specialist[];
  templates: SpecialistTemplate[];
  /** `pool.max_instances` and how many instances hold a reservation now (S4; absent before). */
  max_instances?: number;
  live_total?: number;
}

export interface SpecialistDetail extends Specialist {
  versions: SpecialistVersion[];
}

export interface SpecialistDraft {
  title: string;
  summary: string;
  skills: SpecialistSkill[];
  engine: string;
  model: string;
  effort: string | null;
  capacity: number;
}
