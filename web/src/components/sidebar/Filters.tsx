import { ChevronRight } from "lucide-react";
import { useId, useRef, useState, type KeyboardEvent, type ReactNode } from "react";

import {
  scopeOf,
  scopePatch,
  type Filters,
  type SessionScope,
} from "../../hooks/useSessionsList";
import { isClusterId } from "../../lib/clusters";
import type { ProjectRef, SessionClusterFacet, SessionTagFacet } from "../../types/api";
import styles from "./Filters.module.css";

interface Props {
  filters: Filters;
  facets: {
    projects: ProjectRef[];
    engines: string[];
    missions?: { id: string; title: string; count: number }[];
    no_mission?: number;
    clusters?: SessionClusterFacet[];
    tags?: SessionTagFacet[];
  };
  /** The server could not apply a mission filter (#948). */
  missionFilterUnavailable?: boolean;
  onChange: (patch: Partial<Filters>) => void;
  onClear: () => void;
  /** How many sessions peers share with this instance (#2198): the Shared tab's count, never
   *  the number its search matches. The tab is offered while this is above 0, and while the
   *  Shared scope is selected. */
  sharedCount?: number;
  /** The project tree (`ProjectTree`): THE project filter, inside this fold (#2187). */
  children?: ReactNode;
}

/** Device-local: whether the filter block is open. Like the map workspace (`windowStore.ts`), a
 *  layout choice for THIS screen, so it never goes through `/api/prefs` — a phone can keep it
 *  folded while a desktop keeps it open. */
export const FILTERS_OPEN_KEY = "tr-sidebar-filters-open";

function readOpen(): boolean {
  try {
    return localStorage.getItem(FILTERS_OPEN_KEY) !== "0";
  } catch {
    return true;
  }
}

const TAB_LABEL: Record<SessionScope, string> = {
  active: "Active",
  archived: "Archived",
  shared: "Shared",
};

/** A project filter the facets do not name still narrows the list, so it still gets a name: a
 *  saved folder path (the server accepts a legacy cwd as `project`; its facets list entities
 *  only) by its last segment, anything else (a project that has since gone) generically. */
function unlistedProject(id: string): string {
  if (id.startsWith("/")) return id.replace(/\/+$/, "").split("/").pop() || id;
  return "Selected project";
}

/** The scope the tree (or the mission dropdown) narrows the list to, by name: "project",
 *  "project › cluster" or "project › mission". Empty for all sessions (#2187). Never empty while
 *  a scope filter is set, so the folded header cannot hide one. */
function scopeLabel(filters: Filters, facets: Props["facets"]): string {
  const project = filters.project
    ? (facets.projects.find((p) => p.id === filters.project)?.name ??
      unlistedProject(filters.project))
    : "";
  const cluster = filters.cluster ?? "";
  const mission = filters.mission ?? "";
  const leaf = isClusterId(cluster)
    ? (facets.clusters?.find((c) => c.id === cluster)?.title ?? "Cluster")
    : cluster === "none"
      ? "Not clustered"
      : mission === "none"
        ? "Not in a mission"
        : mission
          ? (facets.missions?.find((m) => m.id === mission)?.title ?? "Mission")
          : "";
  return [project, leaf].filter(Boolean).join(" › ");
}

/** What the folded header says is in force, so a collapsed block can never hide a filter that is
 *  quietly narrowing the list. The scope has its own chip; this is Active/Archived/Shared and how
 *  many of search, agent and tag are set — "+N" beside a scope chip, "N filters" without one. In the
 *  Shared scope (#2198) only its own search counts: the local filters are not in force there. */
function summary(filters: Filters, scoped: boolean): string {
  const tab = scopeOf(filters);
  const n = (tab === "shared" ? [filters.sharedQ] : [filters.q, filters.engine, filters.tag]).filter(
    Boolean,
  ).length;
  const parts = [TAB_LABEL[tab]];
  if (n) parts.push(scoped ? `+${n}` : `${n} filter${n === 1 ? "" : "s"}`);
  return parts.join(" · ");
}

/** Search, the agent, mission and tag dropdowns (server facets), Active/Archived/Shared and the project
 *  tree, behind ONE collapsible "Filters" header (#1069 follow-up: on a phone the open block took a
 *  third of the drawer). The tree is the project filter, so there is no project dropdown and no
 *  second fold for it (#2187). Open by default; the operator's choice is remembered per device.
 *  The controls stay MOUNTED while folded (`hidden`), so folding never drops a half-typed search.
 *
 *  **The Shared tab (#2198)** lists what peers share with this instance. It is offered while a
 *  session is shared, and while it is selected. Search is its only filter, with its own value
 *  (`sharedQ`) and its own Clear, which clears that value and nothing else. The agent and mission
 *  dropdowns and the project tree are not shown there: they filter this instance's own sessions,
 *  keep their values, and apply again on Active or Archived. */
