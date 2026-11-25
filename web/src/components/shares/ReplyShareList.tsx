import { useCallback, useEffect, useState } from "react";
import { ApiError, api } from "../../lib/api";
import { createdLabel, expiryChip, nowSeconds, type ReplyShare } from "../../lib/shareFormat";
import dlg from "../HudDialog.module.css";
import styles from "./Shares.module.css";

function message(e: unknown, fallback: string): string {
  return e instanceof ApiError && e.message ? e.message : fallback;
}

function opened(s: ReplyShare, now: number): string {
  if (!s.open_count) return "not opened yet";
  const ago = s.last_opened_at ? Math.max(0, Math.round((now - s.last_opened_at) / 60)) : null;
  const times = `opened ${s.open_count}×`;
  if (ago == null) return times;
  return ago < 1 ? `${times}, last just now` : ago < 60 ? `${times}, last ${ago} min ago` : times;
}

/** Settings → Sharing: the live reply links (#1956), each with Revoke, and Revoke all. Never a
 *  link itself: its key exists only in the link the operator was shown once. */
export function ReplyShareList() {
  const [shares, setShares] = useState<ReplyShare[] | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [busy, setBusy] = useState<string | null>(null);
  const [now, setNow] = useState(nowSeconds);

  const reload = useCallback(
    () =>
      api
        .replyShares()
        .then((r) => {
          setShares(r.shares.filter((s) => s.live));
          setNow(nowSeconds());
        })
        .catch((e: unknown) => setError(message(e, "Couldn’t load the reply links."))),
    [],
  );
  useEffect(() => {
    void reload();
  }, [reload]);

  const run = async (id: string, fn: () => Promise<unknown>) => {
    setBusy(id);
    setError(null);
    try {
      await fn();
    } catch (e) {
      setError(message(e, "Couldn’t revoke."));
    } finally {
      setBusy(null);
      await reload();
    }
  };

  const live = shares ?? [];
  return (
    <section className={styles.listSection} aria-labelledby="reply-links-h" data-testid="reply-links">
      <div className={styles.settingsHead}>
        <h3 id="reply-links-h" className={dlg.label}>
          Reply links · {shares ? `${live.length} active` : "…"}
        </h3>
        {live.length > 0 && (
          <button
            type="button"
            className={styles.revoke}
            disabled={busy !== null}
            onClick={() => void run("*", () => api.revokeAllReplyShares())}
          >
            {busy === "*" ? "Revoking…" : "Revoke all reply links"}
          </button>
        )}
      </div>
      {error && (
        <p className={dlg.error} role="alert">
          {error}
        </p>
      )}
      {shares && live.length === 0 && (
        <p className={dlg.muted}>
          No reply links are active. Share one from a finished reply’s link icon in an API session.
        </p>
      )}
      {live.length > 0 && (
        <ul className={styles.rows}>
          {live.map((s) => (
            <li key={s.share_id} className={styles.row} data-reply-share-row={s.share_id}>
              <span className="hud-led up" aria-hidden="true" />
              <div className={styles.rowMain}>
                <span className={styles.rowTitle}>
                  {s.agent} reply · {s.session_key}
                </span>
                <span className={styles.chips}>
                  <span className={`${styles.chip} ${styles.chipMode}`}>READ ONLY</span>
                  <span className={s.has_password ? styles.chip : `${styles.chip} ${styles.chipWarn}`}>
                    {s.has_password ? "PASSWORD" : "NO PASSWORD"}
                  </span>
                  <span className={styles.chip}>{expiryChip(s.expires_at, now)}</span>
                </span>
                <span className={styles.rowMeta}>
                  {createdLabel(s.created_at)} · {opened(s, now)}
                </span>
              </div>
              <button
                type="button"
                className={styles.revoke}
                disabled={busy !== null}
                aria-label={`Revoke the ${s.agent} reply link`}
                onClick={() => void run(s.share_id, () => api.revokeReplyShare(s.share_id))}
              >
                {busy === s.share_id ? "Revoking…" : "Revoke"}
              </button>
            </li>
          ))}
        </ul>
      )}
    </section>
  );
}
