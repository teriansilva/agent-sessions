import { useEffect, useRef, useState } from "react";
import { shortCwd } from "../lib/format";
import styles from "./RenameProjectModal.module.css";

/** Rename-a-project modal (#174). Replaces the inline rename input in Settings — a
 *  modal makes the affordance unmistakable and the path read-only context disambiguates
 *  duplicate display names.
 *
 *  Accessibility: `role="dialog"`, `aria-modal`, labelled by the title; focus moves to
 *  the input on open and returns to the trigger on close; Esc cancels (no save); clicking
 *  the backdrop cancels (no save); Enter / clicking Save commits. Empty input clears the
 *  custom name (handled by the parent on save). */
export function RenameProjectModal({
  cwd,
  initialName,
  onCancel,
  onSave,
  returnFocusTo,
}: {
  cwd: string;
  /** The current persisted custom name (or "" if none). Seeds the input. */
  initialName: string;
  onCancel: () => void;
  onSave: (name: string) => void;
  /** The element that opened the modal — focus returns here on close. */
  returnFocusTo?: HTMLElement | null;
}) {
  const [draft, setDraft] = useState(initialName);
  const inputRef = useRef<HTMLInputElement>(null);

  // Move focus into the input on open + restore it to the trigger on close. The empty
  // dep array intentionally only runs at mount; the modal is unmounted on close so this
  // pairs cleanly with the cleanup.
  useEffect(() => {
    inputRef.current?.focus();
    inputRef.current?.select();
    return () => {
      returnFocusTo?.focus?.();
    };
  }, [returnFocusTo]);

  // Global Escape → cancel. Document-level so it catches even when focus moved out of
  // the field for any reason.
  useEffect(() => {
    const onKey = (e: KeyboardEvent) => {
      if (e.key === "Escape") {
        e.preventDefault();
        onCancel();
      }
    };
    document.addEventListener("keydown", onKey);
    return () => document.removeEventListener("keydown", onKey);
  }, [onCancel]);

  const save = () => onSave(draft.trim());
  const titleId = "rename-project-title";

  return (
    <div className={styles.backdrop} onMouseDown={onCancel}>
      <div
        role="dialog"
        aria-modal="true"
        aria-labelledby={titleId}
        className={styles.dialog}
        // Stop clicks inside the dialog from bubbling to the backdrop → no accidental cancel.
        onMouseDown={(e) => e.stopPropagation()}
      >
        <h3 id={titleId} className={styles.title}>
          Rename project
        </h3>
        <p className={styles.path}>{shortCwd(cwd)}</p>
        <label className={styles.field}>
          <span className={styles.label}>Display name</span>
          <input
            ref={inputRef}
            className={styles.input}
            value={draft}
            onChange={(e) => setDraft(e.target.value)}
            placeholder={shortCwd(cwd)}
            onKeyDown={(e) => {
              if (e.key === "Enter") {
                e.preventDefault();
                save();
              }
            }}
            aria-label={`Custom name for ${cwd}`}
          />
        </label>
        <p className={styles.help}>
          Leave the name blank to clear it. The path stays the same — filtering still uses
          the full cwd.
        </p>
        <div className={styles.actions}>
          <button type="button" className={styles.cancel} onClick={onCancel}>
            Cancel
          </button>
          <button type="button" className={styles.save} onClick={save}>
            Save
          </button>
        </div>
      </div>
    </div>
  );
}