export function FiltersBar({
  filters,
  facets,
  onChange,
  onClear,
  missionFilterUnavailable,
  sharedCount = 0,
  children,
}: Props) {
  const mission = filters.mission ?? "";
  const tab = scopeOf(filters);
  const shared = tab === "shared";
  const sharedQ = filters.sharedQ ?? "";
  const tag = filters.tag ?? "";
  const tags = facets.tags ?? [];
  const hasFilter = shared
    ? !!sharedQ
    : !!(filters.q || filters.project || filters.engine || mission || filters.cluster || tag);
  // The project / cluster / mission chip names a filter of the LOCAL list: not in force on Shared.
  const scope = shared ? "" : scopeLabel(filters, facets);
  const tabs: SessionScope[] =
    sharedCount > 0 || shared ? ["active", "archived", "shared"] : ["active", "archived"];
  const tabRefs = useRef<Partial<Record<SessionScope, HTMLButtonElement | null>>>({});
  // Selecting a tab also focuses it. A tab that is offered only while selected (Shared at a
  // count of 0) leaves the DOM when the operator leaves it, and focus must not be lost with it.
  const select = (next: SessionScope) => {
    if (next !== tab) onChange(scopePatch(next));
    tabRefs.current[next]?.focus();
  };
  // The tablist's keys (WAI-ARIA tabs, selection follows focus): arrows wrap, Home and End jump.
  const onTabKey = (e: KeyboardEvent<HTMLButtonElement>) => {
    const i = tabs.indexOf(tab);
    const next =
      e.key === "ArrowRight"
        ? tabs[(i + 1) % tabs.length]
        : e.key === "ArrowLeft"
          ? tabs[(i - 1 + tabs.length) % tabs.length]
          : e.key === "Home"
            ? tabs[0]
            : e.key === "End"
              ? tabs[tabs.length - 1]
              : null;
    if (!next) return;
    e.preventDefault();
    select(next);
  };
  const [open, setOpen] = useState(readOpen);
  const bodyId = useId();
  const toggle = () => {
    const next = !open;
    setOpen(next);
    try {
      localStorage.setItem(FILTERS_OPEN_KEY, next ? "1" : "0");
    } catch {
      /* private mode: the choice lasts for this page only */
    }
  };
  return (
    <div className={styles.bar}>
      <button
        type="button"
        className={styles.toggle}
        aria-expanded={open}
        aria-controls={bodyId}
        onClick={toggle}
        data-testid="filters-toggle"
      >
        <ChevronRight size={14} aria-hidden="true" className={styles.chev} />
        <span>Filters</span>
        {scope ? (
          <small
            className={`${styles.summaryOn} ${styles.scope}`}
            title={scope}
            data-testid="filters-scope"
          >
            {scope}
          </small>
        ) : null}
        <small className={hasFilter && !scope ? styles.summaryOn : undefined}>
          {summary(filters, !!scope)}
        </small>
      </button>
      {/* The fold expands (#1568): `.body` is a one-row grid whose row grows 0fr → 1fr, so the
          height eases on ONE container and `.bodyInner` clips while it does. `hidden` stays the
          state (the controls stay mounted, and leave the tab order while folded). */}
      <div id={bodyId} className={styles.body} hidden={!open}>
        <div className={styles.bodyInner}>
          {/* CLEAR rides the search row (#2187): the search is the one wide field, so the
              dropdown row below keeps room for both dropdowns in a 300px sidebar. */}
          {shared ? (
            // The Shared tab's own search (#2198): its own value, and a Clear that touches only
            // it — never `onClear`, which resets the local list's whole model.
            <div className={styles.searchRow}>
              <input
                className={styles.search}
                type="search"
                placeholder="Search shared sessions…"
                aria-label="Search shared sessions"
                value={sharedQ}
                onChange={(e) => onChange({ sharedQ: e.target.value })}
              />
              {sharedQ && (
                <button
                  type="button"
                  className={styles.clear}
                  onClick={() => onChange({ sharedQ: "" })}
                  aria-label="Clear search"
                >
                  Clear
                </button>
              )}
            </div>
          ) : (
            <div className={styles.searchRow}>
              <input
                className={styles.search}
                type="search"
                placeholder="Search titles…"
                aria-label="Search sessions"
                value={filters.q}
                onChange={(e) => onChange({ q: e.target.value })}
              />
              {hasFilter && (
                <button
                  type="button"
                  className={styles.clear}
                  onClick={onClear}
                  aria-label="Clear filters"
                >
                  Clear
                </button>
              )}
            </div>
          )}
          {/* Agent and mission share one row (#2187). The mission dropdown stays: the tree shows a
              mission under each project it touches, but only this reaches a mission across
              projects, and "Not in a mission". It is offered only when the server could read
              memberships (#948) — a dropdown of "no missions" would be a claim, not a fact. The
              selected value stays listed even when no session in scope carries it any more. */}
          {shared ? null : (
            <div className={styles.selects}>
              {facets.engines.length > 1 && (
                <select
                  aria-label="Filter by agent"
                  value={filters.engine}
                  onChange={(e) => onChange({ engine: e.target.value })}
                >
                  <option value="">All agents</option>
                  {facets.engines.map((e) => (
                    <option key={e} value={e}>
                      {e}
                    </option>
                  ))}
                </select>
              )}
              {facets.missions ? (
                <select
                  aria-label="Filter by mission"
                  value={mission}
                  // A session a mission holds is never in a cluster, so a mission picked here
                  // replaces the cluster rather than intersecting with it (the scope stays true).
                  onChange={(e) => onChange({ mission: e.target.value, cluster: "" })}
                >
                  <option value="">All missions</option>
                  <option value="none">
                    {`Not in a mission${facets.no_mission != null ? ` (${facets.no_mission})` : ""}`}
                  </option>
                  {mission && mission !== "none" && !facets.missions.some((m) => m.id === mission) ? (
                    <option value={mission}>Selected mission</option>
                  ) : null}
                  {facets.missions.map((m) => (
                    <option key={m.id} value={m.id}>
                      {`${m.title} (${m.count})`}
                    </option>
                  ))}
                </select>
              ) : null}
            </div>
          )}
          {/* The tag filter (#2338) gets its own row: a third select in the agent/mission row
              would truncate every label in a 300px sidebar. Offered while a session in scope has
              a tag, and while one is selected; the selected tag stays listed even when no session
              carries it any more, so the control never shows a value it does not offer. An
              option's value is the server's group key, so the stored filter is compared exactly
              and the browser never has to reproduce Python's case folding. */}
          {!shared && (tags.length > 0 || tag) ? (
            <div className={styles.selects}>
              <select
                aria-label="Filter by tag"
                value={tag}
                onChange={(e) => onChange({ tag: e.target.value })}
              >
                <option value="">All tags</option>
                {tag && !tags.some((t) => t.key === tag) ? <option value={tag}>{tag}</option> : null}
                {tags.map((t) => (
                  <option key={t.key} value={t.key}>
                    {`${t.tag} (${t.count})`}
                  </option>
                ))}
              </select>
            </div>
          ) : null}
          {missionFilterUnavailable && !shared ? (
            <div className={styles.note} role="status">
              The mission filter could not be applied — mission memberships are unavailable.
            </div>
          ) : null}
          <div
            className={`${styles.tabs} ${tabs.length === 3 ? styles.three : ""}`}
            role="tablist"
            aria-label="Which sessions"
          >
            {tabs.map((t) => (
              <button
                key={t}
                ref={(el) => {
                  tabRefs.current[t] = el;
                }}
                type="button"
                role="tab"
                aria-selected={t === tab}
                // Roving focus: the selected tab is the tablist's one tab stop.
                tabIndex={t === tab ? 0 : -1}
                className={t === tab ? styles.on : ""}
                onClick={() => select(t)}
                onKeyDown={onTabKey}
                data-testid={`scope-tab-${t}`}
              >
                {TAB_LABEL[t]}
                {t === "shared" && sharedCount > 0 ? (
                  <>
                    {/* A space for the tab's name ("Shared 3"); the flex gap draws the distance. */}{" "}
                    <span className={styles.tabCount} data-testid="shared-count">
                      {sharedCount}
                    </span>
                  </>
                ) : null}
              </button>
            ))}
          </div>
          {/* The project tree filters this instance's own sessions. On Shared it is hidden, not
              unmounted: its open branches are there again on Active or Archived (#2198). */}
          <div hidden={shared}>{children}</div>
        </div>
      </div>
    </div>
  );
}
