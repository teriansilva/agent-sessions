// Types mirroring the existing FastAPI `/api/*` contract (backend is unchanged).

export type EngineId =
  "claude" | "opencode" | "codex" | "gemini" | "antigravity" | "kimi" | "shell";

export interface Session {
  /** engine-qualified identity, e.g. "claude:<uuid>" — the URL + socket + lock key */
  id: string;
  engine: EngineId | string;
  uuid: string;
  short_uuid: string;
  cwd: string;
  /** Resolved project ref (#361): entity or implicit folder group. `cwd` stays the
   *  launch location; with zero entities this is always `{kind:"folder", id: cwd}`. */
  project: ProjectRef;
  last_mtime: number;
  /** Derived per-engine creation time (#506) — the sort key when the list order is
   *  "created_at". Absent on older servers (→ treated as 0). */
  created_at?: number;
  /** Wall-clock of the last byte the agent emitted that we observed (#156). null when
   * the server hasn't seen output for this session in this process (no WS attached). */
  last_output_at?: number | null;
  /** True when ``now - last_output_at`` is inside the working window (#156 v1).
   * Browser-attached-only; a headless session not yet reconnected to reports false. */
  working?: boolean;
  first_user_message: string;
  title: string;
  sticky: boolean;
  /** Custom per-session tag (#551): a short user label (text / emoji) rendered before the
   *  AI summary on the row's second line. "" / undefined when unset. */
  tag?: string;
  archived: boolean;
  /** AI review (#356): one-line summary from the last successful review. */
  ai_summary?: string;
  /** AI-generated title. Display precedence is resolved SERVER-side into `title`
   *  (user title → ai_title → first message); this field is informational. */
  ai_title?: string;
  /** Advisory "needs a human" flag from the last review + its short reason. */
  intervention_required?: boolean;
  intervention_reason?: string;
  /** Wall-clock (s) of the last SUCCESSFUL review — the stale-age source: a failed
   *  review never bumps it, so an old result is visibly old. null/absent = never. */
  reviewed_at?: number | null;
  /** Per-session opt-out from AI review. */
  review_excluded?: boolean;
  /** #726: per-session opt-out from Pulse orchestration. Managed-by-default, so this is
   *  false until the operator withdraws agency for this session. DISTINCT from
   *  review_excluded: an unmanaged session is still listed, still summarised, still
   *  flagged needs-you — it just stops being something the orchestrator may act on. */
  orchestrator_excluded?: boolean;
  /** #481: chronological "what happened in this session" recap over the whole transcript,
   *  shown in the session-brief modal. "" / absent until the first review produces one. */
  ai_recap?: string;
  /** #477: the session's compose box has an unsent draft (text and/or pasted images) →
   *  the blue status dot. The full draft body is fetched separately via api.getDraft. */
  has_draft?: boolean;
  /** Cross-engine handoff provenance (#597): engine-qualified peer ids, "" when unset.
   *  Written server-side only after the target spawn passes the aliveness gate; a stale
   *  half (peer archived/deleted) is tolerated — display strings, never dereferenced. */
  handoff_from?: string;
  handoff_to?: string;
  /** The mission holding this session (#948): `null` when none does. ABSENT when the server
   *  could not read the mission store — unknown, so no adopt/open control is offered. */
  mission?: SessionMissionRef | null;
}

/** One pasted/uploaded attachment carried by a compose draft (#477) — the server-issued
 *  upload path + display name. No image blob ever crosses the wire here. */
export interface DraftAttachment {
  name: string;
  path: string;
}

/** A `{{name}}` slot on an instruction template (#905), filled when the template is used.
 *  `name` is the token (`[a-z][a-z0-9_]{0,31}`); `label` is what the fill step shows. */
export interface TemplateField {
  name: string;
  label: string;
  default: string;
  required: boolean;
}

/** A reference image on a template — an upload path, never a blob. The thumbnail is read back
 *  through `GET /api/uploads/{stored}` where `stored` is the path's basename. */
export interface TemplateImage {
  name: string;
  path: string;
}

/** An instruction template (#905) — `GET /api/templates`. A *message*, not a prompt: the
 *  body is user-turn text the composer pastes into a session, never a system prompt. The
 *  server owns `id` and every timestamp/counter; `updated_at` is also the optimistic-
 *  concurrency fence (`expected_updated_at` on PATCH/DELETE, 409 + `current` on a stale edit).
 *  `last_used_at` / `used_count` move on a send and never touch `updated_at`. */
export interface Template {
  id: string;
  name: string;
  description: string;
  tags: string[];
  body: string;
  fields: TemplateField[];
  images: TemplateImage[];
  created_at: number;
  updated_at: number;
  used_count: number;
  last_used_at: number | null;
}

/** The editable half of a template — what POST/PATCH `/api/templates` accept. */
export type TemplateInput = Pick<
  Template,
  "name" | "description" | "tags" | "body" | "fields" | "images"
>;

/** The server's bounds, served with the library so the editor never hardcodes a cap. */
export interface TemplateLimits {
  templates_max: number;
  name_max: number;
  description_max: number;
  tags_max: number;
  body_max: number;
  fields_max: number;
  label_max: number;
  default_max: number;
  images_max: number;
  image_suffixes: string[];
}

export interface TemplatesResponse {
  templates: Template[];
  limits: TemplateLimits;
}

/** GET /api/sessions/{id}/draft (#477): the saved compose draft for a session, or an empty
 *  draft (`text: ""`, `attachments: []`, `updated_at: null`) when there is none. */
export interface SessionDraft {
  id: string;
  text: string;
  attachments: DraftAttachment[];
  updated_at: number | null;
}

/** AI session review config (#356) — the PUBLIC view from /api/config. The API key is
 *  write-only: only `api_key_set` ever crosses the wire. */
export interface AiReviewConfig {
  enabled: boolean;
  base_url: string;
  model: string;
  interval_minutes: number;
  max_input_chars: number;
  /** Per-request review timeout in seconds (10–600); null = unset → server falls back
   *  to the AGENT_SESSIONS_AI_REVIEW_TIMEOUT env var, then 120s (#391 follow-up). */
  request_timeout: number | null;
  /** A key is stored server-side (its value is never echoed). */
  api_key_set: boolean;
  /** Base URL + key present — the /models proxy + Review now are usable. */
  configured: boolean;
}

/** One row of the prompt catalog (#824) — `GET /api/prompts`.
 *
 *  `value` is what the operator typed and `guard_suffix` is the clause the SERVER appends to
 *  a guarded prompt at call time: separate fields on purpose, so the editor never contains
 *  text the operator cannot change (and a client that echoed it back would have it stripped
 *  server-side anyway). Storage bindings are deliberately absent — the client edits by id. */
export interface PromptEntry {
  id: string;
  /** Feature heading the row sits under — the catalog's only grouping hint. */
  group: string;
  label: string;
  description: string;
  contract: string;
  max_chars: number;
  guarded: boolean;
  guard_suffix: string | null;
  value: string;
  default: string;
  is_default: boolean;
}

/** AI auto-sort config (#424 Phase 6) — the PUBLIC view from /api/config. Opt-in; reuses the
 *  ai_review endpoint, so it holds no secret of its own. Tuning knobs added in #459. */
export interface AutoSortConfig {
  enabled: boolean;
  interval_minutes: number;
  /** Assignment confidence floor (0.5–0.95, default 0.7); below it a session stays
   *  unassigned. Lower it when confident matches are too rare (#459). */
  confidence_min: number;
  /** Max sessions classified per run — the on-demand button AND the background loop
   *  (1–50, default 8) (#459). */
  max_per_pass: number;
  /** The reused ai_review endpoint is usable (base URL + key present). Mirrors
   *  `ai_review.configured` — auto-sort can't run without it. */
  configured: boolean;
}

/** Report from POST /api/projects/auto-sort (#424 Phase 6): one bounded on-demand pass. */
export interface AutoSortReport {
  candidates: number;
  scanned: number;
  assigned: { id: string; project_id: string; confidence: number }[];
  low_confidence: number;
  errors: number;
  /** Unassigned sessions whose best-guess project fell below `confidence_min` — the
   *  actionable near-misses, so the operator can lower the threshold with eyes open.
   *  Bounded + sorted by confidence desc; only known-project picks (#459). */
  near_misses?: { id: string; project_id: string; confidence: number }[];
  /** Present when the pass did nothing (e.g. "no projects" / "not configured"). */
  skipped?: string;
}

/** Pulse recent-work overview config (#441 Phase 3) — the PUBLIC view from /api/config. Opt-in
 *  background scan + the window/depth scans use; reuses the ai_review endpoint for synthesis
 *  (depth ≥ medium), so it holds no secret of its own. */
export interface PulseConfig {
  auto_enabled: boolean;
  interval_minutes: number;
  window_days: number;
  scan_depth: PulseDepth;
  /** The reused ai_review endpoint is usable (base URL + key). Slow synthesis degrades to
   *  fast when this is false; fast scans never need it. */
  configured: boolean;
}

/** `medium` was removed in #956 (it only added a banner nothing rendered). */
export type PulseDepth = "fast" | "slow";

/** A session state bucket on the Pulse overview, ranked needs-you → in-flight → recent → idle. */
export type PulseState = "needs_you" | "in_flight" | "recently_active" | "idle";

