/** Library → Webhooks → New (#2221 frame 7): a name only, then the address and the secret, ONCE.
 *
 *  The secret arrives in the create's answer and lives in this component's state until the
 *  operator leaves the page; it is never written to storage or router state and never re-read —
 *  the server cannot show it twice. Opened from the automation editor's picker, the page carries
 *  the editor's draft in router state (the form only, never a secret) and hands it back with the
 *  new webhook picked. */
import { useEffect, useRef, useState } from "react";
import { Link, useLocation, useNavigate } from "react-router-dom";

import { Led } from "../components/automations/AutomationBits";
import { useNowS } from "../components/automations/useAutomations";
import { SecretOnce } from "../components/webhooks/WebhookBits";
import { api } from "../lib/api";
import { errorWords, type EditorForm } from "../lib/automations";
import { WEBHOOKS_PATH, webhookPath } from "../lib/routes";
import {
  WEBHOOK_NAME_MAX,
  nameProblem,
  RELAY_NOTE,
  webhookAddress,
  webhookStatus,
} from "../lib/webhooks";
import type { Webhook } from "../types/webhooks";
import styles from "../components/automations/automations.module.css";

/** What the automation editor passes when its picker's "+ New webhook…" opens this page. */
export interface NewWebhookHandoff {
  returnTo: string;
  draft: EditorForm;
}

export default function WebhookNew() {
  const location = useLocation();
  const navigate = useNavigate();
  const handoff =
    (location.state as { webhookHandoff?: NewWebhookHandoff } | null)
      ?.webhookHandoff ?? null;
  const nowS = useNowS();
  const [name, setName] = useState("");
  const [touched, setTouched] = useState(false);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [made, setMade] = useState<{ webhook: Webhook; secret: string } | null>(
    null,
  );
  const addressEl = useRef<HTMLElement>(null);
  const mounted = useRef(true);
  useEffect(() => {
    mounted.current = true;
    return () => {
      mounted.current = false;
    };
  }, []);

  const problem = nameProblem(name);

  async function create() {
    setTouched(true);
    if (problem || busy) return;
    setBusy(true);
    setError(null);
    try {
      const r = await api.createWebhook(name.trim());
      if (!mounted.current) return; // left meanwhile: the secret is dropped with the page
      setMade({ webhook: r.webhook, secret: r.secret });
    } catch (e) {
      if (mounted.current) setError(errorWords(e, "Couldn’t make it."));
    } finally {
      if (mounted.current) setBusy(false);
    }
  }

  function handBack(w: Webhook) {
    if (!handoff) return;
    navigate(handoff.returnTo, {
      state: { webhookPick: { id: w.id, draft: handoff.draft } },
    });
  }

  const w = made?.webhook ?? null;
  const address = w ? webhookAddress(w.path, w.relay_url) : "";

  return (
    <div className={styles.page} data-testid="webhook-new-page">
      <div className={`${styles.stack} ${styles.hookStack}`}>
        <section className={styles.panel} aria-labelledby="webhook-new-title">
          <div className={styles.headText}>
            <span className={`nj-kicker ${styles.kicker}`}>
              <span className="nj-notch" aria-hidden="true" />
              Library //{" "}
              <Link className={styles.link} to={WEBHOOKS_PATH}>
                Webhooks
              </Link>{" "}
              // new
            </span>
            <div className={styles.hookHead}>
              <h1 id="webhook-new-title" className={styles.title}>
                {w ? `New webhook · ${w.name}` : "New webhook"}
              </h1>
              {w && (
                <span className={styles.hookTag} data-testid="webhook-status">
                  {webhookStatus(w)}
                </span>
              )}
            </div>
          </div>

          {!w ? (
            <form
              className={`${styles.form} ${styles.more}`}
              onSubmit={(e) => {
                e.preventDefault();
                void create();
              }}
              aria-busy={busy || undefined}
            >
              <div className={styles.field}>
                <label className={styles.label} htmlFor="webhook-new-name">
                  Name
                </label>
                <input
                  id="webhook-new-name"
                  className={styles.control}
                  value={name}
                  maxLength={WEBHOOK_NAME_MAX}
                  onChange={(e) => setName(e.target.value)}
                  placeholder="e.g. shop repo"
                  autoFocus
                  aria-invalid={(touched && !!problem) || undefined}
                  aria-describedby="webhook-new-name-hint"
                  data-testid="webhook-new-name"
                />
                <p id="webhook-new-name-hint" className={styles.hint}>
                  {touched && problem
                    ? problem
                    : "Name it after what sends to it. No provider to choose: any service that signs its deliveries works."}
                </p>
              </div>
              {error && (
                <div
                  className={styles.error}
                  role="alert"
                  data-testid="webhook-new-error"
                >
                  {error}
                </div>
              )}
              <div className={styles.noticeRow}>
                <button
                  type="submit"
                  className={styles.primary}
                  disabled={busy}
                  data-testid="webhook-create"
                >
                  {busy ? "Making it…" : "Make webhook"}
                </button>
                <Link
                  className={styles.ghost}
                  to={handoff ? handoff.returnTo : WEBHOOKS_PATH}
                  state={
                    handoff
                      ? { webhookPick: { id: "", draft: handoff.draft } }
                      : undefined
                  }
                >
                  Cancel
                </Link>
              </div>
            </form>
          ) : (
            <>
              <dl className={`${styles.kv} ${styles.more}`}>
                <dt>Address</dt>
                <dd>
                  <code
                    ref={addressEl}
                    className={styles.hookValue}
                    data-testid="webhook-url"
                  >
                    {address}
                  </code>
                  {w?.relay_url && (
                    <p className={styles.hint} data-testid="webhook-relay-note">
                      {RELAY_NOTE}
                    </p>
                  )}
                </dd>
              </dl>
              <SecretOnce
                secret={made!.secret}
                address={address}
                addressEl={addressEl}
                nowS={nowS}
              />
              <p className={`${styles.hint} ${styles.more}`}>
                Paste both into your service’s webhook settings (content type
                JSON), then send a test. The fields it sends show on the
                webhook’s page after the first signed delivery.
              </p>
              <p className={styles.hookLast} data-testid="webhook-last">
                <Led tone="idle" />
                <span>No delivery yet.</span>
              </p>
              <div className={`${styles.noticeRow} ${styles.more}`}>
                {handoff ? (
                  <button
                    type="button"
                    className={styles.primary}
                    onClick={() => handBack(w)}
                    data-testid="webhook-use-in-automation"
                  >
                    Use it in the automation
                  </button>
                ) : (
                  <Link
                    className={styles.primary}
                    to={webhookPath(w.id)}
                    data-testid="webhook-open"
                  >
                    I’ve copied it — open the webhook
                  </Link>
                )}
                <Link className={styles.ghost} to={WEBHOOKS_PATH}>
                  All webhooks
                </Link>
              </div>
              <p className={styles.hint}>
                Rotate it later from the webhook’s page: a new secret is shown
                once, and the old one keeps working for 24 h.
              </p>
            </>
          )}
        </section>
      </div>
    </div>
  );
}
