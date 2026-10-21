import { useEffect, useState } from "react";
import { api } from "../../lib/api";
import type { Resources, ResourceValues } from "../../types/resources";
import styles from "./ResourceLimits.module.css";

const fields: { key: keyof ResourceValues; label: string; help: string }[] = [
  {
    key: "api_memory_gib",
    label: "API session memory limit (GiB)",
    help: "Combined allowance for the agent and all tools it runs.",
  },
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
type Draft = Record<keyof ResourceValues, string>;
const draftOf = (values: ResourceValues): Draft => ({
  console_tasks: String(values.console_tasks),
  api_tasks: String(values.api_tasks),
  library_threads: String(values.library_threads),
  api_memory_gib: String(values.api_memory_gib),
});

export function ResourceLimits() {
  const [data, setData] = useState<Pick<Resources, "settings"> | null>(null);
  const [draft, setDraft] = useState<Draft | null>(null);
  const [edited, setEdited] = useState<
    Partial<Record<keyof ResourceValues, boolean>>
  >({});
  const [error, setError] = useState("");
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
      })
      .catch((e: unknown) => {
        if (active) {
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

  function renderField(field: (typeof fields)[number]) {
    if (!data || !draft) return null;
    const { key, label, help } = field;
    return (
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
          {help} Recommended: {data.settings.recommended[key].toLocaleString()}.
          Range: {data.settings.bounds[key].min.toLocaleString()}–
          {data.settings.bounds[key].max.toLocaleString()}.
        </small>
      </label>
    );
  }

  return (
    <>
      <section className={styles.card} aria-labelledby="resources-heading">
        <h2 id="resources-heading">Memory limit</h2>
        <p className={styles.hint}>
          Choose how much memory each API session can use, including the tools
          it runs.
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
            <div className={styles.memory}>
              {fields
                .filter(({ key }) => key === "api_memory_gib")
                .map(renderField)}
            </div>
            <p className={styles.hint}>
              Reaching this limit stops the session and its tools. Choose an
              allowance that fits your workload and server. A lower host limit
              still takes precedence.
            </p>
            <details className={styles.advanced}>
              <summary>Advanced process and thread limits</summary>
              <div className={styles.fields}>
                {fields
                  .filter(({ key }) => key !== "api_memory_gib")
                  .map(renderField)}
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
                          console_tasks: String(
                            data.settings.values.console_tasks,
                          ),
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
            </details>
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
                    api_memory_gib: true,
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
              not restart them.
            </p>
            {message && <p role="status">{message}</p>}
          </form>
        )}
      </section>
    </>
  );
}
