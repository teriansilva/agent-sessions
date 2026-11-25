import { useCallback, useEffect, useReducer, useState } from "react";
import { ApiError, api } from "../../lib/api";
import type { FederationGrant } from "../../lib/federation";
import {
  PEER_SUGGESTION_EVENT,
  SUGGESTION_HELD,
  SUGGESTION_NOTE,
  SUGGESTION_POLL_MS,
  cleanSuggestion,
  decisionOutcome,
  suggestionCardsInit,
  suggestionCardsReduce,
  suggestionPeer,
  suggestionPoll,
  waitingCount,
  type SuggestionCard,
} from "../../lib/federationInput";
import { Led } from "../hud/Meters";
import btn from "../ui/actionButton.module.css";
import { PeerBadge } from "./PeerBadge";
import styles from "./PeerSuggestions.module.css";
import { useAnswerFence } from "./useAnswerFence";

/** How often the pane re-reads this session's grants: it decides whether suggestions can arrive
 *  at all, and so whether the list is polled. */
const GRANTS_EVERY_MS = 30_000;

/** A call that never throws synchronously, whatever `api` is in a test double. */
const attempt = <T,>(call: () => Promise<T>): Promise<T> => Promise.resolve().then(call);

/** **What a paired instance suggests** (#2057, #2061), above the composer of the owner's pane.
 *
 *  One card per suggestion, oldest first, and ONE decision per card: Approve, Edit then Approve
 *  edited, or Reject. That is the whole surface, on purpose:
 *
 *  - **Nothing here approves more than one**, and nothing approves on its own: no approve-all, no
 *    "always for this peer", no timer, no keyboard shortcut. A suggestion is approved by
 *    activating that card's own button (which has to hold focus for a key to reach it); the edit
 *    box's Enter is a newline.
 *  - **The text is another party's, so it is plain text.** It is drawn as a text node in a quoted
 *    block (`white-space: pre-wrap`): never Markdown, never markup, never a link, and it is not
 *    passed to anything else on this page (no "ask HQ", no assistant, no clipboard helper).
 *  - **Every outcome is the server's.** Approve answers `approved` (the card leaves; the turn and
 *    its delivery state are in the conversation) or `approving` (decided, delivery not confirmed:
 *    the card says so and offers nothing). A `409` says who decided first; the card shows that
 *    and leaves. Nothing is assumed from a click. */
