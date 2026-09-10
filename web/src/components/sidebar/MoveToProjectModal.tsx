import { Check, FolderTree } from "lucide-react";
import { useEffect, useRef, useState } from "react";
import { api } from "../../lib/api";
import { shortCwd } from "../../lib/format";
import type { ProjectEntity, ProjectRef, Session } from "../../types/api";
import styles from "./MoveToProjectModal.module.css";
import { useFocusContainment } from "../pulse/useModalDrawer";

/** Pick a project to reassign a session to — the keyboard-accessible equivalent of the map's
 *  drag-to-reassign (#424 Phase 5). Opened from the sidebar row's ⋯ menu.
 *
 *  Accessibility: `role="dialog"`, `aria-modal`, labelled by the title; focus moves to the
 *  first option on open and returns to the trigger on close; Esc cancels; clicking the backdrop
 *  cancels; each option is a real `<button>` (Tab to move, Enter/Space to choose). The current
 *  assignment is marked and choosing it is a harmless no-op (handled by the parent). */
export function MoveToProjectModal({
  session,
  onCancel,
  onMove,
  returnFocusTo,
}: {
  session: Session;
  onCancel: () => void;
  /** The chosen target entity, or `null` to unassign (folder fallback). */
  onMove: (ref: ProjectRef | null) => void;
  /** The element that opened the modal — focus returns here on close. */
  returnFocusTo?: HTMLElement | null;
}) {
  const [projects, setProjects] = useState<ProjectEntity[] | null>(null);
  const [error, setError] = useState<string | null>(null);
  const firstRef = useRef<HTMLButtonElement>(null);
  const dialogRef = useRef<HTMLDivElement | null>(null);
  const cancelRef = useRef<HTMLButtonElement | null>(null);
  const currentId =
    session.project.kind === "project" ? session.project.id : null;

  // Load the assignable entities once on open. Focus moves to the first option after they land.
  useEffect(() => {
    let alive = true;
    api
      .projectEntities()
      .then((r) => alive && setProjects(r.projects.filter((p) => !p.archived)))
      .catch(() => alive && setError("Couldn’t load projects."));
    return () => {
      alive = false;
    };
  }, []);
  /** FOCUS ENTERS ON MOUNT, not on a successful fetch (#940 review 4).
   *
   *  This used to be the whole focus story: `if (projects) firstRef.current?.focus()`. Until the
   *  list landed — and forever, if it failed — focus stayed on the Session-actions trigger
   *  OUTSIDE this dialog. That is a broken modal on its own terms, and it also broke the drawer
   *  beneath it: the parent decided whether to stand down by asking where focus was, so one
   *  Escape over the spinner or the error closed the DRAWER and left this mounted inside a panel
   *  that had just been parked and inert.
   *
   *  The drawer no longer asks focus that question (`useModalDrawer`'s surface stack), so this is
   *  not load-bearing for the coordination any more. It is still right: the dialog takes focus the
   *  moment it opens, and hands it to the first option once there is one to hand it to.
   *
   *  **CANCEL, NOT THE CONTAINER** (#940 review 5). The first version focused the dialog element
   *  itself, and a `tabIndex={-1}` container is not a tab stop — so it was not the first entry in
   *  the containment cycle either, and the very first Shift+Tab walked out of the dialog into the
   *  sidebar's Archived tab. Cancel is present in EVERY state of this dialog, including the two
   *  that have no other control, which makes it the honest place for focus to start. (The
   *  containment hook also learned to handle a non-tabbable initial target, so a future surface
   *  that does focus its container is not broken in the same way — but a real control is better
   *  than relying on that.) */
  useEffect(() => {
    cancelRef.current?.focus();
  }, []);
  useEffect(() => {
    if (projects) firstRef.current?.focus();
  }, [projects]);
  // Restore focus to the trigger on unmount.
  /** TAB STAYS IN HERE (#940 review 2). This surface declares `aria-modal`, moves focus in and
   *  restores it on close — the one part of that promise it never kept was containment, which was
   *  survivable until the sidebar drawer beneath it became modal too and the two started fighting
   *  over the same key. `useModalDrawer` stands the drawer down while this holds focus; this is
   *  the other half of that coordination. */
  useFocusContainment({ active: true, panelRef: dialogRef });

  useEffect(() => () => returnFocusTo?.focus?.(), [returnFocusTo]);

  // Global Escape → cancel (catches even if focus drifts).
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

  const titleId = "move-to-project-title";

  return (
    <div className={styles.backdrop} onMouseDown={onCancel}>
      <div
        ref={dialogRef}
        // Focusable as a target, never as a tab stop — the loading and error states have no
        // control of their own to land on, and focus has to be somewhere inside the dialog for it
        // to be a dialog at all.
        tabIndex={-1}
        role="dialog"
        aria-modal="true"
        aria-labelledby={titleId}
        className={styles.dialog}
        onMouseDown={(e) => e.stopPropagation()}
      >
        <h3 id={titleId} className={styles.title}>
          Move to project
        </h3>
        <p className={styles.path}>{session.title || shortCwd(session.cwd)}</p>
        {error ? (
          <p className={styles.empty}>{error}</p>
        ) : projects === null ? (
          <p className={styles.empty}>Loading projects…</p>
        ) : (
          <ul className={styles.list}>
            {projects.map((p, i) => (
              <li key={p.id}>
                <button
                  ref={i === 0 ? firstRef : undefined}
                  type="button"
                  className={styles.option}
                  aria-current={currentId === p.id ? "true" : undefined}
                  onClick={() =>
                    onMove({
                      kind: "project",
                      id: p.id,
                      name: p.name,
                      color: p.color || undefined,
                    })
                  }
                >
                  {p.color && (
                    <span
                      className={styles.dot}
                      style={{ background: p.color }}
                      aria-hidden="true"
                    />
                  )}
                  <span className={styles.optName}>{p.name}</span>
                  {currentId === p.id && (
                    <Check size={14} aria-label="current" />
                  )}
                </button>
              </li>
            ))}
            <li>
              <button
                ref={projects.length === 0 ? firstRef : undefined}
                type="button"
                className={styles.option}
                aria-current={currentId === null ? "true" : undefined}
                onClick={() => onMove(null)}
              >
                <FolderTree size={14} aria-hidden="true" />
                <span className={styles.optName}>Default project</span>
                {currentId === null && <Check size={14} aria-label="current" />}
              </button>
            </li>
          </ul>
        )}
        {projects?.length === 0 && !error && (
          <p className={styles.help}>
            No projects yet — create one in Settings or the overview map.
          </p>
        )}
        <div className={styles.actions}>
          <button
            ref={cancelRef}
            type="button"
            className={styles.cancel}
            onClick={onCancel}
          >
            Cancel
          </button>
        </div>
      </div>
    </div>
  );
}
