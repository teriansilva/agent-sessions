/** Pure view logic for a structured (native API) session (#1311). No React, no fetch: the pane
 *  renders these answers, and the tests pin them. */
import type {
  StructuredAgentTurn,
  StructuredGrant,
  StructuredRequest,
  StructuredSnapshot,
  StructuredTurn,
} from "../../types/api";

const messageTime = new Intl.DateTimeFormat(undefined, {
  year: "numeric", month: "short", day: "numeric", hour: "2-digit", minute: "2-digit",
});
const fullMessageTime = new Intl.DateTimeFormat(undefined, {
  year: "numeric", month: "long", day: "numeric", hour: "2-digit", minute: "2-digit",
  second: "2-digit", timeZoneName: "short",
});

/** A stored message time in the browser's locale/timezone; unknown is never "now". */
export function messageTimestamp(epoch: number | null | undefined, kind: "user" | "assistant") {
  if (typeof epoch !== "number" || !Number.isFinite(epoch) || epoch < 0) return null;
  const date = new Date(epoch * 1000);
  if (!Number.isFinite(date.getTime())) return null;
  return {
    iso: date.toISOString(),
    compact: messageTime.format(date),
    label: `${kind === "user" ? "Message submitted" : "Response started"}: ${fullMessageTime.format(date)}`,
  };
}

/** A turn the agent ran for itself (#1438), never a message someone sent. */
export function isAgentTurn(t: StructuredTurn | StructuredAgentTurn): t is StructuredAgentTurn {
  return t.kind === "agent";
}

/** The operator's conversation: every turn someone sent. */
export function operatorTurns(snap: StructuredSnapshot | null): StructuredTurn[] {
  return (snap?.turns ?? []).filter((t): t is StructuredTurn => !isAgentTurn(t));
}

/** The turn the agent is running for itself right now, on a live worker (#1438). */
export function runningAgentTurn(snap: StructuredSnapshot | null): StructuredAgentTurn | null {
  if (!snap?.native?.worker) return null;
  const last = [...snap.turns].reverse().find(isAgentTurn);
  return last?.state === "running" ? last : null;
}

/** The operator's message is with the agent but has not started: the agent runs a turn of its own
 *  first, one it took from its own queue ahead of the message (#1475). Only then can both be
 *  live at once, since the message's start ends any turn of the agent's own. Nothing is offered to
 *  interrupt: the message is not running yet, and the agent's interrupt stops whatever runs when
 *  it arrives (its own turn now, the message the moment that turn ends). */
export function waitingBehindOwnTurn(snap: StructuredSnapshot | null): boolean {
  if (!snap?.active_turn || !runningAgentTurn(snap)) return false;
  // A message that already shows its reply or work has started, whatever a stale record says.
  const active = operatorTurns(snap).find((t) => t.turn_id === snap.active_turn);
  return !!active && !active.reply && active.tools.length === 0;
}

/** Can Interrupt (and Esc) stop this turn the agent runs for itself? Only the live worker's own
 *  target, from a worker that takes the request directly (#1438). */
export function ownTurnInterruptible(snap: StructuredSnapshot | null, turn: StructuredAgentTurn): boolean {
  return (
    turn.state === "running" &&
    !snap?.active_turn &&
    !!snap?.native?.interrupt_own &&
    snap.native.send_now_background === "interrupt" &&
    snap.native.background_turn === turn.turn_id
  );
}

/** What Esc stops: the operator turn running now, else the agent's own interruptible turn. */
export function interruptTarget(snap: StructuredSnapshot | null): string | null {
  if (!snap) return null;
  if (snap.active_turn) {
    if (waitingBehindOwnTurn(snap)) return null;
    const active = operatorTurns(snap).find((t) => t.turn_id === snap.active_turn);
    return active && (active.state === "running" || active.state === "awaiting_approval") ? active.turn_id : null;
  }
  const own = runningAgentTurn(snap);
  return own && ownTurnInterruptible(snap, own) ? own.turn_id : null;
}

/** The agent is working with no turn shown running: a background task (#1430). A turn it runs for
 *  itself is shown as that turn (#1438), not as this row. Only a live worker can be working: the
 *  flag is replayed from the journal, so an exited one's last word must not keep claiming it. */
