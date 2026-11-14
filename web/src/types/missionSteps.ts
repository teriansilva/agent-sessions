/** One API session per agent step (#1581): the board `GET /api/missions/{id}/steps` builds on every
 *  read. Every state and sentence is server text; the client renders it and decides nothing. */

export type StepRunState =
  "claimed" | "running" | "done" | "failed" | "stopped" | "refused" | "unknown";

export type StepRun = {
  step_id: string;
  episode: number;
  revision: number;
  state: StepRunState;
  session_key: string | null;
  engine: string;
  model: string | null;
  effort: string | null;
  use_kind: "primary" | "alternate";
  alternate_index: number | null;
  use_reason: string | null;
  project_id: string | null;
  cwd: string;
  authority: "operator" | "approval" | "consent";
  detail: string | null;
  created_at: number;
  updated_at: number;
  ended_at: number | null;
  specialist: { id: string; version: number };
};

export type StepAssignment = {
  specialist: { id: string; version: number };
  override: { engine: string; model: string; effort: string | null } | null;
  reason: string;
  alternates: { specialist: { id: string; version: number }; reason: string }[];
  notes: string[];
} | null;

export type MissionStep = {
  id: string;
  title: string;
  actor: string;
  objective: string;
  after: string[];
  derived: string;
  assignment: StepAssignment;
  cwd: string | null;
  can_start: boolean;
  why_not: string | null;
  runs: StepRun[];
};

export type MissionBudget = {
  turns: { used: number; cap: number };
  seconds: { used: number; cap: number };
  units: string;
  estimated: boolean;
  revision: number;
  hold: "budget" | "operator" | null;
  hold_detail: string | null;
  hold_at: number | null;
  /** Stop mission is recorded: open steps are stopped as soon as they can be. */
  stopping: boolean;
  /** The raise the board offers on a hold: the server's numbers, sent back as they are. */
  offer: { turns_cap: number; seconds_cap: number };
};

export type MissionSteps = {
  mission_id: string;
  mission_state?: string;
  revision: number | null;
  concurrency: { open: number; cap: number } | null;
  budget?: MissionBudget | null;
  steps: MissionStep[];
};
