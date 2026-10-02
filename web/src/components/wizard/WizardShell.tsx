import { useEffect, useRef, type ReactNode } from "react";
import { useBlocker } from "react-router-dom";
import { ConfirmDialog } from "../templates/ConfirmDialog";
import styles from "./WizardShell.module.css";

export interface WizardStep {
  id: string;
  label: string;
}

/** THE wizard shell (#1187): one for the app, so the New project wizard, #853 P7's agent install
 *  and #1096's playbook deploy do not each grow their own. It knows steps, not projects:
 *
 *  - a step RAIL on desktop and a progress bar on a phone (≤800 px), both from `steps`;
 *  - the step's heading, which takes focus on every step change so a keyboard or screen-reader
 *    operator lands on the new step rather than on a Next button that may no longer exist;
 *  - Back / Next, with Next disabled while the step is invalid;
 *  - a leave confirmation while `leaveGuard` holds — the router blocker for in-app navigation
 *    (Back, a nav link, the wizard's own Cancel) and `beforeunload` for a reload or a closed tab.
 *
 *  Needs a DATA router (`useBlocker`), which the app is (#905 P2). */
export function WizardShell({
  kicker,
  steps,
  current,
  canJump,
  onJump,
  heading,
  children,
  onBack,
  onNext,
  nextLabel = "Next",
  nextDisabled = false,
  secondary,
  leaveGuard,
  leaveTitle,
  leaveMessage = "What you entered here has not been saved. Leave and lose it, or stay and finish.",
  leaveConfirmLabel = "Discard and leave",
}: {
  /** The mono callsign above everything, e.g. `NEW PROJECT`. */
  kicker: string;
  steps: readonly WizardStep[];
  current: number;
  /** Whether the rail may jump to step `i`. Only earlier or already-valid steps should say yes. */
  canJump?: (i: number) => boolean;
  onJump?: (i: number) => void;
  heading: string;
  children: ReactNode;
  /** Omit to hide Back (the first step, and after a commit that cannot be undone). */
  onBack?: () => void;
  /** Omit to hide Next (a step whose own content carries the actions). */
  onNext?: () => void;
  nextLabel?: string;
  nextDisabled?: boolean;
  /** Left of the nav buttons — typically Cancel. */
  secondary?: ReactNode;
  leaveGuard: boolean;
  leaveTitle: string;
  leaveMessage?: string;
  leaveConfirmLabel?: string;
}) {
  const headingRef = useRef<HTMLHeadingElement>(null);
  const arrived = useRef(false);
  useEffect(() => {
    // Not on arrival: the page itself just loaded, and the first step's own field is where the
    // operator starts. Every CHANGE of step moves focus to its heading.
    if (!arrived.current) {
      arrived.current = true;
      return;
    }
    headingRef.current?.focus();
  }, [current]);

  const blocker = useBlocker(
    ({ currentLocation, nextLocation }) =>
      leaveGuard && currentLocation.pathname !== nextLocation.pathname,
  );
  useEffect(() => {
    if (!leaveGuard) return;
    const onBefore = (e: BeforeUnloadEvent) => {
      e.preventDefault();
      // Chromium before 119 prompts only when returnValue is set.
      e.returnValue = "";
    };
    window.addEventListener("beforeunload", onBefore);
    return () => window.removeEventListener("beforeunload", onBefore);
  }, [leaveGuard]);

  const step = steps[current];
  const pct = Math.round(((current + 1) / steps.length) * 100);

  return (
    <div className={styles.page}>
      <div className={styles.frame}>
        <div className={styles.kicker}>{kicker}</div>

        <nav className={styles.rail} aria-label={`${kicker} steps`}>
          <ol>
            {steps.map((s, i) => {
              const state = i < current ? "done" : i === current ? "current" : "todo";
              const label = (
                <>
                  <span className={styles.num} aria-hidden="true">
                    {String(i + 1).padStart(2, "0")}
                  </span>
                  <span className={styles.stepLabel}>{s.label}</span>
                </>
              );
              const jumpable = i !== current && !!onJump && !!canJump?.(i);
              return (
                <li
                  key={s.id}
                  className={styles[state]}
                  aria-current={i === current ? "step" : undefined}
                >
                  {jumpable ? (
                    <button
                      type="button"
                      className={styles.railItem}
                      onClick={() => onJump?.(i)}
                    >
                      {label}
                    </button>
                  ) : (
                    <span className={styles.railItem}>{label}</span>
                  )}
                </li>
              );
            })}
          </ol>
        </nav>

        <div className={styles.progress} data-testid="wizard-progress">
          <div className={styles.progressText}>
            Step {current + 1} / {steps.length}{" // "}{step?.label}
          </div>
          <div className={styles.bar} aria-hidden="true">
            <div className={styles.fill} style={{ width: `${pct}%` }} />
          </div>
        </div>

        <section className={styles.body} aria-labelledby="wizard-step-heading">
          <h1
            id="wizard-step-heading"
            ref={headingRef}
            tabIndex={-1}
            className={styles.heading}
          >
            {heading}
          </h1>
          {children}
        </section>

        {(onBack || onNext || secondary) && (
          <div className={styles.nav}>
            <div className={styles.secondary}>{secondary}</div>
            {onBack && (
              <button type="button" className={styles.back} onClick={onBack}>
                Back
              </button>
            )}
            {onNext && (
              <button
                type="button"
                className={styles.next}
                onClick={onNext}
                disabled={nextDisabled}
              >
                {nextLabel}
              </button>
            )}
          </div>
        )}
      </div>

      {blocker.state === "blocked" && (
        <ConfirmDialog
          tag="Unsaved changes"
          title={leaveTitle}
          cancelLabel="Keep editing"
          confirmLabel={leaveConfirmLabel}
          danger
          onCancel={() => blocker.reset()}
          onConfirm={() => blocker.proceed()}
        >
          <p>{leaveMessage}</p>
        </ConfirmDialog>
      )}
    </div>
  );
}
