import { useCallback, useEffect, useState } from "react";
import { ApiError, api } from "../../lib/api";
import { nowSeconds, type ShareList } from "../../lib/shareFormat";
import settings from "../../routes/Settings.module.css";
import dlg from "../HudDialog.module.css";
import { ShareRow } from "./ShareRow";
import styles from "./Shares.module.css";

function message(e: unknown, fallback: string): string {
  return e instanceof ApiError && e.message ? e.message : fallback;
}

/** Settings → Sharing (#1404 P4, mockup A3): every live public link, with Revoke and Revoke all.
 *  Links are made from a session's **Share publicly…**; this page only lists and ends them. In
 *  `AUTH_MODE=none` nothing new can be made, but links from before the switch are still listed
 *  here so they can be ended. */
export function SharingSettings() {
  const [list, setList] = useState<ShareList | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [busy, setBusy] = useState<string | null>(null);
  const [confirmAll, setConfirmAll] = useState(false);
  const [now, setNow] = useState(nowSeconds);

  const reload = useCallback(
    () =>
      api
        .shares()
        .then((r) => {
          setList(r);
          setNow(nowSeconds());
        })
        .catch((e: unknown) => setError(message(e, "Couldn’t load the public links."))),
    [],
  );
  useEffect(() => {
    void reload();
  }, [reload]);

  const revoke = async (shareId: string) => {
    setBusy(shareId);
    setError(null);
    try {
      await api.revokeShare(shareId);
    } catch (e) {
      setError(message(e, "Couldn’t revoke that link."));
    } finally {
      setBusy(null);
      await reload();
    }
  };

  const revokeAll = async () => {
    setBusy("*");
    setError(null);
    try {
      await api.revokeAllShares();
      setConfirmAll(false);
    } catch (e) {
      setError(message(e, "Couldn’t revoke the links."));
    } finally {
      setBusy(null);
      await reload();
    }
  };

  const shares = list?.shares ?? [];
  return (
    <section className={settings.section} aria-labelledby="sharing-h" data-testid="sharing-settings">
      <div className={styles.settingsHead}>
        <h2 id="sharing-h">Public links · {list ? `${shares.length} active` : "…"}</h2>
        {shares.length > 0 && !confirmAll && (
          <button type="button" className={styles.revoke} onClick={() => setConfirmAll(true)}>
            Revoke all
          </button>
        )}
      </div>
      {confirmAll && (
        <div className={styles.confirm} role="group" aria-label="Confirm revoke all">
          <span>
            End all {shares.length} link{shares.length === 1 ? "" : "s"}? Anyone using one loses access
            on their next request.
          </span>
          <button
            type="button"
            className={styles.revoke}
            disabled={busy === "*"}
            onClick={() => void revokeAll()}
          >
            {busy === "*" ? "Revoking…" : "Revoke all links"}
          </button>
          <button type="button" className={dlg.cancel} onClick={() => setConfirmAll(false)}>
            Keep them
          </button>
        </div>
      )}
      <p className={settings.hint}>
        Links work only while this BattleLab is reachable at its public address. Expired and revoked
        links are removed. Create one from a session’s <b>Share publicly…</b> action.
      </p>
      {list?.available === false && (
        <p className={dlg.error} role="note">
          {list.reason}
        </p>
      )}
      {error && (
        <p className={dlg.error} role="alert">
          {error}
        </p>
      )}
      {list && shares.length === 0 && <p className={dlg.muted}>No public links are active.</p>}
      {shares.length > 0 && (
        <ul className={styles.rows}>
          {shares.map((s) => (
            <ShareRow
              key={s.share_id}
              share={s}
              now={now}
              showTitle
              busy={busy === s.share_id || busy === "*"}
              onRevoke={() => void revoke(s.share_id)}
            />
          ))}
        </ul>
      )}
    </section>
  );
}
