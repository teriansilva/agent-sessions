import {
  ArrowLeftRight,
  BookMarked,
  History,
  Mic,
  PanelRight,
  Paperclip,
  ScrollText,
  Send,
  Share2,
  Square,
  SquareDashedBottom,
} from "lucide-react";
import { useCallback, useEffect, useLayoutEffect, useRef, useState } from "react";
import type { ClipboardEvent as ReactClipboardEvent, KeyboardEvent } from "react";
import { createPortal } from "react-dom";
import { engineInfo, engineLabel, isAgent, useEngineRoster } from "../../app/engineRoster";
import { isNewSessionPlaceholder } from "../../app/sessionsStore";
import { useSessionRow } from "../../app/useSessionRow";
import { ApiError, api } from "../../lib/api";
import { imageFilesFromAsyncClipboard, imageFilesFromData } from "../../lib/clipboardImages";
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
  StructuredRisk,
  StructuredSnapshot,
  StructuredTurn,
} from "../../types/api";
import chat from "../chat/ChatPane.module.css";
import { Markdown } from "./Markdown";
import styles from "./StructuredPane.module.css";
import {
  choiceLabel,
  isActive,
  operationFor,
  requestIds,
  sendIdentity,
  requestRows,
  requestTitle,
  reviewOnly,
  turnStateLabel,
} from "./structuredView";

/** While a turn runs, the event cursor is read this often; a change re-reads the snapshot. */
export const STRUCTURED_ACTIVE_POLL_MS = 1_000;
/** Idle: background activity (or another tab) can still change the conversation. */
export const STRUCTURED_IDLE_POLL_MS = 10_000;
/** Reconnect back-off ceiling after failed reads. */
const MAX_BACKOFF_MS = 15_000;
/** Pictures per message — the server's cap (`native_images.MAX_IMAGES`), checked again there. */
const MAX_IMAGES = 4;

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
  decision_id: string;
  /** `sending` — awaiting the server; `sent` — accepted, awaiting the agent (the next snapshot
   *  settles it); `unknown` — no answer came back, so it may or may not have landed. */
  phase: "sending" | "sent" | "unknown";
}

function requestKey(req: StructuredRequest): string {
  return `${req.turn_id}\u0000${req.request_id}\u0000${req.payload_digest ?? ""}`;
}

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