/** One curated card on the Pulse overview (#441). All AI-derived text (`ai_summary`,
 *  `synthesis`) is DATA — render it as plain text, never markup. */
export interface PulseCard {
  /** Engine-qualified session key ("engine:uuid") — also the jump target. */
  id: string;
  engine: EngineId | string;
  title: string;
  cwd: string;
  project: ProjectRef;
  /** Wall-clock (s) of the session's last activity. */
  last_activity: number;
  /** One-line summary from the last AI review (#356), or null. */
  ai_summary: string | null;
  intervention_required: boolean;
  intervention_reason: string;
  reviewed_at: number | null;
  /** Live overlay from the registry at scan time (working/attached) → state "in_flight". */
  live: boolean;
  state: PulseState;
  /** Per-session "state + next step" line from a `slow` scan (#441 Phase 4); null otherwise.
   *  When set the card shows it instead of `ai_summary`. */
  synthesis: string | null;
  /** Which mission holds this session, stamped server-side by `routes/pulse._attach_pending`
   *  over EVERY mission — loaded or not (#878).
   *
   *  TRI-STATE, and the three answers are not interchangeable: a mission id means that mission
   *  holds it; `null` means no mission does (adoptable); and the field being ABSENT means the
   *  membership store could not be read, which is neither. The console must not derive this by
   *  scanning the mission rows it has in memory — that list is paged, so a session held by an
   *  unloaded mission read as unheld and was offered an adoption the server refused. */
  mission_id?: string | null;
  /** The live orchestrator action on this session, attached server-side by
   *  `routes/pulse._attach_pending` (#754). Present means the card carries the decision
   *  controls inline; the queue is no longer a separate list. */
  pending_action?: OrchestratorAction;
  /** The orchestrator's most recent SETTLED action on this session (#777) — what it last did
   *  here, now that the separate Activity list is gone. Never set alongside `pending_action`:
   *  a card either has a decision waiting or a history line, not both. */
  last_action?: OrchestratorAction;
  /** The band this card had BEFORE `pending_action` re-banded it to `needs_you`. Present only
   *  when the overlay fired; the client restores it when an action is settled locally, so the
   *  session does not sit under "Needs you" with nothing pending. */
  state_without_action?: PulseState;
  /** True when the card exists only because a live action does — there was no cached card for
   *  the session. Settle the action and there is nothing left to show, so the card goes too. */
  synthesized_for_action?: boolean;
}

/** The cached Pulse overview artifact from GET /api/pulse / POST /api/pulse/scan (#441).
 *  `generated_at` is null before the first scan (the "never scanned" empty overview). */
export interface PulseOverview {
  cache_version: number;
  generated_at: number | null;
  window_days: number;
  scan_depth: PulseDepth;
  input_fingerprint: string | null;
  /** True when a slow scan ran against an unconfigured endpoint and degraded to fast curation
   *  (no per-session synthesis). */
  synthesis_skipped: boolean;
  cards: PulseCard[];
}

/** A Pulse "Ask" match (#522): the full Pulse card plus the model's one-line reason —
 *  rendered by the same Card component, so "Jump in" works unchanged. */
/** #726: the operator's autonomy tier. `off` observes and proposes only; `suggest`
 *  (the default) queues every action for one tap; `yolo` delivers autonomously — but ONLY
 *  the verbs inside the server-owned ceiling, which is `continue` alone in this release. */
export type OrchestratorTier = "off" | "suggest" | "yolo";

/** #726: what the orchestrator proposed. `observe`/`escalate` never reach a session. */
export type OrchestratorVerb =
  | "observe"
  | "continue"
  | "choose"
  | "answer"
  | "dispatch"
  | "escalate"
  /** #983 P3: an AI-drafted direction. Minted only by a mission's supervisor, always a proposal,
   *  and typed only when the operator approves it. Never in the autonomy ceiling. */
  | "draft_direction";

/** #726: the lifecycle of one action. `indeterminate` is deliberate: if the process dies
 *  between the PTY write and the durable record, nothing on disk can prove whether the bytes
 *  landed, so it is parked for the operator rather than retried (which could double-deliver)
 *  or assumed delivered (which could silently drop). At-most-once, stated honestly. */
export type OrchestratorState =
  | "proposed"
  | "approved"
  | "claimed"
  | "delivered"
  | "escalated"
  /** A yolo action below `confidence_min` that kept a real DELIVERING verb (#877). Distinct
   *  from `escalated` — which is the model asking a QUESTION, with nothing to run — because
   *  only this one can be answered "yes". It is in the server's `CLAIMABLE_STATES`; plain
   *  `escalated` is not. */
  | "escalated_low_confidence"
  | "observed"
  | "rejected"
  | "stale"
  | "failed"
  | "expired"
  | "indeterminate";

/** The states an action can no longer leave — `orchestrator_ledger.TERMINAL_STATES`, i.e.
 *  `OrchestratorState` minus that module's `LIVE_STATES`. Note `escalated` is **live**, not
 *  terminal: it is waiting on the operator, which is why it carries decision controls and rides
 *  `pending_action` rather than the history line.
 *
 *  Split out so a map over settled actions (Pulse's history line) can be typed
 *  `Record<TerminalActionState, …>`: adding a state to `OrchestratorState` without giving it
 *  wording is then a **compile error**, not a blank cell nobody notices. */
export type TerminalActionState = Exclude<
  OrchestratorState,
  "proposed" | "approved" | "claimed" | "escalated" | "escalated_low_confidence"
>;

export type EvidenceKind = "screen" | "transcript_tail" | "recap" | "none";

export interface OrchestratorAction {
  id: string;
  /** Feed rows only (#774): how many actions this row stands for. The orchestrator makes a
   *  fresh action per session per pass, so the feed collapses to one row per session and this
   *  says what was folded in. 1 when nothing was. */
  repeats?: number;
  state: OrchestratorState;
  ts: number;
  expires_at?: number;
  tier: OrchestratorTier;
  session_id: string;
  engine: string;
  title: string;
  /** Resolved project name — from the same projects.resolve the sidebar uses, so a feed row
   *  and its sidebar row always agree. */
  project: string;
  project_id: string;
  verb: OrchestratorVerb;
  confidence: number;
  rationale: string;
  evidence: EvidenceKind;
  option?: number;
  answer?: string;
  /** Why this action is `escalated` — server-decided in `orchestrator._decide`, never inferred
   *  here and never model-supplied. `model`: the model chose to escalate. `degraded`: it meant
   *  to deliver but produced no usable option / no answer text. `confidence`: it fell under the
   *  yolo threshold. Absent on every non-escalated action, and on records written before the
   *  field existed — which render NO reason rather than a guessed one. */
  escalation_reason?: "model" | "degraded" | "confidence";
  /** What the session's screen showed when this was ESCALATED (#1060) — `orchestrator.
   *  observed_prompt_for`. An escalation delivers nothing, so it has no precondition; this is the
   *  card's only fact about the screen. `menu` is the engine's own menu when one was recognised at
   *  the bottom of the screen, else null. Its labels are the agent's words — display text, never
   *  bytes: what a tap sends is re-derived from the live screen at approval time. Absent on every
   *  other verb and on records written before the field existed. */
  observed_prompt?: {
    prompt_class: string;
    menu: {
      engine: string;
      question: string;
      options: { n: number; label: string; selected: boolean }[];
    } | null;
    observed_at: number;
  };
  /** How this action should be RENDERED — decided once on the server by
   *  `orchestrator_ledger.project_for_operator` and consumed verbatim (#852/#840 §16).
   *
   *  Three sets on the server already disagreed about what "pending" means, so every surface
   *  that re-derived controls from `state` got `approved` wrong: it is in flight and reject-only,
   *  because approving again is a no-op the backend refuses. `claimed` is live rather than
   *  terminal and offers nothing at all. Optional so a response written before the field existed
   *  still renders — the fallback below reproduces the old behaviour rather than guessing. */
  projection?:
    | "actionable"
    | "in_flight_revocable"
    | "in_flight_locked"
    | "settled"
    | "historical"
    /** The ledger could not be READ — distinct from `historical`, which means it read fine and
     *  the action is not in it. No controls: the mutation routes read through the same reader,
     *  so a Reject offered here comes back 404 "unknown action". The row stays visible and keeps
     *  counting, and regains its controls by itself when the store reads again. */
    | "unknown";
  can_approve?: boolean;
  can_reject?: boolean;
  /** True once this action has been raised in the bell. The rail needs it to tell a decision the
   *  operator has already seen from one that has never surfaced anywhere. */
  announced?: boolean;
  /** Who minted it. `supervisor` is a mission's follow-through (#885). */
  source?: string;
  mission_id?: string;
  objective_key?: string;
  objective_episode?: number;
  /** The objective's current title, stamped by the server on a supervisor nudge (#983 P2). Display
   *  only: a title is never an input to what is typed. */
  objective_title?: string;
  /** A supervisor nudge's EXACT text and the facts it rests on, persisted at proposal (#983). */
  render?: SupervisorRender;
  /** Whether delivery would still type `render.text` (#983 P2): the delivery path's own comparison,
   *  asked early. `sendable: false` comes with `can_approve: false`. */
  render_status?: RenderStatus;
  /** What a delivered supervisor nudge typed (#983). */
  delivered_text?: string;
  /** An AI-drafted direction's text (#983 P3): stored sanitized at proposal, exactly what Send as
   *  written types. Model-authored, so it renders as text and is never interpreted. */
  draft?: string;
  /** The objective incarnation a draft is bound to (#983 P3). Server-side identity; not shown. */
  objective_incarnation?: string;
}

