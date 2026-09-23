/** What one mission timeline event IS, as the thread draws it (#967 P4, #966 P2).
 *
 *  Pure, so every kind is pinned by a unit test without a DOM: `MissionThreadEvent` only draws the
 *  model this returns. The rule it replaces printed the raw `kind` above `event.text`, and a `state`
 *  event carries its from and to in `meta` with no text at all, so a state change read as the word
 *  "state" over an empty row.
 *
 *  Nothing here reads a field as markup, and nothing prints `meta` wholesale: every value that reaches
 *  the screen is a named string the server writes for that kind. An unknown kind falls back to a
 *  generic row with its name humanised and its own text, never its meta. */
import type { Mission, MissionEvent } from "../../types/api";

import { isPersistableKey } from "../overview/windowStore";

/** `engine:uuid` → `/s/:engine/:uuid`, both halves encoded. */
export function sessionRoute(key: string): string {
  const i = key.indexOf(":");
  const engine = i < 0 ? key : key.slice(0, i);
  const uuid = i < 0 ? "" : key.slice(i + 1);
  return `/s/${encodeURIComponent(engine)}/${encodeURIComponent(uuid)}`;
}

export type ThreadRow =
  /** Conversation: the operator's words and the answer to them. */
  | { type: "message"; who: "You" | "Answer" }
  /** A lifecycle move, drawn as two chips. `note` is the why or the detail, when there is one. */
  | { type: "state"; from: string; to: string; note: string | null }
  /** A start that failed: a `state` event from `dispatching` to `failed`, the ONLY way a start fails. */
  | {
      type: "failure";
      from: string;
      to: string;
      message: string;
      detail: string | null;
      sessionKey: string | null;
    }
  /** An `error` event. Its one server writer is `mission_questions._notice`: a question that could not
   *  be asked or delivered. It is not a start failure, so it never gets chips or Start again. */
  | { type: "error"; text: string }
  | { type: "plan"; projectId: string | null; engine: string | null; brief: string }
  | { type: "plan_edit"; changed: string[] }
  | { type: "planning"; outcome: string; label: string; note: string | null }
  /** A supervisor nudge that was typed (#983): whose words it was, and the delivered snapshot. */
  | {
      type: "nudged";
      objectiveKey: string | null;
      /** `ai_draft`: an AI-drafted direction the operator approved (#983 P3).
       *  `ai_auto`: one the opt-in sent with nobody reading it (#983 P4). The two are separate
       *  values, never one flag on top of `ai_draft`, because they are different events: the
       *  operator read one and did not read the other. */
      source: "direction" | "default_nudge" | "ai_draft" | "ai_auto" | null;
      /** Verbatim, exactly as the server recorded what was typed. */
      text: string;
      /** The model's self-reported confidence, for an autonomously sent direction only. */
      confidence?: number;
    }
  /** A supervisor nudge that was not typed, with the server's reason (#983). `draft` marks an
   *  AI-drafted direction that was dismissed, replaced by an edit, or went stale (P3). */
  | { type: "held"; objectiveKey: string | null; reason: string; draft?: true }
  /** A checklist change. The event carries no text — only `meta.ops`, one `{op, key}` per edit —
   *  so it used to reach the generic row as the word "Objective" over an empty box. `counts` is one
   *  entry per verb in first-seen order; keys are not printed, because a dropped objective's title
   *  no longer exists to show. */
  | { type: "checklist"; by: "plan" | "you" | "other"; counts: Array<[string, number]>; reopened: boolean }
  /** A session joining or leaving the mission. Same history: `meta.adopted` / `meta.detached` only. */
  | { type: "session"; change: "adopted" | "released"; role: string | null; sessionKey: string | null }
  | { type: "system"; label: string; text: string | null };

/** How a checklist edit reads, per `op`. Only verbs the store writes; anything else is dropped rather
 *  than printed raw. */
const CHECKLIST_VERBS: Record<string, string> = {
  add: "added",
  drop: "dropped",
  retitle: "renamed",
  waive: "waived",
  reorder: "reordered",
  set_direction: "given a direction",
  reset_direction: "direction reset",
  clear_direction: "direction cleared",
};

function str(meta: Record<string, unknown> | null, key: string): string | null {
  const v = meta?.[key];
  return typeof v === "string" && v.trim() ? v : null;
}

function text(e: MissionEvent): string | null {
  return typeof e.text === "string" && e.text.trim() ? e.text : null;
}

