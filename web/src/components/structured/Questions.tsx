/** An agent's question with fixed options (#1463). The options are the agent's own words, shown
 *  as plain text; the buttons come only from the structured field, never from the reply's prose. */
import { useContext, useId, useRef, useState } from "react";
import type { KeyboardEvent, ReactNode } from "react";
import chat from "../chat/ChatPane.module.css";
import { DockMinimise } from "./questionDockContext";
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
  // One question shows at a time (#2234): a tab per question, the others kept (hidden) so a
  // switch never loses a choice.
  const [active, setActive] = useState(0);
  const tabs = useRef<(HTMLButtonElement | null)[]>([]);
  const dock = useContext(DockMinimise);
  const minimised = !!dock?.minimised;
  const set = (i: number, next: ChoiceAnswer) =>
    setAnswers((all) => all.map((a, j) => (j === i ? next : a)));
  const missing = questions.filter((_, i) => !answered(answers[i]));
  const several = questions.length > 1;
  const current = Math.min(active, questions.length - 1);
  /** A single-choice pick moves on to the next question still unanswered, if any. */
  const advanceFrom = (i: number, next: ChoiceAnswer) => {
    if (!several) return;
    const after = answers.map((a, j) => (j === i ? next : a));
    for (let k = 1; k < questions.length; k++) {
      const j = (i + k) % questions.length;
      if (!answered(after[j])) {
        setActive(j);
        return;
      }
    }
  };
  const onTabKey = (e: KeyboardEvent<HTMLButtonElement>, i: number) => {
    const n = questions.length;
    const to =
      e.key === "ArrowRight" ? (i + 1) % n
      : e.key === "ArrowLeft" ? (i - 1 + n) % n
      : e.key === "Home" ? 0
      : e.key === "End" ? n - 1
      : null;
    if (to === null) return;
    e.preventDefault();
    setActive(to);
    tabs.current[to]?.focus();
  };
  return (
    <section
      className={`${styles.block} ${dock ? styles.inDock : ""}`}
      aria-label={heading}
      data-testid={testId}
      data-minimised={minimised || undefined}
    >
      <div className={styles.head}>
        <span className={styles.mark} aria-hidden="true" />
        <span className={styles.heading}>
          {heading}
          {several && (
            <span className={styles.count}>
              {" "}· {questions.length - missing.length} of {questions.length} answered
            </span>
          )}
        </span>
        {dock && (
          <button
            type="button"
            className={styles.toggle}
            aria-expanded={!minimised}
            aria-label={minimised ? "Show the question" : "Minimise the question"}
            title={minimised ? "Show the question" : "Minimise the question"}
            onClick={dock.toggle}
            data-testid={`${testId}-minimise`}
          >
            {minimised ? "▴" : "▾"}
          </button>
        )}
      </div>
      {!minimised && several && (
        <div className={styles.tabs} role="tablist" aria-label="Questions">
          {questions.map((q, i) => {
            const done = answered(answers[i]);
            return (
              <button
                key={i}
                ref={(el) => { tabs.current[i] = el; }}
                type="button"
                role="tab"
                id={`${id}-tab${i}`}
                aria-controls={`${id}-panel${i}`}
                aria-selected={i === current}
                tabIndex={i === current ? 0 : -1}
                className={`${styles.tab} ${i === current ? styles.tabOn : ""}`}
                onClick={() => setActive(i)}
                onKeyDown={(e) => onTabKey(e, i)}
                data-testid={`${testId}-tab`}
                data-answered={done || undefined}
              >
                <span className={done ? styles.tabDone : styles.tabOpen} aria-hidden="true">
                  {done ? "✓" : ""}
                </span>
                <span className={styles.tabLabel}>{q.header || `Question ${i + 1}`}</span>
                <span className={styles.srOnly}>{done ? ", answered" : ", not answered"}</span>
              </button>
            );
          })}
        </div>
      )}
      <div className={styles.panels} hidden={minimised}>
      {questions.map((q, i) => {
        const a = answers[i] ?? { selected: [] };
        const name = `${id}-q${i}`;
        return (
          <fieldset
            key={i}
            className={styles.question}
            disabled={disabled}
            hidden={several && i !== current}
            {...(several ? { role: "tabpanel", id: `${id}-panel${i}`, "aria-labelledby": `${id}-tab${i}` } : {})}
          >
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
                      onChange={() => {
                        const next = {
                          selected: q.multi
                            ? on
                              ? a.selected.filter((l) => l !== o.label)
                              : [...a.selected.filter((l) => q.options.some((x) => x.label === l)), o.label]
                            : [o.label],
                        };
                        set(i, next);
                        if (!q.multi) advanceFrom(i, next);
                      }}
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
      </div>
      {/* An answer in flight says so even while minimised. */}
      {minimised ? status : null}
      {!minimised && (
        <div className={styles.foot}>
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
            // Named as their tabs are, so the note stays one short line (#2234).
            <p className={styles.note}>
              Answer {questions
                .map((q, i) => (answered(answers[i]) ? null : `“${q.header || `Question ${i + 1}`}”`))
                .filter(Boolean)
                .join(", ")} to send.
            </p>
          )}
          {hint && !(several && missing.length > 0) && <p className={styles.note}>{hint(answers)}</p>}
        </div>
      )}
    </section>
  );
}
