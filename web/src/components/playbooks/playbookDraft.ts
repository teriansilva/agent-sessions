/** One complete in-memory bundle. Unedited files retain their original spelling and bytes. */
import type { PlaybookDetail } from "../../types/playbooks";
import type { TemplateInput } from "../../types/api";
import { sha256 } from "@noble/hashes/sha2.js";
import { bytesToHex } from "@noble/hashes/utils.js";

export type Document = Record<string, unknown>;
export type Files = Record<
  string,
  string | { base64: string } | { toml: Document }
>;
export interface Variable extends Document {
  name: string;
  label?: string;
  help?: string;
  kind?: string;
  type?: string;
  required?: boolean;
  default?: string | number | boolean;
  example?: string | number | boolean;
  choices?: string[];
  pattern?: string;
}
export interface Item extends Document {
  key: string;
  title: string;
  probe?: string;
  required?: boolean;
  probe_args?: Record<string, string | number>;
}
export interface DirectPrompt extends Document {
  id: string;
  title: string;
  body: string;
  source?: {
    id: string;
    revision: string;
    template: TemplateInput;
    bindings: Record<string, string>;
  };
}
export interface PromptComparison {
  state: "current" | "changed" | "deleted";
  retained: DirectPrompt;
  candidate: DirectPrompt | null;
  reason?: string;
}
export interface Step extends Document {
  id: string;
  title: string;
  brief?: string;
  prompts?: DirectPrompt[];
  actor: { kind: string; engine?: string; model?: string; label?: string };
  after?: string[];
  memory?: string;
  outputs?: string[];
  checklist?: Item[];
  rework?: { to: string; when: string; max_rounds: number };
  distinct_from?: { step: string; constraint: string }[];
  /** Format 5 (#1654): what the step needs; filled per mission from the pool. */
  role?: { skills: string[]; min_grade?: string };
  /** Format ≤4 advisory skill references. */
  skills?: { id: string; revision?: string; required?: boolean }[];
}
export interface Flow extends Document {
  format: number;
  title: string;
  description?: string;
  steps: Step[];
}
export interface Manifest extends Document {
  format: number;
  identity: {
    id: string;
    name: string;
    publisher: string;
    version: string;
    domain: string;
    summary?: string;
  };
  variables?: Variable[];
  flows?: { default?: string };
}
export interface Draft {
  files: Files;
  documents: Record<string, Document>;
  readme: string;
}
export interface AuthoringSchema {
  agents: string[];
  actors: string[];
  memory: string[];
  distinct: string[];
  variable_types: string[];
  /** Format 5 (#1654): the local skills library (offered by exact id), the grades a role may
   *  require, and the distinct-from constraints a format 5 step may demand. */
  skills?: { id: string; label: string }[];
  /** Set when the skills library could not be read: roles still edit, the picker is empty. */
  skills_unavailable?: string;
  grades?: string[];
  distinct_role?: string[];
  probes: Record<
    string,
    {
      outputs: string[];
      args: Record<
        string,
        {
          required: boolean;
          type: string;
          literal: boolean;
          variable_types: string[];
          slots: string[];
        }
      >;
    }
  >;
  limits: {
    flows: number;
    steps: number;
    items: number;
    prompts?: number;
    initial_instruction?: number;
    variables: number;
    distinct: number;
    rework_min: number;
    rework_max: number;
  };
}
export const MANIFEST = "playbook.toml";
/** Format 5 (#1654): agent steps name a role (skills + minimum grade), never an engine/model. */
export const ROLE_FORMAT = 5;
/** The one skill-id grammar (D14): role skills, the local library and the gallery. */
export const SKILL_ID = /^[a-z0-9][a-z0-9-]{0,63}$/;
export const stepKey = (path: string, id: string) => `${path}:${id}`;
/** The server's bound on a role's skills (`schema.MAX_SKILLS`). */
export const MAX_ROLE_SKILLS = 8;
/** A step an explicit upgrade changes: shown, with its references, before saving. An agent step
 *  gets a role; any other step LOSES its skill references (format 5 keeps skills only in roles). */