/** A finite number from `meta`, or null. `typeof NaN === "number"`, so the finite test is the
 *  point: a confidence the server could not write properly must not render as "NaN%". */
function num(meta: Record<string, unknown> | null, key: string): number | null {
  const v = meta?.[key];
  return typeof v === "number" && Number.isFinite(v) ? v : null;
}

/** `dispatching -> failed: detail` is how the settlement spells its text. The chips already say the
 *  first half, so only what follows it is worth a line. */
function withoutTransition(t: string | null): string | null {
  if (!t) return null;
  const rest = t.replace(/^\s*[a-z_]+\s*->\s*[a-z_]+\s*(?::\s*)?/i, "");
  return rest.trim() ? rest : null;
}

/** Where a failed start's Open session link points (#967 P4, PR #986 review).
 *
 *  `meta.launch_session_key` first: the server copies it from the dispatch record the launch stamped,
 *  because the production failure path writes no `session_key` on the event. It is a display
 *  identity, not ownership. Then the keys an older event could carry. Never guessed from a
 *  neighbouring `session` event. Each candidate must pass the same `engine:native` shape check the
 *  overview uses before it may become a link, so a malformed or placeholder value builds none. */
function launchTarget(
  e: MissionEvent,
  meta: Record<string, unknown> | null,
): string | null {
  for (const key of [str(meta, "launch_session_key"), e.session_key, str(meta, "session_key")]) {
    if (key && isPersistableKey(key)) return key;
  }
  return null;
}

/** Is this event a start that failed? Only these can carry the Start again actions. An `error` event
 *  is not one: see the `error` row. */
export function isFailedStart(e: MissionEvent): boolean {
  return (
    e.kind === "state" &&
    str(e.meta, "from") === "dispatching" &&
    str(e.meta, "to") === "failed"
  );
}

/** The newest failed start among the events on screen, by `seq` rather than by position, so the rule
 *  does not depend on the order the page arrived in. */
export function latestFailedStartSeq(events: MissionEvent[]): number | null {
  let seq: number | null = null;
  for (const e of events) if (isFailedStart(e) && (seq === null || e.seq > seq)) seq = e.seq;
  return seq;
}

const PLAN_FIELDS: Record<string, string> = {
  project_id: "project",
  engine: "engine",
  brief: "brief",
};

const PLANNING_LABELS: Record<string, string> = {
  skipped: "No plan proposed",
  failed: "Couldn't plan",
  discarded: "Planning result discarded",
  recovered: "Plan recovered",
  project_conflict: "Project kept",
};

function humanise(kind: string): string {
  const words = kind.replace(/_/g, " ").trim();
  return words ? words[0].toUpperCase() + words.slice(1) : "Event";
}

