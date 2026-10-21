import { X } from "lucide-react";
import { useEffect, useRef, useState } from "react";
import { createPortal } from "react-dom";
import dlg from "../HudDialog.module.css";
import { useFocusContainment } from "../pulse/useModalDrawer";

/** Rename a session, or set its tag, from the Overview map (#968).
 *
 *  The sidebar edits both inline in its row. The map cannot: its chips live inside React Flow's
 *  zoom transform, where an inline field at the 0.2 minimum zoom is a few pixels tall. So the map
 *  opens this dialog instead — the `RenameProjectModal` pattern on the shared `HudDialog` sheet,
 *  portalled to `<body>` so no transformed ancestor can contain its fixed backdrop.
 *
 *  The caller owns the write and its rules (an empty title is a no-op, an empty tag clears it);
 *  this only collects the value. Esc, the close button and the backdrop cancel; Enter and Save
 *  commit. */
export function SessionTextDialog({
  mode,
  sessionTitle,
  initial,
  onCancel,
  onSave,
  returnFocusTo,
}: {
  mode: "title" | "tag";
  /** The session being edited, shown read-only so the dialog names its target. */
  sessionTitle: string;
  initial: string;
  onCancel: () => void;
  onSave: (value: string) => void;
  returnFocusTo?: HTMLElement | null;
}) {
  const [draft, setDraft] = useState(initial);
  const inputRef = useRef<HTMLInputElement>(null);
  const dialogRef = useRef<HTMLDivElement>(null);
  const isTag = mode === "tag";
  // `aria-modal` is a promise about the keyboard (#968 review): Tab and Shift+Tab cycle inside the
  // dialog instead of walking out to the map behind it. The shared hook the other HUD dialogs use.
  useFocusContainment({ active: true, panelRef: dialogRef });

  // Focus in on open, back to the opener on close — the modal unmounts on close, so the cleanup
  // is the close.
  useEffect(() => {
    inputRef.current?.focus();
    inputRef.current?.select();
    return () => {
      if (returnFocusTo?.isConnected) returnFocusTo.focus();
    };
  }, [returnFocusTo]);

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
  const titleId = "session-text-dialog-title";

  return createPortal(
    <div className={dlg.backdrop} onMouseDown={onCancel}>
      <div
        ref={dialogRef}
        role="dialog"
        aria-modal="true"
        aria-labelledby={titleId}
        className={`${dlg.dialog} ${dlg.narrow}`}
        onMouseDown={(e) => e.stopPropagation()}
        data-session-text-dialog={mode}
      >
        <div className={dlg.head}>
          <span id={titleId} className={dlg.tag}>
            {isTag ? "Set session tag" : "Rename session"}
          </span>
          <button
            type="button"
            className={dlg.close}
            onClick={onCancel}
            aria-label={isTag ? "Close tag dialog" : "Close rename dialog"}
          >
            <X size={16} aria-hidden="true" />
          </button>
        </div>
        <p className={dlg.from}>
          <span className={dlg.fromLabel}>SESSION //</span>
          <b className={dlg.fromTitle}>{sessionTitle}</b>
        </p>
        <label className={dlg.field}>
          <span className={dlg.label}>{isTag ? "Tag //" : "Title //"}</span>
          <input
            ref={inputRef}
            className={dlg.input}
            value={draft}
            maxLength={isTag ? 32 : undefined}
            placeholder={isTag ? "Tag (text or emoji)" : undefined}
            onChange={(e) => setDraft(e.target.value)}
            onKeyDown={(e) => {
              if (e.key === "Enter") {
                e.preventDefault();
                save();
              }
            }}
            aria-label={isTag ? "Session tag" : "Session title"}
          />
        </label>
        <p className={dlg.help}>
          {isTag
            ? "Leave it blank to clear the tag. Up to 32 characters."
            : "The new title shows everywhere the session is listed."}
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
