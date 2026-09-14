import filterStyles from "../sidebar/Filters.module.css";

/** The mission rail's search + project/state filters, drawn with the SESSIONS filter bar's own
 *  classes (#948 P2): one search input, then the two selects side by side with Clear beside
 *  them. The visible labels went with the old stacked layout; every control keeps its
 *  `aria-label`, which is what a screen reader and the specs address them by. The bar wrapper
 *  itself is the rail's, because the Active | Archived tabs live in the same bar. */

export interface MissionFiltersValue {
  q: string;
  project: string;
  state: string;
}
export function MissionFilters({
  value,
  onChange,
  facets,
  projectNames,
}: {
  value: MissionFiltersValue;
  onChange: (value: MissionFiltersValue) => void;
  facets: { projects: string[]; states: string[] };
  projectNames: Record<string, string>;
}) {
  const active = !!(value.q || value.project || value.state);
  return (
    <>
      <input
        className={filterStyles.search}
        type="search"
        aria-label="Search missions"
        placeholder="Search missions…"
        value={value.q}
        onChange={(e) => onChange({ ...value, q: e.target.value })}
      />
      <div className={filterStyles.selects}>
        <select
          aria-label="Filter missions by project"
          value={value.project}
          onChange={(e) => onChange({ ...value, project: e.target.value })}
        >
          <option value="">All projects</option>
          {[
            ...new Set([
              ...facets.projects,
              ...(value.project ? [value.project] : []),
            ]),
          ].map((id) => (
            <option value={id} key={id}>
              {projectNames[id] || "Unavailable project"}
            </option>
          ))}
        </select>
        <select
          aria-label="Filter missions by state"
          value={value.state}
          onChange={(e) => onChange({ ...value, state: e.target.value })}
        >
          <option value="">All states</option>
          {[
            ...new Set([
              ...facets.states,
              ...(value.state ? [value.state] : []),
            ]),
          ].map((s) => (
            <option value={s} key={s}>
              {s === "dispatching"
                ? "Starting"
                : s.charAt(0).toUpperCase() + s.slice(1)}
            </option>
          ))}
        </select>
        {active ? (
          <button
            type="button"
            className={filterStyles.clear}
            onClick={() => onChange({ q: "", project: "", state: "" })}
            aria-label="Clear mission filters"
          >
            Clear
          </button>
        ) : null}
      </div>
    </>
  );
}
