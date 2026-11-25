/** THE artifacts activation control (#1932 P2, board 4): one OFF / PRIVATE / PUBLIC radio group,
 *  used the same way for a session, a mission and a project. "Inherit" is clearing the scope's own
 *  setting (`mode: null`), so a scope with nothing set shows no segment chosen and says where its
 *  sessions' mode comes from instead. An agent without a BattleLab tool channel shows the control
 *  disabled with the reason (decision 11), never hidden. */
import { useRef, type KeyboardEvent, type ReactNode } from "react";
import { Globe, Lock, Power } from "lucide-react";

import type { ArtifactMode } from "../../types/artifacts";
import { MODES } from "./artifactModel";
import styles from "./artifacts.module.css";

const ICON = { off: Power, private: Lock, public: Globe } as const;

export function ActivationControl({
  value,
  onChange,
  label = "Artifacts tool",
  note,
  disabled = false,
  busy = false,
  source,
  clearLabel,
  testId,
}: {
  /** The scope's own mode, or null when nothing is set there (it inherits). */
  value: ArtifactMode | null;
  onChange: (mode: ArtifactMode | null) => void;
  label?: string;
  /** A short note right of the label ("default for new sessions"). */
  note?: string;
  /** No tool channel for this agent: the control is shown, disabled, without the mode list. */
  disabled?: boolean;
  busy?: boolean;
  /** The line under the control: where the mode comes from, and the order. */
  source?: ReactNode;
  /** Offer clearing the scope's own setting (it inherits again), with this label. */
  clearLabel?: string;
  testId?: string;
}) {
  const group = useRef<HTMLDivElement | null>(null);
  const off = disabled || busy;
  const onKey = (e: KeyboardEvent<HTMLDivElement>) => {
    const delta =
      e.key === "ArrowRight" || e.key === "ArrowDown"
        ? 1
        : e.key === "ArrowLeft" || e.key === "ArrowUp"
          ? -1
          : 0;
    if (!delta || off) return;
    e.preventDefault();
    const at = MODES.findIndex((m) => m.value === value);
    const next = MODES[(Math.max(0, at) + delta + MODES.length) % MODES.length];
    onChange(next.value);
    group.current
      ?.querySelector<HTMLElement>(`[data-value="${next.value}"]`)
      ?.focus();
  };
  return (
    <div className={styles.act} data-testid={testId}>
      <div className={styles.actLabel}>
        <span>{label}</span>
        {note && <em>{note}</em>}
      </div>
      <div
        ref={group}
        className={`nj-seg ${styles.actSeg}`}
        role="radiogroup"
        aria-label={label}
        aria-disabled={disabled || undefined}
        onKeyDown={onKey}
      >
        {MODES.map((m, i) => {
          const Icon = ICON[m.value];
          const on = !disabled && value === m.value;
          // One Tab stop: the chosen mode, or the first when nothing is chosen.
          const tabbable = on || (value === null && i === 0);
          return (
            <button
              key={m.value}
              type="button"
              role="radio"
              aria-checked={on}
              tabIndex={tabbable ? 0 : -1}
              disabled={off}
              data-value={m.value}
              onClick={() => onChange(m.value)}
            >
              <Icon size={14} aria-hidden="true" />
              {m.label}
            </button>
          );
        })}
      </div>
      {!disabled && (
        <ul className={styles.modes}>
          {MODES.map((m) => (
            <li
              key={m.value}
              className={value === m.value ? styles.modeOn : undefined}
            >
              <b>{m.label}</b>
              <span>{m.text}</span>
            </li>
          ))}
        </ul>
      )}
      {source && <p className={styles.src}>{source}</p>}
      {clearLabel && value !== null && !disabled && (
        <button
          type="button"
          className={styles.textBtn}
          disabled={busy}
          onClick={() => onChange(null)}
        >
          {clearLabel}
        </button>
      )}
    </div>
  );
}
