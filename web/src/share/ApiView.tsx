import { useCallback, useEffect, useLayoutEffect, useRef, useState } from "react";
import {
  PublicError,
  publicApi,
  type PublicSnapshot,
  type PublicTurn,
  type Unlocked,
  type VisitorShare,
} from "./publicApi";
import { Composer } from "./Composer";
import { Bars, ShareApproval, ShareTurn } from "./Turns";
import styles from "./ShareViewer.module.css";

/** While a turn runs the journal's revision is read this often (the operator pane's cadence). */
const ACTIVE_POLL_MS = 1_000;
const IDLE_POLL_MS = 10_000;
const MAX_BACKOFF_MS = 15_000;
/** How often the view asks which link the cookie holds (another tab may have re-unlocked). */
const RECHECK_MS = 30_000;

function isActive(snap: PublicSnapshot | null): boolean {
  return (
    !!snap &&
    (snap.state === "running" ||
      snap.state === "awaiting_approval" ||
      snap.state === "queued" ||
      snap.state === "delivering")
  );
}

/** The first queued message: the only one Send now can push ahead (the worker refuses any other,
 *  #1451) — so it is the only one offered. */
function queueHead(turns: PublicTurn[]): string | null {
  return turns.find((t) => t.kind !== "agent" && t.state === "queued")?.turn_id ?? null;
}

/** The live, redacted view of an API session (#1404 P4): the projection `GET /api/public/session`
 *  serves, re-read whenever `GET /api/public/events` reports a new revision — the events carry
 *  `{cursor, kind}` only, never data. In a `send` link the composer sits below. */
