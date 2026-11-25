/** Existing single-project deployment contracts. Inputs stay in memory until BIND. */
export type PlaybookBinding = { name: string; kind: "text" | "secret" } & (
  { value: string } | { ref: "global" }
);
export type PlaybookAssignments = Record<
  string,
  { engine: string; model: string } | null
>;
export interface PlaybookInputs {
  revision: string;
  destination: string;
  project_id?: string;
  create?: boolean;
  bindings: PlaybookBinding[];
  assignments: PlaybookAssignments;
}
export interface DeploymentReview {
  digest: string;
  deployment_id: string;
  destination: { path: string; [key: string]: unknown };
  variables: {
    name: string;
    kind: string;
    type: string;
    state: string;
    source: string | null;
    revision: number | null;
    value?: string | number | boolean;
    set?: boolean;
    explicit_global?: boolean;
  }[];
  materials: {
    path: string;
    disposition: string;
    before: { kind: string };
    after: { kind: string; text?: string; base64?: string; target?: string };
  }[];
  changes: {
    path: string;
    action: string;
    diff?: string;
    before?: unknown;
    after?: unknown;
  }[];
  conflicts: { path: string; reason: string; [key: string]: unknown }[];
  targets: {
    id: string;
    kind: string;
    probe?: string;
    connection_kind?: string;
    args: Record<string, unknown>;
    pending: Record<string, unknown>;
    default_variables: string[];
    requires_confirmation: boolean;
  }[];
  assignments: {
    step: string;
    requested: { engine: string; model: string } | null;
    assignment: { engine: string; model: string } | null;
    reason: string | null;
  }[];
  capability_requests: string[];
  rituals: { name: string; scheduled: boolean }[];
}
export interface DeploymentReceipt {
  receipt: string;
  digest: string;
}
export interface DeploymentStatus {
  project_id: string;
  deployment_id?: string;
  playbook_id?: string;
  state: "none" | "bound" | "applied" | "removed" | "interrupted";
  operation?: "bind" | "apply" | "remove";
  operation_id?: string;
  files?: { path: string; state: string; action?: string }[];
}
export interface DeploymentRemoval {
  digest: string;
  changes: DeploymentReview["changes"];
  conflicts: DeploymentReview["conflicts"];
  bindings: { remove: string[]; restore: string[]; refusal: string | null };
  directories: string[];
  secret_files: { name: string; path: string }[];
}
