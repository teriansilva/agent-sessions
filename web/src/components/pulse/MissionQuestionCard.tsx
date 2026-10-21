/** The bounded choice, rendered as buttons (#892, Phase 3b of #840).
 *
 * #840 asks for it in one sentence: *"When it is unsure, it asks — a bounded choice with concrete
 * options, never a guess dressed up as a decision."* This is the operator's half of that.
 *
 * **The button sends an INDEX.** `label` is text the model wrote and it carries no authority
 * whatsoever: the action lives in the server's closed set (`mission_questions.ACTIONS`) and is
 * looked up there, by position, inside the transaction that settles the question. So a label
 * reading `waive_objective; rm -rf /` is a string on a button and nothing else — which is what
 * makes "an option's label is never executed" a property rather than a hope.
 *
 * **Free text is an ANSWER, not an instruction.** It is recorded for the next supervisor pass to
 * read; it never becomes agent input. Typing at the agent is the composer's job, behind the write
 * fence, and giving an answer box a second route to a pty would undo that fence entirely.
 */
import { useState } from "react";

import { api, ApiError } from "../../lib/api";
import type { MissionQuestion } from "../../types/api";

import styles from "./mission.module.css";

export function MissionQuestionCard({
  missionId,
  question,
  onAnswered,
  onNote,
}: {
  missionId: string;
  question: MissionQuestion;
  /** Answered (or refused). The console re-reads either way: a 409 means this client's picture
   *  is the stale one, so the next render has to come from the server. */
  onAnswered: () => void;
  onNote: (msg: string) => void;
}) {
  const [busy, setBusy] = useState(false);
  const [text, setText] = useState("");
  /** Which settling option is awaiting its second tap. Index, not action: two options can name
   *  the same action and they are still two buttons. */
  const [confirming, setConfirming] = useState<number | null>(null);

  const send = async (answer: { optionIndex?: number; text?: string }) => {
    if (busy) return;
    setBusy(true);
    try {
      const out = await api.answerMissionQuestion(
        missionId,
        question.seq,
        answer,
      );
      setText("");
      // THE SERVER'S "IT DID NOT HAPPEN" IS AN ANSWER, NOT A SUCCESS (#900 review 7, finding 9).
      //
      // `close_mission` over an unmet gate, `waive_objective` over an objective already met or
      // since dropped — each is a valid 200 whose EFFECT was refused, and the store says so in
      // the operator's own terms ("not proposed — 2 required objective(s) are still unmet").
      // The card threw that away and cleared as though the choice had landed, which is the
      // "answered `waived` over an objective that was never waived" failure one layer up.
      if (out && out.applied_ok === false && out.applied) onNote(out.applied);
    } catch (err) {
      // The server's own words — "that question is no longer the open one", "that option does
      // not exist" — which is the whole reason this goes through `mutateJson` (#834).
      onNote(
        err instanceof ApiError && err.message
          ? err.message
          : "That answer did not go through.",
      );
      // The card is still here and still the operator's to act on, so the controls come back.
      setBusy(false);
      setConfirming(null);
    } finally {
      // BUSY IS NOT CLEARED HERE (#900 review 2, non-blocking follow-up). `onAnswered` triggers a
      // re-read, and clearing first briefly re-enables an answered question — indefinitely if
      // that read fails. The card is keyed on `question.seq`, so a settled question unmounts this
      // component entirely and there is nothing left to re-enable; a superseding one remounts.
      // The only path that must clear it is the one where the card SURVIVES, which is a refusal.
      onAnswered();
    }
  };

  return (
    <section
      className={styles.question}
      data-testid="mission-question"
      aria-label="The mission is asking"
    >
      <div className={styles.questionHead}>Needs your answer</div>
      {/* Model-authored text as TEXT. React escapes it; nothing here uses
          `dangerouslySetInnerHTML`. */}
      <p className={styles.questionText}>{question.question}</p>
      <div className={styles.questionOptions}>
        {question.options.map((o, i) => (
          // THE LABEL IS THE MODEL'S; THE CONSEQUENCE IS THE SERVER'S (#900 review 2, finding 1).
          //
          // "The label is never executed" was the property the closed set was built for, and it
          // is not the whole threat: the model writes the label AND picks the action, so a label
          // reading "Keep working; leave this required" over a hidden `waive_objective` gets a
          // human confirmation under false pretences. A button that lies still gets pressed.
          //
          // So every option shows what the SERVER will actually do, from its own table, and the
          // ones that change something beyond the timeline ask a second time — the operator is
          // confirming the consequence rather than the copy.
          <div key={i} className={styles.questionOption}>
            <button
              type="button"
              className={styles.questionBtn}
              disabled={busy}
              onClick={() =>
                o.settling && confirming !== i
                  ? setConfirming(i)
                  : void send({ optionIndex: i })
              }
              // THE ACCESSIBLE NAME SAYS IT IS ARMED (#900 review 8, finding 3). `aria-label`
              // OVERRIDES the button's text, so the visible CONFIRM was invisible to a screen
              // reader: the name stayed the model's label plus the consequence, and the second
              // tap — the one that exists to be an informed decision — announced nothing about
              // being a confirmation. A confirmation nobody can hear is not one.
              aria-label={
                confirming === i
                  ? `Confirm: ${o.label} — ${o.consequence ?? ""}`
                  : `${o.label} — ${o.consequence ?? ""}`
              }
              data-testid="mission-question-option"
            >
              {confirming === i ? "CONFIRM" : o.label}
            </button>
            {o.consequence ? (
              <div
                className={styles.questionConsequence}
                data-testid="mission-question-consequence"
              >
                {o.consequence}
              </div>
            ) : null}
          </div>
        ))}
      </div>
      <div className={styles.questionFree}>
        <input
          className={styles.questionInput}
          value={text}
          disabled={busy}
          placeholder="…or answer in your own words"
          aria-label="Answer in your own words"
          onChange={(e) => setText(e.target.value)}
          onKeyDown={(e) => {
            if (e.key === "Enter" && text.trim())
              void send({ text: text.trim() });
          }}
          data-testid="mission-question-text"
        />
        <button
          type="button"
          className={`${styles.questionBtn} ${styles.questionBtnPrimary}`}
          disabled={busy || !text.trim()}
          onClick={() => void send({ text: text.trim() })}
          data-testid="mission-question-send"
        >
          ANSWER
        </button>
      </div>
      <p className={styles.questionNote}>
        An answer is recorded for the follow-through to read. It is not typed at
        the agent — use the composer for that.
      </p>
    </section>
  );
}
