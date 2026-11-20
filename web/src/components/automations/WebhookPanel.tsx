/** "Connect Forgejo" (#2202 board 2): where a review loop's webhook delivers, its secret, the forge
 *  events to tick, and what arrived last.
 *
 *  **The secret is shown once.** `POST /api/automations/{id}/webhook-secret` returns the plaintext
 *  in that one answer; it lives in this panel's state until the operator presses Done or leaves,
 *  and is never stored, cached or read again — the server cannot show it twice. Rotating calls the
 *  same route; the previous secret keeps verifying deliveries for 24 h so the forge can be updated
 *  without a gap. */
import { useEffect, useRef, useState } from "react";

import { api } from "../../lib/api";
import { appUrl } from "../../lib/appBase";
import { FORGE_EVENTS, errorWords, whenWords } from "../../lib/automations";
import type { AutomationWebhook } from "../../types/automations";
import { Led } from "./AutomationBits";
import styles from "./automations.module.css";

export function WebhookPanel({
  automationId,
  webhook,
  nowS,
  onWebhook,
}: {
  automationId: string;
  webhook: AutomationWebhook;
  nowS: number;
  /** The server's fresh webhook facts after a secret was made — the parent's row adopts them. */
  onWebhook: (w: AutomationWebhook) => void;
}) {
  const [secret, setSecret] = useState<string | null>(null);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [copied, setCopied] = useState<"" | "ok" | "failed">("");
  const secretEl = useRef<HTMLElement>(null);
  const mounted = useRef(true);
  useEffect(() => {
    mounted.current = true;
    return () => {
      mounted.current = false;
    };
  }, []);

  const url = `${window.location.origin}${appUrl(webhook.path)}`;
  const rotating = webhook.secret_set;

  async function makeSecret() {
    setBusy(true);
    setError(null);
    setCopied("");
    try {
      const r = await api.newWebhookSecret(automationId);
      if (!mounted.current) return;
      setSecret(r.secret);
      onWebhook(r.webhook);
    } catch (e) {
      if (mounted.current) setError(errorWords(e, "Couldn’t make a secret."));
    } finally {
      if (mounted.current) setBusy(false);
    }
  }

  async function copy() {
    if (!secret) return;
    try {
      await navigator.clipboard.writeText(secret);
      setCopied("ok");
    } catch {
      // No clipboard here (an insecure origin, a denied permission): select it for a manual copy.
      if (secretEl.current)
        window.getSelection()?.selectAllChildren(secretEl.current);
      setCopied("failed");
    }
  }

  const status = !webhook.secret_set
    ? "no secret yet"
    : webhook.last_delivery_at == null
      ? "waiting for first delivery"
      : "connected";

  return (
    <section
      className={styles.panel}
      aria-labelledby={`hook-${automationId}`}
      data-testid="webhook-panel"
    >
      <div className={styles.hookHead}>
        <h2 id={`hook-${automationId}`} className={styles.panelTitle}>
          Connect Forgejo
        </h2>
        <span className={styles.hookTag} data-testid="webhook-status">
          {status}
        </span>
      </div>
      <dl className={styles.kv}>
        <dt>Payload URL</dt>
        <dd>
          <code className={styles.hookValue} data-testid="webhook-url">
            {url}
          </code>
        </dd>
        <dt>Content type</dt>
        <dd>
          <code className={styles.hookValue}>application/json</code>
        </dd>
      </dl>

      {secret ? (
        <div
          className={`${styles.notice} ${styles.noticeWarn} ${styles.more}`}
          data-testid="webhook-secret"
        >
          <strong>Shown once — copy it now</strong>
          <span>
            Paste it into the webhook’s Secret field on the forge. BattleLab
            can’t show it again.
          </span>
          <code
            ref={secretEl}
            className={styles.hookSecret}
            data-testid="webhook-secret-value"
          >
            {secret}
          </code>
          <div className={styles.noticeRow}>
            <button
              type="button"
              className={styles.ghost}
              onClick={() => void copy()}
              data-testid="webhook-copy"
            >
              Copy secret
            </button>
            <button
              type="button"
              className={styles.primary}
              onClick={() => setSecret(null)}
              data-testid="webhook-done"
            >
              Done
            </button>
          </div>
          {copied === "ok" && (
            <span role="status" data-testid="webhook-copied">
              Copied.
            </span>
          )}
          {copied === "failed" && (
            <span role="status">
              Couldn’t reach the clipboard. The secret is selected: copy it by
              hand.
            </span>
          )}
        </div>
      ) : (
        <p
          className={`${styles.hint} ${styles.more}`}
          data-testid="webhook-secret-state"
        >
          {webhook.secret_set
            ? `A secret is set${webhook.secret_created_at ? ` (made ${whenWords(webhook.secret_created_at, nowS)})` : ""}. It is never shown again: rotate it to get a new one.`
            : "No secret yet. Create one, then paste it into the forge’s webhook settings: a delivery without a valid signature is refused."}
        </p>
      )}
      {webhook.previous_valid_until != null &&
        webhook.previous_valid_until > nowS && (
          <p className={styles.hint} data-testid="webhook-previous">
            The previous secret keeps working until{" "}
            {whenWords(webhook.previous_valid_until, nowS)}.
          </p>
        )}
      {error && (
        <div className={styles.error} role="alert" data-testid="webhook-error">
          {error}
        </div>
      )}
      {!secret && (
        <div className={`${styles.noticeRow} ${styles.more}`}>
          <button
            type="button"
            className={rotating ? styles.ghost : styles.primary}
            disabled={busy}
            onClick={() => void makeSecret()}
            data-testid="webhook-make-secret"
          >
            {busy ? "Working…" : rotating ? "Rotate secret" : "Create secret"}
          </button>
        </div>
      )}
      {rotating && !secret && (
        <p className={styles.hint}>
          After a rotation the old secret keeps working for 24 h, so the forge
          can be updated without missed deliveries.
        </p>
      )}

      <h3 className={`${styles.hookSub} ${styles.more}`}>
        Tick these events in the repository’s webhook settings
      </h3>
      <ul className={styles.hookEvents} data-testid="webhook-events">
        {FORGE_EVENTS.map((e) => (
          <li key={e}>
            <span className={styles.hookTick} aria-hidden="true">
              ✓
            </span>
            {e}
          </li>
        ))}
      </ul>
      <p className={styles.hint}>
        Branch filter: leave it empty. If a proxy in front of BattleLab checks
        logins, let <code className={styles.mono}>/hooks/</code> through without
        one: the signature is what authenticates a delivery.
      </p>

      <h3 className={`${styles.hookSub} ${styles.more}`}>Last delivery</h3>
      <p className={styles.hookLast} data-testid="webhook-last">
        <Led tone={webhook.last_delivery_at == null ? "idle" : "up"} />
        <span>
          {webhook.last_delivery_at == null
            ? "No delivery yet. Use “Test delivery” on the forge: a ping updates this line (it is not a run)."
            : [
                whenWords(webhook.last_delivery_at, nowS),
                webhook.last_event,
                webhook.last_note,
              ]
                .filter(Boolean)
                .join(" · ")}
        </span>
      </p>
      {webhook.unsigned_24h > 0 && (
        <p
          className={`${styles.hint} ${styles.wordDegraded}`}
          data-testid="webhook-unsigned"
        >
          {webhook.unsigned_24h} unsigned{" "}
          {webhook.unsigned_24h === 1 ? "delivery" : "deliveries"} refused in
          the last 24 h. If that was the forge, check its secret.
        </p>
      )}
    </section>
  );
}
