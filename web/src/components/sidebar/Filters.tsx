import { ChevronRight } from "lucide-react";
import { useId, useState } from "react";

import type { Filters } from "../../hooks/useSessionsList";
import type { ProjectRef } from "../../types/api";
import styles from "./Filters.module.css";

interface Props {
  filters: Filters;
  facets: {
    projects: ProjectRef[];
    engines: string[];
    missions?: { id: string; title: string; count: number }[];
    no_mission?: number;
  };
  /** The server could not apply a mission filter (#948). */
  missionFilterUnavailable?: boolean;
  onChange: (patch: Partial<Filters>) => void;
  onClear: () => void;
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

/** What the folded header says is in force, so a collapsed block can never hide a filter that is
 *  quietly narrowing the list. */
function summary(filters: Filters): string {
  const n = [filters.q, filters.project, filters.engine, filters.mission].filter(
    Boolean,
  ).length;
  const parts = [filters.archived ? "Archived" : "Active"];
  if (n) parts.push(`${n} filter${n === 1 ? "" : "s"}`);
  return parts.join(" · ");
}

/** Search + project/agent dropdowns (server facets) + active/archived tabs, behind one
 *  collapsible "Filters" header (#1069 follow-up: on a phone the open block took a third of the
 *  drawer). Open by default; the operator's choice is remembered per device. The controls stay
 *  MOUNTED while folded (`hidden`), so folding never drops a half-typed search. */
export function FiltersBar({
  filters,
  facets,
  onChange,
  onClear,
  missionFilterUnavailable,
}: Props) {
  const mission = filters.mission ?? "";
  const hasFilter = !!(filters.q || filters.project || filters.engine || mission);
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
        <small className={hasFilter ? styles.summaryOn : undefined}>
          {summary(filters)}
        </small>
      </button>
      <div id={bodyId} className={styles.body} hidden={!open}>
        <input
          className={styles.search}
          type="search"
          placeholder="Search titles…"
          aria-label="Search sessions"
          value={filters.q}
          onChange={(e) => onChange({ q: e.target.value })}
        />
        <div className={styles.selects}>
          <select
            aria-label="Filter by project"
            value={filters.project}
            onChange={(e) => onChange({ project: e.target.value })}
          >
            <option value="">All projects</option>
            {facets.projects.map((p) => (
              // The dropdown lists PROJECT ENTITIES (#445): the user's projects (incl. empty
              // ones) plus the synthetic "Default" catch-all — never folder paths. A server-side
              // member count (#361 Phase 3) renders as "Name (N)"; older servers omit it.
              <option key={p.id} value={p.id}>
                {p.name + (p.count != null ? ` (${p.count})` : "")}
              </option>
            ))}
          </select>
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
        {/* Filter by mission (#948). Offered only when the server could read memberships — the facet
            is absent otherwise, and a dropdown of "no missions" would be a claim, not a fact. The
            selected value stays listed even when no session in scope carries it any more. */}
        {facets.missions ? (
          <div className={styles.selects}>
            <select
              aria-label="Filter by mission"
              value={mission}
              onChange={(e) => onChange({ mission: e.target.value })}
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
          </div>
        ) : null}
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
      </div>
    </div>
  );
}
