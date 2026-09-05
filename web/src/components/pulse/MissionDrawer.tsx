/** The rail as a drawer, below 1100px (#878).
 *
 * A breakpoint is not a modal. This declares `aria-modal`, so it owes the whole contract — focus
 * in, `#root` inert, Tab contained, focus back to the trigger on every close path — and it gets
 * it from `useModalDrawer`, the same hook the notification bell uses. One implementation, so the
 * two cannot drift apart.
 *
 * The panel and scrim are portalled to `<body>`, i.e. OUTSIDE `#root`, which is what lets the
 * background be made genuinely inert without also disabling the drawer.
 */
import { useRef, type ReactNode, type RefObject } from "react";
import { createPortal } from "react-dom";

import { useModalDrawer } from "./useModalDrawer";
import styles from "./missionDrawer.module.css";

export function MissionDrawer({
  open,
  onClose,
  triggerRef,
  children,
}: {
  open: boolean;
  onClose: () => void;
  triggerRef: RefObject<HTMLElement | null>;
  children: ReactNode;
}) {
  const panelRef = useRef<HTMLDivElement | null>(null);
  const closeRef = useRef<HTMLButtonElement | null>(null);

  useModalDrawer({
    active: open,
    panelRef,
    initialFocusRef: closeRef,
    triggerRef,
    onClose,
  });

  if (!open) return null;

  return createPortal(
    <>
      <div
        className={styles.scrim}
        onClick={onClose}
        data-testid="rail-scrim"
      />
      <div
        className={styles.panel}
        role="dialog"
        aria-modal="true"
        aria-label="Missions"
        ref={panelRef}
        data-testid="rail-drawer"
      >
        <div className={styles.head}>
          <span className={styles.headLabel}>Missions</span>
          {/* `#root` is inert while this is open, so the trigger cannot be reached — the drawer
              must carry its own close control, and it is also where focus lands. */}
          <button
            type="button"
            className={styles.close}
            onClick={onClose}
            ref={closeRef}
            data-testid="rail-drawer-close"
          >
            Close
          </button>
        </div>
        <div className={styles.body}>{children}</div>
      </div>
    </>,
    document.body,
  );
}
