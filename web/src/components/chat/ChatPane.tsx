import { useCallback, useEffect, useMemo, useRef, useState } from "react";
import type { KeyboardEvent } from "react";
import { Link } from "react-router-dom";
import { engineBadge, engineInfo, engineLabel, useEngineRoster } from "../../app/engineRoster";
import { ApiError, api } from "../../lib/api";
import { agentPath } from "../../routes/settingsTabs";
import type { AgentEndpoint, ChatSession, ChatToolCall, ChatTurn } from "../../types/api";
import styles from "./ChatPane.module.css";

/** How often a pending turn is re-read. The server owns the request (#1209): the pane only READS,
 *  so a reload, a lost response or a dropped connection recovers by polling, never by resending. */
export const CHAT_POLL_MS = 2_000;

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
 *  conversation. No terminal; tools only if the operator turned them on (#1222), and read-only. */
export function ChatPane({ engine, id }: { engine: string; id: string }) {
  useEngineRoster();
  const sid = `${engine}:${id}`;
  const [session, setSession] = useState<ChatSession | null>(null);
  const [loadError, setLoadError] = useState<string | null>(null);
  const [endpoint, setEndpoint] = useState<AgentEndpoint | null>(null);
  const [draft, setDraft] = useState("");
  const [sendError, setSendError] = useState<string | null>(null);
  const [busy, setBusy] = useState(false);
  const [now, setNow] = useState(() => Date.now());
  const logRef = useRef<HTMLDivElement>(null);

  const load = useCallback(async () => {
    try {
      const s = await api.chatGet(sid);
      setSession(s);
      setLoadError(null);
      return s;
    } catch (e) {
      setLoadError(
        e instanceof ApiError && e.status === 404
          ? "This conversation does not exist (or was never started)."
          : "Couldn’t load this conversation.",
      );
      return null;
    }
  }, [sid]);

  useEffect(() => {
    let alive = true;
    api
      .chatGet(sid)
      .then((s) => {
        if (!alive) return;
        setSession(s);
        setLoadError(null);
      })
      .catch((e: unknown) => {
        if (!alive) return;
        setLoadError(
          e instanceof ApiError && e.status === 404
            ? "This conversation does not exist (or was never started)."
            : "Couldn’t load this conversation.",
        );
      });
    return () => {
      alive = false;
    };
  }, [sid]);

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
  // While a turn is pending: re-read it until it settles, and tick the elapsed clock.
  useEffect(() => {
    if (!inFlight) return;
    const poll = setInterval(() => void load(), CHAT_POLL_MS);
    const tick = setInterval(() => setNow(Date.now()), 1_000);
    return () => {
      clearInterval(poll);
      clearInterval(tick);
    };
  }, [inFlight, load]);

  useEffect(() => {
    const el = logRef.current;
    if (el && typeof el.scrollTo === "function") el.scrollTo({ top: el.scrollHeight });
  }, [session?.turns.length, inFlight]);

  const configured = endpoint ? endpoint.configured : engineInfo(engine)?.present !== false;
  const totalTokens = useMemo(
    () =>
      (session?.turns ?? []).reduce((n, t) => n + (t.usage?.total_tokens ?? 0), 0),
    [session],
  );

  // The last send whose outcome is UNKNOWN (the request failed without a server answer): sending
  // the same text again reuses its turn id, so if the server did take it the retry is a no-op
  // instead of a second turn (Hermes on #1219). The pane is keyed per conversation, so this never
  // crosses sessions.
  const unsure = useRef<{ text: string; turn_id: string } | null>(null);

  const send = async () => {
    const text = draft.trim();
    if (!text || busy || inFlight) return;
    const turn_id = unsure.current?.text === text ? unsure.current.turn_id : newTurnId();
    setBusy(true);
    setSendError(null);
    // Optimistic: the turn shows as pending at once; the server's read replaces it.
    setSession((s) =>
      s
        ? {
            ...s,
            in_flight: turn_id,
            turns: [
              ...s.turns,
              {
                turn_id,
                text,
                ts: Date.now() / 1000,
                status: "pending",
                reason: null,
                reply: null,
                reply_ts: null,
                usage: null,
                truncated: false,
                dropped: 0,
              },
            ],
          }
        : s,
    );
    setDraft("");
    try {
      await api.chatSend(sid, turn_id, text);
      unsure.current = null;
      await load();
    } catch (e) {
      // A 4xx is the server's answer: it did not take this turn. Anything else (no response, a
      // gateway error) is ambiguous — it may have been accepted. Either way the transcript is the
      // judge: a turn that is there was accepted, and its text is not put back in the box.
      const definite = e instanceof ApiError && e.status < 500;
      const s = await load();
      if (s?.turns.some((t) => t.turn_id === turn_id)) {
        unsure.current = null;
      } else {
        setDraft(text); // nothing was lost: the message is back in the box
        unsure.current = definite ? null : { text, turn_id };
        setSendError(
          definite && e.message
            ? e.message
            : "The message may not have reached BattleLab. Sending it again is safe — it will not be duplicated.",
        );
      }
    } finally {
      setBusy(false);
    }
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
      void send();
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
      <div className={styles.log} ref={logRef} aria-live="polite">
        {session && turns.length === 0 && (
          <div className={styles.center} data-testid="chat-empty">
            <p className={styles.note}>
              No terminal and no CLI: BattleLab sends your messages to the endpoint configured for
              this agent and keeps the conversation itself.{" "}
              {endpoint?.tools === "read"
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
          void send();
        }}
      >
        <textarea
          className={styles.ta}
          aria-label="Message the agent"
          placeholder={configured ? "Message the agent…" : "This agent has no endpoint"}
          value={draft}
          onChange={(e) => setDraft(e.target.value)}
          onKeyDown={onKey}
          // Read-only while a send settles: a refusal puts the message back, and there must be
          // no newer typing for it to overwrite (Hermes on #1219).
          readOnly={busy}
          rows={2}
          disabled={!configured}
        />
        <button
          type="submit"
          className={styles.send}
          disabled={!configured || busy || Boolean(inFlight) || !draft.trim()}
        >
          Send
        </button>
      </form>
    </div>
  );
}
