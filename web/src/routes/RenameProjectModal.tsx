import { X } from "lucide-react";
import { useEffect, useRef, useState } from "react";
import { createPortal } from "react-dom";
import dlg from "../components/HudDialog.module.css";
import { shortCwd } from "../lib/format";

/** Rename-a-project modal (#174). Replaces the inline rename input in Settings — a
 *  modal makes the affordance unmistakable and the path read-only context disambiguates
 *  duplicate display names.
 *
 *  Accessibility: `role="dialog"`, `aria-modal`, labelled by the head tag; focus moves to
 *  the input on open and returns to the trigger on close; Esc, the close button and the
 *  backdrop cancel (no save); Enter / clicking Save commits. Empty input clears the
 *  custom name (handled by the parent on save).
 *
 *  Drawn with the Hand off design (#597) and portalled to `<body>` (#948). It carried a copy of
 *  the sheet that named undefined tokens and painted purple, and the Settings pane it opens
 *  from is a containing block for its fixed backdrop. */
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

  return createPortal(
    <div className={dlg.backdrop} onMouseDown={onCancel}>
      <div
        role="dialog"
        aria-modal="true"
        aria-labelledby={titleId}
        className={`${dlg.dialog} ${dlg.narrow}`}
        // Stop clicks inside the dialog from bubbling to the backdrop → no accidental cancel.
        onMouseDown={(e) => e.stopPropagation()}
      >
        <div className={dlg.head}>
          <span id={titleId} className={dlg.tag}>
            Rename project
          </span>
          <button
            type="button"
            className={dlg.close}
            onClick={onCancel}
            aria-label="Close rename dialog"
          >
            <X size={16} aria-hidden="true" />
          </button>
        </div>
        <p className={dlg.from}>
          <span className={dlg.fromLabel}>PATH //</span>
          <b className={dlg.fromTitle}>{shortCwd(cwd)}</b>
        </p>
        <label className={dlg.field}>
          <span className={dlg.label}>Display name //</span>
          <input
            ref={inputRef}
            className={dlg.input}
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
        <p className={dlg.help}>
          Leave the name blank to clear it. The path stays the same — filtering
          still uses the full cwd.
        </p>
        <div className={dlg.actions}>
          <button type="button" className={dlg.cancel} onClick={onCancel}>
            Cancel
          </button>
          <button type="button" className={dlg.go} onClick={save}>
            Save
          </button>
        </div>
      </div>
    </div>,
    document.body,
  );
}
