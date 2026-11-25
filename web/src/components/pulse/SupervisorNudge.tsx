/** A supervisor nudge as a decision (#983 P2, D3 / D7 / B1 / B7).
 *
 *  It names the objective, shows **Will type**: the exact text delivery will send, with the facts it
 *  was filled with and where they came from, and keeps the model's reason apart under **Why now · AI**.
 *  When the server says that text is no longer true it shows **not sendable**, what it was going to
 *  type and why, and only Dismiss. Every string from the server renders as text; the typed text is
 *  shown verbatim in a mono block. `ActionRow` owns the requests and the row frame. */
import { Send, X } from "lucide-react";

import type { DirectionFact } from "../../types/api";

import action from "../ui/actionButton.module.css";
import d from "./direction.module.css";
import { factLabel } from "./directionPlaceholders";
import { type NudgeView, provenance, sentence } from "./supervisorNudge";

function clock(ts: number): string {
  return new Date(ts * 1000).toLocaleTimeString([], { hour: "2-digit", minute: "2-digit" });
}

/** A check state reports a real outcome, so it carries its status colour; nothing else does. */
function CheckDot({ fact }: { fact: DirectionFact }) {
  if (fact.name !== "checks") return null;
  const v = String(fact.value);
  const tone =
    v === "success"
      ? d.dotUp
      : v === "pending" || v === "warning"
        ? d.dotDegraded
        : v === "failure" || v === "error"
          ? d.dotDown
          : "";
  return <span className={`${d.dot} ${tone}`} aria-hidden="true" />;
}

export function SupervisorNudge({
  view,
  busy,
  onSend,
  onDismiss,
}: {
  view: NudgeView;
  busy: "" | "approve" | "reject";
  onSend: () => void;
  onDismiss: () => void;
}) {
  const direction = view.source === "direction";
  const prov = provenance(view.facts, clock);
  return (
    <div
      className={d.nudge}
      data-testid="nudge-row"
      data-sendable={view.sendable ? "true" : "false"}
    >
      <div className={d.head}>
        <span className={d.tag}>{direction ? "Nudge · direction" : "Nudge · default nudge"}</span>
        <span className={d.objective} data-testid="nudge-objective">
          {view.objective}
        </span>
        {view.sendable ? null : (
          <span className={d.badge} data-testid="nudge-not-sendable">
            not sendable
          </span>
        )}
      </div>

      {view.sendable ? (
        view.why ? (
          <div role="group" aria-label="Why now, written by the AI" data-testid="nudge-why-group">
            <div className={d.sectionLabel}>
              Why now <span className={d.aiFlag}>AI</span>
            </div>
            <p className={d.why} data-testid="nudge-why">
              {view.why}
            </p>
          </div>
        ) : null
      ) : (
        <p className={d.staleLead} data-testid="nudge-stale">
          <b>This text is no longer true, so nothing will be typed.</b>{" "}
          <span data-testid="nudge-stale-reason">{sentence(view.reason)}</span>
        </p>
      )}

      <div>
        <div className={d.sectionLabel}>
          {!view.sendable
            ? "Was going to type"
            : direction
              ? "Will type · your direction, filled"
              : "Will type · your default nudge"}
        </div>
        <pre
          className={view.sendable ? d.willType : `${d.willType} ${d.willTypeStale}`}
          data-testid="nudge-text"
        >
          {view.text}
        </pre>
        {view.facts.length > 0 ? (
          <ul className={d.facts} aria-label="Checked facts">
            {view.facts.map((f) => (
              <li key={f.name} className={d.fact} data-testid="nudge-fact" data-name={f.name}>
                <CheckDot fact={f} />
                {factLabel(f.name, f.value)}
              </li>
            ))}
          </ul>
        ) : null}
        {prov ? (
          <p className={d.prov} data-testid="nudge-provenance">
            {prov}
          </p>
        ) : null}
      </div>

      {view.canSend || view.canDismiss ? (
        <div className={d.btns}>
          {view.canSend ? (
            <button
              type="button"
              className={action.primary}
              disabled={!!busy}
              onClick={onSend}
              data-testid="nudge-send"
            >
              <Send size={14} aria-hidden="true" />
              {busy === "approve" ? "Sending…" : direction ? "Send direction" : "Send nudge"}
            </button>
          ) : null}
          {view.canDismiss ? (
            <button
              type="button"
              className={action.ghost}
              disabled={!!busy}
              onClick={onDismiss}
              data-testid={view.sendable ? "nudge-reject" : "nudge-dismiss"}
            >
              {view.sendable ? (
                <>
                  <X size={14} aria-hidden="true" />
                  Reject
                </>
              ) : (
                "Dismiss"
              )}
            </button>
          ) : null}
        </div>
      ) : null}
    </div>
  );
}
