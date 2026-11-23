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
  | { kind: "schedule"; cadence: Cadence; tz: string }
  /** A webhook picked from Library → Webhooks (#2221). It starts a review loop, a session, a
   *  message to a session (#2221 P2) or a message to HQ; `when` (omitted when empty) chooses which
   *  deliveries, and a review loop takes none. */
  | { kind: "webhook"; webhook_id: string; when?: WebhookCondition[] }
  /** #2202: a review loop's own Forgejo webhook. Read-only: an existing automation keeps it, but the
   *  server refuses creating a new one in this form. */
  | { kind: "webhook"; scheme: "forgejo" };

export type AutomationMessage =
  | { text: string }
  | {
      template_id: string;
      values: Record<string, string>;
      /** Template fields filled from each webhook delivery, typed (#2221 P2). Omitted when empty;
       *  a field is never both here and in `values`. */
      from_delivery?: Record<string, DeliveryMapping>;
    };

/** The closed set of condition operators (`webhook_events.OPS`). */
export type ConditionOp =
  | "is"
  | "is_not"
  | "one_of"
  | "contains"
  | "is_true"
  | "is_false"
  | "exists"
  | "missing";

export type ConditionScalar = string | number | boolean;

/** One condition on a delivery's signed body: all of an automation's must hold. The path is a
 *  dotted body path (`pull_request.draft`, `labels[].name`), never a header. */
export interface WebhookCondition {
  path: string;
  op: ConditionOp;
  value?: ConditionScalar | ConditionScalar[];
}

/** The types a delivery value may fill a template field with (`webhook_events.TYPES`). */
export type MappedType = "number" | "choice" | "text" | "yes_no";

/** A template field filled from the delivery: one field (never a list path), typed. */
export interface DeliveryMapping {
  path: string;
  type: MappedType;
  /** A choice's allowed values. */
  choices?: string[];
}

export type Autonomy = "propose" | "dispatch" | "dispatch_auto_choose";

export type AutomationAction =
  | {
      kind: "start_mission";
      project_id: string;
      instruction: AutomationMessage;
      /** A record approved before checklists were removed (#1653) may still carry one. Inert. */
      checklist_id?: string | null;
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
    }
  /** "Tell HQ" (#2221): a message in the operator's notifications, filed as an HQ dispatch. HQ is
   *  told only that a message from this automation arrived, never what it says, and never acts on
   *  it. No target, agent or permissions of its own. */
  | { kind: "tell_hq"; message: AutomationMessage }
  | ReviewLoopAction
  | PluginAction;

/** The registry's plugin actions (#2248 P2), stored as ONE kind. `inputs` is the plugin's own,
 *  validated strictly by the server; `from_delivery` maps a webhook delivery's typed values. */
export type PluginId =
  | "send_message"
  | "call_webhook"
  | "publish_artifact"
  | "write_memory"
  | "run_command"
  | "run_script";
export const PLUGIN_IDS: readonly PluginId[] = [
  "send_message",
  "call_webhook",
  "publish_artifact",
  "write_memory",
  // #2248 P3: Run locally.
  "run_command",
  "run_script",
];

/** One element of a run-local action's argument list (#2248 P3): a fixed text (an `option` keeps
 *  the element after it fixed too), or ONE typed value from the delivery filling the whole element. */
export type RunArg = { value: string; option?: true } | { from: string };
/** A run-local action's environment variable: fixed, or one typed value from the delivery. */
export type RunEnv = { name: string; value: string } | { name: string; from: string };
/** The types a run-local action may declare for a field read from its last line of stdout. */
export type RunOutputType = "number" | "text" | "yes_no";

/** "Call a webhook" (#2248 P2): its destination and whether its signing secret exists. Never the
 *  secret: that is in the ONE answer that made it (`destination_secret`). */
export interface DestinationState {
  url: string;
  secret_set: boolean;
  secret_made_at: number | null;
}

export interface PluginAction {
  kind: "plugin";
  plugin: PluginId;
  version: 1;
  inputs: Record<string, unknown>;
  from_delivery?: Record<string, DeliveryMapping>;
}

/** What the wizard picks: one of the five built-in kinds, or a plugin's id. */
export type ActionKind = Exclude<AutomationAction["kind"], "plugin"> | PluginId;

/** The Forgejo review loop (#2202): one repository, one pinned reviewer specialist VERSION, one
 *  reviewer login, inside one project. Every field is part of the consent scope. */
export interface ReviewLoopAction {
  kind: "forge_review_loop";
  project_id: string;
  /** `owner/name` on the configured forge. */
  repo: string;
  reviewer: { specialist_id: string; version: number };
  /** The forge account whose verdicts are trusted. */
  reviewer_login: string;
  legs: { issues: boolean; pulls: boolean };
  /** Reviews per item over its whole life (1..10). */
  max_rounds: { issue: number; pr: number };
  return_to_origin: boolean;
}

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
  | "partial"
  /** #2202: a signed delivery that was not acted on (stale, closed, a bad verdict). Strip `skipped`. */
  | "ignored"
  /** #2202: the item already had its last review; nothing launched. Strip `skipped`. */
  | "capped"
  /** #2202: an accepted verdict that could not be sent to its origin. Strip `failed`. */
  | "undelivered";

export interface RunSummary {
  id: string;
  trigger: "once" | "schedule" | "manual" | "webhook" | string;
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
  /** "Call a webhook" (#2248 P2): the destination's state; `null` for any other action. */
  destination?: DestinationState | null;
  /** The signing secret, in the ONE save answer that made it. Never in a read. */
  destination_secret?: string;
  destination_secret_error?: string;
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
  /** #2202: a webhook automation's delivery facts (never a secret); `null` for any other trigger.
   *  Absent from a server that predates the review loop. */
  webhook?: AutomationWebhook | null;
  created_at: number;
  updated_at: number;
  /** On a verb / PATCH / enable response: the change was acknowledged but a run already past its
   *  last check may still complete (`in_flight_detail` says so). Absent or false = no later effect. */
  in_flight?: boolean;
  in_flight_detail?: string;
}

/** Where the forge delivers and what arrived (#2202). The secret itself is never here: it is shown
 *  once, by the call that makes it. Since #2221 it is the picked webhook's public view plus
 *  `webhook_id`; a legacy review loop's is its own webhook (`legacy_automation` set). The extra
 *  fields are absent from a server that predates the list. */
export interface AutomationWebhook {
  webhook_id?: string;
  id?: string;
  name?: string;
  legacy_automation?: string | null;
  pickable?: boolean;
  /** Root-relative: `/hooks/a/<id>`. */
  path: string;
  secret_set: boolean;
  secret_created_at: number | null;
  /** A rotated-out secret keeps verifying deliveries until then. */
  previous_valid_until: number | null;
  last_delivery_at: number | null;
  last_event: string;
  last_note: string;
  /** Unsigned or badly signed deliveries refused in the last 24 h. */
  unsigned_24h: number;
}

/** `POST /api/automations/{id}/webhook-secret` (201): the plaintext, this once. */
export interface WebhookSecret {
  secret: string;
  webhook: AutomationWebhook;
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
