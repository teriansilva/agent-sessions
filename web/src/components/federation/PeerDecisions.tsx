import { useState } from "react";
import {
  answerOptions,
  answeredBy,
  alreadyAnsweredBy,
  authorName,
  decisionCard,
  decisionRetryLabel,
  lockedAnswer,
  mayGive,
  requestHead,
  settledWord,
  type DecisionAttempt,
  type DecisionOutcome,
  type DecisionView,
  type RemoteRequest,
  type SeenRequest,
} from "../../lib/federationTogether";
import { Led } from "../hud/Meters";
import btn from "../ui/actionButton.module.css";
import card from "./PeerSuggestions.module.css";
import styles from "./Together.module.css";

/** What this pane did about ONE request: the attempt it made and what the owner answered. */
export interface AnswerState {
  attempt: DecisionAttempt;
  phase: "sending" | "done";
  outcome: DecisionOutcome | null;
}

const NO_PICKS: number[][] = [];
const TONE = { primary: btn.primary, ghost: btn.ghost, danger: btn.danger } as const;

/** A pending request on the RECEIVING side (#2062).
 *
 *  With `send` the card is active (*you may answer*) for the one-time kinds the owner lists.
 *  The option that widens the agent's authority is drawn **disabled**: it has no handler, the
 *  `disabled` attribute keeps the keyboard off it, and `mayGive` refuses it again before any
 *  request is built. Below `send` the card has no control at all.
 *
 *  **An answer that was not confirmed is still THE answer** (#2061). After `504 peer_lost`, a
 *  timeout, `429 busy` or any answer that proves nothing, the owner may have taken it. The card
 *  then offers one thing: the same answer again (*Retry: …*, the same key and offer, which the
 *  owner replays without a second effect). Every other choice and every option is disabled
 *  until the owner's answer or the snapshot settles the request: no second answer under a new
 *  key, and never "not accepted".
 *
 *  **What is picked belongs to the offer it was picked for.** The owner can replace a request
 *  under the same turn and request id; its `offer` is then another one, and option index 0 may
 *  now mean something else. The picks are held WITH the offer they were made under and read
 *  only while the request still carries that offer, and a retry lock from an attempt at another
 *  offer is not a lock here: after a replacement nothing is selected and *Send answer* is off
 *  until the operator picks again. (`RemotePane` also keys the card by the offer.)
 *
 *  Everything the other instance wrote here (the summary, a question, an option's label) is a
 *  text node. A question is answered by option INDEX; no label is ever sent. */
