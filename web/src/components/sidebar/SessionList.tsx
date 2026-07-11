import {
  Archive,
  ArchiveRestore,
  Check,
  Eye,
  EyeOff,
  FolderInput,
  Pencil,
  Plus,
  Sparkles,
  Star,
  Tag,
  X,
} from "lucide-react";
import {
  type CSSProperties,
  type ReactNode,
  useCallback,
  useEffect,
  useLayoutEffect,
  useRef,
  useState,
} from "react";
import { Link, NavLink, useMatch, useNavigate } from "react-router-dom";
import { useConfig } from "../../app/config";
import { useSessionsStore } from "../../app/sessionsStore";
import { useSessionsList } from "../../hooks/useSessionsList";
import { ApiError } from "../../lib/api";
import { engineBadge, projectColor, relTime } from "../../lib/format";
import type { ProjectRef, Session } from "../../types/api";
import { FiltersBar } from "./Filters";
import { MoveToProjectModal } from "./MoveToProjectModal";
import { RowMenu, type RowMenuEntry } from "./RowMenu";
import styles from "./SessionList.module.css";

/** Activity since the last successful review makes the summary stale (#356): the AI's
 *  one-liner describes an older state, so the row exposes the review's age instead of
 *  letting an old green summary read as current. Small grace so the review that *caused*
 *  the latest mtime bump doesn't immediately flag itself. */
const REVIEW_STALE_GRACE_S = 60;

/** Review-now outcome toast dwell (#392): success flashes the fresh summary briefly;
 *  an error lingers long enough to actually read the gateway detail. */
export const TOAST_OK_MS = 6_000;
export const TOAST_ERR_MS = 12_000;
const ROW_REORDER_ANIM_MS = 240;
const ROW_REORDER_MIN_DELTA_PX = 2;

function reviewIsStale(s: Session): boolean {
  return (
    s.reviewed_at != null && s.last_mtime > s.reviewed_at + REVIEW_STALE_GRACE_S
  );
}

/** Auto-scrolling text line (#551). Renders `children` (or `text`) inside a truncating
 *  container, and — only while the row is SELECTED, the content actually overflows, and the
 *  user hasn't asked to reduce motion — animates it horizontally back and forth (ping-pong,
 *  pausing at each end) so the whole string can be read. Otherwise it's the plain ellipsis
 *  line it was before. `text` is always exposed via the native `title` tooltip, so the full
 *  value stays reachable under reduced-motion / on hover regardless. Only the selected row
 *  animates, so the list stays calm. */
function MarqueeText({
  active,
  className,
  text,
  children,
}: {
  active: boolean;
  className?: string;
  text: string;
  children?: ReactNode;
}) {
  const wrapRef = useRef<HTMLDivElement>(null);
  const [shift, setShift] = useState(0); // px of overflow to travel; 0 ⇒ static (no scroll)

  useEffect(() => {
    const wrap = wrapRef.current;
    if (!wrap) return;
    const reduce = window.matchMedia?.("(prefers-reduced-motion: reduce)").matches ?? false;
    const measure = () => {
      // scrollWidth − clientWidth is the hidden overflow regardless of the inner display mode;
      // transforms don't affect it, so it stays stable once scrolling.
      const over = wrap.scrollWidth - wrap.clientWidth;
      setShift(active && !reduce && over > 1 ? over : 0);
    };
    measure();
    if (typeof ResizeObserver === "undefined") return; // jsdom / very old browsers → static
    const ro = new ResizeObserver(measure);
    ro.observe(wrap);
    return () => ro.disconnect();
  }, [active, text]);

  const scrolling = shift > 0;
  const style = scrolling
    ? ({
        // Travel the exact overflow; duration scales with distance (bounded) so long and short
        // lines read at a similar pace.
        "--marq-shift": `-${shift}px`,
        "--marq-dur": `${Math.min(20, Math.max(5, shift / 30 + 3))}s`,
      } as CSSProperties)
    : undefined;

  return (
    <div ref={wrapRef} className={className} title={text}>
      <span className={scrolling ? styles.marqScroll : undefined} style={style}>
        {children ?? text}
      </span>
    </div>
  );
}

