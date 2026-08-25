import { useEffect, useRef } from "react";
import { createPortal } from "react-dom";
import { X } from "lucide-react";
import { humanSize } from "./uploadPlan";
import type { CollisionChoice, QueueRow, UploadsApi } from "./useUploads";
import styles from "./filePanel.module.css";

/** Short, terminal label for one row. Every state names itself; none is inferred from a colour. */
function label(row: QueueRow): { text: string; cls: string } {
  switch (row.state.kind) {
    case "queued":
      return { text: "QUEUED", cls: styles.qMuted };
    case "sending":
      return { text: "SENDING", cls: styles.qBusy };
    case "done":
      return { text: "DONE", cls: styles.qOk };
    case "skipped":
      return { text: row.state.reason, cls: styles.qMuted };
    case "collision":
      return { text: row.state.reason, cls: styles.qWarn };
    case "failed":
      return { text: row.state.reason, cls: styles.qBad };
  }
}

/** The upload queue (#807).
 *
 *  A strip **inside** the panel rather than a modal, deliberately: twelve files landing should
 *  not hide the tree they are landing in. Every row carries its own terminal state, because a
 *  folder drop is a batch of independent outcomes and collapsing it to one success/failure would
 *  hide the nine that worked.
 */
export function UploadQueue({ uploads }: { uploads: UploadsApi }) {
  const { rows, busy, refusal, collision } = uploads;
  if (!rows.length && !refusal) return null;

  const done = rows.filter((r) => r.state.kind === "done").length;
  const failed = rows.filter(
    (r) => r.state.kind === "failed" || r.state.kind === "skipped",
  ).length;

  return (
    <>
      <div className={styles.queue} data-upload-queue="">
        <div className={styles.queueHead}>
          <span className="hud-tag">
            {refusal
              ? "UPLOAD // REFUSED"
              : busy
                ? `UPLOADING // ${done}/${rows.length}`
                : `UPLOAD // ${done} DONE${failed ? ` // ${failed} NOT LANDED` : ""}`}
          </span>
          {!busy && (
            <button
              type="button"
              className={styles.queueClose}
              aria-label="Dismiss the upload queue"
              title="Dismiss"
              onClick={uploads.dismiss}
            >
              <X size={12} aria-hidden="true" />
            </button>
          )}
        </div>
        {refusal && (
          <div className={styles.queueRefusal} role="alert" data-upload-refusal="">
            {refusal}
          </div>
        )}
        <div className={styles.queueRows}>
          {rows.map((r) => {
            const { text, cls } = label(r);
            return (
              <div key={r.id} className={styles.queueRow} data-upload-row={r.relpath}>
                {/* "Keep both" renames server-side, so the row shows the name that ACTUALLY
                    landed — reporting the requested one would tell the operator a file exists
                    that does not. */}
                <span
                  className={styles.queueName}
                  title={
                    r.state.kind === "done" && r.state.name !== r.relpath.split("/").pop()
                      ? `${r.relpath} → landed as ${r.state.name}`
                      : r.relpath
                  }
                >
                  {r.state.kind === "done" && r.state.name !== r.relpath.split("/").pop()
                    ? `${r.relpath} → ${r.state.name}`
                    : r.relpath}
                </span>
                {r.size > 0 && <span className={styles.queueSize}>{humanSize(r.size)}</span>}
                <span className={`${styles.queueState} ${cls}`} data-upload-state={r.state.kind}>
                  {text}
                </span>
              </div>
            );
          })}
        </div>
      </div>
      {collision && (
        <CollisionPrompt
          name={collision.row.relpath}
          remaining={collision.remaining}
          onChoose={uploads.resolveCollision}
        />
      )}
    </>
  );
}

/** A name that exists is a **choice**, never an overwrite the panel decided on (#807). */
function CollisionPrompt({
  name,
  remaining,
  onChoose,
}: {
  name: string;
  remaining: number;
  onChoose: (c: CollisionChoice, applyToRest: boolean) => void;
}) {
  const rest = useRef<HTMLInputElement>(null);
  const first = useRef<HTMLButtonElement>(null);
  const box = useRef<HTMLDivElement>(null);
  useEffect(() => {
    // Declaring `aria-modal` while letting focus walk out to the page behind is a false claim —
    // the file viewer already contains Tab for exactly this reason. Focus is also RESTORED to
    // whatever opened the prompt, so a keyboard user is not dumped on <body> afterwards.
    const returnTo = document.activeElement as HTMLElement | null;
    first.current?.focus();
    const onKey = (e: KeyboardEvent) => {
      if (e.key === "Escape") {
        e.preventDefault();
        e.stopPropagation();
        // Escape is the SAFE choice, not a cancel that leaves the batch wedged: skipping this
        // file lets the rest of the queue continue.
        onChoose("skip", false);
        return;
      }
      if (e.key !== "Tab") return;
      const root = box.current;
      if (!root) return;
      const focusable = Array.from(
        root.querySelectorAll<HTMLElement>(
          'button:not([disabled]), input:not([disabled]), [tabindex]:not([tabindex="-1"])',
        ),
      );
      if (!focusable.length) return;
      const firstEl = focusable[0];
      const lastEl = focusable[focusable.length - 1];
      const active = document.activeElement as HTMLElement | null;
      if (e.shiftKey && (active === firstEl || !root.contains(active))) {
        e.preventDefault();
        lastEl.focus();
      } else if (!e.shiftKey && (active === lastEl || !root.contains(active))) {
        e.preventDefault();
        firstEl.focus();
      }
    };
    document.addEventListener("keydown", onKey, true);
    return () => {
      document.removeEventListener("keydown", onKey, true);
      if (returnTo && document.contains(returnTo)) returnTo.focus();
    };
  }, [onChoose]);

  const pick = (c: CollisionChoice) => onChoose(c, Boolean(rest.current?.checked));

  return createPortal(
    <>
      <div className={styles.confirmScrim} />
      <div
        ref={box}
        className={styles.confirm}
        role="alertdialog"
        aria-modal="true"
        aria-label="A file with that name already exists"
        data-collision-prompt=""
      >
        <div className={styles.confirmHead}>
          <span className="hud-tag">Upload // Name taken</span>
        </div>
        <p className={styles.confirmBody}>
          <strong>{name}</strong> already exists here. Nothing has been overwritten.
        </p>
        {remaining > 0 && (
          <label className={styles.confirmCheck}>
            <input ref={rest} type="checkbox" data-apply-rest="" />
            Apply to the remaining {remaining} file{remaining === 1 ? "" : "s"}
          </label>
        )}
        <div className={styles.confirmRow}>
          <button
            ref={first}
            type="button"
            className={styles.ctrlBtn}
            data-collision="skip"
            onClick={() => pick("skip")}
          >
            Skip
          </button>
          <button
            type="button"
            className={styles.ctrlBtn}
            data-collision="keep_both"
            onClick={() => pick("keep_both")}
          >
            Keep both
          </button>
          <button
            type="button"
            className={`${styles.ctrlBtn} ${styles.ctrlBad}`}
            data-collision="replace"
            onClick={() => pick("replace")}
          >
            Replace
          </button>
        </div>
      </div>
    </>,
    document.body,
  );
}
