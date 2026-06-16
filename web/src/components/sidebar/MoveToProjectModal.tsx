import { Check, FolderTree } from "lucide-react";
import { useEffect, useRef, useState } from "react";
import { api } from "../../lib/api";
import { shortCwd } from "../../lib/format";
import type { ProjectEntity, ProjectRef, Session } from "../../types/api";
import styles from "./MoveToProjectModal.module.css";

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
  const currentId = session.project.kind === "project" ? session.project.id : null;

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
  useEffect(() => {
    if (projects) firstRef.current?.focus();
  }, [projects]);
  // Restore focus to the trigger on unmount.
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
                    onMove({ kind: "project", id: p.id, name: p.name, color: p.color || undefined })
                  }
                >
                  {p.color && (
                    <span className={styles.dot} style={{ background: p.color }} aria-hidden="true" />
                  )}
                  <span className={styles.optName}>{p.name}</span>
                  {currentId === p.id && <Check size={14} aria-label="current" />}
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
                <span className={styles.optName}>Unassigned (folder)</span>
                {currentId === null && <Check size={14} aria-label="current" />}
              </button>
            </li>
          </ul>
        )}
        {projects?.length === 0 && !error && (
          <p className={styles.help}>No projects yet — create one in Settings or the overview map.</p>
        )}
        <div className={styles.actions}>
          <button type="button" className={styles.cancel} onClick={onCancel}>
            Cancel
          </button>
        </div>
      </div>
    </div>
  );
}