interface RowProps {
  s: Session;
  /** This row is the currently selected/open session — gates the auto-scroll marquee (#551). */
  active: boolean;
  onRename: (id: string, title: string) => Promise<void>;
  /** Set (or clear, with "") the row's custom tag (#551). */
  onSetTag: (id: string, tag: string) => Promise<void>;
  onToggleArchive: (id: string, currentlyArchived: boolean) => Promise<void>;
  /** Favorite toggle (#122): flips the row's `sticky` flag; favorited rows pin to the top. */
  onToggleFavorite: (id: string, value: boolean) => Promise<void>;
  /** AI review (#356): manual "Review now" + per-session exclude toggle. Undefined when
   *  the feature is unconfigured (the controls are hidden). */
  onReviewNow?: (id: string) => Promise<void>;
  onToggleReviewExcluded?: (id: string, excluded: boolean) => Promise<void>;
  /** Reassign the session to a project entity (or `null` to unassign) — the keyboard path
   *  for the map's drag-to-reassign (#424 Phase 5). */
  onSetProject: (id: string, ref: ProjectRef | null) => Promise<void>;
  /** Close the mobile drawer on tap — tapping the already-active row is a same-route no-op,
   *  so the route-change effect in App won't fire (#283). */
  onNavigate?: () => void;
  /** Physical list item ref used by the sidebar FLIP reorder animation (#607). */
  rowRef?: (el: HTMLLIElement | null) => void;
}

