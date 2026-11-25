/** The pinned question dock (#2234). An open question the agent asked renders in a dock between
 *  the log and the composer, so it never scrolls away and never fills the log. The transcript keeps
 *  a one-line marker where the question was asked. The question keeps living in its turn's React
 *  tree (a portal moves only its DOM), so its answer state and send logic are unchanged. */
import { useContext, useRef, useState } from "react";
import type { ReactNode } from "react";
import { createPortal } from "react-dom";
import { DockMinimise, QuestionDockTarget } from "./questionDockContext";
import styles from "./Questions.module.css";

/** The dock itself: an empty box the pane places above the composer. Hidden while empty (CSS). */
export function QuestionDock({ dockRef }: { dockRef: (el: HTMLDivElement | null) => void }) {
  return <div ref={dockRef} className={styles.dock} data-testid="structured-question-dock" />;
}

/** Renders `children` (a question block) in the pane's dock, and `label` as the inline marker.
 *  Without a dock (no pane around it, or before the dock mounts) the question renders inline,
 *  exactly as before. Keyed by its question, so a new question starts expanded. */
export function DockedQuestion({ label, children }: { label: string; children: ReactNode }) {
  const target = useContext(QuestionDockTarget);
  const [minimised, setMinimised] = useState(false);
  const box = useRef<HTMLDivElement>(null);
  if (!target) return <>{children}</>;
  const show = () => {
    setMinimised(false);
    // After the expand paints: focus the active tab, else the first control in the block.
    requestAnimationFrame(() => {
      const root = box.current;
      root?.scrollIntoView?.({ block: "nearest" });
      (root?.querySelector<HTMLElement>('[role="tab"][aria-selected="true"]')
        ?? root?.querySelector<HTMLElement>("input:not(:disabled), button:not(:disabled)"))
        ?.focus({ preventScroll: true });
    });
  };
  return (
    <>
      <button type="button" className={styles.marker} onClick={show} data-testid="structured-question-marker">
        <span className={styles.mark} aria-hidden="true" />
        {label}
        <span className={styles.count}>· answer below ↓</span>
      </button>
      {createPortal(
        <div ref={box} className={styles.docked}>
          <DockMinimise.Provider value={{ minimised, toggle: () => setMinimised((m) => !m) }}>
            {children}
          </DockMinimise.Provider>
        </div>,
        target,
      )}
    </>
  );
}