/** One checked fact a direction was filled with (#983). `observed_at` is null for a fact that came
 *  from the objective's own probe settings rather than an observation. */
export interface DirectionFact {
  name: string;
  value: string | number;
  target?: Record<string, unknown> | null;
  observed_at?: number | null;
}

/** `mission_directions.render`'s output as a proposal persisted it (#983). */
export interface SupervisorRender {
  text: string;
  source: "direction" | "default_nudge" | (string & {});
  facts: DirectionFact[];
  digest?: string;
  provenance?: Record<string, unknown>;
}

export interface RenderStatus {
  sendable: boolean;
  /** The server's own words for what changed. Empty when sendable. */
  reason: string;
}

/** One row of the server's placeholder table (`mission_directions.placeholder_table`, #983 P2). */
export interface DirectionPlaceholder {
  name: string;
  hint: string;
  /** The probe kinds whose objective can fill this placeholder. */
  probes: string[];
}

/** `POST /api/mission-directions/preview`: the direction filled with EXAMPLE facts. `text` is null
 *  when there is no direction, so a nudge would type the default nudge. */
export interface DirectionPreview {
  text: string | null;
  facts: { name: string; value: string | number }[];
}

export interface OrchestratorConfig {
  enabled: boolean;
  autonomy: OrchestratorTier;
  allowed_verbs: string[];
  /** The server-owned ceiling. Surfaced so the UI can SHOW that choose/answer/dispatch always
   *  need a tap, rather than implying the tier alone decides. */
  auto_verbs_ceiling: string[];
  confidence_min: number;
  interval_minutes: number;
  max_actions_per_pass: number;
  proposal_ttl_minutes: number;
  /** Idle window (#768): past this many hours the orchestrator stops considering a session,
   *  so it stops notifying about it. The session stays visible everywhere else. */
  stale_hours: number;
  nudge_template: string;
  notify: "none" | "escalations" | "all";
  configured: boolean;
  default_nudge_template: string;
  /** AUTONOMOUS AI-WRITTEN DIRECTIONS (#983 P4). Off by default, and the server refuses to turn it
   *  on outside `yolo` — leaving that tier switches it off durably, so the UI must never treat a
   *  remembered `true` as still armed. While it is on, an AI-drafted direction at or above the
   *  threshold below is typed into a session with nobody reading it first. */
  auto_ai_directions: boolean;
  ai_direction_confidence_min: number;
  /** The approved FLOOR (0.90) and ceiling (1.00) for the threshold. Sent by the server rather
   *  than spelled here, so the control cannot offer a value the save would 422. */
  ai_direction_confidence_floor: number;
  ai_direction_confidence_max: number;
}

export interface PulseNotification {
  id: string;
  ts: number;
  read: boolean;
  title: string;
  /** Resolved project name — the same one the sidebar and the feed show. */
  project: string;
  /** Why it was raised. Rendered in-app ONLY; never travels in a push payload (#726). */
  reason: string;
  session_id: string;
  engine: string;
  action_id: string;
}

export interface NotificationList {
  notifications: PulseNotification[];
  unread: number;
  /** Unread escalations whose ledger state could NOT be established — the store would not read,
   *  or the row carries no `action_id`. Deliberately not folded into `unread`: those rows project
   *  as `unknown` and offer no control, so counting them as actionable gives a number the
   *  operator cannot clear by acting (#852 rule 5). Reported separately so the console can say
   *  "something is outstanding and its state is unreadable" instead of overstating or hiding it.
   *  Optional so a response predating the field still parses. */
  uncertain?: number;
  /** A bounded window of recently DECIDED rows, projected back as history with NO controls
   *  (#852). Bounded by the SERVER — newest 10 / 24h — and the bound is on the PROJECTION, not
   *  the store: a row that ages out of this list is still present, still suppressing a
   *  re-announce (#760), and merely stops being drawn. Optional for the same reason as above. */
  settled?: PulseNotification[];
}

/** A registered browser. `origin` only — the endpoint is a per-device capability URL and
 *  never leaves the server. */
export interface PushSubscriptionInfo {
  id: string;
  origin: string;
  created_at: number;
}

export interface OrchestratorState_ {
  config: OrchestratorConfig;
  pending: OrchestratorAction[];
  feed: OrchestratorAction[];
  expired_now: number;
  /** Verbs the actuator can actually render and deliver. Server-owned so the UI cannot offer
   *  Approve on something every delivery would 409 (the client used to keep its own copy). */
  delivering_verbs?: string[];
  /** Per-kind last-run record from `aitasks.snapshot()`. `orchestrator` is the scheduled pass:
   *  a run of failures here is the difference between "nothing needs you" and "nothing has
   *  been looked at since yesterday evening" (#772). */
  last?: Record<string, AiTaskLast | undefined>;
}

/** One AI task kind's last run. */
export interface AiTaskLast {
  finished_at: number;
  ok: boolean;
  detail?: string;
  duration_s?: number;
  /** Why the last run failed — a remote endpoint's message, clamped server-side. Rendered as
   *  plain text, never markup. Null once a run succeeds. */
  error?: string | null;
  /** A single failure is a blip; a run of them is an outage. */
  consecutive_failures?: number;
  /** Wall-clock (s) of the last SUCCESSFUL run, carried across failures. */
  last_ok?: number | null;
}

/** Server-pulled evidence. The model only ever names a `kind`; every byte here comes from the
 *  real session, fetched at render time — a model that can quote a screen can invent one. */
export interface Evidence {
  kind: EvidenceKind;
  text: string;
  available: boolean;
}

export interface PulseAskMatch extends PulseCard {
  why: string;
  /** The live orchestrator action on this session, if there is one — server-supplied, never
   *  model-asserted (`routes/pulse._with_pending`). Absent when nothing is waiting. */
  pending?: { action_id: string; state: string; verb: string };
}

/** POST /api/pulse/ask (#522). `stage`: `catalog` = ranked from session metadata only;
 *  `content` = confirmed against transcript tails; `empty` = no sessions at all (no AI
 *  call was made). Errors: 409 unconfigured (`configured: false` in the body) or a
 *  question already running; 502 endpoint failure. */
/** A mission an Ask answer is about (#1069). `id` is the bare mission id — what `missionLink`
 *  takes. Server-built from the missions store; only `why` is model text. */
export interface PulseAskMissionMatch {
  id: string;
  title: string;
  state: string;
  project_id: string;
  why: string;
}

export interface PulseAskResult {
  answer: string;
  matches: PulseAskMatch[];
  /** Missions the answer names (#1069). Optional: a server before #1069 does not send it. */
  mission_matches?: PulseAskMissionMatch[];
  stage: "catalog" | "content" | "empty";
  configured: boolean;
}

/** One running AI task in the shared activity surface (#441 Phase 1). */
export interface AiActivityTask {
  kind: string;
  detail: string;
  started_at: number;
}

/** The last run of an AI task kind (#441 Phase 1). */
export interface AiActivityLast {
  finished_at: number;
  ok: boolean;
  detail: string;
  duration_s: number;
}

/** GET /api/ai/activity (#441 Phase 1): what AI work is running now + the last run per kind.
 *  Also the body of POST /api/pulse/scan's 409 (a Pulse scan already running), with `detail`. */
export interface AiActivity {
  running: AiActivityTask[];
  last: Record<string, AiActivityLast>;
  /** Only present on the 409 scan-already-running body. */
  detail?: string;
}

export interface SessionsPage {
  sessions: Session[];
  next_offset: number | null;
  total: number;
  facets: {
    projects: ProjectRef[];
    engines: string[];
    /** Missions holding at least one session in scope (#948). Absent when the store was unreadable. */
    missions?: (SessionMissionRef & { count: number })[];
    /** Sessions in scope held by no mission (#948). */
    no_mission?: number;
  };
  /** A `mission` filter was requested but the mission store could not be read (#948). */
  mission_filter_unavailable?: boolean;
  /** The pinned scan to pass on the NEXT page (#1007 Phase 3). Present only when the request sent
   *  `snapshot`, and it may differ from what was sent (an expired pin is replaced, never an error). */
  snapshot?: string;
}

/** The mission that holds a session, as stamped on session rows (#948). */
export interface SessionMissionRef {
  id: string;
  title: string;
  state: string;
}

/** What a session BELONGS to (#361): a project entity, or the implicit folder group
 *  (pre-#361 behaviour — id is the cwd). Resolved server-side by the shared resolver. */
export interface ProjectRef {
  kind: "project" | "folder";
  /** Entity id (`p-…`) or the cwd for a folder ref — also the `project` filter value. */
  id: string;
  name: string;
  /** Entity color (#285 spends it); absent on folder refs. */
  color?: string;
  /** Scoped rows resolving to this ref — set on FACET refs only (#361 Phase 3),
   *  never on a session row's `project`. */
  count?: number;
}

/** A project entity from GET /api/projects (#361): what the Settings manager edits.
 *  `session_count` is the resolver's member count at read time (GET only — the
 *  create/patch responses omit it). */
