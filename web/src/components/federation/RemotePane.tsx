import { useCallback, useEffect, useLayoutEffect, useReducer, useRef, useState } from "react";
import { refreshRemoteShares, useRemoteShares } from "../../app/remoteShares";
import { ApiError, api } from "../../lib/api";
import {
  REMOTE_EVENTS_WAIT_S,
  REMOTE_GONE,
  REMOTE_RETRY_MS,
  abortablePause as pause,
  SUGGEST_UNCONFIRMED,
  SUGGESTION_POLL_OWN_MS,
  composerState,
  deliveryLine,
  draftKey,
  grantEnds,
  inputLevel,
  ownSuggestionLine,
  pendingOps,
  reconcilePlan,
  reloadNotices,
  remoteAccessWords,
  remoteOfflineText,
  remotePaneBadge,
  remotePaneInit,
  remotePaneReduce,
  remoteTurnView,
  remoteTurns,
  remoteViewOnlyText,
  settlesRecord,
  shareKind,
  shortTime,
  writeFailure,
  type InputLevel,
  type OutboxRow,
} from "../../lib/federation";
import { Avatar } from "../chat/Avatar";
import { ShareApproval, ShareTurn } from "../../share/Turns";
import viewer from "../../share/ShareViewer.module.css";
import { Led } from "../hud/Meters";
import btn from "../ui/actionButton.module.css";
import { RemoteFrame } from "./RemoteFrame";
import styles from "./Remote.module.css";

const statusOf = (e: unknown) => (e instanceof ApiError ? e.status : 0);
const codeOf = (e: unknown) =>
  e instanceof ApiError ? (e.record as { code?: unknown } | null | undefined)?.code : undefined;
const uuid = () => crypto.randomUUID();

/** How a message this instance sent fared: a word with its LED, and what it means. */
function Delivery({
  row,
  onResend,
  busy,
}: {
  row: OutboxRow;
  onResend?: () => void;
  busy: boolean;
}) {
  const line = deliveryLine(row.delivery);
  return (
    <p className={styles.delivery} data-testid="remote-delivery" data-delivery={row.delivery}>
      <Led tone={line.tone === "ok" ? "up" : "idle"} />
      <span className={line.tone === "ok" ? styles.wordOk : undefined}>{line.word}</span>
      {line.detail && <span className={styles.deliveryDetail}>{line.detail}</span>}
      {/* ONLY a message the owner says did not reach the agent, and only as the SAME message
          under the SAME key. "Not confirmed" never gets this control. */}
      {line.resend && row.text !== null && onResend && (
        <button
          type="button"
          className={`${btn.ghost} ${btn.compact} ${styles.resend}`}
          disabled={busy}
          data-testid="remote-resend"
          onClick={onResend}
        >
          Send again
        </button>
      )}
    </p>
  );
}

/** The pane of an API session another instance shares. A terminal share is never drawn here:
 *  `RemoteSessionView` gives it `RemoteScreenPane`, which has no composer (#2060).
 *
 *  **Every answer is fenced.** Snapshot, events, history, the suggestion list, the status read
 *  and the answers to Suggest, Send and Withdraw all carry the generation they started under.
 *  The reducer (`remotePaneReduce`) bumps the generation whenever the pane clears (404, 503, a
 *  new peer or grant) and drops an answer from any other generation, and the handlers below
 *  check the same generation before they touch the composer. So a slow success can never refill
 *  a pane that was cleared or now shows another session, never re-enables its composer, and
 *  never clears or overwrites what is being typed now.
 *
 *  **Nothing stale, nothing kept.** Turns, typed text and suggestions live in this component's
 *  state and nowhere else. The ONE thing in storage is `pendingOps`: while a Suggest or Send is
 *  in flight, `{peer_id, grant_id, client_key, kind, started_at}` in `sessionStorage`, so a
 *  reload can ASK the owner what became of it. It holds no text and is never used to resubmit.
 *
 *  **The owner decides.** The mirrored level only chooses which composer to offer. Whether a
 *  message is accepted, and what became of it, is what the owner answered, shown as answered:
 *  "delivery not confirmed" is its own state with no resend control. */
