/** How a `supervisor_judged` objective reads (#1088). Pure, so every state is a table test.
 *
 *  A JUDGMENT is not an OBSERVATION, and the console never lets the two look alike: a row the
 *  supervisor judged reads `judged met (0.93)` with a `judged` tag and its quoted evidence one tap
 *  away; a row a probe settled reads `met 14:02` with an `observed` tag. Nothing here reads a field
 *  as markup — every quote is rendered by the caller as a plain React text node.
 *
 *  The server's record (`observed`) is untyped JSON, so every field is checked rather than assumed:
 *  a shape this file does not recognise degrades to "not judged yet", never to "met". */
import type { MissionObjective } from "../../types/api";

export const JUDGED_PROBE = "supervisor_judged";

/** How the checklist editor and the launch confirmation NAME a probe kind (#1088 carry-forward
 *  73023). The ids stay what the server stores; this is only what a person reads. */
export const PROBE_LABELS: Record<string, string> = {
  none: "Not checked (a note)",
  supervisor_judged: "Supervisor judges",
  git_local: "Git: branch exists",
  forge_pr: "Forge: PR is open",
  forge_checks: "Forge: checks are green",
  forge_review: "Forge: reviewed",
  forge_merged: "Forge: merged",
  forge_run: "Forge: workflow run",
  http_status: "HTTP: status",
  http_revision: "HTTP: revision is live",
};

export function probeLabel(kind: string): string {
  return PROBE_LABELS[kind] ?? kind;
}

export interface Evidence {
  source: string;
  quote: string;
}

export interface Verdict {
  kind: "verdict";
  /** The model said met. */
  met: boolean;
  confidence: number;
  threshold: number;
  /** Counts toward completion: met, at or above the threshold, current, not rejected. */
  counts: boolean;
  evidence: Evidence[];
  reason: string;
  checkedAt: number | null;
  /** The session output changed since this judgment. */
  stale: boolean;
  staleReason: string;
  /** Why it is stale, as the server's closed value — never read out of the prose. */
  staleKind: "criterion" | "input" | "unknown" | null;
  /** The operator rejected it ("Not met — judge again"). */
  rejected: boolean;
}

export interface Unknown {
  kind: "unknown";
  reason: string;
  at: number | null;
}

export type Judged = Verdict | Unknown | { kind: "none" };

function num(v: unknown): number | null {
  return typeof v === "number" && Number.isFinite(v) ? v : null;
}

function str(v: unknown): string {
  return typeof v === "string" ? v : "";
}

export function isJudged(o: Pick<MissionObjective, "probe">): boolean {
  return o.probe === JUDGED_PROBE;
}

/** The judged state of one row, from the server's record. `null` ⇒ not a judged row at all. */
export function judgmentOf(o: MissionObjective): Judged | null {
  if (!isJudged(o)) return null;
  const obs = o.observed;
  if (!obs || typeof obs !== "object") return { kind: "none" };
  const rec = (obs as Record<string, unknown>).judged;
  const at = num((obs as Record<string, unknown>).at);
  const stale = (obs as Record<string, unknown>).stale === true;
  if (!rec || typeof rec !== "object") {
    return stale
      ? { kind: "unknown", reason: str((obs as Record<string, unknown>).reason), at }
      : { kind: "none" };
  }
  const r = rec as Record<string, unknown>;
  const confidence = num(r.confidence);
  const threshold = num(r.threshold);
  if (confidence === null || threshold === null || typeof r.met !== "boolean") {
    // An ATTEMPT record (no verdict): the reason is the server's sentence.
    return { kind: "unknown", reason: str((obs as Record<string, unknown>).reason), at };
  }
  const evidence: Evidence[] = Array.isArray(r.evidence)
    ? (r.evidence as unknown[]).flatMap((q) => {
        if (!q || typeof q !== "object") return [];
        const source = str((q as Record<string, unknown>).source);
        const quote = str((q as Record<string, unknown>).quote);
        return source && quote ? [{ source, quote }] : [];
      })
    : [];
  const rejected = typeof (obs as Record<string, unknown>).rejected_at === "number";
  return {
    kind: "verdict",
    met: r.met,
    confidence,
    threshold,
    counts: !stale && !rejected && (obs as Record<string, unknown>).value === true,
    evidence,
    reason: str((obs as Record<string, unknown>).detail),
    checkedAt: num(r.checked_at) ?? at,
    stale,
    staleReason: stale ? str((obs as Record<string, unknown>).reason) : "",
    staleKind: stale ? staleKindOf((obs as Record<string, unknown>).stale_kind) : null,
    rejected,
  };
}

function staleKindOf(v: unknown): Verdict["staleKind"] {
  return v === "criterion" || v === "input" || v === "unknown" ? v : null;
}

/** The state word on the row's meta line.
 *
 *  A row the store still holds as `met` but whose latest record is not a verdict — the last attempt
 *  failed, or the record is unreadable — does NOT count toward completion, so it must not read
 *  "met" to anyone, sighted or not (#1097 review). It says it was judged before and is not now. */
export function judgedStateWord(j: Judged, fallback: string, state?: string): string {
  // THE OPERATOR'S WAIVER WINS (#1097 review 5040, finding 5). A waived objective no longer needs
  // a judgment at all; the old one stays as history on the evidence panel, never as the state.
  if (state === "waived") return fallback;
  if (j.kind !== "verdict") {
    return state === "met" ? "judged earlier · not current" : fallback;
  }
  const c = j.confidence.toFixed(2);
  if (j.rejected) return `judged met (${c}) · rejected`;
  if (j.stale) return `judged ${j.met && j.confidence >= j.threshold ? "met" : "not yet"} (${c})`;
  return j.counts ? `judged met (${c})` : `judged not yet (${c})`;
}

/** `transcript · claude:5f3c…a1`, `diff`. The label is the server's; only its shape is reformatted. */
export function sourceLabel(source: string): string {
  const i = source.indexOf(":");
  if (i < 0) return source;
  const kind = source.slice(0, i);
  const session = source.slice(i + 1);
  const j = session.indexOf(":");
  const engine = j < 0 ? "" : session.slice(0, j);
  const id = j < 0 ? session : session.slice(j + 1);
  const short = id.length > 10 ? `${id.slice(0, 4)}…${id.slice(-2)}` : id;
  return `${kind} · ${engine ? `${engine}:` : ""}${short}`;
}

/** The server's reason when a judged row cannot be judged for want of an endpoint. */
export const NO_ENDPOINT_REASON = "cannot be judged — no AI endpoint is configured";
