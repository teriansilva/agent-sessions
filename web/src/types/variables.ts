/** The shared library's public shapes. Secret rows never carry value or envelope. */
export type VariableType = "text" | "url" | "path" | "enum" | "int" | "bool";
export type VariableValue = string | number | boolean;
export interface VariableDefinition {
  value_type: VariableType;
  label?: string;
  help?: string;
  required?: boolean;
  choices?: string[];
  pattern?: string;
  default?: VariableValue;
  example?: VariableValue;
}
export interface SharedVariable {
  /** Null only for an old store which has not yet accepted its first shared-library write. */
  id: string | null;
  name: string;
  /** `secret_file` (#1786): content is never returned; only its display name and size. */
  kind: "text" | "secret" | "secret_file";
  definition: VariableDefinition;
  set: boolean;
  needs_reentry?: boolean;
  /** Secrets and secret files: whether agents may request it (#1786). */
  agent_access?: boolean;
  file?: { name: string; size: number };
  /** Secrets: the projects that bind it by explicit reference — whose agents it may reach. */
  projects?: string[];
  value?: VariableValue;
  created_at: number;
  updated_at: number;
}
export interface VariableCollection {
  id: string;
  name: string;
  variable_ids: string[];
  created_at: number;
  updated_at: number;
}
export interface SharedVariablesResponse {
  variables: SharedVariable[];
  collections: VariableCollection[];
  migration_required: boolean;
  limits: { variables_max: number; value_max: number; name_max: number; secret_min: number; collections_max: number };
}
export interface SharedVariableInput {
  name: string;
  kind: "text" | "secret" | "secret_file";
  definition: VariableDefinition;
  value?: VariableValue;
}
export interface SharedVariableUpdate {
  definition?: VariableDefinition;
  value?: VariableValue;
  clear_value?: true;
  /** Secrets (#1786): whether agents in the projects that bind it may request a private copy. */
  agent_access?: boolean;
}
export type CollectionInput = Pick<VariableCollection, "name" | "variable_ids">;

/** One agent request for a secret (#1786): never a value or a path. */
export interface SecretAccessEntry {
  at: number;
  project_id: string | null;
  engine: string | null;
  session_key: string | null;
  outcome: "pending" | "materialized" | "failed" | "refused" | "interrupted";
  reason: string | null;
}
