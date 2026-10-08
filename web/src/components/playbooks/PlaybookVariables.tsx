import type { AuthoringSchema, Variable } from "./playbookDraft";
import { TextField, Choice } from "./PlaybookStepEditor";
import buttons from "../ui/actionButton.module.css";
import styles from "./playbookEditor.module.css";

export function PlaybookVariables({
  variables,
  schema,
  onChange,
}: {
  variables: Variable[];
  schema: AuthoringSchema;
  onChange: (variables: Variable[]) => void;
}) {
  const update = (i: number, value: Variable) =>
    onChange(variables.map((v, n) => (n === i ? value : v)));
  return (
    <details>
      <summary>Variables ({variables.length})</summary>
      <p>
        Define inputs here. Secret values are supplied separately when the
        playbook is assigned to a project.
      </p>
      {variables.map((v, i) => (
        <fieldset key={i}>
          <legend>Variable {i + 1}</legend>
          <div className={styles.fields}>
            <TextField
              label="Variable name"
              value={v.name}
              onChange={(name) => update(i, { ...v, name })}
              field={`variables[${i}].name`}
            />
            <TextField
              label="Variable label"
              value={v.label ?? ""}
              onChange={(label) => update(i, { ...v, label })}
            />
            <Choice
              label="Variable kind"
              value={v.kind ?? "text"}
              choices={["text", "secret"]}
              onChange={(kind) => {
                const next = { ...v, kind };
                if (kind === "secret") {
                  next.type = "text";
                  delete next.default;
                  delete next.example;
                  delete next.choices;
                  delete next.pattern;
                }
                update(i, next);
              }}
            />
            {v.kind !== "secret" && (
              <Choice
                label="Variable type"
                value={v.type ?? "text"}
                choices={schema.variable_types}
                onChange={(type) => {
                  const next = { ...v, type };
                  delete next.default;
                  delete next.example;
                  delete next.choices;
                  delete next.pattern;
                  update(i, next);
                }}
              />
            )}
          </div>
          <TextField
            label="Variable help"
            value={v.help ?? ""}
            onChange={(help) => update(i, { ...v, help })}
          />
          <label className={styles.check}>
            <input
              type="checkbox"
              checked={v.required ?? false}
              onChange={(e) => update(i, { ...v, required: e.target.checked })}
            />
            Required input
          </label>
          {v.type === "enum" && (
            <TextField
              label="Choices (one per line)"
              multiline
              value={(v.choices ?? []).join("\n")}
              onChange={(text) =>
                update(i, { ...v, choices: text.split("\n") })
              }
            />
          )}
          {v.kind !== "secret" &&
            (["default", "example"] as const).map((key) => (
              <div key={key}>
                <label className={styles.check}>
                  <input
                    type="checkbox"
                    checked={key in v}
                    onChange={(e) => {
                      const next = { ...v };
                      if (e.target.checked)
                        next[key] =
                          v.type === "bool" ? false : v.type === "int" ? 0 : "";
                      else delete next[key];
                      update(i, next);
                    }}
                  />
                  Supply {key}
                </label>
                {key in v &&
                  (v.type === "bool" ? (
                    <Choice
                      label={`Variable ${key}`}
                      choices={["true", "false"]}
                      value={String(v[key])}
                      onChange={(value) =>
                        update(i, { ...v, [key]: value === "true" })
                      }
                    />
                  ) : (
                    <TextField
                      label={`Variable ${key}`}
                      value={String(v[key])}
                      onChange={(value) =>
                        update(i, {
                          ...v,
                          [key]:
                            v.type === "int" && /^-?\d+$/.test(value)
                              ? Number(value)
                              : value,
                        })
                      }
                    />
                  ))}
              </div>
            ))}
          <button
            type="button"
            className={buttons.ghost}
            onClick={() => onChange(variables.filter((_, n) => n !== i))}
          >
            Remove variable {i + 1}
          </button>
        </fieldset>
      ))}
      <button
        type="button"
        className={buttons.ghost}
        disabled={variables.length >= schema.limits.variables}
        onClick={() =>
          onChange([
            ...variables,
            {
              name: `input_${variables.length + 1}`,
              label: "New input",
              kind: "text",
              type: "text",
            },
          ])
        }
      >
        Add variable
      </button>
    </details>
  );
}
