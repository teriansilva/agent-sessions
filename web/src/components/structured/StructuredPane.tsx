import {
  ArrowLeftRight,
  BookMarked,
  Globe,
  History,
  Mic,
  PanelRight,
  Paperclip,
  ScrollText,
  Send,
  Share2,
  ShieldOff,
  Square,
  SquareDashedBottom,
  X,
  Zap,
} from "lucide-react";
import { Fragment, useCallback, useEffect, useLayoutEffect, useRef, useState } from "react";
import type { ClipboardEvent as ReactClipboardEvent, KeyboardEvent, ReactNode } from "react";
import { createPortal } from "react-dom";
import { engineInfo, engineLabel, isAgent, useEngineRoster } from "../../app/engineRoster";
import { isNewSessionPlaceholder } from "../../app/sessionsStore";
import { useSessionRow } from "../../app/useSessionRow";
import { ApiError, api } from "../../lib/api";
import {
  filesFromData,
  imageFilesFromAsyncClipboard,
  imageFilesFromData,
} from "../../lib/clipboardImages";
import {
  appendSent,
  confirmOperation,
  confirmSent,
  readSent,
  SENT_HISTORY_EVENT,
  SENT_HISTORY_KEY,
  type SentMessage,
} from "../../lib/sentHistory";
import { substituteFields, uploadStoredName } from "../../lib/templateMessage";
import { shareLink } from "../../lib/shareLink";
import type { TermStatus } from "../../lib/termSocket";
import { useIsMobile } from "../../lib/useIsMobile";
import { HandoffModal } from "../terminal/HandoffModal";
import { HeadActions, type HeadAction } from "../terminal/HeadActions";
import { HeadFacts } from "../terminal/HeadFacts";
import { KeyBar, type KeyAction } from "../terminal/KeyBar";
import type { PaneHost } from "../terminal/paneHost";
import { SessionRecapModal } from "../terminal/SessionRecapModal";
import { SharePubliclyModal } from "../shares/SharePubliclyModal";
import { SentMessagesModal } from "../terminal/SentMessagesModal";
import { TemplatePickerModal } from "../templates/TemplatePickerModal";
import { UploadImage } from "../templates/UploadImage";
import { useDictation } from "../terminal/useDictation";
import compose from "../terminal/Compose.module.css";
import term from "../terminal/Terminal.module.css";
import type {
  Containment,
  Template,
  StructuredRequest,
  StructuredAgentTurn,
  StructuredRisk,
  StructuredSnapshot,
  StructuredTool,
  StructuredTurn,
} from "../../types/api";
import chat from "../chat/ChatPane.module.css";
import { Markdown } from "./Markdown";
import { BackgroundWork } from "./BackgroundWork";
import { autoSendNowHead, useAlwaysSendNow } from "./alwaysSendNow";
import { conversationGroups, type ConversationGroup } from "./conversationOrder";
import { PermissionsDialog } from "./PermissionsDialog";
import styles from "./StructuredPane.module.css";
import {
  answerText,
  answerWire,
  backgroundWorking,
  choiceLabel,
  cutBySendNow,
  grantChoiceLabel,
  inlineGrants,
  ordinal,
  queuePositions,
  interruptTarget,
  isActive,
  isAgentTurn,
  operatorTurns,
  ownTurnInterruptible,
  operationFor,
  requestIds,
  questionPreview,
  requestQuestions,
  sendIdentity,
  requestRows,
  requestTitle,
  requestSummary,
  reviewOnly,
  messageTimestamp,
  openQuestionTurn,
  sendNowTarget,
  turnStateLabel,
  waitingBehindOwnTurn,
} from "./structuredView";
import { ChoiceQuestions } from "./Questions";

/** While a turn runs, the event cursor is read this often; a change re-reads the snapshot. */
export const STRUCTURED_ACTIVE_POLL_MS = 1_000;
/** Idle: background activity (or another tab) can still change the conversation. */
export const STRUCTURED_IDLE_POLL_MS = 10_000;
/** Always send now (#1447): automatic Send now attempts per queued message before it waits for
 *  a click (a refusal while the agent's turn is not acknowledged yet clears up on its own). */
const AUTO_SEND_TRIES = 5;
/** An acknowledged Interrupt whose turn has not ended may be pressed again after this. */
const STOPPING_RETRY_MS = 10_000;
/** Reconnect back-off ceiling after failed reads. */
const MAX_BACKOFF_MS = 15_000;
/** Files/pictures per message — the server checks the same shared four-attachment cap. */
const MAX_ATTACHMENTS = 4;

/** Match the server's safe display name. Keep collision/leading numeric prefixes intact. */
const attachmentName = (path: string) => uploadStoredName(path).replace(/^\d{8}-\d{6}-/, "");

const uuid = () => crypto.randomUUID();

function detail(e: unknown, fallback: string): string {
  return e instanceof ApiError && e.message ? e.message : fallback;
}

/** A server answer (4xx) means the request was NOT taken; anything else may have been. */
function definite(e: unknown): boolean {
  return e instanceof ApiError && e.status < 500;
}

interface Deciding {
  /** The exact request: native request ids repeat across turns and worker generations, so the
   *  turn and the presented payload's digest are part of its identity. */
  key: string;
  request_id: string;
  decision: string;
  /** With `decision: "always"` (#1339): the one offered grant it sends. */
  grant?: string;
  /** With `decision: "answer"` (#1463): the exact answers it sends, frozen with its id, so a retry
   *  can only resend them. */
  answers?: Answers;
  decision_id: string;
  /** `sending` — awaiting the server; `sent` — accepted, awaiting the agent (the next snapshot
   *  settles it); `unknown` — no answer came back, so it may or may not have landed. */
  phase: "sending" | "sent" | "unknown";
}

type Answers = ({ selected: string[] } | { other: string })[];
type OnDecide = (req: StructuredRequest, choice: string, grant?: string, answers?: Answers) => void;

function requestKey(req: StructuredRequest): string {
  return `${req.turn_id}\u0000${req.request_id}\u0000${req.payload_digest ?? ""}`;
}

/** An async question's answer (#1463) while its outcome is open. Bound to the question it
 *  answers, so a Retry resends the identical operation and the server can refuse it once that
 *  question has closed. */
type HeldAnswer = {
  session: string;
  question: string;
  id: string;
  text: string;
  phase: "sending" | "unknown" | "refused";
  message?: string;
};

function Patch({ files }: { files: { path: string; diff: string }[] }) {
  return (
    <div className={styles.patch} data-testid="structured-patch">
      {files.map((f) => (
        <div key={f.path}>
          <div className={styles.patchFile}>{f.path}</div>
          {f.diff.split("\n").map((line, i) => (
            <div
              key={i}
              className={
                line.startsWith("+") && !line.startsWith("+++")
                  ? styles.add
                  : line.startsWith("-") && !line.startsWith("---")
                    ? styles.del
                    : undefined
              }
            >
              {line || " "}
            </div>
          ))}
        </div>
      ))}
    </div>
  );
}

/** The advisory risk of one command (#1339), on a card or a tool row. `RISKY` + reasons in the
 *  warning status colour; "not classified" stays quiet. Nothing is ever labelled safe. */
export function RiskMark({ risk }: { risk?: StructuredRisk | null }) {
  if (!risk || risk.level === "none") return null;
  if (risk.level === "risky") {
    return (
      <span className={styles.risky} data-testid="structured-risk" data-level="risky">
        <b>Risky</b>
        {risk.reasons.length > 0 && <span> — {risk.reasons.join("; ")}</span>}
      </span>
    );
  }
  return (
    <span className={styles.unclassified} data-testid="structured-risk" data-level={risk.level}>
      not classified
    </span>
  );
}

/** Claude's `AskUserQuestion` (#1463): the agent's question with its options, answered, skipped
 *  or stopped — never approved. A decision in flight freezes the choices with its exact answers. */
function QuestionRequest({
  req,
  questions,
  agent,
  deciding,
  readOnly,
  interrupting,
  onDecide,
}: {
  req: StructuredRequest;
  questions: NonNullable<ReturnType<typeof requestQuestions>>;
  agent: string;
  deciding: Deciding | null;
  readOnly: boolean;
  interrupting: boolean;
  onDecide: OnDecide;
}) {
  const mine = deciding?.key === requestKey(req) ? deciding : null;
  const answerLabel = (a: Answers | undefined) =>
    (a ?? []).map((x) => ("other" in x ? x.other : x.selected.join(", "))).join(" · ");
  const mineText = mine?.decision === "answer" ? `“${answerLabel(mine.answers)}”` : `“${choiceLabel(mine?.decision ?? "", req)}”`;
  return (
    <div data-testid="structured-request" data-kind={req.kind}>
      <ChoiceQuestions
        key={requestKey(req)}
        testId="structured-ask"
        heading={requestTitle(req, agent)}
        questions={questions}
        allowOther
        disabled={readOnly || !!mine}
        submitLabel={questions.length > 1 ? "Send answers" : "Send answer"}
        onSubmit={(answers) => onDecide(req, "answer", undefined, answers.map(answerWire))}
        status={
          mine && (
            <p className={styles.pendingDecision} role="status" data-testid="structured-deciding">
              {mine.phase === "sending"
                ? `Sending ${mineText}…`
                : mine.phase === "sent"
                  ? `${mineText} sent — waiting for ${agent} to take it.`
                  : `${mineText} may not have reached BattleLab. Retrying sends the same ${mine.decision === "answer" ? "answer" : "decision"}.`}
            </p>
          )
        }
        actions={
          mine?.phase === "unknown" ? (
            <button
              type="button"
              className={chat.send}
              disabled={readOnly}
              data-testid="structured-ask-retry"
              onClick={() => onDecide(req, mine.decision, undefined, mine.answers)}
            >
              Retry {mineText}
            </button>
          ) : (
            req.choices
              .filter((c) => c === "reject" || c === "cancel")
              .map((c) => (
                <button
                  key={c}
                  type="button"
                  className={chat.ghost}
                  disabled={readOnly || !!mine || (c === "cancel" && interrupting)}
                  onClick={() => onDecide(req, c)}
                >
                  {choiceLabel(c, req)}
                </button>
              ))
          )
        }
        hint={() =>
          req.choices.includes("reject")
            ? `Skip question tells ${agent} you didn’t answer; it carries on with its own judgement.`
            : null
        }
      />
      {/* The complete request the answer goes back with (Hermes #6477): an answer returns the
          original input, so nothing in it may be hidden behind the picker. */}
      <details className={styles.requestDetails} data-testid="structured-ask-details">
        <summary>Details</summary>
        <div className={styles.detailsScroll}>
          <pre className={styles.json}>{JSON.stringify(req.payload ?? null, null, 2)}</pre>
        </div>
      </details>
    </div>
  );
}

