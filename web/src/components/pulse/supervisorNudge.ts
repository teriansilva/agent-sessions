/** A supervisor nudge's decision row, decided without a DOM (#983 P2).
 *
 *  The row shows the EXACT text delivery will type (`render.text`, persisted at proposal), the facts
 *  it was filled with, and the model's reason kept apart. Whether it can still be sent is the
 *  server's answer, never re-derived here: `render_status` comes from the delivery path's own
 *  comparison, and the server also withdraws `can_approve` when it says no. This refuses Send on
 *  either signal, so a response that carries one without the other still cannot offer a Send that
 *  delivery would refuse. */
import type { DirectionFact, OrchestratorAction } from "../../types/api";

export interface NudgeView {
  /** The objective's title, or its key when the title could not be read. */
  objective: string;
  source: "direction" | "default_nudge";
  /** Verbatim. Never trimmed, re-wrapped or re-rendered on the client. */
  text: string;
  facts: DirectionFact[];
  sendable: boolean;
  /** The server's own words for why it is not sendable. Empty when it is. */
  reason: string;
  canSend: boolean;
  canDismiss: boolean;
  /** The model's reason for nudging now. Shown only beside a text that can still be sent. */
  why: string | null;
}

export function isSupervisorNudge(a: OrchestratorAction): boolean {
  return (
    a.verb === "continue" &&
    a.source === "supervisor" &&
    typeof a.render?.text === "string"
  );
}

export function nudgeView(
  a: OrchestratorAction,
  controls: { approvable: boolean; rejectable: boolean },
): NudgeView | null {
  if (!isSupervisorNudge(a) || !a.render) return null;
  const sendable = a.render_status?.sendable !== false;
  const facts = Array.isArray(a.render.facts)
    ? a.render.facts.filter((f) => f && typeof f.name === "string")
    : [];
  return {
    objective: a.objective_title?.trim() || a.objective_key || "an objective",
    source: a.render.source === "direction" ? "direction" : "default_nudge",
    text: a.render.text,
    facts,
    sendable,
    reason: sendable
      ? ""
      : a.render_status?.reason?.trim() || "the server did not say what changed",
    canSend: sendable && controls.approvable,
    canDismiss: controls.rejectable,
    why: sendable ? a.rationale?.trim() || a.title?.trim() || null : null,
  };
}

/** Where the facts came from, in one line: when they were checked, for which repo and head. */
export function provenance(
  facts: readonly DirectionFact[],
  clock: (ts: number) => string,
): string | null {
  if (facts.length === 0) return null;
  const observed = facts.filter((f) => typeof f.observed_at === "number");
  if (observed.length === 0) {
    return "from this objective's probe settings, not read from the session";
  }
  const at = Math.max(...observed.map((f) => f.observed_at as number));
  const parts = [`checked at ${clock(at)}`];
  const target = (observed[0].target ?? {}) as Record<string, unknown>;
  if (typeof target.repo === "string" && target.repo) parts.push(target.repo);
  if (typeof target.head === "string" && target.head) parts.push(`head ${target.head.slice(0, 7)}`);
  return `${parts.join(" · ")} · by this objective's own probe, not read from the session`;
}

/** A server clause as a sentence: first letter up, one closing full stop. */
export function sentence(s: string): string {
  const t = s.trim();
  if (!t) return t;
  const up = t[0].toUpperCase() + t.slice(1);
  return /[.!?]$/.test(up) ? up : `${up}.`;
}
