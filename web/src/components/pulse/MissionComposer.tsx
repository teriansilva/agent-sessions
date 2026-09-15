/** The composer, sending DURABLE turns into a mission (#890).
 *
 * #871 built the route — one model execution per `turn_id` across crashes and retries, each
 * produced action delivered at most once, settled forward on every failure path — and nothing
 * called it. The composer kept posting to `/api/pulse/ask`, which stores nothing, so a mission's
 * timeline carried no operator conversation and an operator who asked a question and reloaded
 * lost the answer.
 *
 * **The transcript is the mission timeline, not this component.** The thread renders
 * `operator_msg` / `assistant_msg` events, which the route writes inside its own transactions —
 * the claim writes the message, the settlement writes the answer. So this component owns exactly
 * two things: the draft, and the turn currently in flight. There is deliberately no local history
 * to go stale against the server's.
 *
 * **The `turn_id` is minted ONCE per send and reused on every retry.** That is what makes the
 * idempotency real: a fresh id per attempt turns every retry into a new execution, which is the
 * double-instruct the route exists to prevent. It is kept with the pending turn, not derived at
 * send time.
 *
 * **`indeterminate` is never retried automatically.** The route answers it precisely when nobody
 * can say whether the instruction went out; sending again could be a second copy of an
 * instruction the agent already has. The operator is told that, in those words, and decides.
 *
 * **A reply may never land in a mission that did not ask for it.** Inherited verbatim from #878,
 * and still needed: the request is not abortable, so keying the subtree on the mission is not
 * enough on its own. Liveness is read at RESOLUTION time, never captured at send time — a
 * boolean captured when the request started answers the question as it was at the moment that
 * does not matter.
 */
import { Send } from "lucide-react";
import { useCallback, useEffect, useRef, useState } from "react";

import { api, ApiError } from "../../lib/api";
import type { Mission, MissionOpenTurn, MissionTurn } from "../../types/api";

import compose from "../terminal/Compose.module.css";
import styles from "./mission.module.css";

/** What this component is holding while a send is outstanding. `id` is the stable `turn_id`. */
interface Pending {
  id: string;
  text: string;
  state: "sending" | "in_progress" | "indeterminate" | "failed";
  detail: string;
}

/** A `turn_id` the route will accept: required, at most 64 characters. `randomUUID` is 36 and is
 *  available on every browser the app supports; the fallback is for a non-secure context, where
 *  a collision is a duplicate turn rather than a security problem. */
function mintTurnId(): string {
  const c = globalThis.crypto;
  if (c && typeof c.randomUUID === "function") return c.randomUUID();
  return `t-${Date.now().toString(36)}-${Math.random().toString(36).slice(2, 12)}`;
}

