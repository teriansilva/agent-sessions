import {
  Archive,
  ArchiveRestore,
  Check,
  Eye,
  EyeOff,
  Pencil,
  Plus,
  Sparkles,
  X,
} from "lucide-react";
import { useEffect, useState } from "react";
import { Link, NavLink } from "react-router-dom";
import { useConfig } from "../../app/config";
import { useOverviewPrefs } from "../../app/overviewPrefs";
import { useSessionsStore } from "../../app/sessionsStore";
import { useSessionsList } from "../../hooks/useSessionsList";
import { displayProjectName, engineBadge, relTime } from "../../lib/format";
import type { Session } from "../../types/api";
import { FiltersBar } from "./Filters";
import { RowMenu, type RowMenuEntry } from "./RowMenu";
import styles from "./SessionList.module.css";

/** Activity since the last successful review makes the summary stale (#356): the AI's
 *  one-liner describes an older state, so the row exposes the review's age instead of
 *  letting an old green summary read as current. Small grace so the review that *caused*
 *  the latest mtime bump doesn't immediately flag itself. */
const REVIEW_STALE_GRACE_S = 60;

function reviewIsStale(s: Session): boolean {
  return (
    s.reviewed_at != null && s.last_mtime > s.reviewed_at + REVIEW_STALE_GRACE_S
  );
}

interface RowProps {
  s: Session;
  onRename: (id: string, title: string) => Promise<void>;
  onToggleArchive: (id: string, currentlyArchived: boolean) => Promise<void>;
  /** AI review (#356): manual "Review now" + per-session exclude toggle. Undefined when
   *  the feature is unconfigured (the controls are hidden). */
  onReviewNow?: (id: string) => Promise<void>;
  onToggleReviewExcluded?: (id: string, excluded: boolean) => Promise<void>;
  /** Close the mobile drawer on tap — tapping the already-active row is a same-route no-op,
   *  so the route-change effect in App won't fire (#283). */
  onNavigate?: () => void;
}