export interface ProjectEntity {
  id: string;
  name: string;
  color: string;
  folders: string[];
  /** Default launch folder (#448): where new sessions in this project start unless overridden.
   *  Always one of `folders` (auto-adopted); "" for legacy folderless projects with none set. */
  default_folder: string;
  archived: boolean;
  created_at: number;
  session_count: number;
}

/** A directory from GET /api/fs/dirs — the folder picker's tree node (#448), bounded to ~/. */
export interface FsDir {
  name: string;
  path: string;
}

/** Bulk archive/unarchive report from POST /api/projects/{id}/(un)archive (#361 Phase 2).
 *  Idempotent + blindly retryable: re-calling after a partial failure retries only the
 *  failed members (the rest report `already_*`). Result keys mirror the direction:
 *  archived/already_archived/failed or unarchived/already_unarchived/failed. */
export interface ProjectArchiveReport {
  id: string;
  archived: boolean;
  sessions: { id: string; result: string; reason?: string }[];
  counts: Record<string, number>;
}

/** A launch-location folder from GET /api/folders — the pre-#361 "project" picker row.
 *  Folders stay where sessions LAUNCH; project entities are what sessions BELONG to. */
export interface Folder {
  cwd: string;
  label: string;
}

/** The forge the objective probes read (#891). PUBLIC view only — the token is write-only and
 *  surfaces as `token_set`, never as a value.
 *
 *  `configured` deliberately does NOT require a token: a public forge is readable without one, and
 *  demanding a credential the probes do not need would turn a working setup into a permanent
 *  `unknown`. */
export interface ForgeConfig {
  enabled: boolean;
  kind: string;
  base_url: string;
  owner: string;
  token_set: boolean;
  configured: boolean;
}

export interface AppConfig {
  /** CSRF token bound to the session cookie; sent as X-CSRF-Token on mutations. */
  csrf: string;
  /** Server hostname (#503), shown in the footer classbar. Absent on older servers. */
  hostname?: string;
  /** Absent on a server from before #891. */
  forge?: ForgeConfig;
  /** Engines that are installed AND can start a new session (drives the picker). */
  new_session_engines: string[];
  terminal_backend: "ttyd" | "ws" | string;
  /** First-run forced password change pending — the SPA routes to /change-password. */
  must_change_password?: boolean;
  /** First-run onboarding (#463): false ⇒ show the setup wizard (after the password gate);
   *  true once completed/skipped, or inferred true for an existing install. Absent on older
   *  servers (the SPA treats absent as onboarded so it never shows for them). */
  onboarded?: boolean;
  /** What's new (#971): the newest release whose notes were dismissed, `null` until then. Absent
   *  on a server from before #971 — and an absent key means "not due", so the dialog stays shut. */
  whats_new_seen?: string | null;
  /** Usage analytics (#1009): the operator's decision, and whether this server allows it at all
   *  (`available` is false under `AGENT_SESSIONS_ANALYTICS=0`). Absent on an older server. */
  analytics?: AnalyticsState;
  /** Per-user UI theme id (dark|light); applied at load. Absent on older servers. */
  theme?: string;
  /** Per-user brand accent (#rrggbb) driving --accent + the xterm cursor (#211 Phase 2);
   *  applied at load. Absent on older servers (→ client default phosphor-amber). */
  accent?: string;
  /** Terminal font size in px (#859), which on a phone IS the agent's column count.
   *  Seeds a device with no local choice; the localStorage cache wins over it. Absent on
   *  older servers (→ client default 13). */
  term_font_size?: number;
  /** Terminal font stack (#866) — the FACE, the second axis beside the size. Seeds a device
   *  with no local choice; the localStorage cache wins over it. Absent on older servers
   *  (→ client default: the system monospace stack). */
  term_font_family?: string;
  /** Compose box default on load: "auto" (device heuristic) | "open" | "collapsed". */
  compose_default?: "auto" | "open" | "collapsed" | string;
  /** Session-list sort order (#506): "recent_activity" (newest update first, default) or
   *  "created_at" (stable, newest-created first). Absent on older servers (→ recent_activity). */
  session_list_order?: "recent_activity" | "created_at" | string;
  /** Overview (#144): expanded cluster cwds (default collapsed). */
  overview_expanded?: string[];
  /** Hidden cwds (#174). NOT a global hide (#615). For every folder it withholds the folder as
   *  a launch location (the new-session picker). For a folder no project has adopted it also
   *  drops that folder's sessions from the sidebar list and the overview map. A folder adopted
   *  by a project entity keeps its sessions in the sidebar — the server's `_visible` exempts
   *  `kind: "project"` rows — and on the map only under `project` grouping (`folder`/`agent`
   *  grouping is cwd/engine-keyed, so a hidden cwd hides its sessions there, #424). Archive the
   *  project to hide an adopted folder's sessions. Nothing is removed from the project filter,
   *  which lists entities, not folders (#445). The legacy `overview_excluded` alias is retired
   *  (#357 Phase 2) — the server migrates old on-disk values into this key. */
  projects_hidden?: string[];
  /** Project-visibility mode (#335): "all" (legacy denylist, default) or "included" (curated
   *  allowlist — only `projects_included` cwds show; new dirs never auto-appear). Mode-exclusive:
   *  in "included" mode `projects_hidden` is ignored and `projects_included` is authoritative. */
  projects_mode?: "all" | "included" | string;
  /** The "included"-mode allowlist of visible project cwds (#335). Ignored in "all" mode. */
  projects_included?: string[];
  /** Preferred new-session PROJECT, by entity id (#615 Phase 2). The picker pre-selects it; an
   *  id that no longer names an unarchived project falls back to the first unarchived one, so a
   *  deleted or archived default degrades silently rather than erroring. "" / absent = no
   *  preference. Supersedes `default_project`. */
  default_project_id?: string;
  /** Legacy preferred new-session start directory (#335 Phase 2), superseded by
   *  `default_project_id`. Retained as the fallback for a start directory no project has adopted
   *  (the migration cannot map it to an id), and as Onboarding's seed. With a project selected
   *  its own `default_folder` (#448) wins, so this never fires. "" / absent = no preference. */
  default_project?: string;
  /** Base dirs under which the UI may create a new project folder (#335 Phase 3) AND the root
   *  scope for discovery (#465) — the merged effective list (prefs roots, else env fallback).
   *  Empty/absent ⇒ the "New folder" affordance is hidden, the mkdir endpoint is disabled, and
   *  discovery is unscoped (today's behaviour). UI-settable via setPrefs (#465). */
  project_roots?: string[];
  /** Manual exclusion list (#465): boundary-aware path prefixes dropped from discovery even when
   *  under a root (for ephemerals that slip past the ~/.cache/act filter). UI-settable. */
  folder_exclusions?: string[];
  /** Per-cwd custom project display names (#148). */
  project_names?: Record<string, string>;
  /** Auth mode: "single-user" (cookie login) or "none" (no login — self-host on a
   * trusted network). Lets the SPA hide login/logout UI. Absent on older servers. */
  auth_mode?: "single-user" | "none" | string;
  /** The operator's LOGIN NAME (#1058) — what the corner tile shows. Never a credential: the
   *  password hash, the secret key and the 2FA secret are not serialised anywhere in this payload.
   *  `null` under `auth_mode: "none"`, where there is no operator to name; absent on older
   *  servers, which renders no tile rather than a made-up one. */
  username?: string | null;
  /** Optional TOTP 2FA on/off (#116) — drives the Settings security section. Just the
   *  bit; the secret/recovery codes are never exposed here. Absent on older servers. */
  two_factor_enabled?: boolean;
  /** AI session review (#356): public config block (write-only key → `api_key_set`). */
  ai_review?: AiReviewConfig;
  /** AI auto-sort (#424 Phase 6): opt-in; reuses the ai_review endpoint (no secret). */
  auto_sort?: AutoSortConfig;
  /** Pulse recent-work overview (#441 Phase 3): opt-in background scan + window/depth;
   *  reuses the ai_review endpoint for synthesis (no secret). */
  pulse?: PulseConfig;
  orchestrator?: OrchestratorConfig;
  /** The mission playbooks (#883), so Settings can edit them (#892). Normalized server-side, so
   *  this is exactly the shape `POST /api/prefs` accepts back. */
  mission_playbooks?: MissionPlaybooks;
  /** What each probe kind takes, FROM THE SERVER'S OWN SCHEMA — never a second copy here. The
   *  editor offers the right fields per kind so an unknown argument is prevented rather than
   *  refused on save; validation itself stays entirely server-side. */
  mission_probes?: MissionProbeSchema;
}

export interface MissionPlaybookObjective {
  key: string;
  title: string;
  probe: string;
  probe_args: Record<string, unknown> | null;
  gate: boolean;
  /** Operator text typed when mission control nudges about this objective, with placeholders from
   *  the server's table (#983). Absent when the template has none. */
  direction?: string | null;
}

export interface MissionPlaybook {
  id: string;
  label: string;
  objectives: MissionPlaybookObjective[];
}

export interface MissionPlaybooks {
  /** The playbook a new mission gets when none is named. `""` means notes-only — NOT "the first
   *  one": substituting a playbook would arm gating objectives nobody chose (#883). */
  default_id: string;
  playbooks: MissionPlaybook[];
  /** SERVER-OWNED and monotonic. A save sends back the one it read; a mismatch is a 409 rather
   *  than a whole-block overwrite of whatever another tab did in the meantime (#900 review 5,
   *  finding 7). Never chosen by the client — the server increments its own. */
  revision?: number;
}

