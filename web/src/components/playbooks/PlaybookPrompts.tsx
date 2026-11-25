/** Direct, retained instructions. Import and comparison only change the unsaved bundle. */
import { useEffect, useId, useLayoutEffect, useRef, useState } from "react";
import { Link } from "react-router-dom";
import { api } from "../../lib/api";
import type { Template } from "../../types/api";
import type { DirectPrompt, PromptComparison, Variable } from "./playbookDraft";
import { freshId } from "./playbookDraft";
import { PlaybookDialog } from "./PlaybookDialog";
import { useFieldError } from "./playbookValidation";
import { errorText } from "./usePlaybooks";
import buttons from "../ui/actionButton.module.css";
import styles from "./playbookPrompts.module.css";

function PromptField({
  label,
  value,
  field,
  onChange,
  multiline = false,
}: {
  label: string;
  value: string;
  field: string;
  onChange: (value: string) => void;
  multiline?: boolean;
}) {
  const id = useId();
  const error = useFieldError(field);
  const props = {
    id,
    value,
    "data-field": field,
    "aria-invalid": !!error,
    "aria-describedby": error ? `${id}-error` : undefined,
    onChange: (
      event: React.ChangeEvent<HTMLInputElement | HTMLTextAreaElement>,
    ) => onChange(event.target.value),
  };
  return (
    <div className={styles.field}>
      <label htmlFor={id}>{label}</label>
      {multiline ? <textarea {...props} rows={5} /> : <input {...props} />}
      {error && (
        <p id={`${id}-error`} className={styles.error}>
          {error}
        </p>
      )}
    </div>
  );
}

type Dialog =
  | { kind: "import"; replace?: string }
  | { kind: "compare"; id: string; result?: PromptComparison }
  | { kind: "remove"; id: string };

