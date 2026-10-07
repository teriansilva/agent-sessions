/** ASK — `find` / `history` against `/api/pulse/ask/stream` (#878, streamed since #1171). Since
 * #1294 it lives in the right-hand sidebar (`AskSidebar`) that the corner icon opens on every route;
 * before that it was a page under Dashboard (#1058, #1171).
 *
 * It used to be the second mode of the mission composer, behind a `NEW MISSION | ASK` segmented
 * control on the mission landing. That put a question about *sessions* behind a section about
 * *missions*, and made it unlinkable: there was no URL that opened it. The transport, the payload,
 * the answer shape and the match rows are unchanged by that move — only where it lives.
 *
 * **The turns are still transient, and that is a statement about the surface rather than unfinished
 * work.** A mission's turns ARE durable: they go to `POST /api/missions/{id}/message` and live in
 * that mission's timeline (`MissionComposer`, #890). This page has no mission to keep a turn in.
 * Creating a mission is how an operator makes a conversation durable. The on-screen notice that
 * said so under every thread was removed at the operator's request: it was read once and then
 * cost a line of every conversation.
 *
 * **The turns are this component's own state, and the sidebar keeps the component mounted** (#1294):
 * a conversation outlives closing the panel and navigating, because the operator asked for Ask to
 * sit beside the work. It is still memory only — a reload or New conversation ends it. It is
 * never written anywhere, so it is not durable, which is a
 * promise the server does not keep.
 *
 * `live` is therefore HYGIENE, not the fence: it keeps a late callback from setting state on a
 * dead component. Since #1171 the request is also ABORTED on unmount and on New conversation —
 * the stream takes a signal, and closing it ends the server's run and frees its one-question gate,
 * so the next question is not refused with "a question is already running".
 *
 * **It must be set on the EFFECT RUN, not only cleared on cleanup.** React StrictMode runs an
 * effect, cleans it up and runs it again on the SAME fiber, and `useRef(true)` initialises exactly
 * once — so a cleanup-only version leaves `live.current` false for the rest of the component's life
 * and silently drops every answer in development. `AskConsole.test.tsx` covers that case
 * explicitly; it is red without the assignment below.
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
 * **Its only chrome is a way to start over and a way to close (#1171, #1294).** The head carries
 * New conversation and the sidebar's ✕ — nothing else. NEEDS YOU still reaches the thread: an answer row for a session on that list
 * carries the marker and ⓘ.
 *
 * **The wait is shown, and the answer arrives as it forms (#1171).** The server streams the ask's
 * steps: which catalog it is searching, the Stage-1 answer the moment it exists, then the answer
 * Stage 2 confirmed against the transcripts. The pending turn names the step it is on under a
 * moving scan bar, shows the Stage-1 answer while the transcripts are read, and swaps in the
 * confirmed one. Text is revealed progressively — instantly under `prefers-reduced-motion`.
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
  PulseAskEvent,
  PulseAskMatch,
  PulseAskMissionMatch,
} from "../../types/api";

import styles from "../pulse/mission.module.css";
import { AskComposer } from "./AskComposer";
import { mapBatch, matchRoute, matchSeed } from "./askMatches";
import a from "./AskConsole.module.css";
import type { AskStep } from "./askStep";
import { AskWorking } from "./AskWorking";
import { RevealText } from "./RevealText";

export interface AskTurn {
  id: number;
  question: string;
  answer: string | null;
  error: string | null;
  /** The sessions the answer is ABOUT, each with the reason it matched. Without these an answer
   *  naming a session gives the operator no way to reach it — the Ask box rendered them and this
   *  page must too. */
  matches: PulseAskMatch[];
  /** The missions the answer is about (#1069), each with why it matched. */
  missions: PulseAskMissionMatch[];
  /** The step the ask is on while it runs (#1171); `null` once it has settled. */
  step: AskStep | null;
  /** The answer shown is Stage 1's, still being confirmed against the transcripts (#1171). */
  provisional: boolean;
}

let nextTurnId = 1;

/** Fold one streamed event into its turn. */
function applyEvent(t: AskTurn, ev: PulseAskEvent): AskTurn {
  if (ev.type === "progress") {
    return {
      ...t,
      step:
        ev.step === "catalog"
          ? { step: "catalog", sessions: ev.sessions, missions: ev.missions }
          : { step: "content", candidates: ev.candidates },
    };
  }
  if (ev.type === "answer") {
    return {
      ...t,
      answer: ev.answer ?? "",
      matches: ev.matches ?? [],
      missions: ev.mission_matches ?? [],
      provisional: !ev.final,
      step: ev.final ? null : t.step,
    };
  }
  return t;
}