function Row({
  s,
  onRename,
  onToggleArchive,
  onReviewNow,
  onToggleReviewExcluded,
  onNavigate,
}: RowProps) {
  const { projectNames } = useOverviewPrefs();
  const [editing, setEditing] = useState(false);
  const [draft, setDraft] = useState(s.title);
  const [busy, setBusy] = useState(false);
  const [reviewing, setReviewing] = useState(false);
  // Keeps the hover-revealed ⋯ cluster visible while its menu is open: the menu lives
  // in a body portal, so :focus-within on the row no longer covers the open state.
  const [menuOpen, setMenuOpen] = useState(false);

  const reviewNow = async () => {
    if (!onReviewNow) return;
    setReviewing(true);
    try {
      await onReviewNow(s.id);
    } catch {
      /* fail-soft (#356): the last good result + its stale age keep showing */
    } finally {
      setReviewing(false);
    }
  };

  const toggleExcluded = async () => {
    if (!onToggleReviewExcluded) return;
    setBusy(true);
    try {
      await onToggleReviewExcluded(s.id, !s.review_excluded);
    } finally {
      setBusy(false);
    }
  };

  const commit = async () => {
    const title = draft.trim();
    if (!title || title === s.title) {
      setEditing(false);
      return;
    }
    setBusy(true);
    try {
      await onRename(s.id, title);
      setEditing(false);
    } finally {
      setBusy(false);
    }
  };

  const toggleArchive = async () => {
    setBusy(true);
    try {
      await onToggleArchive(s.id, s.archived);
    } finally {
      setBusy(false);
    }
  };

  if (editing) {
    return (
      <li className={styles.rowWrap}>
        <form
          className={styles.editRow}
          onSubmit={(e) => {
            e.preventDefault();
            void commit();
          }}
        >
          <input
            ref={(el) => el?.focus()}
            className={styles.editInput}
            aria-label="Session title"
            value={draft}
            disabled={busy}
            onChange={(e) => setDraft(e.target.value)}
            onKeyDown={(e) => {
              if (e.key === "Escape") {
                setDraft(s.title);
                setEditing(false);
              }
            }}
          />
          <button type="submit" className={styles.iconBtn} aria-label="Save title" disabled={busy}>
            <Check size={15} />
          </button>
          <button
            type="button"
            className={styles.iconBtn}
            aria-label="Cancel rename"
            disabled={busy}
            onClick={() => {
              setDraft(s.title);
              setEditing(false);
            }}
          >
            <X size={15} />
          </button>
        </form>
      </li>
    );
  }

  // The row's single ⋯ menu (#384) — replaces the four inline icon buttons. Same
  // actions, same gating: Review-now / exclude items exist only when the AI-review
  // handlers were passed down (ai_review.configured), the exclude item flips label
  // for an excluded row, archive flips for an archived one. Busy items are disabled
  // in place (aria-disabled) rather than removed, so the menu doesn't reflow.
  const menuItems: RowMenuEntry[] = [];
  if (onReviewNow && !s.review_excluded) {
    menuItems.push({
      key: "review",
      label: "Review now",
      ariaLabel: "Review session now",
      icon: <Sparkles size={15} className={reviewing ? styles.spin : undefined} />,
      disabled: busy || reviewing,
      onSelect: () => void reviewNow(),
    });
  }
  if (onToggleReviewExcluded) {
    menuItems.push({
      key: "exclude",
      label: s.review_excluded ? "Include in AI review" : "Exclude from AI review",
      icon: s.review_excluded ? <Eye size={15} /> : <EyeOff size={15} />,
      disabled: busy || reviewing,
      onSelect: () => void toggleExcluded(),
    });
  }
  if (menuItems.length > 0) menuItems.push("separator");
  menuItems.push(
    {
      key: "rename",
      label: "Rename",
      ariaLabel: "Rename session",
      icon: <Pencil size={15} />,
      disabled: busy,
      onSelect: () => {
        setDraft(s.title);
        setEditing(true);
      },
    },
    {
      key: "archive",
      label: s.archived ? "Unarchive" : "Archive",
      ariaLabel: s.archived ? "Unarchive session" : "Archive session",
      icon: s.archived ? <ArchiveRestore size={15} /> : <Archive size={15} />,
      disabled: busy,
      onSelect: () => void toggleArchive(),
    },
  );

  return (
    <li className={styles.rowWrap}>
      <NavLink
        to={`/s/${s.engine}/${s.uuid}`}
        className={({ isActive }) => (isActive ? `${styles.row} ${styles.active}` : styles.row)}
        onClick={onNavigate}
      >
        {/* Leading status LED (#211 4b): a live row pulses green and carries the meaningful
            "agent working" status for screen readers; an idle row shows a dim, decorative dot.
            Reuses the global .hud-led primitive so the indicator matches the topbar/classbar. */}
        {s.working ? (
          <span
            className={`${styles.led} hud-led up`}
            role="status"
            aria-label="agent working"
            title="agent working"
          />
        ) : (
          <span className={`${styles.led} hud-led idle`} aria-hidden="true" title="idle" />
        )}
        {/* Amber ⚠/“!” badge (#356): advisory “needs a human”, reason as tooltip. */}
        {s.intervention_required && !s.review_excluded && (
          <span
            className={styles.alertBadge}
            role="img"
            aria-label={`intervention required: ${s.intervention_reason || "see session"}`}
            title={s.intervention_reason || "Intervention required"}
          >
            !
          </span>
        )}
        <div className={styles.body}>
          <div className={styles.title}>{s.title || "(untitled)"}</div>
          {/* One-line AI summary (#356) — or the exclusion marker; stale-age hint when
              there has been activity since the last successful review. */}
          {s.review_excluded ? (
            <div className={`${styles.summary} ${styles.summaryExcluded}`}>
              Excluded from AI review
            </div>
          ) : (
            s.ai_summary && (
              <div className={styles.summary}>
                {s.ai_summary}
                {reviewIsStale(s) && s.reviewed_at != null && (
                  <span className={styles.summaryStale}>
                    {" "}
                    · reviewed {relTime(s.reviewed_at)}
                  </span>
                )}
              </div>
            )
          )}
          <div className={styles.meta}>
            <span className={styles.engineTag}>{engineBadge(s.engine)}</span>
            <span className={styles.metaText}>
              {/* #361: an assigned/adopted project shows its entity name as a chip (with
                  its color dot when set); unassigned rows keep the per-cwd custom-name
                  (#148) / shortened-path display behind a decorative folder marker. */}
              {s.project.kind === "project" ? (
                <span className={styles.projectChip}>
                  {s.project.color && (
                    <span
                      className={styles.projectDot}
                      style={{ background: s.project.color }}
                      aria-hidden="true"
                    />
                  )}
                  {s.project.name}
                </span>
              ) : (
                <span className={styles.folderChip}>
                  <span className={styles.folderMark} aria-hidden="true">
                    {"▸ "}
                  </span>
                  {displayProjectName(s.cwd, projectNames)}
                </span>
              )}{" "}
              · {relTime(s.last_mtime)}
            </span>
          </div>
        </div>
      </NavLink>
      <div className={`${styles.actions} ${menuOpen ? styles.actionsOpen : ""}`}>
        <RowMenu
          items={menuItems}
          title={s.title || "(untitled)"}
          triggerIcon={
            reviewing ? <Sparkles size={15} className={styles.spin} /> : undefined
          }
          onOpenChange={setMenuOpen}
        />
      </div>
    </li>
  );
}

