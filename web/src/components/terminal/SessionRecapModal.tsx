import { AlertTriangle, RefreshCw, X } from "lucide-react";
import { type CSSProperties, useEffect, useRef, useState } from "react";
import { createPortal } from "react-dom";
import { api, ApiError } from "../../lib/api";
import {
  engineBadge,
  engineName,
  projectColor,
  relTime,
  shortCwd,
} from "../../lib/format";
import { inlineMarkup } from "../../lib/inlineMarkup";
import { sessionStatus, type SessionStatusBase } from "../../lib/sessionStatus";
import type { ProjectRef } from "../../types/api";
import styles from "./SessionRecapModal.module.css";

/** Session-brief modal (#481): the recap icon in the terminal header opens this. Re-entering a
 *  session, this is where you find out what it IS and what happened in it without scrolling the
 *  transcript.
 *
 *  #744 gave it the sidebar row's full identity — title, subtitle (the AI summary, promoted out
 *  of its own labelled section), status LED, engine box, project, last-updated — plus the review
 *  age, and turned the recap into an ordered TIMELINE instead of one pre-wrapped paragraph.
 *
 *  All text here is model-derived DATA. The recap's steps are split on newlines and run through
 *  `inlineMarkup`, which maps a two-token subset (`**bold**`, `` `code` ``) onto React elements
 *  and leaves everything else literal — there is no html sink anywhere in this component.
 *  "Review now" refreshes in place via `POST /api/sessions/{id}/review` (the sidebar/store
 *  catches up on its own poll).
 *
 *  Accessibility mirrors RenameProjectModal: `role="dialog"`/`aria-modal`, labelled by the
 *  title; focus moves in on open and returns to the trigger on close; Esc + backdrop click
 *  close. */
export function SessionRecapModal({
  sessionId,
  engine,
  title,
  project,
  lastMtime,
  statusRow,
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
  /** Resolved project ref for the meta line — entity (name + colour dot) or folder (cwd). */
  project?: ProjectRef;
  /** Session mtime → the "updated <rel>" fact the sidebar row also shows. */
  lastMtime?: number;
  /** The row fields the session's own dot resolves from (`lib/sessionStatus`) — intervention /
   *  working / draft / idle, the same resolver the sidebar row's dot uses, so both entry points
   *  agree. Deliberately NOT the terminal's socket state: `LIVE` means "this browser is
   *  attached", which is a different claim from "the agent is working". Omitted when the session
   *  row isn't resolved yet (an unreconciled placeholder id) — the brief then shows no dot rather
   *  than guessing one.
   *
   *  Carries only what a review cannot change — the intervention pair comes from this modal's
   *  own state, which "Review now" refreshes ahead of the store (see `led` below). */
  statusRow?: SessionStatusBase;
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

  // Re-resolve the dot through THE resolver, but from this modal's intervention pair rather than
  // the row's: "Review now" can clear (or raise) intervention, and the store only catches up on
  // its next poll. Reading the row's pre-resolved status would leave the header LED asserting
  // `attention` while the panel right below it says the intervention is gone — one session
  // showing two contradictory states. Everything the review does NOT own (working > draft > idle,
  // and the review-excluded rule) still comes from the row, so the fallback is the row's answer.
  const led = statusRow
    ? sessionStatus({
        ...statusRow,
        intervention_required: state.interventionRequired,
        intervention_reason: state.interventionReason,
      })
    : undefined;

  const titleId = "session-recap-title";
  const reviewedLabel = state.reviewedAt
    ? `reviewed ${relTime(state.reviewedAt)}`
    : "not reviewed yet";
  // A folder ref's `name` is the FULL cwd by server contract (projects.resolve) — clients
  // shorten it; an adopted project keeps its entity name and gets the colour dot.
  const projectLabel = project
    ? project.kind === "project"
      ? project.name
      : shortCwd(project.name)
    : "";
  const projectStyle =
    project?.kind === "project"
      ? ({
          "--proj": project.color || projectColor(project.id),
        } as CSSProperties)
      : undefined;
  // Subtitle = the sidebar row's summary line, same precedence: the exclusion marker wins over
  // the AI summary, and an unreviewed session says so rather than showing an empty gap.
  const subtitle = reviewExcluded
    ? "Excluded from AI review"
    : state.summary || "No summary yet.";
  // The recap is a newline-separated timeline by contract (review.py `_recap_shape_guard`), so
  // one non-empty line is one step. Blank lines are already dropped server-side; trimming here
  // keeps a hand-seeded or legacy value rendering the same way.
  const steps = state.recap
    .split("\n")
    .map((line) => line.trim())
    .filter(Boolean);

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
        {/* Subtitle (#744): the AI summary promoted out of its own labelled SUMMARY section —
            same text, one fewer micro-label, and it reads as the session's subtitle exactly
            like the sidebar row's second line. */}
        <p
          className={
            state.summary || reviewExcluded
              ? styles.subtitle
              : styles.subtitleMuted
          }
        >
          {subtitle}
        </p>
        {/* Meta run (#744): everything the sidebar row carries about this session's identity —
            live state, engine, project, staleness — plus how fresh the review itself is. */}
        <p className={styles.meta}>
          {/* No row resolved → no dot. An honest omission beats inventing "idle" for a session
              whose state we simply don't have yet (design §7: unavailable states stay honest). */}
          {led && (
            <span
              className={`${styles.metaLed} hud-led ${led.variant}`}
              role={led.role}
              aria-label={led.label}
              aria-hidden={led.variant === "idle" ? true : undefined}
              title={led.title}
            />
          )}
          <span className={styles.engTag} title={engineName(engine)}>
            {engineBadge(engine)}
          </span>
          {projectLabel && (
            <span className={styles.projectChip} style={projectStyle}>
              {project?.kind === "project" && (
                <span className={styles.projectDot} aria-hidden="true" />
              )}
              {projectLabel}
            </span>
          )}
          {lastMtime != null && (
            <>
              <span className={styles.sep} aria-hidden="true">
                ·
              </span>
              <span>updated {relTime(lastMtime)}</span>
            </>
          )}
          <span className={styles.sep} aria-hidden="true">
            ·
          </span>
          <span>{reviewedLabel}</span>
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
          <span className={styles.label}>Recap // chronological</span>
          {steps.length > 0 ? (
            /* An ordered timeline (#744): one step per line, first → last, with the final step
               accented — "where we are now" is what you re-enter a session to find. <ol> carries
               the ordinal semantically, which is why the prompt asks the model NOT to number. */
            <ol className={styles.timeline}>
              {steps.map((step, i) => (
                <li
                  key={i}
                  className={
                    i === steps.length - 1 ? styles.stepLast : undefined
                  }
                >
                  {inlineMarkup(step)}
                </li>
              ))}
            </ol>
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
            <RefreshCw
              size={13}
              className={reviewing ? styles.spin : ""}
              aria-hidden="true"
            />
            {reviewing ? "Reviewing…" : "Review now"}
          </button>
        </div>
      </div>
    </div>,
    document.body,
  );
}
