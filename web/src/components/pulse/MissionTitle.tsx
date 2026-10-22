import { useId, useRef } from "react";
import { ChevronDown } from "lucide-react";
import t from "./MissionTitle.module.css";
import styles from "./mission.module.css";

/** Keep a long title from displacing the composer. The native dialog makes the
 * complete title available by touch/keyboard and owns Escape and focus return. */
export function MissionTitle({ title }: { title: string }) {
  const dialog = useRef<HTMLDialogElement>(null);
  const label = useId();
  const titleLabel = useId();
  return (
    <>
      <button
        type="button"
        className={styles.titleButton}
        aria-haspopup="dialog"
        aria-label="Show full mission title"
        aria-describedby={titleLabel}
        onClick={() => dialog.current?.showModal()}
      >
        <span
          id={titleLabel}
          className={styles.missionTitle}
          data-testid="console-title"
        >
          {title}
        </span>
        <ChevronDown size={16} aria-hidden="true" />
      </button>
      <dialog ref={dialog} className={t.dialog} aria-labelledby={label}>
        <div className={t.head}>
          <h2 id={label} className={t.tag}>
            Mission title
          </h2>
        </div>
        <p className={t.body}>{title}</p>
        <div className={t.actions}>
          <button
            type="button"
            className={t.close}
            onClick={() => dialog.current?.close()}
          >
            Close
          </button>
        </div>
      </dialog>
    </>
  );
}
