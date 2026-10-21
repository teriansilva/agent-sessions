import { useApiLogProps } from "../../theme/apiLookStore";
import { useCallback, useEffect, useMemo, useRef, useState } from "react";
import type { KeyboardEvent } from "react";
import { Link } from "react-router-dom";
import { engineBadge, engineInfo, engineLabel, useEngineRoster } from "../../app/engineRoster";
import { ApiError, api } from "../../lib/api";
import { reloadRoster } from "../../app/reloadRoster";
import { agentPath } from "../../routes/settingsTabs";
import { OpeningPanel } from "../terminal/SessionOpening";
import type { AgentEndpoint, ChatSession, ChatToolCall, ChatTurn } from "../../types/api";
import { OutboxBlock } from "../structured/Outbox";
import { useAlive, useLeaveGuard, useOutbox } from "../structured/useOutbox";
import {
  nextToPost,
  outboxEntry,
  reconcile,
  retried,
  unrecorded,
  updateEntry,
  withoutEntry,
  type OutboxEntry,
} from "../structured/outbox";
import styles from "./ChatPane.module.css";
import { EditProposal } from "./EditProposal";

/** How often a pending turn is re-read. The server owns the request (#1209): the pane only READS,
 *  so a reload, a lost response or a dropped connection recovers by polling, never by resending. */
export const CHAT_POLL_MS = 2_000;
/** After a busy refusal (409), the first fresh read waits this long, doubling per busy answer in a
 *  row up to BUSY_BACKOFF_MAX_MS: a send never hammers a conversation another worker holds. */
export const BUSY_BACKOFF_MS = 1_000;
export const BUSY_BACKOFF_MAX_MS = 15_000;

/** `chat_runtime`'s transient conflicts, told by the 409 itself (not by a read, which can race it):
 *  another worker holds the conversation's fence (`in_flight — another worker owns …`, raised
 *  BEFORE any turn is appended, so a read may still show nothing in flight), a reply is running,
 *  or a proposed change awaits a decision. The bare `in_flight` is different: the server holds
 *  THIS turn pending (an exact repeat). */
const BUSY_DETAIL = /^in_flight — |still waiting for its reply|awaiting your decision/;

function newTurnId(): string {
  return crypto.randomUUID();
}

/** Text with fenced code blocks: everything is rendered as TEXT (React escapes it). Fences become
 *  `<pre>` blocks; the rest keeps its line breaks. No Markdown engine, no HTML ever injected. */