export interface MissionProbeSchema {
  kinds: string[];
  /** Probes that may never gate alone — "the agent believes it wrote tests" is not evidence. */
  non_gating: string[];
  args: Record<string, { required: string[]; optional: string[] }>;
  /** The JSON type each argument takes, keyed the same way. `"text"` means send a string;
   *  `"int"` means send a number, which is the only way `http_status.expect_status` can be
   *  authored at all (#900 review, finding 6). Absent on an older server — treat as `"text"`. */
  types?: Record<string, Record<string, string>>;
  /** Which checked facts a direction may name, per probe (#983 P2). Absent on an older server. */
  placeholders?: DirectionPlaceholder[];
}

/** TOTP enrollment payload (#116): shown once. The secret + recovery codes are never
 *  returned again after this response. */
export interface TwoFactorEnrollment {
  /** base32 TOTP secret (also encoded in otpauth_uri) for manual entry. */
  secret: string;
  /** otpauth://totp/... URI to render as a QR for the authenticator app. */
  otpauth_uri: string;
  /** One-time recovery codes — display once, never persisted by the SPA. */
  recovery_codes: string[];
}

/** One engine provider's discovery status (Settings → Connected agents). */
export interface EngineInfo {
  id: EngineId | string;
  present: boolean;
  supports_new: boolean;
  /** Handoff-target capability (#597) — THE capability source the handoff modal's engine
   *  tiles render from (the server rejects from the same source, so they can't diverge). */
  supports_seed_start: boolean;
  /** Why the engine can't be a handoff target (null exactly when it can). */
  seed_reason: string | null;
  bin: string | null;
}

/** One agent's usage, as the agent itself reports it (#839).
 *
 *  `source` is the whole contract, and the UI must not paper over it:
 *  - `plan`    — the agent's own quota percentage. Authoritative; the operator configures nothing.
 *  - `tokens`  — a token count from the engine's own store, meaningful only against `limit_tokens`.
 *  - `manual`  — the operator's own counter, for an agent that reports nothing.
 *  - `none`    — nothing reported and nothing configured. Not "0%".
 */
export interface AgentUsageWindow {
  label: string;
  used_pct: number;
  resets_at: number | null;
}

export interface AgentUsageRow {
  engine: string;
  source: "plan" | "tokens" | "manual" | "none";
  windows?: AgentUsageWindow[];
  tokens?: {
    in?: number;
    out?: number;
    cache_read?: number;
    cache_write?: number;
  } | null;
  window_days?: number | null;
  plan?: string | null;
  /** When the figures were taken. 0 = never asked. */
  at: number;
  /** When the last attempt happened, which differs from `at` after a failed refresh. */
  checked_at: number | null;
  /** Why the last refresh failed. The figures are still the last good ones. */
  error?: string | null;
  stale: boolean;
  limit_tokens: number;
  manual_used: number;
  /** The one number a threshold is tested against, or null when there isn't one. */
  used_pct: number | null;
}

export interface AgentBudgets {
  threshold_pct: number;
  notify: boolean;
  engines: Record<string, { limit_tokens?: number; manual_used?: number }>;
}

export interface AgentUsageResponse {
  agents: AgentUsageRow[];
  budgets: AgentBudgets;
  refreshing?: boolean;
}

export interface EnginesResponse {
  engines: EngineInfo[];
}

/** Host/system info (Settings → System). Every field is fail-soft server-side, so any
 *  of them may be absent depending on the platform / permissions. */
export interface SystemInfo {
  os?: string;
  platform?: string;
  arch?: string;
  python?: string;
  version?: string;
  hostname?: string;
  cpus?: number;
  load?: { "1": number; "5": number; "15": number };
  mem_total?: number;
  mem_available?: number;
  disk_total?: number;
  disk_free?: number;
  uptime_seconds?: number;
}

export interface UpdateInfo {
  current: string;
  channel: string;
  /** The channel's latest ref (highest v* tag on stable, main HEAD short SHA on main),
   *  or null when git/network is unavailable or no release tag exists yet. */
  latest: string | null;
  update_available: boolean;
  /** #931: the comparison could not be made — the remote HEAD or the running build's own
   *  release tag would not resolve. NOT the same as "up to date", and deliberately its own
   *  field rather than a third value of `update_available`: that flag keeps its type and its
   *  meaning (nothing auto-updates on a guess), while the panel gains a verdict it can render
   *  honestly instead of "You're on the latest". Absent means the comparison happened. */
  undetermined?: boolean;
  /** #538 additive fields (present on current servers; optional for back-compat). */
  auto_update?: boolean;
  last_auto?: UpdateLastAuto | null;
}

/** Recent-runtime status of the last scheduled auto-update pass (#538). In-memory on the
 *  server by design — resets on restart; a status hint, not an audit log. */
export interface UpdateLastAuto {
  ts: number;
  result: string;
}

/** The persisted update settings (#538): the Settings card's cheap read + POST shape. */
export interface UpdateSettings {
  auto_update: boolean;
  channel: string;
  last_auto?: UpdateLastAuto | null;
}

/** One page of older transcript history for scroll-up lazy-load (#348 Phase 3).
 *  `cursor` is a stable per-engine TURN index (never a rendered-line offset): pass it
 *  back as `before` to fetch the next-older page. `null` cursor + `has_more=false`
 *  means the oldest turn was reached (or the engine has no transcript at all). */
export interface HistoryPage {
  ansi: string;
  cursor: number | null;
  has_more: boolean;
}

export interface SessionsQuery {
  limit?: number;
  offset?: number;
  archived?: boolean;
  q?: string;
  project?: string;
  engine?: string;
  /** A mission id, or `none` for sessions no mission holds (#948). */
  mission?: string;
  /** Pin one server scan across a paging sequence (#1007 Phase 3): `new` on the first page, then
   *  the `snapshot` the previous page returned. Omitted (the sidebar), nothing is pinned. */
  snapshot?: string;
}

/** Seed-generation mode (#597): "quick" builds the tail locally; "ai" (Phase 2) asks the
 *  configured AI-review endpoint for a structured brief and degrades to "quick" when that
 *  endpoint is unconfigured or failing. */
export type HandoffMode = "quick" | "ai";

/** Cross-engine handoff (#597): the prepared seed — preview + the opaque server-side
 *  handle the commit step redeems. Nothing is spawned at prepare; cancel = let it expire.
 *  `meta.mode` is what was actually BUILT: an "ai" request that degraded reports "quick"
 *  with `degraded` + a human `notice` the modal shows. */
export interface HandoffPrepared {
  handle: string;
  preview: string;
  meta: {
    mode: string;
    turns: number;
    bytes: number;
    cap: number;
    requested_mode?: string;
    degraded?: boolean;
    notice?: string;
  };
}

/** The committed handoff target: navigate to /s/{engine}/{native} with fresh={cwd,bypass}
 *  and the normal launch path seeds the new session server-side (never via URL/argv). */
export interface HandoffCommitted {
  id: string;
  engine: string;
  native: string;
  cwd: string;
}

/** One entry from GET /api/files/list (#783). `kind` is the entry's OWN kind (lstat), so a
 *  symlink reports "link" and is display-only in phase 1 — never expanded, never opened.
 *  `link_kind` is present ONLY when `link_contained` is true: an uncontained target's kind is
 *  not resolved, let alone reported. */
export interface FileEntry {
  name: string;
  path: string;
  kind: "dir" | "file" | "link";
  size: number;
  mtime: number;
  link_target?: string | null;
  link_contained?: boolean;
  link_kind?: "dir" | "file" | null;
  /** The link is real but its TARGET is not valid UTF-8, so it cannot be shown. Distinct from a
   *  target that simply could not be read — this one exists and is unrepresentable. */
  link_unencodable_target?: boolean;
}

/** GET /api/files/list (#783). One canonical counting rule, shared with the server so the two
 *  cannot drift: `total` is a number **iff `complete`**; a scan stopped by the entry cap or the
 *  wall-clock budget reports `complete: false`, `total: null`, `truncated: true`. The dirs-first
 *  ordering therefore describes the entries actually RETURNED, not the directory. */
export interface FileListing {
  path: string;
  parent: string | null;
  root: string;
  entries: FileEntry[];
  total: number | null;
  complete: boolean;
  truncated: boolean;
  /** Entries omitted because their filename is not valid UTF-8 and cannot survive JSON. POSIX
   *  names are bytes; `scandir` surfaces undecodable ones as lone surrogates, which would
   *  otherwise make one file take down the whole listing. Reported so the response is honest
   *  about what it left out rather than quietly looking smaller than the directory. */
  unencodable?: number;
}

/** GET /api/files/read (#783). Binary files return metadata only — no content, ever. */
export interface FileContent {
  path: string;
  size: number;
  binary: boolean;
  mime?: string;
  content?: string;
  truncated?: boolean;
  /** #950: sha256 of the complete bytes — what a save is bound to. `null` when the read was
   *  truncated, because there is then nothing honest to save against. */
  version?: string | null;
  /** #950: whether the viewer may offer EDIT; when not, `readonly_reason` says why as a predicate
   *  ("is larger than 1 MiB …", "mixes CRLF and LF line endings"). */
  editable?: boolean;
  readonly_reason?: string | null;
  eol?: "\n" | "\r\n";
  bom?: boolean;
}

