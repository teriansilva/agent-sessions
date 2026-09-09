/** The rail: every mission, plus the live sessions no mission owns yet (#878).
 *
 * The UNTRACKED group is not a migration ramp that goes away — it is the permanent home for work
 * started from the sidebar. Deleting the card grid without it would orphan every live session
 * that nobody has adopted, which is the one way this phase could lose something real.
 */
import type { MissionListRow, PulseCard } from "../../types/api";

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
  return m.state;
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
  /** Start a new mission from the rail (#935).
   *
   *  The sessions sidebar leads with "+ New session"; this is its counterpart, so the two
   *  sections offer the same shape of action rather than one of them hiding its primary verb in
   *  the thread. It does not create anything by itself — it puts the composer into NEW MISSION
   *  mode and focuses it, which is where the name and the brief are written. */
  onNewMission?: () => void;
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
}: MissionRailProps) {
  return (
    <nav className={styles.rail} aria-label="Missions">
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
            {storeError}. Missions are not listed; live sessions below are
            unaffected.
          </div>
        </div>
      ) : null}

      {/* The primary action, first — the same place the sessions sidebar puts "+ New session".
          Hidden in the archived scope, where the server refuses ordinary mutations anyway and
          offering a create would advertise something the backend will not honour. */}
      {onNewMission && !archived ? (
        <button
          type="button"
          className={styles.newMission}
          onClick={onNewMission}
          data-testid="rail-new-mission"
        >
          + New mission
        </button>
      ) : null}

      <div className={styles.railGroup}>
        <span>{archived ? "Archived" : "Missions"}</span>
        {/* A scope SWITCH, not a filter chip: the two scopes are disjoint (the server's
            `archived` flag partitions the set), so the rail shows one or the other and says
            which. `aria-pressed` carries the state to a screen reader, which a styled class
            alone does not. */}
        {onScope ? (
          <button
            type="button"
            className={styles.scope}
            aria-pressed={archived}
            onClick={() => onScope(!archived)}
            data-testid="rail-scope"
          >
            {archived ? "Show active" : "Show archived"}
          </button>
        ) : null}
      </div>
      {missions.length === 0 && !storeError ? (
        <div className={styles.empty} data-testid="rail-no-missions">
          {archived ? "Nothing archived yet." : "Nothing tracked yet."}
        </div>
      ) : null}
      {missions.map((m) => (
        <button
          key={m.id}
          type="button"
          className={`${styles.railRow} ${m.id === selectedId ? styles.railRowOn : ""}`}
          aria-current={m.id === selectedId ? "true" : undefined}
          onClick={() => onSelect(m.id)}
          data-testid="rail-mission"
        >
          <span
            className={`${styles.dot} ${dotClass(m)}`}
            role="img"
            aria-label={m.needs_you ? "Needs you" : m.state}
          />
          <span>
            <span className={styles.railTitle}>{m.title}</span>
            <span className={styles.railMeta}>
              {m.project_id || "no project"} · {m.session_keys.length}{" "}
              {m.session_keys.length === 1 ? "session" : "sessions"} ·{" "}
              {stateLabel(m)}
            </span>
          </span>
        </button>
      ))}

      {needsReRead && onReRead ? (
        <button
          type="button"
          className={styles.more}
          onClick={onReRead}
          disabled={loadingMore}
          data-testid="rail-re-read"
        >
          This list was read in pages — re-read it
        </button>
      ) : null}
      {hasMore ? (
        <button
          type="button"
          className={styles.more}
          onClick={onLoadMore}
          disabled={loadingMore}
          data-testid="rail-load-more"
        >
          {loadingMore
            ? "Loading…"
            : `Load more — ${missions.length} of ${total}`}
        </button>
      ) : null}

      {untracked.length > 0 ? (
        <>
          <button
            type="button"
            className={`${styles.railRow} ${
              selectedId === UNTRACKED_VIEW ? styles.railRowOn : ""
            }`}
            aria-current={selectedId === UNTRACKED_VIEW ? "true" : undefined}
            onClick={() => onSelect(UNTRACKED_VIEW)}
            data-testid="rail-untracked-view"
          >
            <span
              className={`${styles.dot} ${
                untracked.some((c) => c.pending_action)
                  ? styles.dotNeedsYou
                  : untracked.some((c) => c.live)
                    ? styles.dotRunning
                    : styles.dot
              }`}
              aria-hidden="true"
            />
            <span>
              <span className={styles.railTitle}>
                Untracked · {untracked.length}
              </span>
              <span className={styles.railMeta}>
                {untracked.filter((c) => c.pending_action).length
                  ? `${untracked.filter((c) => c.pending_action).length} waiting on you`
                  : "live sessions with no mission"}
              </span>
            </span>
          </button>
          {/* The sessions themselves are NOT listed here.
              The rail navigates; the pane carries content. Listing every untracked session in
              both put the same title in two places — which is duplication on a desktop and, on a
              phone where the rail is a DRAWER, put half of it somewhere the operator cannot see
              without opening it. One navigation row, and the sessions live in the view. */}
        </>
      ) : null}
    </nav>
  );
}
