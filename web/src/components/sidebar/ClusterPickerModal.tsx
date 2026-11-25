import { Check, Undo2, X } from "lucide-react";
import { useEffect, useRef, useState } from "react";
import { createPortal } from "react-dom";
import { api, ApiError } from "../../lib/api";
import { announceClustersChanged } from "../../lib/clusterEvents";
import { CLUSTER_TITLE_MAX } from "../../lib/clusters";
import type { ClusterDetail } from "../../types/api";
import dlg from "../HudDialog.module.css";
import { useFocusContainment } from "../pulse/useModalDrawer";
import styles from "./Clusters.module.css";

interface Props {
  /** The dialog's head tag: "Move to cluster" or "Merge into". */
  heading: string;
  /** What is being moved or merged, shown under the head. */
  subjectLabel: string;
  subject: string;
  /** The project whose clusters are offered. A cluster never spans projects. */
  projectId: string;
  /** The cluster the subject is in now (marked), if any. */
  currentId?: string | null;
  /** A cluster that cannot be picked (the one being merged). */
  excludeId?: string;
  /** Offer "Back to HQ's choice" (a session that is in a cluster). */
  onRelease?: () => void;
  /** Offer "New cluster": create one with this title and pick it. */
  allowCreate?: boolean;
  onPick: (cluster: { id: string; title: string }) => void;
  onCancel: () => void;
  /** Shown while the caller's own write is running or after it failed. */
  busy?: boolean;
  error?: string | null;
  returnFocusTo?: HTMLElement | null;
}

/** Pick a cluster of one project (#1997): the session row's "Move to cluster…" and the cluster
 *  head's "Merge into…". Built on the MoveToProjectModal pattern: `role="dialog"`, `aria-modal`,
 *  focus enters on mount (Cancel, present in every state) and returns to the trigger, Tab is
 *  contained, Esc / the close button / the backdrop cancel, every option is a real button. */