export function PlaybookPrompts({
  prompts,
  variables,
  limit,
  field,
  onChange,
}: {
  prompts: DirectPrompt[];
  variables: Variable[];
  limit: number;
  field: string;
  onChange: (prompts: DirectPrompt[]) => void;
}) {
  const [dialog, setDialog] = useState<Dialog | null>(null);
  const [templates, setTemplates] = useState<Template[]>([]);
  const [selected, setSelected] = useState("");
  const [bindings, setBindings] = useState<Record<string, string>>({});
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState("");
  const [notice, setNotice] = useState("");
  const inputId = useId();
  const busyRef = useRef(false);
  const epoch = useRef(0);
  const [trigger, setTrigger] = useState<HTMLElement | null>(null);
  const latest = useRef({ prompts, variables, onChange });
  useLayoutEffect(() => {
    latest.current = { prompts, variables, onChange };
  });
  useEffect(
    () => () => {
      epoch.current++;
    },
    [],
  );
  const template = templates.find((t) => t.id === selected);
  const nonSecret = variables.filter((v) => v.kind !== "secret");
  const signature = () =>
    JSON.stringify([latest.current.prompts, latest.current.variables]);
  const select = (id: string) => {
    setSelected(id);
    setBindings({});
    setError("");
  };
  function close() {
    epoch.current++;
    busyRef.current = false;
    setBusy(false);
    setDialog(null);
    setError("");
  }
  async function request<T>(run: () => Promise<T>, apply: (result: T) => void) {
    if (busyRef.current) return;
    const mine = ++epoch.current,
      expected = signature();
    busyRef.current = true;
    setBusy(true);
    setError("");
    try {
      const result = await run();
      if (mine !== epoch.current) return;
      if (expected !== signature()) {
        setError(
          "The draft changed while this request was running. Try again to use the current draft.",
        );
        return;
      }
      apply(result);
    } catch (cause) {
      if (mine === epoch.current) setError(errorText(cause));
    } finally {
      if (mine === epoch.current) {
        busyRef.current = false;
        setBusy(false);
      }
    }
  }
  function openImport(button: HTMLElement, replace?: string) {
    if (!replace || !dialog) setTrigger(button);
    setDialog({ kind: "import", replace });
    setTemplates([]);
    setSelected("");
    setBindings({});
    void request(api.templates, (result) => {
      setTemplates(result.templates);
      const source = latest.current.prompts.find((p) => p.id === replace)
        ?.source?.id;
      if (source && result.templates.some((t) => t.id === source))
        setSelected(source);
    });
  }
  function compare(button: HTMLElement, prompt: DirectPrompt) {
    setTrigger(button);
    setDialog({ kind: "compare", id: prompt.id });
    void request(
      () => api.compareStepPrompt(prompt, variables),
      (result) => setDialog({ kind: "compare", id: prompt.id, result }),
    );
  }
  function importTemplate() {
    if (!template || template.fields.some((f) => !bindings[f.name])) {
      setError(
        "Choose a template and map each field to a declared workflow input.",
      );
      return;
    }
    const replace = dialog?.kind === "import" ? dialog.replace : undefined;
    void request(
      () =>
        api.importStepPrompt(
          template.id,
          template.updated_at,
          bindings,
          variables,
        ),
      (result) => {
        const current = latest.current;
        current.onChange(
          replace
            ? current.prompts.map((p) =>
                p.id === replace ? { ...result.prompt, id: p.id } : p,
              )
            : [...current.prompts, result.prompt],
        );
        setNotice(
          "Template copied into this draft. Save the playbook to keep it.",
        );
        close();
      },
    );
  }
  function confirm() {
    if (!dialog) return;
    if (dialog.kind === "import") {
      importTemplate();
      return;
    }
    if (dialog.kind === "remove") {
      onChange(prompts.filter((p) => p.id !== dialog.id));
      close();
      return;
    }
    const result = dialog.result;
    if (result?.state === "changed" && result.candidate) {
      onChange(
        prompts.map((p) => (p.id === dialog.id ? result.candidate! : p)),
      );
      setNotice(
        "Source update adopted into this draft. Save the playbook to keep it.",
      );
    }
    close();
  }
  function move(index: number, direction: -1 | 1) {
    const next = [...prompts];
    [next[index], next[index + direction]] = [
      next[index + direction],
      next[index],
    ];
    onChange(next);
  }
  const comparison = dialog?.kind === "compare" ? dialog.result : undefined;
  return (
    <div className={styles.prompts} data-field={field} tabIndex={-1}>
      <p>
        Prompts follow the step instructions in the order shown. Importing,
        editing and saving never send them to an agent.
      </p>
      {notice && <p role="status">{notice}</p>}
      {prompts.map((prompt, index) => (
        <section
          key={prompt.id}
          aria-label={`Prompt ${index + 1}`}
          className={styles.prompt}
        >
          <h4>
            Prompt {index + 1} · {prompt.source ? "Template copy" : "Manual"}
          </h4>
          <PromptField
            label={`Prompt ${index + 1} title`}
            value={prompt.title}
            field={`${field}[${index}].title`}
            onChange={(title) =>
              onChange(
                prompts.map((p) => (p.id === prompt.id ? { ...p, title } : p)),
              )
            }
          />
          <PromptField
            label={`Prompt ${index + 1} instructions`}
            value={prompt.body}
            multiline
            field={`${field}[${index}].body`}
            onChange={(body) =>
              onChange(
                prompts.map((p) => (p.id === prompt.id ? { ...p, body } : p)),
              )
            }
          />
          {prompt.source && (
            <>
              <p>
                Copied from {prompt.source.template.name}. Editing changes this
                workflow copy.
              </p>
              <details data-field={`${field}[${index}].source`} tabIndex={-1}>
                <summary>Retained source and input mappings</summary>
                <p>
                  Source revision{" "}
                  <code>{prompt.source.revision.slice(0, 12)}</code>
                </p>
                <pre>{prompt.source.template.body}</pre>
                <ul>
                  {Object.entries(prompt.source.bindings).map(
                    ([name, alias]) => (
                      <li
                        key={name}
                        data-field={`${field}[${index}].source.bindings.${name}`}
                        tabIndex={-1}
                      >
                        {name} → {alias}
                      </li>
                    ),
                  )}
                </ul>
              </details>
              <button
                type="button"
                className={buttons.ghost}
                onClick={(e) => compare(e.currentTarget, prompt)}
              >
                Compare source for prompt {index + 1}
              </button>
              <button
                type="button"
                className={buttons.ghost}
                onClick={(e) => openImport(e.currentTarget, prompt.id)}
              >
                Remap inputs for prompt {index + 1}
              </button>
            </>
          )}
          <div className={styles.actions}>
            {([-1, 1] as const).map((direction) => (
              <button
                type="button"
                key={direction}
                className={buttons.ghost}
                disabled={
                  index + direction < 0 || index + direction >= prompts.length
                }
                onClick={() => move(index, direction)}
              >
                Move prompt {index + 1} {direction < 0 ? "up" : "down"}
              </button>
            ))}
            <button
              type="button"
              className={buttons.ghost}
              onClick={(e) => {
                setTrigger(e.currentTarget);
                setDialog({ kind: "remove", id: prompt.id });
              }}
            >
              Remove prompt {index + 1}
            </button>
          </div>
        </section>
      ))}
      <div className={styles.actions}>
        <button
          type="button"
          className={buttons.ghost}
          disabled={prompts.length >= limit}
          onClick={() =>
            onChange([
              ...prompts,
              { id: freshId("prompt"), title: "Instructions", body: "" },
            ])
          }
        >
          Add manual prompt
        </button>
        <button
          type="button"
          className={buttons.ghost}
          disabled={prompts.length >= limit}
          onClick={(e) => openImport(e.currentTarget)}
        >
          Import template
        </button>
      </div>
      {!limit && <p>Prompt authoring is unavailable on this server.</p>}
      {!!limit && (
        <p>
          Adding a prompt upgrades this draft to the prompt format when you
          save. Older BattleLab versions cannot edit that format.
        </p>
      )}
      {dialog && (
        <PlaybookDialog
          tag="Instructions"
          title={
            dialog.kind === "import"
              ? "Import template"
              : dialog.kind === "remove"
                ? "Remove prompt?"
                : "Compare template source"
          }
          busy={busy}
          returnFocusTo={trigger}
          onCancel={close}
          onConfirm={confirm}
          confirmLabel={
            dialog.kind === "import"
              ? "Copy into draft"
              : dialog.kind === "remove"
                ? "Remove prompt"
                : comparison?.candidate
                  ? "Adopt source update"
                  : "Keep workflow copy"
          }
        >
          <div className={styles.prompts}>
            {error && (
              <p role="alert" className={styles.error}>
                {error}
              </p>
            )}
            {busy && <p role="status">Loading template…</p>}
            {dialog.kind === "remove" && (
              <p>
                Remove this prompt from the draft? Its library template and
                completion rules remain available.
              </p>
            )}
            {dialog.kind === "import" && (
              <>
                <p>
                  Choose reusable instructions, then map their fields to this
                  playbook’s non-secret inputs. Library values are never copied.
                </p>
                <div className={styles.field}>
                  <label htmlFor={`${inputId}-template`}>Template</label>
                  <select
                    id={`${inputId}-template`}
                    value={selected}
                    disabled={busy}
                    onChange={(e) => select(e.target.value)}
                  >
                    <option value="">Choose a template…</option>
                    {templates.map((t) => (
                      <option
                        key={t.id}
                        value={t.id}
                        disabled={
                          t.images.length > 0 ||
                          t.fields.some((f) => f.kind === "secret")
                        }
                      >
                        {t.name}
                        {t.images.length
                          ? " · images unsupported"
                          : t.fields.some((f) => f.kind === "secret")
                            ? " · secret fields unsupported"
                            : ""}
                      </option>
                    ))}
                  </select>
                </div>
                {!busy && !templates.length && (
                  <p>
                    No templates available.{" "}
                    <Link to="/templates" onClick={close}>
                      Open the template library
                    </Link>
                    .
                  </p>
                )}
                {template && (
                  <>
                    <pre>{template.body}</pre>
                    {template.fields.map((f) => (
                      <div key={f.name} className={styles.field}>
                        <label htmlFor={`${inputId}-${f.name}`}>
                          Input for {f.label || f.name}
                          {f.required ? " (required)" : ""}
                        </label>
                        <select
                          id={`${inputId}-${f.name}`}
                          value={bindings[f.name] ?? ""}
                          disabled={busy}
                          onChange={(e) =>
                            setBindings({
                              ...bindings,
                              [f.name]: e.target.value,
                            })
                          }
                        >
                          <option value="">Choose a workflow input…</option>
                          {nonSecret.map((v) => (
                            <option key={v.name} value={v.name}>
                              {v.label || v.name} ({v.type ?? "text"})
                            </option>
                          ))}
                        </select>
                      </div>
                    ))}
                    {!!template.fields.length && !nonSecret.length && (
                      <p>
                        Add non-secret inputs in Workflow properties first, then
                        import this template.
                      </p>
                    )}
                    {dialog.replace && (
                      <p>
                        This explicitly replaces the customized copy with the
                        selected source and mappings.
                      </p>
                    )}
                  </>
                )}
              </>
            )}
            {dialog.kind === "compare" && comparison && (
              <>
                <p>
                  {comparison.state === "deleted"
                    ? "The source template was deleted. Your retained workflow copy remains usable."
                    : comparison.state === "current"
                      ? "The source has not changed. Your customized copy is retained."
                      : "The source has changed. Review both copies before adopting it."}
                </p>
                <h3>Workflow copy</h3>
                <p>{comparison.retained.title}</p>
                <pre>{comparison.retained.body}</pre>
                <ul>
                  {comparison.retained.source?.template.fields.map((f) => (
                    <li key={f.name}>
                      {f.label || f.name} →{" "}
                      {comparison.retained.source!.bindings[f.name]} ·{" "}
                      {f.required ? "Required" : "Optional"}
                    </li>
                  ))}
                </ul>
                {comparison.candidate && (
                  <>
                    <h3>Current source</h3>
                    <p>{comparison.candidate.title}</p>
                    <pre>{comparison.candidate.body}</pre>
                    <ul>
                      {comparison.candidate.source?.template.fields.map((f) => (
                        <li key={f.name}>
                          {f.label || f.name} →{" "}
                          {comparison.candidate!.source!.bindings[f.name]} ·{" "}
                          {f.required ? "Required" : "Optional"}
                        </li>
                      ))}
                    </ul>
                    <p>
                      Adopting replaces the customized title, instructions and
                      source snapshot in this draft. Completion rules stay
                      unchanged.
                    </p>
                  </>
                )}
                {comparison.reason && (
                  <p className={styles.error}>{comparison.reason}</p>
                )}
                {comparison.state === "changed" && !comparison.candidate && (
                  <button
                    type="button"
                    className={buttons.ghost}
                    onClick={(e) => openImport(e.currentTarget, dialog.id)}
                  >
                    Choose source and remap inputs
                  </button>
                )}
              </>
            )}
          </div>
        </PlaybookDialog>
      )}
    </div>
  );
}
