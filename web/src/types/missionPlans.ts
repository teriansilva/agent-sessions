/** Mission plans in the Library (#1924, #1613 P7a): the shapes `/api/mission-plans` answers. */

export interface MissionPlanCounts {
  objectives: number;
  slots: number;
  steps: number;
}

/** One plan as the list shows it. `origin_kind` is the only provenance a response carries. */
export interface MissionPlanRow {
  id: string;
  version: number;
  revision: number;
  archived: boolean;
  title: string;
  summary: string;
  origin_kind: "mission" | "file";
  created_at: number;
  updated_at: number;
  counts: MissionPlanCounts;
}

export interface MissionPlanProcedure {
  source: "playbook" | "mission_local";
  flow_id: string;
  title: string;
  playbook_id?: string;
}

export interface MissionPlanObjective {
  id: string;
  slot: string;
  procedure: MissionPlanProcedure;
  inputs: string[];
  after?: string[];
}

export interface MissionPlanDocument {
  format: 1;
  title: string;
  summary: string;
  slots: { id: string; label: string }[];
  objectives: MissionPlanObjective[];
}

/** A derived step: its actor and the role it binds (skills and a minimum grade), never stored. */
export interface MissionPlanStep {
  id: string;
  objective: string;
  title: string;
  actor: string;
  after: string[];
  role: { skills: string[]; min_grade: string | null } | null;
}

export interface MissionPlanDetail extends MissionPlanRow {
  document: MissionPlanDocument;
  steps: MissionPlanStep[];
}

/** One plan revision of a mission (`GET /api/missions/{id}/plan/revisions/latest`), as much of it as
 *  the save dialog reads: which project each objective names, in order. */
export interface MissionPlanRevision {
  mission_id: string;
  plan: null | {
    revision: number;
    document: { objectives: { id: string; project_id: string; procedure: { title: string } }[] };
  };
}
