/** Small, shared pieces of the Automations pages (#1201): a state's LED + word, an outcome's word,
 *  the 14-day strip and the kill-switch notice. Colour is only ever the state's, and always beside
 *  its word (docs/design.md §3). */
import type { ReactNode } from "react";

import { dayWord, outcomeWord, stateWord, type Tone } from "../../lib/automations";
import type { StripDay } from "../../types/automations";
import styles from "./automations.module.css";

const LED: Record<Tone, string> = {
  up: styles.up,
  degraded: styles.degraded,
  down: styles.down,
  idle: styles.idle,
};
const WORD: Record<Tone, string> = {
  up: styles.wordUp,
  degraded: styles.wordDegraded,
  down: styles.wordDown,
  idle: styles.wordIdle,
};
const DAY: Record<string, string> = {
  ok: styles.dayOk,
  failed: styles.dayFailed,
  skipped: styles.daySkipped,
  pending: styles.dayPending,
};

export function ToneText({
  tone,
  className,
  children,
}: {
  tone: Tone;
  className?: string;
  children: ReactNode;
}) {
  return <span className={`${WORD[tone]} ${className ?? ""}`}>{children}</span>;
}

export function Led({ tone }: { tone: Tone }) {
  return <span className={`${styles.led} ${LED[tone]}`} aria-hidden="true" />;
}

export function StateWord({ state }: { state: string }) {
  const { word, tone } = stateWord(state);
  return (
    <span className={`${styles.stateWord} ${WORD[tone]}`} data-testid="automation-state">
      <Led tone={tone} />
      {word}
    </span>
  );
}

export function OutcomeWord({ outcome, state }: { outcome: string; state?: string }) {
  const { word, tone } = outcomeWord(outcome, state);
  return (
    <span className={WORD[tone]} data-testid="run-outcome">
      {word}
    </span>
  );
}

export function Strip({ days }: { days: StripDay[] }) {
  return (
    <span className={styles.strip} role="img" aria-label={days.map(dayWord).join("; ")}>
      {days.map((d) => (
        <i
          key={d.date}
          className={`${styles.day} ${d.worst ? DAY[d.worst] : ""}`}
          data-worst={d.worst ?? "none"}
        />
      ))}
    </span>
  );
}

/** `loop.enabled == false`: the server's kill switch. Amber, not red — nothing is failing; the
 *  operator (or their install) switched it off. */
export function KillSwitchNotice() {
  return (
    <div
      className={`${styles.notice} ${styles.noticeWarn}`}
      role="status"
      data-testid="automations-kill-switch"
    >
      <strong>Automations are switched off on this server</strong>
      <span>
        AGENT_SESSIONS_AUTOMATION_LOOP=0 is set. Nothing is scheduled and Run now is refused. Remove
        it and restart BattleLab to turn them back on.
      </span>
    </div>
  );
}

/** A change was acknowledged, but a run already past its last check may still complete. Amber
 *  beside the confirmation — the change took effect; this is a thing to know, not a failure. */
export function InFlightNote({ text }: { text: string }) {
  return (
    <span className={styles.wordDegraded} data-testid="in-flight-note">
      {text}
    </span>
  );
}
