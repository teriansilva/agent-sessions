/** The composer — `find` / `history` against the existing `/api/pulse/ask` (#878).
 *
 * **These turns are transient by design, until #871.** `/api/pulse/ask` stores nothing, so there
 * is no mission turn to persist yet; the history here is component-local and says so on screen
 * rather than letting the operator believe a conversation was kept. Do not "fix" that by
 * inventing storage — #871 is specified to provide it.
 *
 * **A reply may never land in a mission that did not ask for it.** The failure is easy to write
 * and invisible in review: select A, ask, switch to B before it resolves, and the late `then`
 * writes A's answer into B's thread. So every request carries the mission it was made for, and
 * any completion whose mission is no longer selected is DISCARDED rather than rendered — answer,
 * error and loading state alike. A spinner inherited by mission B is the same lie in a quieter
 * form.
 *
 * The mechanism has two halves, and both are needed.
 *
 * **Keying** — the console mounts this inside a subtree keyed on the mission, so a switch
 * unmounts it. The draft text and the busy flag go with it, and mission B cannot inherit A's
 * spinner because B's composer is a different instance.
 *
 * **A live-ness fence** — the in-flight request keeps running (`api.pulseAsk` takes no abort
 * signal, so an `AbortController` here would abort nothing while reading as though it did), and
 * its callbacks close over the mission they were made for. `#878` requires a completion whose
 * mission is no longer selected to be **discarded, not filed**: an answer written into a mission
 * the operator has left is state they never asked to keep and will meet later with no context
 * for it. So the callbacks ask the console whether that mission is still the selected one, and
 * drop the result when it is not.
 *
 * An earlier revision kept the answer under A on the reasoning that A's own answer should be
 * waiting when the operator returns. That is a UX preference, and it loses to the stated
 * contract: the safety property is satisfied either way, and where they differ the contract
 * decides. `Composer.test.tsx` asserts NEITHER mission receives the stale completion.
 */
import { useCallback, useState } from "react";

import { Link } from "react-router-dom";

import { api } from "../../lib/api";
import type { PulseAskMatch } from "../../types/api";

import styles from "./mission.module.css";

export interface AskTurn {
  id: number;
  question: string;
  answer: string | null;
  error: string | null;
  /** The sessions the answer is ABOUT, each with the reason it matched. Without these an answer
   *  naming a session gives the operator no way to reach it — the Ask box rendered them and the
   *  console must too. */
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

export function Composer({
  missionId,
  configured,
  onTurns,
  turns,
  isCurrent,
}: {
  /** The mission these turns belong to. Ownership is keyed on it. */
  missionId: string;
  /** False when no AI endpoint is configured. `/api/pulse/ask` answers 409 in that case and has
   *  no local fallback, so the control is disabled and says why — `find` / `history` genuinely
   *  do not work without a model. */
  configured: boolean;
  turns: AskTurn[];
  onTurns: (missionId: string, fn: (prev: AskTurn[]) => AskTurn[]) => void;
  /** Is `missionId` still the selected mission? Read at RESOLUTION time, never captured — that
   *  is the whole point, so it is a function rather than a boolean prop. */
  isCurrent: (missionId: string) => boolean;
}) {
  const [text, setText] = useState("");
  const [busy, setBusy] = useState(false);
  /** The mission currently owning an in-flight request, read inside the late callback. A ref,
   *  not state, because the callback must see the value at RESOLUTION time, not the one captured
   *  when the request started. */

  const submit = useCallback(
    async (e: React.FormEvent) => {
      e.preventDefault();
      const q = text.trim();
      if (!q || busy || !configured) return;
      const asked = missionId; // the mission this turn belongs to, captured once
      const id = nextTurnId++;
      onTurns(asked, (prev) => [
        ...prev,
        { id, question: q, answer: null, error: null, matches: [] },
      ]);
      setText("");
      setBusy(true);
      try {
        // Prior turns of THIS mission only — the history is per-mission for the same reason the
        // answers are.
        const history = turns.flatMap((t) =>
          t.answer !== null
            ? [
                { role: "user" as const, content: t.question },
                { role: "assistant" as const, content: t.answer },
              ]
            : [],
        );
        const r = await api.pulseAsk(q, history);
        // The operator moved on: discard, and take the pending turn with it so no half-finished
        // row is left behind either.
        if (!isCurrent(asked)) {
          onTurns(asked, (prev) => prev.filter((t) => t.id !== id));
          return;
        }
        onTurns(asked, (prev) =>
          prev.map((t) =>
            t.id === id
              ? { ...t, answer: r.answer ?? "", matches: r.matches ?? [] }
              : t,
          ),
        );
      } catch (err) {
        if (!isCurrent(asked)) {
          onTurns(asked, (prev) => prev.filter((t) => t.id !== id));
          return;
        }
        const msg = err instanceof Error ? err.message : "That didn't work.";
        onTurns(asked, (prev) =>
          prev.map((t) => (t.id === id ? { ...t, error: msg } : t)),
        );
      } finally {
        // Safe unconditionally: on a mission switch this instance is already unmounted, so the
        // call is a no-op rather than a write into another mission's composer.
        setBusy(false);
      }
    },
    [text, busy, configured, missionId, onTurns, turns, isCurrent],
  );

  return (
    <>
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
                    <div key={m.id} className={styles.matchRow} data-testid="ask-match">
                      <div className={styles.eventText}>{m.title}</div>
                      {m.why ? <div className={styles.objReason}>{m.why}</div> : null}
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
            These answers are not kept — they disappear when you reload.
          </div>
        </div>
      ) : null}

      <form className={styles.composer} onSubmit={submit}>
        <textarea
          className={styles.composerInput}
          rows={1}
          value={text}
          onChange={(e) => setText(e.target.value)}
          disabled={!configured}
          placeholder={
            configured
              ? "Ask about your work — find, history…"
              : "Needs an AI endpoint"
          }
          aria-label="Ask about your past work"
          data-testid="composer-input"
        />
        <button
          type="submit"
          className={styles.send}
          disabled={!configured || busy || !text.trim()}
          data-testid="composer-send"
        >
          {busy ? "…" : "SEND"}
        </button>
      </form>
    </>
  );
}
