/** One complete in-memory bundle. Unedited files retain their original spelling and bytes. */
import type { PlaybookDetail } from "../../types/playbooks";
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
export interface Step extends Document {
  id: string;
  title: string;
  brief?: string;
  actor: { kind: string; engine?: string; model?: string; label?: string };
  after?: string[];
  memory?: string;
  outputs?: string[];
  checklist?: Item[];
  rework?: { to: string; when: string; max_rounds: number };
  distinct_from?: { step: string; constraint: string }[];
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
    variables: number;
    distinct: number;
    rework_min: number;
    rework_max: number;
  };
}
export const MANIFEST = "playbook.toml";
export const flowPath = (id: string) => `flows/${id}.toml`;
export const flowId = (path: string) => path.slice(6, -5);
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
        format: 2,
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
      [flowPath("main")]: { format: 2, title: "Main flow", steps: [newStep()] },
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
    const bytes = Uint8Array.from(atob(file.base64), (char) => char.charCodeAt(0));
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
