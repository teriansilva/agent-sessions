import { useCallback, useEffect, useRef, useState } from "react";
import type { KeyboardEvent } from "react";
import { engineBadge, engineInfo, engineLabel, useEngineRoster } from "../../app/engineRoster";
import { ApiError, api } from "../../lib/api";
import type {
  Containment,
  StructuredRequest,
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
  onDecide: (req: StructuredRequest, choice: string) => void;
}) {
  const mine = deciding?.key === requestKey(req) ? deciding : null;
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
                    Not offered: an API session approves one request at a time.
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
            ? `Sending “${choiceLabel(mine.decision, req)}”…`
            : mine.phase === "sent"
              ? `“${choiceLabel(mine.decision, req)}” sent — waiting for ${agent} to take it.`
              : `“${choiceLabel(mine.decision, req)}” may not have reached BattleLab. Retrying sends the same decision.`}
        </p>
      )}
      <div className={styles.actions}>
        {mine?.phase === "unknown" ? (
          <button type="button" className={chat.ghost} onClick={() => onDecide(req, mine.decision)}>
            Retry “{choiceLabel(mine.decision, req)}”
          </button>
        ) : (
          req.choices.map((choice) => (
            <button
              key={choice}
              type="button"
              className={choice === "approve" ? chat.send : chat.ghost}
              disabled={readOnly || !!mine}
              onClick={() => onDecide(req, choice)}
            >
              {choiceLabel(choice, req)}
            </button>
          ))
        )}
      </div>
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
}: {
  turn: StructuredTurn;
  agent: string;
  requests: StructuredRequest[];
  deciding: Deciding | null;
  readOnly: boolean;
  onDecide: (req: StructuredRequest, choice: string) => void;
  onInterrupt: (turnId: string) => void;
  interrupting: boolean;
}) {
  const active = turn.state === "running" || turn.state === "awaiting_approval";
  const failed = ["failed", "interrupted", "uncertain", "unavailable"].includes(turn.state);
  return (
    <div className={chat.exchange} data-testid="structured-turn" data-state={turn.state}>
      <div className={`${chat.turn} ${chat.user}`}>
        <div className={chat.who}>you</div>
        <div className={chat.txt}>
          {turn.text}
          {turn.text_truncated && "…"}
        </div>
      </div>
      {turn.tools.length > 0 && (
        <ul className={chat.tools} aria-label="What the agent did">
          {turn.tools.map((t) => (
            <li key={t.id} className={chat.tool} data-testid="structured-tool">
              <span className={chat.toolVerb}>{t.name}</span>
              {t.summary && <code className={chat.toolPath}>{t.summary}</code>}
              {t.outcome && <span className={chat.toolDetail}>{t.outcome}</span>}
            </li>
          ))}
        </ul>
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
        <div className={`${chat.turn} ${chat.asst}`}>
          <div className={chat.who}>{agent}</div>
          <div className={chat.txt}>
            <Markdown text={turn.reply_truncated ? `${turn.reply}…` : turn.reply} />
          </div>
        </div>
      )}
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

/** A native API client's session (#1311): BattleLab drives the installed CLI through its
 *  structured protocol and keeps a journal; this view only READS it and sends operator input.
 *  Reconnects resume from the event cursor — a closed tab never cancels a turn — and every send
 *  and decision carries an id that makes a retry a no-op on the server. */
export function StructuredPane({ engine, id }: { engine: string; id: string }) {
  useEngineRoster();
  const key = `${engine}:${id}`;
  // The agent is the console agent this client drives (`api.source`), named by its own label.
  const source = engineInfo(engine)?.api?.source;
  const agent = source ? engineLabel(source) : engineLabel(engine);
  const [snap, setSnap] = useState<StructuredSnapshot | null>(null);
  const [loadError, setLoadError] = useState<string | null>(null);
  const [reconnecting, setReconnecting] = useState(false);
  const [draft, setDraft] = useState("");
  const [sendError, setSendError] = useState<string | null>(null);
  const [busy, setBusy] = useState(false);
  const [deciding, setDeciding] = useState<Deciding | null>(null);
  const [actionError, setActionError] = useState<string | null>(null);
  const [interrupting, setInterrupting] = useState(false);
  const [stopping, setStopping] = useState(false);
  const [containment, setContainment] = useState<Containment | null>(null);
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

  useEffect(() => {
    const el = logRef.current;
    if (el && typeof el.scrollTo === "function") el.scrollTo({ top: el.scrollHeight });
  }, [snap?.turns.length, snap?.pending_requests.length]);

  const readOnly = !!snap?.read_only;

  const send = async () => {
    const text = draft.trim();
    if (!text || busy || active || readOnly || !snap) return;
    const attempt = operationFor(op.current, text, uuid);
    op.current = attempt;
    setBusy(true);
    setSendError(null);
    try {
      await api.structuredSubmit(key, attempt.id, text, snap.revision);
      op.current = null;
      setDraft("");
      setContainment(null);
      await load();
    } catch (e) {
      const s = await load();
      if (s?.turns.some((t) => t.turn_id === attempt.id)) {
        op.current = null;
        setDraft("");
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
      setBusy(false);
    }
  };

  const decide = async (req: StructuredRequest, choice: string) => {
    if (readOnly) return;
    const reqKey = requestKey(req);
    if (decidingNow && (decidingNow.key !== reqKey || decidingNow.phase === "sending")) return;
    const decision_id =
      decidingNow?.key === reqKey && decidingNow.decision === choice
        ? decidingNow.decision_id
        : uuid();
    const next: Deciding = {
      key: reqKey,
      request_id: req.request_id,
      decision: choice,
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

  const header = (
    <div className={chat.head}>
      <span className={`${chat.badge} ${styles.badge}`}>{engineBadge(engine).toUpperCase()}</span>
      <span className={chat.title}>{agent}</span>
      <span className={styles.kindTag}>API</span>
      <span className={chat.meta}>
        {model && (
          <span className={`${chat.chip} ${chat.model}`} data-testid="structured-model">
            <span className={chat.sq} aria-hidden="true" />
            {model}
          </span>
        )}
        <span className={chat.chip} data-testid="structured-worker" role="status">
          <span className={`${styles.led} ${workerChip.cls}`} aria-hidden="true" />
          {workerChip.text}
        </span>
        {/* Stop stays available while the client retires: a running worker is never stranded. */}
        {live && (
          <button type="button" className={`${chat.ghost} ${styles.inline}`} disabled={stopping} onClick={() => void stop()}>
            Stop
          </button>
        )}
      </span>
    </div>
  );

  if (loadError) {
    return (
      <div className={chat.pane} data-testid="structured-pane">
        {header}
        <div className={chat.center}>
          <p className={chat.note}>{loadError}</p>
        </div>
      </div>
    );
  }

  const turns = snap?.turns ?? [];
  const pending = snap?.pending_requests ?? [];
  return (
    <div className={chat.pane} data-testid="structured-pane">
      {header}
      {readOnly && (
        <p className={styles.banner} role="status" data-testid="structured-read-only">
          Read only — {snap?.read_only}. The history stays; new messages and decisions are off until
          the client is available again.
        </p>
      )}
      <div className={chat.log} ref={logRef} aria-live="polite">
        {snap && turns.length === 0 && (
          <div className={chat.center} data-testid="structured-empty">
            <p className={chat.note}>
              No terminal: BattleLab drives your installed {agent} CLI through its structured
              protocol, with its own login, config, MCP servers and skills. You answer each
              request it makes here.
            </p>
            <code className={chat.chip}>{snap.cwd}</code>
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
            onDecide={(r, c) => void decide(r, c)}
            onInterrupt={(tid) => void interrupt(tid)}
            interrupting={interrupting}
          />
        ))}
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
      <form
        className={chat.comp}
        onSubmit={(e) => {
          e.preventDefault();
          void send();
        }}
      >
        <textarea
          className={chat.ta}
          aria-label={`Message ${agent}`}
          placeholder={
            readOnly
              ? "This conversation is read only"
              : active
                ? `${agent} is working — your next message waits for this turn`
                : `Message ${agent}…`
          }
          value={draft}
          onChange={(e) => setDraft(e.target.value)}
          onKeyDown={onKey}
          readOnly={busy}
          rows={2}
          disabled={readOnly}
        />
        <button
          type="submit"
          className={chat.send}
          disabled={readOnly || busy || active || !snap || !draft.trim()}
        >
          Send
        </button>
      </form>
    </div>
  );
}