function RequestCard({
  req,
  agent,
  deciding,
  readOnly,
  onDecide,
  folder,
  onInterrupt,
  interrupting,
}: {
  req: StructuredRequest;
  folder?: string;
  onInterrupt: () => void;
  interrupting: boolean;
  agent: string;
  deciding: Deciding | null;
  readOnly: boolean;
  onDecide: OnDecide;
}) {
  const mine = deciding?.key === requestKey(req) ? deciding : null;
  const always = req.choices.includes("approve") ? (req.always ?? []) : [];
  const inline = inlineGrants(always);
  const [alwaysOpen, setAlwaysOpen] = useState(false);
  // Focus moves INTO the grants when they open and back to the toggle on Escape (#1339).
  const alwaysToggle = useRef<HTMLButtonElement>(null);
  const alwaysList = useRef<HTMLUListElement>(null);
  useEffect(() => {
    if (!alwaysOpen) return;
    const list = alwaysList.current;
    list?.scrollIntoView?.({ block: "nearest" });
    list?.querySelector<HTMLButtonElement>("button")?.focus({ preventScroll: true });
  }, [alwaysOpen]);
  const decidedGrant = mine?.grant ? always.find((g) => g.id === mine.grant) : undefined;
  const mineLabel = mine
    ? mine.decision === "always" && decidedGrant
      ? `${inline ? grantChoiceLabel(decidedGrant, req) : choiceLabel("always", req)}: ${decidedGrant.label}`
      : choiceLabel(mine.decision, req)
    : "";
  const summary = requestSummary(req, folder);
  // A question BattleLab can't offer as choices still reads as a question, not as JSON (#1463).
  const preview = questionPreview(req);
  const rows = requestRows(req);
  const ids = requestIds(req);
  const declineOnly = reviewOnly(req);
  return (
    <section
      className={styles.card}
      aria-label={requestTitle(req, agent)}
      data-testid="structured-request"
      data-kind={req.kind}
    >
      <div className={styles.cardHead}>
        <span className={styles.ledAsk} aria-hidden="true" />
        {requestTitle(req, agent)}
      </div>
      {req.risk && req.risk.level !== "none" && (
        <p className={styles.riskLine}>
          <RiskMark risk={req.risk} />
        </p>
      )}
      {preview ? (
        <div className={styles.actionSummary} data-testid="structured-question-preview">
          <p className={styles.why}>
            {agent} asked this, but BattleLab can’t offer it as choices (its questions or options
            repeat, or aren’t in a shape it can answer exactly). Skip it and {agent} carries on with
            its own judgement, or stop the turn and answer in a message.
          </p>
          {preview.map((q, i) => (
            <div key={i} className={styles.previewQuestion}>
              <p>
                {q.header && <span className={styles.previewTag}>{q.header}</span>}
                <b>{q.title}</b>
              </p>
              <ul>
                {q.options.map((o, j) => (
                  <li key={j}>
                    {o.label}
                    {o.description && <small> — {o.description}</small>}
                  </li>
                ))}
              </ul>
            </div>
          ))}
        </div>
      ) : (
      <div className={styles.actionSummary}>
        <code className={styles.actionText}>{summary.action}</code>
        {summary.folder && <p className={styles.requestFolder}>{summary.folder}</p>}
        {summary.reason && <p className={styles.requestReason}>{summary.reason}</p>}
      </div>
      )}
      {rows.filter((row) => row.kind === "patch" && row.files).map((row) => (
        <div className={styles.actionSummary} key={row.key}><Patch files={row.files!} /></div>
      ))}
      {declineOnly && req.kind === "file_change" && (
        <p className={styles.why} data-testid="structured-review-only">
          <b>You can review this change but not approve it here.</b> Nothing guarantees that {agent}{" "}
          writes exactly the patch shown, so BattleLab does not approve file changes for an API
          session. Decline it and {agent} continues without the edit, or stop the turn.
        </p>
      )}
      {mine && (
        <p className={styles.pendingDecision} role="status" data-testid="structured-deciding">
          {mine.phase === "sending"
            ? `Sending “${mineLabel}”…`
            : mine.phase === "sent"
              ? `“${mineLabel}” sent — waiting for ${agent} to take it.`
              : `“${mineLabel}” may not have reached BattleLab. Retrying sends the same decision.`}
        </p>
      )}
      <div className={styles.actions}>
        {mine?.phase === "unknown" ? (
          <button
            type="button"
            className={chat.ghost}
            disabled={readOnly}
            onClick={() => onDecide(req, mine.decision, mine.grant)}
          >
            Retry “{mineLabel}”
          </button>
        ) : (
          req.choices.flatMap((choice) => {
            const button = (
              <button
                key={choice}
                type="button"
                className={choice === "approve" ? chat.send : chat.ghost}
                disabled={readOnly || !!mine}
                onClick={() => onDecide(req, choice)}
              >
                {choiceLabel(choice, req)}
              </button>
            );
            if (choice !== "approve" || always.length === 0) return [button];
            // Each inline grant is one click; its full label rides on hover and in Details.
            if (inline) {
              return [
                button,
                ...inline.map((g) => (
                  <button
                    key={g.id}
                    type="button"
                    className={chat.send}
                    title={g.label}
                    data-testid="structured-always"
                    data-scope={g.scope}
                    disabled={readOnly || !!mine}
                    onClick={() => onDecide(req, "always", g.id)}
                  >
                    {grantChoiceLabel(g, req)}
                  </button>
                )),
              ];
            }
            return [
              button,
              <button
                key="always"
                ref={alwaysToggle}
                type="button"
                className={chat.send}
                aria-expanded={alwaysOpen}
                aria-controls={`always-${req.request_id}`}
                data-testid="structured-always"
                disabled={readOnly || !!mine}
                onClick={() => setAlwaysOpen((v) => !v)}
              >
                {choiceLabel("always", req)} {alwaysOpen ? "▴" : "▾"}
              </button>,
            ];
          })
        )}
        {!req.choices.includes("cancel") && (
          <button type="button" className={chat.ghost} disabled={readOnly || interrupting}
            onClick={onInterrupt}>Reject and stop the turn</button>
        )}
      </div>
      <details className={styles.requestDetails}>
        <summary>Details</summary>
        <div className={styles.detailsScroll}>
          <dl className={styles.kv}>
            {rows.map((row) => (
              <div className={styles.kvRow} key={row.key} data-testid="structured-field" data-field={row.key}>
                <dt>{row.label}</dt>
                <dd>
                  {row.kind === "patch" && row.files ? (
                    <pre className={styles.json}>{JSON.stringify(row.files, null, 2)}</pre>
                  ) : row.kind === "code" ? (
                    <code className={styles.cmd}>{row.text}</code>
                  ) : row.kind === "json" ? (
                    <pre className={styles.json}>{row.text}</pre>
                  ) : row.kind === "suggestions" ? (
                    <>
                      <pre className={styles.json}>{row.text}</pre>
                      <span className={styles.aside}>
                        {always.length > 0
                          ? "Offered under Approve always, as proposed — read each option's scope and where it is saved."
                          : "None of these can be approved always here (BattleLab offers only rules it can describe, never a mode switch)."}
                      </span>
                    </>
                  ) : (
                    row.text
                  )}
                </dd>
              </div>
            ))}
            {inline && (
              <div className={styles.kvRow} data-testid="structured-field" data-field="grants">
                <dt>Standing grants</dt>
                <dd>
                  {inline.map((g) => (
                    <p key={g.id}>
                      <b>{grantChoiceLabel(g, req)}:</b> {g.label}
                    </p>
                  ))}
                </dd>
              </div>
            )}
          </dl>
          <p className={styles.ids}>
            {[`request ${req.request_id}`, ...ids, req.payload_digest ? `digest ${req.payload_digest.slice(0, 8)}…` : null]
              .filter(Boolean)
              .join(" · ")}
          </p>
        </div>
      </details>
      {alwaysOpen && !mine && !inline && always.length > 0 && (
        <ul
          ref={alwaysList}
          className={styles.alwaysList}
          id={`always-${req.request_id}`}
          aria-label="Standing grants this request proposed"
          onKeyDown={(e) => {
            if (e.key !== "Escape") return;
            e.preventDefault();
            setAlwaysOpen(false);
            alwaysToggle.current?.focus();
          }}
        >
          {always.map((g) => (
            <li key={g.id}>
              <button
                type="button"
                className={styles.alwaysOption}
                data-testid="structured-always-option"
                data-scope={g.scope}
                disabled={readOnly}
                onClick={() => {
                  setAlwaysOpen(false);
                  onDecide(req, "always", g.id);
                }}
              >
                {g.label}
              </button>
            </li>
          ))}
          <li className={styles.aside}>
            Only the grants {agent} proposed for this request. BattleLab never adds or widens one.
          </li>
        </ul>
      )}
    </section>
  );
}

/** While the pointer is in the queue, preserve space before a pending sibling so answering
 * cannot move another action underneath it. The queue retires unneeded slots separately. */
function RequestSlot({ children, active }: { children: React.ReactNode; active: boolean }) {
  const box = useRef<HTMLDivElement>(null);
  const [height, setHeight] = useState(0);
  useEffect(() => {
    if (!active || !box.current || typeof ResizeObserver === "undefined") return;
    const el = box.current;
    const resize = new ResizeObserver(() => setHeight(el.getBoundingClientRect().height));
    resize.observe(el);
    return () => resize.disconnect();
  }, [active]);
  return <div ref={box} style={active ? undefined : { minHeight: height }}>
    {active ? children : <p className={styles.closedRequest}>Request closed</p>}
  </div>;
}

function RequestQueue({ requests, render }: {
  requests: StructuredRequest[];
  render: (req: StructuredRequest) => React.ReactNode;
}) {
  const queue = useRef<HTMLDivElement>(null);
  const focused = useRef<string | null>(null);
  const [retaining, setRetaining] = useState(false);
  const [order, setOrder] = useState<string[]>(requests.map(requestKey));
  const pending = requests.map(requestKey);
  const keys = retaining ? [...order] : order.filter((key) => pending.includes(key));
  // A closed suffix protects no sibling. Retire it BEFORE appending new requests, including
  // snapshots which both settle one request and introduce its successor in the same turn.
  while (keys.length && !pending.includes(keys[keys.length - 1])) keys.pop();
  keys.push(...pending.filter((key) => !keys.includes(key)));
  // Adjust derived order before committing children, without an extra effect/render pass.
  if (keys.length !== order.length || keys.some((key, i) => key !== order[i])) setOrder(keys);
  useEffect(() => {
    const outside = (event: PointerEvent) => {
      if (event.target instanceof Node && !queue.current?.contains(event.target)) {
        focused.current = null;
        setRetaining(false);
      }
    };
    document.addEventListener("pointerdown", outside);
    return () => document.removeEventListener("pointerdown", outside);
  }, []);
  useLayoutEffect(() => {
    if (!focused.current || pending.includes(focused.current)) return;
    focused.current = null;
    const root = queue.current;
    // An acknowledgement removes the focused button. Hand off only its focus, never focus
    // belonging to the composer or another pane, and never scroll buttons under the pointer.
    if (document.activeElement !== document.body && !root?.contains(document.activeElement)) return;
    (root?.querySelector<HTMLElement>("button:not(:disabled)")
      ?? root?.querySelector<HTMLElement>("[data-testid=structured-queue]"))?.focus({ preventScroll: true });
  });
  if (!requests.length) return null;
  return <div ref={queue} className={styles.queue}
    onPointerEnter={() => setRetaining(true)}
    onPointerLeave={(event) => { if (event.pointerType === "mouse") setRetaining(false); }}
    onBlurCapture={(event) => {
      if (event.relatedTarget instanceof Node && !event.currentTarget.contains(event.relatedTarget)) focused.current = null;
    }}>
    <p className={styles.queueHead} data-testid="structured-queue" tabIndex={-1}>
      {requests.length} {requests.length === 1 ? "request" : "requests"} waiting
    </p>
    {keys.map((key) => {
      const req = requests.find((r) => requestKey(r) === key);
      return <div key={key} onFocusCapture={() => { focused.current = key; }}>
        <RequestSlot active={!!req}>{req && render(req)}</RequestSlot>
      </div>;
    })}
  </div>;
}

/** The `Esc` beside an Interrupt button; shown only with a fine pointer (CSS). */
function EscHint() {
  return (
    <kbd className={styles.kbd} aria-hidden="true">
      Esc
    </kbd>
  );
}

function toolCount(tools: StructuredTool[], truncated: boolean): string {
  return `${truncated ? "Latest " : ""}${tools.length} commands & tools`;
}

function ToolList({ tools, label }: { tools: StructuredTool[]; label: string }) {
  return (
    <ul className={chat.tools} aria-label={label}>
      {tools.map((t) => (
        <li key={t.id} className={chat.tool} data-testid="structured-tool">
          <span className={chat.toolVerb}>{t.name}</span>
          {t.summary && <code className={chat.toolPath}>{t.summary}</code>}
          {t.outcome && <span className={chat.toolDetail}>{t.outcome}</span>}
          <RiskMark risk={t.risk} />
        </li>
      ))}
    </ul>
  );
}

/** A turn the agent ran for itself (#1438): a scheduled wake-up, a task notification. Its activity
 *  is collapsed with the latest call live in the summary; Interrupt (and Esc) stop it — the turn,
 *  never the worker. */
