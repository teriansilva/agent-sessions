import { useEffect, useState } from "react";
import { api } from "../../lib/api";
import type {
  ResourceCounter,
  Resources,
  ResourceValues,
} from "../../types/resources";
import styles from "./ResourceLimits.module.css";

const fields: { key: keyof ResourceValues; label: string; help: string }[] = [
  {
    key: "console_tasks",
    label: "Console session task limit",
    help: "Processes and threads share this budget.",
  },
  {
    key: "api_tasks",
    label: "API worker task limit",
    help: "Applies to each new worker generation.",
  },
  {
    key: "library_threads",
    label: "Background library threads",
    help: "Default pool size for supported libraries. Explicit environment overrides are preserved.",
  },
];
const containment = {
  disabled:
    "Console isolation is disabled by the host configuration. This console limit is not enforced. API workers still require containment.",
  unavailable:
    "Console isolation was unavailable at its last launch check. Console launches may use the existing fallback; API workers require containment.",
  unverified:
    "Console isolation has not been verified by a launch in this server process. Running group readings below are observed independently.",
  verified:
    "Console isolation passed its launch check. The readings below show current running groups and inherited limits.",
};
const count = (n: number | null) =>
  n === null ? "unknown" : n.toLocaleString();
function usageText(c: ResourceCounter) {
  return `${count(c.current)} / ${c.unlimited ? "unlimited" : count(c.maximum)} tasks`;
}
type Draft = Record<keyof ResourceValues, string>;
const draftOf = (values: ResourceValues): Draft => ({
  console_tasks: String(values.console_tasks),
  api_tasks: String(values.api_tasks),
  library_threads: String(values.library_threads),
});

