/** Automations (#1201): the pure half of the page — words, summaries, and the form ↔ API mapping.
 *
 *  Everything the operator is asked to CONSENT to comes from the server (`scope_lines`,
 *  `scope_digest`, `widened`); nothing here re-derives a scope. What lives here is presentation
 *  (a state's word and tone, a cadence in words, the strip's counts) and the one mapping from the
 *  editor's flat form to the nested body the routes validate strictly — so a field the form does not
 *  own is never sent, and an unknown one can never be. */
import { ApiError } from "./api";
import {
  engineInfo,
  familyOf,
  isActive,
  runsInTerminal,
} from "../app/engineRoster";
import type {
  Automation,
  AutomationAction,
  AutomationConfig,
  AutomationMessage,
  AutomationPolicy,
  AutomationRun,
  AutomationState,
  Autonomy,
  Cadence,
  ConditionOp,
  ConditionScalar,
  ConsentRequired,
  DeliveryMapping,
  MappedType,
  ResultClass,
  ReviewLoopAction,
  RunSummary,
  StripDay,
  Trigger,
  UnattendedEngine,
  Weekday,
  WebhookCondition,
} from "../types/automations";
import type { Template } from "../types/api";
import type { Specialist } from "../types/specialists";
import type { ShapeField, WebhookPreset } from "../types/webhooks";

/** A status tone, which is what decides the LED and the word's colour. `down` is ACTIVE failure
 *  only (docs/design.md §7); `degraded` is "needs you"; `idle` is off, skipped or unknown. */
export type Tone = "up" | "degraded" | "down" | "idle";

export const STATE_WORD: Record<AutomationState, { word: string; tone: Tone }> =
  {
    enabled: { word: "Enabled", tone: "up" },
    paused: { word: "Paused", tone: "degraded" },
    needs_reapproval: { word: "Needs re-approval", tone: "degraded" },
    erroring: { word: "Erroring", tone: "down" },
    off: { word: "Off", tone: "idle" },
    expired: { word: "Expired", tone: "idle" },
    finished: { word: "Finished", tone: "idle" },
    unreadable: { word: "Unreadable", tone: "down" },
  };

export function stateWord(state: string): { word: string; tone: Tone } {
  return (
    STATE_WORD[state as AutomationState] ?? {
      word: state || "Unknown",
      tone: "idle",
    }
  );
}

/** Whether an automation is waiting on the operator — the "why is something not running?" panel.
 *  A `check_note` (its inputs could not be checked just now) counts: it is why a slot did not run. */
export function needsYou(a: Automation): boolean {
  return (
    a.state === "paused" ||
    a.state === "needs_reapproval" ||
    a.state === "erroring" ||
    !!a.check_note
  );
}

/** An API refusal in the operator's words. The server's `detail` is kept; the status adds what it
 *  means for them: gone (404), too large (413), or not right now (503). */
export function errorWords(e: unknown, fallback = "That didn’t work."): string {
  if (!(e instanceof ApiError))
    return e instanceof Error ? e.message : fallback;
  if (e.status === 404)
    return "It no longer exists — it may have been deleted.";
  if (e.status === 413) return "That is too large to save.";
  if (e.status === 503) return `${e.message} — try again in a moment.`;
  return e.message || fallback;
}

/** The amber note a change's confirmation carries when a run already past its last check may
 *  still complete (`in_flight`). `""` when the change has no later effect to warn about. */
export function inFlightNote(
  r: { in_flight?: boolean; in_flight_detail?: string } | null,
): string {
  if (!r?.in_flight) return "";
  return `Note: ${r.in_flight_detail || "a run already in progress may still complete"}.`;
}

/** The words for one run's outcome. A run still `dispatching` has no outcome yet. `interrupted`
 *  is amber, not red: its outcome is UNKNOWN, which is a thing to look at, not a failure. */
export function outcomeWord(
  outcome: string,
  state?: string,
): { word: string; tone: Tone } {
  if (!outcome)
    return {
      word: state === "dispatching" ? "Starting" : "Pending",
      tone: "idle",
    };
  switch (outcome) {
    case "ok":
      return { word: "OK", tone: "up" };
    case "started":
      return { word: "Running", tone: "idle" };
    case "review":
      return { word: "In review", tone: "degraded" };
    case "failed":
      return { word: "Failed", tone: "down" };
    case "refused":
      return { word: "Refused", tone: "down" };
    case "interrupted":
      return { word: "Interrupted", tone: "degraded" };
    case "skipped":
      return { word: "Skipped", tone: "idle" };
    case "stopped":
      return { word: "Stopped", tone: "idle" };
    case "partial":
      // Something may be sitting unsubmitted in front of the agent: an active failure to look at.
      return { word: "Typed, not sent", tone: "down" };
    // The review loop (#2202). `ignored` is a delivery deliberately not acted on (grey, like a
    // skip); `capped` is the item's last review used up, which the operator is told about (amber);
    // `undelivered` is a verdict that never reached its session (counted as failed on the strip).
    case "ignored":
      return { word: "Ignored", tone: "idle" };
    case "capped":
      return { word: "Capped", tone: "degraded" };
    case "undelivered":
      return { word: "Not delivered", tone: "down" };
    default:
      return { word: outcome, tone: "idle" };
  }
}

/** The words beside the 14-day strip. They count RUNS, not days (#1201 mockups v2), so a day with
 *  three runs adds three and a mixed day is never hidden behind its worst square. */
export function stripWords(stats: Automation["stats"]): string {
  const parts: string[] = [];
  if (stats.ok) parts.push(`${stats.ok} ok`);
  if (stats.failed) parts.push(`${stats.failed} failed`);
  if (stats.skipped) parts.push(`${stats.skipped} skipped`);
  if (stats.pending) parts.push(`${stats.pending} in progress`);
  return parts.length ? parts.join(" · ") : "no runs";
}

/** One square's accessible word: what the day's worst outcome was, or that nothing was due. */
export function dayWord(day: StripDay): string {
  const w: Record<ResultClass, string> = {
    ok: "ok",
    failed: "failed",
    skipped: "skipped",
    pending: "in progress",
  };
  return `${day.date}: ${day.worst ? w[day.worst] : "nothing ran"}`;
}

// ---- cadence and trigger words ---------------------------------------------------------------------

export const WEEKDAYS: Weekday[] = [
  "mon",
  "tue",
  "wed",
  "thu",
  "fri",
  "sat",
  "sun",
];
const DAY_NAME: Record<Weekday, string> = {
  mon: "Mon",
  tue: "Tue",
  wed: "Wed",
  thu: "Thu",
  fri: "Fri",
  sat: "Sat",
  sun: "Sun",
};

function ordinal(n: number): string {
  const s =
    n % 100 >= 11 && n % 100 <= 13
      ? "th"
      : (["th", "st", "nd", "rd"][n % 10] ?? "th");
  return `${n}${s}`;
}

export function cadenceWords(c: Cadence): string {
  switch (c.kind) {
    case "interval":
      return c.unit === "minutes"
        ? `Every ${c.every} min`
        : `Every ${c.every} h`;
    case "daily":
      return `Daily ${c.time}`;
    case "weekly": {
      const days = WEEKDAYS.filter((d) => c.days.includes(d));
      if (days.length === 7) return `Daily ${c.time}`;
      if (days.join() === "mon,tue,wed,thu,fri") return `Weekdays ${c.time}`;
      if (days.join() === "sat,sun") return `Weekends ${c.time}`;
      return `${days.map((d) => DAY_NAME[d]).join(", ")} ${c.time}`;
    }
    case "monthly":
      return `Monthly on the ${ordinal(c.day)} ${c.time}`;
  }
}

