/** Shared pieces of Library → Webhooks (#2221): the secret shown once, and the read-only panel an
 *  automation shows for the webhook it picked.
 *
 *  **The secret is shown once.** `SecretOnce` renders the plaintext its parent got from the ONE
 *  answer that made it (create or rotate). The parent holds it in component state only — never in
 *  storage, router state or a cache, and never re-fetched: the server cannot show it twice. Leaving
 *  the page, or pressing Done, forgets it. */
import { useRef, type ReactNode, type RefObject } from "react";
import { Link } from "react-router-dom";

import { whenWords } from "../../lib/automations";
import { webhookPath } from "../../lib/routes";
import {
  deliveryTone,
  lastDeliveryWords,
  unsignedWords,
  webhookAddress,
  webhookStatus,
} from "../../lib/webhooks";
import { Led } from "../automations/AutomationBits";
import styles from "../automations/automations.module.css";
import { useCopy, type CopyState } from "./useCopy";

/** What the last Copy did, as a status line. */
export function CopyNote({ state, what }: { state: CopyState; what: Record<string, string> }) {
  if (!state) return null;
  return (
    <span role="status" data-testid="webhook-copied">
      {state.ok
        ? `${what[state.which] ?? "It"} copied.`
        : `Couldn’t reach the clipboard. The ${(what[state.which] ?? "value").toLowerCase()} is selected: copy it by hand.`}
    </span>
  );
}

export function SecretOnce({
  secret,
  address,
  addressEl,
  previousUntil,
  nowS,
  onDone,
  children,
}: {
  secret: string;
  address: string;
  /** The on-screen address, selected for a manual copy when the clipboard is unavailable. */
  addressEl: RefObject<HTMLElement | null>;
  /** After a rotation: when the old secret stops verifying. */
  previousUntil?: number | null;
  nowS: number;
  onDone?: () => void;
  children?: ReactNode;
}) {
  const secretEl = useRef<HTMLElement>(null);
  const [copied, copy] = useCopy();
  return (
    <div
      className={`${styles.notice} ${styles.noticeWarn} ${styles.more}`}
      data-testid="webhook-secret"
    >
      <strong>Shown once — copy it now</strong>
      <span>
        {previousUntil != null
          ? `The new secret. Put it in your service’s webhook settings: the old one keeps working until ${whenWords(previousUntil, nowS)}, then only this one does.`
          : "Paste the address and this secret into your service’s webhook settings (content type JSON). BattleLab can’t show the secret again."}
      </span>
      <code ref={secretEl} className={styles.hookSecret} data-testid="webhook-secret-value">
        {secret}
      </code>
      <div className={styles.noticeRow}>
        <button
          type="button"
          className={styles.ghost}
          onClick={() => void copy("secret", secret, secretEl)}
          data-testid="webhook-copy-secret"
        >
          Copy secret
        </button>
        <button
          type="button"
          className={styles.ghost}
          onClick={() => void copy("address", address, addressEl)}
          data-testid="webhook-copy-address-once"
        >
          Copy address
        </button>
        {onDone && (
          <button
            type="button"
            className={styles.primary}
            onClick={onDone}
            data-testid="webhook-secret-done"
          >
            Done
          </button>
        )}
      </div>
      <CopyNote state={copied} what={{ secret: "Secret", address: "Address" }} />
      {children}
    </div>
  );
}

/** The webhook an automation picked (#2221), read-only: its name, address and last delivery, with
 *  a link to its page. No secret controls — a shared webhook's secret is rotated on its own page,
 *  because rotating it here would change it for every automation that picked it. */
export function SharedWebhookPanel({
  id,
  name,
  path,
  lastDeliveryAt,
  lastEvent,
  lastNote,
  unsigned24h,
  nowS,
}: {
  id: string;
  name: string;
  path: string;
  lastDeliveryAt: number | null;
  lastEvent: string;
  lastNote: string;
  unsigned24h: number;
  nowS: number;
}) {
  const address = webhookAddress(path);
  const addressEl = useRef<HTMLElement>(null);
  const [copied, copy] = useCopy();
  const w = { last_delivery_at: lastDeliveryAt };
  return (
    <section className={styles.panel} aria-label={`Webhook ${name}`} data-testid="shared-webhook-panel">
      <div className={styles.hookHead}>
        <h2 className={styles.panelTitle}>Webhook</h2>
        <span className={styles.hookTag} data-testid="webhook-status">
          {webhookStatus(w)}
        </span>
      </div>
      <dl className={styles.kv}>
        <dt>Name</dt>
        <dd>
          <Link className={styles.link} to={webhookPath(id)} data-testid="shared-webhook-link">
            {name}
          </Link>
        </dd>
        <dt>Address</dt>
        <dd>
          <code ref={addressEl} className={styles.hookValue} data-testid="webhook-url">
            {address}
          </code>
        </dd>
      </dl>
      <div className={`${styles.noticeRow} ${styles.more}`}>
        <button
          type="button"
          className={styles.ghost}
          onClick={() => void copy("address", address, addressEl)}
          data-testid="webhook-copy-address"
        >
          Copy address
        </button>
        <Link className={styles.ghost} to={webhookPath(id)} data-testid="shared-webhook-open">
          Open webhook
        </Link>
      </div>
      <CopyNote state={copied} what={{ address: "Address" }} />
      <h3 className={`${styles.hookSub} ${styles.more}`}>Last delivery</h3>
      <p className={styles.hookLast} data-testid="webhook-last">
        <Led tone={deliveryTone(w)} />
        <span>
          {lastDeliveryAt == null
            ? "No delivery yet. Send a test from your service."
            : [lastDeliveryWords(w, nowS), lastEvent, lastNote].filter(Boolean).join(" · ")}
        </span>
      </p>
      {unsigned24h > 0 && (
        <p className={`${styles.hint} ${styles.wordDegraded}`} data-testid="webhook-unsigned">
          {unsignedWords(unsigned24h)}: refused. If that was your service, check its secret.
        </p>
      )}
      <p className={styles.hint}>
        Its secret and the fields it delivers are on its page. Other automations may pick it too.
      </p>
    </section>
  );
}