export function RemotePane({ peerId, grantId }: { peerId: string; grantId: string }) {
  const target = `${peerId}/${grantId}`;
  const [state, dispatch] = useReducer(remotePaneReduce, target, (t) =>
    remotePaneInit(t, pendingOps.of(peerId, grantId)),
  );
  // A new peer or grant on the SAME mounted route: clear before anything of the old one is
  // painted (a render-phase update; React re-renders at once, before the commit).
  if (state.target !== target)
    dispatch({ type: "open", target, leftovers: pendingOps.of(peerId, grantId) });

  const shared = useRemoteShares();
  const listed = shared.find((r) => r.peer_id === peerId && r.grant_id === grantId) ?? null;
  // What the list said of this share when it was last listed: the title and the peer's name
  // outlive the row (it leaves the list when the share ends), the content does not.
  const [known, setKnown] = useState<{ target: string; title: string; peer: string } | null>(null);
  if (listed && (known?.target !== target || known.title !== listed.title || known.peer !== listed.peer_name))
    setKnown({ target, title: listed.title, peer: listed.peer_name });
  const facts = known?.target === target ? known : null;
  const peer = facts?.peer || "the other instance";
  const title = facts?.title || "Shared session";

  const { generation, startAfterMs } = state;
  const [historyBusy, setHistoryBusy] = useState(false);
  const signalRef = useRef<AbortSignal | null>(null);
  // What the handlers of this pane's own requests check before they touch anything: the
  // generation and target of the LATEST render.
  const now_ = useRef({ generation, target });
  now_.current = { generation, target };
  const current = (g: number, t: string) => now_.current.generation === g && now_.current.target === t;

  useEffect(() => {
    if (startAfterMs === null) return; // gone: nothing is asked again
    const ctl = new AbortController();
    const { signal } = ctl;
    signalRef.current = signal;
    const fail = (e: unknown) => {
      if (!signal.aborted) dispatch({ type: "failed", generation, status: statusOf(e) });
    };
    const run = async () => {
      await pause(startAfterMs, signal);
      while (!signal.aborted) {
        let snapshot;
        try {
          snapshot = await api.federationRemoteSnapshot(peerId, grantId, signal);
        } catch (e) {
          fail(e);
          await pause(REMOTE_RETRY_MS, signal);
          continue; // a clearing failure bumped the generation: this loop is aborted by then
        }
        if (signal.aborted) return;
        dispatch({ type: "snapshot", generation, snapshot });
        let cursor = Math.max(0, snapshot.event_cursor ?? 0);
        // Follow the journal: an events read is held by the owner until something moves. It
        // carries cursors and kinds only, so any movement is answered by reading the snapshot.
        for (;;) {
          const asked = Date.now();
          let page;
          try {
            page = await api.federationRemoteEvents(
              peerId,
              grantId,
              cursor,
              REMOTE_EVENTS_WAIT_S,
              signal,
            );
          } catch (e) {
            if (signal.aborted) return;
            // 409: the cursor ran ahead of the journal (the session restarted): read it whole.
            if (statusOf(e) !== 409) fail(e);
            await pause(statusOf(e) === 409 ? 0 : REMOTE_RETRY_MS, signal);
            break;
          }
          if (signal.aborted) return;
          const moved =
            (Array.isArray(page.events) && page.events.length > 0) ||
            (page.revision ?? null) !== (snapshot.revision ?? null);
          if (moved) break;
          if (typeof page.next_cursor === "number") cursor = Math.max(cursor, page.next_cursor);
          // An owner that answers at once (nothing held) must not be asked in a tight loop.
          await pause(1_000 - (Date.now() - asked), signal);
        }
      }
    };
    void run();
    return () => ctl.abort();
  }, [peerId, grantId, generation, startAfterMs]);

  // The share ended: the mirror was refreshed by the read that said so, so the list read that
  // follows no longer has the row.
  const gone = state.status === "gone";
  useEffect(() => {
    if (gone) void refreshRemoteShares();
  }, [gone]);

  const turns = remoteTurns(state);
  const oldest = turns[0]?.turn_id;
  const loadOlder = useCallback(async () => {
    const signal = signalRef.current;
    if (!oldest || !signal || signal.aborted) return;
    setHistoryBusy(true);
    try {
      const page = await api.federationRemoteHistory(peerId, grantId, oldest, signal);
      if (!signal.aborted) dispatch({ type: "history", generation, page });
    } catch (e) {
      if (!signal.aborted) dispatch({ type: "failed", generation, status: statusOf(e) });
    } finally {
      setHistoryBusy(false);
    }
  }, [peerId, grantId, generation, oldest]);

  // Follow the conversation while the reader is at the bottom; leave them where they scrolled.
  const scroller = useRef<HTMLDivElement | null>(null);
  const stick = useRef(true);
  const newest = turns[turns.length - 1]?.turn_id;
  const revision = state.snapshot?.revision;
  const outboxSize = state.outbox.length;
  useLayoutEffect(() => {
    const el = scroller.current;
    if (el && stick.current) el.scrollTop = el.scrollHeight;
  }, [newest, revision, outboxSize]);

  // ---- suggesting and sending (#2061) -------------------------------------------------------
  // The clock the countdown and the local expiry are read from. The owner decides; this only
  // stops offering a composer the owner would refuse.
  const [now, setNow] = useState(() => Date.now() / 1000);
  useEffect(() => {
    const timer = setInterval(() => setNow(Date.now() / 1000), 15_000);
    return () => clearInterval(timer);
  }, []);
  const expired = !!listed && listed.expires_at <= now;
  // A terminal is never written to (#2060): this pane is not drawn for one, and if a row ever
  // said so here, it would still offer no composer.
  const screenOnly = !!listed && shareKind(listed) === "terminal";
  const liveLevel = expired || screenOnly ? null : inputLevel(listed?.level);
  // The input level this pane last offered for THIS share: text typed under it is kept when the
  // level then ends.
  const [offer, setOffer] = useState<{ target: string; level: InputLevel | null }>({ target, level: null });
  if (offer.target !== target) setOffer({ target, level: liveLevel });
  else if (liveLevel && offer.level !== liveLevel) setOffer({ target, level: liveLevel });
  const offered = offer.target === target ? offer.level : null;

  // What is being typed: this component's state, keyed by the share it is typed for. Never
  // written to local or session storage.
  const [draft, setDraft] = useState({ target, text: "" });
  const text = draft.target === target ? draft.text : "";
  // The retry key of the message in the composer: one UUID per composed message (`draftKey`).
  const composing = useRef<{ target: string; text: string; key: string } | null>(null);
  // The words of this pane's own suggestions, by retry key: memory only, for "Your suggestions"
  // (the owner lists ids and states, never text).
  const words = useRef<{ target: string; byKey: Map<string, string> }>({ target, byKey: new Map() });
  if (words.current.target !== target) words.current = { target, byKey: new Map() };
  const [inFlight, setInFlight] = useState<{ target: string; generation: number } | null>(null);
  const sending = inFlight?.target === target && inFlight.generation === generation;
  const [flash, setFlash] = useState<{ target: string; generation: number; text: string } | null>(null);
  const notice = flash?.target === target && flash.generation === generation ? flash.text : null;

  const composer = composerState({
    status: state.status,
    peer,
    kind: listed?.kind ?? null,
    level: listed?.level ?? null,
    offered,
    expired,
    refused: state.refusedRevision !== null && state.refusedRevision === (listed?.revision ?? null),
    held: state.held,
    hasText: text.length > 0,
  });
  const canWrite = state.status === "live" && liveLevel !== null;

  const loadSuggestions = useCallback(async () => {
    const signal = signalRef.current;
    if (!signal || signal.aborted) return;
    try {
      const answer = await api.federationRemoteSuggestions(peerId, grantId, signal);
      if (!signal.aborted) dispatch({ type: "suggestions", generation, rows: answer?.suggestions });
    } catch {
      /* the pane's own reads say when the share is gone or the peer offline */
    }
  }, [peerId, grantId, generation]);

  // The list is read when the pane is live and can write, and again when the conversation moves.
  useEffect(() => {
    if (canWrite) void loadSuggestions();
  }, [canWrite, revision, loadSuggestions]);
  const anyOpen = (state.suggestions ?? []).some((s) => s.state === "pending" || s.state === "approving");
  useEffect(() => {
    if (!canWrite || !anyOpen) return;
    const timer = setInterval(() => void loadSuggestions(), SUGGESTION_POLL_OWN_MS);
    return () => clearInterval(timer);
  }, [canWrite, anyOpen, loadSuggestions]);

  // `held` is what the owner answered to ONE call. It is asked again (by the operator pressing
  // the button) once the conversation moved or a minute passed; nothing is resubmitted here.
  const held = state.held;
  const heldAt = useRef<number | null | undefined>(undefined);
  useEffect(() => {
    if (!held) {
      heldAt.current = undefined;
      return;
    }
    if (heldAt.current === undefined) heldAt.current = revision ?? null;
    else if (heldAt.current !== (revision ?? null)) {
      dispatch({ type: "held", generation, held: false });
      return;
    }
    const timer = setTimeout(() => dispatch({ type: "held", generation, held: false }), 60_000);
    return () => clearTimeout(timer);
  }, [held, revision, generation]);

  /** ONE message, ONE request. Nothing here retries: a retry is the operator pressing a button,
   *  and it carries the same key only when it is the same message. */
  const submit = async (again?: OutboxRow) => {
    const kind = again ? "send" : composer.kind;
    const message = again ? again.text : text;
    if (!kind || message === null || !message.trim() || sending) return;
    if (!again && !composer.enabled) return;
    const g = generation;
    const t = target;
    let key: string;
    if (again) key = again.client_key;
    else {
      const held_ = composing.current?.target === t ? composing.current : null;
      const next = draftKey(held_, message, uuid);
      composing.current = { target: t, ...next };
      key = next.key;
    }
    setInFlight({ target: t, generation: g });
    setFlash(null);
    // The content-free record: which request is in flight, so a reload can ask about it.
    pendingOps.add({ peer_id: peerId, grant_id: grantId, client_key: key, kind, started_at: Date.now() });
    if (kind === "send") dispatch({ type: "sending", generation: g, client_key: key, text: message });
    /** The message was accepted: it is no longer the composer's. Only under the generation it
     *  was sent in, and only if the box still holds exactly that text. */
    const accepted = () => {
      if (composing.current?.key === key) composing.current = null;
      setDraft((d) => (d.target === t && d.text === message ? { target: t, text: "" } : d));
    };
    try {
      if (kind === "send") {
        const answer = await api.federationRemoteSend(peerId, grantId, { client_key: key, text: message });
        pendingOps.remove(key);
        if (!current(g, t)) return; // a late success: the pane it was for is gone
        dispatch({ type: "sent", generation: g, client_key: key, handoff: answer?.handoff });
        accepted();
      } else {
        const answer = await api.federationRemoteSuggest(peerId, grantId, { client_key: key, text: message });
        pendingOps.remove(key);
        if (!current(g, t)) return;
        words.current.byKey.set(key, message);
        dispatch({ type: "suggested", generation: g, client_key: key, suggestion: answer, now: Date.now() });
        accepted();
      }
    } catch (e) {
      const failure = writeFailure(statusOf(e), codeOf(e));
      if (settlesRecord(failure)) pendingOps.remove(key);
      if (!current(g, t)) return;
      const say = (line: string) => setFlash({ target: t, generation: g, text: line });
      if (failure === "unconfirmed") {
        if (kind === "send") {
          // No usable answer: it may have arrived. The row says so, and offers no resend.
          dispatch({ type: "unconfirmed", generation: g, client_key: key });
          accepted();
        } else {
          say(SUGGEST_UNCONFIRMED);
          void loadSuggestions();
        }
        return;
      }
      // A definite refusal: nothing was accepted, and the text stays in the box.
      if (kind === "send" && !again) dispatch({ type: "unsent", generation: g, client_key: key });
      if (again) dispatch({ type: "sent", generation: g, client_key: key, handoff: "not_sent" });
      if (failure === "refused") {
        dispatch({ type: "refused", generation: g, revision: listed?.revision ?? null });
        void refreshRemoteShares();
      } else if (failure === "held") dispatch({ type: "held", generation: g, held: true });
      else if (failure === "offline") dispatch({ type: "failed", generation: g, status: 503 });
      else
        say(
          (e instanceof ApiError && e.message) ||
            (kind === "send" ? "Couldn’t send. Nothing was sent." : "Couldn’t suggest that. Nothing was sent."),
        );
    } finally {
      setInFlight((f) => (f?.target === t && f.generation === g ? null : f));
    }
  };

  const [withdrawing, setWithdrawing] = useState<string | null>(null);
  const withdraw = async (suggestionId: string) => {
    if (withdrawing) return;
    const g = generation;
    const t = target;
    setWithdrawing(suggestionId);
    try {
      const answer = await api.federationRemoteWithdraw(peerId, grantId, suggestionId);
      if (current(g, t)) dispatch({ type: "withdrawn", generation: g, suggestion: answer, now: Date.now() });
    } catch {
      // Decided first, or no longer ours to withdraw: the list says what it is now.
      if (current(g, t)) void loadSuggestions();
    } finally {
      setWithdrawing((w) => (w === suggestionId ? null : w));
    }
  };

  // ---- a reload in the middle of a request: find out, never resubmit -------------------------
  const [wake, setWake] = useState(0);
  const leftovers = state.leftovers;
  const live = state.status === "live";
  const check = useCallback(
    (clientKey: string, kind: "send" | "suggest") => {
      const signal = signalRef.current;
      if (!signal || signal.aborted) return;
      const g = generation;
      const t = target;
      dispatch({ type: "checking", generation: g, client_key: clientKey });
      api.federationRemoteOperation(peerId, grantId, clientKey, signal).then(
        (result) => {
          if (signal.aborted || !current(g, t)) return;
          if (result?.known === true) {
            pendingOps.remove(clientKey); // accounted for
            if (kind === "suggest") void loadSuggestions();
          }
          dispatch({ type: "status", generation: g, client_key: clientKey, result, now: Date.now() });
        },
        () => {
          // Offline, refused, an error: not "it did not arrive". The record stays.
          if (signal.aborted || !current(g, t)) return;
          dispatch({ type: "status", generation: g, client_key: clientKey, result: "failed", now: Date.now() });
        },
      );
    },
    [peerId, grantId, generation, target, loadSuggestions],
  );
  useEffect(() => {
    if (!live || leftovers.length === 0) return; // not reachable: asked again when it is
    const plan = reconcilePlan(leftovers, Date.now());
    for (const key of plan.giveUp) {
      // Two minutes of unknown: the notice stays as written, the record goes.
      pendingOps.remove(key);
      dispatch({ type: "giveup", generation, client_key: key });
    }
    for (const key of plan.check) {
      const l = leftovers.find((x) => x.client_key === key);
      if (l) check(key, l.kind);
    }
    if (plan.wakeIn === null) return;
    const timer = setTimeout(() => setWake((n) => n + 1), plan.wakeIn + 25);
    return () => clearTimeout(timer);
  }, [live, leftovers, generation, check, wake]);
  // The conversation changed: an unknown key may have been admitted meanwhile. Ask again.
  const seenRevision = useRef<{ target: string; revision: number | null | undefined }>({ target, revision });
  useEffect(() => {
    const seen = seenRevision.current;
    seenRevision.current = { target, revision };
    if (seen.target !== target || seen.revision === undefined || seen.revision === revision) return;
    if (revision === undefined) return;
    for (const l of leftovers) if (l.phase === "unknown") check(l.client_key, l.kind);
    // eslint-disable-next-line react-hooks/exhaustive-deps -- only a moved conversation asks
  }, [revision, target]);
  const notices = reloadNotices(leftovers, state.status);

  const badge = remotePaneBadge(state.status, state.stalls > 0);
  const until = listed && state.status !== "gone" ? grantEnds(listed.expires_at, now) : null;
  const access = listed && state.status !== "gone" ? remoteAccessWords(expired ? "view" : listed.level) : null;

  const composerView = composer.kind && (
    <>
      {composer.note && (
        <p className={styles.paneNote} role="status" data-testid="remote-composer-note">
          {composer.note}
        </p>
      )}
      {notice && (
        <p className={styles.paneNote} role="alert" data-testid="remote-composer-error">
          {notice}
        </p>
      )}
      <form
        className={`${styles.composer} ${composer.enabled ? "" : styles.composerOff}`}
        aria-label={composer.kind === "send" ? `Message ${peer}'s agent` : `Suggest a message to ${peer}'s operator`}
        data-testid="remote-composer"
        data-kind={composer.kind}
        onSubmit={(e) => {
          e.preventDefault();
          void submit();
        }}
      >
        <textarea
          className={styles.composerText}
          rows={1}
          value={text}
          placeholder={composer.placeholder}
          aria-label={composer.placeholder}
          // Disabled, never emptied: what was typed stays in the box.
          disabled={!composer.enabled}
          autoComplete="off"
          data-testid="remote-composer-text"
          onChange={(e) => {
            setDraft({ target, text: e.target.value });
            if (flash) setFlash(null);
          }}
          onKeyDown={(e) => {
            if (e.key === "Enter" && !e.shiftKey && !e.nativeEvent.isComposing) {
              e.preventDefault();
              void submit();
            }
          }}
        />
        <button
          type="submit"
          className={`${btn.primary} ${styles.composerGo}`}
          disabled={!composer.enabled || sending || !text.trim()}
          data-testid="remote-composer-go"
        >
          {sending ? (composer.kind === "send" ? "Sending…" : "Suggesting…") : composer.button}
        </button>
      </form>
    </>
  );

  if (state.status === "gone")
    return (
      <RemoteFrame
        title={title}
        peer={facts?.peer ?? null}
        word={badge.word}
        empty={REMOTE_GONE}
        notices={notices}
        foot={composerView}
      />
    );
  if (state.status === "offline")
    return (
      <RemoteFrame
        title={title}
        peer={facts?.peer ?? null}
        access={access ?? undefined}
        until={until}
        word={badge.word}
        empty={remoteOfflineText(peer)}
        notices={notices}
        foot={composerView}
      />
    );

  const snap = state.snapshot;
  const byTurn = new Map(state.outbox.filter((r) => r.turn).map((r) => [r.turn!, r]));
  const loose = state.outbox.filter((r) => !r.turn);
  const mine = state.suggestions ?? [];
  return (
    <RemoteFrame
      title={title}
      peer={facts?.peer ?? null}
      access={access ?? undefined}
      until={until}
      led={badge.led}
      word={badge.word}
      notices={notices}
      foot={
        <>
          {mine.length > 0 && (
            <section className={styles.mine} aria-labelledby="remote-mine" data-testid="remote-suggestions">
              <h2 id="remote-mine" className={styles.mineHead}>
                Your suggestions
              </h2>
              <ul>
                {mine.map((s) => {
                  const line = ownSuggestionLine(s.state, peer);
                  const said = words.current.byKey.get(s.client_key);
                  return (
                    <li key={s.suggestion_id} data-testid="remote-suggestion" data-state={s.state}>
                      <span className={styles.mineMain}>
                        <span className={styles.mineText}>
                          {said ?? `Suggested at ${shortTime(s.created_at)}`}
                        </span>
                        <span className={styles.mineState}>
                          <Led tone={line.tone === "ok" ? "up" : "idle"} />
                          <span className={line.tone === "ok" ? styles.wordOk : undefined}>{line.word}</span>
                        </span>
                      </span>
                      {line.withdraw && (
                        <button
                          type="button"
                          className={`${btn.ghost} ${btn.compact} ${styles.withdraw}`}
                          disabled={withdrawing !== null}
                          aria-label={`Withdraw: ${said ?? "this suggestion"}`}
                          onClick={() => void withdraw(s.suggestion_id)}
                        >
                          {withdrawing === s.suggestion_id ? "Withdrawing…" : "Withdraw"}
                        </button>
                      )}
                    </li>
                  );
                })}
              </ul>
            </section>
          )}
          {composerView || (
            <p className={styles.viewOnly} data-testid="remote-view-only">
              <b>View only.</b> {remoteViewOnlyText(peer)}
            </p>
          )}
        </>
      }
    >
      <div
        ref={scroller}
        className={styles.transcript}
        data-testid="remote-transcript"
        onScroll={(e) => {
          const el = e.currentTarget;
          stick.current = el.scrollHeight - el.scrollTop - el.clientHeight < 48;
        }}
      >
        {!snap && (
          <p className={styles.note} role="status">
            Loading the conversation…
          </p>
        )}
        {snap && state.hasMore && (
          <div className={styles.older}>
            <button
              type="button"
              className={`${btn.ghost} ${btn.compact}`}
              disabled={historyBusy}
              data-testid="remote-older"
              onClick={() => void loadOlder()}
            >
              {historyBusy ? "Loading…" : "Show earlier messages"}
            </button>
          </div>
        )}
        {snap && turns.length === 0 && loose.length === 0 && (
          <p className={viewer.muted + " " + styles.note}>No messages yet.</p>
        )}
        {turns.map((t) => {
          const row = byTurn.get(t.turn_id);
          return (
            // The viewer's own turn component, fed the checked projection. `images={false}`: an
            // attachment from a peer is never loaded, whatever it carries; it shows as its name.
            <ShareTurn
              key={t.turn_id}
              turn={remoteTurnView(t)}
              images={false}
              // The same guarded resend as a loose row: `Delivery` draws it for `not_sent`
              // only, and it is the same message under the same key (#2061).
              foot={row && <Delivery row={row} busy={sending} onResend={() => void submit(row)} />}
            />
          );
        })}
        {loose.map((row) => (
          // A message sent from here that the conversation does not show (yet): this pane's own
          // words from memory, or none at all for one found again after a reload.
          <section key={row.client_key} className={viewer.turn} data-testid="remote-own-message">
            <div className={viewer.block} data-author="peer">
              <Avatar spec={{ kind: "peer" }} className={viewer.avatar} />
              <p className={viewer.who}>You · peer</p>
              <p className={row.text === null ? `${viewer.text} ${viewer.muted}` : viewer.text}>
                {row.text ?? "A message you sent before the reload."}
              </p>
              <Delivery row={row} busy={sending} onResend={() => void submit(row)} />
            </div>
          </section>
        ))}
        {(snap?.pending_requests ?? []).map((q, i) => (
          <ShareApproval key={i} request={{ summary: typeof q?.summary === "string" ? q.summary : "" }} />
        ))}
      </div>
    </RemoteFrame>
  );
}