/** A once's `at` (a local wall time in its own zone) in words — parsed, never reinterpreted in the
 *  browser's zone: "2026-10-01T09:00" is 09:00 in `tz`, whatever the viewer's clock says. */
export function onceWords(at: string): string {
  const m = /^(\d{4})-(\d{2})-(\d{2})T(\d{2}:\d{2})$/.exec(at);
  if (!m) return at;
  const [, y, mo, d, hm] = m;
  const date = new Date(Date.UTC(Number(y), Number(mo) - 1, Number(d)));
  const wd = date.toLocaleDateString("en-GB", {
    weekday: "short",
    timeZone: "UTC",
  });
  const mon = date.toLocaleDateString("en-GB", {
    month: "short",
    timeZone: "UTC",
  });
  return `${wd} ${d} ${mon} ${y} ${hm}`;
}

/** A #2202 review loop's own webhook (`scheme`), not one picked from Library → Webhooks (#2221).
 *  It lives on at its old address; the UI never converts it, and the server refuses a new one. */
export function isLegacyHook(t: Trigger | null | undefined): boolean {
  return t?.kind === "webhook" && !("webhook_id" in t);
}

export function triggerWords(t: Trigger | null): {
  label: string;
  detail: string;
} {
  if (!t) return { label: "Unreadable", detail: "its settings can't be read" };
  if (t.kind === "manual")
    return { label: "Manual", detail: "only when you press Run now" };
  if (t.kind === "once")
    return { label: "Once", detail: `${onceWords(t.at)} · ${t.tz}` };
  if (t.kind === "webhook")
    return isLegacyHook(t)
      ? { label: "Forgejo webhook", detail: "when Forgejo delivers a signed event" }
      : { label: "Webhook", detail: "when its webhook delivers a signed event" };
  return { label: "Schedule", detail: `${cadenceWords(t.cadence)} · ${t.tz}` };
}

/** #2152: HQ plans missions, so a mission automation only creates a draft. The dispatch modes stay
 *  readable for an automation saved before that; the server refuses saving or running them. */
export const AUTONOMY_WORDS: Record<Autonomy, string> = {
  propose: "Creates the draft; you plan it with HQ",
  dispatch: "Dispatches the plan without asking (retired: HQ plans missions)",
  dispatch_auto_choose:
    "Dispatches and may answer menus on its own (retired: HQ plans missions)",
};

export function actionLabel(
  kind: AutomationAction["kind"] | undefined,
): string {
  if (kind === "start_mission") return "Start mission";
  if (kind === "start_session") return "Start session";
  if (kind === "send_to_session") return "Send to session";
  if (kind === "forge_review_loop") return "Review loop";
  if (kind === "tell_hq") return "Tell HQ";
  return "Unreadable";
}

/** The last path component, for a folder shown in a narrow cell. */
export function baseName(path: string): string {
  return path.split("/").filter(Boolean).at(-1) ?? path;
}

export function messageWords(
  m: AutomationMessage,
  templateName?: (id: string) => string,
): string {
  if ("text" in m) return "Plain text";
  return `Template ${templateName ? templateName(m.template_id) : m.template_id}`;
}

/** Where the automation's work lands, for the line under its name. */
export function targetWords(
  action: AutomationAction | null,
  projectName: (id: string) => string,
): string {
  if (!action) return "";
  if (action.kind === "start_mission") return projectName(action.project_id);
  if (action.kind === "start_session") return baseName(action.folder);
  if (action.kind === "forge_review_loop")
    return projectName(action.project_id);
  if (action.kind === "tell_hq") return "your notifications";
  return action.session_key;
}

// ---- time words ----------------------------------------------------------------------------------

/** "in 9 h 12 min", "in 4 min", "due now". */
export function untilWords(at: number, nowS: number): string {
  const s = Math.round(at - nowS);
  if (s <= 30) return "due now";
  const m = Math.round(s / 60);
  if (m < 60) return `in ${m} min`;
  const h = Math.floor(m / 60);
  if (h < 48) return m % 60 ? `in ${h} h ${m % 60} min` : `in ${h} h`;
  return `in ${Math.round(h / 24)} days`;
}

/** A time the viewer reads in their own zone: "03:00 today", "03:00 tomorrow", "Tue 29 Sep 03:00". */
export function whenWords(at: number, nowS: number): string {
  const d = new Date(at * 1000);
  const now = new Date(nowS * 1000);
  const hm = d.toLocaleTimeString("en-GB", {
    hour: "2-digit",
    minute: "2-digit",
  });
  const day = (x: Date) => `${x.getFullYear()}-${x.getMonth()}-${x.getDate()}`;
  if (day(d) === day(now)) return `${hm} today`;
  const tomorrow = new Date(now);
  tomorrow.setDate(now.getDate() + 1);
  if (day(d) === day(tomorrow)) return `${hm} tomorrow`;
  const yesterday = new Date(now);
  yesterday.setDate(now.getDate() - 1);
  if (day(d) === day(yesterday)) return `${hm} yesterday`;
  const wd = d.toLocaleDateString("en-GB", {
    weekday: "short",
    day: "2-digit",
    month: "short",
  });
  return `${wd} ${hm}`;
}

/** The "next run" cell: the time and a sub-line, or a dash and WHY nothing is scheduled. */
export function nextRunWords(
  a: Automation,
  nowS: number,
): { main: string; sub: string } {
  if (a.next_run)
    return {
      main: whenWords(a.next_run.at, nowS),
      sub: untilWords(a.next_run.at, nowS),
    };
  switch (a.state) {
    case "off":
      return {
        main: "—",
        sub: a.consented_at == null ? "never enabled" : "turned off",
      };
    case "paused":
      return { main: "—", sub: a.paused_reason || "paused" };
    case "needs_reapproval":
      return {
        main: "—",
        sub: a.reapproval_reason || "needs your approval again",
      };
    case "expired":
      return { main: "—", sub: "past its end date" };
    case "finished":
      return { main: "—", sub: "its one run is done" };
    case "unreadable":
      return { main: "—", sub: "its settings can't be read" };
    default:
      if (a.trigger?.kind === "webhook")
        return {
          main: "On delivery",
          sub: isLegacyHook(a.trigger)
            ? "when Forgejo sends an event"
            : "when its webhook delivers an event",
        };
      return a.trigger?.kind === "manual"
        ? { main: "On Run now", sub: "no schedule" }
        : { main: "—", sub: "nothing due" };
  }
}

/** Why this automation isn't running — the sentence the "why is something not running?" panel
 *  shows, built from the server's own reasons. */
export function whyNotRunning(a: Automation): string {
  const note = a.check_note ? `${capital(a.check_note)}.` : "";
  return [whyNotRunningState(a), note].filter(Boolean).join(" ");
}

function whyNotRunningState(a: Automation): string {
  if (a.state === "needs_reapproval")
    return `${a.reapproval_reason || "An approved input changed"}. It stays paused until you review the change and approve it again.`;
  if (a.state === "paused") {
    const last = a.last_run?.reason
      ? ` The last run: ${a.last_run.reason}.`
      : "";
    return `${capital(a.paused_reason || "Paused")}.${last}`;
  }
  if (a.state === "erroring")
    return `${a.consecutive_failures} failed ${a.consecutive_failures === 1 ? "run" : "runs"} in a row${a.last_run?.reason ? `; the last one: ${a.last_run.reason}` : ""}. It pauses after ${a.policy?.pause_after_failures ?? 3}.`;
  return "";
}

