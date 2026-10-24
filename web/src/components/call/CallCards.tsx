/** HQ's cards on a call (#1397 P1/P2): the same records and the same tap routes as HQ's panel,
 *  re-read after every turn and every voice settlement, plus what the voice may do with each.
 *
 *  - **Say "confirm"**: the server armed a spoken window for this card (after its read-back played
 *    out). A countdown shows how long; with several cards, the ordinal to say.
 *  - **Settled by voice**: what the card's own route answered (and, after a 202, how it ended).
 *  - **Needs a choice on screen**: an operation that needs an input is never voice-confirmable.
 *
 *  A tap on any card during the call says `card_decided` first, which closes its spoken window. The
 *  Needs-you rows HQ read out ("What needs me?") are not conversation cards: the challenge names
 *  their ledger actions, and they are read from the Needs-you list to be shown here. */
import { useCallback, useEffect, useRef, useState } from "react";

import { announceActionResolved } from "../../lib/actionEvents";
import { api, ApiError } from "../../lib/api";
import type { AskConversationTurn, AskProposalCard, NeedsYouRow } from "../../types/api";
import { NudgeCard, OperationCard, SessionCard } from "../ask/AskCards";
import b from "../ui/actionButton.module.css";
import { confirmPhrase, settledWord } from "./cardVoice";
import { useCall } from "./callContext";
import s from "./call.module.css";

function useNow(on: boolean): number {
  const [now, setNow] = useState(() => Date.now());
  useEffect(() => {
    if (!on) return;
    const tick = () => setNow(Date.now());
    const first = window.setTimeout(tick, 0); // a window that opens later must not count from mount
    const id = window.setInterval(tick, 500);
    return () => {
      window.clearTimeout(first);
      window.clearInterval(id);
    };
  }, [on]);
  return now;
}

/** The voice line above one card: the spoken window, the settlement, or why the voice can't. */
function VoiceLine({
  id,
  kind,
  screenOnly,
  now,
}: {
  id: string;
  kind: string;
  screenOnly: boolean;
  now: number;
}) {
  const { snap } = useCall();
  const settled = snap.settled[id];
  const ch = snap.challenge;
  const index = ch ? ch.cards.indexOf(id) : -1;
  const left = ch ? Math.ceil((ch.expiresAt - now) / 1000) : 0;
  if (settled) {
    const w = settledWord(settled, kind);
    return (
      <div className={s.voice} data-testid="call-card-voice" data-voice="settled" data-tone={w.tone}>
        <span className={`nj-stamp ${w.tone === "ok" ? "is-ok" : w.tone === "warn" ? "is-warn" : "is-down"}`}>
          {w.text}
        </span>
        {settled.final === null ? (
          <span className={s.voiceNote}>The card will show when it's done.</span>
        ) : null}
      </div>
    );
  }
  if (ch && index >= 0 && left > 0 && snap.phase === "connected") {
    return (
      <div className={s.voice} data-testid="call-card-voice" data-voice="armed">
        <span className="nj-stamp is-hud" data-testid="call-card-say">
          Say “{confirmPhrase(index, ch.cards.length)}” · {left} s
        </span>
        <span className={s.voiceNote}>
          Listening for “confirm” or “cancel”. Tapping works too; a tap ends the spoken window.
        </span>
      </div>
    );
  }
  if (screenOnly && snap.phase === "connected") {
    return (
      <div className={s.voice} data-testid="call-card-voice" data-voice="screen">
        <span className="nj-stamp">Needs a choice on screen</span>
        <span className={s.voiceNote}>Not confirmable by voice: make the choice here.</span>
      </div>
    );
  }
  return null;
}

/** A Needs-you question HQ read out, answerable here or by voice. */
function NeedsYouCallCard({
  row,
  deciding,
  error,
  onApprove,
  onDismiss,
}: {
  row: NeedsYouRow;
  deciding: boolean;
  error: string | null;
  onApprove: () => void;
  onDismiss: () => void;
}) {
  const a = row.action!;
  return (
    <section className={`nj-panel is-sm ${s.nyCard}`} data-testid="call-needs-you-card">
      <div className="nj-face">
        <span className="nj-rail is-short" />
        <div className="nj-callsign">
          <span className="nj-notch" />
          <span className="nj-title">Needs you</span>
        </div>
        <dl className={s.kv}>
          <dt>Session</dt>
          <dd>{row.title}</dd>
          {row.summary || row.reason ? (
            <>
              <dt>Asks</dt>
              <dd>{row.summary || row.reason}</dd>
            </>
          ) : null}
          <dt>Answer</dt>
          <dd className={s.mono}>{a.answer ?? ""}</dd>
        </dl>
        {error ? (
          <p className={s.voiceNote} role="alert">
            {error}
          </p>
        ) : null}
        <div className={s.acts}>
          <button type="button" className={b.primary} disabled={deciding} onClick={onApprove}>
            Send
          </button>
          <button type="button" className={b.ghost} disabled={deciding} onClick={onDismiss}>
            Dismiss
          </button>
        </div>
      </div>
    </section>
  );
}

/** Can the operator still decide this card? (The server's own `can_*`; never derived here.) */
function actionable(p: AskProposalCard): boolean {
  if (p.kind === "nudge") return p.can_approve || p.can_reject;
  if (p.kind === "session") return p.can_start || p.can_dismiss;
  return p.can_confirm || p.can_dismiss;
}

/** The call's cards: the newest turn's (decided ones show their outcome), plus every OLDER card
 *  still waiting for a decision — a new turn never hides a card still pending, as on the HQ page. */
