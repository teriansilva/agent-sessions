import { ChevronRight } from "lucide-react";
import { useId, useState, type ReactNode } from "react";

import type { Filters } from "../../hooks/useSessionsList";
import { isClusterId } from "../../lib/clusters";
import type { ProjectRef, SessionClusterFacet } from "../../types/api";
import styles from "./Filters.module.css";

interface Props {
  filters: Filters;
  facets: {
    projects: ProjectRef[];
    engines: string[];
    missions?: { id: string; title: string; count: number }[];
    no_mission?: number;
    clusters?: SessionClusterFacet[];
  };
  /** The server could not apply a mission filter (#948). */
  missionFilterUnavailable?: boolean;
  onChange: (patch: Partial<Filters>) => void;
  onClear: () => void;
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
 *  quietly narrowing the list. The scope has its own chip; this is Active/Archived and how many
 *  of search and agent are set — "+N" beside a scope chip, "N filters" without one. */
function summary(filters: Filters, scoped: boolean): string {
  const n = [filters.q, filters.engine].filter(Boolean).length;
  const parts = [filters.archived ? "Archived" : "Active"];
  if (n) parts.push(scoped ? `+${n}` : `${n} filter${n === 1 ? "" : "s"}`);
  return parts.join(" · ");
}

/** Search, the agent and mission dropdowns (server facets), Active/Archived and the project tree,
 *  behind ONE collapsible "Filters" header (#1069 follow-up: on a phone the open block took a
 *  third of the drawer). The tree is the project filter, so there is no project dropdown and no
 *  second fold for it (#2187). Open by default; the operator's choice is remembered per device.
 *  The controls stay MOUNTED while folded (`hidden`), so folding never drops a half-typed search. */
export function FiltersBar({
  filters,
  facets,
  onChange,
  onClear,
  missionFilterUnavailable,
  children,
}: Props) {
  const mission = filters.mission ?? "";
  const hasFilter = !!(
    filters.q ||
    filters.project ||
    filters.engine ||
    mission ||
    filters.cluster
  );
  const scope = scopeLabel(filters, facets);
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
          {/* Agent and mission share one row (#2187). The mission dropdown stays: the tree shows a
              mission under each project it touches, but only this reaches a mission across
              projects, and "Not in a mission". It is offered only when the server could read
              memberships (#948) — a dropdown of "no missions" would be a claim, not a fact. The
              selected value stays listed even when no session in scope carries it any more. */}
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
          {missionFilterUnavailable ? (
            <div className={styles.note} role="status">
              The mission filter could not be applied — mission memberships are unavailable.
            </div>
          ) : null}
          <div className={styles.tabs} role="tablist" aria-label="Archived filter">
            <button
              type="button"
              role="tab"
              aria-selected={!filters.archived}
              className={!filters.archived ? styles.on : ""}
              onClick={() => onChange({ archived: false })}
            >
              Active
            </button>
            <button
              type="button"
              role="tab"
              aria-selected={filters.archived}
              className={filters.archived ? styles.on : ""}
              onClick={() => onChange({ archived: true })}
            >
              Archived
            </button>
          </div>
          {children}
        </div>
      </div>
    </div>
  );
}