function capital(s: string): string {
  return s ? s[0].toUpperCase() + s.slice(1) : s;
}

/** Whether Run now can be offered, and why not. The server refuses the same cases (409); this only
 *  keeps the button honest. */
export function runNowBlock(a: Automation, loopEnabled: boolean): string {
  // A review loop has no Run now (#2202): only a delivery names the item to review.
  if (a.trigger?.kind === "webhook")
    return "It runs only when its webhook delivers an event";
  if (!loopEnabled) return "Automations are switched off on this server";
  if (a.consented_at == null)
    return "Enable it first — it has never been approved";
  if (a.needs_reapproval) return "It needs your approval again";
  if (!a.enabled) return "It is turned off";
  if (a.state === "unreadable") return "Its settings can't be read";
  return "";
}

// ---- the run timeline ----------------------------------------------------------------------------

const STEP_WORDS: Record<string, string> = {
  claimed: "Slot claimed",
  skipped: "Skipped",
  creating_mission: "Creating the mission",
  mission_created: "Mission created",
  dispatched: "Dispatched",
  auto_choose: "Menu answers opted in",
  auto_choose_refused: "Menu answers refused",
  launched: "Launched",
  started: "Started",
  briefed: "Briefed",
  bound: "Bound",
  ok: "Done",
  failed: "Failed",
  refused: "Refused",
  interrupted: "Interrupted",
  review: "Waiting for your review",
  stopped: "Stopped",
  partial: "Typed, not sent",
};

export function stepWords(step: string): string {
  return STEP_WORDS[step] ?? step.replace(/_/g, " ");
}

export function runTriggerWords(r: RunSummary | AutomationRun): string {
  if (r.trigger === "manual") return "Run now (you)";
  if (r.trigger === "webhook") {
    const facts = "inputs" in r ? loopFacts(r) : null;
    if (facts?.item)
      return facts.revision
        ? `${facts.item} at ${shortRev(facts.revision)}`
        : facts.item;
    return "Forgejo delivery";
  }
  if (r.catch_up)
    return `catch-up · covered ${r.covered} missed ${r.covered === 1 ? "slot" : "slots"}`;
  return r.trigger === "once" ? "once" : "scheduled";
}

// ---- the review loop's runs (#2202) ---------------------------------------------------------------

/** What a review-loop run shows about itself, read from the server's recorded `inputs`. `event` and
 *  `delivery` come from UNSIGNED headers: shown, always marked "recorded, untrusted". */
export interface LoopFacts {
  /** `review` starts the reviewer; `return` sends its verdict back. */
  leg: "review" | "return";
  /** "review 2 / 4", "4 / 4 reviews", "review 2", or null when the run carries no round. */
  round: string | null;
  item: string;
  revision: string;
  verdict: string;
  event: string;
  delivery: string;
}

function str(v: unknown): string {
  return typeof v === "string" ? v : typeof v === "number" ? String(v) : "";
}
function int(v: unknown): number | null {
  return typeof v === "number" && Number.isInteger(v) && v > 0 ? v : null;
}

/** A commit sha as people read it (7 characters); an issue's 16-hex digest likewise. */
export function shortRev(rev: string): string {
  return /^[0-9a-f]{12,}$/i.test(rev) ? rev.slice(0, 7) : rev;
}

/** Only for a webhook run; `null` for any other trigger. A verdict that never reached the
 *  acceptance (a malformed marker) carries no `leg` but is still the return leg: its event is a
 *  review or a comment, never an item change. */
export function loopFacts(r: AutomationRun): LoopFacts | null {
  if (r.trigger !== "webhook") return null;
  const i = r.inputs ?? {};
  const leg =
    i.leg === "return" || i.kind === "pr_review" || i.kind === "issue_comment"
      ? "return"
      : "review";
  const round = int(i.round);
  const cap = int(i.cap);
  const reviews = int(i.reviews);
  let roundWords: string | null = null;
  if (r.outcome === "capped" && cap)
    roundWords = `${reviews ?? cap} / ${cap} reviews`;
  else if (round && cap) roundWords = `review ${round} / ${cap}`;
  else if (round) roundWords = `review ${round}`;
  return {
    leg,
    round: roundWords,
    item: str(i.item),
    revision: str(i.revision),
    verdict: str(i.verdict),
    event: str(i.event),
    delivery: str(i.delivery),
  };
}

// ---- the editor's form ---------------------------------------------------------------------------

export type MessageMode = "text" | "template";

export interface EditorForm {
  name: string;
  triggerKind: "once" | "schedule" | "manual" | "webhook";
  onceAt: string;
  tz: string;
  cadenceKind: Cadence["kind"];
  every: number;
  unit: "minutes" | "hours";
  time: string;
  days: Weekday[];
  day: number;
  actionKind: AutomationAction["kind"];
  projectId: string;
  autonomy: Autonomy;
  engine: string;
  folder: string;
  bypass: boolean;
  sessionKey: string;
  messageMode: MessageMode;
  text: string;
  templateId: string;
  values: Record<string, string>;
  concurrency: AutomationPolicy["concurrency"];
  maxConcurrent: number;
  maxRunsPerDay: number;
  pauseAfterFailures: number;
  expiresAt: number | null;
  // The review loop (#2202). `projectId` is shared with a mission.
  repo: string;
  reviewerId: string;
  /** The specialist version pinned: its CURRENT version when it was chosen. */
  reviewerVersion: number | null;
  reviewerLogin: string;
  reviewIssues: boolean;
  reviewPulls: boolean;
  roundsIssue: number;
  roundsPr: number;
  returnToOrigin: boolean;
  /** The webhook picked from Library → Webhooks (#2221), or "" while none is. */
  webhookId: string;
  /** A stored #2202 review loop's own webhook: saved back as it was, never converted. */
  legacyHook: boolean;
  /** Which deliveries it acts on (#2221 P2): all must hold; empty = every signed delivery. */
  when: ConditionDraft[];
  /** Template fields filled from the delivery, by field name (#2221 P2). */
  fromDelivery: Record<string, MappingDraft>;
}

/** A condition as the wizard edits it. Each value is its own element with its OWN type, kept as
 *  TEXT so a half-typed value is never re-rendered under the operator's cursor; `conditionBody`
 *  types each. One element for `is`, `is_not` and `contains`, one per value for `one_of`. Never a
 *  joined string: a value may hold a comma, and a list may mix numbers, texts and yes/no. */
export interface ConditionDraft {
  path: string;
  op: ConditionOp;
  values: ValueDraft[];
}
export interface ValueDraft {
  text: string;
  type: ConditionValueType;
  /** A row the operator added and has not typed into yet. Only this is "missing": an EMPTY text
   *  is a value the server accepts (`is ""`), and one loaded from a stored automation is kept. */
  unset?: true;
}
export type ConditionValueType = "text" | "number" | "yes_no";

/** A template field mapped from the delivery, as the wizard edits it: one element per allowed
 *  choice, never joined (a choice may hold a comma). */
export interface MappingDraft {
  path: string;
  type: MappedType;
  choices: string[];
}

/** The server's round bounds (`review_loop.ROUNDS_*`). */
export const ROUNDS_MIN = 1;
export const ROUNDS_MAX = 10;

