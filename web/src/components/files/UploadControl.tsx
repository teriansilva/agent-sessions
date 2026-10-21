import { useEffect, useRef, useState } from "react";
import { createPortal } from "react-dom";
import { FolderUp, Upload } from "lucide-react";
import { type PlannedFile, supportsFolderPicker } from "./uploadPlan";
import styles from "./filePanel.module.css";

/** The panel-head Upload control (#807): *Files…* and *Folder…*.
 *
 *  The picker is a real `<input type="file">` behind a labelled button, not a drag-only feature —
 *  a drop zone with no keyboard-reachable equivalent is simply inaccessible.
 *
 *  **Where the platform can't, it says so.** iOS Safari has no `webkitdirectory`, so *Folder…* is
 *  disabled *with the reason* rather than offered as a button that quietly produces one flattened
 *  file. A control that lies about what it did is worse than a control that isn't there.
 */
export function UploadControl({
  disabled,
  onFiles,
}: {
  disabled?: boolean;
  onFiles: (files: PlannedFile[]) => void;
}) {
  const [open, setOpen] = useState(false);
  const [pos, setPos] = useState<{ top: number; right: number } | null>(null);
  const btn = useRef<HTMLButtonElement>(null);
  const filesInput = useRef<HTMLInputElement>(null);
  const folderInput = useRef<HTMLInputElement>(null);
  // A lazy state initializer, not an effect: the answer is fixed for a given browser, so probing
  // it once at first render is honest — whereas `setState` inside an effect would render the
  // control as available for one frame and then correct itself, which is a flicker on exactly
  // the platform (iOS Safari) the message is meant for.
  const [canFolder] = useState(() => supportsFolderPicker());

  useEffect(() => {
    if (!open) return;
    const onKey = (e: KeyboardEvent) => {
      if (e.key !== "Escape") return;
      e.preventDefault();
      e.stopPropagation();
      setOpen(false);
      btn.current?.focus();
    };
    document.addEventListener("keydown", onKey, true);
    return () => document.removeEventListener("keydown", onKey, true);
  }, [open]);

  const take = (list: FileList | null) => {
    if (!list) return;
    const out: PlannedFile[] = [];
    for (const file of Array.from(list)) {
      // `webkitRelativePath` is what preserves a picked FOLDER's structure; a flat pick has none
      // and falls back to the bare name.
      const rel =
        (file as File & { webkitRelativePath?: string }).webkitRelativePath || file.name;
      out.push({ file, relpath: rel, size: file.size });
    }
    if (out.length) onFiles(out);
  };

  return (
    <>
      <button
        ref={btn}
        type="button"
        className={styles.iconBtn}
        data-upload-trigger=""
        aria-haspopup="menu"
        aria-expanded={open}
        aria-label="Upload into this folder"
        title="Upload into this folder"
        disabled={disabled}
        onClick={(e) => {
          const r = e.currentTarget.getBoundingClientRect();
          setPos({ top: r.bottom + 2, right: Math.max(8, window.innerWidth - r.right) });
          setOpen((o) => !o);
        }}
      >
        <Upload size={13} aria-hidden="true" />
      </button>
      {/* Real inputs, kept out of the layout but NOT `display:none` — a hidden-but-present input
          is what makes the control keyboard- and screen-reader-reachable. */}
      <input
        ref={filesInput}
        type="file"
        multiple
        className={styles.hiddenInput}
        data-upload-files=""
        tabIndex={-1}
        onChange={(e) => {
          take(e.target.files);
          e.target.value = "";
        }}
      />
      <input
        ref={folderInput}
        type="file"
        multiple
        className={styles.hiddenInput}
        data-upload-folder=""
        tabIndex={-1}
        // Non-standard, and the reason `canFolder` exists. React does not type it.
        {...({ webkitdirectory: "" } as Record<string, string>)}
        onChange={(e) => {
          take(e.target.files);
          e.target.value = "";
        }}
      />
      {open &&
        pos &&
        createPortal(
          <>
            <button
              type="button"
              className={styles.menuScrim}
              aria-label="Close the upload menu"
              onClick={() => setOpen(false)}
            />
            <div
              className={`${styles.headMenu} ${styles.aboveSheet}`}
              role="menu"
              aria-label="Upload"
              data-upload-menu=""
              style={{ top: pos.top, right: pos.right }}
            >
              <button
                type="button"
                role="menuitem"
                className={styles.headMenuItem}
                data-upload-pick="files"
                onClick={() => {
                  setOpen(false);
                  filesInput.current?.click();
                }}
              >
                <Upload size={12} aria-hidden="true" />
                Files…
              </button>
              <button
                type="button"
                role="menuitem"
                className={styles.headMenuItem}
                data-upload-pick="folder"
                disabled={!canFolder}
                title={
                  canFolder
                    ? "Upload a folder, structure preserved"
                    : "This browser cannot pick a folder (no webkitdirectory) — pick the files instead"
                }
                onClick={() => {
                  setOpen(false);
                  folderInput.current?.click();
                }}
              >
                <FolderUp size={12} aria-hidden="true" />
                Folder…
              </button>
              {!canFolder && (
                <div className={styles.menuEmpty} data-folder-unsupported="">
                  This browser cannot pick a folder. Pick the files instead — they will still land
                  here.
                </div>
              )}
            </div>
          </>,
          document.body,
        )}
    </>
  );
}