function AgentTurnView({
  turn,
  beforeReply,
  agent,
  interruptible,
  ahead,
  readOnly,
  live,
  stopping,
  onInterrupt,
  question,
}: {
  turn: StructuredAgentTurn;
  beforeReply?: ReactNode;
  agent: string;
  interruptible: boolean;
  /** The operator's message waits behind this turn in the agent's own queue (#1475). */
  ahead: boolean;
  readOnly: boolean;
  live: boolean;
  stopping: boolean;
  onInterrupt: (turnId: string) => void;
  /** The question block when this turn's question is still open (#1463). */
  question?: ReactNode;
}) {
  const running = turn.state === "running";
  const latest = turn.tools[turn.tools.length - 1];
  const ended =
    turn.state === "interrupted" ? "Interrupted" : turn.state === "failed" ? "Failed" : null;
  const bars = (
    <span className={chat.bars} aria-hidden="true">
      <span />
      <span />
      <span />
    </span>
  );
  return (
    <section
      className={styles.ownTurn}
      data-testid="structured-agent-turn"
      data-state={turn.state}
      aria-label={`${agent}, on its own`}
    >
      <MessageHeader speaker={`${agent} · on its own`} kind="assistant" ts={turn.ts} />
      {turn.tools.length > 0 ? (
        <details className={styles.activity} data-testid="structured-agent-activity">
          <summary>
            {running && bars}
            {running && "Working on its own · "}
            {toolCount(turn.tools, turn.tools_truncated)}
            {ended && <span className={styles.activityFailed}> · {ended}</span>}
            {turn.tools.some((t) => t.risk?.level === "risky") && <span className={styles.activityRisk}> · Risky activity</span>}
            {running && latest && (
              <span className={styles.latest} data-testid="structured-agent-latest">
                <b>{latest.name}</b> {latest.summary}
              </span>
            )}
          </summary>
          <ToolList tools={turn.tools} label="What the agent is doing on its own" />
        </details>
      ) : running ? (
        <div className={chat.wait}>
          {bars}
          Working on its own
        </div>
      ) : (
        ended && <p className={`${styles.ownNote} ${styles.activityFailed}`}>{ended}</p>
      )}
      {beforeReply}
      {turn.reply && (
        <div className={`${chat.txt} ${styles.ownReply}`} data-testid="structured-agent-reply">
          <Markdown text={turn.reply_truncated ? `${turn.reply}…` : turn.reply} />
        </div>
      )}
      {question}
      {turn.state === "failed" && turn.reason && (
        <p className={`${styles.ownNote} ${styles.activityFailed}`} role="alert">{turn.reason}</p>
      )}
      {running && live && !readOnly && (
        interruptible ? (
          <div className={styles.ownControls}>
            <button
              type="button"
              className={`${chat.ghost} ${styles.interrupt}`}
              disabled={stopping}
              onClick={() => onInterrupt(turn.turn_id)}
              data-testid="structured-agent-interrupt"
            >
              {stopping ? "Interrupting…" : "Interrupt"}
              {!stopping && <EscHint />}
            </button>
          </div>
        ) : ahead ? (
          <p className={styles.ownNote} data-testid="structured-agent-ahead">
            Your message is next — it starts when this turn ends.
          </p>
        ) : (
          <p className={styles.ownNote} data-testid="structured-agent-no-interrupt">
            This agent can’t be interrupted from here — Stop ends the worker.
          </p>
        )
      )}
    </section>
  );
}

function MessageHeader({ speaker, title, kind, ts }: {
  speaker: string;
  title?: string;
  kind: "user" | "assistant";
  ts?: number | null;
}) {
  const timestamp = messageTimestamp(ts, kind);
  return (
    <div className={`${chat.who} ${styles.messageHeader}`}>
      <span title={title}>{speaker}</span>
      {timestamp && (
        <time className={styles.messageTimestamp} dateTime={timestamp.iso}
          title={timestamp.label} aria-label={timestamp.label}>
          {timestamp.compact}
        </time>
      )}
    </div>
  );
}

function TurnView({
  folder,
  turn,
  beforeReply,
  agent,
  requests,
  deciding,
  readOnly,
  approvalsDisabled,
  onDecide,
  onInterrupt,
  interrupting,
  onSendNow,
  sendNowMode,
  sendingNow,
  sendNowDisabled,
  alwaysSendNow,
  onAlwaysSendNow,
  onRemove,
  removing,
  canRemove,
  queuePosition,
  cutOff,
  waiting,
  question,
}: {
  folder?: string;
  turn: StructuredTurn;
  /** Acknowledged additions to this turn, after its activity/approvals and before its reply. */
  beforeReply?: ReactNode;
  agent: string;
  requests: StructuredRequest[];
  deciding: Record<string, Deciding>;
  readOnly: boolean;
  approvalsDisabled: boolean;
  onDecide: OnDecide;
  onInterrupt: (turnId: string) => void;
  interrupting: boolean;
  onSendNow: (turnId: string) => void;
  sendNowMode?: "steer" | "interrupt";
  sendingNow: boolean;
  sendNowDisabled: boolean;
  alwaysSendNow: boolean;
  onAlwaysSendNow: (turnId: string) => void;
  onRemove: (turnId: string) => void;
  removing: boolean;
  canRemove: boolean;
  /** Its place in line when queued, 1 = next (#1442). Only the head offers Send now. */
  queuePosition?: number;
  /** It settled interrupted because Send now stopped it (#1442). */
  cutOff?: boolean;
  /** Sent, but the agent runs a turn of its own first (#1475): not running, not interruptible. */
  waiting?: boolean;
  /** The question block when this turn's question is still open (#1463). */
  question?: ReactNode;
}) {
  const active = turn.state === "running" || turn.state === "awaiting_approval";
  const failed = ["failed", "interrupted", "uncertain", "unavailable"].includes(turn.state);
  const canSendNow =
    queuePosition === 1 && (!turn.delivery || turn.delivery === "interrupt_failed");
  return (
    <div className={chat.exchange} data-testid="structured-turn" data-state={turn.state}>
      <div className={`${chat.turn} ${chat.user}`}>
        {/* A visitor's turn (#1404 P2) names them; the name is their own choice, not proof. When
            attribution can't be read, no turn claims to be the operator's. */}
        <MessageHeader
          speaker={
            turn.author?.via === "public_link"
              ? `${turn.author.name} · via public link`
              : turn.author?.via === "unknown"
                ? "sender unknown"
                : "you"
          }
          title={
            turn.author?.via === "public_link"
              ? "Sent through a public link — a name the visitor chose"
              : turn.author?.via === "unknown"
                ? "Who sent this can't be read right now — it may have come through a public link"
                : undefined
          }
          kind="user"
          ts={turn.ts}
        />
        {!!turn.attachments?.length && (
          <div className={styles.turnImages}>
            {turn.attachments.map((a) => a.mime.startsWith("image/") ? (
              // UploadImage's slot fills its parent: this box bounds it to one thumbnail.
              <span key={a.stored} className={styles.turnImageBox}>
                <UploadImage
                  path={a.stored}
                  alt="Attached image"
                  className={styles.turnImage}
                  fallback={<span className={styles.turnImageGone}>image no longer available</span>}
                />
              </span>
            ) : (
              <span
                key={a.stored}
                className={`${compose.pill} ${styles.turnFile}`}
                title={a.name ?? attachmentName(a.stored)}
              >
                <Paperclip size={14} aria-hidden />
                <span className={compose.pn}>{a.name ?? attachmentName(a.stored)}</span>
              </span>
            ))}
          </div>
        )}
        {!!turn.text && (
          <div className={chat.txt}>
            {turn.text}
            {turn.text_truncated && "…"}
          </div>
        )}
      </div>
      {turn.tools.length > 0 && (
        <details className={styles.activity} data-testid="structured-activity">
          <summary>
            {toolCount(turn.tools, turn.tools_truncated)}
            {active && " · Working"}
            {turn.tools.some((t) => /failed|error/i.test(t.outcome)) && <span className={styles.activityFailed}> · Failed activity</span>}
            {turn.tools.some((t) => t.risk?.level === "risky") && <span className={styles.activityRisk}> · Risky activity</span>}
          </summary>
          <ToolList tools={turn.tools} label="What the agent did" />
        </details>
      )}
      <RequestQueue requests={requests} render={(r) => requestQuestions(r) ? (
        <QuestionRequest
          key={requestKey(r)}
          req={r}
          questions={requestQuestions(r)!}
          agent={agent}
          deciding={deciding[requestKey(r)] ?? null}
          readOnly={readOnly || approvalsDisabled}
          interrupting={interrupting}
          onDecide={onDecide}
        />
      ) : (
        <RequestCard
          key={requestKey(r)}
          req={r}
          folder={folder}
          agent={agent}
          deciding={deciding[requestKey(r)] ?? null}
          readOnly={readOnly || approvalsDisabled}
          onDecide={onDecide}
          onInterrupt={() => onInterrupt(turn.turn_id)}
          interrupting={interrupting}
        />
      )} />
      {beforeReply}
      {turn.reply && (
        <div className={`${chat.turn} ${chat.asst} ${styles.reply}`} data-testid="structured-reply">
          <MessageHeader speaker={agent} kind="assistant" ts={turn.reply_ts} />
          <div className={chat.txt}>
            <Markdown text={turn.reply_truncated ? `${turn.reply}…` : turn.reply} />
          </div>
        </div>
      )}
      {question}
      {turn.state === "queued" && (
        <div className={styles.queued} data-testid="structured-queued">
          <div className={styles.queueRow}>
            <span role="status">{turn.delivery === "interrupting"
              ? "Waiting for the current response to stop · sends next"
              : turn.delivery === "steering"
                ? "Awaiting agent acknowledgement"
                : queuePosition === 1
                  ? "1st in queue · sends after the current reply"
                  : `${ordinal(queuePosition ?? 1)} in queue · sends after the message above`}</span>
            {!readOnly && ((sendNowMode && canSendNow) || (canRemove && turn.delivery !== "steering")) && (
              <span className={styles.queueActions}>
                {canRemove && turn.delivery !== "steering" && (
                  <button type="button" className={styles.remove} disabled={removing || sendingNow}
                    aria-label="Remove this queued message" onClick={() => onRemove(turn.turn_id)}>
                    <X size={14} aria-hidden />
                    {removing ? "Removing…" : "Remove"}
                  </button>
                )}
                {/* Send now stays while Always send now is on: if an automatic attempt is
                    refused and nothing changes, a click is still the way through. */}
                {sendNowMode && canSendNow && (
                  <button type="button" className={styles.sendNow} disabled={sendNowDisabled}
                    onClick={() => onSendNow(turn.turn_id)}>
                    {sendingNow ? "Sending…" : "Send now"}
                  </button>
                )}
                {sendNowMode && canSendNow && !alwaysSendNow && (
                  <button type="button" className={styles.alwaysSendNow} disabled={sendNowDisabled}
                    onClick={() => onAlwaysSendNow(turn.turn_id)}>
                    Always send now
                  </button>
                )}
              </span>
            )}
          </div>
          {turn.delivery === "interrupt_failed" && (
            <p className={styles.activityFailed} role="alert">
              Couldn’t interrupt: {turn.delivery_reason}. This message is still queued.
            </p>
          )}
          {!readOnly && sendNowMode && canSendNow && !alwaysSendNow && (
            <p className={styles.queueHint}>{sendNowMode === "steer"
              ? "Adds this message to the current turn."
              : `Stops ${agent}’s current reply, then sends this — and the messages after it, in order.`}{" "}
              <strong>Always send now</strong> does this for every message you send while the agent is working.</p>
          )}
        </div>
      )}
      {turn.state === "delivering" && <div className={chat.wait} role="status">Awaiting agent acknowledgement</div>}
      {turn.state === "delivered" && <div className={chat.wait} role="status">Added to the active turn</div>}
      {active && waiting && (
        <div className={chat.wait} role="status" data-testid="structured-waiting-own-turn">
          Waiting · {agent} is finishing a turn of its own first
        </div>
      )}
      {active && !waiting && (
        <div className={chat.wait} data-testid="structured-working">
          <span className={chat.bars} aria-hidden="true">
            <span />
            <span />
            <span />
          </span>
          {turnStateLabel(turn.state)}
          {!readOnly && (
            <button
              type="button"
              className={`${chat.ghost} ${styles.inline} ${styles.interrupt}`}
              disabled={interrupting}
              onClick={() => onInterrupt(turn.turn_id)}
            >
              {interrupting ? "Interrupting…" : "Interrupt"}
              {!interrupting && <EscHint />}
            </button>
          )}
        </div>
      )}
      {cutOff ? (
        <p className={styles.cutNote} role="status" data-testid="structured-sendnow-cut">
          Interrupted by Send now — your next message is answered below.
        </p>
      ) : failed && (
        <div className={chat.failed}>
          <div className={chat.err} role="alert" data-testid="structured-failed">
            {turnStateLabel(turn.state)}
            {turn.reason ? ` — ${turn.reason}` : ""}
            {turn.state === "uncertain" &&
              " BattleLab will not send it again by itself; check the folder before you repeat it."}
          </div>
        </div>
      )}
    </div>
  );
}

