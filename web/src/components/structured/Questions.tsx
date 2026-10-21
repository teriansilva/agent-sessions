/** An agent's question with fixed options (#1463). The options are the agent's own words, shown
 *  as plain text; the buttons come only from the structured field, never from the reply's prose. */
import { useId, useState } from "react";
import type { ReactNode } from "react";
import chat from "../chat/ChatPane.module.css";
import styles from "./Questions.module.css";

export interface ChoiceOption {
  label: string;
  description?: string;
}

export interface ChoiceQuestion {
  title: string;
  /** A short tag the agent gave the question (Claude's `header`). */
  header?: string;
  options: ChoiceOption[];
  /** Any number of options may be chosen. */
  multi?: boolean;
}

/** One answer per question: the chosen labels, or the operator's own words. */
export interface ChoiceAnswer {
  selected: string[];
  other?: string;
}

function answered(a: ChoiceAnswer | undefined): boolean {
  return !!a && (a.selected.length > 0 || !!a.other?.trim());
}

export function ChoiceQuestions({
  heading,
  questions,
  allowOther = false,
  disabled,
  submitLabel,
  onSubmit,
  actions,
  hint,
  status,
  testId = "structured-question",
}: {
  heading: string;
  questions: ChoiceQuestion[];
  /** Offer a free answer per question (it replaces that question's options). */
  allowOther?: boolean;
  disabled: boolean;
  submitLabel: string;
  onSubmit: (answers: ChoiceAnswer[]) => void;
  /** More actions after Send (Skip question, Stop the turn). */
  actions?: ReactNode;
  /** Explains what Send does, given the current answers. */
  hint?: (answers: ChoiceAnswer[]) => ReactNode;
  /** Sending / sent / uncertain: replaces nothing, never claims the agent acted. */
  status?: ReactNode;
  testId?: string;
}) {
  const id = useId();
  const [answers, setAnswers] = useState<ChoiceAnswer[]>(() => questions.map(() => ({ selected: [] })));
  const set = (i: number, next: ChoiceAnswer) =>
    setAnswers((all) => all.map((a, j) => (j === i ? next : a)));
  const missing = questions.filter((_, i) => !answered(answers[i]));
  const several = questions.length > 1;
  return (
    <section className={styles.block} aria-label={heading} data-testid={testId}>
      <div className={styles.head}>
        <span className={styles.mark} aria-hidden="true" />
        {heading}
        {several && (
          <span className={styles.count}>
            · {questions.length - missing.length} of {questions.length} answered
          </span>
        )}
      </div>
      {questions.map((q, i) => {
        const a = answers[i] ?? { selected: [] };
        const name = `${id}-q${i}`;
        return (
          <fieldset key={i} className={styles.question} disabled={disabled}>
            <legend className={styles.title}>
              {q.header && <span className={styles.tag}>{q.header}</span>}
              {q.title}
            </legend>
            {q.multi && <p className={styles.note}>Choose any number</p>}
            <div className={styles.options}>
              {q.options.map((o, j) => {
                const on = a.selected.includes(o.label) && !a.other;
                return (
                  <label
                    key={j}
                    className={`${styles.option} ${on ? styles.on : ""}`}
                    data-testid={`${testId}-option`}
                  >
                    <input
                      type={q.multi ? "checkbox" : "radio"}
                      name={name}
                      checked={on}
                      onChange={() =>
                        set(i, {
                          selected: q.multi
                            ? on
                              ? a.selected.filter((l) => l !== o.label)
                              : [...a.selected.filter((l) => q.options.some((x) => x.label === l)), o.label]
                            : [o.label],
                        })
                      }
                    />
                    <span>
                      <b>{o.label}</b>
                      {o.description && <small>{o.description}</small>}
                    </span>
                  </label>
                );
              })}
            </div>
            {allowOther && (
              <label className={styles.otherRow}>
                <span className={styles.note}>Or answer in your own words</span>
                <input
                  type="text"
                  className={styles.other}
                  maxLength={2000}
                  value={a.other ?? ""}
                  placeholder="Other answer…"
                  data-testid={`${testId}-other`}
                  onChange={(e) =>
                    set(i, e.target.value ? { selected: [], other: e.target.value } : { selected: [] })
                  }
                />
              </label>
            )}
          </fieldset>
        );
      })}
      {status}
      <div className={styles.actions}>
        <button
          type="button"
          className={chat.send}
          disabled={disabled || missing.length > 0}
          onClick={() => onSubmit(answers)}
          data-testid={`${testId}-send`}
        >
          {submitLabel}
        </button>
        {actions}
      </div>
      {several && missing.length > 0 && (
        <p className={styles.note}>Answer {missing.map((q) => `“${q.header || q.title}”`).join(", ")} to send.</p>
      )}
      {hint && !(several && missing.length > 0) && <p className={styles.note}>{hint(answers)}</p>}
    </section>
  );
}
