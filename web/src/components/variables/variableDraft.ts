import type { SharedVariable, SharedVariableInput, SharedVariableUpdate, VariableDefinition, VariableType, VariableValue } from "../../types/variables";

export interface VariableDraft {
  name: string; kind: "text" | "secret"; valueType: VariableType;
  label: string; help: string; required: boolean; choices: string; pattern: string;
  hasDefault: boolean; defaultValue: string; hasExample: boolean; example: string;
  storeValue: boolean; value: string;
}
export function draftFor(variable?: SharedVariable, kind: "text" | "secret" = "text"): VariableDraft {
  const definition = variable?.definition;
  return {
    name: variable?.name ?? "", kind: variable?.kind ?? kind,
    valueType: definition?.value_type ?? "text", label: definition?.label ?? "", help: definition?.help ?? "",
    required: definition?.required ?? false, choices: definition?.choices?.join("\n") ?? "", pattern: definition?.pattern ?? "",
    hasDefault: definition?.default !== undefined, defaultValue: String(definition?.default ?? ""),
    hasExample: definition?.example !== undefined, example: String(definition?.example ?? ""),
    storeValue: variable?.set ?? true,
    value: variable?.kind === "secret" ? "" : String(variable?.value ?? ""),
  };
}
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
  const definition: VariableDefinition = { value_type: kind === "secret" ? "text" : valueType, label: draft.label || name, help: draft.help, required: draft.required };
  // Patterns apply to text secrets too. An ordinary label/value edit must never drop one.
  if (draft.pattern && (valueType === "text" || valueType === "path")) definition.pattern = draft.pattern;
  if (kind !== "secret") {
    if (valueType === "enum") definition.choices = draft.choices.split("\n").map(s => s.trim()).filter(Boolean);
    if (draft.hasDefault) definition.default = typed(draft.defaultValue, valueType, "Default");
    if (draft.hasExample) definition.example = typed(draft.example, valueType, "Example");
  }
  const payload: SharedVariableInput | SharedVariableUpdate = original ? { definition } : { name, kind, definition };
  if (!draft.storeValue) {
    if (original?.set) (payload as SharedVariableUpdate).clear_value = true;
  } else if (kind === "secret") {
    // Blank means preserve an existing write-only value, never replace it with an empty one.
    if (draft.value) {
      if (draft.value.length < secretMin) throw new Error(`Enter a secret of at least ${secretMin} characters.`);
      payload.value = draft.value;
    }
    else if (!original?.set) throw new Error("Enter a secret, or turn off Store a global value.");
  } else {
    payload.value = typed(draft.value, valueType, "Global value");
  }
  return payload;
}