export function PeerSuggestions({
  sessionKey,
  sharesOpen = false,
}: {
  sessionKey: string;
  /** The share dialog is open: when it closes, this session's grants are read again. */
  sharesOpen?: boolean;
}) {
  const [state, dispatch] = useReducer(suggestionCardsReduce, suggestionCardsInit);
  const [grants, setGrants] = useState<Pick<FederationGrant, "level">[]>([]);
  const [busy, setBusy] = useState<string | null>(null);
  const [editing, setEditing] = useState<{ id: string; text: string } | null>(null);
  const [errors, setErrors] = useState<Record<string, string>>({});
  // Which answers may still change this pane (#2055 review): armed in an effect BODY, so
  // StrictMode's replay arms it again, and a fresh token per session, so an answer for the
  // session the pane showed before, or one that arrives after it closed, is dropped. Declared
  // before the effects that start requests, so it is armed when they run.
  const ticket = useAnswerFence(sessionKey);

  /** The list as the server has it now, or `null` when it could not be read (then nothing is
   *  concluded: the cards stay as they are). Fenced by the session: an answer for another
   *  session's pane is dropped. */
  const fetchRows = useCallback(async (): Promise<unknown[] | null> => {
    const current = ticket();
    try {
      const answer = await attempt(() => api.federationSuggestions(sessionKey));
      if (!current()) return null;
      return Array.isArray(answer?.suggestions) ? answer.suggestions : [];
    } catch {
      return null;
    }
  }, [sessionKey, ticket]);
  const read = useCallback(async () => {
    const rows = await fetchRows();
    if (rows) dispatch({ type: "list", rows, now: Date.now() });
  }, [fetchRows]);

  const readGrants = useCallback(async () => {
    const current = ticket();
    try {
      const answer = await attempt(() => api.federationGrants(sessionKey));
      if (current()) setGrants(Array.isArray(answer?.grants) ? answer.grants : []);
    } catch {
      /* no grants known: the list is then read on the bell's word only */
    }
  }, [sessionKey, ticket]);

  // Another session in the same pane: nothing of the old one stays.
  const [shown, setShown] = useState(sessionKey);
  if (shown !== sessionKey) {
    setShown(sessionKey);
    dispatch({ type: "reset" });
    setGrants([]);
    setEditing(null);
    setErrors({});
    setBusy(null);
  }

  useEffect(() => {
    void read();
    void readGrants();
    const timer = setInterval(() => void readGrants(), GRANTS_EVERY_MS);
    // The bell saw a suggestion arrive (or go): read now, whatever the poll is doing.
    const onBell = () => {
      void read();
      void readGrants();
    };
    window.addEventListener(PEER_SUGGESTION_EVENT, onBell);
    return () => {
      clearInterval(timer);
      window.removeEventListener(PEER_SUGGESTION_EVENT, onBell);
    };
  }, [read, readGrants]);
  useEffect(() => {
    if (!sharesOpen) void readGrants();
  }, [sharesOpen, readGrants]);

  const polling = suggestionPoll(state, grants);
  useEffect(() => {
    if (!polling) return;
    const timer = setInterval(() => void read(), SUGGESTION_POLL_MS);
    return () => clearInterval(timer);
  }, [polling, read]);

  // A card that says what happened leaves on its own clock.
  const nextLeave = state.cards.reduce<number | null>(
    (at, c) => (c.leavesAt === null ? at : at === null ? c.leavesAt : Math.min(at, c.leavesAt)),
    null,
  );
  useEffect(() => {
    if (nextLeave === null) return;
    const timer = setTimeout(
      () => dispatch({ type: "tick", now: Date.now() }),
      Math.max(0, nextLeave - Date.now()) + 20,
    );
    return () => clearTimeout(timer);
  }, [nextLeave]);

  /** One decision for ONE suggestion. `text` only from that card's own edit box. */
  const decide = async (card: SuggestionCard, what: "approve" | "reject", text?: string) => {
    const id = card.suggestion.suggestion_id;
    if (busy) return;
    const current = ticket();
    setBusy(id);
    setErrors((e) => ({ ...e, [id]: "" }));
    try {
      const answer =
        what === "approve"
          ? await attempt(() => api.approveFederationSuggestion(id, text))
          : await attempt(() => api.rejectFederationSuggestion(id));
      if (!current()) return;
      dispatch({ type: "decided", suggestion: answer?.suggestion });
      setEditing((e) => (e?.id === id ? null : e));
    } catch (e) {
      if (!current()) return;
      const status = e instanceof ApiError ? e.status : 0;
      const body = e instanceof ApiError ? e.record : null;
      // Who decided first is the list's to say (the 409 names the state, not who withdrew it).
      const rows = await fetchRows();
      if (!current()) return;
      const now = (rows ?? []).map(cleanSuggestion).find((r) => r?.suggestion_id === id) ?? null;
      const outcome = decisionOutcome(status, body, now);
      // The outcome first, so the card says what happened before the list takes it away.
      if (outcome) {
        dispatch({ type: "outcome", id, ...outcome, now: Date.now() });
        setEditing((x) => (x?.id === id ? null : x));
      }
      if (rows) dispatch({ type: "list", rows, now: Date.now() });
      if (!outcome) {
        const held = status === 409 && (body as { code?: unknown } | null)?.code === "held";
        setErrors((x) => ({
          ...x,
          [id]: held
            ? SUGGESTION_HELD
            : e instanceof ApiError && e.message
              ? e.message
              : what === "approve"
                ? "Couldn’t approve this suggestion. Nothing was sent."
                : "Couldn’t reject this suggestion.",
        }));
      }
    } finally {
      if (current()) setBusy(null);
    }
  };

  if (state.cards.length === 0) return null;
  const waiting = waitingCount(state);
  const firstPending = state.cards.find((c) => c.view.kind === "pending")?.suggestion.suggestion_id;

  return (
    <section className={styles.zone} aria-label="Suggestions from paired instances" data-testid="peer-suggestions">
      {waiting > 1 && (
        <p className={styles.count} data-testid="peer-suggestions-count">
          <Led tone="att" />
          {waiting} waiting
        </p>
      )}
      {state.cards.map((card) => {
        const s = card.suggestion;
        const id = s.suggestion_id;
        const peer = suggestionPeer(s);
        const view = card.view;
        const edit = editing?.id === id ? editing : null;
        const pending = view.kind === "pending";
        return (
          <article
            key={id}
            className={`${styles.card} ${pending ? "" : styles.quiet}`}
            data-testid="peer-suggestion"
            data-state={view.kind === "note" ? view.word : view.kind}
            aria-label={`Suggestion from ${peer}`}
          >
            <header className={styles.head}>
              <Led tone={pending ? "att" : "idle"} />
              <PeerBadge name={peer} />
              <span>{pending ? "suggests" : "suggested"}</span>
              <span className={styles.word} data-testid="peer-suggestion-word">
                {pending ? "waiting" : view.kind === "approving" ? "approved" : view.word}
              </span>
            </header>
            {view.kind === "note" && (
              <p className={`${styles.note} ${styles.only}`} role="status" data-testid="peer-suggestion-outcome">
                {view.text}
              </p>
            )}
            {view.kind === "approving" && (
              // The decision is made; the delivery is not confirmed. Never "delivered" or "sent".
              <p className={`${styles.note} ${styles.only}`} role="status" data-testid="peer-suggestion-approving">
                {view.text}
              </p>
            )}
            {pending && (
              <>
                {edit ? (
                  <textarea
                    className={styles.edit}
                    aria-label={`Edit the message ${peer} suggests`}
                    value={edit.text}
                    autoFocus
                    disabled={busy === id}
                    data-testid="peer-suggestion-edit"
                    onChange={(e) => setEditing({ id, text: e.target.value })}
                  />
                ) : (
                  // A TEXT NODE in a block that keeps its line breaks. Not Markdown, not HTML,
                  // not a link: what another party wrote is shown exactly as characters.
                  <blockquote className={styles.quote} data-testid="peer-suggestion-text">
                    {s.text}
                  </blockquote>
                )}
                <div className={styles.actions}>
                  {edit ? (
                    <>
                      <button
                        type="button"
                        className={btn.primary}
                        disabled={busy !== null || !edit.text.trim()}
                        data-testid="peer-suggestion-approve-edited"
                        onClick={() => void decide(card, "approve", edit.text)}
                      >
                        {busy === id ? "Approving…" : "Approve edited"}
                      </button>
                      <button
                        type="button"
                        className={btn.ghost}
                        disabled={busy === id}
                        onClick={() => setEditing(null)}
                      >
                        Cancel
                      </button>
                    </>
                  ) : (
                    <>
                      <button
                        type="button"
                        className={btn.primary}
                        disabled={busy !== null}
                        data-testid="peer-suggestion-approve"
                        onClick={() => void decide(card, "approve")}
                      >
                        {busy === id ? "Approving…" : "Approve"}
                      </button>
                      <button
                        type="button"
                        className={btn.ghost}
                        disabled={busy !== null}
                        data-testid="peer-suggestion-edit-open"
                        onClick={() => setEditing({ id, text: s.text })}
                      >
                        Edit
                      </button>
                      <button
                        type="button"
                        className={btn.danger}
                        disabled={busy !== null}
                        data-testid="peer-suggestion-reject"
                        onClick={() => void decide(card, "reject")}
                      >
                        Reject
                      </button>
                    </>
                  )}
                </div>
                {errors[id] && (
                  <p className={styles.error} role="alert" data-testid="peer-suggestion-error">
                    {errors[id]}
                  </p>
                )}
                {id === firstPending && (
                  <p className={styles.note} data-testid="peer-suggestion-note">
                    {SUGGESTION_NOTE}
                  </p>
                )}
              </>
            )}
          </article>
        );
      })}
    </section>
  );
}