export function backgroundWorking(snap: StructuredSnapshot | null): boolean {
  return (
    !!snap &&
    !snap.active_turn &&
    !!snap.native?.worker &&
    !!snap.native.background_active &&
    !runningAgentTurn(snap)
  );
}

/** What Send now would stop or steer: the operator turn running now, else a turn the agent
 *  runs for itself that this worker can interrupt (#1427). Null: nothing a queued message can
 *  go past right now. */
export function sendNowTarget(
  snap: StructuredSnapshot | null,
): { turnId: string; mode: "steer" | "interrupt" } | null {
  if (!snap) return null;
  if (snap.active_turn && waitingBehindOwnTurn(snap)) return null;
  if (snap.active_turn) return snap.native?.send_now ? { turnId: snap.active_turn, mode: snap.native.send_now } : null;
  const native = snap.native;
  if (native?.background_turn && native.send_now_background === "interrupt") {
    return { turnId: native.background_turn, mode: "interrupt" };
  }
  return null;
}

/** Turn states during which the agent is still working and the view keeps reading. */
export function isActive(snap: StructuredSnapshot | null): boolean {
  return (
    !!snap &&
    (snap.state === "running" ||
      snap.state === "awaiting_approval" ||
      snap.state === "queued" ||
      snap.state === "delivering" ||
      // The agent's own turn reads live too: its latest call is the summary line (#1438).
      !!runningAgentTurn(snap))
  );
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

/** Claude's `AskUserQuestion` (#1463): a question for the operator, never a permission. */
export const QUESTION_TOOL = "AskUserQuestion";

export function isQuestion(req: StructuredRequest): boolean {
  return req.kind === QUESTION_TOOL;
}

// eslint-disable-next-line no-control-regex -- stripping control bytes is the point
const CONTROL = /[\u0000-\u0008\u000b-\u001f\u007f]/g;
/** Display text, whole: what the operator answers on is never cut (Hermes #6477). */
const display = (value: unknown) =>
  typeof value === "string" ? value.replace(CONTROL, "").trim() : "";

/** How an agent question is answered from its card (#1463): `exact` sends the answers in the
 *  shape Claude asked for; `message` stops the pending question and sends the picks as the
 *  operator's next message — for a session whose worker predates exact answers, or a question
 *  set Claude couldn't take back exactly (repeated questions, text over the caps). Either way a
 *  readable question is always answerable; `null` only when there is nothing to pick from. */
export function questionAnswerMode(req: StructuredRequest): "exact" | "message" | null {
  if (!questionPreview(req)?.some((q) => q.options.length)) return null;
  if (req.choices.includes("answer")) return "exact";
  return req.choices.includes("cancel") ? "message" : null;
}

/** The questions a request offers as choices, as display text. Labels stay verbatim. */
export function requestQuestions(req: StructuredRequest) {
  if (!questionAnswerMode(req)) return null;
  const questions = questionPreview(req);
  return questions?.length ? questions : null;
}

/** The operation id of a question's `message`-mode answer: derived from the request itself, so
 *  every send for this question — a retry, after a reload, from another tab — is the SAME
 *  operation and the server keeps one (Hermes #6495). An idempotency key, not a secret. */
export function questionAnswerId(req: StructuredRequest, attempt = 0): string {
  const key = `answer\u0000${req.turn_id}\u0000${req.request_id}\u0000${req.payload_digest ?? ""}\u0000${attempt}`;
  const words = [0x811c9dc5, 0x01000193, 0x9e3779b9, 0x85ebca6b].map((seed) => {
    let h = seed >>> 0;
    for (let i = 0; i < key.length; i++) {
      h ^= key.charCodeAt(i);
      h = Math.imul(h, 0x01000193) >>> 0;
    }
    return h.toString(16).padStart(8, "0");
  });
  const hex = words.join("");
  // A canonical v8 (custom) UUID: version nibble 8, RFC 4122 variant.
  const variant = ((parseInt(hex[16], 16) & 0x3) | 0x8).toString(16);
  return `${hex.slice(0, 8)}-${hex.slice(8, 12)}-8${hex.slice(13, 16)}-${variant}${hex.slice(17, 20)}-${hex.slice(20, 32)}`;
}

/** Every id a question's message answer may go under: the derived one, then deterministic
 *  fallbacks for an id the server proved dead. Deterministic, so a reload or another tab still
 *  recognises an answer sent under any of them (independent review of #1490). */
export const ANSWER_ATTEMPTS = 4;
export function questionAnswerIds(req: StructuredRequest): string[] {
  return Array.from({ length: ANSWER_ATTEMPTS }, (_, n) => questionAnswerId(req, n));
}

/** The operator's answers as the message the agent reads next (`message` mode). */
export function questionAnswerMessage(
  questions: readonly { title: string }[],
  answers: readonly { selected: string[]; other?: string }[],
): string {
  const lines = questions.map((q, i) => {
    const a = answers[i];
    const text = a?.other?.trim() ? a.other.trim() : (a?.selected ?? []).join(", ");
    return `- ${q.title}\n  → ${text}`;
  });
  return `My answers to your questions:\n${lines.join("\n")}`;
}

/** A question request read as text, answerable or not (#1463): what the card shows instead of
 *  raw JSON when BattleLab can't offer it as choices. Display only. */
export function questionPreview(req: StructuredRequest) {
  if (!isQuestion(req)) return null;
  const questions = object(object(req.payload).input).questions;
  // No rows to show is still a question: the card explains it, never prints raw JSON.
  if (!Array.isArray(questions)) return [];
  return questions.map((raw) => {
    const q = object(raw);
    return {
      title: String(q.question ?? ""),
      header: display(q.header) || undefined,
      multi: q.multiSelect === true,
      options: (Array.isArray(q.options) ? q.options : []).map((o) => ({
        label: String(object(o).label ?? ""),
        description: display(object(o).description) || undefined,
      })),
    };
  });
}

/** One answer as the decide route takes it: the chosen labels, or the operator's own words. */
export function answerWire(a: { selected: string[]; other?: string }) {
  return a.other?.trim() ? { other: a.other } : { selected: [...a.selected] };
}

export function requestTitle(req: StructuredRequest, agent: string): string {
  if (isQuestion(req)) {
    return questionAnswerMode(req)
      ? `${agent} is waiting for your answer`
      : `${agent} asks a question BattleLab can’t show as choices`;
  }
  if (req.kind === "command") return `${agent} asks to run a command`;
  if (req.kind === "file_change") return `${agent} proposes a file change`;
  const payload = object(req.payload);
  const call = object(payload.toolCall);
  if (call.kind === "execute") return `${agent} asks to run a command`;
  if (typeof call.title === "string" && call.title.trim()) return `${agent} asks to use ${call.title}`;
  return `${agent} asks to use ${req.kind}`;
}

function object(value: unknown): Record<string, unknown> {
  return value && typeof value === "object" && !Array.isArray(value) ? value as Record<string, unknown> : {};
}

/** Short action facts. The complete, unchanged payload remains under Details. */
export function requestSummary(req: StructuredRequest, folder?: string) {
  const payload = object(req.payload);
  const call = object(payload.toolCall);
  const context = object(payload.toolContext);
  const input = object(context.rawInput ?? call.rawInput ?? payload.input);
  const command = payload.command ?? input.command;
  const action = typeof command === "string" ? command : Array.isArray(command) ? command.map(String).join(" ")
    : Object.keys(input).length ? Object.entries(input).map(([k, v]) => `${humanize(k)}: ${typeof v === "string" ? v : json(v)}`).join("\n")
      : typeof call.title === "string" ? call.title : typeof payload.tool_name === "string" ? payload.tool_name : req.kind;
  return {
    action,
    folder: typeof input.cwd === "string" ? input.cwd : typeof payload.cwd === "string" ? payload.cwd : folder,
    reason: typeof payload.declineOnly === "string" ? payload.declineOnly : typeof payload.reason === "string" ? payload.reason : null,
  };
}

/** A file change the server will not let anyone approve (#1310): review and decline only. */
export function reviewOnly(req: StructuredRequest): boolean {
  return !req.choices.includes("approve");
}

/** The button text for one server-offered choice. "Approve always" (#1339) offers only the
 *  snapshot's own `always` grants, as buttons or a list (`inlineGrants`); the view invents none. */
export function choiceLabel(choice: string, req: StructuredRequest): string {
  if (isQuestion(req)) {
    if (choice === "answer") return "Send answer";
    if (choice === "reject") return "Skip question";
    if (choice === "cancel") return "Stop the turn";
  }
  const tool = req.kind !== "command" && req.kind !== "file_change";
  if (choice === "approve") return tool ? "Allow once" : "Approve once";
  if (choice === "always") return tool ? "Allow always" : "Approve always";
  if (choice === "reject") return req.kind === "file_change" ? "Decline" : tool ? "Deny" : "Reject";
  if (choice === "cancel") return "Reject and stop the turn";
  return choice;
}

/** The grants a card offers as their own one-click buttons, session-long first; null when they
 *  need the list instead. A button is named only by how long its grant lasts (the command is
 *  already on the card), so that has to tell them apart: one grant, or Codex's session + exec
 *  policy pair. Two of one scope still open the list, where each full label is the choice. */
export function inlineGrants(always: StructuredGrant[]): StructuredGrant[] | null {
  const scopes = always.map((g) => g.scope);
  if (always.length === 1) return always;
  if (always.length !== 2 || !scopes.includes("session") || !scopes.includes("persistent")) return null;
  return always[0].scope === "session" ? always : [always[1], always[0]];
}

/** The button text for one inline grant. */
export function grantChoiceLabel(grant: StructuredGrant, req: StructuredRequest): string {
  if (grant.scope !== "session") return choiceLabel("always", req);
  return req.kind !== "command" && req.kind !== "file_change" ? "Allow for session" : "Approve for session";
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

/** Each queued operator message's place in line, 1 = next (#1442). The snapshot keeps every
 *  message the live worker still holds queued (#1439), so the first one shown is the head. */
export function queuePositions(snap: StructuredSnapshot | null): Map<string, number> {
  const out = new Map<string, number>();
  for (const t of operatorTurns(snap)) if (t.state === "queued") out.set(t.turn_id, out.size + 1);
  return out;
}

export function ordinal(n: number): string {
  const tens = n % 100;
  const suffix = tens >= 11 && tens <= 13 ? "th" : ({ 1: "st", 2: "nd", 3: "rd" } as Record<number, string>)[n % 10] ?? "th";
  return `${n}${suffix}`;
}

/** Was this turn cut off by Send now (#1442)? Only when it settled interrupted AND the message
 *  whose Send now named it has actually GONE OUT: "your next message is answered below" is a
 *  claim about that message. While it still waits (queued, `interrupting`) the turn reads plainly
 *  interrupted (Hermes on #1451). The server keeps the link on a dispatched message only when both
 *  its control and the message itself were sent and not refused; a steer never cuts anything. */
export function cutBySendNow(snap: StructuredSnapshot | null, turn: StructuredTurn): boolean {
  return (
    turn.state === "interrupted" &&
    operatorTurns(snap).some(
      (q) =>
        q.delivery_turn_id === turn.turn_id &&
        q.delivery === undefined &&
        !["queued", "failed", "uncertain", "unavailable"].includes(q.state),
    )
  );
}

/** The turn whose question still takes an answer from its buttons (#1463): the newest turn
 *  carrying `questions`, while no operator message follows it. The agent reads the next message as
 *  the answer, so a later one (queued included) has already answered it or moved on. */
export function openQuestionTurn(snap: StructuredSnapshot | null): string | null {
  const turns = snap?.turns ?? [];
  for (let i = turns.length - 1; i >= 0; i--) {
    const t = turns[i];
    if (t.questions?.length) return t.turn_id;
    if (!isAgentTurn(t)) return null;
  }
  return null;
}

/** The message that answers an async question: the chosen option's own words for one question,
 *  one `title: option` line per question for several. */
export function answerText(
  questions: readonly { title: string }[],
  picks: readonly (string | undefined)[],
): string {
  if (questions.length === 1) return picks[0] ?? "";
  return questions.map((q, i) => `${q.title}: ${picks[i] ?? ""}`).join("\n");
}