export function threadRow(e: MissionEvent): ThreadRow {
  const meta = e.meta && typeof e.meta === "object" ? e.meta : null;
  if (e.kind === "operator_msg") return { type: "message", who: "You" };
  if (e.kind === "assistant_msg") return { type: "message", who: "Answer" };

  if (e.kind === "error") {
    return { type: "error", text: text(e) ?? "An error was recorded." };
  }

  if (isFailedStart(e)) {
    const detail = str(meta, "detail");
    const note = withoutTransition(text(e));
    const message = str(meta, "message") ?? note ?? "The start failed.";
    return {
      type: "failure",
      from: "dispatching",
      to: "failed",
      message,
      detail: detail && detail !== message ? detail : null,
      sessionKey: launchTarget(e, meta),
    };
  }

  if (e.kind === "state") {
    const from = str(meta, "from");
    const to = str(meta, "to");
    if (from && to) {
      return {
        type: "state",
        from,
        to,
        note: str(meta, "why") ?? str(meta, "detail") ?? withoutTransition(text(e)),
      };
    }
  }

  if (e.kind === "plan") {
    return {
      type: "plan",
      projectId: str(meta, "project_id"),
      engine: str(meta, "engine"),
      brief: text(e) ?? "",
    };
  }

  if (e.kind === "plan_edit") {
    const changed = Array.isArray(meta?.changed) ? meta.changed : [];
    return {
      type: "plan_edit",
      changed: changed
        .filter((f): f is string => typeof f === "string" && f.trim() !== "")
        .map((f) => PLAN_FIELDS[f] ?? f.replace(/_/g, " ")),
    };
  }

  if (e.kind === "planning") {
    const outcome = str(meta, "outcome") ?? "";
    // The planner leads its text with the same words as the label; the reason is what follows.
    const note = text(e)?.replace(/^\s*(no plan proposed|could not plan)\s*(?::\s*)?/i, "") ?? "";
    return {
      type: "planning",
      outcome,
      label: PLANNING_LABELS[outcome] ?? "Planning",
      note: note.trim() ? note : null,
    };
  }

  // A SUPERVISOR NUDGE (#983). `stage` names which record this is: `delivered` carries the text that
  // was typed, verbatim; `held` carries why nothing was. An event written before stages existed has
  // no stage, and it was always a held one.
  if (e.kind === "action" && str(meta, "source") === "supervisor") {
    const objectiveKey = str(meta, "objective_key");
    if (str(meta, "stage") === "delivered") {
      const src = str(meta, "text_source");
      // `ai_auto` is read from `text_source`, which the server mints from the RECORDED `sent_by` —
      // so a row keeps saying it was sent unreviewed however the toggle stands when it is drawn.
      const source =
        src === "direction" || src === "default_nudge" || src === "ai_draft" || src === "ai_auto"
          ? src
          : null;
      const confidence = num(meta, "confidence");
      return {
        type: "nudged",
        objectiveKey,
        source,
        text: typeof e.text === "string" ? e.text : "",
        ...(source === "ai_auto" && confidence !== null ? { confidence } : {}),
      };
    }
    const reason = (text(e) ?? "")
      .replace(
        /^\s*(A nudge was prepared but not delivered|An AI-drafted direction was not sent):\s*/i,
        "",
      )
      .trim();
    return {
      type: "held",
      objectiveKey,
      reason: reason || "it was not sent",
      ...(meta?.draft === true ? { draft: true as const } : {}),
    };
  }

  // …and a direction that could not be filled, which the supervisor held and escalated (#983). Its
  // text leads with the objective's title, which the row already names, so the reason is what follows.
  if (e.kind === "escalation" && meta?.held === "direction") {
    const t = text(e) ?? "";
    const at = t.indexOf("its direction could not be filled");
    return {
      type: "held",
      objectiveKey: str(meta, "objective_key"),
      reason: (at >= 0 ? t.slice(at) : t).trim() || "its direction could not be filled",
    };
  }

  // A CHECKLIST CHANGE with nothing in its text (#1063's screenshot: an empty "Objective" box). An
  // `objective` event that DOES carry text — "no checklist applied: …", "nothing on this checklist
  // gates completion …" — is a sentence the server wrote for a person, and keeps the generic row.
  if (e.kind === "objective" && !text(e) && Array.isArray(meta?.ops)) {
    const counts = new Map<string, number>();
    for (const op of meta.ops) {
      const kind = op && typeof op === "object" ? (op as Record<string, unknown>).op : null;
      const verb = typeof kind === "string" ? CHECKLIST_VERBS[kind] : undefined;
      if (verb) counts.set(verb, (counts.get(verb) ?? 0) + 1);
    }
    if (counts.size) {
      const by = str(meta, "by");
      return {
        type: "checklist",
        by: by === "instantiation" ? "plan" : by === "operator" ? "you" : "other",
        counts: [...counts.entries()],
        reopened: meta.reopened === true,
      };
    }
  }

  // A SESSION JOINING OR LEAVING, likewise text-less. A sub-agent spawn carries its own sentence and
  // is left to the generic row.
  if (e.kind === "session" && !text(e)) {
    const sessionKey = e.session_key && isPersistableKey(e.session_key) ? e.session_key : null;
    const role = str(meta, "adopted");
    if (role) return { type: "session", change: "adopted", role, sessionKey };
    if (meta?.detached === true) return { type: "session", change: "released", role: null, sessionKey };
  }

  return { type: "system", label: humanise(e.kind), text: text(e) };
}

/** "2 added · 1 waived" — what a checklist row says after its "Checklist" tag. */
export function checklistSummary(counts: Array<[string, number]>): string {
  return counts.map(([verb, n]) => `${n} ${verb}`).join(" · ");
}

/** May this mission be started again? The DETAIL's answer, never an event's.
 *
 *  `get_mission` computes `retry_eligible` from the same predicate the state write re-checks inside
 *  its transaction. The failure event carries a `retry_eligible` too, and it is a snapshot from the
 *  moment the launch settled: a retained teardown record discharged later, or a mission that went on
 *  to run, changes the answer without touching the event. */
export function canStartAgain(mission: Mission | null): boolean {
  return (
    mission !== null &&
    mission.state === "failed" &&
    mission.archived_at == null &&
    mission.retry_eligible === true
  );
}
