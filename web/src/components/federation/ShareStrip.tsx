import { StickyNote } from "lucide-react";
import { useEffect, useLayoutEffect, useReducer, useRef, useState } from "react";
import { useSearchParams } from "react-router-dom";
import { api } from "../../lib/api";
import type { FederationGrant } from "../../lib/federation";
import {
  NOTES_QUERY,
  STRIP_POLL_MS,
  noteCountWords,
  notesInit,
  notesReduce,
  stripState,
  threadOf,
  watchingLine,
  type StripRow,
} from "../../lib/federationTogether";
import { nowSeconds } from "../../lib/shareFormat";
import { Led } from "../hud/Meters";
import { AnchoredMenu } from "../ui/AnchoredMenu";
import btn from "../ui/actionButton.module.css";
import menu from "../ui/menu.module.css";
import { NotesDrawer } from "./NotesDrawer";
import { PeerBadge } from "./PeerBadge";
import styles from "./Together.module.css";
import { useAnswerFence } from "./useAnswerFence";

/** A call that never throws synchronously, whatever `api` is in a test double. */
const attempt = <T,>(call: () => Promise<T>): Promise<T> => Promise.resolve().then(call);

/** The owner's share strip (#2062), under the head of an API pane whose session has a live grant:
 *  who it is shared with, at which level and until when, and whether that operator is looking.
 *
 *  **Presence is a name and one bit.** `<peer> is watching` shows while the grants read says
 *  `watching`, and goes when it clears. No device, no count, no time, nothing kept.
 *
 *  **One notes thread per grant, chosen explicitly.** Each row has its own Notes control; the
 *  collapsed strip's control opens a picker that lists each peer. The open thread is a grant id,
 *  and the drawer is keyed by it, so a draft for one peer is never in the box for another.
 *
 *  With no live grant it renders nothing. */