export interface UpgradeStep {
  key: string;
  path: string;
  flow: string;
  step: Step;
  agent: boolean;
}
/** Every step of a format ≤4 draft that the upgrade changes, in document order: each agent step,
 *  and each other step that lists skill references (#1654 per-reference confirm). */
export function upgradeSteps(draft: Draft): UpgradeStep[] {
  return Object.entries(draft.documents).flatMap(([path, doc]) =>
    path.startsWith("flows/")
      ? (doc as Flow).steps
          .filter(
            (step) => step.actor.kind === "agent" || (step.skills ?? []).length,
          )
          .map((step) => ({
            key: stepKey(path, step.id),
            path,
            flow: (doc as Flow).title || path,
            step,
            agent: step.actor.kind === "agent",
          }))
      : [],
  );
}

/** The skill references an upgrade proposes by default: REQUIRED, valid, unpinned or
 *  library-pinned ones. Advisory references are never promoted without a choice. */
export function defaultRoleSkills(step: Step): string[] {
  return (step.skills ?? [])
    .filter(
      (r) =>
        r.required &&
        SKILL_ID.test(r.id) &&
        (!r.revision || /^[0-9a-f]{64}$/.test(r.revision)),
    )
    .map((r) => r.id);
}

/** The explicit, whole-document upgrade to format 5 (#1654). Agent steps lose engine/model and gain
 *  a role of exactly the references the operator confirmed in `picks` (a step without picks gets
 *  none — nothing is promoted unseen); step skill lists are dropped (format 5 has none);
 *  `session` constraints are dropped (a different instance is always implied). Never called
 *  implicitly: only the Upgrade action does this. */
export function upgradeToRoles(
  draft: Draft,
  picks: Record<string, string[]>,
): Draft {
  const documents: Record<string, Document> = { ...draft.documents };
  const manifest = documents[MANIFEST] as Manifest;
  documents[MANIFEST] = {
    ...manifest,
    format: Math.max(ROLE_FORMAT, manifest.format),
  };
  for (const [path, doc] of Object.entries(draft.documents)) {
    if (!path.startsWith("flows/")) continue;
    const flow = doc as Flow;
    documents[path] = {
      ...flow,
      format: Math.max(ROLE_FORMAT, flow.format),
      steps: flow.steps.map((step) => {
        const rest: Step = { ...step };
        delete rest.skills;
        const distinct = (step.distinct_from ?? []).filter(
          (d) => d.constraint !== "session",
        );
        const next: Step = {
          ...rest,
          distinct_from: distinct.length ? distinct : undefined,
        };
        if (step.actor.kind === "agent") {
          next.actor = { kind: "agent" };
          next.role = step.role ?? {
            skills: picks[stepKey(path, step.id)] ?? [],
          };
        }
        if (next.distinct_from === undefined) delete next.distinct_from;
        return next;
      }),
    };
  }
  return { ...draft, documents };
}
export const flowPath = (id: string) => `flows/${id}.toml`;
export const flowId = (path: string) => path.slice(6, -5);
/** Opt into retained prompts only on an explicit instruction edit; legacy edits keep format. */
export function withStep(draft: Draft, path: string, step: Step): Draft {
  const flow = draft.documents[path] as Flow;
  const manifest = draft.documents[MANIFEST] as Manifest;
  const formats = step.prompts !== undefined;
  return {
    ...draft,
    documents: {
      ...draft.documents,
      [MANIFEST]: formats
        ? { ...manifest, format: Math.max(4, manifest.format) }
        : manifest,
      [path]: {
        ...flow,
        format: formats ? Math.max(4, flow.format) : flow.format,
        steps: flow.steps.map((old) => (old.id === step.id ? step : old)),
      },
    },
  };
}
export function freshId(prefix: string) {
  return `${prefix}-${crypto.randomUUID().slice(0, 8)}`;
}
export function newStep(): Step {
  return {
    id: freshId("step"),
    title: "New step",
    actor: { kind: "operator" },
    checklist: [
      {
        key: freshId("check"),
        title: "Confirm the step is complete",
        probe: "supervisor_judged",
      },
    ],
  };
}
export function newDraft(): Draft {
  return {
    files: {},
    readme: "",
    documents: {
      [MANIFEST]: {
        format: ROLE_FORMAT,
        identity: {
          id: freshId("playbook"),
          name: "",
          publisher: "Local",
          version: "0.1.0",
          domain: "development",
          summary: "",
        },
        flows: { default: "main" },
      },
      [flowPath("main")]: {
        format: ROLE_FORMAT,
        title: "Main flow",
        steps: [newStep()],
      },
    },
  };
}
export function fromDetail(pb: PlaybookDetail): Draft {
  return structuredClone({
    files: pb.files ?? {},
    documents: pb.documents ?? {},
    readme: pb.readme ?? "",
  });
}
export const same = (a: unknown, b: unknown) =>
  JSON.stringify(a) === JSON.stringify(b);
