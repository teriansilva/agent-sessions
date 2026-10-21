/** Automations (#1201) — the shapes `routes/automations.py` produces.
 *
 *  These are the server's own words for the server's own records: the public automation
 *  (`public()`), a run (`public_run()`) and the 422 a widening save returns. Nothing here is
 *  computed client-side — the scope lines in particular are server-authored, so the consent
 *  dialog shows exactly what the server will hold the operator to. */

export type AutomationState =
  | "off"
  | "enabled"
  | "paused"
  | "needs_reapproval"
  | "erroring"
  | "expired"
  | "finished"
  | "unreadable";

export type Weekday = "mon" | "tue" | "wed" | "thu" | "fri" | "sat" | "sun";

export type Cadence =
  | { kind: "interval"; every: number; unit: "minutes" | "hours" }
  | { kind: "daily"; time: string }
  | { kind: "weekly"; days: Weekday[]; time: string }
  | { kind: "monthly"; day: number; time: string };

export type Trigger =
  | { kind: "manual" }
  | { kind: "once"; at: string; tz: string }
  | { kind: "schedule"; cadence: Cadence; tz: string };

export type AutomationMessage =
  | { text: string }
  | { template_id: string; values: Record<string, string> };

export type Autonomy = "propose" | "dispatch" | "dispatch_auto_choose";

export type AutomationAction =
  | {
      kind: "start_mission";
      project_id: string;
      instruction: AutomationMessage;
      checklist_id: string | null;
      autonomy: Autonomy;
    }
  | {
      kind: "start_session";
      engine: string;
      model: null;
      folder: string;
      bypass: boolean;
      message: AutomationMessage;
    }
  | {
      kind: "send_to_session";
      session_key: string;
      message: AutomationMessage;
    };

export interface AutomationPolicy {
  concurrency: "skip" | "allow";
  max_concurrent: number;
  max_runs_per_day: number;
  pause_after_failures: number;
  expires_at: number | null;
}

/** What a create or a patch sends. `policy` fields the form does not touch keep their defaults. */
export interface AutomationConfig {
  name: string;
  trigger: Trigger;
  action: AutomationAction;
  policy: Partial<AutomationPolicy>;
}

/** Which of three a run counts as on the strip: the worst of a day wins (failed > skipped > ok). */
export type ResultClass = "ok" | "failed" | "skipped" | "pending";

export type RunOutcome =
  | ""
  | "ok"
  | "started"
  | "review"
  | "failed"
  | "refused"
  | "interrupted"
  | "skipped"
  /** The operator's own disable, pause or edit stopped it (strip class `skipped`). */
  | "stopped"
  /** Text was typed into the session but not submitted (strip class `failed`). */
  | "partial";

export interface RunSummary {
  id: string;
  trigger: "once" | "schedule" | "manual" | string;
  slot: string;
  catch_up: boolean;
  covered: number;
  state: "dispatching" | "done" | string;
  outcome: RunOutcome | string;
  result_class: ResultClass;
  reason: string;
  mission_id: string | null;
  session_key: string | null;
  created_at: number;
  finished_at: number | null;
}

export interface RunStep {
  seq: number;
  at: number;
  step: string;
  detail: string;
}

export interface AutomationRun extends RunSummary {
  automation_id: string;
  fire_at: number | null;
  /** Already MASKED server-side — a secret only ever appears as `[secret: name]`. */
  inputs: Record<string, unknown>;
  scope: Record<string, unknown> | null;
  steps?: RunStep[];
}

export interface StripDay {
  date: string;
  worst: ResultClass | null;
}

export interface Automation {
  id: string;
  name: string;
  trigger: Trigger | null;
  action: AutomationAction | null;
  policy: AutomationPolicy | null;
  revision: number;
  state: AutomationState;
  enabled: boolean;
  paused: boolean;
  paused_reason: string;
  needs_reapproval: boolean;
  reapproval_reason: string;
  consented_at: number | null;
  consented_scope: Record<string, unknown> | null;
  /** What enabling (or a widening save) would approve NOW. */
  scope: Record<string, unknown> | null;
  scope_lines: string[];
  scope_digest: string | null;
  consecutive_failures: number;
  /** Why the due check could not check this automation's approved inputs just now, or "". */
  check_note?: string;
  next_run: { slot: string; at: number } | null;
  last_run: RunSummary | null;
  stats: {
    ok: number;
    failed: number;
    skipped: number;
    pending: number;
    runs: number;
    success_rate: number | null;
  };
  strip: StripDay[];
  created_at: number;
  updated_at: number;
  /** On a verb / PATCH / enable response: the change was acknowledged but a run already past its
   *  last check may still complete (`in_flight_detail` says so). Absent or false = no later effect. */
  in_flight?: boolean;
  in_flight_detail?: string;
}

/** `DELETE /api/automations/{id}`. */
export interface AutomationDeleted {
  deleted: string;
  in_flight?: boolean;
  in_flight_detail?: string;
}

/** One roster engine, as the server's unattended-start check sees it right now. */
export interface UnattendedEngine {
  id: string;
  ok: boolean;
  reason: string;
}

export interface AutomationList {
  automations: Automation[];
  /** Absent from a server that predates the editor's engine offer. */
  engines?: UnattendedEngine[];
  loop: { enabled: boolean; owner: boolean };
  limits: {
    interval_min_minutes: number;
    max_runs_per_day_max: number;
    max_concurrent_max: number;
    pause_after_failures_max: number;
    triggers: string[];
    actions: string[];
    autonomy: string[];
    timezone: string;
  };
}

export interface AutomationOrigin {
  kind: "session" | "mission" | string;
  automation_id: string;
  name: string;
  run_id: string;
  deleted: boolean;
}

/** The body of a refused save or enable that needs consent: `widened` names what grew (absent on a
 *  first enable), and the scope lines + digest are what the dialog shows and sends back. */
export interface ConsentRequired {
  detail: string;
  widened?: string[];
  scope?: Record<string, unknown>;
  scope_lines?: string[];
  scope_digest?: string;
}
