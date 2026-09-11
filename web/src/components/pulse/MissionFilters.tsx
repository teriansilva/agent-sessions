import styles from "./mission.module.css";

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
  return (
    <div className={styles.railFilters}>
      <input
        type="search"
        aria-label="Search missions"
        placeholder="Search missions…"
        value={value.q}
        onChange={(e) => onChange({ ...value, q: e.target.value })}
      />
      <label>
        Project
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
      </label>
      <label>
        State
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
      </label>
      {value.q || value.project || value.state ? (
        <button
          type="button"
          onClick={() => onChange({ q: "", project: "", state: "" })}
        >
          Clear mission filters
        </button>
      ) : null}
    </div>
  );
}
