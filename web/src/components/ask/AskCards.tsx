/** The cards inside an Ask turn (#1443 Phase 2, the mockup's `.card`).
 *
 *  **Question** — Ask stopped to ask one thing. Each option is a reply the SERVER stored with the
 *  question; tapping one sends its index, never words of the client's own. Free text is offered
 *  only when the question allows it. An unanswered question never blocks the box.
 *
 *  **Nudge** — a message Ask proposed for a session no mission holds. The exact text sits in a mono
 *  well, verbatim. The one filled control names its effect ("Send to …"); it and Dismiss post to
 *  the SAME approve / reject routes every other decision uses, so the delivery fence (screen
 *  re-checked before the first byte) is the existing one. Once decided, the controls give way to
 *  an outcome row — an LED plus a WORD, never colour alone — and the words are honest: "Delivered"
 *  means the terminal accepted the text, not that the agent did anything with it.
 */
import { Eye, Send } from "lucide-react";
import { useState } from "react";

import type { AskProposalCard, AskQuestionCard } from "../../types/api";

import c from "./AskCards.module.css";

export function QuestionCard({
  ask,
  disabled,
  onAnswer,
}: {
  ask: AskQuestionCard;
  disabled: boolean;
  onAnswer: (reply: { option: number } | { text: string }) => void;
}) {
  const [text, setText] = useState("");
  const off = disabled || ask.answered;
  // The draft is never cleared on send: a failed answer keeps it to retry, and once the question
  // is ANSWERED (settled) the box is not rendered at all.
  return (
    <div className={c.card} data-testid="ask-question">
      <div className={c.head}>
        <span className={c.k}>Question</span>
        <span className={c.st}>{ask.answered ? "Answered" : "Waiting for you"}</span>
      </div>
      <div className={c.body}>
        <p className={c.lead}>{ask.question}</p>
        {ask.options.length > 0 ? (
          <div className={c.opts} role="group" aria-label="Answers">
            {ask.options.map((o, i) => (
              <button
                key={i}
                type="button"
                className={c.opt}
                disabled={off}
                onClick={() => onAnswer({ option: i })}
                data-testid="ask-question-option"
              >
                {o}
              </button>
            ))}
          </div>
        ) : null}
        {ask.allow_text && !ask.answered ? (
          <form
            className={c.freeRow}
            onSubmit={(e) => {
              e.preventDefault();
              const t = text.trim();
              if (!t || off) return;
              onAnswer({ text: t });
            }}
          >
            <input
              aria-label="Your own answer"
              placeholder="Or type your own answer…"
              value={text}
              disabled={off}
              onChange={(e) => setText(e.target.value)}
              data-testid="ask-question-text"
            />
            <button type="submit" className={c.ghost} disabled={off || !text.trim()}>
              Answer
            </button>
          </form>
        ) : null}
        {!ask.answered ? (
          <div className={c.note}>
            You can also just send a new message; this question does not block the box.
          </div>
        ) : null}
      </div>
    </div>
  );
}

type Tone = "ok" | "warn" | "bad" | "neutral";

/** The outcome words for a settled or in-flight proposal — from the ledger's own state. */
function outcome(p: AskProposalCard): { word: string; tone: Tone; detail: string } | null {
  if (p.projection === "actionable") return null;
  if (p.projection === "unknown")
    return {
      word: "Status unavailable",
      tone: "warn",
      detail: "The decision log could not be read just now; this updates on its own.",
    };
  if (p.projection === "historical")
    return { word: "No longer tracked", tone: "neutral", detail: "This decision has left the log." };
  if (p.projection === "in_flight_revocable")
    return {
      word: "Queued",
      tone: "neutral",
      detail: "Approved and waiting to be typed. You can still dismiss it until it starts.",
    };
  if (p.projection === "in_flight_locked")
    return { word: "Sending", tone: "neutral", detail: "Being typed into the session now." };
  switch (p.state) {
    case "delivered":
      return {
        word: "Delivered",
        tone: "ok",
        detail: "The terminal accepted the text. That is not the agent having done it.",
      };
    case "stale":
      return {
        word: "Out of date",
        tone: "warn",
        detail: "The screen moved on; nothing was sent. Ask again if it still applies.",
      };
    case "expired":
      return { word: "Expired", tone: "warn", detail: "Nobody approved it in time; nothing was sent." };
    case "rejected":
      return { word: "Dismissed", tone: "neutral", detail: "Nothing was sent." };
    case "indeterminate":
      return {
        word: "Uncertain",
        tone: "bad",
        detail: "It may or may not have been typed — check the session. It is not retried.",
      };
    default:
      return {
        word: "Failed",
        tone: "bad",
        detail: p.detail || "Nothing was confirmed sent.",
      };
  }
}

export function NudgeCard({
  p,
  deciding,
  onApprove,
  onDismiss,
}: {
  p: AskProposalCard;
  deciding: boolean;
  onApprove: () => void;
  onDismiss: () => void;
}) {
  const out = outcome(p);
  const edge =
    out?.tone === "warn" ? c.edgeWarn : out?.tone === "bad" ? c.edgeBad : "";
  return (
    <div className={`${c.card} ${edge}`} data-testid="ask-nudge" data-state={p.state ?? p.projection}>
      <div className={c.head}>
        <span className={c.k}>Proposal</span>
        Nudge
        <span className={c.st}>{out ? out.word : "Waiting for you"}</span>
      </div>
      <div className={`${c.body} ${out ? c.muted : ""}`}>
        <dl className={c.kv}>
          <dt>Session</dt>
          <dd>{p.title || p.key}</dd>
        </dl>
        <div className={c.blockLabel}>Text to send, verbatim</div>
        <div className={c.verbatim} data-testid="ask-nudge-text">
          {p.text}
        </div>
        {!out ? (
          <div className={c.note}>
            <Eye size={14} aria-hidden="true" />
            <span>
              Sent only if the screen still shows the same prompt. Checked again before the
              first byte; if it moved on, nothing is sent.
            </span>
          </div>
        ) : null}
      </div>
      {!out ? (
        <div className={c.actions}>
          <button
            type="button"
            className={c.cta}
            disabled={deciding || !p.can_approve}
            onClick={onApprove}
            data-testid="ask-nudge-send"
          >
            <Send size={15} aria-hidden="true" />
            Send to {p.title || "this session"}
          </button>
          <button
            type="button"
            className={c.ghost}
            disabled={deciding || !p.can_reject}
            onClick={onDismiss}
            data-testid="ask-nudge-dismiss"
          >
            Dismiss
          </button>
        </div>
      ) : (
        <>
          <div className={c.outcome} role="status" data-testid="ask-nudge-outcome">
            <span className={`${c.led} ${c[`led_${out.tone}`]}`} aria-hidden="true" />
            <span className={`${c.ow} ${c[`ow_${out.tone}`]}`}>{out.word}</span>
            <span className={c.od}>{out.detail}</span>
          </div>
          {p.can_reject ? (
            // Whatever the server projection permits stays on offer — an approved message that
            // has not been claimed yet can still be revoked.
            <div className={c.actions}>
              <button
                type="button"
                className={c.ghost}
                disabled={deciding}
                onClick={onDismiss}
                data-testid="ask-nudge-dismiss"
              >
                Dismiss
              </button>
            </div>
          ) : null}
        </>
      )}
    </div>
  );
}