export function blankForm(tz: string): EditorForm {
  return {
    name: "",
    triggerKind: "schedule",
    onceAt: "",
    tz,
    cadenceKind: "daily",
    every: 30,
    unit: "minutes",
    time: "03:00",
    days: ["mon", "tue", "wed", "thu", "fri"],
    day: 1,
    actionKind: "start_mission",
    projectId: "",
    autonomy: "propose",
    engine: "",
    folder: "",
    bypass: false,
    sessionKey: "",
    messageMode: "text",
    text: "",
    templateId: "",
    values: {},
    concurrency: "skip",
    maxConcurrent: 2,
    maxRunsPerDay: 48,
    pauseAfterFailures: 3,
    expiresAt: null,
    repo: "",
    reviewerId: "",
    reviewerVersion: null,
    reviewerLogin: "",
    reviewIssues: true,
    reviewPulls: true,
    roundsIssue: 5,
    roundsPr: 4,
    returnToOrigin: true,
    webhookId: "",
    legacyHook: false,
    when: [],
    fromDelivery: {},
  };
}

/** What a trigger may start (#2221 P2, mirroring `automations.validate_config`):
 *
 *  - a review loop is started only by a webhook, and takes no conditions (it reads its own events);
 *  - a webhook from the list starts a review loop, a session, a message to a session or a message
 *    to HQ (`tell_hq`) — never a mission (that is a later phase);
 *  - a #2202 review loop's own webhook (`legacyHook`) starts only that review loop;
 *  - conditions and values from the delivery go only with a webhook from the list.
 *
 *  Choosing a side that the other can't pair with moves the other to the nearest choice that can,
 *  so the form never holds a pairing the server refuses. A review loop's deliveries come in bursts
 *  (a push and an edit), so it defaults to running them side by side, as the server does. */
export function withTrigger(
  f: EditorForm,
  kind: EditorForm["triggerKind"],
  maxConcurrent: number,
): EditorForm {
  if (kind === f.triggerKind) return f;
  if (kind === "webhook") {
    const next: EditorForm = { ...f, triggerKind: kind };
    if (f.legacyHook) return intoLoop(next, maxConcurrent);
    // A webhook never starts a mission: a session is the nearest it can start.
    if (f.actionKind === "start_mission") next.actionKind = "start_session";
    return next;
  }
  const next: EditorForm = { ...f, triggerKind: kind, when: [], fromDelivery: {} };
  if (f.actionKind === "forge_review_loop") return outOfLoop(next, "trigger");
  return next;
}

export function withAction(
  f: EditorForm,
  kind: EditorForm["actionKind"],
  maxConcurrent: number,
): EditorForm {
  if (kind === f.actionKind) return f;
  if (kind === "forge_review_loop")
    return intoLoop({ ...f, actionKind: kind }, maxConcurrent);
  let next: EditorForm = { ...f, actionKind: kind };
  if (f.actionKind === "forge_review_loop") next = outOfLoop(next, "action");
  // A mission, or anything but the loop on a review loop's own webhook, can't be webhook-started.
  if (next.triggerKind === "webhook" && (kind === "start_mission" || legacyTrigger(next))) {
    const blank = blankForm(f.tz);
    next = { ...next, triggerKind: blank.triggerKind, when: [], fromDelivery: {} };
  }
  return next;
}

function intoLoop(f: EditorForm, maxConcurrent: number): EditorForm {
  return {
    ...f,
    triggerKind: "webhook",
    actionKind: "forge_review_loop",
    // A review loop picks its own events; it takes no conditions and no values.
    when: [],
    fromDelivery: {},
    concurrency: "allow",
    maxConcurrent: Math.max(2, maxConcurrent),
  };
}

function outOfLoop(f: EditorForm, chose: "trigger" | "action"): EditorForm {
  const blank = blankForm(f.tz);
  return {
    ...f,
    actionKind: chose === "action" ? f.actionKind : blank.actionKind,
    concurrency: blank.concurrency,
    maxConcurrent: blank.maxConcurrent,
  };
}

/** A stored automation as the editor's form. Fields the stored action does not carry keep the
 *  blank form's values, so switching the action kind in the editor starts from a sane default. */
export function formFromAutomation(a: Automation, tz: string): EditorForm {
  const f = blankForm(tz);
  f.name = a.name;
  const t = a.trigger;
  if (t) {
    f.triggerKind = t.kind;
    if (t.kind === "once") {
      f.onceAt = t.at;
      f.tz = t.tz;
    } else if (t.kind === "webhook") {
      if ("webhook_id" in t) {
        f.webhookId = t.webhook_id;
        f.when = (t.when ?? []).map(conditionDraft);
      } else f.legacyHook = true; // #2202's own webhook: kept as it is, never converted here
    } else if (t.kind === "schedule") {
      f.tz = t.tz;
      const c = t.cadence;
      f.cadenceKind = c.kind;
      if (c.kind === "interval") {
        f.every = c.every;
        f.unit = c.unit;
      } else {
        f.time = c.time;
        if (c.kind === "weekly") f.days = [...c.days];
        if (c.kind === "monthly") f.day = c.day;
      }
    }
  }
  const act = a.action;
  if (act?.kind === "forge_review_loop") {
    f.actionKind = act.kind;
    f.projectId = act.project_id;
    f.repo = act.repo;
    f.reviewerId = act.reviewer.specialist_id;
    f.reviewerVersion = act.reviewer.version;
    f.reviewerLogin = act.reviewer_login;
    f.reviewIssues = act.legs.issues;
    f.reviewPulls = act.legs.pulls;
    f.roundsIssue = act.max_rounds.issue;
    f.roundsPr = act.max_rounds.pr;
    f.returnToOrigin = act.return_to_origin;
  } else if (act) {
    f.actionKind = act.kind;
    const msg = act.kind === "start_mission" ? act.instruction : act.message;
    if ("text" in msg) {
      f.messageMode = "text";
      f.text = msg.text;
    } else {
      f.messageMode = "template";
      f.templateId = msg.template_id;
      f.values = { ...msg.values };
      for (const [field, m] of Object.entries(msg.from_delivery ?? {}))
        f.fromDelivery[field] = {
          path: m.path,
          type: m.type,
          choices: [...(m.choices ?? [])],
        };
    }
    if (act.kind === "start_mission") {
      f.projectId = act.project_id;
      f.autonomy = act.autonomy;
    } else if (act.kind === "start_session") {
      f.engine = act.engine;
      f.folder = act.folder;
      f.bypass = act.bypass;
    } else if (act.kind === "send_to_session") {
      f.sessionKey = act.session_key;
    }
  }
  const p = a.policy;
  if (p) {
    f.concurrency = p.concurrency;
    f.maxConcurrent = p.max_concurrent;
    f.maxRunsPerDay = p.max_runs_per_day;
    f.pauseAfterFailures = p.pause_after_failures;
    f.expiresAt = p.expires_at;
  }
  return f;
}

/** Whether the trigger IS a #2202 review loop's own webhook right now. `legacyHook` alone is the
 *  stored identity, kept while the operator tries another trigger so switching back restores it. */
export function legacyTrigger(f: EditorForm): boolean {
  return f.triggerKind === "webhook" && f.legacyHook;
}

/** Whether this form takes values and conditions from a delivery: a webhook from the list. */
export function fromListHook(f: EditorForm): boolean {
  return f.triggerKind === "webhook" && !f.legacyHook;
}

function formMessage(f: EditorForm): AutomationMessage {
  if (f.messageMode === "text") return { text: f.text };
  const mapped = fromListHook(f) ? mappingBody(f.fromDelivery) : {};
  // Only the fields the operator typed: a library field takes its value from the library at run
  // time, and the server refuses a value sent for one. A mapped field never has a fixed value.
  const values: Record<string, string> = {};
  for (const [k, v] of Object.entries(f.values))
    if (v !== "" && !(k in mapped)) values[k] = v;
  return Object.keys(mapped).length
    ? { template_id: f.templateId, values, from_delivery: mapped }
    : { template_id: f.templateId, values };
}

