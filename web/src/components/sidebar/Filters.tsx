import type { Filters } from "../../hooks/useSessionsList";
import type { ProjectRef } from "../../types/api";
import styles from "./Filters.module.css";

interface Props {
  filters: Filters;
  facets: { projects: ProjectRef[]; engines: string[] };
  onChange: (patch: Partial<Filters>) => void;
  onClear: () => void;
}

/** Search + project/agent dropdowns (server facets) + active/archived tabs. */
export function FiltersBar({ filters, facets, onChange, onClear }: Props) {
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
