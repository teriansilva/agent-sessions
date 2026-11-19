import { X } from "lucide-react";
import { useCallback, useEffect, useRef, useState } from "react";
import { createPortal } from "react-dom";
import { Link } from "react-router-dom";
import { ApiError, api } from "../../lib/api";
import {
  PEER_DISCLOSURE,
  PEER_DISCLOSURE_DETAIL,
  TERMINAL_LIMIT_DETAIL,
  TERMINAL_LIMIT_LEAD,
  TERMINAL_LIMIT_UNPROTECTED,
  TERMINAL_LIMIT_WORD,
  TERMINAL_NOT_SHOWN,
  TERMINAL_VIEW_ONLY,
  grantEnds,
  grantFor,
  grantRowState,
  levelLabel,
  peerRowState,
  shareablePeers,
  shortDate,
  type FederationGrant,
  type FederationState,
  type ShareKind,
} from "../../lib/federation";
import {
  GRANT_BUSY,
  GRANT_LEVELS,
  LEVEL_EXPIRIES,
  LEVEL_PRESELECTED,
  LEVEL_TILES,
  MISSION_HELD_LINE,
  SEND_ACK,
  SUGGEST_LINE,
  ackKey,
  acknowledged,
  canShare,
  expiryFor,
  isGrantBusy,
  isHeldRefusal,
  sendEndsText,
  sendRiskText,
  shareRequest,
  type GrantLevel,
} from "../../lib/federationInput";
import { nowSeconds } from "../../lib/shareFormat";
import { settingsPath } from "../../routes/settingsTabs";
import dlg from "../HudDialog.module.css";
import { Seg } from "../hud/Controls";
import { Led } from "../hud/Meters";
import { useFocusContainment } from "../pulse/useModalDrawer";
import btn from "../ui/actionButton.module.css";
import styles from "./SharePeer.module.css";
import { useAnswerFence } from "./useAnswerFence";

function message(e: unknown, fallback: string): string {
  return e instanceof ApiError && e.message ? e.message : fallback;
}

/** The one level a terminal is offered (#2060): its screen, to watch. */
const TERMINAL_LEVELS: readonly GrantLevel[] = ["view"];

/** What a refused write was, when it was the grant's fence being busy: retried by the operator. */
type Stuck = { what: "share" } | { what: "revoke"; grant: FederationGrant };

/** **Share with a peer…** (#2055, #2061): give ONE paired instance access to this API session
 *  (view, suggest or send) and see or end what is already shared. A sibling of
 *  `SharePubliclyModal`.
 *
 *  **View is what the dialog opens on, every time**, also for a session that already has a Send
 *  grant: Send is never preselected. **Send cannot be shared without the acknowledgement**: the
 *  red risk box says what it means, its checkbox is required, Share is disabled until it is
 *  ticked, and the request carries `accept_risk: true` only then (`shareRequest`). The tick
 *  belongs to one choice: another peer, another level or a reopened dialog starts unticked.
 *
 *  What the other instance will see is stated in the dialog at all times, never behind a toggle.
 *  On a phone the body scrolls and the action row stays at the sheet's floor
 *  (`HudDialog.module.css` `.pinned`), so Share is reachable under a long list.
 *
 *  **A terminal (`kind="terminal"`, #2060)** is shared as its screen. Its dialog says the limit
 *  of what is hidden FIRST, directly under the session line and above the peer picker, so it is
 *  on screen when the dialog or the phone sheet opens: the operator reads it before choosing
 *  anything. Access offers View alone, as the one thing a terminal can be shared for: the levels
 *  of #2061 (Suggest, Send), the Send risk box and its acknowledgement belong to an API session
 *  and are not shown for a terminal at all, and the request is always `view` without
 *  `accept_risk` (the server refuses anything else for a terminal for good). The grant is made
 *  with the terminal session's key; the server stores its kind. */
