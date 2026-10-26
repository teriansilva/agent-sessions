import { useApiLogProps } from "../../theme/apiLookStore";
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
import { useNavigate } from "react-router-dom";
import { memorySentence, ownLimitHelps } from "../../lib/memoryPressure";
import { settingsPath } from "../../routes/settingsTabs";
import { engineInfo, engineLabel, isAgent, useEngineRoster } from "../../app/engineRoster";
import { isNewSessionPlaceholder } from "../../app/sessionsStore";
import { useSessionLookup } from "../../app/useSessionRow";
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
import { humanDuration } from "../../lib/format";
import { useCompactHead, useIsMobile } from "../../lib/useIsMobile";
import { SESSIONS_PATH } from "../../lib/routes";
import { reloadRoster } from "../../app/reloadRoster";
import { HandoffModal } from "../terminal/HandoffModal";
import { HeadActions, type HeadAction } from "../terminal/HeadActions";
import { HeadFacts } from "../terminal/HeadFacts";
import { KeyBar, type KeyAction } from "../terminal/KeyBar";
import type { PaneHost } from "../terminal/paneHost";
import { SessionRecapModal } from "../terminal/SessionRecapModal";
import { OpeningPanel } from "../terminal/SessionOpening";
import { resolveApiModel } from "../../lib/apiModel";
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
  StructuredBackgroundTask,
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
import { fold, interleave, type ReplyPart } from "./activityOrder";
import { autoSendNowHead, useAlwaysSendNow } from "./alwaysSendNow";
import { conversationGroups, type ConversationGroup } from "./conversationOrder";
import { PermissionsDialog } from "./PermissionsDialog";
import { consumeStart, peekStart, type StartOutcome } from "./startHandoff";
import { HistoryTop } from "./HistoryTop";
import { useApiHistory } from "./useApiHistory";
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
  questionAnswerIds,
  questionAnswerMessage,
  questionAnswerMode,
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
import {
  nextToPost,
  outboxEntry,
  reconcile,
  retried,
  revisionFor,
  unrecorded,
  updateEntry,
  withoutEntry,
  type OutboxEntry,
} from "./outbox";
import { OutboxBlock } from "./Outbox";
import { NotSentRow } from "./NotSent";
import {
  type Attachment as NotSentAttachment,
  discardNotSent,
  notSentDiscarded,
  notSentState,
} from "./notSent";
import { useAlive, useLeaveGuard, useOutbox } from "./useOutbox";
import { TermUplinkVeil } from "../terminal/TermUplinkVeil";
import { useUplinkVeil } from "../terminal/useUplinkVeil";

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
/** How long a read gap during a start is held from the uplink veil (#1647); the veil then adds its
 *  own LOST_GRACE_MS, so the gap shows after 15 s in all. */
export const START_VEIL_HOLD_MS = 10_000;
/** Files/pictures per message — the server checks the same shared four-attachment cap. */
const MAX_ATTACHMENTS = 4;

/** Match the server's safe display name. Keep collision/leading numeric prefixes intact. */
const attachmentName = (path: string) => uploadStoredName(path).replace(/^\d{8}-\d{6}-/, "");

const uuid = () => crypto.randomUUID();

function detail(e: unknown, fallback: string): string {
  return e instanceof ApiError && e.message ? e.message : fallback;
}

/** How long a queued message has waited (#1644), from the time it was queued as the server keeps
 *  it (its header shows that time): a refresh or a reconnect shows the same wait, never a timer
 *  that restarts with the page. */
function QueuedSince({ ts }: { ts: number }) {
  const [now, setNow] = useState(() => Date.now() / 1000);
  useEffect(() => {
    const timer = setInterval(() => setNow(Date.now() / 1000), 30_000);
    return () => clearInterval(timer);
  }, []);
  return (
    <p className={styles.queuedSince} data-testid="structured-queued-since">
      Queued for {humanDuration(Math.max(0, now - ts))}
    </p>
  );
}

/** The closed reason a Send now refusal carries (#1644), or null for any other refusal. */
function refusalReason(e: unknown): "would_stop_background" | "inventory_changed" | null {
  const reason = e instanceof ApiError ? (e.record as { reason?: unknown } | undefined)?.reason : null;
  return reason === "would_stop_background" || reason === "inventory_changed" ? reason : null;
}