function RequestCard({
  req,
  agent,
  deciding,
  readOnly,
  onDecide,
}: {
  req: StructuredRequest;
  agent: string;
  deciding: Deciding | null;
  readOnly: boolean;
  onDecide: (req: StructuredRequest, choice: string, grant?: string) => void;
}) {
  const mine = deciding?.key === requestKey(req) ? deciding : null;
  const always = req.choices.includes("approve") ? (req.always ?? []) : [];
  const [alwaysOpen, setAlwaysOpen] = useState(false);
  // Focus moves INTO the grants when they open and back to the toggle on Escape (#1339).
  const alwaysToggle = useRef<HTMLButtonElement>(null);
  const alwaysList = useRef<HTMLUListElement>(null);
  useEffect(() => {
    if (alwaysOpen) alwaysList.current?.querySelector<HTMLButtonElement>("button")?.focus();
  }, [alwaysOpen]);
  const decidedGrant = mine?.grant ? always.find((g) => g.id === mine.grant) : undefined;
  const mineLabel = mine
    ? mine.decision === "always" && decidedGrant
      ? `${choiceLabel("always", req)}: ${decidedGrant.label}`
      : choiceLabel(mine.decision, req)
    : "";
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
      <dl className={styles.kv}>
        {rows.map((row) => (
          <div className={styles.kvRow} key={row.key} data-testid="structured-field" data-field={row.key}>
            <dt>{row.label}</dt>
            <dd>
              {row.kind === "patch" && row.files ? (
                <Patch files={row.files} />
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
      </dl>
      <p className={styles.ids}>
        {[`request ${req.request_id}`, ...ids, req.payload_digest ? `digest ${req.payload_digest.slice(0, 8)}…` : null]
          .filter(Boolean)
          .join(" · ")}
      </p>
      {declineOnly && req.kind === "file_change" && (
        <p className={styles.why} data-testid="structured-review-only">
          <b>You can review this change but not approve it here.</b> Nothing guarantees that{" "}
          {agent} writes exactly the patch shown, so BattleLab does not approve file changes for
          an API session. Decline it and {agent} continues without the edit, or stop the turn.
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
            return [
              button,
              <button
                key="always"
                ref={alwaysToggle}
                type="button"
                className={chat.ghost}
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
      </div>
      {alwaysOpen && !mine && always.length > 0 && (
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

function TurnView({
  turn,
  agent,
  requests,
  deciding,
  readOnly,
  onDecide,
  onInterrupt,
  interrupting,
  onSendNow,
  sendNowMode,
  sendingNow,
  sendNowDisabled,
}: {
  turn: StructuredTurn;
  agent: string;
  requests: StructuredRequest[];
  deciding: Deciding | null;
  readOnly: boolean;
  onDecide: (req: StructuredRequest, choice: string, grant?: string) => void;
  onInterrupt: (turnId: string) => void;
  interrupting: boolean;
  onSendNow: (turnId: string) => void;
  sendNowMode?: "steer" | "interrupt";
  sendingNow: boolean;
  sendNowDisabled: boolean;
}) {
  const active = turn.state === "running" || turn.state === "awaiting_approval";
  const failed = ["failed", "interrupted", "uncertain", "unavailable"].includes(turn.state);
  const canSendNow = !turn.delivery || turn.delivery === "interrupt_failed";
  return (
    <div className={chat.exchange} data-testid="structured-turn" data-state={turn.state}>
      <div className={`${chat.turn} ${chat.user}`}>
        <div className={chat.who}>you</div>
        {!!turn.attachments?.length && (
          <div className={styles.turnImages}>
            {turn.attachments.map((a) => (
              // UploadImage's slot fills its parent: this box bounds it to one thumbnail.
              <span key={a.stored} className={styles.turnImageBox}>
                <UploadImage
                  path={a.stored}
                  alt="Attached image"
                  className={styles.turnImage}
                  fallback={<span className={styles.turnImageGone}>image no longer available</span>}
                />
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
            {turn.tools_truncated ? "Latest " : ""}{turn.tools.length} commands & tools
            {active && " · Working"}
            {turn.tools.some((t) => /failed|error/i.test(t.outcome)) && <span className={styles.activityFailed}> · Failed activity</span>}
            {turn.tools.some((t) => t.risk?.level === "risky") && <span className={styles.activityRisk}> · Risky activity</span>}
          </summary>
          <ul className={chat.tools} aria-label="What the agent did">
            {turn.tools.map((t) => (
              <li key={t.id} className={chat.tool} data-testid="structured-tool">
                <span className={chat.toolVerb}>{t.name}</span>
                {t.summary && <code className={chat.toolPath}>{t.summary}</code>}
                {t.outcome && <span className={chat.toolDetail}>{t.outcome}</span>}
                <RiskMark risk={t.risk} />
              </li>
            ))}
          </ul>
        </details>
      )}
      {requests.map((r) => (
        <RequestCard
          key={r.request_id}
          req={r}
          agent={agent}
          deciding={deciding}
          readOnly={readOnly}
          onDecide={onDecide}
        />
      ))}
      {turn.reply && (
        <div className={`${chat.turn} ${chat.asst} ${styles.reply}`} data-testid="structured-reply">
          <div className={chat.who}>{agent}</div>
          <div className={chat.txt}>
            <Markdown text={turn.reply_truncated ? `${turn.reply}…` : turn.reply} />
          </div>
        </div>
      )}
      {turn.state === "queued" && (
        <div className={styles.queued} data-testid="structured-queued">
          <div className={styles.queueRow}>
            <span role="status">{turn.delivery === "interrupting"
              ? "Waiting for the current response to stop · sends next"
              : turn.delivery === "steering"
                ? "Awaiting agent acknowledgement"
                : "Queued · sends after earlier turns"}</span>
            {!readOnly && sendNowMode && canSendNow && (
              <button type="button" className={styles.sendNow} disabled={sendNowDisabled}
                onClick={() => onSendNow(turn.turn_id)}>
                {sendingNow ? "Sending…" : "Send now"}
              </button>
            )}
          </div>
          {turn.delivery === "interrupt_failed" && (
            <p className={styles.activityFailed} role="alert">
              Couldn’t interrupt: {turn.delivery_reason}. This message is still queued.
            </p>
          )}
          {!readOnly && sendNowMode && canSendNow && (
            <p className={styles.queueHint}>{sendNowMode === "steer"
              ? "Adds this message to the current turn."
              : "Interrupts the current response and sends this message next."}</p>
          )}
        </div>
      )}
      {turn.state === "delivering" && <div className={chat.wait} role="status">Awaiting agent acknowledgement</div>}
      {turn.state === "delivered" && <div className={chat.wait} role="status">Added to the active turn</div>}
      {active && (
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
              className={`${chat.ghost} ${styles.inline}`}
              disabled={interrupting}
              onClick={() => onInterrupt(turn.turn_id)}
            >
              Interrupt
            </button>
          )}
        </div>
      )}
      {failed && (
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
  // #1332 Phase 3: the pictures this send carries, by the upload they landed in.
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
    const turns = new Set(snap.turns.map((t) => t.turn_id));
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
  const [deciding, setDeciding] = useState<Deciding | null>(null);
  const [actionError, setActionError] = useState<string | null>(null);
  const [interrupting, setInterrupting] = useState(false);
  const [sendingNow, setSendingNow] = useState<string | null>(null);
  const [sendNowUnknown, setSendNowUnknown] = useState<{ key: string; queuedTurnId: string } | null>(null);
  const recoveringSendNow = sendNowUnknown?.key === key ? sendNowUnknown : null;
  const sendNowIds = useRef(new Map<string, { id: string; turnId: string }>());
  const sendNowBusy = useRef(false);
  const [stopping, setStopping] = useState(false);
  const [containment, setContainment] = useState<Containment | null>(null);
  const [recap, setRecap] = useState<{ open: boolean; trigger: HTMLElement | null }>({
    open: false,
    trigger: null,
  });
  const [handoff, setHandoff] = useState<{ open: boolean; trigger: HTMLElement | null }>({
    open: false,
    trigger: null,
  });
  const [linkToast, setLinkToast] = useState<{ tick: number; ok: boolean }>({ tick: 0, ok: true });
  const op = useRef<{ id: string; text: string } | null>(null);
  const cursor = useRef(0);
  const logRef = useRef<HTMLDivElement>(null);

  const apply = useCallback((s: StructuredSnapshot) => {
    // Reads race (the poll, and a re-read after every action): an older answer arriving last
    // must never roll the view back. The journal revision only grows.
    if (s.revision < cursor.current) return;
    cursor.current = s.event_cursor;
    setSnap(s);
    // A decision is over once its exact request is no longer pending — forget it, so a later
    // request that reuses the native id never inherits it.
    setDeciding((d) =>
      d && d.phase !== "sending" && !s.pending_requests.some((r) => requestKey(r) === d.key) ? null : d,
    );
    setLoadError(null);
    setReconnecting(false);
  }, []);
  const failed = useCallback((e: unknown) => {
    if (e instanceof ApiError && e.status === 404) setLoadError("This conversation does not exist.");
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
        if (page.revision !== cursor.current || !active || ticks % 5 === 0) await load();
      } catch (e) {
        if (!alive) return;
        if (e instanceof ApiError && e.status === 409) await load();
        else {
          setReconnecting(true);
          backoff = Math.min(MAX_BACKOFF_MS, (backoff || 1_000) * 2);
        }
      }
      if (alive) timer = setTimeout(tick, backoff || (active ? STRUCTURED_ACTIVE_POLL_MS : STRUCTURED_IDLE_POLL_MS));
    };
    timer = setTimeout(tick, active ? STRUCTURED_ACTIVE_POLL_MS : STRUCTURED_IDLE_POLL_MS);
    return () => {
      alive = false;
      clearTimeout(timer);
    };
  }, [key, active, load]);

  // A decision settles when its exact request is no longer pending — never on the POST alone.
  const decidingNow =
    deciding &&
    (deciding.phase === "sending" ||
      (snap?.pending_requests.some((r) => requestKey(r) === deciding.key) ?? true))
      ? deciding
      : null;

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
    changes.observe(el, { childList: true, characterData: true, subtree: true });
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

  const send = async () => {
    if (!dict.settled()) {
      sendAfterDictation.current = true;
      if (!dict.finalizingNow()) dict.release(); // end capture; the tail still lands
      if (!dict.settled()) return;
      sendAfterDictation.current = false;
    }
    const text = draft.trim();
    const names = attachments.map((a) => a.stored);
    if ((!text && !names.length) || busy || readOnly || !snap) return;
    if (slots.current.inFlight > 0 || sending.current) return; // a picture is still on its way
    const attempt = operationFor(op.current, sendIdentity(text, names), uuid);
    op.current = attempt;
    if (historyFor.current?.op !== attempt.id) {
      // Recorded at submit time, untrimmed, so Restore round-trips exactly what was typed.
      historyFor.current = {
        op: attempt.id,
        id: appendSent({ text: draft, attachments: names, session: key, operation: attempt.id }),
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
      if (s?.turns.some((t) => t.turn_id === attempt.id)) {
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

  const decide = async (req: StructuredRequest, choice: string, grant?: string) => {
    if (readOnly) return;
    const reqKey = requestKey(req);
    if (decidingNow && (decidingNow.key !== reqKey || decidingNow.phase === "sending")) return;
    // A retry reuses the id only for the SAME decision and grant (#1339): the server binds both.
    const decision_id =
      decidingNow?.key === reqKey && decidingNow.decision === choice && decidingNow.grant === grant
        ? decidingNow.decision_id
        : uuid();
    const next: Deciding = {
      key: reqKey,
      request_id: req.request_id,
      decision: choice,
      ...(grant ? { grant } : {}),
      decision_id,
      phase: "sending",
    };
    setDeciding(next);
    setActionError(null);
    try {
      await api.structuredDecide(key, {
        decision_id,
        turn_id: req.turn_id,
        request_id: req.request_id,
        decision: choice,
        ...(grant ? { grant } : {}),
      });
      setDeciding({ ...next, phase: "sent" }); // settled by the next snapshot, not by this answer
      await load();
    } catch (e) {
      if (definite(e)) {
        setDeciding(null);
        setActionError(`Decision not taken: ${detail(e, "refused")}`);
        await load();
      } else {
        setDeciding({ ...next, phase: "unknown" });
      }
    }
  };

  const interrupt = async (turnId: string) => {
    setInterrupting(true);
    setActionError(null);
    try {
      await api.structuredInterrupt(key, uuid(), turnId);
    } catch (e) {
      setActionError(`Interrupt not sent: ${detail(e, "no answer")}`);
    } finally {
      setInterrupting(false);
      await load();
    }
  };

  const sendNow = async (queuedTurnId: string) => {
    if (sendNowBusy.current) return;
    const target = `${key}:${queuedTurnId}`;
    let operation = sendNowIds.current.get(target);
    if (recoveringSendNow?.queuedTurnId === queuedTurnId) {
      // Observe the original receipt even after delivery, worker exit, or journal truncation.
      // Current capability/turn state cannot authorize a replacement request on this path.
      if (!operation) return;
    } else {
      if (recoveringSendNow || readOnly || !snap?.active_turn || !snap?.native?.send_now) return;
      const queued = snap.turns.find((t) => t.turn_id === queuedTurnId);
      if (queued?.state !== "queued" || (queued.delivery && queued.delivery !== "interrupt_failed")) return;
      if (queued.delivery === "interrupt_failed") operation = undefined;
      operation ??= { id: uuid(), turnId: snap.active_turn };
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
        setActionError(`Send now not applied: ${detail(e, "refused")}`);
      } else {
        setSendNowUnknown({ key, queuedTurnId });
      }
    } finally {
      await load();
      setSendingNow(null);
      sendNowBusy.current = false;
    }
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
    if (!snap.turns.some((t) => t.turn_id === held.id)) return;
    op.current = null;
    const names = attachments.map((a) => a.stored);
    if (sendIdentity(draft.trim(), names) === held.text) {
      setDraft("");
      clearAttachments();
      setSendError(null);
    }
  }, [snap, draft, attachments]);

  /** Restore / Insert (#1332 Phase 3b): replace the draft with `text` and these uploads. Only
   *  pictures this client takes come along, up to the cap; what is left behind is said. */
  const fill = (text: string, uploads: string[]): string[] | null => {
    // Tools are disabled then too. A live dictation would overwrite a replaced draft with its
    // next result, so it counts as in flight.
    if (sending.current || slots.current.inFlight > 0 || !dict.settled()) return null;
    clearAttachments();
    const pictures = snap?.images ? uploads.filter((p) => /\.(png|jpe?g|gif|webp)$/i.test(p)) : [];
    const kept = pictures.slice(0, MAX_IMAGES).map((p) => {
      const stored = uploadStoredName(p);
      return { name: stored, stored };
    });
    slots.current.held = kept.length;
    setAttachments(kept);
    op.current = null; // a new draft is a new request
    setDraft(text);
    const dropped = uploads.length - kept.length;
    setSendError(
      dropped > 0
        ? `${dropped} attachment${dropped === 1 ? " was" : "s were"} left out: ${
            snap?.images ? `only up to ${MAX_IMAGES} images can be sent here` : `${agent} takes no images`
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
      op.current = { id: entry.operation, text: sendIdentity(entry.text.trim(), names) };
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
      setSendError("Wait until this message is sent before attaching another image.");
      return;
    }
    const images = files.filter((f) => f.type.startsWith("image/"));
    if (!images.length) {
      if (files.length) setSendError("Only images can be attached here.");
      return;
    }
    // Reserve slots NOW, before any await: a second paste racing this one sees them taken.
    const room = MAX_IMAGES - slots.current.held - slots.current.inFlight;
    const taken = images.slice(0, Math.max(0, room));
    if (taken.length < images.length) setSendError(`A message carries at most ${MAX_IMAGES} images.`);
    else setSendError(null);
    if (!taken.length) return;
    slots.current.inFlight += taken.length;
    setInFlight(slots.current.inFlight);
    const gen = generation.current;
    let failed = false;
    try {
      for (const file of taken) {
        try {
          const up = await api.upload(file);
          if (gen !== generation.current) continue; // its draft was sent or cleared meanwhile
          const stored = up.stored ?? uploadStoredName(up.path);
          slots.current.held += 1;
          setAttachments((prev) => [...prev, { name: up.name, stored }]);
        } catch {
          failed = true;
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
        if (failed) setSendError("An image could not be uploaded.");
        if (fileRef.current) fileRef.current.value = "";
      }
    }
  };

  // An image paste becomes an attachment; plain text is left to the textarea. A deferred
  // clipboard that delivers neither falls back to the async clipboard (#530), as Compose does.
  const onPaste = (e: ReactClipboardEvent<HTMLTextAreaElement>) => {
    if (!snap?.images) return;
    if (sending.current) {
      // A read-only textarea still receives paste: nothing joins a message already sending.
      e.preventDefault();
      return;
    }
    const images = imageFilesFromData(e.clipboardData);
    if (images.length) {
      e.preventDefault();
      void attach(images);
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
    void imageFilesFromAsyncClipboard().then(
      (fallback) => {
        release();
        if (fallback.length && gen === generation.current) void attach(fallback);
      },
      release,
    );
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
        setRecap({ open: true, trigger: trigger ?? (document.activeElement as HTMLElement | null) }),
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
          ...(snap.images
            ? [
                {
                  id: "attach",
                  aria: "Attach images",
                  title: "Attach images",
                  icon: <Paperclip size={16} />,
                  disabled: busy || attachments.length + inFlight >= MAX_IMAGES,
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
        {/* Skip-permissions is fixed at create (#1339): say so, so a session that never asks is
            never mistaken for one that does. */}
        {snap?.bypass && (
          <span
            className={`${chat.chip} ${styles.skipChip}`}
            data-testid="structured-bypass"
            title="Started with Skip permission prompts: this agent never asks before acting."
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
  const pending = snap?.pending_requests ?? [];
  return (
    <div className={`${chat.pane} ${styles.pane}`} data-testid="structured-pane">
      {header}
      {overlays}
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
                  <dd data-testid="structured-model">{model ?? "agent default"}</dd>
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
        {turns.map((t) => (
          <TurnView
            key={t.turn_id}
            turn={t}
            agent={agent}
            requests={pending.filter((r) => r.turn_id === t.turn_id)}
            deciding={decidingNow}
            readOnly={readOnly}
            onDecide={(r, c, g) => void decide(r, c, g)}
            onInterrupt={(tid) => void interrupt(tid)}
            interrupting={interrupting}
            onSendNow={(tid) => void sendNow(tid)}
            sendNowMode={snap?.active_turn ? snap.native?.send_now : undefined}
            sendingNow={sendingNow === t.turn_id}
            sendNowDisabled={!!sendingNow || !!recoveringSendNow || turns.some((q) => q.state === "queued" && q.delivery === "interrupting")}
          />
        ))}
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
            <div className={compose.pills} aria-label="Attached images" role="list">
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
            disabled={readOnly || notStarted}
          />
        </div>
        <div className={compose.row}>
          <KeyBar actions={keyActions} />
          <span className={compose.spacer} role="status">
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
              <span className={compose.micLabel}>Push to talk</span>
            </button>
          )}
          <button
            type="submit"
            className={`${compose.send} shine`}
            title="Send + Enter"
            disabled={
              readOnly ||
              notStarted ||
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
        {snap?.images && !readOnly && (
          <input
            ref={fileRef}
            type="file"
            accept="image/png,image/jpeg,image/gif,image/webp"
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
