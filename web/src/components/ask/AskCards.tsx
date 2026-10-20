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
 *
 *  **Session** (#1443 Phase 3a) — a session Ask proposed to start: the project, the agent and the
 *  brief, verbatim. Permission bypass is a control ON the card, starting from the new-session
 *  form's default; the tap carries what it shows. "Started" means the agent's own store has the
 *  session and it received the brief; anything less proved says so, and nothing is retried.
 */
import { Eye, Play, Send } from "lucide-react";
import { type ReactNode, useState } from "react";

import type { AskNudgeCard, AskQuestionCard, AskSessionCard, AskUpdate } from "../../types/api";

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
function outcome(p: AskNudgeCard): { word: string; tone: Tone; detail: string } | null {
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
  p: AskNudgeCard;
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

/** The outcome words for a session proposal — from the server's own state. */
function sessionOutcome(p: AskSessionCard): { word: string; tone: Tone; detail: string } | null {
  switch (p.state) {
    case "pending":
      return null;
    case "deciding":
      return {
        word: "Starting",
        tone: "neutral",
        detail: "Launching, and waiting for the agent to confirm it started and got the brief.",
      };
    case "started":
      return {
        word: "Started",
        tone: "ok",
        detail: "The agent has the session and received the brief. That is not the work done.",
      };
    case "unconfirmed":
      return { word: "Not confirmed", tone: "bad", detail: p.reason || "Check your sessions." };
    case "failed":
      return { word: "Failed", tone: "bad", detail: p.reason || "Nothing was started." };
    case "stale":
      return {
        word: "Out of date",
        tone: "warn",
        detail: p.reason ? `${p.reason}. Nothing was started.` : "Nothing was started.",
      };
    case "dismissed":
      return { word: "Dismissed", tone: "neutral", detail: "Nothing was started." };
    case "interrupted":
      return { word: "Interrupted", tone: "bad", detail: p.reason || "Check your sessions." };
    case "unknown":
      return {
        word: "Status unavailable",
        tone: "warn",
        detail: "This proposal could not be read just now; this updates on its own.",
      };
    default:
      return {
        word: "No longer tracked",
        tone: "neutral",
        detail: "There is no record to decide, so nothing will start.",
      };
  }
}

export function SessionCard({
  p,
  deciding,
  error,
  onStart,
  onDismiss,
  ways,
}: {
  p: AskSessionCard;
  deciding: boolean;
  /** Why the last tap was refused, when the card itself did not change. */
  error: string | null;
  onStart: (bypass: boolean) => void;
  onDismiss: () => void;
  /** Open / Open in map, for a started session. */
  ways?: ReactNode;
}) {
  // The control starts from the card's starting value. A NEW starting value (the default changed
  // and the card asks again) remounts the card — its parent keys it on that value.
  const [bypass, setBypass] = useState(p.bypass_default);
  const out = sessionOutcome(p);
  const edge = out?.tone === "warn" ? c.edgeWarn : out?.tone === "bad" ? c.edgeBad : "";
  const name = p.title || "this project";
  return (
    <div className={`${c.card} ${edge}`} data-testid="ask-session" data-state={p.state}>
      <div className={c.head}>
        <span className={c.k}>Proposal</span>
        Session
        <span className={c.st}>{out ? out.word : "Waiting for you"}</span>
      </div>
      <div className={`${c.body} ${out && p.state !== "deciding" ? c.muted : ""}`}>
        <dl className={c.kv}>
          <dt>Project</dt>
          <dd>{p.title}</dd>
          <dt>Agent</dt>
          <dd>{p.agent}</dd>
          {out && p.bypass !== null ? (
            <>
              <dt>Bypass</dt>
              <dd data-testid="ask-session-bypass-used">
                {p.bypass ? "On — it does not ask before acting" : "Off — it asks before acting"}
              </dd>
            </>
          ) : null}
        </dl>
        <div className={c.blockLabel}>Brief, verbatim</div>
        <div className={c.verbatim} data-testid="ask-session-brief">
          {p.text}
        </div>
        {!out ? (
          <>
            <label className={c.switchRow} data-testid="ask-session-bypass">
              <input
                type="checkbox"
                role="switch"
                checked={bypass}
                disabled={deciding || !p.can_start}
                onChange={(e) => setBypass(e.target.checked)}
                aria-describedby={`bypass-${p.action_id}`}
              />
              <span className={c.switchText}>
                <span className={c.switchName}>Permission bypass · {bypass ? "On" : "Off"}</span>
                <span id={`bypass-${p.action_id}`} className={c.switchRisk}>
                  {bypass
                    ? "The agent runs commands and edits files without asking you first."
                    : "The agent asks you before it runs commands or edits files."}
                </span>
              </span>
            </label>
            {p.notice === "busy" ? (
              <div className={c.note} role="status" data-testid="ask-session-notice">
                <span>
                  The project was being changed just as this started, so nothing started. Start
                  it again.
                </span>
              </div>
            ) : null}
            {p.notice === "default_changed" ? (
              <div className={c.note} role="status" data-testid="ask-session-notice">
                <span>
                  Your default for permission bypass changed since this was proposed. Check the
                  setting above and start again.
                </span>
              </div>
            ) : null}
            {error ? (
              <div className={c.note} role="alert" data-testid="ask-session-error">
                <span>{error}</span>
              </div>
            ) : null}
          </>
        ) : null}
      </div>
      {!out ? (
        <div className={c.actions}>
          <button
            type="button"
            className={c.cta}
            disabled={deciding || !p.can_start}
            onClick={() => onStart(bypass)}
            data-testid="ask-session-start"
          >
            <Play size={15} aria-hidden="true" />
            Start session in {name}
          </button>
          <button
            type="button"
            className={c.ghost}
            disabled={deciding || !p.can_dismiss}
            onClick={onDismiss}
            data-testid="ask-session-dismiss"
          >
            Dismiss
          </button>
        </div>
      ) : (
        <div className={c.outcome} role="status" data-testid="ask-session-outcome">
          <span className={`${c.led} ${c[`led_${out.tone}`]}`} aria-hidden="true" />
          <span className={`${c.ow} ${c[`ow_${out.tone}`]}`}>{out.word}</span>
          <span className={c.od}>{out.detail}</span>
          {p.state === "started" && ways ? <span className={c.ways}>{ways}</span> : null}
        </div>
      )}
    </div>
  );
}

/** A followed session changed (#1443 Phase 4): a compact row between turns — an LED plus a WORD,
 *  the session's title, a way in, and Unfollow while it is still followed. The words are authored
 *  here from the update's kind. */
const UPDATE_WORDS: Record<AskUpdate["kind"], { word: string; tone: Tone; detail: string }> = {
  finished: { word: "Finished its turn", tone: "neutral", detail: "It stopped working." },
  needs_you: { word: "Needs you", tone: "warn", detail: "It is waiting for your decision." },
  ended: {
    word: "No longer followed",
    tone: "neutral",
    detail: "It was archived or is gone.",
  },
};

/** One session the conversation follows (#1443 Phase 4). Listed whether or not it has an update
 *  yet, so Unfollow never waits for the first change (Hermes on #1500). Not a state, so no LED:
 *  the flat accent square is the non-status bullet (design.md §3). */
export function FollowingRow({
  title,
  onUnfollow,
  busy,
  open,
}: {
  title: string;
  onUnfollow: () => void;
  busy: boolean;
  open?: ReactNode;
}) {
  return (
    <div className={c.update} data-testid="ask-following-row" data-kind="following">
      <span className={c.bullet} aria-hidden="true" />
      <span className={c.updateBody}>
        <span className={c.ow}>Following</span>
        <span className={c.updateTitle}>{title || "A session this conversation started"}</span>
      </span>
      <span className={c.ways}>
        {open}
        <button
          type="button"
          className={c.ghost}
          disabled={busy}
          onClick={onUnfollow}
          aria-label={`Unfollow ${title || "this session"}`}
          data-testid="ask-following-unfollow"
        >
          Unfollow
        </button>
      </span>
    </div>
  );
}

export function UpdateRow({
  u,
  onUnfollow,
  busy,
  open,
}: {
  u: AskUpdate;
  onUnfollow: () => void;
  busy: boolean;
  /** The session's way in (Open), when it can still be opened. */
  open?: ReactNode;
}) {
  const w = UPDATE_WORDS[u.kind];
  return (
    <div className={c.update} data-testid="ask-update" data-kind={u.kind} role="status">
      <span className={`${c.led} ${c[`led_${w.tone}`]}`} aria-hidden="true" />
      <span className={c.updateBody}>
        <span className={`${c.ow} ${c[`ow_${w.tone}`]}`}>{w.word}</span>
        <span className={c.updateTitle}>{u.title || "A session you follow"}</span>
        <span className={c.od}>{w.detail}</span>
      </span>
      <span className={c.ways}>
        {u.kind !== "ended" ? open : null}
        {u.following ? (
          <button
            type="button"
            className={c.ghost}
            disabled={busy}
            onClick={onUnfollow}
            aria-label={`Unfollow ${u.title || "this session"}`}
            data-testid="ask-update-unfollow"
          >
            Unfollow
          </button>
        ) : null}
      </span>
    </div>
  );
}