export function formTrigger(f: EditorForm): Trigger {
  if (f.triggerKind === "manual") return { kind: "manual" };
  if (f.triggerKind === "webhook") {
    if (f.legacyHook) return { kind: "webhook", scheme: "forgejo" };
    // A review loop picks its own events; the key is omitted when there are none.
    const when =
      f.actionKind === "forge_review_loop" ? [] : f.when.map(conditionBody);
    return when.length
      ? { kind: "webhook", webhook_id: f.webhookId, when }
      : { kind: "webhook", webhook_id: f.webhookId };
  }
  if (f.triggerKind === "once") return { kind: "once", at: f.onceAt, tz: f.tz };
  let cadence: Cadence;
  if (f.cadenceKind === "interval")
    cadence = { kind: "interval", every: f.every, unit: f.unit };
  else if (f.cadenceKind === "daily") cadence = { kind: "daily", time: f.time };
  else if (f.cadenceKind === "weekly")
    cadence = {
      kind: "weekly",
      days: WEEKDAYS.filter((d) => f.days.includes(d)),
      time: f.time,
    };
  else cadence = { kind: "monthly", day: f.day, time: f.time };
  return { kind: "schedule", cadence, tz: f.tz };
}

export function formAction(f: EditorForm): AutomationAction {
  if (f.actionKind === "forge_review_loop") return formReviewLoop(f);
  const message = formMessage(f);
  if (f.actionKind === "start_mission")
    return {
      kind: "start_mission",
      project_id: f.projectId,
      instruction: message,
      autonomy: f.autonomy,
    };
  if (f.actionKind === "start_session")
    // `model` is fixed to null until #1189: the server refuses any other value.
    return {
      kind: "start_session",
      engine: f.engine,
      model: null,
      folder: f.folder,
      bypass: f.bypass,
      message,
    };
  // A message to HQ (#2221): the message only — it has no target, agent or permissions.
  if (f.actionKind === "tell_hq") return { kind: "tell_hq", message };
  return { kind: "send_to_session", session_key: f.sessionKey, message };
}

/** A review loop's action, exactly the fields the server's strict validator accepts. */
function formReviewLoop(f: EditorForm): ReviewLoopAction {
  return {
    kind: "forge_review_loop",
    project_id: f.projectId,
    repo: f.repo.trim(),
    reviewer: { specialist_id: f.reviewerId, version: f.reviewerVersion ?? 0 },
    reviewer_login: f.reviewerLogin.trim(),
    legs: { issues: f.reviewIssues, pulls: f.reviewPulls },
    max_rounds: { issue: f.roundsIssue, pr: f.roundsPr },
    return_to_origin: f.returnToOrigin,
  };
}

/** The server's own alphabets for a repository and a login (`automations._REPO_RE`, `_LOGIN_RE`). */
export const REPO_RE = /^[A-Za-z0-9_.-]{1,100}\/[A-Za-z0-9_.-]{1,100}$/;
export const LOGIN_RE = /^[A-Za-z0-9_.-]{1,64}$/;

/** The whole create/patch body. The policy is sent in full, so what the operator sees in the
 *  Limits section is what is stored. */
export function bodyFromForm(f: EditorForm): AutomationConfig {
  const policy: Partial<AutomationPolicy> = {
    concurrency: f.concurrency,
    max_runs_per_day: f.maxRunsPerDay,
    pause_after_failures: f.pauseAfterFailures,
    expires_at: f.expiresAt,
  };
  if (f.concurrency === "allow") policy.max_concurrent = f.maxConcurrent;
  return {
    name: f.name.trim(),
    trigger: formTrigger(f),
    action: formAction(f),
    policy,
  };
}

/** What the form still needs before Save can be pressed — client-side only for fields whose
 *  absence would make the server's refusal unhelpful. The server validates everything. In the
 *  wizard's step order, the name (asked last) first. */
export function formProblems(f: EditorForm): string[] {
  const by = stepProblems(f);
  return [...by.review, ...by.when, ...by.events, ...by.what, ...by.limits];
}

/** The problems that hold each wizard step (#2221 P2b). */
export function stepProblems(f: EditorForm): Record<WizardStepId, string[]> {
  return {
    when: whenProblems(f),
    events: eventsProblems(f),
    what: whatProblems(f),
    limits: [],
    review: f.name.trim() ? [] : ["Give it a name"],
  };
}

function whenProblems(f: EditorForm): string[] {
  const out: string[] = [];
  if (f.triggerKind === "once" && !f.onceAt) out.push("Choose when it runs");
  if (
    f.triggerKind === "schedule" &&
    f.cadenceKind === "weekly" &&
    !f.days.length
  )
    out.push("Choose at least one day");
  if (fromListHook(f) && !f.webhookId) out.push("Pick a webhook");
  return out;
}

function eventsProblems(f: EditorForm): string[] {
  if (!fromListHook(f) || f.actionKind === "forge_review_loop") return [];
  const out: string[] = [];
  if (f.when.length > CONDITIONS_MAX)
    out.push(`At most ${CONDITIONS_MAX} conditions`);
  f.when.forEach((c, i) => {
    const what = `Condition ${i + 1}`;
    if (!validPath(c.path)) {
      out.push(`${what}: pick a field of the delivery (like pull_request.draft)`);
      return;
    }
    const takes = OP_TAKES[c.op];
    if (!takes) return;
    const items = conditionValues(c);
    // Missing: no value, a fresh row never typed into, or an empty value where the server needs
    // text (`contains`) or a number / yes-no. An empty TEXT for is, is not or one of is a value.
    const missing = (v: ValueDraft) =>
      v.unset || (v.text === "" && (takes === "text" || v.type !== "text"));
    if (!items.length || items.some(missing)) {
      out.push(`${what}: give it a value`);
      return;
    }
    if (takes === "list" && items.length > CHOICES_MAX)
      out.push(`${what}: at most ${CHOICES_MAX} values`);
    if (items.some((v) => v.text.length > VALUE_MAX || /\n/.test(v.text)))
      out.push(`${what}: a value is one line of at most ${VALUE_MAX} characters`);
    else {
      const bad = items.find((v) => takes !== "text" && typedValue(v.text, v.type) === undefined);
      if (bad)
        out.push(
          bad.type === "number"
            ? `${what}: “${bad.text}” is not a number; give a number`
            : `${what}: “${bad.text}” is not yes or no; answer yes or no`,
        );
    }
  });
  return out;
}

