/** The automation wizard's small shared parts (#2221 P2b): the props every step takes, a choice of
 *  cards, a two-way segmented choice, the time zone field and a step's problems and warnings. */
import type { ReactNode } from "react";

import type { EditorForm } from "../../../lib/automations";
import styles from "../automations.module.css";
import w from "./wizard.module.css";

/** What every step edits: the form, and a stable id for each of its controls. */
export interface StepProps {
  form: EditorForm;
  set: <K extends keyof EditorForm>(k: K, v: EditorForm[K]) => void;
  /** A change that moves more than one field (a pairing rule, a preset). */
  update: (fn: (f: EditorForm) => EditorForm) => void;
  f: (name: string) => string;
}

export interface CardOption<T extends string> {
  value: T;
  title: string;
  sub: string;
}

/** A choice of cards: one pressed, each a ≥44 px button with a title and a line saying what it is. */
export function Cards<T extends string>({
  label,
  value,
  options,
  onChange,
  testId,
}: {
  label: string;
  value: T;
  options: CardOption<T>[];
  onChange: (v: T) => void;
  testId: string;
}) {
  return (
    <div className={w.cards} role="group" aria-label={label} data-testid={testId}>
      {options.map((o) => (
        <button
          key={o.value}
          type="button"
          className={w.card}
          aria-pressed={value === o.value}
          onClick={() => onChange(o.value)}
          data-value={o.value}
          data-testid={`${testId}-${o.value}`}
        >
          <span className={w.cardTitle}>{o.title}</span>
          <span className={w.cardSub}>{o.sub}</span>
        </button>
      ))}
    </div>
  );
}

export function Seg<T extends string>({
  label,
  value,
  options,
  onChange,
  testId,
}: {
  label: string;
  value: T;
  options: { value: T; label: string }[];
  onChange: (v: T) => void;
  testId?: string;
}) {
  return (
    <div className={w.segs} role="group" aria-label={label} data-testid={testId}>
      {options.map((o) => (
        <button
          key={o.value}
          type="button"
          className={w.seg}
          aria-pressed={value === o.value}
          onClick={() => onChange(o.value)}
          data-value={o.value}
        >
          {o.label}
        </button>
      ))}
    </div>
  );
}

/** What holds a step (Next waits for it) and what may not match (said, never holding it). */
export function StepIssues({
  problems,
  warnings = [],
  testId = "step-problems",
}: {
  problems: string[];
  warnings?: string[];
  testId?: string;
}) {
  if (!problems.length && !warnings.length) return null;
  return (
    <>
      {problems.length > 0 && (
        <ul className={w.issues} data-testid={testId}>
          {problems.map((p) => (
            <li key={p} className={w.problem}>
              {p}
            </li>
          ))}
        </ul>
      )}
      {warnings.length > 0 && (
        <ul className={w.issues} data-testid="step-warnings">
          {warnings.map((p) => (
            <li key={p} className={w.warning}>
              {p}
            </li>
          ))}
        </ul>
      )}
    </>
  );
}

export function TzField({
  id,
  zones,
  value,
  onChange,
}: {
  id: string;
  zones: string[] | null;
  value: string;
  onChange: (v: string) => void;
}) {
  return (
    <div className={styles.field}>
      <label className={styles.label} htmlFor={id}>
        Time zone
      </label>
      {zones ? (
        <select id={id} className={styles.control} value={value} onChange={(e) => onChange(e.target.value)}>
          {!zones.includes(value) && <option value={value}>{value}</option>}
          {zones.map((z) => (
            <option key={z} value={z}>
              {z}
            </option>
          ))}
        </select>
      ) : (
        <input id={id} className={styles.control} value={value} onChange={(e) => onChange(e.target.value)} />
      )}
    </div>
  );
}

/** A fold: advanced options, out of the way until asked for. */
export function Fold({
  summary,
  open,
  onToggle,
  children,
  testId,
}: {
  summary: string;
  open: boolean;
  onToggle: (open: boolean) => void;
  children: ReactNode;
  testId: string;
}) {
  return (
    <details
      className={w.fold}
      open={open}
      onToggle={(e) => onToggle((e.currentTarget as HTMLDetailsElement).open)}
      data-testid={testId}
    >
      <summary className={w.foldHead}>{summary}</summary>
      <div className={w.foldBody}>{children}</div>
    </details>
  );
}
