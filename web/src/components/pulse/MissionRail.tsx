/** The rail: every mission, plus the live sessions no mission owns yet (#878).
 *
 * It is drawn with the SESSIONS sidebar's own classes (#948 P2) — the `+ New session` button,
 * the filter bar and its Active | Archived tabs, and the row anatomy (LED, one title line, one
 * mono meta line). The mission rail used to restate every one of those values in its own rules,
 * and restating them is exactly how the two sidebars drifted apart; sharing the classes is what
 * keeps them aligned. Only what a `<button>` brings with it is reset locally.
 *
 * The UNTRACKED group is not a migration ramp that goes away — it is the permanent home for work
 * started from the sidebar. Deleting the card grid without it would orphan every live session
 * that nobody has adopted, which is the one way this phase could lose something real.
 */
import { Plus } from "lucide-react";
import type { ReactNode } from "react";
import { createPortal } from "react-dom";
import { relTime } from "../../lib/format";
import type { MissionListRow, PulseCard } from "../../types/api";
import filterStyles from "../sidebar/Filters.module.css";
import listStyles from "../sidebar/SessionList.module.css";

import styles from "./mission.module.css";

/** The state dot's class. SEMANTIC tokens only — the brand accent never means a state. */
function dotClass(m: MissionListRow): string {
  if (m.needs_you) return styles.dotNeedsYou;
  if (m.state === "running" || m.state === "dispatching")
    return styles.dotRunning;
  if (m.state === "failed") return styles.dotFailed;
  if (m.state === "done" || m.state === "abandoned") return styles.dotDone;
  return styles.dot;
}

function stateLabel(m: MissionListRow): string {
  if (m.needs_you) return "needs you";
  return m.state === "dispatching" ? "starting" : m.state;
}

/** The sentinel id for the UNTRACKED view. A real mission id is `msn_…`, so this cannot collide.
 *
 *  It exists because an untracked session can carry a PENDING DECISION, and before this phase
 *  that decision rode a card with controls. Without somewhere to render it the console would
 *  silently drop decisions for exactly the sessions nobody has organised yet — the opposite of
 *  what MISSION CONTROL is for. Selecting UNTRACKED shows those decisions in the thread, which
 *  is the surface wide enough for them. */
export const UNTRACKED_VIEW = "__untracked__";

export interface MissionRailProps {
  filters?: ReactNode;
  untrackedFilters?: ReactNode;
  loading?: boolean;
  filtered?: boolean;
  projectNames?: Record<string, string>;
  missions: MissionListRow[];
  /** Live sessions with no mission. `PulseCard` is the existing overview row shape. */
  untracked: PulseCard[];
  selectedId: string | null;
  onSelect: (id: string) => void;
  storeError?: string | null;
  /** How many missions exist in the current scope. `missions.length < total` means the rail is
   *  showing a page, not the set — and the operator is told, rather than the remainder simply
   *  not existing. */
  total?: number;
  /** Whether there is anything left to PAGE — from the same cursor the handler stops on, never
   *  from the rendered count (#896 review 18). Deduping drives those apart, and a LOAD MORE
   *  rendered on the rendered count while the handler had already reached the end was a control
   *  that looked like the way to the missing mission and did nothing.
   *
   *  REQUIRED, so there is exactly one rule rather than a fallback that could reintroduce the
   *  rendered-count one. */
  hasMore: boolean;
  /** The list could not be proved complete: it was stitched from several offset pages, or every
   *  row has been consumed and the rail is still short of `total`. An offset append cannot fill
   *  that hole — only a fresh read can — which is why this is not another LOAD MORE. */
  needsReRead?: boolean;
  onReRead?: () => void;
  loadingMore?: boolean;
  onLoadMore?: () => void;
  /** Which scope the rail is listing. Archiving is not deletion — an archived mission keeps its
   *  objectives, its timeline and its decisions — so without a way back to it the console loses
   *  every finished mission the moment it is put away. */
  archived?: boolean;
  onScope?: (archived: boolean) => void;
  /** Start a new mission from the rail (#935). The sessions sidebar leads with "+ New session";
   *  this is its counterpart, and since #948 it is literally the same button class. It does not
   *  create anything by itself — it opens the place where the brief is written. */
  onNewMission?: () => void;
  /** The shell's head-row slot (#948 P2). The counts render there, in the same 38px row the
   *  sessions sidebar uses for its ORDER control. `null` renders an equivalent row in place — the
   *  standalone fallback for tests and for any mount without the shell. */
  headEl?: HTMLElement | null;
}

