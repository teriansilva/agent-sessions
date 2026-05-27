import { useOverviewPrefs } from "../../app/overviewPrefs";
import type { Filters } from "../../hooks/useSessionsList";
import { displayProjectName } from "../../lib/format";
import styles from "./Filters.module.css";

interface Props {
  filters: Filters;
  facets: { projects: string[]; engines: string[] };
  onChange: (patch: Partial<Filters>) => void;
  onClear: () => void;
}

/** Search + project/agent dropdowns (server facets) + active/archived tabs. */
export function FiltersBar({ filters, facets, onChange, onClear }: Props) {
  const { projectNames } = useOverviewPrefs();
  const hasFilter = !!(filters.q || filters.project || filters.engine);
  return (
    <div className={styles.bar}>
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
            <option key={p} value={p}>
              {displayProjectName(p, projectNames)}
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
          <button type="button" className={styles.clear} onClick={onClear} aria-label="Clear filters">
            Clear
          </button>
        )}
      </div>
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
  );
}
