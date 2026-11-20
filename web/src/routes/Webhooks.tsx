/** Library → Webhooks (#2221 frame 1): a simple list. Each webhook is a name, an address and a
 *  secret that any service which signs its deliveries can use, and any automation can pick. A row
 *  shows its last delivery (LED + time), who uses it, what arrived last and refused unsigned
 *  deliveries; the whole row opens its page. "New webhook" asks for a name only.
 *
 *  A #2202 review loop's own webhook is listed too ("old address · used by …"): it belongs to that
 *  automation alone and is never offered as a pick. A failed read says so and offers Retry; it never
 *  shows the empty state. */
import { Link } from "react-router-dom";
import { ChevronRight, Plus, RefreshCw } from "lucide-react";

import { Led } from "../components/automations/AutomationBits";
import { useNowS } from "../components/automations/useAutomations";
import { useWebhooks } from "../components/webhooks/useWebhooks";
import { WEBHOOK_NEW_PATH, webhookPath } from "../lib/routes";
import { deliveryTone, lastDeliveryWords, webhookMeta } from "../lib/webhooks";
import styles from "../components/automations/automations.module.css";

export default function Webhooks() {
  const { list, error, loading, reload } = useWebhooks();
  const nowS = useNowS();

  return (
    <div className={styles.page} data-testid="webhooks-page">
      <div className={`${styles.stack} ${styles.hookStack}`}>
        <section className={styles.panel} aria-labelledby="webhooks-title">
          <div className={styles.head}>
            <div className={styles.headText}>
              <span className={`nj-kicker ${styles.kicker}`}>
                <span className="nj-notch" aria-hidden="true" />
                Library // <b>Webhooks</b>
                {list && !error && <> // {list.length}</>}
              </span>
              <h1 id="webhooks-title" className={styles.title}>
                Webhooks
              </h1>
              <p className={styles.lede}>
                An address and a secret for any service that signs its deliveries. Automations pick
                one as their trigger; one webhook can start several.
              </p>
            </div>
            <div className={styles.headActions}>
              <Link to={WEBHOOK_NEW_PATH} className={styles.primary} data-testid="webhook-new">
                <Plus size={15} aria-hidden="true" /> New webhook
              </Link>
            </div>
          </div>

          {error ? (
            <div className={`${styles.notice} ${styles.noticeBad} ${styles.more}`} role="alert" data-testid="webhooks-error">
              <strong>Couldn’t load the webhooks</strong>
              <span>{error}. Nothing was changed, and deliveries keep arriving.</span>
              <div className={styles.noticeRow}>
                <button type="button" className={styles.ghost} onClick={reload} disabled={loading}>
                  <RefreshCw size={14} aria-hidden="true" /> {loading ? "Retrying…" : "Retry"}
                </button>
              </div>
            </div>
          ) : list === null ? (
            <p className={`${styles.lede} ${styles.more}`}>Loading…</p>
          ) : list.length === 0 ? (
            <div className={styles.empty} data-testid="webhooks-empty">
              <strong>No webhooks yet</strong>
              <span className={styles.hint}>
                Make one with a name. You get an address and a secret to paste into your service; then
                pick it in an automation’s trigger.
              </span>
              <Link to={WEBHOOK_NEW_PATH} className={styles.primary}>
                <Plus size={15} aria-hidden="true" /> New webhook
              </Link>
            </div>
          ) : (
            <ul className={styles.hookList} aria-label="Webhooks">
              {list.map((w) => (
                <li key={w.id}>
                  <Link
                    className={styles.hookRow}
                    to={webhookPath(w.id)}
                    data-testid="webhook-row"
                    data-id={w.id}
                  >
                    <span className={styles.hookName}>{w.name}</span>
                    <span className={styles.hookWhen} data-testid="webhook-row-last">
                      <Led tone={deliveryTone(w)} />
                      {lastDeliveryWords(w, nowS)}
                    </span>
                    <span
                      className={`${styles.hookMeta} ${w.unsigned_24h > 0 ? styles.wordDegraded : ""}`}
                      data-testid="webhook-row-meta"
                    >
                      {webhookMeta(w)}
                    </span>
                    <ChevronRight size={16} className={styles.hookChev} aria-hidden="true" />
                  </Link>
                </li>
              ))}
            </ul>
          )}
          <p className={`${styles.hint} ${styles.more}`}>
            No provider to choose: BattleLab checks each delivery’s signature against the webhook’s
            secret, and what a delivery means is up to the automation that picked it.
          </p>
        </section>
      </div>
    </div>
  );
}