export function ApiView({
  unlocked,
  onPresent,
  onShare,
  onGone,
  onExpired,
  onRecheck,
}: {
  unlocked: Unlocked;
  /** How many have the session open (#1931), for the top bar; null until known. */
  onPresent?: (n: number | null) => void;
  onShare: (share: VisitorShare) => void;
  onGone: () => void;
  onExpired: () => void;
  /** Re-read `GET /api/public/share`: the cookie may name a new CSRF token, or another link. */
  onRecheck: () => Promise<void>;
}) {
  const [snap, setSnap] = useState<PublicSnapshot | null>(null);
  const [reconnecting, setReconnecting] = useState(false);
  const [actionError, setActionError] = useState<string | null>(null);
  const [pushing, setPushing] = useState<string | null>(null);
  const cursor = useRef(-1);
  const scroller = useRef<HTMLDivElement | null>(null);
  const stick = useRef(true);
  const { share, csrf } = unlocked;
  /** Every answer is checked against the link this view was opened for: an answer for another
   *  link (another tab unlocked it, replacing the browser's one share cookie) is never shown. The
   *  parent re-reads the link and remounts this view for whatever the cookie names now — a fresh
   *  snapshot and cursor, never one session's revision compared with another's. */
  const foreign = useCallback(
    (link: string | undefined) => {
      if (link === share.link) return false;
      setReconnecting(true); // until the re-read lands, what is on screen may be stale
      void onRecheck();
      return true;
    },
    [share.link, onRecheck],
  );

  /** Any refusal that means "this link is over" ends the view; anything else is retried. */
  const refused = useCallback(
    (e: unknown): boolean => {
      if (e instanceof PublicError && e.status === 404) {
        onGone();
        return true;
      }
      if (e instanceof PublicError && e.status === 401) {
        onExpired();
        return true;
      }
      return false;
    },
    [onGone, onExpired],
  );

  const load = useCallback(async () => {
    const s = await publicApi.session();
    if (foreign(s.link)) return;
    // An older answer landing after a newer one must not roll the view back.
    if (s.revision < cursor.current) return;
    cursor.current = s.revision;
    setSnap(s);
  }, [foreign]);

  useEffect(() => {
    let alive = true;
    publicApi.session().then(
      (s) => {
        if (!alive || foreign(s.link)) return;
        cursor.current = s.revision;
        setSnap(s);
      },
      (e: unknown) => {
        if (alive && !refused(e)) setReconnecting(true);
      },
    );
    return () => {
      alive = false;
    };
  }, [refused, foreign]);

  const active = isActive(snap);
  useEffect(() => {
    let alive = true;
    let timer: ReturnType<typeof setTimeout>;
    let backoff = 0;
    let ticks = 0;
    let checked = Date.now();
    const tick = async () => {
      ticks += 1;
      if (Date.now() - checked > RECHECK_MS) {
        checked = Date.now();
        await onRecheck();
        if (!alive) return;
      }
      try {
        const page = await publicApi.events(Math.max(0, cursor.current));
        if (!alive || foreign(page.link)) return;
        backoff = 0;
        setReconnecting(false);
        // A changed journal re-reads at once; lifecycle facts that write no journal record are
        // caught by a full re-read every idle tick and every 5th active one (as the owner's pane).
        if (page.revision !== cursor.current || !active || ticks % 5 === 0) await load();
      } catch (e) {
        if (!alive || refused(e)) return;
        if (e instanceof PublicError && e.status === 409) {
          // The cursor ran ahead of the journal (the session restarted): re-read it whole, as
          // the operator's pane does.
          cursor.current = -1;
          await load().catch(() => setReconnecting(true));
        } else {
          setReconnecting(true);
          backoff = Math.min(MAX_BACKOFF_MS, (backoff || 1_000) * 2);
        }
      }
      if (alive) timer = setTimeout(tick, backoff || (active ? ACTIVE_POLL_MS : IDLE_POLL_MS));
    };
    timer = setTimeout(tick, active ? ACTIVE_POLL_MS : IDLE_POLL_MS);
    return () => {
      alive = false;
      clearTimeout(timer);
    };
  }, [active, load, refused, onRecheck, foreign]);

  // Follow the conversation while the visitor is at the bottom; leave them where they scrolled.
  useLayoutEffect(() => {
    const el = scroller.current;
    if (el && stick.current) el.scrollTop = el.scrollHeight;
  }, [snap]);

  const act = async (fn: () => Promise<unknown>) => {
    setActionError(null);
    try {
      await fn();
    } catch (e) {
      if (refused(e)) return;
      setActionError(e instanceof PublicError ? e.message : "That didn’t go through — try again.");
      return;
    }
    // The action was taken; a failed re-read is the poll's to retry, not the action's failure.
    await load().catch(() => setReconnecting(true));
  };

  const sendNow = (turnId: string) => {
    setPushing(turnId);
    void act(() => publicApi.sendNow(csrf, turnId)).finally(() => setPushing(null));
  };

  const present = typeof snap?.present === "number" ? snap.present : null;
  useEffect(() => {
    onPresent?.(present);
  }, [present, onPresent]);
  useEffect(() => () => onPresent?.(null), [onPresent]);

  const turns = snap?.turns ?? [];
  const head = queueHead(turns);
  // Background work shows as its own row only when no turn shows it already: a turn the agent
  // runs for itself carries its own steps (#1438), as in the operator pane.
  const background = !!snap?.background && !snap.active_turn && !turns.some((t) => t.state === "running");
  const working = snap?.state === "running" || !!snap?.active_turn || background;

  return (
    <>
      <div className={styles.status}>
        <span
          className={styles.led}
          data-state={working ? "up" : "idle"}
          aria-hidden="true"
        />
        <span>{working ? "WORKING" : snap ? "IDLE" : "…"}</span>
        {reconnecting && <span className={styles.reconnecting}>reconnecting…</span>}
      </div>
      <main
        ref={scroller}
        className={styles.transcript}
        data-testid="share-transcript"
        onScroll={(e) => {
          const el = e.currentTarget;
          stick.current = el.scrollHeight - el.scrollTop - el.clientHeight < 48;
        }}
      >
        {!snap && !reconnecting && (
          <p className={styles.muted} role="status">
            Loading the conversation…
          </p>
        )}
        {snap && snap.omitted_turns > 0 && (
          <p className={styles.omitted}>
            {snap.omitted_turns} earlier message{snap.omitted_turns === 1 ? "" : "s"} not shown
          </p>
        )}
        {snap && turns.length === 0 && <p className={styles.muted}>No messages yet.</p>}
        {turns.map((t) => (
          <ShareTurn
            key={t.turn_id}
            turn={t}
            operatorAvatar={snap?.operator_avatar}
            operatorName={snap?.operator_name}
            queueAction={
              t.mine === true &&
              t.turn_id === head &&
              // Already on its way past the running turn (or steered in): the worker
              // would refuse a second push, as the operator pane knows too.
              (!t.delivery || t.delivery === "interrupt_failed") &&
              share.mode === "send" &&
              snap?.can_send ? (
                <button
                  type="button"
                  className={styles.ghost}
                  disabled={pushing === t.turn_id}
                  onClick={() => sendNow(t.turn_id)}
                >
                  Send now
                </button>
              ) : null
            }
          />
        ))}
        {background && (
          <div className={`${styles.wait} ${styles.backgroundRow}`} role="status" data-testid="share-background-work">
            <Bars />
            <span className={styles.waitText}>A background task is running</span>
          </div>
        )}
        {(snap?.pending_requests ?? []).map((q) => (
          <ShareApproval key={q.request_id} request={q} />
        ))}
      </main>
      {actionError && (
        <p className={`${styles.error} ${styles.footError}`} role="alert">
          {actionError}
        </p>
      )}
      {share.mode === "send" && snap && (
        <Composer
          share={share}
          csrf={csrf}
          snap={snap}
          onShare={onShare}
          onSent={() => load().catch(() => setReconnecting(true))}
          onRecheck={onRecheck}
          refused={refused}
        />
      )}
      {share.mode === "view" && (
        <footer className={styles.viewOnly} data-testid="share-view-only">
          <b>VIEW ONLY</b> · You can watch this conversation, not send to it.
        </footer>
      )}
    </>
  );
}