export function ResourceLimits() {
  const [data, setData] = useState<Resources | null>(null);
  const [draft, setDraft] = useState<Draft | null>(null);
  const [edited, setEdited] = useState<
    Partial<Record<keyof ResourceValues, boolean>>
  >({});
  const [error, setError] = useState("");
  const [readError, setReadError] = useState(false);
  const [message, setMessage] = useState("");
  const [busy, setBusy] = useState(false);
  const [loading, setLoading] = useState(true);
  const [reload, setReload] = useState(0);
  function refresh() {
    setLoading(true);
    setReload((n) => n + 1);
  }

  useEffect(() => {
    let active = true;
    api
      .resources()
      .then((response) => {
        if (!active) return;
        setData(response);
        // Explicit refresh does not overwrite a form the operator is editing.
        setDraft((previous) => previous ?? draftOf(response.settings.values));
        setError("");
        setReadError(false);
      })
      .catch((e: unknown) => {
        if (active) {
          setReadError(true);
          setError(
            e instanceof Error
              ? e.message
              : "Unable to read resource settings.",
          );
        }
      })
      .finally(() => {
        if (active) setLoading(false);
      });
    return () => {
      active = false;
    };
  }, [reload]);

  const valid =
    data &&
    draft &&
    fields.every(
      ({ key }) =>
        /^\d+$/.test(draft[key]) &&
        Number(draft[key]) >= data.settings.bounds[key].min &&
        Number(draft[key]) <= data.settings.bounds[key].max,
    );
  async function save() {
    if (!valid || !data || !draft || busy) return;
    setBusy(true);
    setError("");
    setMessage("");
    // A displayed fallback is not an edit. In particular, saving an API/library
    // change must preserve an untouched legacy console percentage or infinity.
    const patch: Partial<ResourceValues> = {};
    for (const { key } of fields) {
      if (
        edited[key] &&
        (Number(draft[key]) !== data.settings.values[key] ||
          data.settings.sources[key] !== "settings")
      )
        patch[key] = Number(draft[key]);
    }
    if (Object.keys(patch).length === 0) {
      setBusy(false);
      setEdited({});
      setMessage("No limits were changed.");
      return;
    }
    try {
      const result = await api.setResources(patch);
      setData((previous) =>
        previous ? { ...previous, settings: result.settings } : previous,
      );
      setDraft(draftOf(result.settings.values));
      setEdited({});
      setMessage(
        "Saved for new launches. Running sessions keep their current limits.",
      );
    } catch (e: unknown) {
      setError(
        e instanceof Error ? e.message : "Unable to save resource limits.",
      );
    } finally {
      setBusy(false);
    }
  }

  return (
    <>
      <section className={styles.card} aria-labelledby="resources-heading">
        <h2 id="resources-heading">Resource limits</h2>
        <p className={styles.hint}>
          Give sessions room to work while keeping background workers bounded.
          Changes apply when a new session or API worker starts.
        </p>
        {error && (
          <p className={styles.notice} role="alert">
            {error}
          </p>
        )}
        {!data && loading && <p role="status">Loading resource settings…</p>}
        {!data && !loading && <button onClick={refresh}>Retry</button>}
        {data && draft && (
          <form
            onSubmit={(event) => {
              event.preventDefault();
              void save();
            }}
          >
            <div className={styles.fields}>
              {fields.map(({ key, label, help }) => (
                <label key={key}>
                  <span>{label}</span>
                  <input
                    type="number"
                    inputMode="numeric"
                    min={data.settings.bounds[key].min}
                    max={data.settings.bounds[key].max}
                    step="1"
                    required
                    value={draft[key]}
                    disabled={busy}
                    onChange={(event) => {
                      setDraft({ ...draft, [key]: event.target.value });
                      setEdited((previous) => ({ ...previous, [key]: true }));
                      setMessage("");
                    }}
                  />
                  <small>
                    {help} Recommended:{" "}
                    {data.settings.recommended[key].toLocaleString()}. Range:{" "}
                    {data.settings.bounds[key].min.toLocaleString()}–
                    {data.settings.bounds[key].max.toLocaleString()}.
                  </small>
                </label>
              ))}
            </div>
            {data.settings.sources.console_tasks === "environment" && (
              <div className={styles.notice}>
                <p>
                  The next console launch currently uses TasksMax=
                  {data.settings.console_tasks_max} from the host environment.
                </p>
                <p>
                  {edited.console_tasks
                    ? "Saving will replace this override with the console value above for new launches."
                    : "Edit the console limit or select its value to replace this override. Other changes preserve it."}
                </p>
                <button
                  type="button"
                  disabled={busy}
                  onClick={() => {
                    if (edited.console_tasks)
                      setDraft({
                        ...draft,
                        console_tasks: String(data.settings.values.console_tasks),
                      });
                    setEdited((previous) => ({
                      ...previous,
                      console_tasks: !previous.console_tasks,
                    }));
                    setMessage("");
                  }}
                >
                  {edited.console_tasks
                    ? "Keep host override"
                    : "Use console value above"}
                </button>
              </div>
            )}
            {data.settings.notice && (
              <p className={styles.notice}>{data.settings.notice}</p>
            )}
            {!!data.settings.library_overrides.length && (
              <details>
                <summary>
                  Library environment overrides (
                  {data.settings.library_overrides.length})
                </summary>
                <p className={styles.hint}>
                  These explicit values take precedence over the background
                  library default. A child tool may also set its own values.
                </p>
                <ul>
                  {data.settings.library_overrides.map((override) => (
                    <li key={override.name}>
                      <code>{override.name}</code>: {override.value}
                    </li>
                  ))}
                </ul>
              </details>
            )}
            <div className={styles.actions}>
              <button
                className={styles.primary}
                type="submit"
                disabled={!valid || busy || loading}
              >
                {busy ? "Saving…" : "Save limits"}
              </button>
              <button
                type="button"
                disabled={busy}
                onClick={() => {
                  setDraft(draftOf(data.settings.recommended));
                  setEdited({
                    console_tasks: true,
                    api_tasks: true,
                    library_threads: true,
                  });
                  setMessage(
                    "Recommended values selected. Save to apply them to new launches.",
                  );
                }}
              >
                Restore recommended
              </button>
            </div>
            <p className={styles.hint}>
              Running sessions keep the limits they started with. Saving does
              not restart them. Library threads are defaults; task limits
              provide the enforced boundary where containment is available.
            </p>
            {message && <p role="status">{message}</p>}
          </form>
        )}
      </section>
      {data && (
        <section
          className={styles.card}
          aria-labelledby="resource-usage-heading"
        >
          <div className={styles.heading}>
            <h2 id="resource-usage-heading">Running resource usage</h2>
            <button disabled={loading || busy} onClick={refresh}>
              {loading ? "Refreshing…" : "Refresh readings"}
            </button>
          </div>
          <p className={styles.hint}>
            {containment[data.usage.console_containment]}
          </p>
          <p className={styles.hint}>
            A shared server may host several conversations. Stored session
            history does not consume this task budget. Parent budgets are shared
            with other groups.
          </p>
          <p className={styles.hint}>
            {data.usage.groups.length} groups observed at{" "}
            {new Date(data.usage.observed_at * 1000).toLocaleTimeString()}.
            Blocked starts are cumulative for each group’s lifetime.
          </p>
          {readError && (
            <p className={styles.notice}>
              Refresh failed. The previous readings above and below may be
              stale.
            </p>
          )}
          {data.usage.error && (
            <p className={styles.notice}>{data.usage.error}</p>
          )}
          {data.usage.truncated && (
            <p className={styles.notice}>
              The observation limit was reached. This list is incomplete.
            </p>
          )}
          {!data.usage.error && !data.usage.groups.length && (
            <p>
              No BattleLab resource groups were found in the observed user
              scope.
            </p>
          )}
          {data.usage.groups.map((group) => (
            <article
              className={styles.group}
              key={group.unit}
              data-severity={group.severity}
            >
              <div className={styles.heading}>
                <strong>
                  {group.kind === "api" ? "API worker" : "Console group"}
                </strong>
                <span>
                  {group.severity === "normal"
                    ? "Within observed budgets"
                    : group.severity === "unknown"
                      ? "Usage unknown"
                      : group.severity === "critical"
                        ? "Critical task pressure"
                        : "High task pressure"}
                </span>
              </div>
              <code className={styles.unit}>{group.unit}</code>
              <p>
                {usageText(group.own)} · {count(group.own.denied)} blocked
                starts recorded
              </p>
              <p>
                {group.headroom === null
                  ? "Available task slots unknown"
                  : `${count(group.headroom)} task slots available across own and parent budgets`}
              </p>
              {group.incomplete && (
                <p className={styles.notice}>
                  Some counters are unavailable. Missing readings do not
                  establish healthy capacity.
                </p>
              )}
              <details>
                <summary>Inherited limits</summary>
                <ul>
                  {group.ancestors.map((ancestor) => (
                    <li key={ancestor.group}>
                      <code>{ancestor.group}</code>
                      <br />
                      {usageText(ancestor)} · {count(ancestor.denied)} blocked
                      starts recorded
                    </li>
                  ))}
                </ul>
              </details>
            </article>
          ))}
        </section>
      )}
    </>
  );
}