export function AskConsole({
  configured,
  needsYou,
  onDetails,
  onClose,
  closeRef,
}: {
  /** False when no AI endpoint is configured. `/api/pulse/ask` answers 409 in that case and has
   *  no local fallback, so the control is disabled and says why — `find` / `history` genuinely
   *  do not work without a model. */
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
  const navigate = useNavigate();
  const isMobile = useIsMobile();
  const map = useMapWindows();
  // Window mode where it exists: the map provider is mounted, this is not a phone, and the map
  // has not measured too small to host one (`null` = never measured, treated as available — the
  // drain's hand-back covers a wrong guess, as for the pane's "To map").
  const canMap = Boolean(map) && !isMobile && map?.hostable !== false;
  const openInMap = useCallback(
    (list: PulseAskMatch[]) => {
      if (!map || !list.length) return;
      for (const m of list) map.requestOpen(matchSeed(m));
      navigate(MAP_PATH);
    },
    [map, navigate],
  );
  const [busy, setBusy] = useState(false);
  const inputRef = useRef<HTMLTextAreaElement | null>(null);
  /** False once this page has unmounted. Read at RESOLUTION time, never captured as a value —
   *  a captured boolean answers the question as it was when the request started, which is
   *  exactly the moment that does not matter. See the module note on the StrictMode re-run. */
  const live = useRef(true);
  useEffect(() => {
    live.current = true;
    return () => {
      live.current = false;
    };
  }, []);

  /** The ask in flight, so New conversation and unmounting can end it. The abort on unmount waits
   *  a microtask and re-checks `live`: StrictMode's cleanup-and-rerun sets `live` back to true
   *  before then, so its fake unmount does not kill a question already asked. */
  const inflight = useRef<AbortController | null>(null);
  useEffect(
    () => () =>
      queueMicrotask(() => {
        if (!live.current) inflight.current?.abort();
      }),
    [],
  );

  const ask = useCallback(
    async (q: string) => {
      if (!q || busy || !configured) return;
      const id = nextTurnId++;
      const ctl = new AbortController();
      inflight.current = ctl;
      setTurns((prev) => [
        ...prev,
        {
          id,
          question: q,
          answer: null,
          error: null,
          matches: [],
          missions: [],
          step: null,
          provisional: false,
        },
      ]);
      setBusy(true);
      const update = (fn: (t: AskTurn) => AskTurn) =>
        setTurns((prev) => prev.map((t) => (t.id === id ? fn(t) : t)));
      try {
        // Prior turns of THIS page only, and only the ones that actually answered.
        const history = turns.flatMap((t) =>
          t.answer !== null && !t.provisional
            ? [
                { role: "user" as const, content: t.question },
                { role: "assistant" as const, content: t.answer },
              ]
            : [],
        );
        await api.pulseAskStream(
          q,
          history,
          (ev) => {
            // The operator left: discard, and take the pending turn with it.
            if (live.current) update((t) => applyEvent(t, ev));
          },
          ctl.signal,
        );
        // A stream that ended without its final answer must not leave a turn spinning, or
        // keep a Stage-1 answer labelled as still being checked.
        if (live.current)
          update((t) => ({ ...t, step: null, provisional: false }));
      } catch (err) {
        // Ended on purpose (New conversation / leaving): its turn is already gone.
        if (!live.current || ctl.signal.aborted) return;
        const msg = err instanceof Error ? err.message : "That didn't work.";
        // An answer Stage 1 already gave stays; the failure is said beside it.
        update((t) => ({ ...t, error: msg, step: null, provisional: false }));
      } finally {
        // Only the ask that is still current may clear `busy`: one ended by New conversation
        // finishes AFTER the next question may have started, and must not unlock the box
        // under it.
        if (inflight.current === ctl) {
          inflight.current = null;
          setBusy(false);
        }
      }
    },
    [busy, configured, turns],
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
        <span className={a.sp} />
        <button
          type="button"
          className={a.newBtn}
          aria-label="New conversation"
          disabled={turns.length === 0}
          onClick={() => {
            inflight.current?.abort();
            inflight.current = null;
            setBusy(false);
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
                  aria-busy={t.answer === null && !t.error}
                >
                  <div className={`${styles.eventHead} ${a.label}`}>Answer</div>
                  {t.answer !== null ? (
                    <>
                      <RevealText
                        className={`${styles.eventText} ${a.text}`}
                        text={t.answer}
                        testId="ask-answer"
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
                      {t.provisional ? <AskWorking step={t.step} /> : null}
                    </>
                  ) : t.error ? null : (
                    <AskWorking step={t.step} />
                  )}
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
