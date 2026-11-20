/** The automation editor's webhook picker (#2221 frame 3): a select of the webhooks an automation
 *  may pick, each with its last delivery, and "+ New webhook…" which opens the create flow. A
 *  legacy (#2202) webhook is never offered. A picked id that is no longer in the list stays shown,
 *  named as missing, so a save never silently changes the trigger. */
import { pickWords, pickableWebhooks } from "../../lib/webhooks";
import type { Webhook } from "../../types/webhooks";
import styles from "../automations/automations.module.css";

/** The select's value for "+ New webhook…": not an id the server mints (`[a-z0-9]`). */
const NEW_WEBHOOK_OPTION = "+new";

export function WebhookPicker({
  id,
  value,
  list,
  error,
  nowS,
  onPick,
  onNew,
}: {
  id: string;
  value: string;
  list: Webhook[] | null;
  error: string;
  nowS: number;
  onPick: (webhookId: string) => void;
  onNew: () => void;
}) {
  const options = pickableWebhooks(list);
  const known = options.some((w) => w.id === value);
  return (
    <div className={styles.field}>
      <label className={styles.label} htmlFor={id}>
        Webhook
      </label>
      <select
        id={id}
        className={styles.control}
        value={value}
        disabled={list === null && !error}
        onChange={(e) => {
          if (e.target.value === NEW_WEBHOOK_OPTION) onNew();
          else onPick(e.target.value);
        }}
        data-testid="trigger-webhook"
      >
        <option value="">{list === null && !error ? "Loading webhooks…" : "Choose a webhook…"}</option>
        {options.map((w) => (
          <option key={w.id} value={w.id}>
            {pickWords(w, nowS)}
          </option>
        ))}
        {value && !known && list !== null && (
          <option value={value}>{value} (not in the list any more)</option>
        )}
        <option value={NEW_WEBHOOK_OPTION}>+ New webhook…</option>
      </select>
      {error ? (
        <p className={`${styles.hint} ${styles.wordDown}`} role="alert" data-testid="trigger-webhook-error">
          Couldn’t read the webhooks: {error}
        </p>
      ) : list !== null && options.length === 0 ? (
        <p className={styles.hint} data-testid="trigger-webhook-empty">
          No webhooks yet. Choose “+ New webhook…” to make one: a name, then an address and a secret
          for your service.
        </p>
      ) : (
        <p className={styles.hint}>
          One webhook can start several automations. Its address and secret are on its page in
          Library → Webhooks.
        </p>
      )}
    </div>
  );
}