export function SharePeerModal({
  sessionKey,
  title,
  agent,
  missionHeld = false,
  kind = "api",
  onClose,
  returnFocusTo,
}: {
  sessionKey: string;
  title: string;
  /** The agent's label, for the badge beside the title. */
  agent: string;
  /** A mission holds this session: it can only be watched, so only View is offered. */
  missionHeld?: boolean;
  /** What is shared: an API session's conversation (the default) or a terminal's screen. */
  kind?: ShareKind;
  onClose: () => void;
  returnFocusTo?: HTMLElement | null;
}) {
  const dialogRef = useRef<HTMLDivElement | null>(null);
  const closeRef = useRef<HTMLButtonElement | null>(null);
  const [fed, setFed] = useState<FederationState | null>(null);
  const [grants, setGrants] = useState<FederationGrant[] | null>(null);
  const [loadError, setLoadError] = useState<string | null>(null);
  const [picked, setPicked] = useState<string | null>(null);
  // Always View when the dialog opens: nothing here reads an existing grant's level into it.
  const [level, setLevel] = useState<GrantLevel>(LEVEL_PRESELECTED);
  const [seconds, setSeconds] = useState<number | null>(null);
  // The choice (peer + level) the acknowledgement was ticked for; `null` = unticked.
  const [ackFor, setAckFor] = useState<string | null>(null);
  // The server said a mission holds the session (409 `held`), whatever the list said.
  const [heldSeen, setHeldSeen] = useState(false);
  const [stuck, setStuck] = useState<Stuck | null>(null);
  const [busy, setBusy] = useState<string | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [done, setDone] = useState<string | null>(null);
  const [now, setNow] = useState(nowSeconds);
  // An answer applies only while the dialog is open for the session it was asked for. Declared
  // before the loading effect, so it is armed when that runs (StrictMode replay included).
  const ticket = useAnswerFence(sessionKey);

  const loadGrants = useCallback(() => {
    const current = ticket();
    return api.federationGrants(sessionKey).then(
      (r) => {
        if (!current()) return;
        setGrants(Array.isArray(r?.grants) ? r.grants : []);
        setNow(nowSeconds());
      },
      (e: unknown) => {
        if (current()) setLoadError(message(e, "Couldn’t load this session’s shares."));
      },
    );
  }, [sessionKey, ticket]);

  useEffect(() => {
    const current = ticket();
    api.federation().then(
      (state) => {
        if (current()) setFed(state);
      },
      (e: unknown) => {
        if (current()) setLoadError(message(e, "Couldn’t load the paired instances."));
      },
    );
    void loadGrants();
  }, [loadGrants, ticket]);

  useEffect(() => {
    closeRef.current?.focus();
  }, []);
  useFocusContainment({ active: true, panelRef: dialogRef });
  useEffect(() => () => returnFocusTo?.focus?.(), [returnFocusTo]);
  useEffect(() => {
    const onKey = (e: KeyboardEvent) => {
      if (e.key === "Escape") {
        e.preventDefault();
        onClose();
      }
    };
    document.addEventListener("keydown", onKey);
    return () => document.removeEventListener("keydown", onKey);
  }, [onClose]);

  const peers = fed ? shareablePeers(fed.peers) : null;
  // The first peer is chosen until the operator picks one; a pick that is no longer listed (the
  // peer was revoked meanwhile) falls back the same way.
  const chosen = peers?.find((p) => p.peer_id === picked) ?? peers?.[0] ?? null;
  const live = grants ?? [];
  const existing = chosen ? grantFor(live, chosen.peer_id) : undefined;
  const held = missionHeld || heldSeen;
  // A terminal is shared as its screen, View only for good (#2060): the levels of #2061 are an API
  // session's. A mission-held session can only be watched. Either way, whatever was picked, the
  // choice is View, and so the request is `view` without `accept_risk`.
  const terminal = kind === "terminal";
  const chosenLevel: GrantLevel = held || terminal ? "view" : level;
  const expiry = expiryFor(chosenLevel, seconds);
  const peerId = chosen?.peer_id ?? null;
  const ticked = acknowledged(ackFor, peerId, chosenLevel);
  const choice = { peerId, level: chosenLevel, seconds: expiry.seconds, ackFor, missionHeld: held, kind };
  const shareable = canShare(choice);

  // A countdown moves: while a listed grant ends within a day, the clock is re-read.
  const counting = live.some((g) => grantEnds(g.expires_at, now).countdown);
  useEffect(() => {
    if (!counting) return;
    const timer = setInterval(() => setNow(nowSeconds()), 15_000);
    return () => clearInterval(timer);
  }, [counting]);

  /** A refused write: the fence being busy has its own state and a retry; a mission holding the
   *  session closes the two higher levels; anything else is the server's words. Never success. */
  const refused = (e: unknown, current: () => boolean, what: Stuck, fallback: string) => {
    if (!current()) return;
    const status = e instanceof ApiError ? e.status : 0;
    const body = e instanceof ApiError ? e.record : null;
    if (isGrantBusy(status, body)) return setStuck(what);
    if (isHeldRefusal(status, body)) {
      setHeldSeen(true);
      setAckFor(null);
      return;
    }
    setError(message(e, fallback));
  };

  const share = async () => {
    // The request is built from the SAME state the button was enabled by; with Send unticked
    // there is no request to make.
    const body = shareRequest(choice);
    if (!chosen || busy || !body) return;
    const current = ticket();
    setBusy("share");
    setError(null);
    setDone(null);
    setStuck(null);
    try {
      const { grant } = existing
        ? await api.changeFederationGrant(existing.grant_id, {
            ...body,
            revision: existing.revision,
          })
        : await api.createFederationGrant({
            session_key: sessionKey,
            peer_id: chosen.peer_id,
            ...body,
          });
      if (current()) {
        const until = grantEnds(grant.expires_at, nowSeconds());
        setDone(
          `Shared with ${chosen.name} (${levelLabel(grant.level)}) ${until.countdown ? `· ${until.text}` : until.text}.`,
        );
        // One acknowledgement, one grant: renewing or sharing again asks again.
        setAckFor(null);
      }
    } catch (e) {
      refused(e, current, { what: "share" }, "Couldn’t share this session.");
    } finally {
      // Whatever happened, the list is the server's: a refused change may mean it moved.
      await loadGrants();
      if (current()) setBusy(null);
    }
  };

  const revoke = async (grant: FederationGrant) => {
    if (busy) return;
    const current = ticket();
    setBusy(grant.grant_id);
    setError(null);
    setDone(null);
    setStuck(null);
    try {
      await api.revokeFederationGrant(grant.grant_id);
    } catch (e) {
      refused(e, current, { what: "revoke", grant }, "Couldn’t revoke that share.");
    } finally {
      await loadGrants();
      if (current()) setBusy(null);
    }
  };

  const titleId = "share-peer-title";
  const none = peers !== null && peers.length === 0;

  return createPortal(
    <div className={dlg.backdrop} onMouseDown={onClose}>
      <div
        ref={dialogRef}
        tabIndex={-1}
        role="dialog"
        aria-modal="true"
        aria-labelledby={titleId}
        className={`${dlg.dialog} ${dlg.pinned} ${styles.dialog}`}
        data-testid="share-peer"
        data-kind={kind}
        onMouseDown={(e) => e.stopPropagation()}
      >
        <div className={dlg.head}>
          <h2 id={titleId} className={dlg.tag}>
            Share with a peer
          </h2>
          <button
            ref={closeRef}
            type="button"
            className={`${dlg.close} ${styles.close}`}
            aria-label="Close"
            onClick={onClose}
          >
            <X size={14} aria-hidden="true" />
          </button>
        </div>
        <p className={`${dlg.from} ${styles.from}`}>
          <span className={dlg.fromLabel}>SESSION</span>
          <span className={dlg.fromTitle}>{title}</span>
          <span className={styles.tagBox}>{terminal ? "Terminal" : agent}</span>
        </p>

        <div className={`${dlg.sheetBody} ${styles.body}`} data-testid="share-peer-body">
          {/* A terminal: the limit comes first, whatever else the body goes on to show. */}
          {terminal && (
            <div className={styles.limit} role="note" data-testid="share-peer-limit">
              <span className={styles.limitWord}>
                <Led tone="att" />
                {TERMINAL_LIMIT_WORD}
              </span>
              {TERMINAL_LIMIT_LEAD} <b>{TERMINAL_LIMIT_UNPROTECTED}</b>
              <span className={styles.limitNotShown} data-testid="share-peer-not-shown">
                {TERMINAL_NOT_SHOWN}
              </span>
              <span className={styles.limitDetail}>{TERMINAL_LIMIT_DETAIL}</span>
            </div>
          )}
          {loadError && (
            <p className={dlg.error} role="alert">
              {loadError}
            </p>
          )}
          {peers === null && !loadError && (
            <p className={dlg.muted} role="status">
              Loading the paired instances…
            </p>
          )}
          {none && (
            <div className={styles.none} data-testid="share-peer-none">
              <p className={styles.noneHead}>No paired instances yet.</p>
              <p className={styles.help}>
                A session can be shared with an instance this one is paired with.
              </p>
              <Link className={styles.linkAction} to={settingsPath("peers")} onClick={onClose}>
                Open Settings → Peers
              </Link>
            </div>
          )}
          {peers !== null && peers.length > 0 && (
            <>
              <span
                className={`${dlg.label} ${styles.label} ${terminal ? "" : styles.first}`}
                id="share-peer-who"
              >
                Peer
              </span>
              <div className={styles.tiles} role="radiogroup" aria-labelledby="share-peer-who">
                {peers.map((p) => {
                  const row = peerRowState(p, fed?.owner ?? false, now);
                  return (
                    <button
                      key={p.peer_id}
                      type="button"
                      role="radio"
                      aria-checked={chosen?.peer_id === p.peer_id}
                      className={styles.tile}
                      data-peer={p.peer_id}
                      onClick={() => {
                        setPicked(p.peer_id);
                        // Another peer is another choice: its acknowledgement is not this one's.
                        setAckFor(null);
                        setDone(null);
                        setError(null);
                        setStuck(null);
                      }}
                    >
                      <span className={styles.tileBody}>
                        <span className={styles.tileName}>{p.name}</span>
                      </span>
                      <span className={styles.state} data-testid="share-peer-state">
                        <Led tone={row.led} />
                        <span className={row.kind === "connected" ? styles.wordOk : undefined}>
                          {row.word}
                        </span>
                      </span>
                    </button>
                  );
                })}
              </div>

              <span className={`${dlg.label} ${styles.label}`} id="share-peer-access">
                Access
              </span>
              <div
                className={`${styles.tiles} ${terminal ? "" : styles.three}`}
                role="radiogroup"
                aria-labelledby="share-peer-access"
              >
                {(terminal ? TERMINAL_LEVELS : GRANT_LEVELS).map((l) => {
                  const off = held && l !== "view";
                  return (
                    <button
                      key={l}
                      type="button"
                      role="radio"
                      aria-checked={chosenLevel === l}
                      className={styles.tile}
                      data-level={l}
                      disabled={off}
                      onClick={() => {
                        if (l === chosenLevel) return;
                        setLevel(l);
                        // Another level is another choice: the acknowledgement starts over.
                        setAckFor(null);
                        setDone(null);
                        setError(null);
                        setStuck(null);
                        setNow(nowSeconds());
                      }}
                    >
                      <span className={styles.tileBody}>
                        <span className={styles.tileName}>{LEVEL_TILES[l].name}</span>
                        <span className={styles.tileDesc}>
                          {terminal ? "Watch the screen" : LEVEL_TILES[l].desc}
                        </span>
                      </span>
                    </button>
                  );
                })}
              </div>
              {held && !terminal && (
                <p className={styles.heldLine} data-testid="share-peer-held">
                  {MISSION_HELD_LINE}
                </p>
              )}
              {terminal && (
                <p className={styles.help} data-testid="share-peer-terminal-only">
                  {TERMINAL_VIEW_ONLY}
                </p>
              )}

              <span className={`${dlg.label} ${styles.label}`}>Expires</span>
              <Seg
                options={LEVEL_EXPIRIES[chosenLevel].map((o) => ({
                  value: String(o.seconds),
                  label: o.label,
                }))}
                value={String(expiry.seconds)}
                onChange={(v) => {
                  setSeconds(Number(v));
                  setNow(nowSeconds());
                }}
                label="Expires"
                className={styles.expiry}
                testId="share-peer-expiry"
              />
              {chosenLevel === "send" ? (
                <>
                  <p className={styles.help} data-testid="share-peer-hint">
                    {sendEndsText(expiry.span)}
                  </p>
                  {/* The only red edge in the dialog, with the word: what Send means, and the
                      acknowledgement Share waits for. */}
                  <div
                    className={styles.risk}
                    role="group"
                    aria-labelledby="share-peer-risk"
                    data-testid="share-peer-risk"
                  >
                    <span className={styles.riskWord} id="share-peer-risk">
                      <Led tone="down" />
                      Risk
                    </span>
                    <p className={styles.riskText}>{sendRiskText(chosen?.name ?? "the peer")}</p>
                    <label className={styles.ack}>
                      <input
                        type="checkbox"
                        checked={ticked}
                        required
                        data-testid="share-peer-ack"
                        onChange={(e) =>
                          setAckFor(e.target.checked ? ackKey(peerId, chosenLevel) : null)
                        }
                      />
                      <span>{SEND_ACK}</span>
                    </label>
                  </div>
                </>
              ) : (
                <p className={styles.help} data-testid="share-peer-hint">
                  Ends {shortDate(now + expiry.seconds)}. You can revoke it before then. It also ends
                  when this session is archived or the peer is revoked.
                </p>
              )}
              {chosen && chosen.connected === false && (
                <p className={styles.help} data-testid="share-peer-offline">
                  {chosen.name} is offline. It sees the {terminal ? "screen" : "session"} when it
                  next connects.
                </p>
              )}
              {existing && chosen && (
                <p className={styles.help} data-testid="share-peer-existing">
                  Already shared with {chosen.name} ({levelLabel(existing.level)}),{" "}
                  {grantEnds(existing.expires_at, now).text}. Sharing again replaces its access and
                  when it ends.
                </p>
              )}
            </>
          )}
          {/* Always visible, with or without a peer to pick: what leaves this instance. */}
          {!none && !terminal && (
            <p className={styles.disclosure} data-testid="share-peer-disclosure">
              {PEER_DISCLOSURE}
              <span>{PEER_DISCLOSURE_DETAIL}</span>
            </p>
          )}
          {!none && chosenLevel === "suggest" && (
            <p className={styles.help} data-testid="share-peer-suggest-line">
              {SUGGEST_LINE}
            </p>
          )}
          {error && (
            <p className={dlg.error} role="alert" data-testid="share-peer-error">
              {error}
            </p>
          )}
          {stuck && (
            // The grant's fence was busy (a delivery under it is in flight): nothing changed.
            <div className={styles.stuck} role="alert" data-testid="share-peer-busy">
              <span>{GRANT_BUSY}</span>
              <button
                type="button"
                className={`${btn.ghost} ${btn.compact} ${styles.retry}`}
                disabled={busy !== null || (stuck.what === "share" && !shareable)}
                data-testid="share-peer-retry"
                onClick={() => void (stuck.what === "share" ? share() : revoke(stuck.grant))}
              >
                Try again
              </button>
            </div>
          )}
          {done && (
            <p className={styles.done} role="status" data-testid="share-peer-done">
              {done}
            </p>
          )}

          {live.length > 0 && (
            <section className={styles.listSection} aria-labelledby="share-peer-live">
              <h3 id="share-peer-live" className={`${dlg.label} ${styles.label} ${styles.first}`}>
                Shared with · {live.length}
              </h3>
              <ul className={styles.grants}>
                {live.map((g) => {
                  const row = grantRowState(g, "peer", now);
                  return (
                    <li
                      key={g.grant_id}
                      className={styles.grant}
                      data-testid="share-peer-grant"
                      data-level={g.level}
                    >
                      <span className={styles.grantMain}>
                        <span className={styles.grantTitle}>{row.title}</span>
                        <span className={styles.tagBox}>{row.level}</span>
                        <span
                          className={row.countdown ? styles.grantCountdown : styles.grantMeta}
                          data-testid="share-peer-until"
                        >
                          {row.until}
                        </span>
                      </span>
                      <button
                        type="button"
                        className={`${btn.danger} ${btn.compact} ${styles.revoke}`}
                        aria-label={`Revoke ${row.title}`}
                        disabled={busy !== null}
                        onClick={() => void revoke(g)}
                      >
                        {busy === g.grant_id ? "Revoking…" : "Revoke"}
                      </button>
                    </li>
                  );
                })}
              </ul>
            </section>
          )}
        </div>

        <div className={`${dlg.actions} ${styles.actions}`}>
          <button type="button" className={dlg.cancel} onClick={onClose}>
            Cancel
          </button>
          <button
            type="button"
            className={dlg.go}
            disabled={!shareable || busy !== null || grants === null}
            data-testid="share-peer-go"
            onClick={() => void share()}
          >
            {busy === "share" ? "Sharing…" : "Share"}
          </button>
        </div>
      </div>
    </div>,
    document.body,
  );
}
