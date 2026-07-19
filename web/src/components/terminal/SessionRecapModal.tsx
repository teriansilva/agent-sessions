import { AlertTriangle, RefreshCw, X } from "lucide-react";
import { useEffect, useRef, useState } from "react";
import { createPortal } from "react-dom";
import { api, ApiError } from "../../lib/api";
import { relTime } from "../../lib/format";
import styles from "./SessionRecapModal.module.css";

/** Session-brief modal (#481): the recap icon in the terminal header opens this. Shows the
 *  full (untruncated) title, an optional intervention chip, the one-line AI summary, and the
 *  chronological RECAP of the whole session — so you can re-enter a session and understand
 *  what happened overall without scrolling the transcript.
 *
 *  All text here is model-derived DATA rendered as escaped plain text (React); the recap keeps
 *  its newlines via `white-space: pre-wrap`. "Review now" refreshes in place via the existing
 *  `POST /api/sessions/{id}/review` (the sidebar/store catches up on its own poll).
 *
 *  Accessibility mirrors RenameProjectModal: `role="dialog"`/`aria-modal`, labelled by the
 *  title; focus moves in on open and returns to the trigger on close; Esc + backdrop click
 *  close. */
export function SessionRecapModal({
  sessionId,
  engine,
  title,
  project,
  summary,
  recap,
  interventionRequired,
  interventionReason,
  reviewedAt,
  reviewExcluded,
  onClose,
  returnFocusTo,
}: {
  /** engine-qualified id (`<engine>:<native_id>`) — the review endpoint key. */
  sessionId: string;
  engine: string;
  /** Resolved display title (already user title → ai_title → first message). */
  title: string;
  /** Project label for the meta line, when resolved. */
  project?: string;
  summary?: string;
  recap?: string;
  interventionRequired?: boolean;
  interventionReason?: string;
  reviewedAt?: number | null;
  reviewExcluded?: boolean;
  onClose: () => void;
  /** The element that opened the modal — focus returns here on close. */
  returnFocusTo?: HTMLElement | null;
}) {
  // Local copy so "Review now" refreshes the modal in place; seeded from the row the header
  // already has. The store/sidebar reconverge on their own poll.
  const [state, setState] = useState<{
    summary: string;
    recap: string;
    interventionRequired: boolean;
    interventionReason: string;
    reviewedAt: number | null;
  }>({
    summary: summary ?? "",
    recap: recap ?? "",
    interventionRequired: !!interventionRequired,
    interventionReason: interventionReason ?? "",
    reviewedAt: reviewedAt ?? null,
  });
  const [reviewing, setReviewing] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const closeRef = useRef<HTMLButtonElement>(null);

  // Move focus to the close button on open + restore it to the trigger on close.
  useEffect(() => {
    closeRef.current?.focus();
    return () => returnFocusTo?.focus?.();
  }, [returnFocusTo]);

  // Global Escape → close (document-level so it catches wherever focus is).
  useEffect(() => {
    const onKey = (e: KeyboardEvent) => {
      if (e.key === "Escape") {
        e.preventDefault();
        onClose();
      }
    };
    document.addEventListener("keydown", onKey);
    return () => document.removeEventListener("keydown", onKey);
  }, [onClose]);

  const reviewNow = async () => {
    if (reviewing) return;
    setReviewing(true);
    setError(null);
    try {
      const r = await api.reviewNow(sessionId);
      setState({
        summary: r.ai_summary,
        recap: r.ai_recap,
        interventionRequired: r.intervention_required,
        interventionReason: r.intervention_reason,
        reviewedAt: r.reviewed_at,
      });
    } catch (e) {
      // 409 = unconfigured; anything else = the review failed and the last good result stays.
      setError(
        e instanceof ApiError && e.status === 409
          ? "AI review isn't configured — set it up in Settings → AI Review."
          : "Review failed — showing the last result.",
      );
    } finally {
      setReviewing(false);
    }
  };

  const titleId = "session-recap-title";
  const reviewedLabel = state.reviewedAt
    ? `reviewed ${relTime(state.reviewedAt)}`
    : "not reviewed yet";

  // Portal to <body>: the terminal pane is a backdrop-filter containing block, which would
  // otherwise clip a position:fixed overlay to the pane instead of the full viewport.
  return createPortal(
    <div className={styles.backdrop} onMouseDown={onClose}>
      <div
        role="dialog"
        aria-modal="true"
        aria-labelledby={titleId}
        className={styles.dialog}
        // Stop clicks inside from bubbling to the backdrop → no accidental close.
        onMouseDown={(e) => e.stopPropagation()}
      >
        <div className={styles.head}>
          <span className={styles.tag}>Session brief</span>
          <button
            ref={closeRef}
            type="button"
            className={styles.close}
            onClick={onClose}
            aria-label="Close session brief"
          >
            <X size={16} aria-hidden="true" />
          </button>
        </div>

        <h2 id={titleId} className={styles.title}>
          {title}
        </h2>
        <p className={styles.meta}>
          {engine.toUpperCase()}
          {project ? ` · ${project}` : ""} · {reviewedLabel}
        </p>

        {state.interventionRequired && (
          <p className={styles.chip} role="status">
            <AlertTriangle size={13} aria-hidden="true" />
            <span>
              Needs you
              {state.interventionReason ? ` · ${state.interventionReason}` : ""}
            </span>
          </p>
        )}

        <div className={styles.section}>
          <span className={styles.label}>Summary</span>
          {state.summary ? (
            <p className={styles.summary}>{state.summary}</p>
          ) : (
            <p className={styles.muted}>No summary yet.</p>
          )}
        </div>

        <div className={styles.section}>
          <span className={styles.label}>Recap // chronological</span>
          {state.recap ? (
            <p className={styles.recap}>{state.recap}</p>
          ) : (
            <p className={styles.muted}>
              {reviewExcluded
                ? "This session is excluded from AI review."
                : "No recap yet — generated when this session is reviewed."}
            </p>
          )}
        </div>

        {error && <p className={styles.error}>{error}</p>}

        <div className={styles.actions}>
          <span className={styles.foot}>
            Auto-generated from this session · updates as it continues
          </span>
          <button
            type="button"
            className={styles.review}
            onClick={reviewNow}
            disabled={reviewing || reviewExcluded}
            title={
              reviewExcluded
                ? "This session is excluded from AI review"
                : "Re-run the AI review now"
            }
          >
            <RefreshCw size={13} className={reviewing ? styles.spin : ""} aria-hidden="true" />
            {reviewing ? "Reviewing…" : "Review now"}
          </button>
        </div>
      </div>
    </div>,
    document.body,
  );
}