/** A task as the Stop all and send choice names it: its own description, else its id. */
function taskLabel(task: StructuredBackgroundTask): string {
  return task.description || task.summary || task.task_type || task.task_id;
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
/** A question answered as the operator's next message (#1463 `message` mode), while it is on its
 *  way: its own operation id, so a retry resends the identical message and never duplicates it. */
type MessageAnswer = { id: string; text: string; phase: "sending" | "unknown" | "refused"; message?: string };
type OnAnswerAsMessage = (req: StructuredRequest, text: string, retry?: MessageAnswer) => void;

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
  sent,
  answered,
  onAnswerAsMessage,
}: {
  req: StructuredRequest;
  questions: NonNullable<ReturnType<typeof requestQuestions>>;
  agent: string;
  deciding: Deciding | null;
  readOnly: boolean;
  interrupting: boolean;
  onDecide: OnDecide;
  /** This question's answer on its way as a message (`message` mode). */
  sent: MessageAnswer | null;
  /** The answer already in the conversation (survives a reload; may come from another tab):
   *  its text, so the card says WHAT was sent. Only the stop is left. */
  answered: string | null;
  onAnswerAsMessage: OnAnswerAsMessage;
}) {
  const mine = deciding?.key === requestKey(req) ? deciding : null;
  const asMessage = questionAnswerMode(req) === "message";
  const sending = sent?.phase === "sending" || sent?.phase === "unknown" || answered !== null;
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
        disabled={readOnly || !!mine || sending || (asMessage && interrupting)}
        submitLabel={questions.length > 1 ? "Send answers" : "Send answer"}
        onSubmit={(answers) =>
          asMessage
            ? onAnswerAsMessage(req, questionAnswerMessage(questions, answers))
            : onDecide(req, "answer", undefined, answers.map(answerWire))
        }
        status={
          answered !== null && !mine ? (
            <p className={styles.pendingDecision} role="status" data-testid="structured-ask-message-status">
              {sent?.text && sent.text !== answered
                ? `Another answer was already sent, not yours: “${answered}”. Stop the question so ${agent} reads it, or answer again afterwards.`
                : `Your answers are sent — stopping the question so ${agent} reads them.`}
            </p>
          ) : sent && !mine ? (
            <p className={styles.pendingDecision} role="status" data-testid="structured-ask-message-status">
              {sent.phase === "sending"
                ? "Sending your answers…"
                : sent.phase === "unknown"
                  ? "Your answers may not have reached BattleLab. Retrying sends the same message."
                  : `Not sent: ${sent.message}`}
            </p>
          ) : mine && (
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
          answered !== null && (!mine || mine.phase === "unknown") ? (
            <button
              type="button"
              className={chat.send}
              disabled={readOnly || interrupting || mine?.phase === "sending"}
              data-testid="structured-ask-stop"
              onClick={() => onDecide(req, "cancel")}
            >
              Stop the question
            </button>
          ) : sent?.phase === "unknown" && !mine ? (
            <button
              type="button"
              className={chat.send}
              disabled={readOnly}
              data-testid="structured-ask-retry"
              onClick={() => onAnswerAsMessage(req, sent.text, sent)}
            >
              Retry answers
            </button>
          ) : mine?.phase === "unknown" ? (
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
                  disabled={readOnly || !!mine || sending || (c === "cancel" && interrupting)}
                  onClick={() => onDecide(req, c)}
                >
                  {choiceLabel(c, req)}
                </button>
              ))
          )
        }
        hint={() =>
          asMessage
            ? `This session can’t take answers directly, so ${agent}’s question is stopped and your answers arrive as your next message.`
            : req.choices.includes("reject")
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
            {agent} asked this, but BattleLab can’t offer it as choices: it carries no options it
            can read. Stop the turn and answer in a message, or skip it and {agent} carries on with
            its own judgement.
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

/** The tool rows grouped in the order they ran: one block before any text, or none. */
function activityGroups(reply: string | null, tools: StructuredTool[], breaks?: number[]) {
  const order = interleave(reply, tools, breaks);
  const groups = order
    ? [order.lead, ...order.parts.map((p) => p.tools)].filter((g) => g.length)
    : tools.length ? [tools] : [];
  return { order, first: groups[0], last: groups[groups.length - 1] };
}

/** What this view did not load before a long turn's reply (#1643): a truthful count, never shown
 *  as folded — the fold only ever holds loaded messages. */
function hasOmission(omitted?: { messages: number; chars: number } | null): boolean {
  return !!omitted && (omitted.messages > 0 || omitted.chars > 0);
}

function NotLoaded({ omitted }: { omitted?: { messages: number; chars: number } | null }) {
  if (!omitted || !hasOmission(omitted)) return null;
  const what = omitted.messages
    ? `${omitted.messages.toLocaleString()} earlier ${omitted.messages === 1 ? "update" : "updates"}`
    : "Earlier text";
  return (
    <p className={styles.notLoaded} data-testid="structured-not-loaded">
      … {what} not loaded — this view holds only the newest part of this turn
    </p>
  );
}

/** A long turn's reply (#1643): above eight loaded messages, the older ones behind one disclosure,
 *  the newest three (a finished turn's final reply among them) always out. A first message whose
 *  start was not loaded says so before its text. */
function FoldedReply({
  parts,
  partialStart,
  renderPart,
  signals,
}: {
  parts: ReplyPart[];
  partialStart: boolean;
  renderPart: (part: ReplyPart, end: boolean) => ReactNode;
  /** What the folded activity says that must never be hidden by the fold (Hermes on #1651): the
   *  live "Working" of the newest calls, failed and risky activity. Shown on its summary. */
  signals: (folded: StructuredTool[][]) => ReactNode;
}) {
  const folded = fold(parts);
  const lead = partialStart && (
    <p className={styles.notLoaded} data-testid="structured-partial-start">
      … start of this message not loaded
    </p>
  );
  const run = (list: ReplyPart[], last: boolean) =>
    list.map((part, i) => (
      <Fragment key={i}>
        {i === 0 && part.message === 0 && lead}
        {renderPart(part, last && i === list.length - 1)}
      </Fragment>
    ));
  if (!folded) return <>{run(parts, true)}</>;
  return (
    <>
      <details className={styles.fold} data-testid="structured-fold">
        <summary>
          {folded.count.toLocaleString()} earlier {folded.count === 1 ? "update" : "updates"}
          {signals(folded.folded.map((p) => p.tools))}
        </summary>
        {run(folded.folded, false)}
      </details>
      {run(folded.shown, true)}
    </>
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
  const { order, first, last } = activityGroups(turn.reply, turn.tools, turn.reply_breaks);
  // The turn's state rides on its newest block; only the oldest can be missing earlier calls.
  const activity = (tools: StructuredTool[]) => (
    <details className={styles.activity} data-testid="structured-agent-activity">
      <summary>
        {running && tools === last && bars}
        {running && tools === last && "Working on its own · "}
        {toolCount(tools, turn.tools_truncated && tools === first)}
        {ended && tools === last && <span className={styles.activityFailed}> · {ended}</span>}
        {tools.some((t) => t.risk?.level === "risky") && <span className={styles.activityRisk}> · Risky activity</span>}
        {running && tools === last && latest && (
          <span className={styles.latest} data-testid="structured-agent-latest">
            <b>{latest.name}</b> {latest.summary}
          </span>
        )}
      </summary>
      <ToolList tools={tools} label="What the agent is doing on its own" />
    </details>
  );
  const reply = (text: string, end: boolean) => (
    <div className={`${chat.txt} ${styles.ownReply}`} data-testid="structured-agent-reply">
      <Markdown text={end && turn.reply_truncated ? `${text}…` : text} />
    </div>
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
        (order ? order.lead.length > 0 : true) && activity(order ? order.lead : turn.tools)
      ) : running ? (
        <div className={chat.wait}>
          {bars}
          Working on its own
        </div>
      ) : (
        ended && <p className={`${styles.ownNote} ${styles.activityFailed}`}>{ended}</p>
      )}
      {beforeReply}
      {/* Also with no reply text at all: a turn the shared budget left nothing for (#1643). */}
      <NotLoaded omitted={turn.reply_omitted} />
      {order ? (
        <FoldedReply
          parts={order.parts}
          partialStart={!!turn.first_message_partial}
          renderPart={(part, end) => (
            <>
              {reply(part.text, end)}
              {part.tools.length > 0 && activity(part.tools)}
            </>
          )}
          signals={(groups) => (
            <>
              {running && groups.includes(last) && <span> · Working on its own</span>}
              {ended && groups.includes(last) && (
                <span className={styles.activityFailed}> · {ended}</span>
              )}
              {groups.flat().some((t) => t.risk?.level === "risky") && (
                <span className={styles.activityRisk}> · Risky activity</span>
              )}
            </>
          )}
        />
      ) : (
        turn.reply && reply(turn.reply, true)
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
  answerMessages,
  answeredQuestions,
  onAnswerAsMessage,
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
  notSent,
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
  answerMessages: Record<string, MessageAnswer>;
  /** Operator turns in the conversation by operation id (their text), to recognise a sent answer. */
  answeredQuestions: ReadonlyMap<string, string>;
  onAnswerAsMessage: OnAnswerAsMessage;
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
  /** A queued message proven never sent (#1538): its reason and Send again, in place of the
   *  generic failure line. */
  notSent?: ReactNode;
}) {
  const active = turn.state === "running" || turn.state === "awaiting_approval";
  const failed = ["failed", "interrupted", "uncertain", "unavailable"].includes(turn.state);
  const canSendNow =
    queuePosition === 1 && (!turn.delivery || turn.delivery === "interrupt_failed");
  const { order, first, last } = activityGroups(turn.reply, turn.tools, turn.reply_breaks);
  const activity = (tools: StructuredTool[]) => tools.length > 0 && (
    <details className={styles.activity} data-testid="structured-activity">
      <summary>
        {toolCount(tools, turn.tools_truncated && tools === first)}
        {active && tools === last && " · Working"}
        {tools.some((t) => /failed|error/i.test(t.outcome)) && <span className={styles.activityFailed}> · Failed activity</span>}
        {tools.some((t) => t.risk?.level === "risky") && <span className={styles.activityRisk}> · Risky activity</span>}
      </summary>
      <ToolList tools={tools} label="What the agent did" />
    </details>
  );
  const replyText = (text: string, end: boolean) => (
    <div className={`${chat.txt} ${styles.replyText}`}>
      <Markdown text={end && turn.reply_truncated ? `${text}…` : text} />
    </div>
  );
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
      {activity(order ? order.lead : turn.tools)}
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
          sent={answerMessages[requestKey(r)] ?? null}
          answered={questionAnswerIds(r).map((id) => answeredQuestions.get(id)).find((t) => t !== undefined) ?? null}
          onAnswerAsMessage={onAnswerAsMessage}
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
      {/* A reply the shared snapshot budget left no text for (#1643) still says it was not
          loaded, rather than reading as no reply at all (Hermes on #1651). */}
      {(turn.reply || hasOmission(turn.reply_omitted)) && (
        <div className={`${chat.turn} ${chat.asst} ${styles.reply}`} data-testid="structured-reply">
          <MessageHeader speaker={agent} kind="assistant" ts={turn.reply_ts} />
          <NotLoaded omitted={turn.reply_omitted} />
          {order ? (
            <FoldedReply
              parts={order.parts}
              partialStart={!!turn.first_message_partial}
              renderPart={(part, end) => (
                <>
                  {replyText(part.text, end)}
                  {activity(part.tools)}
                </>
              )}
              signals={(groups) => (
                <>
                  {active && groups.includes(last) && <span> · Working</span>}
                  {groups.flat().some((t) => /failed|error/i.test(t.outcome)) && (
                    <span className={styles.activityFailed}> · Failed activity</span>
                  )}
                  {groups.flat().some((t) => t.risk?.level === "risky") && (
                    <span className={styles.activityRisk}> · Risky activity</span>
                  )}
                </>
              )}
            />
          ) : (
            turn.reply && replyText(turn.reply, true)
          )}
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
          {typeof turn.ts === "number" && turn.ts > 0 && <QueuedSince ts={turn.ts} />}
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
      ) : notSent ? (
        notSent
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
/** The SERVER-side order of one launch (#1505), for responses the client sequence fence cannot
 *  order (both sent after the last action): within a generation a launch only moves forward
 *  (starting → ready | failed), and a later generation is never replaced by an earlier one.
 *  Response ORDER on this client is the sequence fence's job (`apply`), not this one's. */
const LAUNCH_ORDER = { starting: 1, ready: 2, failed: 2 } as const;
function staleLaunch(previous: StructuredSnapshot, incoming: StructuredSnapshot): boolean {
  const before = previous.launch;
  const after = incoming.launch;
  if (!before || !after) return false;
  if (before.gen !== after.gen) {
    // Another generation (#1518): a Start again's launch replaces the one before it, and a late
    // read of the earlier one must not replace it back. Each view is stamped when it is written,
    // and only ever by its own generation, so an earlier stamp is an earlier generation.
    return before.since != null && after.since != null && after.since < before.since;
  }
  return LAUNCH_ORDER[after.state] < LAUNCH_ORDER[before.state];
}

export function StructuredPane({
  engine,
  id,
  host = NO_HOST,
  runtimeConfirmed = true,
}: {
  engine: string;
  id: string;
  /** What the session's host hands a pane: Files, To map, a window's chrome slot (#1332). */
  host?: PaneHost;
  /** False while this pane was chosen from a CACHED runtime hint the roster has not confirmed
   *  yet (#1502). The hint can be stale (the engine is a terminal engine now), so until the roster
   *  answers, a failed first read — "This conversation does not exist" above all — is provisional:
   *  it renders as "Opening…" (bounded by Retry), never as a verdict. */
  runtimeConfirmed?: boolean;
}) {
  // The API-session look (#1494 P3), on the conversation log only: `{}` for the default look.
  const logLook = useApiLogProps();
  useEngineRoster();
  const key = `${engine}:${id}`;
  // Actions act on the id the URL settled on (#867), the same split the terminal keeps.
  const actionKey = host.rowKey || key;
  const { row, phase: rowPhase } = useSessionLookup(key, host.rowKey);
  const isMobile = useIsMobile();
  // The in-pane head folds on touch tablets too (#1589); the window chrome keeps `isMobile`.
  const compactHead = useCompactHead();
  // The agent is the console agent this client drives (`api.source`), named by its own label.
  const source = engineInfo(engine)?.api?.source;
  const agent = source ? engineLabel(source) : engineLabel(engine);
  const [snap, setSnap] = useState<StructuredSnapshot | null>(null);
  // A skip start's own outcome (#1505): handed over by New session, or this pane's Start. A
  // refusal shows its reason; a lost answer says the start may not have reached BattleLab.
  // Keyed, so an outcome never shows on another session's pane.
  const [attempt, setStartOutcome] = useState<
    | null
    | { key: string; phase: "sending" }
    | { key: string; phase: "refused"; reason: string }
    | { key: string; phase: "unknown" }
  >(() => (peekStart(key) ? { key, phase: "sending" } : null));
  // The latest rendered snapshot, for a POST started from an effect or a click.
  const snapRef = useRef<StructuredSnapshot | null>(null);
  snapRef.current = snap;
  const [loadError, setLoadError] = useState<string | null>(null);
  const [reconnecting, setReconnecting] = useState(false);
  // Consecutive failed reads: the uplink veil's try count, as the socket's backoff counter is the
  // terminal's (#1570 follow-up — one veil, every session).
  const [failures, setFailures] = useState(0);
  // A read failed since the last APPLIED snapshot (#1591, Hermes on #1592). Only a fresh snapshot
  // clears it — a healthy `/events` cursor alone does not — so the outbox never posts against a
  // conversation it has not re-read since the outage.
  const [needFresh, setNeedFresh] = useState(false);
  // The poll loop's view of it: after an outage the next good cursor read re-reads the snapshot.
  const needFreshRef = useRef(false);
  useEffect(() => {
    needFreshRef.current = needFresh;
  }, [needFresh]);
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
  /** The history entry of a restored unconfirmed message (with `op`), so its re-send keeps ONE entry. */
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
  const [answerMessages, setAnswerMessages] = useState<Record<string, MessageAnswer>>({});
  // A question's message answer is ONE operation, under the first of its deterministic ids the
  // server hasn't proven dead (refused and never in the conversation, e.g. the queued answer was
  // removed). Every check (answered, Remove, sending) knows all of them, so a reload or another
  // tab still recognises an answer sent under a fallback id (independent review of #1490).
  const [answerAttempts, setAnswerAttempts] = useState<Record<string, number>>({});
  const answerIdFor = (req: StructuredRequest) =>
    questionAnswerIds(req)[Math.min(answerAttempts[requestKey(req)] ?? 0, questionAnswerIds(req).length - 1)];
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
  // Send now refused because only a Stop could cut the turn (#1644): the operator chooses Wait or
  // Stop all and send. `previous`: the tasks a confirmation named that no longer match.
  const [stopChoice, setStopChoice] = useState<{
    key: string;
    queuedTurnId: string;
    previous: StructuredBackgroundTask[] | null;
  } | null>(null);
  const [stoppingAll, setStoppingAll] = useState(false);
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
  /** The composer's next request identity: set only by Restore of this session's unconfirmed
   *  entry (#1346), so an unchanged re-send replays it. Consumed at Send; never the identity of a
   *  queued message — each outbox entry carries its own (#1465). */
  const op = useRef<{ id: string; text: string } | null>(null);
  const cursor = useRef(0);
  const logRef = useRef<HTMLDivElement>(null);
  // The outbox (#1465): sent messages the conversation does not show yet, FIFO, only the head in
  // flight. A late POST result for an unmounted pane (a session switch) touches nothing.
  const { outbox, outboxRef, setOutbox } = useOutbox();
  const alive = useAlive();

  // ONE ordering fence for everything this pane shows (#1505, Hermes on #1518). Every snapshot
  // read and every state-changing request takes a sequence number WHEN IT IS SENT. A response is
  // applied only if it was sent after the last one applied, and a completed action raises the
  // floor past everything sent before it finished — so no read sent before an action (or while
  // it ran) can overwrite that action's result; the read that follows the action shows it. The
  // journal revision and `staleLaunch` stay as secondary guards for the server's own order.
  const sentSeq = useRef(0);
  const floorSeq = useRef(-1);
  const apply = useCallback((s: StructuredSnapshot, sent: number): boolean => {
    if (sent <= floorSeq.current) return false;
    // Reads race (the poll, and a re-read after every action): an older answer arriving last
    // must never roll the view back. The journal revision only grows.
    if (s.revision < cursor.current) return false;
    floorSeq.current = sent;
    cursor.current = s.event_cursor;
    // A recorded turn replaces its pending entry, whatever that entry's POST says or will say.
    setOutbox((list) => reconcile(list, operatorTurns(s).map((t) => t.turn_id), s.revision));
    if (s.bypass) setPermissionsOpen(null);
    setSnap((previous) => {
      // Lifecycle changes need not advance the journal. Reject the entire older snapshot:
      // preserving just its mode would still resurrect closed approvals and the old worker.
      const prior = previous?.permission_change?.state;
      const incoming = s.permission_change?.state;
      return previous && ((prior === "enabled" && incoming !== "enabled") ||
        (prior === "stopping" && !incoming) ||
        staleLaunch(previous, s))
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
    setFailures(0);
    setNeedFresh(false);
    return true;
  }, [setOutbox]);
  /** A state-changing request: sequenced when sent; when it settles (either way) nothing sent
   *  before that moment may be applied. Its own snapshot, if it answers with one, is applied at
   *  its send sequence — so a newer read already shown is never replaced by it. */
  const act = useCallback(
    async <T,>(request: Promise<T>, own?: (value: T) => StructuredSnapshot): Promise<T> => {
      const sent = ++sentSeq.current;
      try {
        const value = await request;
        if (own) apply(own(value), sent);
        return value;
      } finally {
        floorSeq.current = Math.max(floorSeq.current, sentSeq.current);
      }
    },
    [apply],
  );
  const failed = useCallback((e: unknown) => {
    if (e instanceof ApiError && e.status === 404)
      setLoadError("This conversation does not exist.");
    else {
      setReconnecting(true);
      setFailures((n) => n + 1);
      setNeedFresh(true);
    }
  }, []);
  const load = useCallback(async (): Promise<StructuredSnapshot | null> => {
    const sent = ++sentSeq.current;
    try {
      const s = await api.structuredSnapshot(key);
      apply(s, sent);
      return s;
    } catch (e) {
      failed(e);
      return null;
    }
  }, [key, apply, failed]);
  // Older turns, loaded as the operator scrolls up past the snapshot's window (#1504).
  const earlier = useApiHistory(key, snap, logRef, load);

  useEffect(() => {
    let alive = true;
    const sent = ++sentSeq.current;
    api
      .structuredSnapshot(key)
      .then((s) => alive && apply(s, sent))
      .catch((e: unknown) => alive && failed(e));
    return () => {
      alive = false;
    };
  }, [key, apply, failed]);

  // The skip start New session fired without waiting (#1505): its outcome, read once.
  useEffect(() => {
    const handed = peekStart(key);
    if (!handed) return;
    let alive = true;
    // Sent before this pane's first read: the oldest request here. Its answer applies only if
    // nothing has been shown since, and its completion — like any action's — retires every read
    // sent before it settled.
    void handed.then((outcome: StartOutcome) => {
      if (!alive) return;
      consumeStart(key, handed);
      const shown = outcome.kind === "ok" && apply(outcome.snap, 0);
      floorSeq.current = Math.max(floorSeq.current, sentSeq.current);
      if (outcome.kind === "ok") {
        setStartOutcome(null);
        if (!shown) void load(); // a newer read was shown first: read again, after the start
      } else if (outcome.kind === "refused") {
        setStartOutcome({ key, phase: "refused", reason: outcome.reason });
      } else {
        setStartOutcome({ key, phase: "unknown" });
      }
    });
    return () => {
      alive = false;
    };
  }, [key, apply, load]);

  // The launch view (#1505): the page opens on launch, and the snapshot says when it is ready.
  const launch = snap?.launch;
  const launchStarting = launch?.state === "starting" || !!snap?.start_in_flight;
  // Once the snapshot shows a launch, its truth wins over a refused or lost start answer.
  const startAttempt =
    attempt?.key !== key ||
    (attempt.phase !== "sending" && (launchStarting || launch?.state === "ready"))
      ? null
      : attempt;
  // A guarded create whose worker never became ready: its own stop closed it, and `/start` does
  // not relaunch a guarded creation, so it offers New session — never Start again.
  const createFailed =
    launch?.state === "failed" && !snap?.pending_start && !snap?.start_expired;
  const startFailure =
    startAttempt?.phase === "refused"
      ? startAttempt.reason
      : launch?.state === "failed"
        ? (launch.reason ?? "the agent did not become ready")
        : snap?.start_failed
          ? (snap.start_failed_reason ?? "the agent did not become ready")
          : null;
  // The cursor loop: cheap reads of the journal's revision; a change re-reads the snapshot.
  const active = isActive(snap);
  // Task progress reads live without changing the turn-active predicate used by Send (#1461).
  // Starting polls too (#1505): ready or failed repaints within one tick. A message on its way
  // (or of unknown outcome) is settled by the conversation: read it promptly too (#1465).
  const startSending = startAttempt?.phase === "sending";
  const pollingActive = active || backgroundWorking(snap) || launchStarting || startSending ||
    outbox.some((e) => e.phase === "sending" || e.phase === "unknown");
  // Starting, or a start request still in flight: the snapshot itself is read every tick.
  const readSnapshotEachTick = launchStarting || startSending;
  // #1647: a start on a loaded host can take tens of seconds, and its Starting state is already
  // the loading state. A read gap inside it reaches the veil only after START_VEIL_HOLD_MS plus
  // the veil's own LOST_GRACE_MS (15 s together), so a short gap never flashes LINK LOST and then
  // RESTORED over a session that is simply starting. A longer one still shows: delayed, never
  // hidden. Only this pane's status is held; the veil's rules and the terminals' are unchanged.
  const [startVeilDue, setStartVeilDue] = useState(false);
  const startGap = readSnapshotEachTick && (reconnecting || needFresh);
  useEffect(() => {
    setStartVeilDue(false);
    if (!startGap) return;
    const timer = setTimeout(() => setStartVeilDue(true), START_VEIL_HOLD_MS);
    return () => clearTimeout(timer);
  }, [startGap]);
  const startQuiet = readSnapshotEachTick && !!snap && !startVeilDue;
  useEffect(() => {
    let alive = true;
    let timer: ReturnType<typeof setTimeout>;
    let backoff = 0;
    let ticks = 0;
    const tick = async () => {
      ticks += 1;
      try {
        if (readSnapshotEachTick) {
          // Starting (#1505): readiness changes the lifecycle, not the journal, so the cursor
          // need not move — the snapshot itself is read every tick until the launch settles. So
          // is the pending → starting handoff while a start request is in flight.
          await load();
          if (!alive) return;
        } else {
          const page = await api.structuredEvents(key, cursor.current, 1);
          if (!alive) return;
          backoff = 0;
          setReconnecting(false);
          setFailures(0);
          // A changed journal re-reads at once. Lifecycle facts that write no journal record — a
          // worker's idle exit, the client turning read only — are picked up by a full re-read
          // every idle tick and every 5th active tick (Hermes on #1315).
          if (
            page.revision !== cursor.current ||
            !pollingActive ||
            ticks % 5 === 0 ||
            needFreshRef.current
          )
            await load();
        }
      } catch (e) {
        if (!alive) return;
        if (e instanceof ApiError && e.status === 409) await load();
        else {
          setReconnecting(true);
          setFailures((n) => n + 1);
          setNeedFresh(true);
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
  }, [key, pollingActive, readSnapshotEachTick, load]);

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
  // Nor does a discarded one (#1647): the server refuses every send to it, so the box is off.
  const notStarted = !!snap?.pending_start || !!snap?.start_expired || !!snap?.start_discarded;
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

  /** Send (#1465): the composer's message becomes ONE outbox entry, frozen with its own operation
   *  id, and the composer is free at once. The pump below posts it when it is the head. */
  const send = async () => {
    if (!dict.settled()) {
      sendAfterDictation.current = true;
      if (!dict.finalizingNow()) dict.release(); // end capture; the tail still lands
      if (!dict.settled()) return;
      sendAfterDictation.current = false;
    }
    const text = draft.trim();
    const names = attachments.map((a) => a.stored);
    if ((!text && !names.length) || busy || readOnly || permissionPending || notStarted || !snap) return;
    // Starting (#1505): Send waits for ready; the draft stays in the box, nothing is queued.
    if (launchStarting || createFailed) return;
    if (slots.current.inFlight > 0) return; // a picture is still on its way
    const attempt = operationFor(op.current, sendIdentity(text, names), uuid);
    if (outboxRef.current.some((e) => e.request.id === attempt.id)) {
      // A restored unconfirmed message that is already on its way: one operation, sent once.
      setSendError("This message is already on its way.");
      return;
    }
    // Recorded at submit time, untrimmed, so Restore round-trips exactly what was typed. A
    // restored unconfirmed entry re-sends under its own id and keeps its one history entry.
    const history = historyFor.current?.op === attempt.id
      ? historyFor.current.id
      : appendSent({ text: draft, attachments: names, session: key, operation: attempt.id });
    setHistory(readSent());
    op.current = null;
    historyFor.current = null;
    setOutbox((list) => [
      ...list,
      outboxEntry({ id: attempt.id, text, draft, attachments, history }),
    ]);
    setDraft("");
    clearAttachments();
    setSendError(null);
  };

  const confirmHistory = (history: string | null) => {
    // The server recorded the turn — never a claim that the agent has acted on it.
    if (!history) return;
    confirmSent(history);
    setHistory(readSent());
  };

  /** POST one entry: exactly its frozen request. The outcome only ever updates THAT entry, and
   *  only while it is still in the outbox (a snapshot may have settled it first). */
  const post = async (entry: OutboxEntry) => {
    const s = snapRef.current;
    if (!s) return;
    const { id, text, attachments: files, history } = entry.request;
    setOutbox((list) => updateEntry(list, id, (e) => ({ ...e, phase: "sending" })));
    try {
      // Sequenced (#1505): no snapshot read sent before this settles can overwrite what follows it.
      const answer = await act(
        api.structuredSubmit(key, id, text, revisionFor(s), files.map((a) => a.stored)),
      );
      confirmHistory(history);
      if (!alive.current) return;
      // The answer's revision is the durable receipt: a later snapshot at or past it settles the
      // entry even if its (completed) turn has aged out of the bounded window meanwhile.
      const receipt = typeof answer?.revision === "number" ? answer.revision : undefined;
      setOutbox((list) => updateEntry(list, id, (e) => ({ ...e, recorded: true, receipt })));
      setContainment(null);
      await load(); // its turn replaces the entry; then the next head goes
    } catch (e) {
      if (!alive.current) return;
      const latest = await load();
      if (!alive.current) return;
      if (operatorTurns(latest).some((t) => t.turn_id === id)) {
        confirmHistory(history); // the conversation has it: `apply` already dropped the entry
        return;
      }
      setOutbox((list) =>
        updateEntry(list, id, (x) =>
          definite(e)
            ? {
                ...x,
                phase: "refused",
                reason: detail(e, e instanceof ApiError && e.status === 409 ? "the conversation changed" : "refused"),
              }
            : { ...x, phase: "unknown" },
        ),
      );
    }
  };

  // The pump: the head goes when it is waiting and nothing holds it; a refused or unknown head
  // stops the queue. A native worker queues turns itself, so a busy agent does not hold the head
  // (the HTTP chat client, which takes one reply at a time, is ChatPane's: it waits for idle).
  const blocked = readOnly || permissionPending || notStarted;
  // While reads are failing the head waits instead of being posted into a dead link (where it would
  // land as "unknown" and stop the queue): the outbox holds it and the pump resumes on the first good
  // read. What the uplink veil promises an API session (#1570 follow-up).
  const holdQueue = blocked || reconnecting || needFresh;
  useEffect(() => {
    const head = nextToPost(outboxRef.current, !!snap && !holdQueue);
    if (head) void post(head);
    // `post` reads the latest snapshot itself; the outbox, the snapshot and the guards are the triggers.
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [outbox, snap, holdQueue]);

  // Anything the server does not have yet is lost on a reload or close: ask first.
  useLeaveGuard(unrecorded(outbox));

  const retryEntry = (id: string) => setOutbox((list) => retried(list, id));
  const dropEntry = (id: string) => {
    setOutbox((list) => withoutEntry(list, id));
    requestAnimationFrame(() => taRef.current?.focus());
  };
  // Edit replaces nothing the operator is writing: it needs an empty, settled composer.
  const composerHolds = !!draft.trim() || attachments.length > 0 || uploading || dictating || busy;
  /** Edit (refused only — the server took nothing under its id): its words and pictures go back
   *  into the composer and it leaves the queue, releasing the messages behind it. Sent again, it
   *  is a NEW message (new id) at the end of the queue. */
  const editEntry = (id: string) => {
    const entry = outboxRef.current.find((e) => e.request.id === id);
    if (!entry || entry.phase !== "refused" || composerHolds || slots.current.inFlight > 0) return;
    setOutbox((list) => list.filter((e) => e.request.id !== id));
    clearAttachments();
    slots.current.held = entry.request.attachments.length;
    setAttachments(entry.request.attachments.map((a) => ({ name: a.name, stored: a.stored })));
    op.current = null; // a new request
    historyFor.current = null;
    setDraft(entry.request.draft);
    setSendError(null);
    requestAnimationFrame(() => taRef.current?.focus());
  };

  /** Send again (#1538): a queued message proven never sent goes out again under the ONE
   *  operation id the server derived for it (`not_sent.retry`), so a second tab, a reload or a
   *  repeated click resends the same operation and the server delivers it at most once. It goes
   *  through the outbox, so it shows pending at once behind anything already waiting; whether it
   *  was sent again is read from the server (`not_sent.retried`), never remembered here. */
  const [, setDiscardTick] = useState(0);
  const discardNotSent_ = (turnId: string) => {
    discardNotSent(key, turnId);
    setDiscardTick((n) => n + 1);
    requestAnimationFrame(() => taRef.current?.focus());
  };
  const sendAgain = (turn: StructuredTurn, files: NotSentAttachment[]) => {
    const id = turn.not_sent?.retry;
    if (readOnly || !id || turn.not_sent?.retried || turn.not_sent?.retry_recorded) return;
    const text = turn.text ?? "";
    if (!text.trim() && files.length === 0) return;
    const held = outboxRef.current.find((e) => e.request.id === id);
    if (held && held.phase !== "refused") return; // already on its way
    setOutbox((list) => [
      // A refused retry recorded nothing: the same id goes again, at the end of the queue.
      ...list.filter((e) => e.request.id !== id),
      outboxEntry({
        id,
        text,
        draft: text,
        attachments: files.map((a) => ({ name: a.name ?? attachmentName(a.stored), stored: a.stored })),
        history: null,
      }),
    ]);
    requestAnimationFrame(() => taRef.current?.focus());
  };

  const sendAnswer = async (attempt: { question: string; id: string; text: string }) => {
    if (busy || readOnly || permissionPending || notStarted || !snap || sending.current) return;
    if (!attempt.text.trim()) return;
    sending.current = true;
    setBusy(true);
    setAnswer({ session: key, ...attempt, phase: "sending" });
    try {
      // No revision expectation: Codex keeps working, and the question binding is the check.
      await act(api.structuredSubmit(key, attempt.id, attempt.text, undefined, [], attempt.question));
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
      await act(api.structuredDecide(key, {
        decision_id,
        turn_id: req.turn_id,
        request_id: req.request_id,
        decision: choice,
        ...(grant ? { grant } : {}),
        ...(answers ? { answers } : {}),
      }));
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

  /** `message` mode (#1463): the answers go as the operator's next message, then the pending
   *  question is stopped so the agent reads them. Message FIRST: while the question holds the
   *  turn it waits in the queue, so a failed send leaves the question pending and answerable; a
   *  failed stop leaves the message visibly queued and Stop the turn on the card. */
  const answerAsMessage = async (req: StructuredRequest, text: string, retry?: MessageAnswer) => {
    if (readOnly || permissionPending || !text.trim()) return;
    const reqKey = requestKey(req);
    const current = answerMessages[reqKey];
    if (current && current.phase === "sending") return;
    // ONE operation per question, whatever this tab remembers (Hermes #6495): a retry, a resend
    // after a reload or from another tab is the same id, so the server keeps a single message.
    const id = answerIdFor(req);
    const attempt = { id, text: retry?.id === id ? retry.text : text };
    const set = (next: MessageAnswer | null) =>
      setAnswerMessages((all) => {
        const rest = Object.fromEntries(Object.entries(all).filter(([k]) => k !== reqKey));
        return next ? { ...rest, [reqKey]: next } : rest;
      });
    set({ ...attempt, phase: "sending" });
    try {
      await act(api.structuredSubmit(key, attempt.id, attempt.text, undefined));
    } catch (e) {
      const s = await load();
      // Already in the conversation — this send, or an earlier one with other picks (the server
      // refuses a changed text under the same id): either way the answer is sent; stop the question.
      const recorded = new Map(operatorTurns(s ?? null).map((t) => [t.turn_id, t.text ?? ""] as const));
      const sentText = questionAnswerIds(req).map((c) => recorded.get(c)).find((t) => t !== undefined);
      if (sentText !== undefined && sentText !== attempt.text) {
        // Another tab answered first, with other picks: the server refused ours. Say so, and leave
        // the stop to the operator — never claim this selection was sent (Hermes #6498).
        set({ ...attempt, phase: "refused", message: "another answer was already sent" });
        return;
      }
      if (sentText === undefined) {
        if (definite(e)) {
          // Refused, and the conversation (read successfully) holds none of its ids: this one never
          // reached the agent and may be dead, so the next Send moves to the next derived id.
          if (s) setAnswerAttempts((all) => ({ ...all, [reqKey]: (all[reqKey] ?? 0) + 1 }));
          set({ ...attempt, phase: "refused", message: detail(e, "refused") });
        } else {
          set({ ...attempt, phase: "unknown" });
        }
        return;
      }
    }
    set(null);
    await decide(req, "cancel");
  };

  const interrupt = async (turnId: string) => {
    if (interrupting) return; // Esc held or pressed twice: one request
    setInterrupting(true);
    setActionError(null);
    try {
      await act(api.structuredInterrupt(key, uuid(), turnId));
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
      await act(api.structuredSendNow(key, operation.id, operation.turnId, queuedTurnId));
      setSendNowUnknown(null);
    } catch (e) {
      // Refusing a recovery request says nothing about the original unknown handoff.
      if (definite(e) && !recoveringSendNow) {
        sendNowIds.current.delete(target);
        setSendNowUnknown(null);
        if (!auto && refusalReason(e) === "would_stop_background") {
          // Only a Stop could cut it now (#1644): a choice, never a dead end. Always send now
          // never offers it; its hint above the composer says why the message waits.
          setStopChoice({ key, queuedTurnId, previous: null });
        } else if (!auto || (autoTries.current.get(target)?.tries ?? 0) >= AUTO_SEND_TRIES) {
          // An automatic attempt retries on its own; only its last refusal is worth showing.
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

  /** Stop all and send (#1644): the operator confirmed ending exactly the tasks shown. The
   *  worker checks the generation, the turn, the queued head and the task ids again, after its
   *  claim too; any change answers `inventory_changed` and the choice is shown again. */
  const stopAllAndSend = async () => {
    const choice = stopChoice?.key === key ? stopChoice : null;
    if (!choice || !snap || sendNowBusy.current || readOnly) return;
    const sendTarget = sendNowTarget(snap);
    const worker = snap.native?.worker;
    const tasks = snap.native?.background_tasks ?? [];
    if (!sendTarget || !worker || tasks.length === 0) return;
    sendNowBusy.current = true;
    setStoppingAll(true);
    setActionError(null);
    try {
      // A new identity each time: what it confirms is part of the request.
      await act(api.structuredSendNow(key, uuid(), sendTarget.turnId, choice.queuedTurnId, {
        worker,
        task_ids: tasks.map((t) => t.task_id),
      }));
      setStopChoice(null);
    } catch (e) {
      if (definite(e) && refusalReason(e) === "inventory_changed") {
        // The tasks, the turn, the queued head or the worker moved: ask again about what runs
        // NOW, for the message that is first in line now (Hermes on #1650).
        const fresh = await load();
        const head = fresh
          ? [...queuePositions(fresh)].find(([, position]) => position === 1)?.[0] ?? null
          : null;
        if (fresh && head && sendNowTarget(fresh)) {
          setStopChoice({ key, queuedTurnId: head, previous: tasks });
        } else {
          setStopChoice(null);
          setActionError(head
            ? "Stop all and send not needed: the agent’s turn ended, and your message is next in line."
            : "Stop all and send not applied: no message is waiting any more.");
        }
      } else {
        setStopChoice(null);
        setActionError(definite(e)
          ? `Stop all and send not applied: ${detail(e, "refused")}`
          : "Stop all and send: no answer from the agent, so it may have been applied. Check the conversation before you try again.");
      }
    } finally {
      await load();
      setStoppingAll(false);
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
      await act(api.structuredWithdraw(key, operation, queuedTurnId));
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

  // Not while the worker says Send now would stop background tasks (#1644): every attempt would
  // be refused. It goes once that changes; the composer says why it waits meanwhile.
  const autoHead = alwaysSendNow && !readOnly && !permissionPending && !recoveringSendNow && !sendingNow
    && snap?.native?.send_now_state !== "would_stop_background"
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

  // Send now refused because only a Stop could cut the turn (#1644): Wait, or Stop all and send
  // naming every task it ends. Shown while that message is still the one waiting.
  const choice = stopChoice?.key === key ? stopChoice : null;
  const choiceQueued = choice && snap
    ? operatorTurns(snap).find((t) => t.turn_id === choice.queuedTurnId)?.state === "queued"
    : false;
  const choiceTasks = snap?.native?.background_tasks ?? [];
  // Which message the choice sends: it may have been rebound to a new head after a change.
  const choiceFull = choice && snap
    ? operatorTurns(snap).find((t) => t.turn_id === choice.queuedTurnId)?.text ?? ""
    : "";
  const choiceText = choiceFull.length > 80 ? `${choiceFull.slice(0, 79)}…` : choiceFull;
  const stopChoiceView = choice && choiceQueued && !readOnly ? (() => {
    const before = new Set((choice.previous ?? []).map((t) => t.task_id));
    const now = new Set(choiceTasks.map((t) => t.task_id));
    const gone = (choice.previous ?? []).filter((t) => !now.has(t.task_id));
    const changed = choice.previous !== null;
    return (
      <section className={styles.stopChoice} role="dialog" aria-labelledby="structured-stop-choice-title"
        data-testid="structured-stop-choice">
        <h4 id="structured-stop-choice-title" className={styles.stopChoiceTitle}>
          {changed ? "Background tasks changed" : "Send now would stop background tasks"}
        </h4>
        {choiceText && <p className={styles.stopChoiceFor}>For “{choiceText}”</p>}
        {choiceTasks.length === 0 ? (
          <p>The background tasks have ended: Send now no longer stops anything.</p>
        ) : (
          <p>{changed
            ? "Since you asked, the agent’s background tasks changed. Confirm against the current list:"
            : "Cutting this turn now would also end:"}</p>
        )}
        {(choiceTasks.length > 0 || gone.length > 0) && (
          <ul className={styles.stopChoiceList} data-testid="structured-stop-choice-tasks">
            {choiceTasks.map((t) => (
              <li key={t.task_id} className={changed && !before.has(t.task_id) ? styles.stopChoiceAdded : undefined}>
                {changed && !before.has(t.task_id) ? "+ " : ""}{taskLabel(t)}
              </li>
            ))}
            {gone.map((t) => (
              <li key={`gone:${t.task_id}`} className={styles.stopChoiceGone}>{taskLabel(t)} (ended)</li>
            ))}
          </ul>
        )}
        <div className={styles.stopChoiceActions}>
          <button type="button" className={styles.sendNow} disabled={stoppingAll}
            onClick={() => setStopChoice(null)}>
            Wait — send after this turn
          </button>
          {choiceTasks.length === 0 ? (
            <button type="button" className={styles.sendNow} disabled={stoppingAll || !!sendingNow}
              onClick={() => { setStopChoice(null); void sendNow(choice.queuedTurnId); }}>
              Send now
            </button>
          ) : (
            <button type="button" className={styles.stopAll} disabled={stoppingAll}
              onClick={() => void stopAllAndSend()}>
              {stoppingAll
                ? "Stopping…"
                : changed ? `Stop these ${choiceTasks.length} and send` : "Stop all and send"}
            </button>
          )}
        </div>
      </section>
    );
  })() : null;
  // What Send now would do right now (#1644), BEFORE sending: only where the operator expects it
  // to cut in (Always send now, or a message waiting for it), and only for a Stop-style Send now.
  const sendNowState = snap?.native?.send_now_state;
  const stopStyle = sendNowTarget(snap)?.mode === "interrupt";
  const waitingHead = !!snap && operatorTurns(snap).some((t) => t.state === "queued");
  const sendNowHint = !readOnly && isActive(snap) && stopStyle && (alwaysSendNow || waitingHead)
    && (sendNowState === "would_stop_background" || sendNowState === "unknown") && !stopChoiceView ? (
      <p role="status" data-testid="structured-sendnow-hint" data-state={sendNowState}
        className={`${styles.sendNowHint} ${sendNowState === "unknown" ? styles.sendNowHintUnknown : styles.sendNowHintBlocked}`}>
        {sendNowState === "unknown"
          ? "Send now status unknown (the agent hasn’t reported it yet). Send queues until it is known."
          : `Send now unavailable: the agent has ${choiceTasks.length === 1 ? "a background task" : choiceTasks.length > 1 ? `${choiceTasks.length} background tasks` : "background work"} running. Send queues behind its current turn.`}
      </p>
    ) : null;

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
    setStartOutcome({ key, phase: "sending" });
    try {
      await act(api.structuredStart(key), (s) => s);
      setStartOutcome(null);
    } catch (e) {
      // A definite refusal shows its reason; no answer may still have reached BattleLab (#1505).
      setStartOutcome(
        e instanceof ApiError && e.status >= 400 && e.status < 500
          ? { key, phase: "refused", reason: detail(e, "refused") }
          : { key, phase: "unknown" },
      );
    } finally {
      setStarting(false);
      await load();
    }
  };
  const [checking, setChecking] = useState(false);
  const checkStart = async () => {
    setChecking(true);
    const s = await load();
    setChecking(false);
    if (s && (s.launch || s.start_in_flight || !s.pending_start)) setStartOutcome(null);
  };
  const navigate = useNavigate();
  const memory = snap?.native?.memory_pressure ?? null;
  // Back to New session with this session's agent, project and model preselected (#1505) —
  // and GUARDED, as this session was: the form's stored skip default must not turn it into one.
  // The folder comes with the project: the form takes no folder of its own (#1458). So the project is
  // never GUESSED (Hermes on #1646): a session with no project hands over an explicit "" (No
  // project; the form then names the folder it would use, or asks for a project), never the
  // `null` that means "untouched" and lets the starred project — and ITS folder — be preselected.
  // While this session's row is still being looked up there is nothing to hand over yet, so the
  // button waits for it (`rowPending`); a lookup that settled with no row is a session with no
  // project we can name, so it hands over "" too.
  const rowPending = !row && rowPhase === "pending";
  const newSessionLikeThis = () =>
    navigate(SESSIONS_PATH, {
      state: {
        apiGuardedEngine: engine,
        restoreDraft: {
          engineChoice: engine,
          bypassChoice: null,
          returnTo: null,
          projectChoice: row?.project?.kind === "project" ? row.project.id : "",
          ...(snap?.model_requested
            ? { modelChoice: { engine, model: snap.model_requested } }
            : {}),
        },
      },
    });

  const stop = async () => {
    setStopping(true);
    setActionError(null);
    try {
      setContainment((await act(api.structuredStop(key))).containment);
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
      await act(api.structuredEnableBypass(key, id), (s) => s);
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

  // The restored unconfirmed message this composer holds (#1346) has landed: retire its id so
  // Send can never replay it (Hermes on #1346). If the draft is still exactly that message, it
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
  const workerChip = reconnecting && !startQuiet
    ? { cls: styles.ledWarn, text: "reconnecting" }
    : launchStarting
      ? { cls: styles.ledIdle, text: "starting" }
      : startFailure && (createFailed || snap?.pending_start)
        ? { cls: styles.ledDown, text: "start failed" }
    : containment === "gone"
      ? { cls: styles.ledIdle, text: "stopped" }
      : containment === "unknown"
        ? { cls: styles.ledWarn, text: "stop unconfirmed" }
        : live
          ? { cls: styles.ledUp, text: "worker live" }
          : { cls: styles.ledIdle, text: "no worker" };
  // What this session runs (#1511): an "agent default" session names the model and level its
  // client reported, not the literal "agent default" — the same resolution the recap uses.
  const running = resolveApiModel(snap ?? {});

  // A map window's chrome LED reads this pane's connection state (#1109), as it reads the
  // terminal's socket status: the journal read is this pane's connection.
  // #1502: nothing has been read yet and the runtime is only a cached hint — not an answer.
  const provisional = !runtimeConfirmed && !snap && (loadError !== null || reconnecting);
  const status: TermStatus = provisional
    ? { kind: "connecting" }
    : loadError
    ? { kind: "rejected", reason: loadError }
    : // Still down until a fresh snapshot is APPLIED, not merely once the cursor answers (Hermes on
      // #1592): the veil and the outbox hold on the same condition, so RESTORED is never announced
      // over a conversation that cannot be read yet.
      (reconnecting || needFresh) && !startQuiet
      ? { kind: "reconnecting", attempt: Math.max(1, failures) }
      : snap
        ? { kind: "connected" }
        : { kind: "connecting" };
  // #1570 follow-up: the same uplink veil as a terminal, from the same status, with the same rules.
  const veil = useUplinkVeil(status);
  const veilUp = !!veil && veil.phase !== "restored";
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
  // A session that takes no message (unstarted, expired, discarded) takes nothing into its box
  // either: every writer of the draft is off with the textarea (#1647).
  const toolsBusy = busy || uploading || dictating || notStarted;
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
                  disabled: busy || notStarted || attachments.length + inFlight >= MAX_ATTACHMENTS,
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
        collapsed={compactHead}
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

  if (provisional) return <OpeningPanel reload={reloadRoster} />;

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

  const turns = earlier.turns;
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
  // Where loaded history meets the window (a steering addition grouped under an older turn
  // carries no seam of its own).
  const seam = earlier.seam;
  if (backgroundAt >= 0) conversation.splice(backgroundAt, 0, null);
  const pending = snap?.pending_requests ?? [];
  const operatorIds = new Map(operatorTurns(snap).map((t) => [t.turn_id, t.text ?? ""] as const));
  const pendingAnswerIds = new Set(
    pending.filter((r) => questionAnswerMode(r) === "message").flatMap((r) => questionAnswerIds(r)),
  );
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
      answerMessages={answerMessages}
      answeredQuestions={operatorIds}
      onAnswerAsMessage={(r, text, retry) => void answerAsMessage(r, text, retry)}
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
      // A question's queued answer is settled by Stop the question, not removed: removing it
      // would leave the question pending with its answer gone.
      canRemove={!!snap?.native?.withdraw && !pendingAnswerIds.has(t.turn_id)}
      question={questionFor(t)}
      notSent={t.not_sent && (
        <NotSentRow
          turn={t}
          choice={notSentState(
            t,
            outbox.find((e) => e.request.id === t.not_sent?.retry)?.phase ?? null,
            notSentDiscarded(key, t.turn_id),
          )}
          readOnly={readOnly}
          probe={(stored) => api.uploadBlob(stored)}
          onSendAgain={(files) => sendAgain(t, files)}
          onDiscard={() => discardNotSent_(t.turn_id)}
        />
      )}
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
        <div
          className={`${styles.banner} ${startFailure && startAttempt?.phase !== "unknown" ? styles.failBanner : ""}`}
          role="status"
          data-testid="structured-pending-start"
        >
          {startAttempt?.phase === "unknown" ? (
            <p data-testid="structured-start-unknown">
              Start may not have reached BattleLab. Check again, or start it again — a repeat
              start only observes one that already went through.
            </p>
          ) : startFailure && startAttempt?.phase !== "sending" ? (
            <p data-testid="structured-start-failed">
              <b>Couldn’t start:</b> {startFailure}
            </p>
          ) : (
            <p>
              {snap.start_incomplete
                ? "This session’s start didn’t finish. Nothing has run; start it again or discard it."
                : "This session skips permission prompts and hasn’t started. Nothing has run."}
            </p>
          )}
          {startAttempt?.phase === "unknown" && (
            <button
              type="button"
              className={`${chat.ghost} ${styles.inline}`}
              disabled={checking || starting || stopping}
              onClick={() => void checkStart()}
            >
              Check again
            </button>
          )}
          <button
            type="button"
            className={chat.send}
            disabled={starting || stopping || startAttempt?.phase === "sending"}
            onClick={() => void startPending()}
          >
            {startFailure || startAttempt?.phase === "unknown"
              ? "Start again"
              : "Start (skips prompts)"}
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
      {createFailed && (
        <div
          className={`${styles.banner} ${styles.failBanner}`}
          role="status"
          data-testid="structured-create-failed"
        >
          <p>
            <b>Couldn’t start:</b> {startFailure}
          </p>
          <button
            type="button"
            className={chat.send}
            onClick={newSessionLikeThis}
            disabled={rowPending}
            title={rowPending ? "Reading this session’s project…" : undefined}
          >
            New session
          </button>
          <button
            type="button"
            className={`${chat.ghost} ${styles.inline}`}
            disabled={stopping}
            onClick={() => void stop()}
          >
            Discard
          </button>
        </div>
      )}
      {snap?.start_discarded && (
        <p className={styles.banner} role="status" data-testid="structured-start-discarded">
          This session was discarded before it started. Nothing ran.
        </p>
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
      {/* Near its memory limit (#1643): the live generation's own reading, fresh and pinned
          (the server sends nothing stale), with the way to raise the limit. */}
      {memory && (
        <div
          className={`${styles.banner} ${styles.memBanner} ${
            memory.level === "critical" ? styles.memCritical : styles.memHigh
          }`}
          role="status"
          data-testid="structured-memory"
          data-level={memory.level}
          data-scope={memory.scope ?? "session"}
        >
          <p>
            <b>{memory.level === "critical" ? "Memory critical" : "Memory high"}</b>{" "}
            {memorySentence(memory)}
          </p>
          {ownLimitHelps(memory) && (
            <button
              type="button"
              className={`${chat.ghost} ${styles.inline}`}
              onClick={() => navigate(settingsPath("resources"))}
            >
              Resource limits
            </button>
          )}
        </div>
      )}
      {readOnly && (
        <p className={styles.banner} role="status" data-testid="structured-read-only">
          Read only — {snap?.read_only}. The history stays; new messages and decisions are off until
          the client is available again.
        </p>
      )}
      {/* The conversation area: the log, and the uplink veil over it (a sibling, so the veil's
          heading is never inside the log's own live region or its busy state). */}
      <div className={styles.logArea}>
        <div
          className={chat.log}
          ref={logRef}
          aria-live="polite"
          aria-busy={veilUp ? true : undefined}
          data-testid="api-log"
          {...logLook}
          {...earlier.handlers}
        >
          {snap && turns.length === 0 && (
            <div className={chat.center} data-testid="structured-empty">
              {launchStarting && (
                <div className={styles.starting} role="status" data-testid="structured-starting">
                  <p>
                    Starting {agent} API… <span>the agent is getting ready</span>
                  </p>
                  <div className={styles.startingBar} aria-hidden="true" />
                </div>
              )}
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
                      {/* One inline run: `dd` is a flex row, which would gap every fragment. */}
                      <span>
                        {running.model ? (
                          <>
                            {running.model} · {running.effort}
                            {running.agentDefault && (
                              <span className={styles.infoMuted}> · agent default</span>
                            )}
                          </>
                        ) : (
                          "agent default"
                        )}
                      </span>
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
                    <dd data-testid="structured-worker" role={veil ? undefined : "status"}>
                      <span className={`${styles.led} ${workerChip.cls}`} aria-hidden="true" />
                      {workerChip.text}
                    </dd>
                  </div>
                </dl>
              </section>
            </div>
          )}
          <HistoryTop status={earlier.status} onRetry={earlier.retry} />
          {conversation.map((group) => {
            if (group === null) return (
              <BackgroundWork key="background" native={snap?.native} />
            );
            const { turn: t, additions } = group;
            return (
              <Fragment key={t.turn_id}>
                {t.turn_id === seam && (
                  <p className={styles.historySeam} data-history-row="" data-testid="api-history-seam">
                    {earlier.catchingUp ? "catching up on newer turns…" : "earlier turns ↑"}
                  </p>
                )}
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
          <OutboxBlock
            entries={outbox}
            agent={agent}
            headHold={
              readOnly
                ? "Can’t go out while this conversation is read only."
                : permissionPending
                  ? "Goes out once the permission change has finished."
                  : null
            }
            editBlocked={composerHolds}
            onRetry={retryEntry}
            onEdit={editEntry}
            onDrop={dropEntry}
          />
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
          {reconnecting && snap && !startQuiet && (
            <p className={chat.notice} data-testid="structured-reconnecting">
              Connection to BattleLab lost. The turn keeps running on the server; this view resumes
              from event {snap.event_cursor} when it reconnects. Nothing was cancelled or re-sent.
            </p>
          )}
        </div>
        {veil && <TermUplinkVeil veil={veil} title={row?.title} input="outbox" />}
      </div>
      {stopChoiceView}
      {sendNowHint}
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
                : createFailed
                  ? "This session didn’t start"
                  : snap?.start_discarded
                  ? "This session was discarded"
                  : notStarted
                  ? "Start this session first"
                  : active && alwaysSendNow && sendNowTarget(snap)
                      && snap?.native?.send_now_state === "would_stop_background"
                    ? `Message ${agent} — queues behind the agent’s current turn.`
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
            disabled={readOnly || notStarted || createFailed || permissionPending}
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
                <span className={styles.alwaysLabel}>Always send now</span>
              </button>
            )}
          </div>
          <span className={`${compose.spacer} ${styles.composeNote}`} role="status">
            {dictNote || (launchStarting && !readOnly ? "Type now — Send unlocks when the agent is ready." : "")}
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
              aria-disabled={dictFinalizing || busy || notStarted}
              title={
                dictListening
                  ? "Release to stop — the last phrase still lands"
                  : dictFinalizing
                    ? "Finishing transcription…"
                    : "Hold to talk"
              }
              disabled={busy || notStarted}
              {...micHandlers}
            >
              <Mic size={16} aria-hidden />
              <span className={`${compose.micLabel} ${styles.actionLabel}`}>Talk</span>
            </button>
          )}
          <button
            type="submit"
            className={`${compose.send} shine`}
            title="Send + Enter"
            disabled={
              readOnly ||
              notStarted ||
              createFailed ||
              launchStarting ||
              permissionPending ||
              busy ||
              uploading ||
              !snap ||
              (!draft.trim() && attachments.length === 0)
            }
            data-testid="structured-send"
          >
            <Send size={15} aria-hidden />
            <span className={styles.actionLabel}>
              {uploading ? "Uploading" : launchStarting ? "Starting…" : "Send"}
            </span>
          </button>
        </div>
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