function Row({
  s,
  active,
  onRename,
  onSetTag,
  onToggleArchive,
  onToggleFavorite,
  onReviewNow,
  onToggleReviewExcluded,
  onSetProject,
  onNavigate,
  rowRef,
}: RowProps) {
  // Inline editor (#551): the row's title (Rename) OR its custom tag (Set tag…) share one
  // input row. "none" = not editing; `draft` holds whichever value is being edited.
  const [editMode, setEditMode] = useState<"none" | "title" | "tag">("none");
  const [draft, setDraft] = useState(s.title);
  const [busy, setBusy] = useState(false);
  const [reviewing, setReviewing] = useState(false);
  // Keeps the hover-revealed ⋯ cluster visible while its menu is open: the menu lives
  // in a body portal, so :focus-within on the row no longer covers the open state.
  const [menuOpen, setMenuOpen] = useState(false);
  // "Move to project" picker (#424 Phase 5b) — the keyboard path for drag-to-reassign.
  const [moving, setMoving] = useState(false);
  const [moveReturnFocus, setMoveReturnFocus] = useState<HTMLElement | null>(null);

  const handleMove = async (ref: ProjectRef | null) => {
    setMoving(false);
    const current = s.project.kind === "project" ? s.project.id : null;
    const next = ref && ref.kind === "project" ? ref.id : null;
    if (next === current) return; // chose the current assignment → no-op
    setBusy(true);
    try {
      await onSetProject(s.id, ref);
    } finally {
      setBusy(false);
    }
  };

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
    const value = draft.trim();
    if (editMode === "tag") {
      // Empty IS valid for a tag — it clears it; skip the write only when unchanged.
      if (value === (s.tag ?? "")) {
        setEditMode("none");
        return;
      }
      setBusy(true);
      try {
        await onSetTag(s.id, value);
        setEditMode("none");
      } finally {
        setBusy(false);
      }
      return;
    }
    // Title (Rename): an empty title is a no-op, unlike a tag.
    if (!value || value === s.title) {
      setEditMode("none");
      return;
    }
    setBusy(true);
    try {
      await onRename(s.id, value);
      setEditMode("none");
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

  const toggleFavorite = async () => {
    setBusy(true);
    try {
      await onToggleFavorite(s.id, !s.sticky);
    } finally {
      setBusy(false);
    }
  };

  if (editMode !== "none") {
    const isTag = editMode === "tag";
    return (
      <li ref={rowRef} className={styles.rowWrap}>
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
            aria-label={isTag ? "Session tag" : "Session title"}
            placeholder={isTag ? "Tag (text or emoji)" : undefined}
            maxLength={isTag ? 32 : undefined}
            value={draft}
            disabled={busy}
            onChange={(e) => setDraft(e.target.value)}
            onKeyDown={(e) => {
              if (e.key === "Escape") setEditMode("none");
            }}
          />
          <button
            type="submit"
            className={styles.iconBtn}
            aria-label={isTag ? "Save tag" : "Save title"}
            disabled={busy}
          >
            <Check size={15} />
          </button>
          <button
            type="button"
            className={styles.iconBtn}
            aria-label={isTag ? "Cancel tag edit" : "Cancel rename"}
            disabled={busy}
            onClick={() => setEditMode("none")}
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
      // Favorite toggle (#508): relocated off the row surface into the menu. The visible
      // ★ now lives as a small prefix on the meta line (favorited rows only); this item
      // carries the on/off accessible state the old standalone .favBtn used to.
      key: "favorite",
      label: s.sticky ? "Unfavorite" : "Favorite",
      ariaLabel: s.sticky ? "Unfavorite session" : "Favorite session",
      icon: <Star size={15} fill={s.sticky ? "currentColor" : "none"} />,
      disabled: busy,
      onSelect: () => void toggleFavorite(),
    },
    {
      key: "rename",
      label: "Rename",
      ariaLabel: "Rename session",
      icon: <Pencil size={15} />,
      disabled: busy,
      onSelect: () => {
        setDraft(s.title);
        setEditMode("title");
      },
    },
    {
      // Custom tag (#551): the same inline input as Rename, seeded with the current tag.
      key: "tag",
      label: s.tag ? "Edit tag…" : "Set tag…",
      ariaLabel: s.tag ? "Edit session tag" : "Set session tag",
      icon: <Tag size={15} />,
      disabled: busy,
      onSelect: () => {
        setDraft(s.tag ?? "");
        setEditMode("tag");
      },
    },
    {
      key: "move",
      label: "Move to project…",
      ariaLabel: "Move session to a project",
      icon: <FolderInput size={15} />,
      disabled: busy,
      onSelect: () => {
        // The trigger had focus when the menu item fired; restore to it when the modal closes.
        setMoveReturnFocus(document.activeElement as HTMLElement | null);
        setMoving(true);
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

  // #477: one leading status dot carries the whole row state — colour is the only signal,
  // no extra glyph or row width. Precedence (highest first): intervention (orange, never
  // masked by a draft) > working (green) > unsent draft (blue) > idle (grey). Every non-idle
  // state keeps a role + accessible name, so meaning is never colour-only (design §8).
  const dot =
    s.intervention_required && !s.review_excluded
      ? {
          variant: "attention",
          role: "img" as const,
          label: `intervention required: ${s.intervention_reason || "see session"}`,
          title: s.intervention_reason || "Intervention required",
        }
      : s.working
        ? { variant: "up", role: "status" as const, label: "agent working", title: "agent working" }
        : s.has_draft
          ? { variant: "draft", role: "img" as const, label: "unsent draft", title: "Unsent draft" }
          : { variant: "idle", role: undefined, label: undefined, title: "idle" };

  // Per-project accent (#285): the explicit entity color (Settings, #361) wins; otherwise a
  // stable hash of the ref key (entity id / folder cwd). One CSS var feeds the rail + dot.
  const proj = { "--proj": s.project.color || projectColor(s.project.id) } as CSSProperties;

  // Summary line (#356 + #551): an optional custom tag prefixes the AI summary (or the
  // "excluded" marker), joined by " · ". The line renders whenever there's a tag OR summary,
  // so a tagged-but-unreviewed row still shows its tag. `summaryText` is the plain-text form
  // for the marquee tooltip / reduced-motion fallback.
  const staleHint =
    !s.review_excluded && s.ai_summary && reviewIsStale(s) && s.reviewed_at != null
      ? ` · reviewed ${relTime(s.reviewed_at)}`
      : "";
  const summaryBody = s.review_excluded ? "Excluded from AI review" : (s.ai_summary ?? "");
  const showSummary = !!(s.tag || summaryBody);
  const summaryText = [s.tag, summaryBody].filter(Boolean).join(" · ") + staleHint;

  return (
    <li ref={rowRef} className={styles.rowWrap}>
      {moving && (
        <MoveToProjectModal
          session={s}
          onCancel={() => setMoving(false)}
          onMove={(ref) => void handleMove(ref)}
          returnFocusTo={moveReturnFocus}
        />
      )}
      <NavLink
        to={`/s/${s.engine}/${s.uuid}`}
        className={({ isActive }) => (isActive ? `${styles.row} ${styles.active}` : styles.row)}
        style={proj}
        onClick={onNavigate}
      >
        {/* Project rail (#285): its own inset layer so the active row's border-left accent
            and the status LED stay legible as separate cues. */}
        <span className={styles.projRail} aria-hidden="true" />
        {/* Single leading status dot (#477): colour-coded by precedence (intervention >
            working > draft > idle), reusing the global .hud-led primitive so it matches the
            topbar/classbar. This replaces the old separate green/idle LED + amber "!" badge
            (#211 4b / #356) — intervention now wins the dot's colour, never hidden by a draft. */}
        <span
          className={`${styles.led} hud-led ${dot.variant}`}
          role={dot.role}
          aria-label={dot.label}
          aria-hidden={dot.variant === "idle" ? true : undefined}
          title={dot.title}
        />
        <div className={styles.body}>
          {/* Title + summary auto-scroll (#551) when this row is selected and the text
              overflows; otherwise the same single-line ellipsis as before. */}
          <MarqueeText active={active} className={styles.title} text={s.title || "(untitled)"} />
          {/* One-line summary (#356): optional custom tag (#551) + AI summary / exclusion
              marker + stale-age hint when there's been activity since the last review. */}
          {showSummary && (
            <MarqueeText active={active} className={styles.summary} text={summaryText}>
              {s.tag && <span className={styles.summaryTag}>{s.tag}</span>}
              {s.tag && summaryBody ? " · " : null}
              {s.review_excluded ? (
                <span className={styles.summaryExcluded}>Excluded from AI review</span>
              ) : (
                <>
                  {s.ai_summary}
                  {reviewIsStale(s) && s.reviewed_at != null && (
                    <span className={styles.summaryStale}> · reviewed {relTime(s.reviewed_at)}</span>
                  )}
                </>
              )}
            </MarqueeText>
          )}
          <div className={styles.meta}>
            {/* Favorite star (#508): a small amber ★ leads the meta line on favorited rows
                only (they already pin to the top). Decorative — the on/off state lives on the
                menu's Favorite/Unfavorite item. */}
            {s.sticky && (
              <span className={styles.favStar} aria-hidden="true" title="Favorited">
                <Star size={11} fill="currentColor" />
              </span>
            )}
            <span className={styles.engineTag}>{engineBadge(s.engine)}</span>
            <span className={styles.metaText}>
              {" · "}
              {/* #508: the launch-folder chip was dropped to declutter the narrow sidebar; an
                  entity-assigned row still shows its project chip (name + colour dot when set). */}
              {s.project.kind === "project" && (
                <>
                  <span className={styles.projectChip}>
                    <span className={styles.projectDot} aria-hidden="true" />
                    {s.project.name}
                  </span>
                  {" · "}
                </>
              )}
              {relTime(s.last_mtime)}
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
    setTag,
    setArchived,
    setSticky,
    reviewRow,
    setReviewExcluded,
    setProject,
  } = useSessionsList();
  const rowRefs = useRef(new Map<string, HTMLLIElement>());
  const previousRowTops = useRef(new Map<string, number>());
  const reorderTimers = useRef<number[]>([]);

  const setRowRef = useCallback(
    (id: string) => (el: HTMLLIElement | null) => {
      if (el) rowRefs.current.set(id, el);
      else rowRefs.current.delete(id);
    },
    [],
  );

  const clearReorderTimers = useCallback(() => {
    for (const id of reorderTimers.current) window.clearTimeout(id);
    reorderTimers.current = [];
  }, []);

  useEffect(() => clearReorderTimers, [clearReorderTimers]);

  // Animate rows to their new visual position when the server refresh changes the session order
  // (#607). This is FLIP: remember each row's previous top, let React commit the new order, then
  // temporarily translate moved rows back to their old top and transition transform to zero.
  // Sort/pagination semantics stay owned by useSessionsList and the API; this is only motion.
  useLayoutEffect(() => {
    const nextTops = new Map<string, number>();
    for (const s of sessions) {
      const el = rowRefs.current.get(s.id);
      if (el) nextTops.set(s.id, el.getBoundingClientRect().top);
    }

    const prevTops = previousRowTops.current;
    previousRowTops.current = nextTops;
    if (prevTops.size === 0) return; // initial load
    if (window.matchMedia?.("(prefers-reduced-motion: reduce)").matches) return;

    for (const timer of reorderTimers.current) window.clearTimeout(timer);
    reorderTimers.current = [];

    for (const s of sessions) {
      const el = rowRefs.current.get(s.id);
      const before = prevTops.get(s.id);
      const after = nextTops.get(s.id);
      if (!el || before == null || after == null) continue;
      const delta = before - after;
      if (Math.abs(delta) < ROW_REORDER_MIN_DELTA_PX) continue;

      el.classList.remove(styles.rowReordering);
      el.style.transition = "none";
      el.style.transform = `translateY(${delta}px)`;
      el.dataset.reorderMotion = "true";
      el.classList.add(styles.rowReordering);
      void el.offsetHeight; // force the inverted position before transitioning to zero
      requestAnimationFrame(() => {
        el.style.transition = `transform ${ROW_REORDER_ANIM_MS}ms cubic-bezier(0.2, 0.8, 0.2, 1)`;
        el.style.transform = "translateY(0)";
      });
      const cleanupTimer = window.setTimeout(() => {
        el.classList.remove(styles.rowReordering);
        el.style.removeProperty("transition");
        el.style.removeProperty("transform");
        delete el.dataset.reorderMotion;
      }, ROW_REORDER_ANIM_MS + 100);
      reorderTimers.current.push(cleanupTimer);
    }
  }, [sessions]);

  // The currently open session (#551) — gates the per-row auto-scroll marquee so only the
  // selected row animates. Matches the row link route `/s/:engine/:id`.
  const openMatch = useMatch("/s/:engine/:id");
  const navigate = useNavigate();

  // Archiving the session you're currently viewing must leave its `/s/:engine/:id` route (#631):
  // otherwise the terminal stays mounted, its socket keeps trying to reconnect to a session that
  // is now archived (a background agent would relaunch-loop). navigate("/") unmounts SessionView →
  // Terminal, whose cleanup disposes the socket (`sock.close()`). Only on ARCHIVE (not unarchive)
  // and only when the archived id IS the open route.
  const handleToggleArchive = useCallback(
    async (id: string, currentlyArchived: boolean) => {
      await setArchived(id, currentlyArchived);
      const openId = openMatch
        ? `${openMatch.params.engine}:${openMatch.params.id}`
        : null;
      if (!currentlyArchived && openId === id) navigate("/");
    },
    [setArchived, openMatch, navigate],
  );

  // AI review controls (#356) only appear once the endpoint is configured — an
  // unconfigured install keeps the lean three-button row.
  const aiConfigured = useConfig()?.ai_review?.configured ?? false;

  // Review-now outcome toast (#392): the spinner shows activity but not OUTCOME — success
  // flashes the fresh one-line summary; failure shows the server's sanitized error `detail`
  // (e.g. "review endpoint returned HTTP 502") and lingers longer so it can be read.
  const [reviewToast, setReviewToast] = useState<{ kind: "ok" | "err"; text: string } | null>(
    null,
  );
  useEffect(() => {
    if (!reviewToast) return;
    const ms = reviewToast.kind === "ok" ? TOAST_OK_MS : TOAST_ERR_MS;
    const t = window.setTimeout(() => setReviewToast(null), ms);
    return () => window.clearTimeout(t);
  }, [reviewToast]);
  const reviewNowWithOutcome = useCallback(
    async (id: string) => {
      try {
        const r = await reviewRow(id);
        setReviewToast({ kind: "ok", text: r.ai_summary || "Review complete." });
      } catch (e) {
        // `api.reviewNow` surfaces the server's `detail` (already operator-safe: the
        // backend never embeds keys/headers in it). Anything else gets a generic line.
        const detail = e instanceof ApiError ? e.message : "";
        setReviewToast({ kind: "err", text: `Review failed — ${detail || "unexpected error."}` });
      }
    },
    [reviewRow],
  );

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
              active={
                !!openMatch &&
                openMatch.params.engine === s.engine &&
                openMatch.params.id === s.uuid
              }
              onRename={renameRow}
              onSetTag={setTag}
              onToggleArchive={handleToggleArchive}
              onToggleFavorite={setSticky}
              onReviewNow={aiConfigured ? reviewNowWithOutcome : undefined}
              onToggleReviewExcluded={aiConfigured ? setReviewExcluded : undefined}
              onSetProject={setProject}
              onNavigate={onNavigate}
              rowRef={setRowRef(s.id)}
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
      {/* Always-mounted polite live region (#392): the region exists BEFORE its content
       *  changes, the reliable announcement pattern for assistive tech (Hermes on PR #544
       *  — inserting region + text in one render can fail to announce in some SR/browser
       *  combos). Only the toast CONTENT is conditional; empty, the region collapses to
       *  zero height. e2e role queries elsewhere disambiguate by text (shell.spec). */}
      <div className={styles.toastRegion} role="status" aria-live="polite">
        {reviewToast && (
          <div
            className={`${styles.toast} ${
              reviewToast.kind === "ok" ? styles.toastOk : styles.toastErr
            }`}
          >
            <span className={styles.toastLed} aria-hidden="true" />
            <span className={styles.toastText}>{reviewToast.text}</span>
            <button
              type="button"
              className={styles.toastClose}
              aria-label="Dismiss review result"
              onClick={() => setReviewToast(null)}
            >
              <X size={14} />
            </button>
          </div>
        )}
      </div>
    </div>
  );
}