/** POST /api/files/write (#950) — a completed save. `retained` is the version it replaced, kept in
 *  the recovery store; null when the text was unchanged and nothing was written. */
export interface FileWriteResult {
  path: string;
  version: string;
  size: number;
  retained: { path: string; version: string } | null;
}

/** The body of a 409 from POST /api/files/write (#950), carried as `ApiError.record`. */
export interface FileWriteRefusal {
  detail: string;
  reason?: "changed" | "open_elsewhere" | "opened_during_save" | "too_large" | "not_editable";
  /** The version now on disk, when the refusal is `changed`. */
  version?: string;
  /** Best-effort name of the process holding the file open. Often absent; never required. */
  holder?: { pid: number; comm: string };
  /** Both surviving names, when a writer claimed the name mid-save and nothing was overwritten. */
  both?: string[];
}

/** GET /api/files/capabilities (#783) — the panel fails CLOSED when the platform cannot support
 *  the containment contract, rather than degrading to a weaker check. */
export interface FileCapabilities {
  ok: boolean;
  reason: string;
}

/** One changed path from GET /api/git/status (#784). A single porcelain record can yield TWO of
 *  these — an `MM` path is a staged edit AND a later unstaged one, which is real git state, so it
 *  renders in both groups with its own diff side rather than the UI picking a winner. */
export interface GitEntry {
  path: string;
  index: string;
  worktree: string;
  kind: "staged" | "changed" | "untracked" | "unmerged";
  oid: string | null;
  /** Rename/copy source, when the record carried one. */
  orig_path?: string;
  /** #950 review 4833: git reported this path (or its rename source) as bytes that are not
   *  UTF-8. The name shown is lossy, so two different files can look alike — the server refuses
   *  to write to it, and the panel offers nothing to write with. */
  undecodable?: boolean;
  /** What this row IS, not what it is called — see `entry_fingerprint` server-side. The panel
   *  echoes it back on a write so the operation acts on the bytes that were displayed; the
   *  session agent shares this worktree and never takes the panel's lock, so binding to the
   *  pathname alone meant "act on whatever is there when the command runs". */
  fp: string;
  /** #950: both sides' object ids and modes, as porcelain v2 reports them. Whether an index entry
   *  still holds the last commit's PARENT version is a `(mode, oid)` question. */
  oid_head?: string;
  oid_index?: string;
  mode_head?: string;
  mode_index?: string;
}

/** GET /api/git/status (#784). `repo: null` is a normal 200 — "not a repository" is a state.
 *  `ahead`/`behind` are **null when absent**, which is different from 0: detached HEAD and "no
 *  upstream" have no divergence to report, and level-with-upstream does. */
export interface GitStatus {
  repo: string | null;
  branch: string | null;
  upstream: string | null;
  ahead: number | null;
  behind: number | null;
  entries: GitEntry[];
  truncated: boolean;
  /** The whole staged SET as of this read. A commit records the index, not the listed rows, so
   *  this is what a commit binds to — a per-row check cannot see a file ADDED to the index. */
  staged_fp: string;
  /** The set of paths making the tree dirty — the precondition `switch` refuses on. */
  dirty_fp: string;
  /** #950: the commit HEAD is on (null on a branch with no commit yet) — what a write that must act
   *  on "the commit the operator was shown" sends back. */
  head?: string | null;
  /** #950: paths the last commit changed whose index entry still holds the parent's version —
   *  i.e. staged reversals of it. An unfinished panel commit and a reversal staged on purpose look
   *  identical, so this is reported, never acted on. `null` = could not be determined. */
  unsettled?: string[] | null;
  /** #950: paths the last commit changed whose index is AT the commit but whose worktree file still
   *  holds the parent's version — what a revert that could not write a file leaves. `null` = could
   *  not be determined (too many files, or one too large to hash). Reported, never acted on. */
  unsettled_worktree?: string[] | null;
}

/** POST /api/files/upload/batch (#807): a server-side reservation minted from an immutable
 *  manifest. ADMISSION only — an over-budget drop fails here before a byte moves; the bound that
 *  actually holds is charged per chunk while streaming. */
export interface UploadBatch {
  batch_id: string;
  files: number;
  bytes: number;
  files_limit: number;
  bytes_limit: number;
  file_limit: number;
}

/** POST /api/files/upload (#807): one file, landed. */
export interface UploadResult {
  path: string;
  name: string;
  relpath: string;
  bytes: number;
  batch: {
    batch_id: string;
    files_used: number;
    bytes_used: number;
    files_limit: number;
    bytes_limit: number;
  } | null;
}

/** GET /api/git/diff (#784). Assembled from `cat-file` blobs + the descriptor-verified worktree
 *  read — never `git diff`, because no flag stops a repo-configured `filter.*` clean driver.
 *  `added`/`removed` are **null when `truncated`**: a count taken from a cut-off prefix is not a
 *  total, and showing it as one would be a lie the UI could not detect. */
export interface GitDiff {
  path: string;
  repo: string;
  diff: string;
  added: number | null;
  removed: number | null;
  truncated: boolean;
  binary: boolean;
  too_large: boolean;
  /** Ours-vs-theirs, not worktree-vs-anything: the two sides of an unresolved merge. */
  conflict?: boolean;
  /** The pair exceeded the comparison budget, so this is a whole-block replacement rather than a
   *  line-by-line diff. Said out loud instead of passed off as a real diff. */
  coarse: boolean;
}

/** GET /api/git/branches (#806): what the branch menu lists. A **read**, so it comes off the read
 *  path's sanitized gitdir like every other read — the write side never grows its own listing. */
export interface GitBranches {
  repo: string | null;
  current: string | null;
  local: string[];
  remote: string[];
}

/** One RECENT COMMITS row (#950). */
export interface GitLogCommit {
  sha: string;
  short: string;
  subject: string;
  author: string;
  /** Author time, epoch seconds. */
  time: number | null;
  /** More than one is a merge, which the panel does not revert; zero is the branch's first commit. */
  parents: number;
  /** Whether the upstream already has it. ABSENT when there is no upstream to ask — "not known",
   *  never "not pushed". */
  pushed?: boolean;
}

/** GET /api/git/log (#950): the branch's first-parent history, newest first, off the read path's
 *  sanitized gitdir. The window it lists is also the only set of commits a revert may name. */
export interface GitLog {
  repo: string | null;
  branch: string | null;
  head: string | null;
  commits: GitLogCommit[];
}

/** GET /api/git/push-target (#806): the dry preflight the PUSH control renders *before* the
 *  operator commits to a write. Ambiguity comes back as `ok:false` + `candidates` rather than an
 *  error, because the control has to DRAW the refusal — enforcement still lives on the POST. */
export interface GitPushTarget {
  ok: boolean;
  reason: string | null;
  branch: string | null;
  remote: string | null;
  /** `origin/devopsagent/git-write` — the resolved name, never a hardcoded `origin`. */
  target: string | null;
  /** Opaque expectation the POST must echo back. Pins the destination the preflight RESOLVED,
   *  not just its label — `remote.<n>.pushurl` can move while `origin/master` stays true. */
  expect: string | null;
  candidates: string[];
  set_upstream: boolean;
}

/** Every `/api/git/*` write answers with the post-write status, so the panel settles from the
 *  server rather than from an optimistic guess (#806). A git operation that appears to have
 *  worked and did not is worse than a spinner. */
export interface GitWriteResult {
  /** `discard` only: `{path: [blob-oid, ...]}` for every version of the bytes that were
   *  REPLACED. The server displaces the file with `rename` before writing anything, so each set
   *  of bytes that occupied the name is in the object database — usually one, more than one only
   *  when something else was writing at the same moment. Recover with `git cat-file -p <oid>`. */
  recoverable?: Record<string, string[]>;
  /** `push` only: the commit the REMOTE received. Present even when local settlement failed. */
  pushed?: string;
  /** `push` and `pull`: whether the LOCAL bookkeeping finished after the durable change landed.
   *  `false` means the remote/branch update SUCCEEDED and only the local part did not — a retry
   *  is safe and idempotent, which is a different action from retrying a failed operation. */
  settled?: boolean;
  settle_error?: string | null;
  /** Null or absent when the post-write status read itself failed. The operation still happened —
   *  this says only that the panel could not re-read the repository afterwards, so the caller must
   *  KEEP the status it already had rather than adopting an absence as the new truth. #950 writes
   *  (`commit-paths`, `settle`, `discard`) omit it and say why in `status_error`. */
  status?: GitStatus | null;
  status_error?: string;
  /** Present on the operations that have something specific to report. */
  branch?: string;
  remote?: string;
  target?: string;
  set_upstream?: boolean;
  commit?: string;
  files?: number;
  paths?: string[];
  discarded?: string[];
  deleted?: string;
  created?: boolean;
  staged?: boolean;
  upstream?: string;
  /** #950 `commit-paths` / `settle` / revert-file: the full commit SHA. */
  sha?: string;
  /** #950: whether the real index caught up with the write. `pending` means the commit (or the
   *  worktree revert) happened and some index entries were left — see `index_left`. Deliberately
   *  not `settled`, which pull and push already use for a different fact. */
  index?: "settled" | "pending";
  index_left?: string[];
  index_reason?: string | null;
  /** #950: present, as `false`, only when the index write LANDED but syncing it to disk could not be
   *  confirmed (review 4833). Neither "not updated" nor a plain success: say so, with `index_reason`. */
  index_durable?: boolean;
  /** #950 revert-file: the staged blob each path had before, recoverable with `git cat-file -p`. */
  staged_recoverable?: Record<string, string>;
  reverted?: string[];
  /** #950 revert-commit: the commit that was undone (`sha` is the new commit that undoes it). */
  reverted_commit?: string;
  /** #950 revert-commit and revert-file: whether worktree restoration completed. `pending`
   *  lists incomplete paths in `worktree_left`; a path may have been left alone or partially
   *  restored. Inspect `worktree_reason` and `recoverable`, which carries versions already
   *  moved aside. For revert-file, `discarded` lists the paths successfully put back. */
  worktree?: "settled" | "pending";
  worktree_left?: string[];
  worktree_reason?: string | null;
  /** #950 settle: the worktree files SETTLE wrote the committed version of. */
  worktree_paths?: string[];
}

