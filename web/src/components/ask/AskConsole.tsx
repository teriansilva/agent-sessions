/** ASK — `find` / `history` against `/api/pulse/ask` (#878), on its own page since #1058.
 *
 * It used to be the second mode of the mission composer, behind a `NEW MISSION | ASK` segmented
 * control on the mission landing. That put a question about *sessions* behind a section about
 * *missions*, and made it unlinkable: there was no URL that opened it. The transport, the payload,
 * the answer shape, the match rows and the "Jump in" links are all unchanged — only where it lives.
 *
 * **The turns are still transient, and that is a statement about the surface rather than unfinished
 * work.** A mission's turns ARE durable: they go to `POST /api/missions/{id}/message` and live in
 * that mission's timeline (`MissionComposer`, #890). This page has no mission to keep a turn in, so
 * the honest answer is to say so on screen rather than to invent a home for it. Creating a mission
 * is how an operator makes a conversation durable.
 *
 * **A completion whose page is gone is DISCARDED, not filed** — the #878 contract, and on a route
 * it is STRUCTURAL rather than enforced. In the composer it needed a two-part fence: a key on the
 * mission (so a switch unmounted the box) plus a `visit()` token compared at resolution time,
 * because switching missions, or flipping Active → Archived, did not necessarily unmount anything.
 * Here leaving the page unmounts it, and the turns are this component's own state, so they go with
 * it. That is the guarantee; say it plainly rather than dressing a ref up as the thing that holds
 * it. Nothing retains these turns above the router — deliberately (#1058), because that would make
 * them durable, which is a promise the server does not keep.
 *
 * `live` is therefore HYGIENE, not the fence: `api.pulseAsk` takes no abort signal (an
 * `AbortController` here would abort nothing while reading as though it did), so the request keeps
 * running after unmount and its callbacks would otherwise set state on a dead component.
 *
 * **It must be set on the EFFECT RUN, not only cleared on cleanup.** React StrictMode runs an
 * effect, cleans it up and runs it again on the SAME fiber, and `useRef(true)` initialises exactly
 * once — so a cleanup-only version leaves `live.current` false for the rest of the component's life
 * and silently drops every answer in development. `AskConsole.test.tsx` covers that case
 * explicitly; it is red without the assignment below.
 *
 * The composer box, the turn rows and the match rows are the console's own primitives
 * (`mission.module.css`) rather than a second copy of them — the same cross-directory import the
 * composer already made for the session pane's Send (`terminal/Compose.module.css`). One box, drawn
 * one way, wherever it appears.
 */
import { Send } from "lucide-react";
import { useCallback, useEffect, useRef, useState } from "react";

import { Link } from "react-router-dom";

import { api } from "../../lib/api";
import type { PulseAskMatch } from "../../types/api";

import compose from "../terminal/Compose.module.css";
import styles from "../pulse/mission.module.css";

export interface AskTurn {
  id: number;
  question: string;
  answer: string | null;
  error: string | null;
  /** The sessions the answer is ABOUT, each with the reason it matched. Without these an answer
   *  naming a session gives the operator no way to reach it — the Ask box rendered them and this
   *  page must too. */
  matches: PulseAskMatch[];
}

let nextTurnId = 1;

/** `engine:uuid` → `/s/:engine/:uuid`, both halves encoded. */
function matchRoute(key: string): string {
  const i = key.indexOf(":");
  const engine = i < 0 ? key : key.slice(0, i);
  const uuid = i < 0 ? "" : key.slice(i + 1);
  return `/s/${encodeURIComponent(engine)}/${encodeURIComponent(uuid)}`;
}

