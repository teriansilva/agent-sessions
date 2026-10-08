/** The bundle API (#1191), distinct from mission checklists' historical playbook ids. */
export interface PlaybookStep {
  id: string;
  title: string;
  actor: {
    kind: "agent" | "operator" | "external" | "none";
    engine?: string;
    model?: string;
    label?: string;
  };
  after: string[];
  note: boolean;
}
export interface PlaybookCardData {
  id: string;
  ok: boolean;
  error: string | null;
  source: "local" | "bundled" | "catalog";
  editable: boolean;
  revision: string | null;
  default: boolean;
  name?: string;
  publisher?: string;
  version?: string;
  domain?: string;
  summary?: string;
  default_flow?: string | null;
  flows?: { id: string; title: string; steps: PlaybookStep[] }[];
  ships?: {
    materials: number;
    runbooks: number;
    templates: number;
    variables: number;
  };
  requires?: { binaries: string[]; connections: string[] };
  connections?: string[];
  capabilities?: string[];
}
export interface PlaybookList {
  playbooks: PlaybookCardData[];
  default: string | null;
  recovery_total: number;
}
export interface PlaybookDetail extends PlaybookCardData {
  readme?: string;
  files?: Record<string, string | { base64: string }>;
  documents?: Record<string, Record<string, unknown>>;
  requires_present?: { binaries: Record<string, boolean> };
  recovery_total: number;
}
export interface PlaybookWriteResult {
  durable?: boolean;
  state_durable?: boolean;
  recovery_durable?: boolean;
}
export interface PlaybookFleet {
  playbook_id: string;
  revision: string;
  projects: {
    project_id: string;
    deployment_id: string;
    state: string;
    revision: string | null;
    update_available: boolean;
  }[];
}
export interface PlaybookVerify {
  deployment_id: string;
  ok: boolean;
  checks: Record<
    string,
    { ok?: boolean; drift?: string[]; missing?: string[]; error?: string }
  >;
}
export interface PlaybookFleetReview {
  playbook_id: string;
  revision: string;
  digest: string;
  projects: {
    project_id: string;
    batchable: boolean;
    reasons: string[];
    changes?: {
      path: string;
      action: string;
      diff?: string;
      before?: unknown;
      after?: unknown;
    }[];
  }[];
}
export interface PlaybookFleetResult {
  operation_id: string;
  projects: { project_id: string; outcome: string; detail: string }[];
}