// ---------------------------------------------------------------------------------------------
// Missions (#846 Phase 1, #852/#862 Phase 2a). Shapes derived from the running store, not from
// the SQL — `missions.create_mission` / `get_mission` / `objectives` / `list_missions` were
// called directly and their output transcribed, so a field that does not exist cannot be typed
// here by wishful thinking.
// ---------------------------------------------------------------------------------------------

/** One entry in a mission's timeline. `kind` is `missions.EVENT_KINDS`. */
/** One durable operator turn (#871, wired in #890).
 *
 *  `state` is the whole reason this route exists rather than a second Ask box:
 *
 *  - `in_progress` — the model is running, or the turn is waiting on an action that has not
 *    settled. A reload finds it here rather than finding nothing.
 *  - `done` — settled; `answer` is the stored answer and a replay returns exactly this.
 *  - `indeterminate` — **nobody can say whether the instruction went out.** It is never retried
 *    automatically: a retry could be a second copy of an instruction the agent already has.
 *  - `failed` — settled with an honest failure outcome. */
export interface MissionTurn {
  turn_id: string;
  state: "in_progress" | "done" | "indeterminate" | "failed" | string;
  intent?: string | null;
  answer?: string | null;
  delivery_error?: string | null;
  matches?: PulseAskMatch[];
  actions?: OrchestratorAction[];
}

/** The timeline kinds the console knows by name. Open-ended (`string & {}`): the server adds kinds,
 *  and an unknown one still renders as a generic row rather than failing to type-check.
 *
 *  `planning` and `plan_edit` are #967 P2's. A `planning` event closes or annotates one planning
 *  attempt (`MissionPlanningMeta`); a `plan_edit` records WHICH fields an operator's edit changed and
 *  never carries the brief (`MissionPlanEditMeta`, no `text`). */
export type MissionEventKind =
  | "operator_msg"
  | "assistant_msg"
  | "state"
  | "plan"
  | "plan_edit"
  | "planning"
  | (string & {});

/** `meta` of a `planning` event (#967). `discarded` is a settlement the generation fence refused
 *  because a newer attempt or the operator's own save owned the mission by then. */
export interface MissionPlanningMeta {
  outcome:
    | "skipped"
    | "failed"
    | "discarded"
    | "recovered"
    | "project_conflict"
    | (string & {});
  generation?: number;
}

/** `meta` of a `plan_edit` event (#967): the plan it produced and the fields that changed. */
export interface MissionPlanEditMeta {
  plan_id: string;
  changed: string[];
}

export interface MissionEvent {
  seq: number;
  mission_id: string;
  at: number;
  kind: MissionEventKind;
  session_key: string | null;
  action_id: string | null;
  text: string | null;
  meta: Record<string, unknown> | null;
  /** The IMMUTABLE settlement projection frozen onto a decision event (#840). Present only on
   *  decision events; a stored answer that changes is not a stored answer. */
  settlement: Record<string, unknown> | null;
}

/** An objective. `state` is never written by the client — an operator edit may add, drop,
 *  retitle, waive or reorder, and can never mark something met (#840). */
export interface MissionObjective {
  mission_id: string;
  key: string;
  ord: number;
  title: string;
  probe: string;
  probe_args: Record<string, unknown> | null;
  gate: boolean;
  state: string;
  met_at: number | null;
  /** The last thing a probe actually saw, with when it saw it. Rendered as STALE rather than
   *  as current when the probe could not run — nothing is marked met or failed on data the
   *  server could not fetch. */
  observed: Record<string, unknown> | null;
  source: string;
  /** This mission's direction for the objective (#983): copied from the playbook (`template`) or
   *  written for this mission (`operator`). Null when a nudge would type the default nudge. */
  direction?: string | null;
  direction_source?: "template" | "operator" | null;
}

/** A row from `GET /api/missions` — the LIST shape.
 *
 *  It carries `session_count`, **not** `sessions`. That is not an omission: a list row has never
 *  had a roster, and the console originally iterated `m.sessions` on one, which threw on any
 *  non-empty production list while every unit mock hid it by supplying the array. The two shapes
 *  are separate types so consuming a detail-only field from a list row does not compile. */
export interface MissionListRow {
  id: string;
  title: string;
  project_id: string | null;
  cwd: string | null;
  state: string;
  created_at: number;
  updated_at: number;
  closed_at: number | null;
  archived_at: number | null;
  outcome: string | null;
  /** Active memberships (`removed_at IS NULL`) — the KEYS, not the roster rows.
   *
   *  Both of the console's questions are this one fact: how many sessions the mission holds (the
   *  rail's line) and which sessions are held at all, so a session another mission owns is not
   *  offered as untracked. A bare count answers the first and gets the second silently wrong. */
  session_keys: string[];
  /** DERIVED at read time from the ledger, never stored (#840). */
  needs_you?: boolean;
  needs_you_why?: string[];
}

/** A row from `GET /api/missions/{id}` — the DETAIL shape, which does carry the roster. */
/** One objective as the FOLLOW-THROUGH supervisor sees it (#885). Distinct from
 *  `MissionObjective`, which is the objective itself: this is what the supervisor is allowed to
 *  do about it next, and why. */
export interface SupervisorObjective {
  key: string;
  title: string | null;
  gate: boolean;
  state: string;
  met: boolean;
  /** Bumped when the objective goes round again; the nudge budget is PER EPISODE, so a new
   *  episode restores it. */
  episode: number;
  /** The operator asked not to be told about this objective again. */
  stood_down: boolean;
  /** A question is open against this objective, so the supervisor has stood it down UNTIL IT IS
   *  ANSWERED. Distinct from `stood_down`, and the server keeps them apart for the reader's sake:
   *  one says the operator asked for quiet, this one says the operator owes an answer. */
  awaiting_answer: boolean;
  spent: number;
  remaining: number;
  may_nudge: boolean;
  /** The ledger could not be READ. Distinct from a spent budget: the server reports
   *  `remaining: 0` in this case because none can be justified, so the number alone cannot tell
   *  "unknown" from "exhausted" — read this, never infer it. */
  unreadable: boolean;
  /** A nudge may or may not have been delivered. Charged and terminal. */
  indeterminate: boolean;
  /** How many nudges for this episode are still in flight. */
  live: number;
  /** Whether the refusal ENDS the episode (exhausted or indeterminate) as opposed to merely
   *  describing right now (live, or an unreadable ledger). Only a terminal refusal escalates. */
  terminal: boolean;
  /** The server's OWN sentence for the refusal, rendered verbatim. Never re-derived here: the
   *  supervisor's rule is that every refusal names itself, and a second copy of that vocabulary
   *  in the client is a second thing to keep in step. */
  why_not: string;
}

export interface MissionSupervisor {
  objectives: SupervisorObjective[];
  /** A PROPOSAL that the mission looks finished — never a close, and false for a mission with no
   *  objectives, which is unmeasured rather than done. */
  likely_done: boolean;
  unmet_gates: number;
  /** How many objectives GATE completion. `unmet_gates: 0` reads the same whether every gate
   *  passed or there was never one to pass, and only one of those is progress — so the board
   *  cannot word itself honestly without this (#1063). Optional: a client may be rendering a
   *  response from a server that predates it. */
  gates?: number;
  /** How many sessions the mission currently holds, and the discriminator derived from it.
   *
   *  `no_session` is NOT "idle" and NOT "stalled" — those are claims about an agent. This is a
   *  claim about the MISSION: there is no agent for it to be either. Releasing the last session
   *  from a `running` mission reaches that state, and the board has to say so rather than paint
   *  a follow-through it cannot perform (#896 review 7, finding 2). */
  held_sessions?: number | null;
  no_session?: boolean;
  /** The roster could not be READ. Not the same claim as `no_session`, and the difference is the
   *  point: "we could not look" is not "there is nothing there". Both cannot be true. */
  sessions_unreadable?: boolean;
  checked_at: number;
}

