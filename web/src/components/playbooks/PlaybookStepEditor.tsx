import { useId } from "react";
import { useFieldError } from "./playbookValidation";
import {
  engineName,
  offeredModels,
  useEngineRoster,
} from "../../app/engineRoster";
import type { AuthoringSchema, Item, Step, Variable } from "./playbookDraft";
import { ancestors, freshId } from "./playbookDraft";
import buttons from "../ui/actionButton.module.css";
import styles from "./playbookEditor.module.css";

export function TextField({
  label,
  value,
  onChange,
  multiline = false,
  readOnly = false,
  field,
}: {
  label: string;
  value: string;
  onChange?: (value: string) => void;
  multiline?: boolean;
  readOnly?: boolean;
  field?: string;
}) {
  const id = useId();
  const error = useFieldError(field);
  const props = {
    id,
    value,
    readOnly,
    "data-field": field,
    "aria-invalid": !!error,
    "aria-describedby": error ? `${id}-error` : undefined,
    onChange: (e: React.ChangeEvent<HTMLInputElement | HTMLTextAreaElement>) =>
      onChange?.(e.target.value),
  };
  return (
    <div className={styles.field}>
      <label htmlFor={id}>{label}</label>
      {multiline ? <textarea {...props} rows={4} /> : <input {...props} />}
      {error && (
        <span id={`${id}-error`} className={styles.fieldError}>
          {error}
        </span>
      )}
    </div>
  );
}
export function Choice({
  label,
  value,
  choices,
  onChange,
  optional = false,
  field,
}: {
  label: string;
  value: string;
  choices: (string | { value: string; label: string })[];
  onChange: (value: string) => void;
  optional?: boolean;
  field?: string;
}) {
  const id = useId();
  const error = useFieldError(field);
  const options = choices.map((c) =>
    typeof c === "string" ? { value: c, label: c } : c,
  );
  return (
    <div className={styles.field}>
      <label htmlFor={id}>{label}</label>
      <select
        id={id}
        value={value}
        onChange={(e) => onChange(e.target.value)}
        data-field={field}
        aria-invalid={!!error}
        aria-describedby={error ? `${id}-error` : undefined}
      >
        {optional && <option value="">None</option>}
        {value && !options.some((c) => c.value === value) && (
          <option value={value}>{value} · unavailable</option>
        )}
        {!optional && !value && <option value="">Choose…</option>}
        {options.map((c) => (
          <option key={c.value} value={c.value}>
            {c.label}
          </option>
        ))}
      </select>
      {error && (
        <span id={`${id}-error`} className={styles.fieldError}>
          {error}
        </span>
      )}
    </div>
  );
}
function Checks({
  label,
  values,
  choices,
  onChange,
}: {
  label: string;
  values: string[];
  choices: { id: string; title: string }[];
  onChange: (values: string[]) => void;
}) {
  const all = [
    ...choices,
    ...values
      .filter((v) => !choices.some((c) => c.id === v))
      .map((v) => ({ id: v, title: `${v} · unavailable` })),
  ];
  return (
    <fieldset>
      <legend>{label}</legend>
      {all.map((c) => (
        <label className={styles.check} key={c.id}>
          <input
            type="checkbox"
            checked={values.includes(c.id)}
            onChange={(e) =>
              onChange(
                e.target.checked
                  ? [...values, c.id]
                  : values.filter((v) => v !== c.id),
              )
            }
          />
          {c.title}
        </label>
      ))}
      {!all.length && <p>No eligible choices yet.</p>}
    </fieldset>
  );
}
export function PlaybookStepEditor({
  step,
  onBack,
  steps,
  variables,
  schema,
  onChange,
  onRemove,
  field,
}: {
  step: Step;
  onBack: () => void;
  steps: Step[];
  variables: Variable[];
  schema: AuthoringSchema;
  onChange: (step: Step) => void;
  onRemove: () => void;
  field: string;
}) {
  const roster = useEngineRoster();
  const patch = (value: Partial<Step>) => onChange({ ...step, ...value });
  const parents = ancestors(steps, step.id);
  const previous = steps.filter((s) => parents.has(s.id));
  const actor = step.actor;
  const eligible = schema.agents.filter((id) =>
    roster.engines.some((e) => e.id === id),
  );
  const models = offeredModels(actor.engine ?? "");
  const model = actor.model ?? "default";
  const knownModel =
    model === "default" ||
    models.some((m) => m.id === model || m.aliases.includes(model));
  const checklist = step.checklist ?? [];
  const changeItem = (index: number, item: Item) =>
    patch({ checklist: checklist.map((old, i) => (i === index ? item : old)) });
  const availableOutputs = [
    ...new Set(
      checklist.flatMap(
        (item) => schema.probes[item.probe ?? "none"]?.outputs ?? [],
      ),
    ),
  ];
  return (
    <section className={styles.inspector} aria-label="Step inspector">
      <h2 tabIndex={-1}>Step inspector</h2>
      <button
        type="button"
        className={`${buttons.ghost} ${styles.mobileBack}`}
        onClick={onBack}
      >
        ← Back to steps
      </button>
      <TextField
        label="Step title"
        value={step.title}
        onChange={(title) => patch({ title })}
        field={`${field}.title`}
      />
      <TextField label="Step ID" value={step.id} readOnly />
      <TextField
        label="Brief"
        multiline
        value={step.brief ?? ""}
        onChange={(brief) => patch({ brief })}
        field={`${field}.brief`}
      />
      <Choice
        label="Actor"
        value={actor.kind}
        choices={schema.actors.map((kind) => ({
          value: kind,
          label:
            kind === "none"
              ? "No actor"
              : kind.charAt(0).toUpperCase() + kind.slice(1),
        }))}
        onChange={(kind) =>
          patch({
            actor:
              kind === "agent"
                ? { kind, engine: "" }
                : kind === "external"
                  ? { kind, label: "External reviewer" }
                  : { kind },
          })
        }
      />
      {actor.kind === "agent" && (
        <>
          <Choice
            label="Agent"
            value={actor.engine ?? ""}
            choices={eligible.map((id) => ({
              value: id,
              label: engineName(id),
            }))}
            onChange={(engine) =>
              patch({ actor: { ...actor, engine, model: "default" } })
            }
            field={`${field}.actor.engine`}
          />
          <Choice
            label="Model"
            value={model}
            choices={[
              "default",
              ...models.map((m) => ({ value: m.id, label: m.id })),
              ...(knownModel &&
              model !== "default" &&
              !models.some((m) => m.id === model)
                ? [model]
                : []),
            ]}
            onChange={(model) => patch({ actor: { ...actor, model } })}
            field={`${field}.actor.model`}
          />
          {(!eligible.includes(actor.engine ?? "") || !knownModel) && (
            <p className={styles.warning}>
              This agent or model is unavailable. Its stored reference is
              retained until you choose a replacement.
            </p>
          )}
        </>
      )}
      {actor.kind === "external" && (
        <TextField
          label="External actor label"
          value={actor.label ?? ""}
          onChange={(label) => patch({ actor: { ...actor, label } })}
        />
      )}
      <Checks
        label="After these steps"
        values={step.after ?? []}
        choices={steps.filter(
          (s) =>
            s.id !== step.id &&
            !(s.actor.kind === "none" && !s.checklist?.length) &&
            !ancestors(steps, s.id).has(step.id),
        )}
        onChange={(after) => patch({ after })}
      />
      <h3>Checklist evidence</h3>
      {checklist.map((item, i) => {
        const probe = item.probe ?? "none";
        return (
          <fieldset key={item.key} aria-label={`Checklist item ${i + 1}`}>
            <legend>Check {i + 1}</legend>
            <TextField
              label="Evidence required"
              value={item.title}
              onChange={(title) => changeItem(i, { ...item, title })}
              field={`${field}.checklist[${i}].title`}
            />
            <Choice
              label="Probe"
              value={probe}
              choices={Object.keys(schema.probes)}
              onChange={(probe) =>
                changeItem(i, { ...item, probe, probe_args: {} })
              }
            />
            <label className={styles.check}>
              <input
                type="checkbox"
                checked={item.required ?? true}
                onChange={(e) =>
                  changeItem(i, { ...item, required: e.target.checked })
                }
              />
              Required evidence
            </label>
            {Object.entries(schema.probes[probe]?.args ?? {}).map(
              ([name, spec]) => {
                const options = [
                  ...variables
                    .filter(
                      (v) =>
                        v.kind !== "secret" &&
                        spec.variable_types.includes(v.type ?? "text"),
                    )
                    .map((v) => `{{${v.name}}}`),
                  ...previous.flatMap((s) =>
                    (s.outputs ?? [])
                      .filter((slot) => spec.slots.includes(slot))
                      .map((slot) => `{{steps.${s.id}.${slot}}}`),
                  ),
                ];
                const val = item.probe_args?.[name];
                const update = (value: string) => {
                  const args = { ...item.probe_args };
                  if (!value) delete args[name];
                  else
                    args[name] =
                      spec.literal &&
                      spec.type === "int" &&
                      /^-?\d+$/.test(value)
                        ? Number(value)
                        : value;
                  changeItem(i, { ...item, probe_args: args });
                };
                return (
                  <div key={name}>
                    <Choice
                      label={`${name}${spec.required ? " (required)" : ""}`}
                      value={
                        typeof val === "string" && val.startsWith("{{")
                          ? val
                          : ""
                      }
                      choices={options}
                      optional
                      onChange={update}
                      field={`${field}.checklist[${i}].probe_args.${name}`}
                    />
                    {spec.literal && (
                      <TextField
                        label={`${name} literal expectation`}
                        value={
                          val !== undefined && !String(val).startsWith("{{")
                            ? String(val)
                            : ""
                        }
                        onChange={update}
                      />
                    )}
                    {!spec.literal && !options.length && (
                      <p>
                        Declare a matching variable, or an eligible ancestor
                        output.
                      </p>
                    )}
                  </div>
                );
              },
            )}
            <button
              type="button"
              className={buttons.ghost}
              onClick={() =>
                patch({
                  checklist: checklist.filter((_, index) => index !== i),
                })
              }
            >
              Remove check {i + 1}
            </button>
          </fieldset>
        );
      })}
      <button
        type="button"
        className={buttons.ghost}
        disabled={checklist.length >= schema.limits.items}
        onClick={() =>
          patch({
            checklist: [
              ...checklist,
              {
                key: freshId("check"),
                title: "Confirm the result",
                probe: "supervisor_judged",
              },
            ],
          })
        }
      >
        Add checklist item
      </button>
      <Checks
        label="Outputs"
        values={step.outputs ?? []}
        choices={availableOutputs.map((id) => ({ id, title: id }))}
        onChange={(outputs) => patch({ outputs })}
      />
      <fieldset>
        <legend>Bounded rework</legend>
        <Choice
          label="Rework to"
          value={step.rework?.to ?? ""}
          choices={previous.map((s) => ({ value: s.id, label: s.title }))}
          optional
          onChange={(to) => {
            if (!to) {
              const copy = { ...step };
              delete copy.rework;
              onChange(copy);
            } else
              patch({
                rework: {
                  to,
                  when: step.rework?.when ?? checklist[0]?.key ?? "",
                  max_rounds: step.rework?.max_rounds ?? 3,
                },
              });
          }}
        />
        {step.rework && (
          <>
            <Choice
              label="When check fails"
              value={step.rework.when}
              choices={checklist.map((c) => ({ value: c.key, label: c.title }))}
              onChange={(when) => patch({ rework: { ...step.rework!, when } })}
            />
            <label>
              Maximum rework rounds
              <input
                type="number"
                min={schema.limits.rework_min}
                max={schema.limits.rework_max}
                value={step.rework.max_rounds}
                onChange={(e) =>
                  patch({
                    rework: {
                      ...step.rework!,
                      max_rounds: e.target.valueAsNumber || 0,
                    },
                  })
                }
              />
            </label>
          </>
        )}
      </fieldset>
      <fieldset>
        <legend>Independent from</legend>
        {(step.distinct_from ?? []).map((d, i) => (
          <div className={styles.rule} key={i}>
            <Choice
              label="Distinct step"
              value={d.step}
              choices={steps
                .filter((s) => s.id !== step.id)
                .map((s) => ({ value: s.id, label: s.title }))}
              onChange={(value) =>
                patch({
                  distinct_from: step.distinct_from!.map((old, j) =>
                    i === j ? { ...old, step: value } : old,
                  ),
                })
              }
            />
            <Choice
              label="Distinct constraint"
              value={d.constraint}
              choices={schema.distinct}
              onChange={(constraint) =>
                patch({
                  distinct_from: step.distinct_from!.map((old, j) =>
                    i === j ? { ...old, constraint } : old,
                  ),
                })
              }
            />
            <button
              type="button"
              className={buttons.ghost}
              onClick={() =>
                patch({
                  distinct_from: step.distinct_from!.filter((_, j) => i !== j),
                })
              }
            >
              Remove constraint {i + 1}
            </button>
          </div>
        ))}
        <button
          type="button"
          className={buttons.ghost}
          disabled={
            (step.distinct_from?.length ?? 0) >= schema.limits.distinct ||
            steps.length < 2
          }
          onClick={() =>
            patch({
              distinct_from: [
                ...(step.distinct_from ?? []),
                {
                  step: steps.find((s) => s.id !== step.id)!.id,
                  constraint: "session",
                },
              ],
            })
          }
        >
          Add distinct constraint
        </button>
      </fieldset>
      <Choice
        label="Shared memory"
        value={step.memory ?? "none"}
        choices={schema.memory}
        onChange={(memory) => patch({ memory })}
      />
      <button type="button" className={buttons.ghost} onClick={onRemove}>
        Remove step…
      </button>
    </section>
  );
}