function ReplyText({ text }: { text: string }) {
  const parts = text.split(/```[^\n]*\n?/);
  return (
    <>
      {parts.map((p, i) =>
        i % 2 === 1 ? (
          <pre key={i} className={styles.code}>
            {p.replace(/\n$/, "")}
          </pre>
        ) : p ? (
          <span key={i} className={styles.prose}>
            {p}
          </span>
        ) : null,
      )}
    </>
  );
}

function lineSpan(c: ChatToolCall): string {
  if (c.start_line == null || c.end_line == null) return "";
  const of = c.total_lines != null ? ` of ${c.total_lines}` : "";
  return `lines ${c.start_line}–${c.end_line}${of}`;
}

/** One tool call, as a one-line summary (#1222). Nothing here renders file contents: the server
 *  never stores or returns them. */
function ToolRow({ call }: { call: ChatToolCall }) {
  const verb =
    call.outcome === "refused"
      ? "Refused"
      : call.outcome === "stopped"
        ? "Stopped"
        : call.outcome === "running"
        ? call.name === "list_files"
          ? "Listing"
          : "Reading"
        : call.name === "list_files"
          ? "Listed"
          : "Read";
  const detail =
    call.outcome === "refused"
      ? (call.reason ?? "not allowed")
      : call.outcome === "stopped"
        ? "did not finish"
        : call.outcome === "running"
        ? "…"
        : call.name === "list_files"
          ? `${call.entries ?? 0} ${call.entries === 1 ? "entry" : "entries"}`
          : lineSpan(call);
  return (
    <li
      className={`${styles.tool} ${call.outcome === "refused" || call.outcome === "stopped" ? styles.toolRefused : ""}`}
      data-testid="chat-tool"
      data-outcome={call.outcome}
    >
      <span className={styles.toolVerb}>{verb}</span>
      <code className={styles.toolPath}>{call.path || "."}</code>
      {detail && <span className={styles.toolDetail}>{detail}</span>}
    </li>
  );
}

function runningCall(t: ChatTurn): string | null {
  const c = (t.tools ?? []).find((x) => x.outcome === "running");
  if (!c) return null;
  return `${c.name === "list_files" ? "listing" : "reading"} ${c.path || "."}`;
}

function ToolRows({ calls }: { calls: ChatToolCall[] }) {
  if (calls.length === 0) return null;
  return (
    <ul className={styles.tools} aria-label="Files the agent looked at">
      {calls.map((c) => (
        <ToolRow key={c.call_id} call={c} />
      ))}
    </ul>
  );
}

function clock(ts: number | null | undefined): string {
  if (!ts) return "";
  return new Date(ts * 1000).toLocaleTimeString([], { hour: "2-digit", minute: "2-digit" });
}

function tokens(n: number): string {
  return n >= 1000 ? `${(n / 1000).toFixed(1)}k tok` : `${n} tok`;
}

/** A chat-runtime session (#853 P9a, #1209): BattleLab talks to the agent's endpoint and keeps the
 *  conversation. No terminal; tools require opt-in. Proposed file edits (#1230) each require a separate decision. */
export function ChatPane({
  engine,
  id,
  runtimeConfirmed = true,
}: {
  engine: string;
  id: string;
  /** False while this pane was chosen from a CACHED runtime hint the roster has not confirmed
   *  (#1502). Until it is, a failed first read is not a verdict about the conversation — the
   *  engine may run as another runtime now — so it renders "Opening…" (bounded by Retry). */
  runtimeConfirmed?: boolean;
}) {
  useEngineRoster();
  // The API-session look (#1494 P3), on the conversation log only: `{}` for the default look.
  const logLook = useApiLogProps();
  const sid = `${engine}:${id}`;
  const [session, setSession] = useState<ChatSession | null>(null);
  const [loadError, setLoadError] = useState<string | null>(null);
  const [endpoint, setEndpoint] = useState<AgentEndpoint | null>(null);
  const [draft, setDraft] = useState("");
  const [sendError, setSendError] = useState<string | null>(null);
  const [now, setNow] = useState(() => Date.now());
  const logRef = useRef<HTMLDivElement>(null);
  const taRef = useRef<HTMLTextAreaElement>(null);
  // The outbox (#1465), the same contract as the structured clients': Send moves the message here
  // at once; FIFO, only the head in flight; a turn the conversation shows replaces its entry.
  const { outbox, outboxRef, setOutbox } = useOutbox();
  const alive = useAlive();
  // A re-read that fails once the conversation is showing keeps it (and every pending entry) in
  // view and says so; only a first read or a 404 replaces the pane with an error.
  const [stale, setStale] = useState(false);
  const shown = useRef(false);
  // After a busy 409 nothing is posted again until a read that SUCCEEDED after the backoff
  // (`until`) shows the conversation idle — never against an older idle read, which would re-POST
  // in a loop while reads fail. `busyRun` counts busy answers in a row, for the backoff.
  const [hold, setHoldState] = useState<{ until: number } | null>(null);
  const holdRef = useRef<{ until: number } | null>(null);
  const setHold = useCallback((next: { until: number } | null) => {
    holdRef.current = next;
    setHoldState(next);
  }, []);
  const busyRun = useRef(0);
  const backOff = useCallback(() => {
    busyRun.current += 1;
    const wait = Math.min(BUSY_BACKOFF_MAX_MS, BUSY_BACKOFF_MS * 2 ** (busyRun.current - 1));
    setHold({ until: Date.now() + wait });
  }, [setHold]);

  const take = useCallback((s: ChatSession) => {
    shown.current = true;
    setSession(s);
    setLoadError(null);
    setStale(false);
    if (holdRef.current && Date.now() >= holdRef.current.until) setHold(null); // a fresh read
    // Turns are keyed by the id the browser minted, so a recorded message replaces its entry.
    setOutbox((list) => reconcile(list, s.turns.map((t) => t.turn_id)));
  }, [setOutbox, setHold]);
  const failedRead = useCallback((e: unknown) => {
    const gone = e instanceof ApiError && e.status === 404;
    if (shown.current && !gone) {
      setStale(true);
      return;
    }
    setLoadError(
      gone
        ? "This conversation does not exist (or was never started)."
        : "Couldn’t load this conversation.",
    );
  }, []);

  const load = useCallback(async () => {
    try {
      const s = await api.chatGet(sid);
      take(s);
      return s;
    } catch (e) {
      failedRead(e);
      return null;
    }
  }, [sid, take, failedRead]);

  useEffect(() => {
    let alive = true;
    api
      .chatGet(sid)
      .then((s) => {
        if (alive) take(s);
      })
      .catch((e: unknown) => {
        if (alive) failedRead(e);
      });
    return () => {
      alive = false;
    };
  }, [sid, take, failedRead]);

  useEffect(() => {
    let alive = true;
    api
      .agentEndpoint(engine)
      .then((ep) => alive && setEndpoint(ep))
      .catch(() => {});
    return () => {
      alive = false;
    };
  }, [engine]);

  const inFlight = session?.in_flight ?? null;
  // A message on its way (or of unknown outcome) is settled by the conversation: read it too.
  const settling = outbox.some((e) => e.phase === "sending" || e.phase === "unknown");
  // While a turn is pending: re-read it until it settles, and tick the elapsed clock.
  useEffect(() => {
    if (!inFlight && !settling) return;
    const poll = setInterval(() => void load(), CHAT_POLL_MS);
    const tick = setInterval(() => setNow(Date.now()), 1_000);
    return () => {
      clearInterval(poll);
      clearInterval(tick);
    };
  }, [inFlight, settling, load]);

  useEffect(() => {
    const el = logRef.current;
    if (el && typeof el.scrollTo === "function") el.scrollTo({ top: el.scrollHeight });
  }, [session?.turns.length, inFlight, outbox.length]); // a message just sent shows at the bottom

  const configured = endpoint ? endpoint.configured : engineInfo(engine)?.present !== false;
  const totalTokens = useMemo(
    () =>
      (session?.turns ?? []).reduce((n, t) => n + (t.usage?.total_tokens ?? 0), 0),
    [session],
  );

  /** Send (#1465): the message becomes ONE outbox entry with its own turn id, and the composer is
   *  free at once. Messages sent while a reply runs wait in order; the pump posts the head. */
  const send = () => {
    const text = draft.trim();
    if (!text || !configured) return;
    setOutbox((list) => [
      ...list,
      outboxEntry({ id: newTurnId(), text, draft, attachments: [], history: null }),
    ]);
    setDraft("");
    setSendError(null);
  };

  /** POST one entry: exactly its frozen turn id and text. The outcome only updates THAT entry, and
   *  only while it is still in the outbox (a read may have settled it first). */
  const post = async (entry: OutboxEntry) => {
    const { id, text } = entry.request;
    setOutbox((list) => updateEntry(list, id, (e) => ({ ...e, phase: "sending" })));
    try {
      await api.chatSend(sid, id, text);
      busyRun.current = 0;
      if (!alive.current) return;
      setOutbox((list) => updateEntry(list, id, (e) => ({ ...e, recorded: true })));
      await load(); // its turn replaces the entry
    } catch (e) {
      if (!alive.current) return;
      const latest = await load();
      if (!alive.current) return;
      if (latest?.turns.some((t) => t.turn_id === id)) return; // the conversation has it
      // A 4xx is the server's answer: it did not take this turn. Anything else (no response, a
      // gateway error) is ambiguous — it may have been accepted.
      const definite = e instanceof ApiError && e.status < 500;
      const detail = e instanceof ApiError ? e.message : "";
      const conflict = e instanceof ApiError && e.status === 409;
      // `chat_runtime` answers an exact repeat of a turn it holds pending with a bare 409
      // "in_flight": the server HAS this message (a Retry after a lost response). Never a refusal.
      const held = conflict && detail === "in_flight";
      // Busy (another worker holds the conversation, a reply runs, a change awaits a decision —
      // another tab got there first) is not a refusal of THIS message: it waits, under its id,
      // and goes again only after a fresh successful read (see `hold`).
      const notNow = conflict && !held && (BUSY_DETAIL.test(detail) || !!latest?.in_flight);
      if (notNow) backOff();
      const reason = detail || "refused";
      setOutbox((list) =>
        updateEntry(list, id, (x) =>
          held
            ? { ...x, phase: "sending", recorded: true }
            : notNow
              ? { ...x, phase: "waiting" }
              : definite
                ? { ...x, phase: "refused", reason }
                : { ...x, phase: "unknown" },
        ),
      );
    }
  };

  // The pump. `chat_runtime` takes ONE reply at a time and refuses a second turn while a reply
  // runs or a proposed change awaits a decision (409), so the head goes only when the latest read
  // shows nothing in flight (`in_flight` covers both), and a recorded POST alone never advances
  // the queue: the read that shows its turn also shows it running. The chat route carries no
  // expected revision; admission is read fresh from the conversation instead.
  const admitted = !!session && configured && !session.in_flight && !hold;
  // The hold's re-read: at `until`, read; a failed read backs off again, so a waiting message is
  // never stranded once the other worker lets go, and reads stay bounded while the server is down.
  useEffect(() => {
    if (!hold) return;
    const timer = setTimeout(() => {
      void load().then((s) => {
        if (!s && alive.current && holdRef.current === hold) backOff();
      });
    }, Math.max(0, hold.until - Date.now()));
    return () => clearTimeout(timer);
  }, [hold, load, backOff, alive]);
  useEffect(() => {
    const head = nextToPost(outboxRef.current, admitted);
    if (head) void post(head);
    // `post` is per render; the outbox and the admission are the triggers.
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [outbox, admitted]);

  // Anything the server does not have yet is lost on a reload or close: ask first.
  useLeaveGuard(unrecorded(outbox));

  // Edit replaces nothing the operator is writing: it needs an empty composer.
  const composerHolds = !!draft.trim();
  /** Edit (refused only — the server took nothing under its id): the words go back into the
   *  composer and the entry leaves the queue, releasing the messages behind it. Sent again, it is
   *  a NEW message (new id) at the end of the queue. */
  const editEntry = (id: string) => {
    const entry = outboxRef.current.find((e) => e.request.id === id);
    if (!entry || entry.phase !== "refused" || composerHolds) return;
    setOutbox((list) => list.filter((e) => e.request.id !== id));
    setDraft(entry.request.draft);
    setSendError(null);
    requestAnimationFrame(() => taRef.current?.focus());
  };
  const dropEntry = (id: string) => {
    setOutbox((list) => withoutEntry(list, id));
    requestAnimationFrame(() => taRef.current?.focus());
  };

  const retry = async (turn: ChatTurn) => {
    setSendError(null);
    try {
      await api.chatRetry(sid, turn.turn_id);
    } catch (e) {
      setSendError(e instanceof ApiError && e.message ? e.message : "Retry failed to start.");
    }
    await load();
  };

  const onKey = (e: KeyboardEvent<HTMLTextAreaElement>) => {
    if (e.key === "Enter" && !e.shiftKey && !e.nativeEvent.isComposing) {
      e.preventDefault();
      send();
    }
  };

  const header = (
    <div className={styles.head}>
      <span className={styles.badge}>{engineBadge(engine).toUpperCase()}</span>
      <span className={styles.title}>{engineLabel(engine)}</span>
      <span className={styles.meta}>
        {endpoint?.model && (
          <span className={`${styles.chip} ${styles.model}`} data-testid="chat-model">
            <span className={styles.sq} aria-hidden="true" />
            {endpoint.model}
          </span>
        )}
        {totalTokens > 0 && <span className={styles.chip}>{tokens(totalTokens)}</span>}
      </span>
    </div>
  );

  if (!runtimeConfirmed && !session && loadError) return <OpeningPanel reload={reloadRoster} />;

  if (loadError) {
    return (
      <div className={styles.pane} data-testid="chat-pane">
        {header}
        <div className={styles.center}>
          <p className={styles.note}>{loadError}</p>
        </div>
      </div>
    );
  }

  if (!configured && (session?.turns.length ?? 0) === 0) {
    return (
      <div className={styles.pane} data-testid="chat-pane">
        {header}
        <div className={styles.center} data-testid="chat-unconfigured">
          <p className={styles.note}>
            This agent has no endpoint yet. Nothing is sent anywhere until you add one.
          </p>
          <Link className={styles.ghost} to={agentPath(engine)}>
            Open agent settings
          </Link>
        </div>
      </div>
    );
  }

  const turns = session?.turns ?? [];
  return (
    <div className={styles.pane} data-testid="chat-pane">
      {header}
      <div className={styles.log} ref={logRef} aria-live="polite" data-testid="api-log" {...logLook}>
        {session && turns.length === 0 && (
          <div className={styles.center} data-testid="chat-empty">
            <p className={styles.note}>
              No terminal and no CLI: BattleLab sends your messages to the endpoint configured for
              this agent and keeps the conversation itself.{" "}
              {endpoint?.tools === "write"
                ? "It can read files and propose edits to existing text files in this conversation’s folder. You review and approve each change before it is saved. Hidden and credential-shaped files are refused."
                : endpoint?.tools === "read"
                ? "It can list and read files in this conversation’s folder — never write, delete or run anything. Hidden and credential-shaped files are refused."
                : "It has no tools — it can only reply."}
            </p>
            {endpoint?.model && (
              <span className={styles.chip}>
                <span className={styles.sq} aria-hidden="true" />
                {endpoint.model}
              </span>
            )}
          </div>
        )}
        {turns.map((t) => (
          <div key={t.turn_id} className={styles.exchange} data-testid="chat-turn">
            {t.dropped > 0 && (
              <p className={styles.notice} data-testid="chat-dropped">
                {t.dropped} earlier {t.dropped === 1 ? "turn was" : "turns were"} not sent — the
                conversation is longer than this endpoint’s context window.
              </p>
            )}
            <div className={`${styles.turn} ${styles.user}`}>
              <div className={styles.who}>
                you <span className={styles.ts}>{clock(t.ts)}</span>
              </div>
              <div className={styles.txt}>{t.text}</div>
            </div>
            <ToolRows calls={t.tools ?? []} />
            {(t.proposals ?? []).map((p) => <EditProposal key={p.id} sid={sid} proposal={p} reload={load} />)}
            {t.status === "awaiting_approval" && <p className={styles.notice}>
              The agent is waiting for your decision. You can leave and return to review this change.
            </p>}
            {t.status === "done" && t.reply !== null && (
              <div className={`${styles.turn} ${styles.asst}`}>
                <div className={styles.who}>
                  {engineLabel(engine)}{" "}
                  <span className={styles.ts}>
                    {clock(t.reply_ts)}
                    {t.usage?.total_tokens ? ` · ${tokens(t.usage.total_tokens)}` : ""}
                  </span>
                </div>
                <div className={styles.txt}>
                  <ReplyText text={t.reply} />
                </div>
                {t.truncated && (
                  <p className={styles.notice} data-testid="chat-truncated">
                    The reply stopped at the output limit — ask it to continue.
                  </p>
                )}
              </div>
            )}
            {t.status === "pending" && (
              <div className={styles.wait} data-testid="chat-waiting">
                <span className={styles.bars} aria-hidden="true">
                  <span />
                  <span />
                  <span />
                </span>
                {runningCall(t) ?? "waiting for the endpoint"} ·{" "}
                {Math.max(0, Math.round(now / 1000 - t.ts))}s
              </div>
            )}
            {t.status === "failed" && (
              <div className={styles.failed}>
                <div className={styles.err} role="alert" data-testid="chat-failed">
                  {t.reason ?? "The request failed."} Your message was kept; nothing was lost.
                </div>
                <button
                  type="button"
                  className={styles.ghost}
                  onClick={() => void retry(t)}
                  disabled={Boolean(inFlight)}
                >
                  Retry
                </button>
              </div>
            )}
          </div>
        ))}
        <OutboxBlock
          entries={outbox}
          agent={engineLabel(engine)}
          headHold={
            !configured
              ? "Can’t go out until this agent has an endpoint."
              : turns.some((t) => t.status === "awaiting_approval")
                ? "Goes out once you have decided the proposed change."
                : inFlight
                  ? "Goes out when the current reply has finished."
                  : null
          }
          editBlocked={composerHolds}
          onRetry={(id) => setOutbox((list) => retried(list, id))}
          onEdit={editEntry}
          onDrop={dropEntry}
        />
        {stale && (
          <p className={styles.notice} role="status" data-testid="chat-reconnecting">
            Couldn’t re-read this conversation. Showing what was last read; nothing was re-sent.
          </p>
        )}
      </div>
      {sendError && (
        <p className={styles.sendError} role="alert" data-testid="chat-send-error">
          {sendError}
        </p>
      )}
      <form
        className={styles.comp}
        onSubmit={(e) => {
          e.preventDefault();
          send();
        }}
      >
        <textarea
          ref={taRef}
          className={styles.ta}
          aria-label="Message the agent"
          placeholder={configured ? "Message the agent…" : "This agent has no endpoint"}
          value={draft}
          onChange={(e) => setDraft(e.target.value)}
          onKeyDown={onKey}
          rows={2}
          disabled={!configured}
        />
        <button
          type="submit"
          className={styles.send}
          disabled={!configured || !draft.trim()}
        >
          Send
        </button>
      </form>
    </div>
  );
}
