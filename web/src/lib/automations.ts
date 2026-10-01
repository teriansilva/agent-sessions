/** Automations (#1201): the pure half of the page — words, summaries, and the form ↔ API mapping.
 *
 *  Everything the operator is asked to CONSENT to comes from the server (`scope_lines`,
 *  `scope_digest`, `widened`); nothing here re-derives a scope. What lives here is presentation
 *  (a state's word and tone, a cadence in words, the strip's counts) and the one mapping from the
 *  editor's flat form to the nested body the routes validate strictly — so a field the form does not
 *  own is never sent, and an unknown one can never be. */
import { ApiError } from "./api";
import type {
  Automation,
  AutomationAction,
  AutomationConfig,
  AutomationMessage,
  AutomationPolicy,
  AutomationState,
  Autonomy,
  Cadence,
  ConsentRequired,
  ResultClass,
  RunSummary,
  StripDay,
  Trigger,
  Weekday,
} from "../types/automations";

/** A status tone, which is what decides the LED and the word's colour. `down` is ACTIVE failure
 *  only (docs/design.md §7); `degraded` is "needs you"; `idle` is off, skipped or unknown. */
export type Tone = "up" | "degraded" | "down" | "idle";

export const STATE_WORD: Record<AutomationState, { word: string; tone: Tone }> = {
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
  return STATE_WORD[state as AutomationState] ?? { word: state || "Unknown", tone: "idle" };
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
  if (!(e instanceof ApiError)) return e instanceof Error ? e.message : fallback;
  if (e.status === 404) return "It no longer exists — it may have been deleted.";
  if (e.status === 413) return "That is too large to save.";
  if (e.status === 503) return `${e.message} — try again in a moment.`;
  return e.message || fallback;
}

/** The amber note a change's confirmation carries when a run already past its last check may
 *  still complete (`in_flight`). `""` when the change has no later effect to warn about. */
export function inFlightNote(r: { in_flight?: boolean; in_flight_detail?: string } | null): string {
  if (!r?.in_flight) return "";
  return `Note: ${r.in_flight_detail || "a run already in progress may still complete"}.`;
}

/** The words for one run's outcome. A run still `dispatching` has no outcome yet. `interrupted`
 *  is amber, not red: its outcome is UNKNOWN, which is a thing to look at, not a failure. */
export function outcomeWord(outcome: string, state?: string): { word: string; tone: Tone } {
  if (!outcome) return { word: state === "dispatching" ? "Starting" : "Pending", tone: "idle" };
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

export const WEEKDAYS: Weekday[] = ["mon", "tue", "wed", "thu", "fri", "sat", "sun"];
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
  const s = n % 100 >= 11 && n % 100 <= 13 ? "th" : ["th", "st", "nd", "rd"][n % 10] ?? "th";
  return `${n}${s}`;
}

