/** ASK — `find` / `history` against `/api/pulse/ask/stream` (#878, streamed since #1171). Since
 * #1294 it lives in the right-hand sidebar (`AskSidebar`) that the corner icon opens on every route;
 * before that it was a page under Dashboard (#1058, #1171).
 *
 * It used to be the second mode of the mission composer, behind a `NEW MISSION | ASK` segmented
 * control on the mission landing. That put a question about *sessions* behind a section about
 * *missions*, and made it unlinkable: there was no URL that opened it. The transport, the payload,
 * the answer shape and the match rows are unchanged by that move — only where it lives.
 *
 * **The conversation is durable (#1443).** Each one is kept on the server (`/api/ask/*`), and a
 * turn is the SERVER's: it runs as a task there, so closing the panel, navigating, reloading or a
 * dropped connection never cancels it. A stream that ends early is a statement about the
 * connection, so the console reads the conversation again and, while that turn is still
 * `pending`, polls until it settles. On open the panel reopens the latest conversation; the head's
 * switcher (`AskSwitcher`) opens an older one, and New conversation starts an empty thread whose
 * conversation is created on its first question. Nothing is ever deleted from here — retention
 * (30 days, 50 conversations) is the server's. `/api/pulse/ask[/stream]` stays the transient,
 * read-only seam for its other callers (#1397).
 *
 * Every late result is checked against the conversation it belongs to (`cidRef`), so an answer
 * for a conversation the operator has switched away from never lands in the one on screen. `live`
 * is hygiene — it keeps a late callback from setting state on a dead component — and it must be
 * set on the EFFECT RUN, not only cleared on cleanup: StrictMode runs an effect, cleans it up and
 * runs it again on the SAME fiber, and `useRef(true)` initialises once.
 *
 * **Missions are answers too (#1069).** The server may name missions beside sessions; they arrive as
 * `mission_matches` and render as their own group above the sessions, each opening the mission
 * through `missionLink` — the one deep link the console shape-checks.
 *
 * **A matched session opens two ways.** "Open" is the full-screen route, as before. "Open in map"
 * asks the map's window workspace for a floating window and goes to the map — the same request
 * the sidebar row and "To map" make (`requestOpen`, #936) — and an answer naming several sessions
 * offers "Open all in map". The drain hands a refused request back to the full-screen route, which
 * is right for one window and loses the tail of a batch, so a batch is held to what the map is
 * KNOWN to admit: no more windows than the cap still has room for (`room`, which counts a stored
 * layout not yet restored), and only once a map measurement said it can host at all (`hostable`
 * true — "never measured" is enough for one window, whose refusal has a home, not for several).
 * Window mode is offered only where it exists — never on a phone, never once the map has measured
 * too small to host one.
 *
 * **It is laid out like the mission thread, because it IS the same kind of surface (#1069).** A chat
 * column: a scrolling thread that grows UP from a composer docked on the bottom edge
 * (`.threadCol` / `.pane` / `.paneAtBottom` / `.composerDock`, the #942 layout), each question and
 * answer an `article` named for its speaker ("You" / "Answer", the mission thread's `MessageRow`
 * words). Before the first question the thread is empty, so a short greeting sits centred in it;
 * it gives way to the conversation, the way a chat does.
 *
 * **Its chrome is the switcher, a way to start over and a way to close (#1171, #1294, #1443).** The
 * head carries the conversation switcher, New conversation and the sidebar's ✕ — nothing else. NEEDS YOU still reaches the thread: an answer row for a session on that list
 * carries the marker and ⓘ.
 *
 * **The wait is shown (#1171).** The server streams the turn's steps — the model reading, the read
 * tool it is calling, which catalog a search covers and how many transcripts it checks — and the
 * pending turn names the step it is on under a moving scan bar. Text is revealed progressively —
 * instantly under `prefers-reduced-motion`.
 *
 * The composer box, the turn rows and the match rows are the console's own primitives
 * (`mission.module.css`) rather than a second copy of them — the same cross-directory import the
 * composer already made for the session pane's Send (`terminal/Compose.module.css`). One box, drawn
 * one way, wherever it appears.
 */
