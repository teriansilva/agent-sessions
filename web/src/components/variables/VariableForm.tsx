import { useEffect, useId, useRef } from "react";
import type { VariableType } from "../../types/variables";
import type { VariableDraft } from "./variableDraft";
import styles from "../../routes/Variables.module.css";

export function VariableForm({ draft, editing, disabled, onChange }: {
  draft: VariableDraft;
  editing: boolean;
  disabled: boolean;
  onChange: (draft: VariableDraft) => void;
}) {
  const nameRef = useRef<HTMLInputElement>(null);
  const nameId = useId();
  const typeId = useId();
  const valueId = useId();
  const helpId = useId();
  const choicesId = useId();
  useEffect(() => { nameRef.current?.focus(); }, []);
  const update = (patch: Partial<VariableDraft>) => onChange({ ...draft, ...patch });
  const secret = draft.kind === "secret";
  const valueControl = (key: "value" | "defaultValue" | "example", label: string) => (
    <label className={styles.field}>
      <span id={`${valueId}-${key}`}>{label}</span>
      {draft.valueType === "bool" && !secret ? (
        <select aria-labelledby={`${valueId}-${key}`} value={draft[key]} onChange={e => update({ [key]: e.target.value })}>
          <option value="">Choose a value</option><option value="true">True</option><option value="false">False</option>
        </select>
      ) : draft.valueType === "enum" && !secret ? (
        <select aria-labelledby={`${valueId}-${key}`} value={draft[key]} onChange={e => update({ [key]: e.target.value })}>
          <option value="">Choose a value</option>
          {draft.choices.split("\n").map(s => s.trim()).filter(Boolean).map((choice, i) => <option key={i} value={choice}>{choice}</option>)}
        </select>
      ) : secret ? (
        <input aria-labelledby={`${valueId}-${key}`} type="password" autoComplete="new-password" value={draft[key]} onChange={e => update({ [key]: e.target.value })} />
      ) : (
        <textarea aria-labelledby={`${valueId}-${key}`} rows={2} spellCheck={false} inputMode={draft.valueType === "int" ? "numeric" : undefined} value={draft[key]} onChange={e => update({ [key]: e.target.value })} />
      )}
    </label>
  );
  return (
    <fieldset disabled={disabled} className={styles.fields}>
      <div className={styles.twoColumns}>
        <label className={styles.field}><span id={nameId}>Name</span>
          <input ref={nameRef} aria-labelledby={nameId} aria-describedby={`${nameId}-help`} value={draft.name} readOnly={editing} maxLength={32} autoComplete="off" spellCheck={false} onChange={e => update({ name: e.target.value })} />
          <small id={`${nameId}-help`}>{editing ? "The reference name stays fixed. You can change its label below." : "Lowercase letters, numbers and underscores; start with a letter."}</small>
        </label>
        <label className={styles.field}><span id={typeId}>Value type</span>
          <select aria-labelledby={typeId} value={draft.valueType} disabled={secret} onChange={e => update({ valueType: e.target.value as VariableType, value: "", defaultValue: "", example: "", hasDefault: false, hasExample: false, pattern: "" })}>
            <option value="text">Text</option><option value="url">URL</option><option value="path">Path</option><option value="enum">Choice</option><option value="int">Integer</option><option value="bool">Boolean</option>
          </select>
          {secret && <small>Secrets are write-only text.</small>}
        </label>
      </div>
      <label className={styles.field}><span>Label</span><input value={draft.label} onChange={e => update({ label: e.target.value })} /></label>
      <label className={styles.field}><span id={helpId}>Help</span><textarea aria-labelledby={helpId} rows={2} value={draft.help} onChange={e => update({ help: e.target.value })} /></label>
      {!secret && draft.valueType === "enum" && <label className={styles.field}><span id={choicesId}>Choices, one per line</span><textarea aria-labelledby={choicesId} rows={4} value={draft.choices} onChange={e => update({ choices: e.target.value })} /></label>}
      <label className={styles.check}><input type="checkbox" checked={draft.required} onChange={e => update({ required: e.target.checked })} /> Require a value when used</label>
      <div className={styles.valueSection}>
        <label className={styles.check}><input type="checkbox" checked={draft.storeValue} onChange={e => update({ storeValue: e.target.checked })} /> Store a global value</label>
        <p className={styles.help}>{draft.storeValue ? "Global values can be reused across projects through explicit bindings." : "Save the definition without a global value. Supply its value when binding it to a project."}</p>
        {draft.storeValue && valueControl("value", secret && editing ? "Replacement secret" : "Global value")}
        {secret && editing && draft.storeValue && <p className={styles.help}>Leave blank to keep the stored secret. The current value is never displayed.</p>}
        {editing && !draft.storeValue && <p className={styles.help}>Saving removes the global value and keeps this definition and its collection memberships.</p>}
      </div>
      <details className={styles.advanced}>
        <summary>{secret ? "Validation" : "Validation, default & example"}</summary>
        {(draft.valueType === "text" || draft.valueType === "path") && <label className={styles.field}><span>Pattern (regular expression)</span><input value={draft.pattern} spellCheck={false} onChange={e => update({ pattern: e.target.value })} /></label>}
        {!secret && <><label className={styles.check}><input type="checkbox" checked={draft.hasDefault} onChange={e => update({ hasDefault: e.target.checked })} /> Define a default</label>
        {draft.hasDefault && valueControl("defaultValue", "Default")}
        <label className={styles.check}><input type="checkbox" checked={draft.hasExample} onChange={e => update({ hasExample: e.target.checked })} /> Add an example</label>
        {draft.hasExample && valueControl("example", "Example")}
        </>}
      </details>
    </fieldset>
  );
}
