import type { SharedVariable, SharedVariableInput, SharedVariableUpdate, VariableDefinition, VariableType, VariableValue } from "../../types/variables";

export type SecretMode = "single" | "multi" | "file";
export interface VariableDraft {
  name: string; kind: "text" | "secret" | "secret_file"; valueType: VariableType;
  label: string; help: string; required: boolean; choices: string; pattern: string;
  hasDefault: boolean; defaultValue: string; hasExample: boolean; example: string;
  storeValue: boolean; value: string;
  /** Secrets (#1786): how the value is entered; `file` is the `secret_file` kind. */
  secretMode: SecretMode;
  /** Secrets (#1786): agent access, and the projects that bind it (whose agents it may reach). */
  agentAccess: boolean; projects: string[];
  /** A newly chosen file's name and size (the bytes stay outside the draft, in memory only). */
  fileName: string; fileSize: number;
}
export function draftFor(variable?: SharedVariable, kind: "text" | "secret" | "secret_file" = "text"): VariableDraft {
  const definition = variable?.definition;
  const k = variable?.kind ?? kind;
  return {
    name: variable?.name ?? "", kind: k,
    valueType: definition?.value_type ?? "text", label: definition?.label ?? "", help: definition?.help ?? "",
    required: definition?.required ?? false, choices: definition?.choices?.join("\n") ?? "", pattern: definition?.pattern ?? "",
    hasDefault: definition?.default !== undefined, defaultValue: String(definition?.default ?? ""),
    hasExample: definition?.example !== undefined, example: String(definition?.example ?? ""),
    storeValue: variable?.set ?? true,
    value: variable?.kind === "text" ? String(variable?.value ?? "") : "",
    secretMode: k === "secret_file" ? "file" : "single",
    agentAccess: variable?.agent_access ?? false, projects: [...(variable?.projects ?? [])].sort(),
    fileName: "", fileSize: 0,
  };
}
export const isSecret = (d: Pick<VariableDraft, "kind">) => d.kind !== "text";
function typed(value: string, type: VariableType, field: string): VariableValue {
  if (type === "bool") {
    if (value !== "true" && value !== "false") throw new Error(`${field}: choose true or false.`);
    return value === "true";
  }
  if (type === "int") {
    if (!/^(?:0|-?[1-9][0-9]*)$/.test(value) || !Number.isSafeInteger(Number(value))) throw new Error(`${field}: enter a whole number.`);
    return Number(value);
  }
  return value;
}
export function variablePayload(draft: VariableDraft, original?: SharedVariable, secretMin = 8): SharedVariableInput | SharedVariableUpdate {
  const { name, kind, valueType } = draft;
  if (!/^[a-z][a-z0-9_]{0,31}$/.test(name)) throw new Error("Name: use lowercase letters, numbers and underscores, starting with a letter (up to 32).");
  const definition: VariableDefinition = { value_type: kind === "text" ? valueType : "text", label: draft.label || name, help: draft.help, required: draft.required };
  // Patterns apply to text secrets too. An ordinary label/value edit must never drop one.
  if (draft.pattern && (valueType === "text" || valueType === "path")) definition.pattern = draft.pattern;
  if (kind === "text") {
    if (valueType === "enum") definition.choices = draft.choices.split("\n").map(s => s.trim()).filter(Boolean);
    if (draft.hasDefault) definition.default = typed(draft.defaultValue, valueType, "Default");
    if (draft.hasExample) definition.example = typed(draft.example, valueType, "Example");
  }
  const payload: SharedVariableInput | SharedVariableUpdate = original ? { definition } : { name, kind, definition };
  if (original && draft.agentAccess !== (original.agent_access ?? false)) (payload as SharedVariableUpdate).agent_access = draft.agentAccess;
  if (kind === "secret_file") {
    // The content is uploaded separately (multipart) after the definition is saved.
    if (!original && !draft.fileName) throw new Error("Choose a file to store.");
    return payload;
  }
  if (!draft.storeValue) {
    if (original?.set) (payload as SharedVariableUpdate).clear_value = true;
  } else if (kind === "secret") {
    // Blank means preserve an existing write-only value, never replace it with an empty one.
    // A pasted PEM/JSON usually ends with a newline; a secret cannot start or end with
    // whitespace (redaction would miss the trimmed form), so surrounding blank space is removed.
    const value = draft.secretMode === "multi" ? draft.value.trim() : draft.value;
    if (value) {
      if (value.length < secretMin) throw new Error(`Enter a secret of at least ${secretMin} characters.`);
      payload.value = value;
    }
    else if (!original?.set) throw new Error("Enter a secret, or turn off Store a global value.");
  } else {
    payload.value = typed(draft.value, valueType, "Global value");
  }
  return payload;
}

const meaningful = (d: object | undefined) => JSON.stringify(Object.entries(d ?? {}).filter(([, v]) => v !== undefined && v !== "" && v !== false).sort(([a], [b]) => a.localeCompare(b)));

/** True when an edit's payload would change nothing: Save then skips the request, so a
 *  projects-only change never bumps the record for nothing. */
export function isNoopUpdate(payload: SharedVariableUpdate, original: SharedVariable): boolean {
  const { definition, ...rest } = payload;
  return Object.keys(rest).length === 0 && meaningful(definition) === meaningful(original.definition);
}
