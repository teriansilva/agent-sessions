/** Contextual step Properties; completion probes retain their serialized checklist identity. */
import { useId } from "react";
import { useFieldError } from "./playbookValidation";
import {
  engineName,
  offeredModels,
  useEngineRoster,
} from "../../app/engineRoster";
import type { AuthoringSchema, Item, Step, Variable } from "./playbookDraft";
import { ancestors, freshId } from "./playbookDraft";
import { PlaybookPrompts } from "./PlaybookPrompts";
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
  field,
}: {
  label: string;
  field?: string;
  values: string[];
  choices: { id: string; title: string }[];
  onChange: (values: string[]) => void;
}) {
  const error = useFieldError(field);
  const all = [
    ...choices,
    ...values
      .filter((v) => !choices.some((c) => c.id === v))
      .map((v) => ({ id: v, title: `${v} · unavailable` })),
  ];
  return (
    <fieldset data-field={field} tabIndex={-1} aria-invalid={!!error}>
      <legend>{label}</legend>
      {error && <p className={styles.fieldError}>{error}</p>}
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
  onMove,
  field,
}: {
  step: Step;
  onBack: () => void;
  steps: Step[];
  variables: Variable[];
  schema: AuthoringSchema;
  onChange: (step: Step) => void;
  onRemove: () => void;
  onMove: (direction: -1 | 1) => void;
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
    <section className={styles.inspector} aria-label="Step properties">
      <h2 tabIndex={-1}>Properties</h2>
      <p className={styles.hint}>
        Step {steps.indexOf(step) + 1} of {steps.length} · {step.title}
      </p>
      <button type="button" className={buttons.ghost} onClick={onBack}>
        ← Back to flowchart
      </button>
      <TextField
        label="Step title"
        value={step.title}
        onChange={(title) => patch({ title })}
        field={`${field}.title`}
      />
      <section
        className={styles.propertyGroup}
        aria-label="Who performs this step"
      >
        <h3>Who performs this step</h3>
        <p className={styles.hint}>
          Choose an agent, an external service, the operator, or a note with no
          performer.
        </p>
        <Choice
          label="Who does this step?"
          field={`${field}.actor.kind`}
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
            <p className={styles.warning}>
              Saved agent assignments are retained. API profile setup and
              workflow execution are not available yet.
            </p>
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
            field={`${field}.actor.label`}
            value={actor.label ?? ""}
            onChange={(label) => patch({ actor: { ...actor, label } })}
          />
        )}
      </section>
      <section className={styles.propertyGroup} aria-label="Instructions">
        <h3>Instructions</h3>
        <TextField
          label="Step instructions"
          multiline
          value={step.brief ?? ""}
          onChange={(brief) => patch({ brief })}
          field={`${field}.brief`}
        />
        <p className={styles.hint}>
          Describe the work for this step. External and operator steps use this
          text as reference instructions.
        </p>
        <PlaybookPrompts
          prompts={step.prompts ?? []}
          variables={variables}
          limit={schema.limits.prompts ?? 0}
          field={`${field}.prompts`}
          onChange={(prompts) => patch({ prompts })}
        />
      </section>
      <section className={styles.propertyGroup} aria-label="Completion rules">
        <h3>Completion rules</h3>
        <p className={styles.hint}>
          Independent evidence decides when this step is complete. A finished
          agent turn is not evidence.
        </p>
        {checklist.map((item, i) => {
          const probe = item.probe ?? "none";
          return (
            <fieldset key={item.key} aria-label={`Completion rule ${i + 1}`}>
              <legend>Rule {i + 1}</legend>
              <TextField
                label="Evidence required"
                value={item.title}
                onChange={(title) => changeItem(i, { ...item, title })}
                field={`${field}.checklist[${i}].title`}
              />
              <Choice
                label="Evidence check"
                field={`${field}.checklist[${i}].probe`}
                value={probe}
                choices={Object.keys(schema.probes)}
                onChange={(probe) =>
                  changeItem(i, { ...item, probe, probe_args: {} })
                }
              />
              <label className={styles.check}>
                <input
                  type="checkbox"
                  data-field={`${field}.checklist[${i}].required`}
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
                Remove completion rule {i + 1}
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
          Add completion rule
        </button>
      </section>
      <section className={styles.propertyGroup} aria-label="What happens next">
        <h3>What happens next</h3>
        <p className={styles.hint}>
          This step starts after all selected prerequisites finish. Select or
          clear them here to connect without dragging.
        </p>
        <Checks
          label="Starts after"
          field={`${field}.after`}
          values={step.after ?? []}
          choices={steps.filter(
            (s) =>
              s.id !== step.id &&
              !(s.actor.kind === "none" && !s.checklist?.length) &&
              !ancestors(steps, s.id).has(step.id),
          )}
          onChange={(after) => patch({ after })}
        />
        <fieldset>
          <legend>If review requests changes</legend>
          <Choice
            label="Rework to"
            field={`${field}.rework.to`}
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
                label="When completion rule fails"
                field={`${field}.rework.when`}
                value={step.rework.when}
                choices={checklist.map((c) => ({
                  value: c.key,
                  label: c.title,
                }))}
                onChange={(when) =>
                  patch({ rework: { ...step.rework!, when } })
                }
              />
              <label>
                Maximum revision rounds
                <input
                  type="number"
                  data-field={`${field}.rework.max_rounds`}
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
        <p className={styles.hint}>
          Rework returns to an earlier step for at most the chosen rounds, then
          asks the operator. It is separate from normal dependencies.
        </p>
        <p className={styles.hint}>
          Order changes the presentation. Prerequisites determine when steps
          begin.
        </p>
        <div className={styles.actions}>
          {([-1, 1] as const).map((direction) => (
            <button
              key={direction}
              type="button"
              className={buttons.ghost}
              disabled={
                steps.indexOf(step) + direction < 0 ||
                steps.indexOf(step) + direction >= steps.length
              }
              onClick={() => onMove(direction)}
            >
              Move {step.title} {direction < 0 ? "up" : "down"}
            </button>
          ))}
          <button type="button" className={buttons.ghost} onClick={onRemove}>
            Remove step…
          </button>
        </div>
      </section>
      <details>
        <summary>Advanced step properties</summary>
        <TextField
          label="Step ID"
          value={step.id}
          readOnly
          field={`${field}.id`}
        />
        <Checks
          label="What later steps can use"
          field={`${field}.outputs`}
          values={step.outputs ?? []}
          choices={availableOutputs.map((id) => ({ id, title: id }))}
          onChange={(outputs) => patch({ outputs })}
        />
        <fieldset>
          <legend>Independent from</legend>
          {(step.distinct_from ?? []).map((d, i) => (
            <div className={styles.rule} key={i}>
              <Choice
                label="Distinct step"
                field={`${field}.distinct_from[${i}].step`}
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
                field={`${field}.distinct_from[${i}].constraint`}
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
                    distinct_from: step.distinct_from!.filter(
                      (_, j) => i !== j,
                    ),
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
          field={`${field}.memory`}
          value={step.memory ?? "none"}
          choices={schema.memory}
          onChange={(memory) => patch({ memory })}
        />
      </details>
    </section>
  );
}