import { Info, X } from "lucide-react";
import {
  useCallback,
  useEffect,
  useLayoutEffect,
  useRef,
  useState,
  type RefObject,
} from "react";

import { Link, useNavigate } from "react-router-dom";

import { MAP_PATH, useMapWindows } from "../../app/workspaceWindows";
import { api } from "../../lib/api";
import { useIsMobile } from "../../lib/useIsMobile";
import { missionLink } from "../../lib/missionLink";
import { settingsPath } from "../../routes/settingsTabs";
import type {
  AskConversationSummary,
  AskConversationTurn,
  AskStoredMatch,
  AskStoredMissionMatch,
  AskTurnEvent,
} from "../../types/api";

import styles from "../pulse/mission.module.css";
import { AskComposer } from "./AskComposer";
import { mapBatch, matchRoute, matchSeed } from "./askMatches";
import a from "./AskConsole.module.css";
import type { AskStep } from "./askStep";
import { AskSwitcher } from "./AskSwitcher";
import { AskWorking } from "./AskWorking";
import { RevealText } from "./RevealText";

export interface AskTurn {
  /** The server's `turn_id`. */
  id: string;
  question: string;
  answer: string | null;
  error: string | null;
  /** The sessions the answer is ABOUT, each with the reason it matched. Without these an answer
   *  naming a session gives the operator no way to reach it. */
  matches: AskStoredMatch[];
  /** The missions the answer is about (#1069), each with why it matched. */
  missions: AskStoredMissionMatch[];
  /** The step the turn is on while it runs (#1171); `null` once it has settled. */
  step: AskStep | null;
  /** Still running on the server. */
  pending: boolean;
  /** Its answer arrived while this page watched — the only answer that writes itself out. One read
   *  back from the server is history and shows at once. */
  live?: boolean;
}

/** How often a turn this page is not streaming is read again until it settles. */
const POLL_MS = 2000;

function fromServer(t: AskConversationTurn): AskTurn {
  return {
    id: t.turn_id,
    question: t.question,
    answer: t.answer,
    error:
      t.status === "failed"
        ? t.detail || "That didn't work."
        : null,
    matches: t.matches ?? [],
    missions: t.mission_matches ?? [],
    step: null,
    pending: t.status === "pending",
  };
}

/** Fold one streamed progress line into its turn. */
function applyEvent(t: AskTurn, ev: AskTurnEvent): AskTurn {
  if (ev.type !== "progress") return t;
  if (ev.step === "catalog")
    return { ...t, step: { step: "catalog", sessions: ev.sessions, missions: ev.missions } };
  if (ev.step === "content") return { ...t, step: { step: "content", candidates: ev.candidates } };
  if (ev.step === "tool") return { ...t, step: { step: "tool", tool: ev.tool } };
  return { ...t, step: { step: "thinking", round: ev.round } };
}

/** The server's turns, plus any turn this page submitted that the server does not have yet (its
 *  conversation is still being created, or the read was taken before it was stored). */
function merge(server: AskTurn[], local: AskTurn[]): AskTurn[] {
  const known = new Set(server.map((t) => t.id));
  return [...server, ...local.filter((t) => !known.has(t.id) && t.pending && !t.error)];
}

interface Gate {
  promise: Promise<void>;
  settled: boolean;
  open: () => void;
}

/** A one-shot gate: `promise` resolves, and `settled` turns true, on the first `open()`. */
function gate(): Gate {
  let resolve!: () => void;
  const g: Gate = {
    promise: new Promise<void>((r) => (resolve = r)),
    settled: false,
    open: () => {
      g.settled = true;
      resolve();
    },
  };
  return g;
}

function newTurnId(): string {
  return crypto.randomUUID();
}