export function MissionRail({
  missions,
  untracked,
  selectedId,
  onSelect,
  storeError,
  total = 0,
  hasMore,
  needsReRead = false,
  onReRead,
  loadingMore,
  onLoadMore,
  archived = false,
  onScope,
  onNewMission,
  filters,
  untrackedFilters,
  loading = false,
  filtered = false,
  projectNames = {},
  headEl = null,
}: MissionRailProps) {
  // Counted over the LOADED rows, like the rail's own dots — a count of what is on screen.
  const needsYou = missions.filter((m) => m.needs_you).length;
  const counts = (
    <span className="hud-tag" data-testid="rail-counts">
      {loading ? (
        "Loading…"
      ) : storeError && !missions.length ? (
        "Unavailable"
      ) : (
        <>
          <b className="num">{total}</b> {archived ? "archived" : "active"}
          {needsYou ? (
            <>
              {" · "}
              <b className="num">{needsYou}</b> needs you
            </>
          ) : null}
        </>
      )}
    </span>
  );

  const rowClass = (on: boolean) =>
    `${listStyles.row} ${styles.railButton} ${on ? listStyles.active : ""}`;

  return (
    <nav
      className={`${styles.rail} ${selectedId === UNTRACKED_VIEW ? styles.railWithSessions : ""}`}
      aria-label="Missions"
    >
      {headEl ? (
        createPortal(counts, headEl)
      ) : (
        <div className="sidebar-head">
          <span className="hud-tag">Missions</span>
          {counts}
        </div>
      )}
      {storeError ? (
        // A store that would not answer is NOT "you have no missions". Saying the second when
        // the first is true is the lie this notice exists to prevent.
        <div
          className={styles.notice}
          role="status"
          data-testid="rail-store-error"
        >
          <div className={styles.noticeLead}>
            The mission store could not be read.
          </div>
          <div>
            {storeError}.{" "}
            {missions.length
              ? "Showing the last available result."
              : "Missions are unavailable."}{" "}
            Sessions below are unaffected.
            {onReRead ? (
              <button type="button" className={styles.more} onClick={onReRead}>
                Retry missions
              </button>
            ) : null}
          </div>
        </div>
      ) : null}

      {/* The primary action, first — the sessions sidebar's own button. Hidden in the archived
          scope, where the server refuses ordinary mutations anyway and offering a create would
          advertise something the backend will not honour. */}
      {onNewMission && !archived ? (
        <button
          type="button"
          className={`${listStyles.newBtn} ${styles.railButtonReset} shine`}
          onClick={onNewMission}
          data-testid="rail-new-mission"
        >
          <Plus size={16} />
          New mission
        </button>
      ) : null}

      <div className={filterStyles.bar}>
        {filters}
        {/* The scope, as the sessions sidebar's Active | Archived tabs. The two scopes are
            disjoint (the server's `archived` flag partitions the set), which is exactly what a
            tab pair expresses — and `aria-selected` carries it to a screen reader. */}
        {onScope ? (
          <div
            className={filterStyles.tabs}
            role="tablist"
            aria-label="Mission scope"
          >
            <button
              type="button"
              role="tab"
              aria-selected={!archived}
              className={!archived ? filterStyles.on : ""}
              onClick={() => archived && onScope(false)}
              data-testid="rail-scope-active"
            >
              Active
            </button>
            <button
              type="button"
              role="tab"
              aria-selected={archived}
              className={archived ? filterStyles.on : ""}
              onClick={() => !archived && onScope(true)}
              data-testid="rail-scope-archived"
            >
              Archived
            </button>
          </div>
        ) : null}
      </div>

      <div className={styles.railScroll} data-testid="mission-list-scroll">
        {loading ? (
          <div className={listStyles.empty} role="status">
            Loading missions…
          </div>
        ) : null}
        {missions.length === 0 && !storeError && !loading ? (
          <div className={listStyles.empty} data-testid="rail-no-missions">
            {filtered
              ? "No missions match these filters."
              : archived
                ? "Nothing archived yet."
                : "Nothing tracked yet."}
          </div>
        ) : null}
        {missions.map((m) => (
          <div key={m.id} className={listStyles.rowWrap}>
            <button
              type="button"
              className={rowClass(m.id === selectedId)}
              aria-current={m.id === selectedId ? "true" : undefined}
              onClick={() => onSelect(m.id)}
              data-testid="rail-mission"
            >
              <span
                className={`${listStyles.led} ${styles.dot} ${styles.railDot} ${dotClass(m)}`}
                role="img"
                aria-label={m.needs_you ? "Needs you" : m.state}
              />
              <span className={listStyles.body}>
                <span className={`${listStyles.title} ${styles.railText}`}>
                  {m.title}
                </span>
                <span className={listStyles.meta}>
                  <span className={listStyles.engineTag}>{stateLabel(m)}</span>
                  <span className={listStyles.metaText}>
                    {" · "}
                    {m.project_id
                      ? projectNames[m.project_id] ||
                        (m.cwd?.split("/").filter(Boolean).at(-1) ??
                          "Unavailable project")
                      : "No project"}
                    {" · "}
                    {m.session_keys.length}{" "}
                    {m.session_keys.length === 1 ? "session" : "sessions"}
                    {m.archived_at != null ? " · archived" : ""}
                    {" · "}
                    {relTime(m.updated_at)}
                  </span>
                </span>
              </span>
            </button>
          </div>
        ))}

        {needsReRead && onReRead ? (
          <div className={listStyles.more}>
            <button
              type="button"
              onClick={onReRead}
              disabled={loadingMore}
              data-testid="rail-re-read"
            >
              This list was read in pages — re-read it
            </button>
          </div>
        ) : null}
        {hasMore ? (
          <div className={listStyles.more}>
            <button
              type="button"
              onClick={onLoadMore}
              disabled={loadingMore}
              data-testid="rail-load-more"
            >
              {loadingMore
                ? "Loading…"
                : `Load more — ${missions.length} of ${total}`}
            </button>
          </div>
        ) : null}
      </div>
      {untracked.length > 0 ? (
        <div className={listStyles.rowWrap}>
          <button
            type="button"
            className={rowClass(selectedId === UNTRACKED_VIEW)}
            aria-current={selectedId === UNTRACKED_VIEW ? "true" : undefined}
            onClick={() => onSelect(UNTRACKED_VIEW)}
            data-testid="rail-untracked-view"
          >
            <span
              className={`${listStyles.led} ${styles.dot} ${styles.railDot} ${
                untracked.some((c) => c.pending_action)
                  ? styles.dotNeedsYou
                  : untracked.some((c) => c.live)
                    ? styles.dotRunning
                    : styles.dot
              }`}
              aria-hidden="true"
            />
            <span className={listStyles.body}>
              <span className={`${listStyles.title} ${styles.railText}`}>
                Sessions without a mission · {untracked.length}
              </span>
              <span className={listStyles.meta}>
                <span className={listStyles.metaText}>
                  {untracked.filter((c) => c.pending_action).length
                    ? `${untracked.filter((c) => c.pending_action).length} waiting on you`
                    : "live sessions with no mission"}
                </span>
              </span>
            </span>
          </button>
          {/* The sessions themselves are NOT listed here.
              The rail navigates; the pane carries content. Listing every untracked session in
              both put the same title in two places — which is duplication on a desktop and, on a
              phone where the rail is a DRAWER, put half of it somewhere the operator cannot see
              without opening it. One navigation row, and the sessions live in the view. */}
        </div>
      ) : null}
      {selectedId === UNTRACKED_VIEW ? untrackedFilters : null}
    </nav>
  );
}