interface SessionListProps {
  /** Close the mobile off-canvas drawer on tap. Threaded onto New session and the session
   *  rows because those can be same-route no-ops that the route-change effect misses (#283). */
  onNavigate?: () => void;
}

/** Sidebar: filters + facets + paginated session list. Rows link to the session
 *  URL (open/switch) and expose rename + archive/unarchive actions. */
export function SessionList({ onNavigate }: SessionListProps = {}) {
  const {
    sessions,
    total,
    facets,
    filters,
    loading,
    error,
    hasMore,
    loadMore,
    update,
    clear,
    renameRow,
    setArchived,
    reviewRow,
    setReviewExcluded,
  } = useSessionsList();

  // AI review controls (#356) only appear once the endpoint is configured — an
  // unconfigured install keeps the lean three-button row.
  const aiConfigured = useConfig()?.ai_review?.configured ?? false;

  // Publish the loaded rows so the compact header can resolve the current session's title.
  const { setSessions } = useSessionsStore();
  useEffect(() => {
    setSessions(sessions);
  }, [sessions, setSessions]);

  // Relative-time labels otherwise stay frozen ("2m ago", "2m ago", …) until something else
  // re-renders the list. Bump a counter every ~30s while the tab is visible so `relTime` is
  // recomputed without forcing an API call (#159). Background poll handles row order.
  const [, setClockTick] = useState(0);
  useEffect(() => {
    if (typeof document === "undefined") return;
    let id: number | undefined;
    const start = () => {
      if (id == null) id = window.setInterval(() => setClockTick((t) => t + 1), 30_000);
    };
    const stop = () => {
      if (id != null) window.clearInterval(id);
      id = undefined;
    };
    const onVis = () => (document.hidden ? stop() : start());
    if (!document.hidden) start();
    document.addEventListener("visibilitychange", onVis);
    return () => {
      stop();
      document.removeEventListener("visibilitychange", onVis);
    };
  }, []);

  return (
    <div className={styles.wrap}>
      <Link to="/" className={`${styles.newBtn} shine`} onClick={onNavigate}>
        <Plus size={16} />
        New session
      </Link>
      <FiltersBar filters={filters} facets={facets} onChange={update} onClear={clear} />
      {error ? (
        <div className={styles.empty}>{error}</div>
      ) : sessions.length === 0 && !loading ? (
        <div className={styles.empty}>
          {filters.q || filters.project || filters.engine
            ? "No sessions match — clear filters."
            : filters.archived
              ? "No archived sessions."
              : "No sessions yet."}
        </div>
      ) : (
        <ul className={styles.list} aria-label={`${total} sessions`}>
          {sessions.map((s) => (
            <Row
              key={s.id}
              s={s}
              onRename={renameRow}
              onToggleArchive={setArchived}
              onReviewNow={aiConfigured ? reviewRow : undefined}
              onToggleReviewExcluded={aiConfigured ? setReviewExcluded : undefined}
              onNavigate={onNavigate}
            />
          ))}
          {hasMore && (
            <li className={styles.more}>
              <button type="button" onClick={loadMore} disabled={loading}>
                {loading ? "Loading…" : "Load more"}
              </button>
            </li>
          )}
        </ul>
      )}
    </div>
  );
}
