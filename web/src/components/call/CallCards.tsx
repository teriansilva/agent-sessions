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
import { CommandCard, MessageCard, NudgeCard, OperationCard, SessionCard } from "../ask/AskCards";
import b from "../ui/actionButton.module.css";
import { BATCH_NOTE, BATCH_PHRASE, confirmPhrase, listeningFor, settledWord } from "./cardVoice";
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

/** A settlement's stamp; a dismiss is neutral, never a success or failure colour (#2120). */
const STAMP_CLASS = { ok: "is-ok", warn: "is-warn", alert: "is-down", neutral: "" } as const;

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
        <span className={`nj-stamp ${STAMP_CLASS[w.tone]}`}>
          {w.text}
        </span>
        {settled.final === null ? (
          <span className={s.voiceNote}>The card will show when it's done.</span>
        ) : null}
      </div>
    );
  }
  if (ch && index >= 0 && left > 0 && snap.phase === "connected") {
    if (ch.batch) return null; // one band above the cards says it for all of them (#2389)
    return (
      <div className={s.voice} data-testid="call-card-voice" data-voice="armed">
        <span className="nj-stamp is-hud" data-testid="call-card-say">
          Say “{confirmPhrase(index, ch.cards.length)}” · {left} s
        </span>
        <span className={s.voiceNote}>
          Listening for {listeningFor(kind)}. Tapping works too; a tap ends the spoken window.
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

/** The call's ONE open selection (#1911): the cards of the newest turn that presented a
 *  selection — cards, or a question (answered or not: once HQ asked, what came before is behind
 *  it). A newer selection takes the older cards off the call screen (hidden here, never
 *  dismissed: they stay on the HQ page); a turn that presents nothing new (talk, a prose reply)
 *  leaves the current one where it is. A card a spoken window still names stays in view, whatever
 *  its turn: "confirm" never answers a card the screen no longer shows. */
function callCards(
  turns: AskConversationTurn[],
  armed: readonly string[],
  pin: Pin | null = null,
): AskProposalCard[] {
  const newest = [...turns]
    .reverse()
    .find((t) => (t.proposals ?? []).length > 0 || t.ask != null);
  // Cards a spoken window named stay after it is consumed (#2389, Hermes 8973): a re-offered card
  // and its result, until a selection that came later (and holds none of them) takes over.
  if (pin) {
    // "Later" is by the SERVER's turn order (Hermes 8975), never by what this screen had loaded
    // when the window armed: until the offering turn is in the history, nothing is later.
    const at = pin.turn ? turns.findIndex((t) => t.turn_id === pin.turn) : -1;
    const later = (at < 0 ? [] : turns.slice(at + 1)).some(
      (t) =>
        ((t.proposals ?? []).length > 0 || t.ask != null) &&
        !(t.proposals ?? []).some((p) => pin.ids.includes(p.action_id)),
    );
    if (!later) armed = [...armed, ...pin.ids.filter((id) => !armed.includes(id))];
  }
  const shown = newest?.proposals ?? [];
  const seen = new Set(shown.map((p) => p.action_id));
  const named: AskProposalCard[] = [];
  for (const p of turns.flatMap((t) => t.proposals ?? [])) {
    if (armed.includes(p.action_id) && !seen.has(p.action_id)) {
      seen.add(p.action_id);
      named.push(p);
    }
  }
  return [...named, ...shown];
}

/** The ONE voice band of a batch (#2389): one yes settles every card it names. */
function BatchBand({ now }: { now: number }) {
  const { snap } = useCall();
  const ch = snap.challenge;
  const left = ch ? Math.ceil((ch.expiresAt - now) / 1000) : 0;
  if (!ch?.batch || left <= 0 || snap.phase !== "connected") return null;
  return (
    <div className={`${s.voice} ${s.batchBand}`} data-testid="call-batch-voice" data-voice="armed">
      <span className="nj-stamp is-hud" data-testid="call-batch-say">
        {BATCH_PHRASE} · {left} s
      </span>
      <span className={s.voiceNote}>{BATCH_NOTE}</span>
    </div>
  );
}

/** Cards shown before "+ N more" (#2389): a batch of twelve would push everything else away. A
 *  card whose voice settlement did not go through always shows, so "it's still on your screen"
 *  is never a card hidden behind the fold. */
export const CARDS_SHOWN = 3;

/** The cards the newest spoken window named, and the HQ turn that offered them. */
interface Pin {
  ids: string[];
  turn: string | null;
}

export function CallCards({ cid, seq }: { cid: string | null; seq: number }) {
  const call = useCall();
  const { snap } = call;
  const [turns, setTurns] = useState<AskConversationTurn[]>([]);
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
        setTurns(c.turns);
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
  const challenged = snap.challenge?.cards ?? [];
  const [pin, setPin] = useState<Pin | null>(null);
  const named = challenged.join(",");
  const [pinFor, setPinFor] = useState({ cid, named: "" });
  if (pinFor.cid !== cid || pinFor.named !== named) {
    // A newly named window pins its cards; another conversation drops the pin (set during
    // render, React's pattern for state derived from a changed prop).
    setPinFor({ cid, named });
    if (named) setPin({ ids: named.split(","), turn: snap.challenge?.turn ?? null });
    else if (pinFor.cid !== cid) setPin(null);
  }
  const proposals = callCards(turns, challenged, pin);
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
  const [expanded, setExpanded] = useState(false);
  const moreId = `call-cards-${cid ?? "none"}`;
  if (!proposals.length && !needsYou.length) return null;
  const folded = !expanded && proposals.length > CARDS_SHOWN + 1;
  // The stamp's own semantics (Hermes 8973): a 202 that ended `failed` is a failure too.
  const attention = (p: AskProposalCard) => {
    const st = snap.settled[p.action_id];
    return !!st && (!!st.unknown || settledWord(st, p.kind).tone === "alert");
  };
  const shown = folded ? proposals.filter((p, i) => i < CARDS_SHOWN || attention(p)) : proposals;
  const hidden = proposals.length - shown.length;
  return (
    <div className={s.cards} data-testid="call-cards" id={moreId}>
      <BatchBand now={now} />
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
                onDismiss={() =>
                  void run(id, () => api.needsYouDismiss(r.id, id, { actionIds: [id] }), dropNeedsYou(id))
                }
              />
            )}
          </div>
        );
      })}
      {shown.map((p) => (
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
          ) : p.kind === "message" ? (
            <MessageCard
              p={p}
              deciding={deciding.has(p.action_id)}
              error={errors.get(p.action_id) ?? null}
              onSend={() => void run(p.action_id, () => api.decideAskMessage(p.action_id, "send"))}
              onDismiss={() => void run(p.action_id, () => api.decideAskMessage(p.action_id, "dismiss"))}
            />
          ) : p.kind === "command" ? (
            // HQ's command card (#2404): on a call it is confirmed on this screen, never by voice.
            <CommandCard
              p={p}
              deciding={deciding.has(p.action_id)}
              error={errors.get(p.action_id) ?? null}
              onRun={() => void run(p.action_id, () => api.decideAskCommand(p.action_id, "confirm"))}
              onDismiss={() => void run(p.action_id, () => api.decideAskCommand(p.action_id, "dismiss"))}
              onStop={() => void run(p.action_id, () => api.decideAskCommand(p.action_id, "stop"))}
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
      {proposals.length > CARDS_SHOWN + 1 && (hidden > 0 || expanded) ? (
        <button
          type="button"
          className={`${b.ghost} ${s.more}`}
          data-testid="call-cards-more"
          aria-expanded={expanded}
          aria-controls={moreId}
          onClick={() => setExpanded((on) => !on)}
        >
          {expanded ? "Show fewer" : `+ ${hidden} more`}
        </button>
      ) : null}
    </div>
  );
}