export function PeerDecisionCard({
  request,
  level,
  owner,
  state,
  onAnswer,
}: {
  request: RemoteRequest;
  level: string | null;
  owner: string;
  state: AnswerState | null;
  onAnswer: (choice: string, options: number[][] | null) => void;
}) {
  const view = decisionCard(request, level, owner);
  // The picks, and the offer they were made for. Another offer: no picks (not "the same indexes").
  const [held, setHeld] = useState<{ offer: string | null; picks: number[][] }>({ offer: request.offer, picks: [] });
  const picked = held.offer === request.offer ? held.picks : NO_PICKS;
  const questions = request.questions;
  const options = questions ? answerOptions(questions, picked) : null;
  const sending = state?.phase === "sending";
  const outcome = state?.outcome ?? null;
  // Someone else's answer counted, or this one was taken: the buttons are gone. Never retried.
  const over = outcome?.kind === "lost" || outcome?.kind === "accepted" || outcome?.kind === "no_grant";
  const active = view.role === "answer" && !over;
  // Not confirmed: only the attempt's own answer may be sent again (`lockedAnswer`).
  // An attempt at ANOTHER offer was about another request: it locks nothing here.
  const locked = state && state.attempt.offer === request.offer ? lockedAnswer(state) : null;
  const shownPicks = locked?.options ?? picked;
  const pick = (q: number, index: number, multi: boolean) =>
    setHeld((was) => {
      const all = was.offer === request.offer ? was.picks : NO_PICKS;
      const next = questions!.map((_, i) => [...(all[i] ?? [])]);
      next[q] = multi
        ? next[q].includes(index)
          ? next[q].filter((n) => n !== index)
          : [...next[q], index]
        : [index];
      return { offer: request.offer, picks: next };
    });

  return (
    <article
      className={`${card.card} ${active ? "" : card.quiet}`}
      data-testid="peer-decision"
      data-role={view.role}
      data-request={request.request_id}
      aria-label={view.head}
    >
      <header className={card.head}>
        <Led tone={active ? "att" : "idle"} />
        <span>{view.head}</span>
        <span className={card.word} data-testid="peer-decision-word">
          {active ? view.word : "waiting"}
        </span>
      </header>
      {questions ? (
        questions.map((q, qi) => (
          <div key={qi}>
            <p className={styles.qTitle} data-testid="peer-decision-question">
              {q.question}
            </p>
            {active && (
              <div className={styles.qOpts} role={q.multi ? "group" : "radiogroup"} aria-label={q.question}>
                {q.options.map((label, oi) => (
                  <button
                    key={oi}
                    type="button"
                    role={q.multi ? "checkbox" : "radio"}
                    aria-checked={(shownPicks[qi] ?? []).includes(oi)}
                    className={styles.qOpt}
                    disabled={sending || locked !== null}
                    data-testid="peer-decision-option"
                    onClick={() => pick(qi, oi, q.multi)}
                  >
                    {label}
                  </button>
                ))}
              </div>
            )}
          </div>
        ))
      ) : (
        request.summary && (
          <p className={styles.cmd} data-testid="peer-decision-summary">
            {request.summary}
          </p>
        )
      )}
      {active && (
        <div className={card.actions}>
          {view.buttons.map((b) => {
            const needsPick = b.choice === "answer" && options === null && !locked;
            const other = locked !== null && locked.choice !== b.choice;
            return (
              <button
                key={b.choice}
                type="button"
                className={TONE[b.tone]}
                // A disabled button takes no click and no key. `always` is never enabled here.
                disabled={!b.enabled || sending || needsPick || other}
                data-testid="peer-decision-choice"
                data-choice={b.choice}
                onClick={
                  b.enabled && mayGive(view, b.choice)
                    ? () =>
                        // A retry sends what the attempt was made with, not what is picked now.
                        locked
                          ? onAnswer(locked.choice, locked.options)
                          : onAnswer(b.choice, b.choice === "answer" ? options : null)
                    : undefined
                }
              >
                {sending && state?.attempt.answer.includes(`"${b.choice}"`)
                  ? "Sending…"
                  : locked?.choice === b.choice
                    ? decisionRetryLabel(b.label)
                    : b.label}
              </button>
            );
          })}
        </div>
      )}
      {active && view.line && (
        <p className={card.note} data-testid="peer-decision-line">
          {view.line}
        </p>
      )}
      {view.role === "watch" && !over && (
        <p className={`${card.note} ${card.only}`} data-testid="peer-decision-line">
          {view.line}
        </p>
      )}
      {outcome?.kind === "accepted" && (
        <p className={`${card.note} ${card.only}`} role="status" data-testid="peer-decision-outcome" data-outcome="accepted">
          Your answer was taken.
        </p>
      )}
      {outcome?.kind === "lost" && (
        <p className={`${card.note} ${card.only}`} role="status" data-testid="peer-decision-outcome" data-outcome="lost">
          {outcome.text}
        </p>
      )}
      {outcome?.kind === "no_grant" && (
        <p className={`${card.note} ${card.only}`} role="status" data-testid="peer-decision-outcome" data-outcome="no_grant">
          This share no longer allows that. Nothing was answered.
        </p>
      )}
      {(outcome?.kind === "refused" || outcome?.kind === "unconfirmed") && (
        <p
          className={card.note}
          role="alert"
          data-testid="peer-decision-outcome"
          data-outcome={outcome.kind === "refused" ? outcome.code : "unconfirmed"}
        >
          {outcome.text}
        </p>
      )}
    </article>
  );
}

/** A request that was answered, as EVERY view shows it: the answer's effect and the author the
 *  owner stored. `lost` is this pane's own answer that came second: said once, never retried. */
export function SettledDecisionCard({
  decision,
  seen,
  side,
  owner,
  lost,
}: {
  decision: DecisionView;
  seen: SeenRequest | undefined;
  side: "owner" | "peer";
  /** The owning instance's name (receiving side). */
  owner: string;
  lost: boolean;
}) {
  const question = seen ? seen.question !== null : decision.answer === "answer";
  const kind = seen?.kind ?? "command";
  const who = authorName(decision, side, owner);
  return (
    <article
      className={`${card.card} ${styles.done}`}
      data-testid="settled-decision"
      data-request={decision.request_id}
      aria-label={requestHead(kind, question, true)}
    >
      <header className={card.head}>
        <Led tone="up" />
        <span>{requestHead(kind, question, true)}</span>
        <span className={`${card.word} ${styles.doneWord}`}>answered</span>
      </header>
      {seen?.question && <p className={styles.qTitle}>{seen.question}</p>}
      {seen && seen.question === null && seen.summary && <p className={styles.cmd}>{seen.summary}</p>}
      <p className={styles.answer}>
        <span className={styles.state}>
          <Led tone="up" />
          <b data-testid="settled-answer">{settledWord(decision.answer, kind, question)}</b>
        </span>
        <span className={styles.by} data-testid="settled-by">
          {answeredBy(who)}
        </span>
      </p>
      {lost && (
        <p className={card.note} role="status" data-testid="settled-lost">
          {alreadyAnsweredBy(who)}
        </p>
      )}
    </article>
  );
}
