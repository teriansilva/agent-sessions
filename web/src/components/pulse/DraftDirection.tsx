/** An AI-drafted direction as a decision (#983 P3, D5 / B2 / B4).
 *
 *  Dashed and labelled AI-drafted, because the words are the model's. It names the objective, says
 *  why there is a draft at all, and shows the text in the same Will type block as a nudge,
 *  verbatim: what Send as written types. Send does not take focus on its own. Edit hands the text to
 *  its session's message box under Context, where it goes out as the operator's own message and
 *  replaces the draft.
 *  There is no state on this card in which it has been sent without a tap, because no such state
 *  exists. `ActionRow` owns the requests and the row frame. */
import { Pencil, Send, X } from "lucide-react";

import action from "../ui/actionButton.module.css";
import d from "./direction.module.css";
import type { DraftView } from "./draftDirection";

export function DraftDirection({
  view,
  busy,
  boxed,
  onSend,
  onEdit,
  onDismiss,
}: {
  view: DraftView;
  busy: "" | "approve" | "reject";
  /** Draw the dashed frame here. Where the host row has a frame of its own (the console), the host
   *  row is dashed instead; an embedded row has none, so the card carries it. */
  boxed?: boolean;
  onSend: () => void;
  onEdit: () => void;
  onDismiss: () => void;
}) {
  return (
    <div
      className={boxed ? `${d.nudge} ${d.draftBox}` : d.nudge}
      role="group"
      aria-label={`AI-drafted direction: ${view.objective}`}
      data-testid="draft-card"
    >
      <div className={d.head}>
        <span className={d.draftFlag}>
          <Pencil size={11} aria-hidden="true" />
          AI-drafted direction
        </span>
        <span className={d.objective} data-testid="draft-objective">
          {view.objective}
        </span>
        <span className={d.badge} data-testid="draft-waits">
          waits for your tap
        </span>
      </div>
      <p className={d.why} data-testid="draft-lead">
        This objective has no direction, so mission control drafted one from the session. It is
        typed exactly as shown, so read it first.
      </p>
      <div>
        <div className={d.sectionLabel}>Will type · drafted by the AI</div>
        <pre className={d.willType} data-testid="draft-text">
          {view.text}
        </pre>
      </div>
      {view.canSend || view.canEdit || view.canDismiss ? (
        <div className={d.btns}>
          {view.canSend ? (
            <button
              type="button"
              className={action.primary}
              disabled={!!busy}
              onClick={onSend}
              data-testid="draft-send"
            >
              <Send size={14} aria-hidden="true" />
              {busy === "approve" ? "Sending…" : "Send as written"}
            </button>
          ) : null}
          {view.canEdit ? (
            <button
              type="button"
              className={action.ghost}
              disabled={!!busy}
              onClick={onEdit}
              data-testid="draft-edit"
            >
              <Pencil size={14} aria-hidden="true" />
              Edit
            </button>
          ) : null}
          {view.canDismiss ? (
            <button
              type="button"
              className={action.ghost}
              disabled={!!busy}
              onClick={onDismiss}
              data-testid="draft-dismiss"
            >
              <X size={14} aria-hidden="true" />
              Dismiss
            </button>
          ) : null}
        </div>
      ) : null}
      <p className={d.draftHint} data-testid="draft-hint">
        Never sent on its own: it waits for your tap.
        {view.canEdit ? (
          <>
            {" "}
            <b>Edit</b> opens it in this session&apos;s message box, under Context, where you send
            it as your own message.
          </>
        ) : null}
      </p>
    </div>
  );
}
