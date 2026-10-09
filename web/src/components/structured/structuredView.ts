/** Pure view logic for a structured (native API) session (#1311). No React, no fetch: the pane
 *  renders these answers, and the tests pin them. */
import type { StructuredRequest, StructuredSnapshot } from "../../types/api";

/** Turn states during which the agent is still working and the view keeps reading. */
export function isActive(snap: StructuredSnapshot | null): boolean {
  return !!snap && (snap.state === "running" || snap.state === "awaiting_approval" || snap.state === "queued");
}

/** One row of a request card. EVERY top-level field of the payload becomes a row: the operator
 *  must see everything a decision answers (#1310's digest binds exactly these bytes), so nothing
 *  is dropped for being unfamiliar — an unknown field renders as JSON. */
export interface RequestRow {
  key: string;
  label: string;
  kind: "text" | "code" | "json" | "patch" | "suggestions";
  text: string;
  files?: { path: string; diff: string }[];
}

const LABELS: Record<string, string> = {
  command: "Command",
  cwd: "Folder",
  reason: "Reason",
  title: "Title",
  tool_name: "Tool",
  input: "Input",
  blocked_path: "Path",
  decision_reason: "Why asked",
  permission_suggestions: "Suggested rules",
  networkApprovalContext: "Network",
  proposedExecpolicyAmendment: "Rule changes",
  changes: "Changes",
  grantRoot: "Grant root",
};

// Shown first, in this order, when present; everything else follows alphabetically.
const ORDER = [
  "title",
  "tool_name",
  "command",
  "input",
  "changes",
  "cwd",
  "blocked_path",
  "reason",
  "decision_reason",
  "networkApprovalContext",
  "proposedExecpolicyAmendment",
  "permission_suggestions",
];

// Correlation fields: shown in the request footer, not as content rows.
const PLUMBING = new Set(["threadId", "turnId", "itemId", "tool_use_id", "request_id", "callId"]);

function humanize(key: string): string {
  return LABELS[key] ?? key.replace(/_/g, " ").replace(/([a-z])([A-Z])/g, "$1 $2");
}

function json(value: unknown): string {
  return JSON.stringify(value, null, 2) ?? String(value);
}

function patchFiles(value: unknown): { path: string; diff: string }[] | null {
  if (!Array.isArray(value)) return null;
  const files = value.filter(
    (c): c is { path: string; diff: string } =>
      !!c && typeof c === "object" && typeof (c as { path?: unknown }).path === "string" &&
      typeof (c as { diff?: unknown }).diff === "string",
  );
  return files.length === value.length ? files : null;
}

/** The request as rows. A payload that is not an object (an over-long request the server could
 *  only present as text) is one text row — still the whole of what was presented. */
export function requestRows(req: StructuredRequest): RequestRow[] {
  const payload = req.payload;
  if (payload === undefined || payload === null) {
    return req.summary ? [{ key: "summary", label: "Request", kind: "code", text: req.summary }] : [];
  }
  if (typeof payload !== "object" || Array.isArray(payload)) {
    return [{ key: "request", label: "Request", kind: "code", text: typeof payload === "string" ? payload : json(payload) }];
  }
  const obj = payload as Record<string, unknown>;
  const keys = Object.keys(obj).filter((k) => !PLUMBING.has(k));
  keys.sort((a, b) => {
    const ia = ORDER.indexOf(a);
    const ib = ORDER.indexOf(b);
    if (ia !== -1 || ib !== -1) return (ia === -1 ? 99 : ia) - (ib === -1 ? 99 : ib);
    return a.localeCompare(b);
  });
  return keys.map((key): RequestRow => {
    const value = obj[key];
    const label = humanize(key);
    if (key === "changes") {
      const files = patchFiles(value);
      if (files) return { key, label, kind: "patch", text: "", files };
    }
    if (key === "permission_suggestions") return { key, label, kind: "suggestions", text: json(value) };
    if (value === null || value === undefined) return { key, label, kind: "text", text: "none" };
    if (key === "command") {
      return { key, label, kind: "code", text: Array.isArray(value) ? value.map(String).join(" ") : typeof value === "string" ? value : json(value) };
    }
    if (typeof value === "string") return { key, label, kind: key === "cwd" || key === "blocked_path" ? "code" : "text", text: value };
    if (typeof value === "number" || typeof value === "boolean") return { key, label, kind: "text", text: String(value) };
    return { key, label, kind: "json", text: json(value) };
  });
}

/** Correlation ids for the request footer (shown, never hidden). */
export function requestIds(req: StructuredRequest): string[] {
  const obj = req.payload && typeof req.payload === "object" && !Array.isArray(req.payload) ? (req.payload as Record<string, unknown>) : {};
  return Object.keys(obj)
    .filter((k) => PLUMBING.has(k))
    .map((k) => `${humanize(k)} ${String(obj[k])}`);
}

export function requestTitle(req: StructuredRequest, agent: string): string {
  if (req.kind === "command") return `${agent} asks to run a command`;
  if (req.kind === "file_change") return `${agent} proposes a file change`;
  return `${agent} asks to use ${req.kind}`;
}

/** A file change the server will not let anyone approve (#1310): review and decline only. */
export function reviewOnly(req: StructuredRequest): boolean {
  return !req.choices.includes("approve");
}

/** The button text for one server-offered choice. "Approve always" (#1339) lists only the
 *  snapshot's own `always` grants; the view invents none. */
export function choiceLabel(choice: string, req: StructuredRequest): string {
  const tool = req.kind !== "command" && req.kind !== "file_change";
  if (choice === "approve") return tool ? "Allow once" : "Approve once";
  if (choice === "always") return tool ? "Allow always" : "Approve always";
  if (choice === "reject") return req.kind === "file_change" ? "Decline" : tool ? "Deny" : "Reject";
  if (choice === "cancel") return "Reject and stop the turn";
  return choice;
}

/** The operation id for a send: reused while the text is the same (a retry after an unknown
 *  outcome is then a no-op on the server), new as soon as the text changes. `text` is the
 *  send's whole identity — `sendIdentity` folds the attached pictures in (#1332 Phase 3). */
export function operationFor(
  prev: { id: string; text: string } | null,
  text: string,
  mint: () => string,
): { id: string; text: string } {
  return prev && prev.text === text ? prev : { id: mint(), text };
}

export function turnStateLabel(state: string): string {
  switch (state) {
    case "running":
      return "working";
    case "awaiting_approval":
      return "waiting for your decision";
    case "interrupted":
      return "interrupted";
    case "uncertain":
      return "outcome unknown";
    case "failed":
      return "failed";
    default:
      return state;
  }
}

/** A send's identity for `operationFor`: its words AND its pictures, so attaching or removing a
 *  picture mints a new operation id exactly as editing the text does (#1332 Phase 3). */
export function sendIdentity(text: string, attachments: readonly string[]): string {
  return JSON.stringify([text, attachments]);
}