export function draftFiles(draft: Draft, baseline: Draft): Files {
  const files = { ...draft.files };
  for (const path of Object.keys(baseline.documents)) {
    if (!(path in draft.documents)) delete files[path];
  }
  for (const [path, doc] of Object.entries(draft.documents)) {
    if (!(path in files) || !same(doc, baseline.documents[path]))
      files[path] = { toml: doc };
  }
  if (
    draft.readme !== baseline.readme ||
    (!("README.md" in files) && draft.readme)
  )
    files["README.md"] = draft.readme;
  return files;
}
export function ancestors(steps: Step[], id: string): Set<string> {
  const found = new Set<string>();
  const visit = (sid: string) => {
    for (const parent of steps.find((s) => s.id === sid)?.after ?? []) {
      if (parent === id || found.has(parent)) continue;
      found.add(parent);
      visit(parent);
    }
  };
  visit(id);
  return found;
}
export function differences(
  draft: Draft,
  current: Draft,
): { path: string; mine: string; current: string }[] {
  const paths = new Set([
    ...Object.keys(draft.files),
    ...Object.keys(current.files),
    ...Object.keys(draft.documents),
    ...Object.keys(current.documents),
  ]);
  paths.add("README.md");
  function source(d: Draft, path: string): string {
    const file = d.files[path];
    if (file === undefined) return "(absent)";
    if (typeof file === "string") return file;
    if ("toml" in file) return JSON.stringify(file.toml, null, 2);
    const bytes = Uint8Array.from(atob(file.base64), (char) =>
      char.charCodeAt(0),
    );
    return `Binary file: ${bytes.length} bytes\nSHA-256: ${bytesToHex(sha256(bytes))}`;
  }
  function view(d: Draft, path: string, fields: boolean, raw: boolean): string {
    if (path === "README.md") return d.readme;
    if (fields && d.documents[path]) {
      const parsed = JSON.stringify(d.documents[path], null, 2);
      return raw
        ? `Fields:\n${parsed}\n\nSource TOML (before any draft field edits):\n${source(d, path)}`
        : parsed;
    }
    return source(d, path);
  }
  return [...paths]
    .filter((path) =>
      path === "README.md"
        ? draft.readme !== current.readme
        : !same(draft.documents[path], current.documents[path]) ||
          !same(draft.files[path], current.files[path]),
    )
    .sort()
    .map((path) => {
      // Raw changes and field changes are independent: show both when both changed, so
      // a concurrent comment is visible even when the document's fields also differ.
      const fields = !same(draft.documents[path], current.documents[path]);
      const raw = !same(draft.files[path], current.files[path]);
      return {
        path,
        mine: view(draft, path, fields, raw),
        current: view(current, path, fields, raw),
      };
    });
}