function whatProblems(f: EditorForm): string[] {
  const out: string[] = [];
  const hook = f.triggerKind === "webhook";
  if (hook && f.actionKind === "start_mission")
    out.push("A webhook can’t start a mission; start or message a session, or tell HQ");
  if (legacyTrigger(f) && f.actionKind !== "forge_review_loop")
    out.push("A review loop’s own webhook starts only that review loop");
  if (!hook && f.actionKind === "forge_review_loop")
    out.push("A review loop starts from a webhook: pick one in step 1");
  if (f.actionKind === "start_mission" && !f.projectId)
    out.push("Choose a project");
  if (f.actionKind === "start_session" && !f.engine)
    out.push("Choose an agent");
  if (f.actionKind === "start_session" && !f.folder)
    out.push("Choose a folder");
  if (f.actionKind === "send_to_session" && !f.sessionKey)
    out.push("Choose a session");
  if (fromListHook(f) && !apiTarget(f))
    out.push("A webhook starts or messages an API session only; pick an API agent");
  if (f.actionKind === "forge_review_loop") {
    if (!f.projectId) out.push("Choose a project");
    if (!REPO_RE.test(f.repo.trim()))
      out.push("Name the repository as owner/name");
    if (!f.reviewerId || !f.reviewerVersion)
      out.push("Choose a reviewer specialist");
    if (!LOGIN_RE.test(f.reviewerLogin.trim()))
      out.push("Name the reviewer’s forge account");
    if (!f.reviewIssues && !f.reviewPulls)
      out.push("Review issues, pull requests or both");
    for (const n of [f.roundsIssue, f.roundsPr])
      if (!Number.isInteger(n) || n < ROUNDS_MIN || n > ROUNDS_MAX) {
        out.push(`Reviews per item: ${ROUNDS_MIN} to ${ROUNDS_MAX}`);
        break;
      }
    return out; // a review loop's briefs are BattleLab's own text: there is no message to write
  }
  if (f.messageMode === "text" && !f.text.trim())
    out.push(
      f.actionKind === "start_mission"
        ? "Write the instruction"
        : "Write the message",
    );
  if (f.messageMode === "template" && !f.templateId)
    out.push("Choose a template");
  if (f.messageMode === "template" && fromListHook(f)) out.push(...mappingProblems(f));
  return out;
}

function mappingProblems(f: EditorForm): string[] {
  const out: string[] = [];
  const entries = Object.entries(f.fromDelivery);
  if (entries.length > MAPPED_MAX)
    out.push(`At most ${MAPPED_MAX} fields from the delivery`);
  for (const [field, m] of entries) {
    if (!validPath(m.path)) {
      out.push(`${field}: pick a field of the delivery`);
      continue;
    }
    if (m.path.includes("[]"))
      out.push(`${field}: a value comes from one field, not a list`);
    if (m.type === "choice") {
      const ch = m.choices;
      if (!ch.length || ch.some((v) => v === ""))
        out.push(`${field}: list the allowed values, none empty`);
      else if (ch.length > CHOICES_MAX)
        out.push(`${field}: at most ${CHOICES_MAX} allowed values`);
      else if (ch.some((v) => v.length > TEXT_MAX || !TEXT_RE.test(v)))
        out.push(
          `${field}: each allowed value is a short plain text (letters, digits, spaces and _.,:;/#@()+-)`,
        );
    }
  }
  return out;
}

/** Whether the session a webhook starts or messages is an API client. Only a target the roster
 *  KNOWS runs in a terminal fails: an unknown roster is left to the server's check at save. */
function apiTarget(f: EditorForm): boolean {
  if (f.actionKind === "start_session")
    return !f.engine || runsInTerminal(f.engine) !== true;
  if (f.actionKind === "send_to_session") {
    const engine = sessionEngine(f.sessionKey);
    return !engine || runsInTerminal(engine) !== true;
  }
  return true;
}

/** The engine of a session key (`<engine>:<native id>`), or "". */
export function sessionEngine(key: string): string {
  const i = key.indexOf(":");
  return i > 0 ? key.slice(0, i) : "";
}

// ---- the wizard (#2221 P2b) ------------------------------------------------------------------------

export type WizardStepId = "when" | "events" | "what" | "limits" | "review";

/** The five steps, in order. Each fits one phone screen; advanced options are folded away. */
export const WIZARD_STEPS: readonly { id: WizardStepId; label: string }[] = [
  { id: "when", label: "When" },
  { id: "events", label: "Events" },
  { id: "what", label: "What" },
  { id: "limits", label: "Limits" },
  { id: "review", label: "Review & enable" },
];

/** A schedule, a once and Run now have no events to choose: step 2 is skipped. */
export function eventsSkipped(f: EditorForm): boolean {
  return f.triggerKind !== "webhook";
}

/** The server's bounds (`webhook_events`). */
export const CONDITIONS_MAX = 8;
export const CHOICES_MAX = 20;
export const MAPPED_MAX = 16;
const VALUE_MAX = 200;
const TEXT_MAX = 200;
const PATH_MAX = 160;
const PATH_RE =
  /^[A-Za-z0-9_-]{1,64}(\[\])?(\.[A-Za-z0-9_-]{1,64}(\[\])?){0,5}$/;
