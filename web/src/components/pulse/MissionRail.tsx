/** The rail: every mission (#878).
 *
 * It is drawn with the SESSIONS sidebar's own classes (#948 P2) — the `+ New session` button,
 * the filter bar and its Active | Archived tabs, and the row anatomy (LED, one title line, one
 * mono meta line). The mission rail used to restate every one of those values in its own rules,
 * and restating them is exactly how the two sidebars drifted apart; sharing the classes is what
 * keeps them aligned. Only what a `<button>` brings with it is reset locally.
 */
import { Plus } from "lucide-react";
import type { ReactNode } from "react";
import { createPortal } from "react-dom";
import { useEffect } from "react";
import { relTime } from "../../lib/format";
import {
  missionOriginKey,
  noteKeys,
  originOf,
  useAutomationOrigins,
} from "../../app/automationOrigins";
import { OriginBadge } from "../automations/OriginBadge";
import type { MissionListRow } from "../../types/api";
import filterStyles from "../sidebar/Filters.module.css";
import listStyles from "../sidebar/SessionList.module.css";

import styles from "./mission.module.css";
import { missionDotClass, missionStateLabel } from "./missionState";

/** The state dot's class. Needs-you outranks the state; the state's own colour is the one the
 *  header's chip uses too (#967). */
function dotClass(m: MissionListRow): string {
  if (m.needs_you) return styles.dotNeedsYou;
  return missionDotClass(m.state);
}

function stateLabel(m: MissionListRow): string {
  if (m.needs_you) return "needs you";
  return missionStateLabel(m.state);
}

export interface MissionRailProps {
  filters?: ReactNode;
  loading?: boolean;
  filtered?: boolean;
  projectNames?: Record<string, string>;
  missions: MissionListRow[];
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
  /** The shell's footer slot (#948 §1): mission and held-session telemetry. `null` renders
   *  nothing — the footer belongs to the shell, and a standalone mount has none. */
  footEl?: HTMLElement | null;
}

export function MissionRail({
  missions,
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
  loading = false,
  filtered = false,
  projectNames = {},
  headEl = null,
  footEl = null,
}: MissionRailProps) {
  // Missions an automation started carry its badge (#1201); a mission the rail has not shown
  // before asks the origins map again.
  const origins = useAutomationOrigins();
  const idSig = missions.map((m) => missionOriginKey(m.id)).join("\n");
  useEffect(() => {
    noteKeys(idSig ? idSig.split("\n") : []);
  }, [idSig]);
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

  // Held sessions, counted the same way: distinct keys across the LOADED rows. With more pages to
  // load that is a floor, so it says `+` rather than presenting a partial sum as the whole.
  const held = new Set(missions.flatMap((m) => m.session_keys ?? [])).size;
  const foot = (
    <span className="hud-tag" data-testid="rail-foot">
      <b className="num">{total}</b> MISSIONS ·{" "}
      <b className="num">
        {held}
        {hasMore ? "+" : ""}
      </b>{" "}
      HELD
    </span>
  );

  const rowClass = (on: boolean) =>
    `${listStyles.row} ${styles.railButton} ${on ? listStyles.active : ""}`;

  return (
    <nav
      className={styles.rail}
      aria-label="Missions"
    >
      {footEl && !loading && !(storeError && !missions.length)
        ? createPortal(foot, footEl)
        : null}
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
              : "Missions are unavailable."}
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
                  {originOf(origins, missionOriginKey(m.id)) ? (
                    <OriginBadge origin={originOf(origins, missionOriginKey(m.id))!} />
                  ) : null}
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
    </nav>
  );
}