export function MissionComposer({
  missionId,
  configured,
  isCurrent,
  onSettled,
  detail,
}: {
  /** The mission this turn belongs to. Ownership is keyed on it. */
  missionId: string;
  /** False when no AI endpoint is configured. The route answers 409 and there is no local
   *  fallback, so the control is disabled and says why. */
  configured: boolean;
  /** Is `missionId` still the selected mission? Read at RESOLUTION time, never captured — which
   *  is why it is a function rather than a boolean prop. */
  isCurrent: (missionId: string) => boolean;
  /** A turn reached a state the TIMELINE knows about, so the thread should re-read. Called on
   *  every settlement, success or failure: the operator's message is written by the claim, so
   *  even a failed turn has changed what the timeline says. */
  onSettled: () => void;
  /** THE MISSION AS THE SERVER LAST DESCRIBED IT — the whole row, deliberately, not just its
   *  `turn` (#902 review, finding 1).
   *
   *  Two things are needed here and only one of them is the turn. The turn is what a reload
   *  finds. The ROW IDENTITY is the signal that the server has spoken *at all*: a turn that
   *  settles between two detail reads leaves `turn` at `null` both times, so a fence keyed on the
   *  turn alone never fires and the composer goes on saying "still working" over an answer the
   *  timeline has already printed. A new object per read is exactly the edge that is missing. */
  detail: Mission | null;
}) {
  const [text, setText] = useState("");
  const [pending, setPending] = useState<Pending | null>(null);
  /** The pending turn, readable inside a late callback without re-identifying it. */
  const pendingRef = useRef<Pending | null>(null);
  const set = useCallback((p: Pending | null) => {
    pendingRef.current = p;
    setPending(p);
  }, []);

  const send = useCallback(
    async (turnId: string, body: string) => {
      const asked = missionId; // the mission this turn belongs to, captured once
      set({ id: turnId, text: body, state: "sending", detail: "" });
      try {
        const r: MissionTurn = await api.missionMessage(asked, {
          message: body,
          turnId,
        });
        // The operator moved on. DISCARD — an answer written into a mission they have left is
        // state they never asked to keep and will meet later with no context for it (#878).
        if (!isCurrent(asked)) return;
        if (r.state === "in_progress") {
          // A SERVER FACT, not a spinner: the turn is really still running, and a reload will
          // find it in the same state rather than finding nothing.
          set({
            id: turnId,
            text: body,
            state: "in_progress",
            detail: r.delivery_error
              ? `The instruction could not be delivered (${r.delivery_error}).`
              : "",
          });
          onSettled();
          return;
        }
        if (r.state === "indeterminate") {
          set({
            id: turnId,
            text: body,
            state: "indeterminate",
            detail:
              r.delivery_error ??
              "Nobody can say whether that instruction reached the agent.",
          });
          onSettled();
          return;
        }
        set(null);
        onSettled();
      } catch (err) {
        if (!isCurrent(asked)) return;
        // BY KIND, never by payload. #871's route refuses to put mission content in error
        // bodies, and echoing a raw server error into the thread would undo that — but its
        // `detail` strings are authored, short and actionable ("turn_id was already used for a
        // different message", "a question is already running"), so they are shown as written.
        const msg =
          err instanceof ApiError && err.message
            ? err.message
            : "That didn't go through.";
        set({ id: turnId, text: body, state: "failed", detail: msg });
        // The claim writes the operator's message before anything can fail, so the timeline has
        // changed even here.
        onSettled();
      }
    },
    [missionId, isCurrent, onSettled, set],
  );

  /** DISMISS an ambiguous turn — durably, because the state it clears is durable.
   *
   *  Clearing only the local copy would put the banner back on the next reload, which is the
   *  same bug one level down: a decision the operator made, kept somewhere that does not survive
   *  them closing the tab. `onSettled` re-reads, so the row disappears because the SERVER says
   *  it is gone rather than because this component stopped drawing it.
   */
  const dismiss = useCallback(
    async (turnId: string) => {
      const asked = missionId;
      try {
        await api.ackMissionTurn(asked, turnId);
      } catch {
        // A dismissal that did not land must not look like one that did.
        return;
      }
      if (!isCurrent(asked)) return;
      set(null);
      onSettled();
    },
    [missionId, isCurrent, onSettled, set],
  );

  const submit = useCallback(
    (e: React.FormEvent) => {
      e.preventDefault();
      const q = text.trim();
      if (!q || pending?.state === "sending" || !configured) return;
      setText("");
      void send(mintTurnId(), q);
    },
    [text, pending, configured, send],
  );

  // RECONCILED BY `turn_id`, in one place, so the row on screen has exactly one source of truth.
  //
  // Neither half can do this alone. The store is what a RELOAD finds — that is the whole point of
  // #871's durable turn, and the reason "still working" and an ambiguous turn used to vanish when
  // the tab was closed. But the store cannot answer during `sending` (the claim's response has
  // not come back) or for `failed` (a fact about this request, not about a row), and it is one
  // detail read behind a settlement the composer already knows about. So: same `turn_id` ⇒ the
  // server's version wins; otherwise the local one carries it until the read catches up.
  //
  // The message TEXT rides only on the LOCAL echo, and only while the claim may not have
  // committed. Once it has, the operator's message is an `operator_msg` event that the thread
  // above renders, and a second copy here was the duplicate row the review reproduced (#902
  // review, finding 2).
  const openTurn = detail?.turn ?? null;

  const fromServer = (o: MissionOpenTurn) => ({
    id: o.turn_id,
    text: o.text,
    state: o.state as Pending["state"],
    detail:
      o.state === "in_progress"
        ? o.delivery_error
          ? `The instruction could not be delivered (${o.delivery_error}).`
          : ""
        : o.delivery_error ||
          "Nobody can say whether that instruction reached the agent.",
    echo: false,
  });

  const shown = pending
    ? openTurn && openTurn.turn_id === pending.id
      ? // THE SERVER OUTRANKS A LOCAL FAILURE when it knows the turn (#902 review 2, finding 1).
        // A `failed` row means "this REQUEST did not come back", which is not the same claim as
        // "the turn did not happen": the server can have committed and settled it and the
        // response can still be lost.
        fromServer(openTurn)
      : { ...pending, echo: pending.state === "sending" }
    : openTurn
      ? fromServer(openTurn)
      : null;

  // A turn the SERVER has settled clears the local copy — INCLUDING a local failure.
  //
  // Without the first half the composer went on saying "still working" over an answer the
  // timeline had already printed. Without the second it showed TRY AGAIN, for ever, beside an
  // answer the server had stored: the request's response was lost, the turn completed, and the
  // only two things the client knew — "my promise rejected" and "there is no open turn" — read
  // identically to "the claim never landed".
  //
  // The discriminator is the ANSWER's own `turn_id`, which `settle_turn` stamps on the assistant
  // event. Present ⇒ the turn happened; absent ⇒ keep the failure, because TRY AGAIN is then the
  // right control to offer.
  useEffect(() => {
    const p = pendingRef.current;
    if (!p || p.state === "sending") return;
    if (detail?.turn?.turn_id === p.id) return; // the server still has it; `shown` uses that
    if (p.state === "failed") {
      const answered = (detail?.events ?? []).some(
        (e) =>
          e.kind === "assistant_msg" &&
          (e.meta as { turn_id?: string } | null)?.turn_id === p.id,
      );
      if (answered) set(null);
      return;
    }
    set(null);
    // Keyed on the ROW, not on the turn: a settlement between two reads leaves `turn` null both
    // times, and a fence that only fires when the turn CHANGES would never notice it.
  }, [detail, set]);

  const busy = pending?.state === "sending" || shown?.state === "in_progress";

  return (
    <>
      {shown ? (
        <div className={styles.event} data-testid="turn-pending">
          {/* The text is shown ONLY while the store has no row for this turn. Once the claim has
              committed, the operator's message is a timeline event above and repeating it here
              is a second row for one message. */}
          {shown.echo ? (
            <>
              <div className={styles.eventHead}>You</div>
              <div className={styles.eventText}>{shown.text}</div>
            </>
          ) : null}
          {shown.state === "sending" ? (
            <div className={styles.objReason}>…</div>
          ) : shown.state === "in_progress" ? (
            <div className={styles.objReason} data-testid="turn-in-progress">
              Still working on this. {shown.detail}
            </div>
          ) : shown.state === "indeterminate" ? (
            <div className={styles.objStale} data-testid="turn-indeterminate">
              <div>{shown.detail}</div>
              {/* THE OPERATOR DECIDES. Sending again reuses the SAME `turn_id`, so the route
                  replays its stored outcome rather than executing a second time — which is what
                  makes offering the choice safe at all. Nothing here retries on its own. */}
              <div className={styles.objReason}>
                Sending again asks the server about this same turn — it will not
                run a second time. Write a new message instead if you want the
                agent to act again.
              </div>
              <button
                type="button"
                className={styles.missionQBtn}
                onClick={() => void send(shown.id, shown.text)}
                data-testid="turn-recheck"
              >
                CHECK AGAIN
              </button>
              <button
                type="button"
                className={styles.missionQBtn}
                onClick={() => void dismiss(shown.id)}
                data-testid="turn-dismiss"
              >
                DISMISS
              </button>
            </div>
          ) : (
            <div className={styles.objStale} data-testid="turn-error">
              <div>{shown.detail}</div>
              <button
                type="button"
                className={styles.missionQBtn}
                onClick={() => void send(shown.id, shown.text)}
                data-testid="turn-retry"
              >
                TRY AGAIN
              </button>
            </div>
          )}
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
              ? "Ask this mission — find, history, or tell an agent what to do"
              : "Needs an AI endpoint"
          }
          aria-label="Send a message to this mission"
          data-testid="composer-input"
        />
        {/* THE SESSION PANE'S SEND (#967): the same class and the same paper-plane icon, so the two
            Sends are identical by construction rather than by copied values. A turn in flight
            disables it; the label stays "Send" so the control does not change width under a tap. */}
        <button
          type="submit"
          className={`${compose.send} shine`}
          disabled={!configured || busy || !text.trim()}
          data-testid="composer-send"
        >
          <Send size={15} aria-hidden="true" />
          Send
        </button>
      </form>
    </>
  );
}