export interface Mission {
  id: string;
  title: string;
  instruction: string | null;
  brief: string | null;
  project_id: string | null;
  cwd: string | null;
  engine: string | null;
  engine_source: string | null;
  state: string;
  playbook_id: string | null;
  created_at: number;
  updated_at: number;
  closed_at: number | null;
  archived_at: number | null;
  archiving_at: number | null;
  unarchiving_at: number | null;
  outcome: string | null;
  /** How the objective PRODUCER finished, or `pending` while it is still running (#883).
   *
   *  Load-bearing for the console's honesty: `POST /api/missions` returns 201 before the model
   *  has been asked, so a mission legitimately has an empty checklist for a moment. Rendering
   *  that as "no objectives" states a fact the server has not established. `null` is a mission
   *  from before the producer existed; `skipped` and `failed` each carry their own reason into
   *  the timeline.
   *
   *  The PLAN CARD reads it for the same reason (#904 review 2, finding 8): "nothing required"
   *  and "not worked out yet" are different things to approve a launch against. */
  objectives_state?: "pending" | "done" | "failed" | "skipped" | null;
  objectives_at?: number | null;
  /** How PLANNING stands (#967 P2). A new mission plans itself, so this is `pending` from the create
   *  transaction until the attempt settles: `ready` (a plan was written, in the same transaction),
   *  `failed` (with `plan_detail`), or `skipped` (no AI endpoint). A mission from before the column
   *  was backfilled `ready` if it had a plan and `skipped` otherwise, with no `plan_detail`.
   *
   *  `pending` is a server-side fence as well as a label: DISPATCH answers 409 while it holds. */
  plan_state?: "pending" | "ready" | "failed" | "skipped" | null;
  /** The planning ATTEMPT. Every intent (create, Plan again, a manual first plan) takes a new one. */
  plan_generation?: number;
  /** Why the last attempt settled `failed` or `skipped`. Null otherwise. */
  plan_detail?: string | null;
  plan_at?: number | null;
  /** What PLAN MANUALLY may choose from (#967 P2b). Present only while the mission has no plan and
   *  planning settled `skipped` or `failed` — exactly the states `PATCH /plan` accepts a first plan
   *  in. Once a plan exists the lists ride on it (`MissionPlan.project_options`). */
  plan_options?: {
    project_options: { id: string; name: string; cwd: string }[];
    engine_options: { id: string; label: string }[];
  } | null;
  sessions: MissionSession[];
  objectives?: MissionObjective[];
  /** How many LIVE sub-agents this mission may hold (#894). A server constant, echoed so the
   *  console can show "n of N used" and withhold a control that could only 409. The client never
   *  enforces it — `claim_spawn` does, inside the transaction that reserves the slot. */
  spawn_cap?: number;
  /** Where a sub-agent would run, server-resolved. `null` when the project no longer resolves. */
  spawn_cwd?: string | null;
  /** Which agent a sub-agent would be, derived from the session this mission holds. */
  spawn_engine?: string | null;
  /** Sub-agents started and not proven stopped — the SAME count the claim enforces.
   *  Absent (not 0) when the server could not read it: an unknown budget is not an empty one. */
  spawn_live?: number;
  /** The supervisor's mechanical reading, attached at read time (#885). ABSENT when the
   *  assessment could not be produced — never an empty reading, which would render as
   *  "nothing to follow up" and is a different claim from "we could not look". */
  supervisor?: MissionSupervisor;
  events?: MissionEvent[];
  /** Cursor for the NEXT page of older events, or null when the first page is all of them. */
  events_next_seq?: number | null;
  /** DERIVED at read time from the ledger, never stored (#840). */
  needs_you?: boolean;
  needs_you_why?: string[];
  /** The turn the operator is still owed an outcome for, from the STORE (#890). Absent/null when
   *  there is none. This is what makes "still working" and an ambiguous turn survive a reload —
   *  a durable turn whose only representation is component state is not durable. */
  /** The question awaiting an answer, or null (#892). Carried on the mission rather than found
   *  in `events`, which is paginated — a question older than the newest page would disappear
   *  from the console while `needs_you` still said the mission wanted an answer. */
  question?: MissionQuestion | null;
  turn?: MissionOpenTurn | null;
  /** The dispatch PROPOSAL, when there is one (#893). Absent/null when the mission has not been
   *  planned. It survives a reload because it is a row, not component state — the operator reads
   *  it, edits it, and decides. */
  plan?: MissionPlan | null;
  /** May this failed mission be started again (#966)? Computed by `get_mission` from the same
   *  predicate the state write re-checks in its transaction. The failure EVENT carries a
   *  `retry_eligible` too; that one is a settle-time snapshot and never decides a control. */
  retry_eligible?: boolean;
  /** What the failed launch typed: `not_attempted`, `zero_write`, `partial`, `delivered`, `unknown`. */
  seed_outcome?: string | null;
  /** Why Start again is refused, set only while the mission is failed and not eligible. */
  retry_reason?: string | null;
}

/** An unresolved turn: still running, or terminal-and-ambiguous. `done` never appears here —
 *  a finished turn's answer is a timeline event, which is where finished turns live. */
export interface MissionOpenTurn {
  turn_id: string;
  state: "in_progress" | "indeterminate";
  /** The operator's own words, read back from the event the claim wrote. */
  text: string;
  delivery_error: string;
  created_at: number;
}

/** A proposal: which project, which agent, what the agent will be told — and why that agent.
 *
 *  `plan_id` is the identity DISPATCH compares against. Every edit mints a new one, so a dispatch
 *  approved against the version on screen a minute ago cannot run the version typed since. */
export interface MissionPlan {
  plan_id: string;
  mission_id: string;
  project_id: string | null;
  /** Resolved SERVER-SIDE from the project entity. The client never sends a path. */
  cwd: string | null;
  engine: string | null;
  /** Why this agent, in the model's words — empty when the operator chose it themselves. */
  engine_reason: string;
  brief: string;
  created_at: number;
  /** What an override may choose from. Current as of the read, not as of the plan. */
  project_options?: { id: string; name: string; cwd: string }[];
  engine_options?: { id: string; label: string }[];
  /** Fields the model's reply could not supply, so the card can say why one is empty rather
   *  than looking merely unfilled. */
  dropped?: string[];
}

/** A bounded choice the supervisor is waiting on (#892).
 *
 *  `label` is DISPLAY TEXT and carries no authority: the client sends back the option's INDEX,
 *  and the server looks the action up from its own closed set. Nothing the model wrote is ever
 *  executed, which is why the label is never sent back.
 *
 *  **That is not the whole threat, and `consequence` is the rest of it.** The model authors the
 *  label AND picks the action, and the operator sees only the label — so a label reading "Keep
 *  working; leave this required" over a hidden `waive_objective` obtains a confirmation under
 *  false pretences (#900 review 2, finding 1). `consequence` is the SERVER's own sentence about
 *  what the option does, and `settling` marks the ones that change something beyond the
 *  timeline. Both are server-owned; neither can be model-authored. */
export interface MissionQuestion {
  seq: number;
  question: string;
  objective?: string | null;
  episode?: number | null;
  options: {
    label: string;
    action: string;
    consequence?: string;
    settling?: boolean;
  }[];
}

export interface MissionSession {
  mission_id?: string;
  session_key: string;
  role?: string | null;
  added_at?: number;
  removed_at?: number | null;
}

/** What one held session is doing right now (#1064), from `GET /api/missions/{id}/now`. Derived
 *  facts only — the server never sends screen text on this route. `unobserved` is NOT quiet: it
 *  means this process has seen no output for the session, so it cannot say. */
export type MissionNowStatus = "producing" | "at_prompt" | "quiet" | "unobserved";

export interface MissionNowSession {
  session_key: string;
  status: MissionNowStatus;
  seconds_since_output: number | null;
  /** Set only while `at_prompt`: `choice` / `confirm` / `question`. */
  prompt_class: string | null;
  recap_age_s: number | null;
  /** The last recap was written before the latest output, so it describes an earlier screen. */
  recap_older_than_output: boolean;
}

export interface MissionNow {
  sessions: MissionNowSession[];
  checked_at: number;
}

export interface MissionList {
  missions: MissionListRow[];
  total: number;
  limit: number;
  offset: number;
  facets: { projects: string[]; states: string[] };
  /** A locked or corrupt store empties the rail and SAYS WHY rather than taking the console
   *  down — the same fail-soft the opencode reader gives the sidebar. Never conflate this with
   *  "you have no missions": one is a store that would not answer, the other is an answer. */
  store_error?: string | null;
  /** A digest of the ORDERED ids of the full filtered set this page was sliced out of (#896
   *  review 19). Offsets only compose if the thing they index has not moved, and a client
   *  stitching several pages had no way to establish that: deduplication catches a reorder and
   *  cannot catch a removal, so an archive between two pages leaves a rail that is
   *  count-consistent, duplicate-free, holding a stale row and permanently missing a live one.
   *
   *  Equal digests across two pages is a proof rather than a heuristic. **A missing one is not a
   *  match** — a degraded read carries `null`, and the honest reading of that is "cannot prove
   *  one snapshot". */
  snapshot?: string | null;
}

/** Project, cwd, branch, git summary and session roster for one mission. Takes NO client path —
 *  the cwd is read from the mission row, which is what makes it traversal-proof (#862). */
export interface MissionContext {
  id: string;
  project_id: string;
  cwd: string;
  sessions: MissionSession[];
  git: GitStatus | null;
  /** The exception KIND only, never the path or message — the panel says "git could not be
   *  read" without leaking where. Fails closed: `git` stays null. */
  git_error: string | null;
}

/** Usage analytics (#1009). `decided` false means the operator was never asked or never answered —
 *  which is off. */
export interface AnalyticsState {
  enabled: boolean;
  decided: boolean;
  available: boolean;
}