export function AskConsole({
  configured,
  needsYou,
  onDetails,
  onClose,
  closeRef,
}: {
  /** False when no AI endpoint is configured. Ask has no local fallback, so the control is
   *  disabled and says why. */
  configured: boolean;
  /** Sessions currently on the NEEDS YOU list: an answer row for one of them carries the marker
   *  and ⓘ, which opens the same details the list does (#1086). */
  needsYou?: Set<string>;
  onDetails?: (sessionId: string) => void;
  /** The sidebar's ✕ (#1294). */
  onClose?: () => void;
  /** The ✕ itself: the mobile drawer moves focus to it on open, as the bell's drawer does. */
  closeRef?: RefObject<HTMLButtonElement | null>;
}) {
  const [turns, setTurns] = useState<AskTurn[]>([]);
  const [cid, setCid] = useState<string | null>(null);
  const [title, setTitle] = useState("");
  const [conversations, setConversations] = useState<AskConversationSummary[] | null>(null);
  const navigate = useNavigate();
  const isMobile = useIsMobile();
  const map = useMapWindows();
  // Window mode where it exists: the map provider is mounted, this is not a phone, and the map
  // has not measured too small to host one (`null` = never measured, treated as available — the
  // drain's hand-back covers a wrong guess, as for the pane's "To map").
  const canMap = Boolean(map) && !isMobile && map?.hostable !== false;
  const openInMap = useCallback(
    (list: AskStoredMatch[]) => {
      if (!map || !list.length) return;
      for (const m of list) map.requestOpen(matchSeed(m));
      navigate(MAP_PATH);
    },
    [map, navigate],
  );
  const [streaming, setStreaming] = useState(false);
  const inputRef = useRef<HTMLTextAreaElement | null>(null);
  /** False once this console has unmounted (module note). */
  const live = useRef(true);
  useEffect(() => {
    live.current = true;
    return () => {
      live.current = false;
    };
  }, []);
  /** Every read of a conversation, and every send, takes a new generation; a read applies only if
   *  no newer one started since. So a slow read never overwrites a turn submitted after it was
   *  sent, and A→B→A never lets the first A read land over the second. */
  const gen = useRef(0);
  /** The first selection (reopen the latest conversation, or none). A question asked before it
   *  settles is shown at once and WAITS for it, then goes to the conversation it opened (or a new
   *  one) — the latest conversation opening is never allowed to sweep the question away. */
  const selectedRef = useRef<Gate | null>(null);
  if (selectedRef.current === null) selectedRef.current = gate();
  /** The conversation whose read last landed. A send into one whose read was fenced off re-reads
   *  it after settling, so its earlier turns are shown. */
  const loadedFor = useRef<string | null>(null);
  /** The conversation on screen — read at RESOLUTION time, never captured. */
  const cidRef = useRef<string | null>(null);
  const current = (id: string | null) => live.current && cidRef.current === id;

  /** The stream being watched. Aborting it stops WATCHING; the server's turn goes on. */
  const inflight = useRef<AbortController | null>(null);
  useEffect(
    () => () =>
      queueMicrotask(() => {
        if (!live.current) inflight.current?.abort();
      }),
    [],
  );
  const stopWatching = () => {
    inflight.current?.abort();
    inflight.current = null;
    setStreaming(false);
  };

  const refreshList = useCallback(async () => {
    try {
      const r = await api.askConversations();
      if (live.current) setConversations(r.conversations ?? []);
    } catch {
      /* the switcher keeps what it had */
    }
  }, []);

  /** Show conversation `id` as the server has it. */
  const load = useCallback(async (id: string) => {
    const g = ++gen.current;
    const fresh = () => current(id) && g === gen.current;
    try {
      const c = await api.askConversation(id);
      if (!fresh()) return;
      loadedFor.current = id;
      setTitle(c.title ?? "");
      setTurns((prev) => merge((c.turns ?? []).map(fromServer), prev));
    } catch {
      // Keep what is shown; a fresh array re-arms the poll of a still-pending turn.
      if (fresh()) setTurns((prev) => [...prev]);
    }
    // `current` reads refs only.
  }, []);

  /** `initial`: the first selection, which may land under a question already asked — so it
   *  neither stops that question's watch nor clears it from the thread. */
  const openConversation = useCallback(
    (id: string, initial = false) => {
      if (!initial) stopWatching();
      cidRef.current = id;
      setCid(id);
      setTitle("");
      setTurns((prev) => (initial ? prev.filter((t) => t.pending && !t.error) : []));
      void load(id);
    },
    [load],
  );

  // On first mount: the latest conversation, if there is one.
  useEffect(() => {
    let cancelled = false;
    void (async () => {
      try {
        const r = await api.askConversations();
        if (cancelled || !live.current) return;
        const rows = r.conversations ?? [];
        setConversations(rows);
        if (rows.length && cidRef.current === null) openConversation(rows[0].id, true);
      } catch {
        if (!cancelled && live.current) setConversations([]);
      } finally {
        if (!cancelled) selectedRef.current?.open();
      }
    })();
    return () => {
      cancelled = true;
    };
  }, [openConversation]);

  // A turn still running on the server that this page is not streaming (a reload, a dropped
  // connection, another tab) is read again until it settles.
  const waiting = !streaming && turns.some((t) => t.pending);
  useEffect(() => {
    if (!waiting || !cid) return;
    const timer = window.setTimeout(() => void load(cid), POLL_MS);
    return () => window.clearTimeout(timer);
  }, [waiting, cid, turns, load]);

  const busy = streaming || turns.some((t) => t.pending);

  const ask = useCallback(
    async (q: string) => {
      if (!q || busy || !configured) return;
      const turnId = newTurnId();
      gen.current++; // a read already in flight must not land over this turn
      const ctl = new AbortController();
      inflight.current = ctl;
      setStreaming(true);
      const optimistic: AskTurn = {
        id: turnId,
        question: q,
        answer: null,
        error: null,
        matches: [],
        missions: [],
        step: null,
        pending: true,
      };
      setTurns((prev) => [...prev, optimistic]);
      const update = (fn: (t: AskTurn) => AskTurn) =>
        setTurns((prev) => prev.map((t) => (t.id === turnId ? fn(t) : t)));
      let id: string | null = null;
      try {
        const sel = selectedRef.current;
        if (sel && !sel.settled) {
          await sel.promise;
          if (!live.current || ctl.signal.aborted) return;
        }
        id = cidRef.current;
        if (id === null) {
          // A new conversation is created by its first question, never by New conversation.
          const c = await api.askNewConversation();
          if (!live.current || ctl.signal.aborted) return;
          if (!c?.id) throw new Error("Could not start a conversation — ask again.");
          id = c.id;
          cidRef.current = id;
          loadedFor.current = id; // created empty: nothing earlier to read
          setCid(id);
        }
        const settled = await api.askMessageStream(
          id,
          { turnId, text: q },
          (ev) => {
            if (current(id)) update((t) => applyEvent(t, ev));
          },
          ctl.signal,
        );
        // A watch this page stopped (New conversation, a switch) applies nothing, however late.
        if (ctl.signal.aborted) return;
        if (current(id)) {
          const done: AskTurn = { ...fromServer(settled), live: true };
          // Upsert: whatever a read did meanwhile, the settled turn is shown.
          setTurns((prev) =>
            prev.some((t) => t.id === turnId)
              ? prev.map((t) => (t.id === turnId ? done : t))
              : [...prev, done],
          );
          if (!title) setTitle(settled.question);
          if (loadedFor.current !== id) void load(id);
        }
        void refreshList();
      } catch (err) {
        if (!live.current || ctl.signal.aborted) return;
        const msg = err instanceof Error ? err.message : "That didn't work.";
        if (id !== null && current(id)) {
          // The turn may be stored and still running — ask the server what became of it.
          const g = ++gen.current;
          try {
            const c = await api.askConversation(id);
            if (!current(id) || g !== gen.current) return;
            if ((c.turns ?? []).some((t) => t.turn_id === turnId)) {
              setTurns((prev) => merge(c.turns.map(fromServer), prev));
              return;
            }
          } catch {
            /* fall through: say what failed */
          }
        }
        if (current(id)) update((t) => ({ ...t, error: msg, step: null, pending: false }));
      } finally {
        if (inflight.current === ctl) {
          inflight.current = null;
          setStreaming(false);
        }
      }
    },
    // `current` reads refs only.
    [busy, configured, load, refreshList, title],
  );

  /** The newest turn is where the operator is looking: keep the thread scrolled to it, as a chat
   *  does, whenever a turn is added or answered. */
  const paneRef = useRef<HTMLDivElement | null>(null);
  useLayoutEffect(() => {
    const el = paneRef.current;
    if (el && turns.length) el.scrollTop = el.scrollHeight;
  }, [turns]);

  return (
    <div className={`${styles.threadCol} ${a.col}`} data-testid="ask-col">
      <header className={`${a.measure} ${a.head}`} data-testid="ask-head">
        <h2 className={a.title}>Ask</h2>
        <AskSwitcher
          conversations={conversations}
          currentId={cid}
          currentTitle={title || (turns[0]?.question ?? "")}
          onOpen={() => void refreshList()}
          onPick={(id) => {
            if (id !== cidRef.current) openConversation(id);
          }}
        />
        <button
          type="button"
          className={a.newBtn}
          aria-label="New conversation"
          disabled={turns.length === 0}
          onClick={() => {
            stopWatching();
            cidRef.current = null;
            setCid(null);
            setTitle("");
            setTurns([]);
            inputRef.current?.focus();
          }}
          data-testid="ask-new"
        >
          <span className={a.long}>New conversation</span>
          <span className={a.short}>New</span>
        </button>
        {onClose ? (
          <button
            ref={closeRef}
            type="button"
            className={a.back}
            aria-label="Close Ask"
            onClick={onClose}
            data-testid="ask-close"
          >
            <X size={18} aria-hidden="true" />
          </button>
        ) : null}
      </header>
      <div className={styles.pane} ref={paneRef} data-testid="ask-pane">
        {turns.length === 0 ? (
          <div className={`${a.measure} ${a.intro}`} data-testid="ask-empty">
            <p className={a.greeting}>
              Ask about anything your sessions and missions hold — answers are
              read from the transcripts this install can already see.
            </p>
            {!configured ? (
              <p className={a.needsEndpoint} data-testid="ask-needs-endpoint">
                Ask needs an AI endpoint — it has no local fallback. Set one up
                in{" "}
                <Link to={settingsPath("ai-endpoint")}>
                  Settings → Endpoint &amp; model
                </Link>
                , then come back.
              </p>
            ) : null}
          </div>
        ) : (
          <div
            className={`${a.measure} ${styles.paneAtBottom}`}
            data-testid="ask-turns"
          >
            {turns.map((t) => (
              <div key={t.id} data-testid="ask-turn">
                <article
                  className={`${styles.event} ${a.you}`}
                  aria-label="You"
                >
                  <div className={`${styles.eventHead} ${a.label}`}>You</div>
                  <div className={`${styles.eventText} ${a.text}`}>{t.question}</div>
                </article>
                <article
                  className={styles.event}
                  aria-label="Answer"
                  aria-busy={t.pending}
                >
                  <div className={`${styles.eventHead} ${a.label}`}>Answer</div>
                  {t.answer !== null ? (
                    <>
                      <RevealText
                        className={`${styles.eventText} ${a.text}`}
                        text={t.answer}
                        testId="ask-answer"
                        instant={!t.live}
                      />
                      {/* Missions first, then sessions — each group labelled only when there
                          are missions, so a sessions-only answer reads as it did before #1069. */}
                      {t.missions.length > 0 ? (
                        <>
                          <div className={`${styles.eventHead} ${a.label} ${a.group}`}>
                            Missions
                          </div>
                          {t.missions.map((m) => (
                            <div
                              key={m.id}
                              className={`${styles.matchRow} ${a.arrive}`}
                              data-testid="ask-mission-match"
                            >
                              <div className={styles.matchBody}>
                                <div className={`${styles.eventText} ${a.text}`}>{m.title}</div>
                                {m.why ? (
                                  <div className={`${styles.objReason} ${a.reason}`}>{m.why}</div>
                                ) : null}
                              </div>
                              <Link
                                className={styles.openSession}
                                to={missionLink(m.id)}
                                aria-label={`Open mission ${m.title}`}
                              >
                                Open mission
                              </Link>
                            </div>
                          ))}
                          {t.matches.length > 0 ? (
                            <div className={`${styles.eventHead} ${a.label} ${a.group}`}>
                              Sessions
                            </div>
                          ) : null}
                        </>
                      ) : null}
                      {/* The matched sessions, each with why it matched and a way in. An answer
                          that names a session the operator cannot reach is half an answer. */}
                      {t.matches.map((m) => (
                        <div
                          key={m.id}
                          className={`${styles.matchRow} ${a.arrive}`}
                          data-testid="ask-match"
                        >
                          <div
                            className={`${styles.matchBody}${canMap ? ` ${a.matchBody}` : ""}`}
                          >
                            <div className={`${styles.eventText} ${a.text}`}>
                              {m.title}
                              {needsYou?.has(m.id) ? (
                                <span className={a.needsTag} data-testid="ask-match-needs-you">
                                  <span className={a.needsDot} aria-hidden="true" />
                                  Needs you
                                </span>
                              ) : null}
                            </div>
                            {m.why ? (
                              <div className={`${styles.objReason} ${a.reason}`}>{m.why}</div>
                            ) : null}
                          </div>
                          {needsYou?.has(m.id) && onDetails ? (
                            <button
                              type="button"
                              className={a.detailsBtn}
                              aria-label={`Details for ${m.title}`}
                              onClick={() => onDetails(m.id)}
                            >
                              <Info size={16} aria-hidden="true" />
                            </button>
                          ) : null}
                          <span className={a.opens} data-testid="ask-match-opens">
                            <Link
                              className={styles.openSession}
                              to={matchRoute(m.id)}
                              aria-label={`Open ${m.title}`}
                              data-testid="ask-match-open"
                            >
                              Open
                            </Link>
                            {canMap ? (
                              <button
                                type="button"
                                className={`${styles.openSession} ${a.mapBtn}`}
                                aria-label={`Open ${m.title} in map`}
                                title={
                                  map?.openKeys.has(m.id)
                                    ? "Already open on the map — show its window"
                                    : map && map.room < 1
                                      ? "The map's window limit is reached — close a window or raise the limit"
                                      : "Open as a floating window on the map"
                                }
                                disabled={
                                  !map?.openKeys.has(m.id) && (map?.room ?? 0) < 1
                                }
                                onClick={() => openInMap([m])}
                                data-testid="ask-match-map"
                              >
                                Open in map
                              </button>
                            ) : null}
                          </span>
                        </div>
                      ))}
                      {canMap && map && map.hostable === true && t.matches.length > 1
                        ? (() => {
                            const { take, left } = mapBatch(
                              t.matches,
                              map.openKeys,
                              map.room,
                            );
                            return (
                              <div className={a.batch}>
                                <button
                                  type="button"
                                  className={`${styles.openSession} ${a.mapBtn}`}
                                  disabled={take.length === 0}
                                  onClick={() => openInMap(take)}
                                  title={
                                    left > 0
                                      ? `The map's window limit leaves room for ${take.length} of these ${t.matches.length} — close a window or raise the limit for the rest`
                                      : `Open all ${t.matches.length} as floating windows on the map`
                                  }
                                  data-testid="ask-match-map-all"
                                >
                                  {left > 0
                                    ? `Open ${take.length} of ${t.matches.length} in map`
                                    : `Open all ${t.matches.length} in map`}
                                </button>
                              </div>
                            );
                          })()
                        : null}
                    </>
                  ) : t.pending ? (
                    <AskWorking step={t.step} />
                  ) : null}
                  {t.error ? (
                    <div className={styles.objStale} data-testid="ask-error">
                      {t.error}
                    </div>
                  ) : null}
                </article>
              </div>
            ))}
          </div>
        )}
      </div>
      {/* THE COMPOSER, DOCKED — the mission thread's #942 dock, on the bottom edge at every
          height, under the same centred measure as the thread. */}
      <div className={styles.composerDock}>
        <div className={a.measure}>
          <AskComposer
            configured={configured}
            busy={busy}
            onAsk={(q) => void ask(q)}
            inputRef={inputRef}
          />
        </div>
      </div>
    </div>
  );
}
