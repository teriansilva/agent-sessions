import { X } from "lucide-react";
import { useCallback, useEffect, useRef, useState } from "react";
import { createPortal } from "react-dom";
import { Link } from "react-router-dom";
import { ApiError, api } from "../../lib/api";
import {
  GRANT_EXPIRY_DAYS,
  GRANT_EXPIRY_DEFAULT,
  PEER_DISCLOSURE,
  PEER_DISCLOSURE_DETAIL,
  expiresInSeconds,
  grantFor,
  grantRowState,
  peerRowState,
  shareablePeers,
  shortDate,
  type FederationGrant,
  type FederationState,
  type GrantExpiryDays,
} from "../../lib/federation";
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

const EXPIRY_OPTIONS = GRANT_EXPIRY_DAYS.map((d) => ({
  value: String(d),
  label: d === 1 ? "1 day" : `${d} days`,
}));

/** **Share with a peer…** (#2055): give ONE paired instance a view of this API session, and see
 *  or end what is already shared. A sibling of `SharePubliclyModal`.
 *
 *  View is the only level this build grants; Suggest and Send are shown, disabled, so the levels
 *  are understood before they exist. What the other instance will see is stated in the dialog at
 *  all times, never behind a toggle. On a phone the body scrolls and the action row stays at the
 *  sheet's floor (`HudDialog.module.css` `.pinned`), so Share is reachable under a long list. */
export function SharePeerModal({
  sessionKey,
  title,
  agent,
  onClose,
  returnFocusTo,
}: {
  sessionKey: string;
  title: string;
  /** The agent's label, for the badge beside the title. */
  agent: string;
  onClose: () => void;
  returnFocusTo?: HTMLElement | null;
}) {
  const dialogRef = useRef<HTMLDivElement | null>(null);
  const closeRef = useRef<HTMLButtonElement | null>(null);
  const [fed, setFed] = useState<FederationState | null>(null);
  const [grants, setGrants] = useState<FederationGrant[] | null>(null);
  const [loadError, setLoadError] = useState<string | null>(null);
  const [picked, setPicked] = useState<string | null>(null);
  const [days, setDays] = useState<GrantExpiryDays>(GRANT_EXPIRY_DEFAULT);
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
  const ends = shortDate(now + expiresInSeconds(days));

  const share = async () => {
    if (!chosen || busy) return;
    const current = ticket();
    setBusy("share");
    setError(null);
    setDone(null);
    try {
      const { grant } = existing
        ? await api.changeFederationGrant(
            existing.grant_id,
            expiresInSeconds(days),
            existing.revision,
          )
        : await api.createFederationGrant({
            session_key: sessionKey,
            peer_id: chosen.peer_id,
            expires_in: expiresInSeconds(days),
          });
      if (current()) setDone(`Shared with ${chosen.name} until ${shortDate(grant.expires_at)}.`);
    } catch (e) {
      if (current()) setError(message(e, "Couldn’t share this session."));
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
    try {
      await api.revokeFederationGrant(grant.grant_id);
    } catch (e) {
      if (current()) setError(message(e, "Couldn’t revoke that share."));
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
          <span className={styles.tagBox}>{agent}</span>
        </p>

        <div className={`${dlg.sheetBody} ${styles.body}`} data-testid="share-peer-body">
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
              <span className={`${dlg.label} ${styles.label} ${styles.first}`} id="share-peer-who">
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
                        setDone(null);
                        setError(null);
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
                className={`${styles.tiles} ${styles.three}`}
                role="radiogroup"
                aria-labelledby="share-peer-access"
              >
                <button type="button" role="radio" aria-checked="true" className={styles.tile}>
                  <span className={styles.tileBody}>
                    <span className={styles.tileName}>View</span>
                    <span className={styles.tileDesc}>Read the conversation</span>
                  </span>
                </button>
                {(
                  [
                    ["Suggest", "Propose a message"],
                    ["Send", "Send messages"],
                  ] as const
                ).map(([name, desc]) => (
                  <button
                    key={name}
                    type="button"
                    role="radio"
                    aria-checked="false"
                    className={styles.tile}
                    disabled
                  >
                    <span className={styles.tileBody}>
                      <span className={styles.tileName}>
                        {name}
                        <span className={`${styles.tagBox} ${styles.later}`}>later</span>
                      </span>
                      <span className={styles.tileDesc}>{desc}</span>
                    </span>
                  </button>
                ))}
              </div>

              <span className={`${dlg.label} ${styles.label}`}>Expires</span>
              <Seg
                options={EXPIRY_OPTIONS}
                value={String(days)}
                onChange={(v) => {
                  setDays(Number(v) as GrantExpiryDays);
                  setNow(nowSeconds());
                }}
                label="Expires"
                className={styles.expiry}
                testId="share-peer-expiry"
              />
              <p className={styles.help} data-testid="share-peer-hint">
                Ends {ends}. You can revoke it before then. It also ends when this session is
                archived or the peer is revoked.
              </p>
              {chosen && chosen.connected === false && (
                <p className={styles.help} data-testid="share-peer-offline">
                  {chosen.name} is offline. It sees the session when it next connects.
                </p>
              )}
              {existing && chosen && (
                <p className={styles.help} data-testid="share-peer-existing">
                  Already shared with {chosen.name} until {shortDate(existing.expires_at)}. Sharing
                  again changes when it ends.
                </p>
              )}
            </>
          )}
          {/* Always visible, with or without a peer to pick: what leaves this instance. */}
          {!none && (
            <p className={styles.disclosure} data-testid="share-peer-disclosure">
              {PEER_DISCLOSURE}
              <span>{PEER_DISCLOSURE_DETAIL}</span>
            </p>
          )}
          {error && (
            <p className={dlg.error} role="alert" data-testid="share-peer-error">
              {error}
            </p>
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
                  const row = grantRowState(g, "peer");
                  return (
                    <li key={g.grant_id} className={styles.grant} data-testid="share-peer-grant">
                      <span className={styles.grantMain}>
                        <span className={styles.grantTitle}>{row.title}</span>
                        <span className={styles.tagBox}>{row.level}</span>
                        <span className={styles.grantMeta}>{row.until}</span>
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
            disabled={!chosen || busy !== null || grants === null}
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