const TEXT_RE = /^[A-Za-z0-9 _.,:;/#@()+-]*$/;

/** Whether `path` is a field path the server accepts (`webhook_events.PATH_RE`). */
export function validPath(path: string): boolean {
  return path.length <= PATH_MAX && PATH_RE.test(path);
}

/** Which operators take a value, and what kind (`webhook_events.OPS`). */
export const OP_TAKES: Record<ConditionOp, "one" | "list" | "text" | null> = {
  is: "one",
  is_not: "one",
  one_of: "list",
  contains: "text",
  is_true: null,
  is_false: null,
  exists: null,
  missing: null,
};

export const OP_WORDS: Record<ConditionOp, string> = {
  is: "is",
  is_not: "is not",
  one_of: "is one of",
  contains: "contains",
  is_true: "is yes",
  is_false: "is no",
  exists: "is present",
  missing: "is absent",
};

export const MAPPED_TYPE_WORDS: Record<MappedType, string> = {
  number: "number",
  choice: "choice",
  text: "short text",
  yes_no: "yes / no",
};

/** The values an operator reads: every element for `one_of`, the first for the others. */
export function conditionValues(c: ConditionDraft): ValueDraft[] {
  const takes = OP_TAKES[c.op];
  if (!takes) return [];
  return takes === "list" ? c.values : c.values.slice(0, 1);
}

/** How a delivery field's type (`webhooks._type_of`) reads a condition's value. */
export function valueTypeOf(shapeType: string | undefined): ConditionValueType {
  if (shapeType === "number") return "number";
  if (shapeType === "yes/no") return "yes_no";
  return "text";
}

/** One value text as its type, or `undefined` when it isn't one. */
function typedValue(text: string, t: ConditionValueType): ConditionScalar | undefined {
  const v = text.trim();
  if (t === "number") {
    if (v === "") return undefined;
    const n = Number(v);
    return Number.isFinite(n) ? n : undefined;
  }
  if (t === "yes_no") {
    const w = v.toLowerCase();
    if (w === "yes" || w === "true") return true;
    if (w === "no" || w === "false") return false;
    return undefined;
  }
  return text;
}

function scalarText(v: ConditionScalar): string {
  if (typeof v === "boolean") return v ? "yes" : "no";
  return String(v);
}

/** A stored value as the wizard edits it: its text, and its own type. */
export function valueDraft(v: ConditionScalar): ValueDraft {
  return {
    text: scalarText(v),
    type: typeof v === "number" ? "number" : typeof v === "boolean" ? "yes_no" : "text",
  };
}

/** A stored condition as the wizard edits it: each value its own element, with its own type. */
export function conditionDraft(c: WebhookCondition): ConditionDraft {
  const vals = Array.isArray(c.value) ? c.value : c.value === undefined ? [] : [c.value];
  return { path: c.path, op: c.op, values: vals.map(valueDraft) };
}

/** One value as the server stores it: typed by its own type. One that doesn't read as its type is
 *  sent as typed (the step holds it as a problem first). */
function valueBody(v: ValueDraft): ConditionScalar {
  return typedValue(v.text, v.type) ?? v.text;
}

/** The condition the server stores: each value typed, and only for an operator that takes one. */
export function conditionBody(d: ConditionDraft): WebhookCondition {
  const takes = OP_TAKES[d.op];
  const path = d.path.trim();
  if (!takes) return { path, op: d.op };
  if (takes === "list") return { path, op: d.op, value: d.values.map(valueBody) };
  const first = d.values[0] ?? { text: "", type: "text" as const };
  // `contains` reads a text, whatever the field.
  if (takes === "text") return { path, op: d.op, value: first.text };
  return { path, op: d.op, value: valueBody(first) };
}

/** The mapping the server stores: choices only for a choice, and only non-empty. */
export function mappingBody(
  m: Record<string, MappingDraft>,
): Record<string, DeliveryMapping> {
  const out: Record<string, DeliveryMapping> = {};
  for (const [field, d] of Object.entries(m)) {
    out[field] =
      d.type === "choice"
        ? { path: d.path.trim(), type: d.type, choices: [...d.choices] }
        : { path: d.path.trim(), type: d.type };
  }
  return out;
}

/** The preset the form's conditions ARE (`""` for none, so every signed delivery), or `null` when
 *  they are the operator's own. */
export function chosenPreset(
  f: EditorForm,
  presets: readonly WebhookPreset[],
): string | null {
  if (!f.when.length) return "";
  const mine = JSON.stringify(f.when.map(conditionBody));
  return presets.find((p) => JSON.stringify(p.when) === mine)?.id ?? null;
}

/** Choosing a preset COPIES its conditions into the automation: a later catalogue change can't
 *  widen it. `null` = every signed delivery. */
export function withPreset(f: EditorForm, preset: WebhookPreset | null): EditorForm {
  return { ...f, when: preset ? preset.when.map(conditionDraft) : [] };
}

/** The template fields a delivery may fill: the template's own typed fields — never a library
 *  field, never a secret. */
export function mappableFields(t: Template | null): string[] {
  return (t?.fields ?? [])
    .filter((x) => x.source !== "library" && x.kind !== "secret")
    .map((x) => x.name);
}

/** A field's value source: a fixed value, or from the delivery (an empty mapping to fill in). */
export function withFieldSource(
  f: EditorForm,
  field: string,
  source: "fixed" | "delivery",
): EditorForm {
  const values = { ...f.values };
  const fromDelivery = { ...f.fromDelivery };
  if (source === "delivery") {
    delete values[field];
    fromDelivery[field] ??= { path: "", type: "text", choices: [] };
  } else delete fromDelivery[field];
  return { ...f, values, fromDelivery };
}

/** What a field of the last delivery is, in words ("a number", "text"). */
function shapeWord(t: string): string {
  if (t === "number") return "a number";
  if (t === "yes/no") return "yes or no";
  if (t === "list") return "a list";
  if (t === "object") return "an object";
  if (t === "empty") return "empty";
  return "text";
}

const MAPPED_FITS: Record<MappedType, string[]> = {
  number: ["number"],
  yes_no: ["yes/no"],
  text: ["text"],
  choice: ["text"],
};

/** What the last delivery's SHAPE says about the conditions and the mapping: a field it did not
 *  have is a warning (it may never match; a run missing it is refused), a type that can't fit is a
 *  problem that holds the step. No shape (no delivery yet) checks nothing. */
export function deliveryChecks(
  f: EditorForm,
  shape: readonly ShapeField[] | null,
): Record<"events" | "what", { problems: string[]; warnings: string[] }> {
  const out = {
    events: { problems: [] as string[], warnings: [] as string[] },
    what: { problems: [] as string[], warnings: [] as string[] },
  };
  if (!fromListHook(f) || !shape || !shape.length) return out;
  const types = new Map(shape.map((x) => [x.path, String(x.type)]));
  if (f.actionKind !== "forge_review_loop")
    f.when.forEach((c, i) => {
      if (!validPath(c.path)) return;
      const t = types.get(c.path);
      if (t === undefined) {
        if (c.op !== "missing")
          out.events.warnings.push(
            `“${c.path}” was not in the last delivery — it may never match.`,
          );
        return;
      }
      const what = `Condition ${i + 1}`;
      if ((c.op === "is_true" || c.op === "is_false") && t !== "yes/no" && t !== "empty")
        out.events.problems.push(
          `${what}: ${c.path} is ${shapeWord(t)}, not yes or no. Use “is” or pick a yes/no field.`,
        );
      if (c.op === "contains" && t !== "text" && t !== "empty")
        out.events.problems.push(`${what}: ${c.path} is ${shapeWord(t)}; “contains” needs a text field.`);
    });
  if (f.messageMode === "template" && f.actionKind !== "forge_review_loop")
    for (const [field, m] of Object.entries(f.fromDelivery)) {
      if (!validPath(m.path) || m.path.includes("[]")) continue;
      const t = types.get(m.path);
      if (t === undefined) {
        out.what.warnings.push(
          `${field}: “${m.path}” was not in the last delivery — a delivery without it is refused.`,
        );
        continue;
      }
      if (t === "empty") {
        out.what.warnings.push(
          `${field}: “${m.path}” was empty in the last delivery, so its type can’t be checked.`,
        );
        continue;
      }
      if (!MAPPED_FITS[m.type].includes(t))
        out.what.problems.push(
          `${field} is ${m.type === "number" ? "a number" : m.type === "yes_no" ? "yes or no" : m.type === "choice" ? "a choice" : "short text"}; ${m.path} is ${shapeWord(t)}. Pick ${
            m.type === "number" ? "a number" : m.type === "yes_no" ? "a yes/no" : "a text"
          } field, or change its type.`,
        );
    }
  return out;
}

/** The limits in one line, for step 4 and the review. */
export function limitsWords(f: EditorForm): string {
  const conc =
    f.concurrency === "skip"
      ? "one run at a time"
      : `up to ${f.maxConcurrent} runs at once`;
  const ends =
    f.expiresAt == null
      ? "runs until you turn it off"
      : `stops on ${new Date(f.expiresAt * 1000).toLocaleDateString("en-GB", { day: "2-digit", month: "short", year: "numeric" })}`;
  return `${conc} · at most ${f.maxRunsPerDay} runs a day · pauses after ${f.pauseAfterFailures} failures in a row · ${ends}`;
}

/** Which events it acts on, in words, for the review. */
export function eventsWords(
  f: EditorForm,
  presets: readonly WebhookPreset[],
): string {
  if (!fromListHook(f) && f.triggerKind === "webhook")
    return "the review loop’s own events";
  if (f.triggerKind !== "webhook") return "";
  if (f.actionKind === "forge_review_loop") return "the review loop picks its own events";
  const p = chosenPreset(f, presets);
  if (p === "") return "every signed delivery";
  if (p) return presets.find((x) => x.id === p)?.label ?? p;
  return f.when
    .map(
      (c) =>
        `${c.path} ${OP_WORDS[c.op]}${OP_TAKES[c.op] ? ` ${conditionValues(c).map((v) => v.text).join(", ")}` : ""}`,
    )
    .join(" · ");
}

// ---- consent -------------------------------------------------------------------------------------

/** The consent a refused request asks for, or `null` when the refusal is something else.
 *
 *  Two shapes carry it: a widening save / a first enable (422, `scope_digest` in the body) and a
 *  scope that changed between reading it and consenting (409, the fresh scope in the body). Either
 *  way the dialog shows the server's lines and resends with the server's digest — never one the
 *  client computed. */
export function consentFromError(e: unknown): ConsentRequired | null {
  if (!(e instanceof ApiError) || (e.status !== 422 && e.status !== 409))
    return null;
  const r = e.record as ConsentRequired | undefined;
  if (!r || typeof r.scope_digest !== "string" || !Array.isArray(r.scope_lines))
    return null;
  return {
    detail: typeof r.detail === "string" ? r.detail : e.message,
    // PRESENCE IS KEPT: `undefined` = the server did not say; `[]` = it said nothing widened.
    widened: Array.isArray(r.widened) ? r.widened : undefined,
    scope: r.scope,
    scope_lines: r.scope_lines,
    scope_digest: r.scope_digest,
  };
}

/** The consent an enable needs, from the automation as read: its current scope and digest. */
export function consentForEnable(a: Automation): ConsentRequired | null {
  if (!a.scope_digest) return null;
  return {
    detail: "enabling needs your consent to its full scope",
    widened: [],
    scope: a.scope ?? undefined,
    scope_lines: a.scope_lines,
    scope_digest: a.scope_digest,
  };
}

/** The server's widened items are phrases ("higher mission autonomy"); a scope line is highlighted
 *  when it is about one of them. Matched on a keyword per item, so a wording change on either side
 *  degrades to "the list above names what widened", never to a wrong highlight of an unrelated
 *  line. */
const WIDENED_KEYS: [RegExp, RegExp][] = [
  [/autonomy/i, /^Autonomy/i],
  [/bypass/i, /bypass/i],
  [/target|action/i, /^(Starts|Types into)/i],
  [/agent|model/i, /^(Starts a|The agent)/i],
  [/trigger|schedule|more often/i, /^(On a schedule|Once|Only when)/i],
  [/daily cap/i, /\/day/i],
  [/failures/i, /^Pauses after/i],
  [/checklist/i, /^Checklist/i],
];

export function widenedLine(line: string, widened: string[]): boolean {
  return widened.some((w) =>
    WIDENED_KEYS.some(([item, l]) => item.test(w) && l.test(line)),
  );
}

/** A copy's body: the same configuration under a new name. Always created OFF by the server. */
export function duplicateBody(a: Automation): AutomationConfig | null {
  if (!a.trigger || !a.action || !a.policy) return null;
  const trigger =
    // A once in the past cannot be saved; the copy becomes manual until the operator picks a time.
    a.trigger.kind === "once" && a.next_run == null
      ? ({ kind: "manual" } as Trigger)
      : a.trigger;
  return {
    name: `${a.name} (copy)`.slice(0, 80),
    trigger,
    action: a.action,
    policy: a.policy,
  };
}

/** What a dialog says when the scope moved while it was open (a 409 with a fresh scope). */
export const SCOPE_MOVED_NOTE =
  "The scope changed again since you opened this — review every line before confirming.";

/** The consent to re-open after a 409 carrying a fresh scope. The server's `widened` wins whenever
 *  it is PRESENT — an explicit `[]` means nothing widens now, and must not be overridden by labels
 *  from the old dialog. Only when the field is absent (an older server) is the previous list
 *  carried forward, so what widened is never silently dropped. */
export function reconsent(
  next: ConsentRequired,
  prev: ConsentRequired | null,
): ConsentRequired {
  if (next.widened !== undefined) return next;
  return { ...next, widened: prev?.widened ?? [] };
}

// ---- API first: terminal targets and "Switch to API client" (#1728 Phase 2) -------------------------

/** Does this automation start a session in a TERMINAL (a console client)? Asked of the roster's
 *  runtime — any console target, whatever its history: nothing is stamped on the row. Such an
 *  automation keeps running (no cutoff before #1728 Phase 3) and is labelled "Terminal —
 *  unreliable unattended": a terminal has nobody watching it. `false` while the roster is unknown. */
export function isTerminalTarget(
  action: AutomationAction | null | undefined,
): boolean {
  return (
    action?.kind === "start_session" && runsInTerminal(action.engine) === true
  );
}

/** The API client a terminal automation can switch to: its family's API client (the server's
 *  `families` block), preferring one the server admits for unattended use (`engines[].ok`, the
 *  same check that refuses the save). `null` when the family has no API client — then there is
 *  nothing to switch to and no button. `ok: false` carries the server's reason: the button is
 *  shown, disabled, saying why — never a terminal fallback. */
export function apiSwitchFor(
  action: AutomationAction | null | undefined,
  admitted: readonly UnattendedEngine[],
): { engine: string; ok: boolean; reason: string } | null {
  if (!action || action.kind !== "start_session" || !isTerminalTarget(action))
    return null;
  const family = familyOf(action.engine);
  if (!family) return null;
  const apiClients = family.clients.filter((c) => {
    const info = engineInfo(c);
    return !!info && info.runtime === "api" && isActive(info);
  });
  if (apiClients.length === 0) return null;
  const ok = apiClients.find((c) => admitted.some((e) => e.id === c && e.ok));
  if (ok) return { engine: ok, ok: true, reason: "" };
  const first = apiClients[0];
  const why =
    admitted.find((e) => e.id === first)?.reason ||
    family.reasons[first] ||
    "it can’t be started unattended yet";
  return { engine: first, ok: false, reason: why };
}

/** The body "Switch to API client" saves through the ordinary validated `PATCH`: the same action
 *  on the API client, on its default model, asking before each request. The terminal's permission
 *  grant is NEVER carried over — a switch is a fresh consent (the server re-asks for an enabled
 *  automation, and validates the client's own model and permission settings). */
export function switchBody(
  a: Automation,
  engine: string,
): { revision: number; action: AutomationAction } | null {
  if (!a.action || a.action.kind !== "start_session") return null;
  return {
    revision: a.revision,
    action: { ...a.action, engine, model: null, bypass: false },
  };
}

// ---- the review loop's reviewer (#2202) ----------------------------------------------------------

export interface ReviewerOption {
  id: string;
  /** The version a choice pins: the specialist's CURRENT one. */
  version: number;
  label: string;
  /** Why it can't be chosen right now, or "". */
  blocked: string;
}

/** The specialists a review loop may start: only those on an API client (the roster's runtime —
 *  nothing unattended drives a terminal), never an archived one. One the server reports it can't
 *  start unattended (`engines[].ok`) is offered disabled with its reason; the server checks again
 *  at save and at every launch. `engineLabel` is passed in so this stays a pure function. */
export function reviewerOptions(
  specialists: readonly Specialist[] | null,
  admitted: readonly UnattendedEngine[],
  engineLabel: (id: string) => string,
): ReviewerOption[] {
  return (specialists ?? [])
    .filter(
      (s) => !s.archived && engineInfo(s.current.engine)?.runtime === "api",
    )
    .filter((s) => {
      const info = engineInfo(s.current.engine);
      return !!info && isActive(info);
    })
    .map((s) => {
      const v = s.current;
      const verdict = admitted.find((e) => e.id === v.engine);
      const model = v.model ? ` · ${v.model}` : "";
      return {
        id: s.id,
        version: v.version,
        label: `${v.title || s.id} v${v.version} · ${engineLabel(v.engine)}${model}`,
        blocked:
          verdict && !verdict.ok
            ? verdict.reason || "it can’t be started unattended"
            : "",
      };
    });
}

/** The Forgejo events the loop reads: item changes (review leg) and the reviewer's verdicts
 *  (return leg). Anything else the forge sends is chatter and only updates "Last delivery".
 *  "Pull request synchronized" is its OWN checkbox on Forgejo (`pull_request_sync`, gated apart
 *  from "Pull request"): without it a push to an open PR never starts the next review, which is
 *  the loop's whole point (#2202, Hermes on #2214). Pinned by its tests. */
export const FORGE_EVENTS = [
  "Issues",
  "Issue comment",
  "Pull request",
  "Pull request synchronized",
  "Pull request reviewed",
];