function callCards(turns: AskConversationTurn[]): AskProposalCard[] {
  const newest = [...turns].reverse().find((t) => (t.proposals ?? []).length > 0);
  const latest = newest?.proposals ?? [];
  const seen = new Set(latest.map((p) => p.action_id));
  const older: AskProposalCard[] = [];
  for (const t of turns) {
    if (t === newest) continue;
    for (const p of t.proposals ?? []) {
      if (!seen.has(p.action_id) && actionable(p)) {
        seen.add(p.action_id);
        older.push(p);
      }
    }
  }
  return [...older, ...latest];
}

export function CallCards({ cid, seq }: { cid: string | null; seq: number }) {
  const call = useCall();
  const { snap } = call;
  const [cards, setCards] = useState<AskProposalCard[]>([]);
  const [needsYou, setNeedsYou] = useState<NeedsYouRow[]>([]);
  const [deciding, setDeciding] = useState<Set<string>>(new Set());
  const [errors, setErrors] = useState<Map<string, string>>(new Map());
  // Only the NEWEST read of THIS conversation may show its cards: an older read answering late
  // must never bring back cards a newer turn replaced (#1640 review).
  const reads = useRef(0);
  const load = useCallback(() => {
    if (!cid) return;
    const mine = ++reads.current;
    api
      .askConversation(cid)
      .then((c) => {
        if (reads.current !== mine) return;
        setCards(callCards(c.turns));
      })
      .catch(() => undefined);
  }, [cid]);
  useEffect(() => {
    reads.current += 1; // another conversation: every read in flight is stale
  }, [cid]);
  useEffect(() => {
    load(); // on opening the call (its conversation may already hold cards), and after each turn
  }, [seq, load]);

  // A spoken window naming cards that are not this conversation's: Needs-you actions HQ read out.
  const proposals = cards;
  const challenged = snap.challenge?.cards ?? [];
  const unknown = challenged.filter(
    (id) => !proposals.some((p) => p.action_id === id) && !needsYou.some((r) => r.action?.id === id),
  );
  const wanted = unknown.join(",");
  useEffect(() => {
    if (!wanted) return;
    const ids = new Set(wanted.split(","));
    api
      .needsYou()
      .then((p) => {
        const rows = p.rows.filter((r) => r.action && ids.has(r.action.id));
        if (rows.length) {
          setNeedsYou((prev) => [...prev.filter((r) => !rows.some((n) => n.id === r.id)), ...rows]);
        }
      })
      .catch(() => undefined);
  }, [wanted]);

  const run = async (id: string, act: () => Promise<unknown>, after?: () => void) => {
    call.cardDecided(id);
    setDeciding((d) => new Set(d).add(id));
    try {
      await act();
      announceActionResolved(null);
      after?.();
      setErrors((m) => {
        const n = new Map(m);
        n.delete(id);
        return n;
      });
    } catch (e) {
      const msg = e instanceof ApiError || e instanceof Error ? e.message : "That did not go through.";
      setErrors((m) => new Map(m).set(id, msg));
    } finally {
      setDeciding((d) => {
        const n = new Set(d);
        n.delete(id);
        return n;
      });
      load();
    }
  };
  const dropNeedsYou = (id: string) => () => setNeedsYou((rows) => rows.filter((r) => r.action?.id !== id));

  const now = useNow(!!snap.challenge);
  if (!proposals.length && !needsYou.length) return null;
  return (
    <div className={s.cards} data-testid="call-cards">
      {needsYou.map((r) => {
        const id = r.action!.id;
        return (
          <div key={id} className={s.callCard} data-testid="call-card" data-card={id}>
            <VoiceLine id={id} kind="needs_you" screenOnly={false} now={now} />
            {snap.settled[id]?.ok ? null : (
              <NeedsYouCallCard
                row={r}
                deciding={deciding.has(id)}
                error={errors.get(id) ?? null}
                onApprove={() => void run(id, () => api.approveAction(id), dropNeedsYou(id))}
                onDismiss={() => void run(id, () => api.needsYouDismiss(r.id, id), dropNeedsYou(id))}
              />
            )}
          </div>
        );
      })}
      {proposals.map((p) => (
        <div key={p.action_id} className={s.callCard} data-testid="call-card" data-card={p.action_id}>
          <VoiceLine
            id={p.action_id}
            kind={p.kind}
            screenOnly={p.kind === "operation" && (p.needs ?? []).length > 0 && p.state === "pending"}
            now={now}
          />
          {p.kind === "session" ? (
            <SessionCard
              // A new starting value (the card asks again) starts the bypass control afresh — as
              // the HQ console does: never a previous choice shown or sent for a changed default.
              key={`${p.action_id}:${p.bypass_default}`}
              p={p}
              deciding={deciding.has(p.action_id)}
              error={errors.get(p.action_id) ?? null}
              onStart={(bypass) =>
                void run(p.action_id, () => api.decideAskProposal(p.action_id, "start", bypass))
              }
              onDismiss={() => void run(p.action_id, () => api.decideAskProposal(p.action_id, "dismiss"))}
            />
          ) : p.kind === "operation" ? (
            <OperationCard
              p={p}
              deciding={deciding.has(p.action_id)}
              error={errors.get(p.action_id) ?? null}
              onConfirm={(inputs) =>
                void run(p.action_id, () => api.decideAskOperation(p.action_id, "confirm", inputs))
              }
              onDismiss={() => void run(p.action_id, () => api.decideAskOperation(p.action_id, "dismiss"))}
            />
          ) : (
            <NudgeCard
              p={p}
              deciding={deciding.has(p.action_id)}
              error={errors.get(p.action_id) ?? null}
              onApprove={() => void run(p.action_id, () => api.approveAction(p.action_id))}
              onDismiss={() => void run(p.action_id, () => api.rejectAction(p.action_id))}
            />
          )}
        </div>
      ))}
    </div>
  );
}