export function ShareStrip({
  sessionKey,
  sharesOpen,
  anchor,
  onOpenShare,
  onGrants,
}: {
  sessionKey: string;
  /** The share dialog is open: when it closes the grants are read again at once. */
  sharesOpen: boolean;
  /** The pane the notes drawer sits over. */
  anchor: HTMLElement | null;
  onOpenShare: (trigger: HTMLElement | null) => void;
  /** The live grants, for what else in the pane depends on them (who may answer a request). */
  onGrants?: (grants: FederationGrant[]) => void;
}) {
  const [grants, setGrants] = useState<FederationGrant[]>([]);
  const [now, setNow] = useState(nowSeconds);
  const [open, setOpen] = useState<boolean | null>(null);
  const [notes, dispatch] = useReducer(notesReduce, notesInit);
  const [thread, setThread] = useState<{ grantId: string; peer: string; trigger: HTMLElement | null } | null>(
    null,
  );
  const [epoch, setEpoch] = useState(0);
  // The epoch of the latest render, for a note write whose answer outlives its drawer.
  const epochNow = useRef(epoch);
  useLayoutEffect(() => {
    epochNow.current = epoch;
  });
  const tell = useRef(onGrants);
  useLayoutEffect(() => {
    tell.current = onGrants;
  });
  // Whether this strip is mounted for THIS session: a fence armed in an effect body, so
  // StrictMode's replay arms it again (`useAnswerFence`, #2061). Declared before the read.
  const ticket = useAnswerFence(sessionKey);

  // Another session in the same pane: nothing of the old one stays, drafts included.
  const [shown, setShown] = useState(sessionKey);
  if (shown !== sessionKey) {
    setShown(sessionKey);
    setGrants([]);
    setThread(null);
    setOpen(null);
    setEpoch((n) => n + 1);
    dispatch({ type: "reset" });
  }

  // The grants read: on mount, every few seconds (the `watching` bit lasts 30 s on the server),
  // and again at once when the share dialog or a thread opens or closes (a grant or a count may
  // have changed). Fenced by the session: an answer for another session's pane is dropped.
  const threadOpen = thread !== null;
  useEffect(() => {
    // One controller per run of this effect: its cleanup (a session change, the dialog or a
    // thread opening or closing, the unmount, StrictMode's replay) stops this run's answers.
    const ctl = new AbortController();
    const current = ticket();
    // Reads of one run can overlap (a slow answer, the next tick): each takes a number when it
    // starts, and one that lands after a later one was applied is dropped.
    const order = { started: 0, applied: 0 };
    const read = async () => {
      const n = ++order.started;
      try {
        const answer = await attempt(() => api.federationGrants(sessionKey));
        if (ctl.signal.aborted || !current() || n < order.applied) return;
        order.applied = n;
        const list = Array.isArray(answer?.grants) ? answer.grants : [];
        setGrants(list);
        setNow(nowSeconds());
        tell.current?.(list);
      } catch {
        /* the strip keeps what it showed; the next read corrects it */
      }
    };
    void read();
    const timer = setInterval(() => void read(), STRIP_POLL_MS);
    return () => {
      ctl.abort();
      clearInterval(timer);
    };
  }, [sessionKey, sharesOpen, threadOpen, ticket]);

  const strip = stripState(grants, now, open);
  const rows = strip.rows;

  // A bell entry for a note names its grant (`?notes=<grant id>`): that thread opens, once.
  const [params, setParams] = useSearchParams();
  const wanted = params.get(NOTES_QUERY);
  const [handled, setHandled] = useState<string | null>(null);
  const linked = wanted && wanted !== handled ? rows.find((r) => r.grantId === wanted) : undefined;
  if (wanted && linked) {
    setHandled(wanted);
    setThread({ grantId: linked.grantId, peer: linked.peer, trigger: null });
  } else if (!wanted && handled) setHandled(null);
  useEffect(() => {
    if (!handled || params.get(NOTES_QUERY) !== handled) return;
    const next = new URLSearchParams(params);
    next.delete(NOTES_QUERY);
    setParams(next, { replace: true });
  }, [handled, params, setParams]);

  if (rows.length === 0 && !thread) return null;

  const notesButton = (row: StripRow) => (
    <button
      type="button"
      className={`${btn.ghost} ${btn.compact} ${styles.stripTool}`}
      aria-label={`Notes with ${row.peer}`}
      data-testid="strip-notes"
      data-grant={row.grantId}
      onClick={(e) => setThread({ grantId: row.grantId, peer: row.peer, trigger: e.currentTarget })}
    >
      <StickyNote size={13} aria-hidden="true" /> Notes
    </button>
  );
  const watching = (row: StripRow) =>
    row.watching && (
      <span className={styles.state} data-testid="strip-watching">
        <Led tone="up" />
        <span className={styles.wordOk}>{watchingLine(row.peer)}</span>
      </span>
    );

  return (
    <>
      {rows.length > 0 && (
        <div className={styles.strip} role="note" aria-label="Who this session is shared with" data-testid="share-strip">
          {rows.length === 1 ? (
            <div className={styles.stripRow} data-testid="strip-row" data-grant={rows[0].grantId}>
              <button
                type="button"
                className={styles.stripMain}
                data-testid="strip-open"
                onClick={(e) => onOpenShare(e.currentTarget)}
              >
                <span>Shared with</span> <PeerBadge name={rows[0].peer} />
                <span>
                  · {rows[0].access} · {rows[0].until.countdown ? <b>{rows[0].until.text}</b> : rows[0].until.text}
                </span>
              </button>
              {watching(rows[0])}
              {notesButton(rows[0])}
            </div>
          ) : (
            <>
              <div className={styles.stripRow}>
                <button
                  type="button"
                  className={`${styles.stripMain} ${strip.collapsed ? "" : styles.stripTitle}`}
                  data-testid="strip-open"
                  onClick={(e) => onOpenShare(e.currentTarget)}
                >
                  {strip.summary}
                </button>
                {
                  <button
                    type="button"
                    className={`${btn.ghost} ${btn.compact} ${styles.stripTool}`}
                    aria-expanded={!strip.collapsed}
                    data-testid="strip-toggle"
                    onClick={() => setOpen(strip.collapsed)}
                  >
                    {strip.collapsed ? "Show" : "Hide"}
                  </button>
                }
                {strip.collapsed && (
                  <AnchoredMenu
                    label="Notes: choose a peer"
                    trigger={
                      <>
                        <StickyNote size={13} aria-hidden="true" /> Notes
                      </>
                    }
                    triggerClassName={`${btn.ghost} ${btn.compact} ${styles.stripTool}`}
                    triggerTestId="strip-notes-pick"
                    menuTestId="strip-notes-picker"
                    focus="first-item"
                    portal
                    classes={{ wrap: styles.pickerWrap, panel: styles.pickerPanel, items: styles.pickerItems }}
                  >
                    {(close, trigger) =>
                      rows.map((row) => (
                        <button
                          key={row.grantId}
                          type="button"
                          role="menuitem"
                          className={menu.item}
                          data-grant={row.grantId}
                          onClick={() => {
                            close();
                            setThread({ grantId: row.grantId, peer: row.peer, trigger: trigger() });
                          }}
                        >
                          <PeerBadge name={row.peer} />
                          <span className={styles.pickerCount}>{noteCountWords(row.notes)}</span>
                        </button>
                      ))
                    }
                  </AnchoredMenu>
                )}
              </div>
              {!strip.collapsed &&
                rows.map((row) => (
                  <div key={row.grantId} className={styles.stripRow} data-testid="strip-row" data-grant={row.grantId}>
                    <span className={styles.stripMain}>
                      <PeerBadge name={row.peer} />
                      <span>
                        · {row.access} · {row.until.countdown ? <b>{row.until.text}</b> : row.until.text}
                      </span>
                    </span>
                    {watching(row)}
                    {notesButton(row)}
                  </div>
                ))}
            </>
          )}
        </div>
      )}
      {thread && (
        <NotesDrawer
          // Keyed by the grant: another peer's thread is another drawer, with its own box.
          key={thread.grantId}
          side="owner"
          grantId={thread.grantId}
          other={thread.peer}
          thread={threadOf(notes, thread.grantId)}
          dispatch={dispatch}
          epoch={epoch}
          stillCurrent={(e) => ticket()() && epochNow.current === e}
          anchor={anchor}
          onClose={() => setThread(null)}
          returnFocusTo={thread.trigger}
        />
      )}
    </>
  );
}