export function AskConsole({
  configured,
}: {
  /** False when no AI endpoint is configured. `/api/pulse/ask` answers 409 in that case and has
   *  no local fallback, so the control is disabled and says why — `find` / `history` genuinely
   *  do not work without a model. */
  configured: boolean;
}) {
  const [turns, setTurns] = useState<AskTurn[]>([]);
  const [text, setText] = useState("");
  const [busy, setBusy] = useState(false);
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

  const submit = useCallback(
    async (e: React.FormEvent) => {
      e.preventDefault();
      const q = text.trim();
      if (!q || busy || !configured) return;
      const id = nextTurnId++;
      setTurns((prev) => [
        ...prev,
        { id, question: q, answer: null, error: null, matches: [] },
      ]);
      setText("");
      setBusy(true);
      try {
        // Prior turns of THIS page only, and only the ones that actually answered.
        const history = turns.flatMap((t) =>
          t.answer !== null
            ? [
                { role: "user" as const, content: t.question },
                { role: "assistant" as const, content: t.answer },
              ]
            : [],
        );
        const r = await api.pulseAsk(q, history);
        // The operator left: discard, and take the pending turn with it.
        if (!live.current) return;
        setTurns((prev) =>
          prev.map((t) =>
            t.id === id
              ? { ...t, answer: r.answer ?? "", matches: r.matches ?? [] }
              : t,
          ),
        );
      } catch (err) {
        if (!live.current) return;
        const msg = err instanceof Error ? err.message : "That didn't work.";
        setTurns((prev) =>
          prev.map((t) => (t.id === id ? { ...t, error: msg } : t)),
        );
      } finally {
        // Safe unconditionally: after unmount this is a no-op on a dead component, not a write
        // into another surface.
        setBusy(false);
      }
    },
    [text, busy, configured, turns],
  );

  return (
    <>
      <form
        className={styles.composerBox}
        onSubmit={submit}
        data-testid="ask-form"
      >
        <textarea
          className={`${styles.composerInput} ${styles.boxInput}`}
          rows={1}
          value={text}
          onChange={(e) => setText(e.target.value)}
          // Enter is a newline — a question can run to several lines. Ctrl/⌘+Enter sends it,
          // the same shortcut the mission brief uses.
          onKeyDown={(e) => {
            if (e.key === "Enter" && (e.ctrlKey || e.metaKey)) {
              e.preventDefault();
              e.currentTarget.form?.requestSubmit();
            }
          }}
          aria-keyshortcuts="Control+Enter Meta+Enter"
          disabled={!configured}
          placeholder={
            configured
              ? "Ask about your work — find, history…"
              : "Needs an AI endpoint"
          }
          aria-label="Ask about your past work"
          data-testid="composer-input"
        />
        <div className={styles.composerFoot} data-testid="composer-foot">
          <div className={styles.footLead}>
            {/* The shortcut, for the eye. The textarea's `aria-keyshortcuts` is its accessible
                form. */}
            <span className={styles.footHint} aria-hidden="true">
              Ctrl + Enter
            </span>
          </div>
          <div className={styles.footTrail}>
            <span className={styles.footSpacer} aria-hidden="true" />
            {/* THE SESSION PANE'S SEND (#967), its class and its icon — identical by construction,
                so this page, the mission thread and a session cannot draw three different Sends. */}
            <button
              type="submit"
              className={`${compose.send} shine`}
              disabled={!configured || busy || !text.trim()}
              data-testid="composer-send"
            >
              <Send size={15} aria-hidden="true" />
              Send
            </button>
          </div>
        </div>
      </form>

      {turns.length > 0 ? (
        <div data-testid="ask-turns">
          {turns.map((t) => (
            <div key={t.id} className={styles.event} data-testid="ask-turn">
              <div className={styles.eventHead}>You</div>
              <div className={styles.eventText}>{t.question}</div>
              {t.answer !== null ? (
                <>
                  <div className={styles.eventHead} style={{ marginTop: 6 }}>
                    Answer
                  </div>
                  <div className={styles.eventText}>{t.answer}</div>
                  {/* The matched sessions, each with why it matched and a way in. An answer that
                      names a session the operator cannot reach is half an answer. */}
                  {t.matches.map((m) => (
                    <div
                      key={m.id}
                      className={styles.matchRow}
                      data-testid="ask-match"
                    >
                      <div className={styles.matchBody}>
                        <div className={styles.eventText}>{m.title}</div>
                        {m.why ? (
                          <div className={styles.objReason}>{m.why}</div>
                        ) : null}
                      </div>
                      <Link
                        className={styles.openSession}
                        to={matchRoute(m.id)}
                        aria-label={`Jump into ${m.title}`}
                      >
                        Jump in
                      </Link>
                    </div>
                  ))}
                </>
              ) : t.error ? (
                <div className={styles.objStale} data-testid="ask-error">
                  {t.error}
                </div>
              ) : (
                <div className={styles.objReason}>…</div>
              )}
            </div>
          ))}
          <div className={styles.objReason} data-testid="ask-transient">
            These answers are not kept — this page has no mission to keep them
            in, so they disappear when you leave. A mission's own conversation
            is saved.
          </div>
        </div>
      ) : null}
    </>
  );
}