export function cadenceWords(c: Cadence): string {
  switch (c.kind) {
    case "interval":
      return c.unit === "minutes" ? `Every ${c.every} min` : `Every ${c.every} h`;
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
  const wd = date.toLocaleDateString("en-GB", { weekday: "short", timeZone: "UTC" });
  const mon = date.toLocaleDateString("en-GB", { month: "short", timeZone: "UTC" });
  return `${wd} ${d} ${mon} ${y} ${hm}`;
}

export function triggerWords(t: Trigger | null): { label: string; detail: string } {
  if (!t) return { label: "Unreadable", detail: "its settings can't be read" };
  if (t.kind === "manual") return { label: "Manual", detail: "only when you press Run now" };
  if (t.kind === "once") return { label: "Once", detail: `${onceWords(t.at)} · ${t.tz}` };
  return { label: "Schedule", detail: `${cadenceWords(t.cadence)} · ${t.tz}` };
}

export const AUTONOMY_WORDS: Record<Autonomy, string> = {
  propose: "Plans it and waits for you to dispatch",
  dispatch: "Dispatches the plan without asking",
  dispatch_auto_choose: "Dispatches and may answer menus on its own",
};

export function actionLabel(kind: AutomationAction["kind"] | undefined): string {
  if (kind === "start_mission") return "Start mission";
  if (kind === "start_session") return "Start session";
  if (kind === "send_to_session") return "Send to session";
  return "Unreadable";
}

/** The last path component, for a folder shown in a narrow cell. */
export function baseName(path: string): string {
  return path.split("/").filter(Boolean).at(-1) ?? path;
}

export function messageWords(m: AutomationMessage, templateName?: (id: string) => string): string {
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
  const hm = d.toLocaleTimeString("en-GB", { hour: "2-digit", minute: "2-digit" });
  const day = (x: Date) => `${x.getFullYear()}-${x.getMonth()}-${x.getDate()}`;
  if (day(d) === day(now)) return `${hm} today`;
  const tomorrow = new Date(now);
  tomorrow.setDate(now.getDate() + 1);
  if (day(d) === day(tomorrow)) return `${hm} tomorrow`;
  const yesterday = new Date(now);
  yesterday.setDate(now.getDate() - 1);
  if (day(d) === day(yesterday)) return `${hm} yesterday`;
  const wd = d.toLocaleDateString("en-GB", { weekday: "short", day: "2-digit", month: "short" });
  return `${wd} ${hm}`;
}

/** The "next run" cell: the time and a sub-line, or a dash and WHY nothing is scheduled. */
export function nextRunWords(a: Automation, nowS: number): { main: string; sub: string } {
  if (a.next_run) return { main: whenWords(a.next_run.at, nowS), sub: untilWords(a.next_run.at, nowS) };
  switch (a.state) {
    case "off":
      return { main: "—", sub: a.consented_at == null ? "never enabled" : "turned off" };
    case "paused":
      return { main: "—", sub: a.paused_reason || "paused" };
    case "needs_reapproval":
      return { main: "—", sub: a.reapproval_reason || "needs your approval again" };
    case "expired":
      return { main: "—", sub: "past its end date" };
    case "finished":
      return { main: "—", sub: "its one run is done" };
    case "unreadable":
      return { main: "—", sub: "its settings can't be read" };
    default:
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
    const last = a.last_run?.reason ? ` The last run: ${a.last_run.reason}.` : "";
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
  if (!loopEnabled) return "Automations are switched off on this server";
  if (a.consented_at == null) return "Enable it first — it has never been approved";
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

export function runTriggerWords(r: RunSummary): string {
  if (r.trigger === "manual") return "Run now (you)";
  if (r.catch_up)
    return `catch-up · covered ${r.covered} missed ${r.covered === 1 ? "slot" : "slots"}`;
  return r.trigger === "once" ? "once" : "scheduled";
}

// ---- the editor's form ---------------------------------------------------------------------------

export type MessageMode = "text" | "template";

export interface EditorForm {
  name: string;
  triggerKind: "once" | "schedule" | "manual";
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
  /** `null` = the project's default checklist, `":none"` = none, else a checklist id. */
  checklistId: string | null;
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
}

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
    checklistId: null,
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
  if (act) {
    f.actionKind = act.kind;
    const msg = act.kind === "start_mission" ? act.instruction : act.message;
    if ("text" in msg) {
      f.messageMode = "text";
      f.text = msg.text;
    } else {
      f.messageMode = "template";
      f.templateId = msg.template_id;
      f.values = { ...msg.values };
    }
    if (act.kind === "start_mission") {
      f.projectId = act.project_id;
      f.checklistId = act.checklist_id;
      f.autonomy = act.autonomy;
    } else if (act.kind === "start_session") {
      f.engine = act.engine;
      f.folder = act.folder;
      f.bypass = act.bypass;
    } else {
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

function formMessage(f: EditorForm): AutomationMessage {
  if (f.messageMode === "text") return { text: f.text };
  // Only the fields the operator typed: a library field takes its value from the library at run
  // time, and the server refuses a value sent for one.
  const values: Record<string, string> = {};
  for (const [k, v] of Object.entries(f.values)) if (v !== "") values[k] = v;
  return { template_id: f.templateId, values };
}

export function formTrigger(f: EditorForm): Trigger {
  if (f.triggerKind === "manual") return { kind: "manual" };
  if (f.triggerKind === "once") return { kind: "once", at: f.onceAt, tz: f.tz };
  let cadence: Cadence;
  if (f.cadenceKind === "interval") cadence = { kind: "interval", every: f.every, unit: f.unit };
  else if (f.cadenceKind === "daily") cadence = { kind: "daily", time: f.time };
  else if (f.cadenceKind === "weekly")
    cadence = { kind: "weekly", days: WEEKDAYS.filter((d) => f.days.includes(d)), time: f.time };
  else cadence = { kind: "monthly", day: f.day, time: f.time };
  return { kind: "schedule", cadence, tz: f.tz };
}

export function formAction(f: EditorForm): AutomationAction {
  const message = formMessage(f);
  if (f.actionKind === "start_mission")
    return {
      kind: "start_mission",
      project_id: f.projectId,
      instruction: message,
      checklist_id: f.checklistId,
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
  return { kind: "send_to_session", session_key: f.sessionKey, message };
}

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
  return { name: f.name.trim(), trigger: formTrigger(f), action: formAction(f), policy };
}

/** What the form still needs before Save can be pressed — client-side only for fields whose
 *  absence would make the server's refusal unhelpful. The server validates everything. */
export function formProblems(f: EditorForm): string[] {
  const out: string[] = [];
  if (!f.name.trim()) out.push("Give it a name");
  if (f.triggerKind === "once" && !f.onceAt) out.push("Choose when it runs");
  if (f.triggerKind === "schedule" && f.cadenceKind === "weekly" && !f.days.length)
    out.push("Choose at least one day");
  if (f.actionKind === "start_mission" && !f.projectId) out.push("Choose a project");
  if (f.actionKind === "start_session" && !f.engine) out.push("Choose an agent");
  if (f.actionKind === "start_session" && !f.folder) out.push("Choose a folder");
  if (f.actionKind === "send_to_session" && !f.sessionKey) out.push("Choose a session");
  if (f.messageMode === "text" && !f.text.trim())
    out.push(f.actionKind === "start_mission" ? "Write the instruction" : "Write the message");
  if (f.messageMode === "template" && !f.templateId) out.push("Choose a template");
  return out;
}

// ---- consent -------------------------------------------------------------------------------------

/** The consent a refused request asks for, or `null` when the refusal is something else.
 *
 *  Two shapes carry it: a widening save / a first enable (422, `scope_digest` in the body) and a
 *  scope that changed between reading it and consenting (409, the fresh scope in the body). Either
 *  way the dialog shows the server's lines and resends with the server's digest — never one the
 *  client computed. */
export function consentFromError(e: unknown): ConsentRequired | null {
  if (!(e instanceof ApiError) || (e.status !== 422 && e.status !== 409)) return null;
  const r = e.record as ConsentRequired | undefined;
  if (!r || typeof r.scope_digest !== "string" || !Array.isArray(r.scope_lines)) return null;
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
  return widened.some((w) => WIDENED_KEYS.some(([item, l]) => item.test(w) && l.test(line)));
}

/** A copy's body: the same configuration under a new name. Always created OFF by the server. */
export function duplicateBody(a: Automation): AutomationConfig | null {
  if (!a.trigger || !a.action || !a.policy) return null;
  const trigger =
    // A once in the past cannot be saved; the copy becomes manual until the operator picks a time.
    a.trigger.kind === "once" && a.next_run == null ? ({ kind: "manual" } as Trigger) : a.trigger;
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
export function reconsent(next: ConsentRequired, prev: ConsentRequired | null): ConsentRequired {
  if (next.widened !== undefined) return next;
  return { ...next, widened: prev?.widened ?? [] };
}
