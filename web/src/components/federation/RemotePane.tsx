import { useCallback, useEffect, useLayoutEffect, useReducer, useRef, useState } from "react";
import { refreshRemoteShares, useRemoteShares } from "../../app/remoteShares";
import { ApiError, api } from "../../lib/api";
import {
  REMOTE_EVENTS_WAIT_S,
  REMOTE_GONE,
  REMOTE_RETRY_MS,
  abortablePause as pause,
  remoteOfflineText,
  remotePaneBadge,
  remotePaneInit,
  remotePaneReduce,
  remoteTurnView,
  remoteTurns,
  remoteViewOnlyText,
  shortDate,
} from "../../lib/federation";
import { ShareApproval, ShareTurn } from "../../share/Turns";
import viewer from "../../share/ShareViewer.module.css";
import btn from "../ui/actionButton.module.css";
import { RemoteFrame } from "./RemoteFrame";
import styles from "./Remote.module.css";

const statusOf = (e: unknown) => (e instanceof ApiError ? e.status : 0);

/** The read-only pane.
 *
 *  **Every read is fenced.** Snapshot, events and history requests carry the generation they
 *  started under and the `AbortSignal` of that generation. The reducer (`remotePaneReduce`) bumps
 *  the generation whenever the pane clears its content (404, 503, a new peer or grant) and drops
 *  an answer from any other generation; the effect below aborts the old generation's requests
 *  when it changes and when the pane unmounts (leaving discards the state with the component). So a slow success can never refill a pane that was
 *  cleared or now shows another session.
 *
 *  **Nothing stale, nothing kept.** The turns live in this component's state and nowhere else:
 *  no storage, no retained list. 503 drops them and says the peer is not connected, then asks
 *  again until it is back; 404 drops them for good. */
export function RemotePane({ peerId, grantId }: { peerId: string; grantId: string }) {
  const target = `${peerId}/${grantId}`;
  const [state, dispatch] = useReducer(remotePaneReduce, target, remotePaneInit);
  // A new peer or grant on the SAME mounted route: clear before anything of the old one is
  // painted (a render-phase update; React re-renders at once, before the commit).
  if (state.target !== target) dispatch({ type: "open", target });

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
  useLayoutEffect(() => {
    const el = scroller.current;
    if (el && stick.current) el.scrollTop = el.scrollHeight;
  }, [newest, revision]);

  const badge = remotePaneBadge(state.status, state.stalls > 0);
  const until = listed && state.status !== "gone" ? shortDate(listed.expires_at) : null;

  if (state.status === "gone")
    return <RemoteFrame title={title} peer={facts?.peer ?? null} word={badge.word} empty={REMOTE_GONE} />;
  if (state.status === "offline")
    return (
      <RemoteFrame
        title={title}
        peer={facts?.peer ?? null}
        until={until}
        word={badge.word}
        empty={remoteOfflineText(peer)}
      />
    );

  const snap = state.snapshot;
  return (
    <RemoteFrame
      title={title}
      peer={facts?.peer ?? null}
      until={until}
      led={badge.led}
      word={badge.word}
      foot={
        <p className={styles.viewOnly} data-testid="remote-view-only">
          <b>View only.</b> {remoteViewOnlyText(peer)}
        </p>
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
        {snap && turns.length === 0 && <p className={viewer.muted + " " + styles.note}>No messages yet.</p>}
        {turns.map((t) => (
          // The viewer's own turn component, fed the checked projection. `images={false}`: an
          // attachment from a peer is never loaded, whatever it carries; it shows as its name.
          <ShareTurn key={t.turn_id} turn={remoteTurnView(t)} images={false} />
        ))}
        {(snap?.pending_requests ?? []).map((q, i) => (
          <ShareApproval key={i} request={{ summary: typeof q?.summary === "string" ? q.summary : "" }} />
        ))}
      </div>
    </RemoteFrame>
  );
}