export function ClusterPickerModal({
  heading,
  subjectLabel,
  subject,
  projectId,
  currentId,
  excludeId,
  onRelease,
  allowCreate,
  onPick,
  onCancel,
  busy,
  error,
  returnFocusTo,
}: Props) {
  const [clusters, setClusters] = useState<ClusterDetail[] | null>(null);
  const [loadError, setLoadError] = useState<string | null>(null);
  const [draft, setDraft] = useState("");
  const [creating, setCreating] = useState(false);
  const [createError, setCreateError] = useState<string | null>(null);
  /** The title of a cluster this dialog created, said once it exists. */
  const [created, setCreated] = useState<string | null>(null);
  const firstRef = useRef<HTMLButtonElement>(null);
  const dialogRef = useRef<HTMLDivElement | null>(null);
  const cancelRef = useRef<HTMLButtonElement | null>(null);

  useEffect(() => {
    let alive = true;
    api
      .clusters(projectId)
      .then((r) => {
        if (!alive) return;
        if (r.unreadable) setLoadError("Clusters are unavailable right now.");
        else setClusters(r.clusters.filter((c) => c.id !== excludeId));
      })
      .catch(() => alive && setLoadError("Couldn’t load clusters."));
    return () => {
      alive = false;
    };
  }, [projectId, excludeId]);
  useEffect(() => {
    cancelRef.current?.focus();
  }, []);
  useEffect(() => {
    if (clusters?.length) firstRef.current?.focus();
  }, [clusters]);
  useFocusContainment({ active: true, panelRef: dialogRef });
  useEffect(() => () => returnFocusTo?.focus?.(), [returnFocusTo]);
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

  const create = async () => {
    const title = draft.trim();
    if (!title || creating) return;
    setCreating(true);
    setCreateError(null);
    try {
      const made = await api.createCluster(projectId, title);
      // The cluster exists from here on, whatever the caller's own write does next. Keep it in
      // the list and clear the draft, so a failed move is retried by picking it — never by
      // creating a second one (an operator cluster survives empty and counts toward the cap).
      setClusters((prev) => [...(prev ?? []).filter((c) => c.id !== made.id), made]);
      setDraft("");
      setCreated(made.title);
      announceClustersChanged();
      onPick({ id: made.id, title: made.title });
    } catch (e) {
      setCreateError(e instanceof ApiError ? e.message : "That didn’t save.");
    } finally {
      setCreating(false);
    }
  };

  const titleId = "cluster-picker-title";
  const locked = busy || creating;

  return createPortal(
    <div className={dlg.backdrop} onMouseDown={onCancel}>
      <div
        ref={dialogRef}
        tabIndex={-1}
        role="dialog"
        aria-modal="true"
        aria-labelledby={titleId}
        className={`${dlg.dialog} ${dlg.narrow}`}
        onMouseDown={(e) => e.stopPropagation()}
        data-testid="cluster-picker"
      >
        <div className={dlg.head}>
          <span id={titleId} className={dlg.tag}>
            {heading}
          </span>
          <button
            type="button"
            className={dlg.close}
            onClick={onCancel}
            aria-label="Close cluster dialog"
          >
            <X size={16} aria-hidden="true" />
          </button>
        </div>
        <p className={dlg.from}>
          <span className={dlg.fromLabel}>{subjectLabel} //</span>
          <b className={dlg.fromTitle}>{subject}</b>
        </p>
        <span className={dlg.label}>Cluster //</span>
        {loadError ? (
          <p className={dlg.muted} role="alert">
            {loadError}
          </p>
        ) : clusters === null ? (
          <p className={dlg.muted}>Loading clusters…</p>
        ) : (
          <ul className={dlg.list}>
            {clusters.map((c, i) => (
              <li key={c.id}>
                <button
                  ref={i === 0 ? firstRef : undefined}
                  type="button"
                  className={dlg.option}
                  aria-current={currentId === c.id ? "true" : undefined}
                  disabled={locked}
                  onClick={() => onPick({ id: c.id, title: c.title })}
                >
                  <span className={styles.bullet} aria-hidden="true" />
                  <span className={dlg.optName}>{c.title}</span>
                  <span className={styles.pickCount}>{c.count}</span>
                  {currentId === c.id && <Check size={14} aria-label="current" />}
                </button>
              </li>
            ))}
            {onRelease ? (
              <li>
                <button
                  type="button"
                  className={dlg.option}
                  disabled={locked}
                  onClick={onRelease}
                >
                  <Undo2 size={14} aria-hidden="true" />
                  <span className={dlg.optName}>Back to HQ’s choice</span>
                </button>
              </li>
            ) : null}
          </ul>
        )}
        {clusters?.length === 0 && !loadError ? (
          <p className={dlg.help}>
            {allowCreate
              ? "This project has no clusters yet. Name one below."
              : "This project has no other cluster."}
          </p>
        ) : null}
        {allowCreate && clusters !== null ? (
          <form
            className={styles.create}
            onSubmit={(e) => {
              e.preventDefault();
              void create();
            }}
          >
            <label className={dlg.label} htmlFor="cluster-picker-new">
              New cluster //
            </label>
            <div className={styles.createRow}>
              <input
                id="cluster-picker-new"
                className={dlg.input}
                value={draft}
                maxLength={CLUSTER_TITLE_MAX}
                placeholder="Topic"
                disabled={locked}
                onChange={(e) => setDraft(e.target.value)}
              />
              <button
                type="submit"
                className={dlg.go}
                disabled={locked || !draft.trim()}
              >
                Create
              </button>
            </div>
          </form>
        ) : null}
        {onRelease || allowCreate ? (
          <p className={dlg.help}>
            A session you move stays where you put it. HQ keeps grouping the others.
          </p>
        ) : null}
        {created ? (
          <p className={dlg.help} role="status" data-testid="cluster-picker-created">
            Created “{created}”.{error ? " Pick it above to try the move again." : ""}
          </p>
        ) : null}
        {error || createError ? (
          <p className={styles.err} role="alert">
            {error || createError}
          </p>
        ) : null}
        <div className={dlg.actions}>
          <button
            ref={cancelRef}
            type="button"
            className={dlg.cancel}
            onClick={onCancel}
          >
            Cancel
          </button>
        </div>
      </div>
    </div>,
    document.body,
  );
}
