import { useCallback, useEffect, useId, useState } from "react";
import { ApiError, api } from "../../lib/api";
import {
  grantRowState,
  levelLabel,
  shortDate,
  type FederationGrant,
  type RemoteShare,
} from "../../lib/federation";
import { Disclosure } from "../hud/Controls";
import btn from "../ui/actionButton.module.css";
import styles from "./Peers.module.css";
import { useAnswerFence } from "./useAnswerFence";

/** What is shared with one peer, and what it shares with this instance (#2055), under its row in
 *  Settings → Peers. Collapsed, the row shows the two counts.
 *
 *  - **Shared with <peer>**: this instance's live grants to it (`GET /api/federation/grants
 *    ?peer=`), each with Revoke. Revoking ends that one share at once.
 *  - **Shared by <peer>**: what it shares here, from the same list the session list shows. Read
 *    only: the other operator decides how long it stays shared. */
export function PeerShares({
  peerId,
  peerName,
  remote,
}: {
  peerId: string;
  peerName: string;
  /** Everything peers share with this instance; this component shows this peer's part. */
  remote: readonly RemoteShare[];
}) {
  const [grants, setGrants] = useState<FederationGrant[] | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [busy, setBusy] = useState<string | null>(null);
  const outId = useId();
  const inId = useId();
  // An answer applies only while this list is shown for the peer it was asked for. Declared
  // before the loading effect, so it is armed when that runs (StrictMode replay included).
  const ticket = useAnswerFence(peerId);

  const load = useCallback(() => {
    const current = ticket();
    return api.federationGrantsToPeer(peerId).then(
      (r) => {
        if (current()) setGrants(Array.isArray(r?.grants) ? r.grants : []);
      },
      () => {
        // The counts stay as they were; a first read that failed counts nothing.
        if (current()) setGrants((prev) => prev ?? []);
      },
    );
  }, [peerId, ticket]);
  useEffect(() => {
    void load();
  }, [load]);

  const revoke = async (grant: FederationGrant) => {
    if (busy) return;
    const current = ticket();
    setBusy(grant.grant_id);
    setError(null);
    try {
      await api.revokeFederationGrant(grant.grant_id);
    } catch (e) {
      if (current())
        setError(e instanceof ApiError && e.message ? e.message : "Couldn’t revoke that share.");
    } finally {
      await load();
      if (current()) setBusy(null);
    }
  };

  const out = grants ?? [];
  const received = remote.filter((r) => r.peer_id === peerId);

  return (
    <Disclosure
      className={styles.shares}
      testId="peer-shares"
      summary={
        <span data-testid="peer-shares-counts">
          Sessions · <b>{out.length} shared · {received.length} received</b>
        </span>
      }
    >
      <div className={styles.sharesBody}>
        <section aria-labelledby={outId} data-testid="peer-shares-out">
          <h4 id={outId} className={styles.sharesHead}>
            Shared with {peerName} · {out.length}
          </h4>
          {out.length === 0 ? (
            <p className={styles.rowMeta}>Nothing is shared with {peerName}.</p>
          ) : (
            <ul className={styles.grants}>
              {out.map((g) => {
                const row = grantRowState(g, "session");
                return (
                  <li key={g.grant_id} className={styles.grant} data-testid="peer-grant">
                    <span className={styles.grantMain}>
                      <span className={styles.grantTitle}>{row.title}</span>
                      <span className={styles.level}>{row.level}</span>
                      <span className={styles.grantMeta}>{row.until}</span>
                    </span>
                    <button
                      type="button"
                      className={`${btn.danger} ${btn.compact} ${styles.grantRevoke}`}
                      aria-label={`Revoke ${row.title} for ${peerName}`}
                      disabled={busy !== null}
                      onClick={() => void revoke(g)}
                    >
                      {busy === g.grant_id ? "Revoking…" : "Revoke"}
                    </button>
                  </li>
                );
              })}
            </ul>
          )}
          {error && (
            <p className="nj-error" role="alert">
              {error}
            </p>
          )}
        </section>
        <section aria-labelledby={inId} data-testid="peer-shares-in">
          <h4 id={inId} className={styles.sharesHead}>
            Shared by {peerName} · {received.length}
          </h4>
          {received.length === 0 ? (
            <p className={styles.rowMeta}>{peerName} shares nothing with this instance.</p>
          ) : (
            <>
              <ul className={styles.grants}>
                {received.map((r) => (
                  <li key={r.grant_id} className={styles.grant} data-testid="peer-received">
                    <span className={styles.grantMain}>
                      <span className={styles.grantTitle}>{r.title || "(untitled)"}</span>
                      <span className={styles.level}>{levelLabel(r.level)}</span>
                      <span className={styles.grantMeta}>until {shortDate(r.expires_at)}</span>
                    </span>
                  </li>
                ))}
              </ul>
              <p className={styles.rowMeta} data-testid="peer-shares-readonly">
                Read-only here. {peerName}'s operator decides how long it stays shared.
              </p>
            </>
          )}
        </section>
      </div>
    </Disclosure>
  );
}