const NO_HOST: PaneHost = {};
const LINK_TOAST_MS = 1200;
const LINK_FAILED_TOAST_MS = 2600;

/** A native API client's session (#1311): BattleLab drives the installed CLI through its
 *  structured protocol and keeps a journal; this view only READS it and sends operator input.
 *  Reconnects resume from the event cursor — a closed tab never cancels a turn — and every send
 *  and decision carries an id that makes a retry a no-op on the server. */
export function StructuredPane({
  engine,
  id,
  host = NO_HOST,
}: {
  engine: string;
  id: string;
  /** What the session's host hands a pane: Files, To map, a window's chrome slot (#1332). */
  host?: PaneHost;
}) {
  useEngineRoster();
  const key = `${engine}:${id}`;
  // Actions act on the id the URL settled on (#867), the same split the terminal keeps.
  const actionKey = host.rowKey || key;
  const row = useSessionRow(key, host.rowKey);
  const isMobile = useIsMobile();
  // The agent is the console agent this client drives (`api.source`), named by its own label.
  const source = engineInfo(engine)?.api?.source;
  const agent = source ? engineLabel(source) : engineLabel(engine);
  const [snap, setSnap] = useState<StructuredSnapshot | null>(null);
  const [loadError, setLoadError] = useState<string | null>(null);
  const [reconnecting, setReconnecting] = useState(false);
  const [draft, setDraft] = useState("");
  // Files and pictures this send carries, by their server-owned upload identities.
  const [attachments, setAttachments] = useState<{ name: string; stored: string }[]>([]);
  // Uploads in flight, counted (Hermes on #1345): overlapping pastes each hold a slot, a send
  // waits for ALL of them, and the cap counts reserved slots — never a stale render's length.
  const [inFlight, setInFlight] = useState(0);
  const uploading = inFlight > 0;
  const slots = useRef({ held: 0, inFlight: 0 }); // synchronous truth; state only renders it
  // A send in flight admits no new picture, and every draft clear starts a new generation: an
  // upload or clipboard read that settles into an older generation is dropped, never carried
  // onto the next message (Hermes on #1345, round 2).
  const sending = useRef(false);
  const generation = useRef(0);
  // Sent history and templates (#1332 Phase 3b): the terminal composer's ring and picker.
  const [history, setHistory] = useState<SentMessage[]>(() => readSent());
  // Open = the trigger focus returns to on close; null = closed.
  const [historyOpen, setHistoryOpen] = useState<HTMLElement | null>(null);
  const [templatesOpen, setTemplatesOpen] = useState<HTMLElement | null>(null);
  const taRef = useRef<HTMLTextAreaElement>(null);
  // The terminal composer's auto-grow: the box follows its text up to 28% of the viewport.
  useEffect(() => {
    const ta = taRef.current;
    if (!ta) return;
    ta.style.height = "auto";
    ta.style.height = `${Math.min(ta.scrollHeight, Math.round(window.innerHeight * 0.28))}px`;
  }, [draft]);
  /** The history entry of the operation in flight: one entry per operation, not per retry. */
  const historyFor = useRef<{ op: string; id: string | null } | null>(null);
  // An "outcome unknown" entry of THIS session is settled by the conversation itself: once its
  // operation shows up as a turn, the server had it (Hermes on #1346).
  useEffect(() => {
    if (!snap) return;
    const turns = new Set(operatorTurns(snap).map((t) => t.turn_id));
    const settled = readSent().filter(
      (e) => !e.confirmed && e.session === key && e.operation && turns.has(e.operation),
    );
    for (const e of settled) confirmOperation(e.operation!);
  }, [snap, key]);
  // The ring is shared: another tab (`storage`) or another composer in this tab can write the
  // first entry after this pane mounted, and Sent must appear then (Hermes on #1346).
  useEffect(() => {
    const refresh = () => setHistory(readSent());
    const onStorage = (e: StorageEvent) => {
      if (e.key === null || e.key === SENT_HISTORY_KEY) refresh();
    };
    window.addEventListener("storage", onStorage);
    window.addEventListener(SENT_HISTORY_EVENT, refresh);
    return () => {
      window.removeEventListener("storage", onStorage);
      window.removeEventListener(SENT_HISTORY_EVENT, refresh);
    };
  }, []);
  const fileRef = useRef<HTMLInputElement>(null);
  const [sendError, setSendError] = useState<string | null>(null);
  const [busy, setBusy] = useState(false);
  const [deciding, setDeciding] = useState<Record<string, Deciding>>({});
  const [actionError, setActionError] = useState<string | null>(null);
  const [interrupting, setInterrupting] = useState(false);
  // The turn an acknowledged Interrupt is waiting on (#1438): "Interrupting…" until it ends, with
  // a retry after STOPPING_RETRY_MS in case the agent never acts on it.
  const [stoppingTurn, setStoppingTurn] = useState<string | null>(null);
  const [sendingNow, setSendingNow] = useState<string | null>(null);
  const [sendNowUnknown, setSendNowUnknown] = useState<{ key: string; queuedTurnId: string } | null>(null);
  const recoveringSendNow = sendNowUnknown?.key === key ? sendNowUnknown : null;
  const sendNowIds = useRef(new Map<string, { id: string; turnId: string }>());
  const sendNowBusy = useRef(false);
  const [alwaysSendNow, setAlwaysSendNow] = useAlwaysSendNow();
  // Always send now (#1447) presses Send now once per queued head; a refusal the agent may
  // outgrow (its turn not acknowledged yet) is retried when the journal moves, a few times.
  const autoTries = useRef(new Map<string, { revision: number; tries: number }>());
  const [removing, setRemoving] = useState<string | null>(null);
  // A lost Remove response keeps its operation id, so pressing Remove again replays it.
  const removeIds = useRef(new Map<string, string>());
  const [stopping, setStopping] = useState(false);
  const [containment, setContainment] = useState<Containment | null>(null);
  // Share publicly… (#1404 P4): a link anyone can open, made and revoked in its own dialog.
  const [sharePublic, setSharePublic] = useState<{ open: boolean; trigger: HTMLElement | null }>({
    open: false,
    trigger: null,
  });
  const [permissionsOpen, setPermissionsOpen] = useState<{
    trigger: HTMLElement | null;
  } | null>(null);
  const [enablingPermissions, setEnablingPermissions] = useState(false);
  const [permissionError, setPermissionError] = useState<string | null>(null);
  const permissionOperation = useRef<string | null>(null);
  const permissionSending = useRef(false);
  const [recap, setRecap] = useState<{
    open: boolean;
    trigger: HTMLElement | null;
  }>({
    open: false,
    trigger: null,
  });
  const [handoff, setHandoff] = useState<{
    open: boolean;
    trigger: HTMLElement | null;
  }>({
    open: false,
    trigger: null,
  });
  const [linkToast, setLinkToast] = useState<{ tick: number; ok: boolean }>({
    tick: 0,
    ok: true,
  });
  const op = useRef<{ id: string; text: string } | null>(null);
  const cursor = useRef(0);
  const logRef = useRef<HTMLDivElement>(null);

  const apply = useCallback((s: StructuredSnapshot) => {
    // Reads race (the poll, and a re-read after every action): an older answer arriving last
    // must never roll the view back. The journal revision only grows.
    if (s.revision < cursor.current) return;
    cursor.current = s.event_cursor;
    if (s.bypass) setPermissionsOpen(null);
    setSnap((previous) => {
      // Lifecycle changes need not advance the journal. Reject the entire older snapshot:
      // preserving just its mode would still resurrect closed approvals and the old worker.
      const prior = previous?.permission_change?.state;
      const incoming = s.permission_change?.state;
      return previous && ((prior === "enabled" && incoming !== "enabled") ||
        (prior === "stopping" && !incoming))
        ? previous
        : s;
    });
    // A decision is over once its exact request is no longer pending — forget it, so a later
    // request that reuses the native id never inherits it.
    setDeciding((all) => Object.fromEntries(Object.entries(all).filter(([, d]) =>
      d.phase === "sending" || s.pending_requests.some((r) => requestKey(r) === d.key),
    )));
    setLoadError(null);
    setReconnecting(false);
  }, []);
  const failed = useCallback((e: unknown) => {
    if (e instanceof ApiError && e.status === 404)
      setLoadError("This conversation does not exist.");
    else setReconnecting(true);
  }, []);
  const load = useCallback(async (): Promise<StructuredSnapshot | null> => {
    try {
      const s = await api.structuredSnapshot(key);
      apply(s);
      return s;
    } catch (e) {
      failed(e);
      return null;
    }
  }, [key, apply, failed]);

  useEffect(() => {
    let alive = true;
    api
      .structuredSnapshot(key)
      .then((s) => alive && apply(s))
      .catch((e: unknown) => alive && failed(e));
    return () => {
      alive = false;
    };
  }, [key, apply, failed]);

  // The cursor loop: cheap reads of the journal's revision; a change re-reads the snapshot.
  const active = isActive(snap);
  // Task progress reads live without changing the turn-active predicate used by Send (#1461).
  const pollingActive = active || backgroundWorking(snap);
  useEffect(() => {
    let alive = true;
    let timer: ReturnType<typeof setTimeout>;
    let backoff = 0;
    let ticks = 0;
    const tick = async () => {
      ticks += 1;
      try {
        const page = await api.structuredEvents(key, cursor.current, 1);
        if (!alive) return;
        backoff = 0;
        setReconnecting(false);
        // A changed journal re-reads at once. Lifecycle facts that write no journal record — a
        // worker's idle exit, the client turning read only — are picked up by a full re-read
        // every idle tick and every 5th active tick (Hermes on #1315).
        if (page.revision !== cursor.current || !pollingActive || ticks % 5 === 0) await load();
      } catch (e) {
        if (!alive) return;
        if (e instanceof ApiError && e.status === 409) await load();
        else {
          setReconnecting(true);
          backoff = Math.min(MAX_BACKOFF_MS, (backoff || 1_000) * 2);
        }
      }
      if (alive)
        timer = setTimeout(
          tick,
          backoff || (pollingActive ? STRUCTURED_ACTIVE_POLL_MS : STRUCTURED_IDLE_POLL_MS),
        );
    };
    timer = setTimeout(tick, pollingActive ? STRUCTURED_ACTIVE_POLL_MS : STRUCTURED_IDLE_POLL_MS);
    return () => {
      alive = false;
      clearTimeout(timer);
    };
  }, [key, pollingActive, load]);

  useLayoutEffect(() => {
    const el = logRef.current;
    if (!el) return;
    let following = true;
    let lastTop = el.scrollTop;
    const follow = () => {
      if (following) {
        el.scrollTop = el.scrollHeight;
        lastTop = el.scrollTop;
      }
    };
    const onScroll = () => {
      if (el.scrollHeight - el.clientHeight - el.scrollTop <= 4) following = true;
      else if (el.scrollTop < lastTop) following = false;
      // A delayed event from our own scroll can arrive after content grows again. An unchanged
      // position is not the operator scrolling up, so it must not turn following off.
      lastTop = el.scrollTop;
    };
    // Observe the transcript's children too: the scroll box keeps its height while a streamed
    // reply or a delayed image grows. Browser anchoring preserves reading above the bottom.
    const resize = typeof ResizeObserver === "undefined" ? null : new ResizeObserver(follow);
    const observe = () => {
      resize?.disconnect();
      resize?.observe(el, { box: "border-box" });
      for (const child of el.children) resize?.observe(child, { box: "border-box" });
      follow();
    };
    const changes = new MutationObserver(observe);
    changes.observe(el, {
      childList: true,
      characterData: true,
      subtree: true,
    });
    el.addEventListener("scroll", onScroll, { passive: true });
    observe();
    return () => {
      el.removeEventListener("scroll", onScroll);
      changes.disconnect();
      resize?.disconnect();
    };
  }, [key, loadError]);

  const readOnly = !!snap?.read_only;
  // An unstarted (or expired) skip creation takes no message: nothing runs until Start (#1339).
  const notStarted = !!snap?.pending_start || !!snap?.start_expired;
  const permissionPending = enablingPermissions || snap?.permission_change?.state === "stopping";

  // Push-to-talk (#1332 Phase 3c): the terminal composer's own recognizer lifecycle, writing into
  // this draft. No global Space hotkey — the terminal composer on the same page keeps that.
  const [dictNote, setDictNote] = useState("");
  const dict = useDictation(
    {
      readDraft: () => draft,
      writeDraft: setDraft,
      note: (message) => {
        setDictNote(message);
        window.setTimeout(() => setDictNote(""), 4000);
      },
      globalSpace: false,
    },
    !readOnly && !!snap,
  );
  const {
    listening: dictListening,
    finalizing: dictFinalizing,
    supported: dictSupported,
    micBtnRef,
    micHandlers,
  } = dict;
  /** Dictation still owes the draft words: a held mic (set at the press, before the mic grant)
   *  or a finalizing tail. Handlers re-check `dict.settled()` for the exact state. */
  const dictating = dictListening || dictFinalizing;
  // A send pressed while dictation still owes the draft words is HELD and re-issued once the
  // draft has settled, as the terminal composer does (Hermes on #908) — never a half sentence.
  const sendAfterDictation = useRef(false);

  // An async question's answer (#1463) is an ordinary message with its own operation id, so it
  // never takes the composer's draft or pictures with it. It names the question it answers
  // (`reply_to`), and the server admits it only while that question is still open, inside the
  // journal claim. A Retry therefore resends the identical operation: it replays the original,
  // or it is refused once the question closed. It can never answer a newer question or answer
  // twice, so nothing here fences the composer or survives a reload.
  // It is shown only on its own question while that question is open, so once its message is
  // in the conversation, or a newer question or another message closed it, it simply goes away.
  const [answer, setAnswer] = useState<HeldAnswer | null>(null);

  const send = async () => {
    if (!dict.settled()) {
      sendAfterDictation.current = true;
      if (!dict.finalizingNow()) dict.release(); // end capture; the tail still lands
      if (!dict.settled()) return;
      sendAfterDictation.current = false;
    }
    const text = draft.trim();
    const names = attachments.map((a) => a.stored);
    if ((!text && !names.length) || busy || readOnly || permissionPending || !snap) return;
    if (slots.current.inFlight > 0 || sending.current) return; // a picture is still on its way
    const attempt = operationFor(op.current, sendIdentity(text, names), uuid);
    op.current = attempt;
    if (historyFor.current?.op !== attempt.id) {
      // Recorded at submit time, untrimmed, so Restore round-trips exactly what was typed.
      historyFor.current = {
        op: attempt.id,
        id: appendSent({
          text: draft,
          attachments: names,
          session: key,
          operation: attempt.id,
        }),
      };
      setHistory(readSent());
    }
    const sent = () => {
      // The server recorded the turn — never a claim that the agent has acted on it.
      if (historyFor.current?.op === attempt.id && historyFor.current.id) {
        confirmSent(historyFor.current.id);
        setHistory(readSent());
      }
    };
    sending.current = true;
    setBusy(true);
    setSendError(null);
    try {
      // Streaming observations advance revision continuously; a queued operator message binds
      // its immutable operation ID, not a snapshot of the turn that is still running.
      await api.structuredSubmit(key, attempt.id, text, active ? undefined : snap.revision, names);
      sent();
      op.current = null;
      setDraft("");
      clearAttachments();
      setContainment(null);
      await load();
    } catch (e) {
      const s = await load();
      if (operatorTurns(s ?? null).some((t) => t.turn_id === attempt.id)) {
        sent();
        op.current = null;
        setDraft("");
        clearAttachments();
      } else if (definite(e)) {
        op.current = null; // refused: nothing was recorded under that id
        setSendError(
          e instanceof ApiError && e.status === 409
            ? `Not sent: ${detail(e, "the conversation changed")}. Your message is kept; review the new activity and send again.`
            : `Not sent: ${detail(e, "refused")}`,
        );
      } else {
        setSendError(
          "The message may not have reached BattleLab. Sending it again is safe — it will not be duplicated.",
        );
      }
    } finally {
      sending.current = false;
      setBusy(false);
    }
  };

  const sendAnswer = async (attempt: { question: string; id: string; text: string }) => {
    if (busy || readOnly || permissionPending || notStarted || !snap || sending.current) return;
    if (!attempt.text.trim()) return;
    sending.current = true;
    setBusy(true);
    setAnswer({ session: key, ...attempt, phase: "sending" });
    try {
      // No revision expectation: Codex keeps working, and the question binding is the check.
      await api.structuredSubmit(key, attempt.id, attempt.text, undefined, [], attempt.question);
      setAnswer(null);
      await load();
    } catch (e) {
      const s = await load();
      if (operatorTurns(s ?? null).some((t) => t.turn_id === attempt.id)) {
        setAnswer(null);
      } else if (definite(e)) {
        setAnswer({ session: key, ...attempt, phase: "refused", message: detail(e, "refused") });
      } else {
        setAnswer({ session: key, ...attempt, phase: "unknown" });
      }
    } finally {
      sending.current = false;
      setBusy(false);
    }
  };

  const decide = async (req: StructuredRequest, choice: string, grant?: string, answers?: Answers) => {
    if (readOnly || permissionPending) return;
    const reqKey = requestKey(req);
    const decidingNow = deciding[reqKey];
    if (decidingNow && decidingNow.phase !== "unknown") return;
    // A retry reuses the id only for the SAME decision, grant and answers (#1339, #1463): the
    // server binds all three.
    const decision_id =
      decidingNow?.key === reqKey &&
      decidingNow.decision === choice &&
      decidingNow.grant === grant &&
      JSON.stringify(decidingNow.answers) === JSON.stringify(answers)
        ? decidingNow.decision_id
        : uuid();
    const next: Deciding = {
      key: reqKey,
      request_id: req.request_id,
      decision: choice,
      ...(grant ? { grant } : {}),
      ...(answers ? { answers } : {}),
      decision_id,
      phase: "sending",
    };
    setDeciding((all) => ({ ...all, [reqKey]: next }));
    setActionError(null);
    try {
      await api.structuredDecide(key, {
        decision_id,
        turn_id: req.turn_id,
        request_id: req.request_id,
        decision: choice,
        ...(grant ? { grant } : {}),
        ...(answers ? { answers } : {}),
      });
      setDeciding((all) => ({ ...all, [reqKey]: { ...next, phase: "sent" } })); // settled by the next snapshot, not by this answer
      await load();
    } catch (e) {
      if (definite(e)) {
        setDeciding((all) => Object.fromEntries(Object.entries(all).filter(([k]) => k !== reqKey)));
        setActionError(`Decision not taken: ${detail(e, "refused")}`);
        await load();
      } else {
        setDeciding((all) => ({ ...all, [reqKey]: { ...next, phase: "unknown" } }));
      }
    }
  };

  const interrupt = async (turnId: string) => {
    if (interrupting) return; // Esc held or pressed twice: one request
    setInterrupting(true);
    setActionError(null);
    try {
      await api.structuredInterrupt(key, uuid(), turnId);
      setStoppingTurn(turnId);
    } catch (e) {
      setActionError(`Interrupt not sent: ${detail(e, "no answer")}`);
    } finally {
      setInterrupting(false);
      await load();
    }
  };

  // Only while that turn still runs: its end (or the retry window) releases the button.
  const stoppingNow = snap?.turns.some(
    (t) => t.turn_id === stoppingTurn && (t.state === "running" || t.state === "awaiting_approval"),
  )
    ? stoppingTurn
    : null;
  useEffect(() => {
    if (!stoppingNow) return;
    const retry = setTimeout(() => setStoppingTurn(null), STOPPING_RETRY_MS);
    return () => clearTimeout(retry);
  }, [stoppingNow]);

  /** Esc interrupts the running turn — the operator's, else the agent's own (#1438) — but only
   *  when nothing else wanted the key: a menu, list or dialog closes first (they handle Escape
   *  before it bubbles here), portaled UI is not this pane, and IME composition is typing. */
  const onPaneKey = (e: KeyboardEvent<HTMLDivElement>) => {
    if (e.key !== "Escape" || e.defaultPrevented || e.repeat || e.nativeEvent.isComposing) return;
    if (!(e.target instanceof Node) || !e.currentTarget.contains(e.target)) return;
    if (document.querySelector('[aria-modal="true"], [role="menu"]')) return;
    if (readOnly || interrupting) return;
    const target = interruptTarget(snap);
    if (!target || stoppingNow === target) return;
    e.preventDefault();
    void interrupt(target);
  };

  const sendNow = async (queuedTurnId: string, auto = false) => {
    if (sendNowBusy.current) return;
    const target = `${key}:${queuedTurnId}`;
    let operation = sendNowIds.current.get(target);
    if (recoveringSendNow?.queuedTurnId === queuedTurnId) {
      // Observe the original receipt even after delivery, worker exit, or journal truncation.
      // Current capability/turn state cannot authorize a replacement request on this path.
      if (!operation) return;
    } else {
      const sendTarget = sendNowTarget(snap);
      if (recoveringSendNow || readOnly || permissionPending || !snap || !sendTarget) return;
      const queued = operatorTurns(snap).find((t) => t.turn_id === queuedTurnId);
      // Only the head goes now (#1442); the server refuses anything else too.
      if (queuePositions(snap).get(queuedTurnId) !== 1) return;
      if (queued?.state !== "queued" || (queued.delivery && queued.delivery !== "interrupt_failed")) return;
      if (queued.delivery === "interrupt_failed") operation = undefined;
      operation ??= { id: uuid(), turnId: sendTarget.turnId };
    }
    sendNowIds.current.set(target, operation);
    sendNowBusy.current = true;
    setSendingNow(queuedTurnId);
    setActionError(null);
    try {
      await api.structuredSendNow(key, operation.id, operation.turnId, queuedTurnId);
      setSendNowUnknown(null);
    } catch (e) {
      // Refusing a recovery request says nothing about the original unknown handoff.
      if (definite(e) && !recoveringSendNow) {
        sendNowIds.current.delete(target);
        setSendNowUnknown(null);
        // An automatic attempt retries on its own; only its last refusal is worth showing.
        if (!auto || (autoTries.current.get(target)?.tries ?? 0) >= AUTO_SEND_TRIES) {
          setActionError(`Send now not applied: ${detail(e, "refused")}`);
        }
      } else {
        setSendNowUnknown({ key, queuedTurnId });
      }
    } finally {
      await load();
      setSendingNow(null);
      sendNowBusy.current = false;
    }
  };

  const removeQueued = async (queuedTurnId: string) => {
    if (removing || readOnly) return;
    const target = `${key}:${queuedTurnId}`;
    const operation = removeIds.current.get(target) ?? uuid();
    removeIds.current.set(target, operation);
    setRemoving(queuedTurnId);
    setActionError(null);
    try {
      await api.structuredWithdraw(key, operation, queuedTurnId);
      removeIds.current.delete(target);
    } catch (e) {
      if (definite(e)) {
        removeIds.current.delete(target);
        setActionError(`Not removed: ${detail(e, "refused")}`);
      } else {
        setActionError(`Remove status is unknown: ${detail(e, "no answer")}. Press Remove again to check.`);
      }
    } finally {
      await load();
      setRemoving(null);
    }
  };

  const autoHead = alwaysSendNow && !readOnly && !permissionPending && !recoveringSendNow && !sendingNow
    ? autoSendNowHead(snap)
    : null;
  useEffect(() => {
    if (!autoHead || !snap || sendNowBusy.current) return;
    const target = `${key}:${autoHead}`;
    const seen = autoTries.current.get(target);
    if (seen && (seen.tries >= AUTO_SEND_TRIES || seen.revision === snap.revision)) return;
    autoTries.current.set(target, { revision: snap.revision, tries: (seen?.tries ?? 0) + 1 });
    void sendNow(autoHead, true);
    // sendNow reads the current snapshot itself; the head and revision are the triggers.
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [autoHead, snap?.revision, key]);

  const turnOnAlwaysSendNow = (queuedTurnId: string) => {
    setAlwaysSendNow(true);
    // This click is a Send now too; the effect then follows with the next queued message.
    autoTries.current.set(`${key}:${queuedTurnId}`, { revision: snap?.revision ?? 0, tries: 1 });
    void sendNow(queuedTurnId);
  };

  // The second phase of a skip-permissions creation (#1339): nothing ran yet; the operator starts
  // it here or discards it (the existing stop closes a never-launched creation for good).
  const [starting, setStarting] = useState(false);
  const startPending = async () => {
    setStarting(true);
    setActionError(null);
    try {
      apply(await api.structuredStart(key));
    } catch (e) {
      setActionError(`Couldn’t start: ${detail(e, "no answer")}`);
    } finally {
      setStarting(false);
      await load();
    }
  };

  const stop = async () => {
    setStopping(true);
    setActionError(null);
    try {
      setContainment((await api.structuredStop(key)).containment);
    } catch (e) {
      setContainment("unknown");
      setActionError(`Stop not confirmed: ${detail(e, "no answer")}`);
    } finally {
      setStopping(false);
      await load();
    }
  };

  const enablePermissions = async () => {
    if (permissionSending.current || readOnly || !snap?.can_enable_bypass || snap.bypass) return;
    const id = snap.permission_change?.operation_id ?? permissionOperation.current ?? uuid();
    permissionOperation.current = id;
    permissionSending.current = true;
    setEnablingPermissions(true);
    setPermissionError(null);
    try {
      apply(await api.structuredEnableBypass(key, id));
      setPermissionsOpen(null);
    } catch (e) {
      const latest = await load();
      if (latest?.bypass) setPermissionsOpen(null);
      else {
        if (definite(e) && !latest?.permission_change?.operation_id)
          permissionOperation.current = null;
        setPermissionError(
          `Skip permissions is not confirmed. ${detail(e, "The response was lost.")} Retry to check and finish the same change.`,
        );
      }
    } finally {
      permissionSending.current = false;
      setEnablingPermissions(false);
    }
  };

  /** Upload pictures into this send (#1332 Phase 3). The server re-checks every one — format by
   *  content, size, count — when the turn is submitted; this only keeps the obvious out. */
  const clearAttachments = () => {
    // A new draft: the old one's pending uploads and clipboard reads are abandoned — their slots
    // are released here, and their completions (fenced by generation) touch nothing of this one.
    generation.current += 1;
    slots.current.held = 0;
    slots.current.inFlight = 0;
    setInFlight(0);
    setAttachments([]);
  };
  const removeAttachment = (stored: string) => {
    slots.current.held -= 1;
    setAttachments((prev) => prev.filter((x) => x.stored !== stored));
  };

  // The retry this pane is holding (a lost answer, a failed re-read) has landed: retire its id
  // so Send can never replay it (Hermes on #1346). If the draft is still exactly that message, it
  // WAS sent — clear it as a send does; an edited draft stays, as a new request.
  useEffect(() => {
    const held = op.current;
    if (!held || !snap || sending.current) return;
    if (!operatorTurns(snap).some((t) => t.turn_id === held.id)) return;
    op.current = null;
    const names = attachments.map((a) => a.stored);
    if (sendIdentity(draft.trim(), names) === held.text) {
      setDraft("");
      clearAttachments();
      setSendError(null);
    }
  }, [snap, draft, attachments]);

  /** Restore / Insert: replace the draft with supported uploads, up to the shared cap. */
  const fill = (text: string, uploads: string[]): string[] | null => {
    // Tools are disabled then too. A live dictation would overwrite a replaced draft with its
    // next result, so it counts as in flight.
    if (sending.current || slots.current.inFlight > 0 || !dict.settled()) return null;
    clearAttachments();
    const supported = snap?.files
      ? uploads
      : snap?.images ? uploads.filter((p) => /\.(png|jpe?g|gif|webp)$/i.test(p)) : [];
    const kept = supported.slice(0, MAX_ATTACHMENTS).map((p) => {
      const stored = uploadStoredName(p);
      return { name: attachmentName(stored), stored };
    });
    slots.current.held = kept.length;
    setAttachments(kept);
    op.current = null; // a new draft is a new request
    setDraft(text);
    const dropped = uploads.length - kept.length;
    setSendError(
      dropped > 0
        ? `${dropped} attachment${dropped === 1 ? " was" : "s were"} left out: ${
            snap?.files || snap?.images
              ? `only up to ${MAX_ATTACHMENTS} ${snap.files ? "files" : "images"} can be sent here`
              : `${agent} takes no images`
          }.`
        : null,
    );
    requestAnimationFrame(() => taRef.current?.focus());
    return kept.map((k) => k.stored);
  };
  useEffect(() => {
    if (!sendAfterDictation.current || dictFinalizing || !dict.settled()) return;
    sendAfterDictation.current = false;
    void send();
    // eslint-disable-next-line react-hooks/exhaustive-deps -- re-checked whenever dictation or the draft moves; `send` reads the latest state
  }, [dictFinalizing, dictListening, draft]);

  // Restore and Insert make a FRESH draft, and only while nothing is in flight — a restore can
  // never abandon work or interleave with a send (Hermes on #1346). One exception keeps a
  // possibly-recorded turn from running twice: THIS session's entry whose outcome is unknown
  // re-sends unchanged under its own operation id, so the server replays it if it has it. A
  // confirmed entry is a deliberate repeat (new id); any edit is a new request.
  const restoreSent = (entry: SentMessage) => {
    setHistoryOpen(null);
    const names = fill(entry.text, entry.attachments);
    if (names && entry.operation && entry.session === key && !entry.confirmed) {
      op.current = {
        id: entry.operation,
        text: sendIdentity(entry.text.trim(), names),
      };
      historyFor.current = { op: entry.operation, id: entry.id };
    }
  };
  // INSERT only: a template with secret fields is rendered and delivered by the server, which
  // has no structured-session path yet — the picker disables Insert for it, so no secret ever
  // reaches this text box.
  const insertTemplate = (t: Template, values: Record<string, string>) => {
    setTemplatesOpen(null);
    fill(
      substituteFields(t.body, t.fields, values),
      t.images.map((i) => i.path),
    );
  };

  const attach = async (files: File[]) => {
    if (sending.current) {
      setSendError("Wait until this message is sent before attaching another file.");
      return;
    }
    const supported = snap?.files ? files : files.filter((f) => f.type.startsWith("image/"));
    if (!supported.length) {
      if (files.length) setSendError("Only images can be attached here.");
      return;
    }
    // Reserve slots NOW, before any await: a second paste racing this one sees them taken.
    const room = MAX_ATTACHMENTS - slots.current.held - slots.current.inFlight;
    const taken = supported.slice(0, Math.max(0, room));
    if (taken.length < supported.length) {
      setSendError(`A message carries at most ${MAX_ATTACHMENTS} ${snap?.files ? "files" : "images"}.`);
    } else setSendError(null);
    if (!taken.length) return;
    slots.current.inFlight += taken.length;
    setInFlight(slots.current.inFlight);
    const gen = generation.current;
    let failure: string | null = null;
    try {
      for (const file of taken) {
        try {
          const up = await api.upload(file);
          if (gen !== generation.current) continue; // its draft was sent or cleared meanwhile
          const stored = up.stored ?? uploadStoredName(up.path);
          slots.current.held += 1;
          setAttachments((prev) => [...prev, { name: up.name, stored }]);
        } catch (e) {
          failure = e instanceof ApiError && e.status === 413
            ? `${file.name}: file too large (max 25 MB).`
            : detail(e, "A file could not be uploaded.");
        } finally {
          // An abandoned draft's slot was already released by `clearAttachments`.
          if (gen === generation.current) {
            slots.current.inFlight -= 1;
            setInFlight(slots.current.inFlight);
          }
        }
      }
    } finally {
      if (gen === generation.current) {
        if (failure) setSendError(failure);
        if (fileRef.current) fileRef.current.value = "";
      }
    }
  };

  // A file paste becomes an attachment; plain text is left to the textarea. A deferred
  // clipboard that delivers neither falls back to the async clipboard (#530), as Compose does.
  const onPaste = (e: ReactClipboardEvent<HTMLTextAreaElement>) => {
    if (!snap?.files && !snap?.images) return;
    if (sending.current) {
      // A read-only textarea still receives paste: nothing joins a message already sending.
      e.preventDefault();
      return;
    }
    const files = snap.files ? filesFromData(e.clipboardData) : imageFilesFromData(e.clipboardData);
    if (files.length) {
      e.preventDefault();
      void attach(files);
      return;
    }
    if (e.clipboardData?.getData("text/plain")) return;
    if (!Array.from(e.clipboardData?.items ?? []).some((i) => i.kind === "file")) return;
    e.preventDefault();
    // The deferred read holds a slot like an upload does, so Send waits for it too.
    slots.current.inFlight += 1;
    setInFlight(slots.current.inFlight);
    const gen = generation.current;
    const release = () => {
      if (gen !== generation.current) return; // released already, with its draft
      slots.current.inFlight -= 1;
      setInFlight(slots.current.inFlight);
    };
    void imageFilesFromAsyncClipboard().then((fallback) => {
      release();
      if (fallback.length && gen === generation.current) void attach(fallback);
    }, release);
  };

  const onKey = (e: KeyboardEvent<HTMLTextAreaElement>) => {
    if (e.key === "Enter" && !e.shiftKey && !e.nativeEvent.isComposing) {
      e.preventDefault();
      void send();
    }
  };

  const live = !!snap?.native?.worker;
  const workerChip = reconnecting
    ? { cls: styles.ledWarn, text: "reconnecting" }
    : containment === "gone"
      ? { cls: styles.ledIdle, text: "stopped" }
      : containment === "unknown"
        ? { cls: styles.ledWarn, text: "stop unconfirmed" }
        : live
          ? { cls: styles.ledUp, text: "worker live" }
          : { cls: styles.ledIdle, text: "no worker" };
  const model = snap?.model_effective ?? snap?.model_requested ?? null;
  // The requested level (#1407) rides beside the model it belongs to.
  const effort = model ? (snap?.effort_requested ?? null) : null;

  // A map window's chrome LED reads this pane's connection state (#1109), as it reads the
  // terminal's socket status: the journal read is this pane's connection.
  const status: TermStatus = loadError
    ? { kind: "rejected", reason: loadError }
    : reconnecting
      ? { kind: "reconnecting", attempt: 1 }
      : snap
        ? { kind: "connected" }
        : { kind: "connecting" };
  const onTermStatus = host.onTermStatus;
  const statusKind = status.kind;
  const statusReason = status.kind === "rejected" ? status.reason : "";
  useEffect(() => {
    if (!onTermStatus) return;
    onTermStatus(
      statusKind === "rejected"
        ? { kind: "rejected", reason: statusReason }
        : statusKind === "reconnecting"
          ? { kind: "reconnecting", attempt: 1 }
          : { kind: statusKind },
    );
  }, [onTermStatus, statusKind, statusReason]);

  // The pane head (#1332): the terminal's actions, in the terminal's order, wherever they work
  // for an API session. Recap and Hand off read the session's journal (#1311's transcript fold);
  // Adopt to mission is NOT offered — a mission cannot drive a structured session yet (#1273
  // Phase 3), so adopting one would underwrite work nothing can do. Repaint and text size are
  // terminal controls.
  const placeholder = isNewSessionPlaceholder(actionKey);
  const actionNative = actionKey.slice(actionKey.indexOf(":") + 1);
  const headActions: HeadAction[] = [
    ...(snap?.can_enable_bypass && !readOnly
      ? [
          {
            id: "skip-permissions",
            label: "Skip permissions",
            aria: "Enable Skip permissions for this session",
            title: "Enable Skip permissions for this existing conversation",
            icon: <ShieldOff size={13} aria-hidden="true" />,
            disabled: enablingPermissions,
            run: (trigger?: HTMLElement | null) =>
              setPermissionsOpen({ trigger: trigger ?? taRef.current }),
          },
        ]
      : []),
    ...(host.onToggleFiles
      ? [
          {
            id: "files",
            label: "Files",
            aria: "Browse session files",
            title: host.filesDisabledReason ?? "Browse this session's files and folders",
            icon: <PanelRight size={13} aria-hidden="true" />,
            active: host.filesOpen,
            disabled: Boolean(host.filesDisabledReason),
            run: (trigger?: HTMLElement | null) => host.onToggleFiles?.(trigger),
          },
        ]
      : []),
    {
      id: "recap",
      label: "Recap",
      aria: "Open session brief",
      title: "Session brief: full title, summary, and a chronological recap of this session",
      icon: <ScrollText size={13} aria-hidden="true" />,
      run: (trigger?: HTMLElement | null) =>
        setRecap({
          open: true,
          trigger: trigger ?? (document.activeElement as HTMLElement | null),
        }),
    },
    ...(isAgent(engine) && !placeholder
      ? [
          {
            id: "handoff",
            label: "Hand off",
            aria: "Hand off session to another engine",
            title:
              "Hand off: start a new session in another engine, seeded with this session's context",
            icon: <ArrowLeftRight size={13} aria-hidden="true" />,
            run: (trigger?: HTMLElement | null) =>
              setHandoff({
                open: true,
                trigger: trigger ?? (document.activeElement as HTMLElement | null),
              }),
          },
        ]
      : []),
    ...(host.onToMap
      ? [
          {
            id: "to-map",
            label: "To map",
            aria: "Open this session as a window on the map",
            title:
              "To map: open this session as a floating window on the overview map, alongside the others",
            icon: <SquareDashedBottom size={13} aria-hidden="true" />,
            run: () => host.onToMap?.(),
          },
        ]
      : []),
    ...(!placeholder
      ? [
          {
            id: "share-link",
            label: "Share link",
            aria: "Share a link to this session",
            title: "Share link: send or copy a link that opens this session",
            icon: <Share2 size={13} aria-hidden="true" />,
            run: () => {
              void shareLink({
                title: row?.title || "BattleLab session",
                path: `/s/${actionKey.slice(0, actionKey.indexOf(":"))}/${actionNative}`,
              }).then((outcome) => {
                if (outcome !== "copied" && outcome !== "failed") return;
                const tick = Date.now();
                setLinkToast({ tick, ok: outcome === "copied" });
                window.setTimeout(
                  () => setLinkToast((t) => (t.tick === tick ? { tick: 0, ok: true } : t)),
                  outcome === "copied" ? LINK_TOAST_MS : LINK_FAILED_TOAST_MS,
                );
              });
            },
          },
          // Beside Share link, not instead of it: the operator's one-tap link (#1232) stays one tap.
          {
            id: "share-public",
            label: "Share publicly…",
            aria: "Share this session publicly",
            title:
              "Share publicly: a link anyone can open without a BattleLab account — view only or able to send, with an optional password and expiry",
            icon: <Globe size={13} aria-hidden="true" />,
            run: (trigger?: HTMLElement | null) =>
              setSharePublic({
                open: true,
                trigger: trigger ?? (document.activeElement as HTMLElement | null),
              }),
          },
        ]
      : []),
    // Stop is a head action while a worker runs, in the pane's own bar and in a window's chrome
    // (and either one's ⋯): a running worker is never stranded behind a missing button.
    ...(live
      ? [
          {
            id: "stop",
            label: "Stop",
            aria: "Stop the worker",
            title: "Stop: end this session's worker and report whether it is confirmed gone",
            icon: <Square size={13} aria-hidden="true" />,
            disabled: stopping,
            run: () => void stop(),
          },
        ]
      : []),
  ];

  // The composer's chips, in the terminal's order where they overlap (attach, Sent, Templates).
  // The terminal-only keys (arrows, esc, tab, collapse) have nothing to drive here.
  const toolsBusy = busy || uploading || dictating;
  const keyActions: KeyAction[] =
    readOnly || !snap
      ? []
      : [
          ...(snap.files || snap.images
            ? [
                {
                  id: "attach",
                  aria: snap.files ? "Attach files" : "Attach images",
                  title: snap.files ? "Attach files" : "Attach images",
                  icon: <Paperclip size={16} />,
                  disabled: busy || attachments.length + inFlight >= MAX_ATTACHMENTS,
                  run: () => fileRef.current?.click(),
                },
              ]
            : []),
          ...(history.length > 0
            ? [
                {
                  id: "history",
                  aria: "Sent messages",
                  title: `Sent messages (last ${history.length})`,
                  icon: <History size={16} />,
                  disabled: toolsBusy,
                  run: () => {
                    setHistory(readSent()); // another tab may have sent since we last looked
                    // The chip may sit in KeyBar's "…" menu: focus returns to whatever held it.
                    setHistoryOpen(document.activeElement as HTMLElement);
                  },
                },
              ]
            : []),
          {
            id: "templates",
            aria: "Use a template",
            title: "Use a saved template — fill it in, then insert it here",
            icon: <BookMarked size={16} />,
            disabled: toolsBusy,
            run: () => setTemplatesOpen(document.activeElement as HTMLElement),
          },
        ];

  // The terminal session's own bar (#1348): the shared facts run + the same actions, so an API
  // session and a terminal session cannot drift apart. The journal read is this pane's link LED.
  const header = host.suppressHead ? null : (
    <div className={term.panelHead} data-panel-head="">
      <span className={term.headLeft}>
        <HeadFacts engine={engine} status={status} row={row} />
        {/* Show the effective mode, including an explicit change after starting. */}
        {snap?.bypass && (
          <span
            className={`${chat.chip} ${styles.skipChip}`}
            data-testid="structured-bypass"
            title="Skip permission prompts is enabled: this agent never asks before acting."
          >
            <span className={styles.skipLong}>Skip permissions</span>
            <span className={styles.skipShort} aria-hidden="true">
              Skip
            </span>
          </span>
        )}
      </span>
      <HeadActions
        className={term.headActions}
        btnClassName={term.restartBtn}
        labelClassName={term.headActionLabel}
        actions={headActions}
        collapsed={isMobile}
      />
    </div>
  );

  const overlays = (
    <>
      {permissionsOpen && (
        <PermissionsDialog
          busy={enablingPermissions}
          retry={!!snap?.permission_change?.operation_id || !!permissionError}
          active={active}
          error={permissionError}
          onConfirm={() => void enablePermissions()}
          onClose={() => setPermissionsOpen(null)}
          returnFocusTo={permissionsOpen.trigger}
          fallbackFocusRef={taRef}
        />
      )}
      {host.suppressHead &&
        host.headActionsSlot &&
        createPortal(
          <HeadActions
            className={term.headActions}
            btnClassName={term.restartBtn}
            labelClassName={term.headActionLabel}
            actions={headActions}
            collapsed={isMobile}
            reservePx={host.headReservePx}
            foldInto="external"
            overflowRef={host.headOverflowRef}
            allRef={host.headAllRef}
            barRef={host.headBarRef}
          />,
          host.headActionsSlot,
        )}
      {recap.open && (
        <SessionRecapModal
          sessionId={actionKey}
          engine={engine}
          title={row?.title ?? agent}
          project={row?.project}
          lastMtime={row?.last_mtime}
          statusRow={row}
          summary={row?.ai_summary}
          recap={row?.ai_recap}
          interventionRequired={row?.intervention_required}
          interventionReason={row?.intervention_reason}
          reviewedAt={row?.reviewed_at}
          reviewExcluded={row?.review_excluded}
          onClose={() => setRecap({ open: false, trigger: null })}
          returnFocusTo={recap.trigger}
        />
      )}
      {sharePublic.open && (
        <SharePubliclyModal
          sessionKey={actionKey}
          title={row?.title || "BattleLab session"}
          agent={engineLabel(engine)}
          onClose={() => setSharePublic({ open: false, trigger: null })}
          returnFocusTo={sharePublic.trigger}
        />
      )}
      {handoff.open && (
        <HandoffModal
          sessionId={actionKey}
          engine={engine}
          title={row?.title ?? agent}
          onClose={() => setHandoff({ open: false, trigger: null })}
          returnFocusTo={handoff.trigger}
        />
      )}
      {linkToast.tick !== 0 && (
        <div
          key={linkToast.tick}
          className={linkToast.ok ? term.copiedToast : `${term.copiedToast} ${term.copyFailed}`}
          role="status"
          aria-live="polite"
          data-link-toast=""
        >
          {linkToast.ok ? "Link copied" : "Copy needs a secure origin"}
        </div>
      )}
    </>
  );

  if (loadError) {
    return (
      <div className={`${chat.pane} ${styles.pane}`} data-testid="structured-pane">
        {header}
        {overlays}
        <div className={chat.center}>
          <p className={chat.note}>{loadError}</p>
        </div>
      </div>
    );
  }

  const turns = snap?.turns ?? [];
  const groups = conversationGroups(turns);
  const positions = queuePositions(snap);
  const questionTurn = openQuestionTurn(snap);
  const questionFor = (t: {
    turn_id: string;
    questions?: { title: string; options: string[] }[];
    question_id?: string;
  }) => {
    if (t.turn_id !== questionTurn || !t.questions?.length || !t.question_id) return undefined;
    const questions = t.questions;
    const question = t.question_id;
    // An answer in flight, or one whose outcome is unknown, holds this question: its only action
    // is the identical Retry, so a different pick can't race it.
    const mine = answer?.session === key && answer.question === question ? answer : null;
    const holding = mine?.phase === "sending" || mine?.phase === "unknown";
    return (
      <ChoiceQuestions
        key={`${t.turn_id}:${question}`}
        heading={`${agent} asks · it keeps working meanwhile`}
        questions={questions.map((q) => ({ title: q.title, options: q.options.map((label) => ({ label })) }))}
        disabled={readOnly || permissionPending || notStarted || busy || holding}
        submitLabel={questions.length > 1 ? "Send answers" : "Send answer"}
        onSubmit={(answers) =>
          void sendAnswer({
            question,
            id: uuid(),
            text: answerText(questions, answers.map((a) => a.selected[0])),
          })
        }
        status={
          mine && (
            <>
              <p className={styles.pendingDecision} role="status" data-testid="structured-question-status">
                {mine.phase === "sending"
                  ? `Sending “${mine.text}”…`
                  : mine.phase === "unknown"
                    ? `“${mine.text}” may not have reached BattleLab. Retrying sends the same message.`
                    : `Not sent: ${mine.message}`}
              </p>
              {mine.phase === "unknown" && (
                <button
                  type="button"
                  className={`${chat.ghost} ${styles.inline}`}
                  disabled={readOnly || permissionPending || busy}
                  onClick={() => void sendAnswer({ question: mine.question, id: mine.id, text: mine.text })}
                  data-testid="structured-question-retry"
                >
                  Retry answer
                </button>
              )}
            </>
          )
        }
        hint={(answers) => {
          const text = answerText(questions, answers.map((a) => a.selected[0]));
          return answers.every((a) => a.selected.length)
            ? `Sent as your next message, after earlier turns: “${text}”. Or type your own answer below.`
            : "Pick an option, or type your own answer below.";
        }}
      />
    );
  };
  // Where the background-task row goes: above the first queued message, else last (-1: none).
  const firstQueued = groups.findIndex(({ turn: t }) => !isAgentTurn(t) && t.state === "queued");
  const backgroundAt = !backgroundWorking(snap) ? -1 : firstQueued === -1 ? groups.length : firstQueued;
  // One keyed list keeps the task panel's DOM (including focus and open Details) when a
  // queued message moves it from the transcript tail to before the queue (#1461).
  const conversation: (ConversationGroup | null)[] = [...groups];
  if (backgroundAt >= 0) conversation.splice(backgroundAt, 0, null);
  const pending = snap?.pending_requests ?? [];
  const renderTurn = (t: StructuredTurn, beforeReply?: ReactNode) => (
    <TurnView
      key={t.turn_id}
      beforeReply={beforeReply}
      turn={t}
      folder={snap?.cwd}
      agent={agent}
      requests={pending.filter((r) => r.turn_id === t.turn_id)}
      deciding={deciding}
      readOnly={readOnly}
      approvalsDisabled={permissionPending}
      onDecide={(r, c, g, a) => void decide(r, c, g, a)}
      onInterrupt={(tid) => void interrupt(tid)}
      interrupting={interrupting || stoppingNow === t.turn_id}
      onSendNow={(tid) => void sendNow(tid)}
      sendNowMode={sendNowTarget(snap)?.mode}
      sendingNow={sendingNow === t.turn_id}
      queuePosition={positions.get(t.turn_id)}
      cutOff={cutBySendNow(snap, t)}
      waiting={t.turn_id === snap?.active_turn && waitingBehindOwnTurn(snap)}
      sendNowDisabled={permissionPending || !!sendingNow || !!recoveringSendNow || operatorTurns(snap).some((q) => q.state === "queued" && q.delivery === "interrupting")}
      alwaysSendNow={alwaysSendNow}
      onAlwaysSendNow={turnOnAlwaysSendNow}
      onRemove={(tid) => void removeQueued(tid)}
      removing={removing === t.turn_id}
      canRemove={!!snap?.native?.withdraw}
      question={questionFor(t)}
    />
  );
  return (
    <div className={`${chat.pane} ${styles.pane}`} data-testid="structured-pane" onKeyDown={onPaneKey}>
      {header}
      {overlays}
      {snap?.permission_change?.state === "stopping" && !enablingPermissions && (
        <div className={styles.banner} role="status">
          <p>
            Enabling Skip permissions has not finished. New messages are paused until the session’s
            tools are confirmed stopped.
          </p>
          {!readOnly && (
            <button
              type="button"
              className={chat.send}
              onClick={(e) => setPermissionsOpen({ trigger: e.currentTarget })}
            >
              Retry enabling
            </button>
          )}
        </div>
      )}
      {snap?.pending_start && (
        <div className={styles.banner} role="status" data-testid="structured-pending-start">
          <p>
            {snap.start_incomplete
              ? "This session’s start didn’t finish. Nothing has run; start it again or discard it."
              : "This session skips permission prompts and hasn’t started. Nothing has run."}
          </p>
          <button
            type="button"
            className={chat.send}
            disabled={starting || stopping}
            onClick={() => void startPending()}
          >
            Start (skips prompts)
          </button>
          <button
            type="button"
            className={`${chat.ghost} ${styles.inline}`}
            disabled={starting || stopping}
            onClick={() => void stop()}
          >
            Discard
          </button>
        </div>
      )}
      {snap?.start_expired && (
        <p className={styles.banner} role="status" data-testid="structured-start-expired">
          This session’s start expired before it was confirmed. It never ran.
        </p>
      )}
      {/* A map window hides this pane's head behind its own chrome (#1109), and that chrome
          carries no permission mode: say it here, always visible, so a session that never asks
          is never mistaken for one that does (#1339, Hermes 5948). */}
      {host.suppressHead && snap?.bypass && (
        <p
          className={`${styles.banner} ${styles.skipBanner}`}
          role="status"
          data-testid="structured-bypass-banner"
        >
          <b>Skip permissions</b> — this agent never asks before acting.
        </p>
      )}
      {readOnly && (
        <p className={styles.banner} role="status" data-testid="structured-read-only">
          Read only — {snap?.read_only}. The history stays; new messages and decisions are off until
          the client is available again.
        </p>
      )}
      <div className={chat.log} ref={logRef} aria-live="polite">
        {snap && turns.length === 0 && (
          <div className={chat.center} data-testid="structured-empty">
            <section className={styles.info} aria-label="Session info">
              <p className={styles.infoTag}>{agent} · API</p>
              <dl className={styles.infoFacts}>
                <div>
                  <dt>Agent</dt>
                  <dd>{agent}</dd>
                </div>
                <div>
                  <dt>Client</dt>
                  <dd>{engineLabel(engine)}</dd>
                </div>
                <div>
                  <dt>Model</dt>
                  <dd data-testid="structured-model">
                    {model ?? "agent default"}
                    {effort ? ` · ${effort}` : ""}
                  </dd>
                </div>
                <div>
                  <dt>Folder</dt>
                  <dd>
                    <code title={snap.cwd}>{snap.cwd}</code>
                  </dd>
                </div>
                <div>
                  <dt>Worker</dt>
                  <dd data-testid="structured-worker" role="status">
                    <span className={`${styles.led} ${workerChip.cls}`} aria-hidden="true" />
                    {workerChip.text}
                  </dd>
                </div>
              </dl>
            </section>
          </div>
        )}
        {snap && snap.omitted_turns > 0 && (
          <p className={chat.notice}>{snap.omitted_turns} earlier turns are not shown.</p>
        )}
        {conversation.map((group) => {
          if (group === null) return (
            <BackgroundWork key="background" native={snap?.native} />
          );
          const { turn: t, additions } = group;
          return (
            <Fragment key={t.turn_id}>
              {isAgentTurn(t) ? (
                <AgentTurnView
                  beforeReply={additions.map((added) => renderTurn(added))}
                  turn={t}
                  agent={agent}
                  interruptible={ownTurnInterruptible(snap, t)}
                  ahead={waitingBehindOwnTurn(snap)}
                  readOnly={readOnly}
                  live={!!snap?.native?.worker}
                  stopping={interrupting || stoppingNow === t.turn_id}
                  onInterrupt={(tid) => void interrupt(tid)}
                  question={questionFor(t)}
                />
              ) : renderTurn(t, additions.map((added) => renderTurn(added)))}
            </Fragment>
          );
        })}
        {recoveringSendNow && (
          <div className={styles.queued} data-testid="structured-send-now-recovery">
            <div className={styles.queueRow}>
              <span role="alert">Send now status is unknown.</span>
              <button type="button" className={styles.sendNow} disabled={!!sendingNow}
                onClick={() => void sendNow(recoveringSendNow.queuedTurnId)}>
                {sendingNow ? "Checking…" : "Retry Send now"}
              </button>
            </div>
            <p className={styles.queueHint}>Checks the original request without sending it twice.</p>
          </div>
        )}
        {reconnecting && snap && (
          <p className={chat.notice} data-testid="structured-reconnecting">
            Connection to BattleLab lost. The turn keeps running on the server; this view resumes
            from event {snap.event_cursor} when it reconnects. Nothing was cancelled or re-sent.
          </p>
        )}
      </div>
      {(sendError || actionError) && (
        <p className={chat.sendError} role="alert" data-testid="structured-error">
          {sendError ?? actionError}
        </p>
      )}
      {/* The terminal session's composer (#1348): Compose.module.css itself, so the two cannot
          drift — pills, the text box, then ONE row of chips, the push-to-talk chip and Send. */}
      <form
        className={compose.compose}
        aria-label="Compose message"
        onSubmit={(e) => {
          e.preventDefault();
          void send();
        }}
      >
        <div className={compose.fields}>
          {attachments.length > 0 && (
            <div className={compose.pills} aria-label="Attachments" role="list">
              {attachments.map((a) => (
                <span
                  key={a.stored}
                  className={compose.pill}
                  title={a.name}
                  role="listitem"
                  data-testid="structured-attachment"
                >
                  <span className={compose.pn}>{a.name}</span>
                  <button
                    type="button"
                    aria-label={`Remove ${a.name}`}
                    disabled={busy}
                    onClick={() => removeAttachment(a.stored)}
                  >
                    ×
                  </button>
                </span>
              ))}
            </div>
          )}
          <textarea
            ref={taRef}
            className={compose.textarea}
            aria-label={`Message ${agent}`}
            placeholder={
              readOnly
                ? "This conversation is read only"
                : notStarted
                  ? "Start this session first"
                  : active && alwaysSendNow && sendNowTarget(snap)
                    ? sendNowTarget(snap)?.mode === "steer"
                      ? `Message ${agent} — sends now, into the current turn.`
                      : `Message ${agent} — sends now, stopping the current reply.`
                    : active
                    ? `Message ${agent} — sends after earlier turns.`
                    : `Message ${agent} — Enter sends, Shift+Enter = newline.`
            }
            value={draft}
            onChange={(e) => setDraft(e.target.value)}
            onKeyDown={onKey}
            onPaste={onPaste}
            readOnly={busy}
            rows={2}
            disabled={readOnly || notStarted || permissionPending}
          />
        </div>
        <div className={`${compose.row} ${styles.composeRow}`}>
          <div className={styles.composeTools}>
            <KeyBar actions={keyActions} />
            {!readOnly && (alwaysSendNow || snap?.native?.send_now) && (
              <button
                type="button"
                className={styles.alwaysChip}
                aria-pressed={alwaysSendNow}
                title={alwaysSendNow
                  ? "Always send now is on. Click to queue messages again."
                  : snap?.native?.send_now === "steer"
                    ? "Send each queued message into the current turn while the agent works."
                    : "Stop the current reply and send each queued message while the agent works."}
                onClick={() => setAlwaysSendNow(!alwaysSendNow)}
                data-testid="structured-always-send-now"
              >
                <Zap size={14} aria-hidden />
                Always send now
              </button>
            )}
          </div>
          <span className={`${compose.spacer} ${styles.composeNote}`} role="status">
            {dictNote}
          </span>
          {!readOnly && snap && dictSupported && (
            <button
              type="button"
              ref={micBtnRef}
              className={
                dictListening
                  ? `${compose.mic} ${compose.micOn}`
                  : dictFinalizing
                    ? `${compose.mic} ${compose.micFinalizing}`
                    : compose.mic
              }
              aria-label={
                dictListening
                  ? "Stop voice input — release to finish"
                  : dictFinalizing
                    ? "Finishing voice input"
                    : "Start voice input — hold to talk"
              }
              aria-pressed={dictListening}
              aria-disabled={dictFinalizing || busy}
              title={
                dictListening
                  ? "Release to stop — the last phrase still lands"
                  : dictFinalizing
                    ? "Finishing transcription…"
                    : "Hold to talk"
              }
              disabled={busy}
              {...micHandlers}
            >
              <Mic size={16} aria-hidden />
              <span className={compose.micLabel}>Talk</span>
            </button>
          )}
          <button
            type="submit"
            className={`${compose.send} shine`}
            title="Send + Enter"
            disabled={
              readOnly ||
              notStarted ||
              permissionPending ||
              busy ||
              uploading ||
              !snap ||
              (!draft.trim() && attachments.length === 0)
            }
          >
            <Send size={15} aria-hidden />
            {uploading ? "Uploading" : "Send"}
          </button>
        </div>
        {snap?.native?.worker && snap.files === false && !readOnly && (
          <div className={chat.note}>
            To attach documents, stop this session, then send a message to resume it.
          </div>
        )}
        {(snap?.files || snap?.images) && !readOnly && (
          <input
            ref={fileRef}
            type="file"
            accept={snap.files ? undefined : "image/png,image/jpeg,image/gif,image/webp"}
            multiple
            hidden
            data-testid="structured-file-input"
            onChange={(e) => void attach(Array.from(e.target.files ?? []))}
          />
        )}
      </form>
      {historyOpen && (
        <SentMessagesModal
          entries={history}
          currentSession={key}
          onRestore={restoreSent}
          onClose={() => setHistoryOpen(null)}
          returnFocusTo={historyOpen}
        />
      )}
      {templatesOpen && (
        <TemplatePickerModal
          onInsert={insertTemplate}
          insertLabel="Insert into message"
          insertTarget="message"
          onClose={() => setTemplatesOpen(null)}
          returnFocusTo={templatesOpen}
        />
      )}
    </div>
  );
}
