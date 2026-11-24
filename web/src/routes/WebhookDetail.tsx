/** Library → Webhooks → one webhook (#2221 frame 2): its address (copy), its secret's STATUS (never
 *  the secret), Rotate (the new secret shown once; the old one keeps working 24 h), Rename, Delete
 *  (disabled, with the reason, while an automation uses it), the last delivery as field NAMES and
 *  TYPES only — the server never keeps a value — and the automations that use it.
 *
 *  A retired #2202 review loop's own webhook shows its old address and its automation, and is never
 *  deleted here: its address receives nothing any more (#2222), and it goes when that automation is
 *  deleted. */
import { useCallback, useEffect, useRef, useState } from "react";
import { Link, useNavigate, useParams } from "react-router-dom";

import { ConfirmDialog } from "../components/templates/ConfirmDialog";
import { Led } from "../components/automations/AutomationBits";
import { useNowS } from "../components/automations/useAutomations";
import { CopyNote, SecretOnce } from "../components/webhooks/WebhookBits";
import { useCopy } from "../components/webhooks/useCopy";
import { ApiError, api } from "../lib/api";
import { errorWords, whenWords } from "../lib/automations";
import { WEBHOOKS_PATH, automationPath } from "../lib/routes";
import {
  WEBHOOK_NAME_MAX,
  deleteBlock,
  deliveryTone,
  lastDeliveryWords,
  legacyOwner,
  nameProblem,
  secretWords,
  unsignedWords,
  RELAY_NOTE,
  webhookAddress,
  webhookStatus,
} from "../lib/webhooks";
import type { WebhookDetail as Detail } from "../types/webhooks";
import styles from "../components/automations/automations.module.css";

export default function WebhookDetail() {
  const { webhookId = "" } = useParams<{ webhookId: string }>();
  // A fresh mount per webhook: nothing loaded — or a secret revealed — for one lands on another.
  return <Page key={webhookId} id={webhookId} />;
}

function Page({ id }: { id: string }) {
  const navigate = useNavigate();
  const nowS = useNowS();
  const [w, setW] = useState<Detail | null>(null);
  const [loadError, setLoadError] = useState<string | null>(null);
  const [missing, setMissing] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [notice, setNotice] = useState<string | null>(null);
  const [busy, setBusy] = useState<"" | "rotate" | "rename" | "delete">("");
  /** The rotated secret, from the rotate's own answer: this page's state only, until Done or leave. */
  const [secret, setSecret] = useState<string | null>(null);
  const [renaming, setRenaming] = useState<string | null>(null);
  const [confirmDelete, setConfirmDelete] = useState(false);
  const [returnTo, setReturnTo] = useState<HTMLElement | null>(null);
  const addressEl = useRef<HTMLElement>(null);
  const [copied, copy] = useCopy();
  const live = useRef(true);
  useEffect(() => {
    live.current = true;
    return () => {
      live.current = false;
    };
  }, []);

  const load = useCallback(async () => {
    try {
      const r = await api.webhook(id);
      if (!live.current || r.id !== id) return;
      setW(r);
      setLoadError(null);
    } catch (e) {
      if (!live.current) return;
      setLoadError(errorWords(e, "Couldn’t load it."));
      if (e instanceof ApiError && e.status === 404) setMissing(true);
    }
  }, [id]);
  useEffect(() => {
    // The read's state lands after its await, never synchronously in this body.
    // eslint-disable-next-line react-hooks/set-state-in-effect
    void load();
  }, [load]);

  async function rotate() {
    if (!w) return;
    setBusy("rotate");
    setError(null);
    setNotice(null);
    try {
      const r = await api.rotateWebhook(w.id);
      if (!live.current) return;
      setSecret(r.secret);
      setW(r.webhook);
    } catch (e) {
      if (live.current) setError(errorWords(e, "Couldn’t rotate the secret."));
    } finally {
      if (live.current) setBusy("");
    }
  }

  async function rename() {
    if (!w || renaming == null || nameProblem(renaming)) return;
    setBusy("rename");
    setError(null);
    setNotice(null);
    try {
      const r = await api.renameWebhook(w.id, renaming.trim(), w.revision);
      if (!live.current) return;
      setW((x) => (x ? { ...x, ...r } : x));
      setRenaming(null);
      setNotice(`Renamed to “${r.name}”.`);
    } catch (e) {
      if (!live.current) return;
      setError(
        e instanceof ApiError && e.status === 409
          ? "It changed elsewhere since you opened it. Reload it, then rename it again."
          : errorWords(e, "Couldn’t rename it."),
      );
    } finally {
      if (live.current) setBusy("");
    }
  }

  async function remove() {
    if (!w) return;
    setBusy("delete");
    setError(null);
    try {
      await api.deleteWebhook(w.id);
      if (live.current) navigate(WEBHOOKS_PATH, { replace: true });
    } catch (e) {
      if (!live.current) return;
      setConfirmDelete(false);
      setError(errorWords(e, "Couldn’t delete it."));
      void load(); // a 409 means someone picked it meanwhile: show who
    } finally {
      if (live.current) setBusy("");
    }
  }

  if (loadError && !w) {
    return (
      <div className={styles.page}>
        <div className={`${styles.notice} ${styles.noticeBad}`} role="alert">
          <strong data-testid="webhook-load-error">
            {missing
              ? "This webhook doesn’t exist"
              : "Couldn’t load this webhook"}
          </strong>
          <span>{loadError}</span>
          <div className={styles.noticeRow}>
            {!missing && (
              <button
                type="button"
                className={styles.ghost}
                onClick={() => void load()}
              >
                Retry
              </button>
            )}
            <Link className={styles.ghost} to={WEBHOOKS_PATH}>
              All webhooks
            </Link>
          </div>
        </div>
      </div>
    );
  }
  if (!w) {
    return (
      <div className={styles.page}>
        <p className={styles.lede}>Loading…</p>
      </div>
    );
  }

  const address = webhookAddress(w.path, w.relay_url);
  const blocked = deleteBlock(w);
  const renameProblem = renaming == null ? "" : nameProblem(renaming);

  return (
    <div className={styles.page} data-testid="webhook-detail">
      <div className={`${styles.stack} ${styles.hookStack}`}>
        <section className={styles.panel} aria-labelledby="webhook-title">
          <div className={styles.headText}>
            <span className={`nj-kicker ${styles.kicker}`}>
              <span className="nj-notch" aria-hidden="true" />
              Library //{" "}
              <Link className={styles.link} to={WEBHOOKS_PATH}>
                Webhooks
              </Link>
            </span>
            <div className={styles.hookHead}>
              <h1
                id="webhook-title"
                className={styles.title}
                data-testid="webhook-name"
              >
                {w.name}
              </h1>
              <span className={styles.hookTag} data-testid="webhook-status">
                {w.legacy_automation ? "old address" : webhookStatus(w)}
              </span>
            </div>
          </div>

          {w.legacy_automation && (
            <div
              className={`${styles.notice} ${styles.more}`}
              data-testid="webhook-legacy"
            >
              <strong>A retired review loop’s old address</strong>
              <span>
                It belongs to “{legacyOwner(w)}”, a review loop from before
                webhooks were a list. Review loops were retired: this address
                receives nothing any more, and no other automation can pick it.
                It goes when you delete that automation.
              </span>
            </div>
          )}

          {notice && (
            <div
              className={`${styles.notice} ${styles.noticeOk} ${styles.more}`}
              role="status"
              data-testid="webhook-notice"
            >
              {notice}
            </div>
          )}
          {error && (
            <div
              className={`${styles.error} ${styles.more}`}
              role="alert"
              data-testid="webhook-error"
            >
              {error}
            </div>
          )}

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
            <dt>Secret</dt>
            <dd data-testid="webhook-secret-state">{secretWords(w, nowS)}</dd>
            {w.previous_valid_until != null &&
              w.previous_valid_until > nowS && (
                <>
                  <dt>Old secret</dt>
                  <dd data-testid="webhook-previous">
                    keeps working until{" "}
                    {whenWords(w.previous_valid_until, nowS)}
                  </dd>
                </>
              )}
          </dl>

          {secret && (
            <SecretOnce
              secret={secret}
              address={address}
              addressEl={addressEl}
              previousUntil={w.previous_valid_until}
              nowS={nowS}
              onDone={() => setSecret(null)}
            />
          )}

          {renaming != null ? (
            <form
              className={`${styles.form} ${styles.more}`}
              onSubmit={(e) => {
                e.preventDefault();
                void rename();
              }}
            >
              <div className={styles.field}>
                <label className={styles.label} htmlFor="webhook-rename">
                  Name
                </label>
                <input
                  id="webhook-rename"
                  className={styles.control}
                  value={renaming}
                  maxLength={WEBHOOK_NAME_MAX}
                  onChange={(e) => setRenaming(e.target.value)}
                  autoFocus
                  aria-invalid={!!renameProblem || undefined}
                  data-testid="webhook-rename-input"
                />
                {renameProblem && (
                  <p className={styles.hint}>{renameProblem}</p>
                )}
              </div>
              <div className={styles.noticeRow}>
                <button
                  type="submit"
                  className={styles.primary}
                  disabled={busy !== "" || !!renameProblem}
                  data-testid="webhook-rename-save"
                >
                  {busy === "rename" ? "Saving…" : "Save name"}
                </button>
                <button
                  type="button"
                  className={styles.ghost}
                  onClick={() => setRenaming(null)}
                >
                  Cancel
                </button>
              </div>
            </form>
          ) : (
            <div className={`${styles.noticeRow} ${styles.more}`}>
              <button
                type="button"
                className={styles.ghost}
                onClick={() => void copy("address", address, addressEl)}
                data-testid="webhook-copy-address"
              >
                Copy address
              </button>
              {!secret && (
                <button
                  type="button"
                  className={styles.ghost}
                  disabled={busy !== ""}
                  onClick={() => void rotate()}
                  data-testid="webhook-rotate"
                >
                  {busy === "rotate"
                    ? "Rotating…"
                    : w.secret_set
                      ? "Rotate secret"
                      : "Make a secret"}
                </button>
              )}
              <button
                type="button"
                className={styles.ghost}
                disabled={busy !== ""}
                onClick={() => setRenaming(w.name)}
                data-testid="webhook-rename"
              >
                Rename
              </button>
              <button
                type="button"
                className={styles.ghost}
                disabled={busy !== "" || !!blocked}
                aria-describedby={blocked ? "webhook-delete-why" : undefined}
                onClick={(e) => {
                  setReturnTo(e.currentTarget);
                  setConfirmDelete(true);
                }}
                data-testid="webhook-delete"
              >
                Delete
              </button>
            </div>
          )}
          <CopyNote state={copied} what={{ address: "Address" }} />
          {blocked && (
            <p
              id="webhook-delete-why"
              className={styles.hint}
              data-testid="webhook-delete-why"
            >
              Delete is off. {blocked}
            </p>
          )}
          {!secret && w.secret_set && (
            <p className={styles.hint}>
              Rotating shows a new secret once; the old one keeps working for 24
              h, so the sender can be updated without missed deliveries.
            </p>
          )}

          <h2
            className={`${styles.hookSub} ${styles.more}`}
            data-testid="webhook-last-title"
          >
            Last delivery
            {w.last_delivery_at != null
              ? ` · ${lastDeliveryWords(w, nowS)}`
              : ""}
          </h2>
          <p className={styles.hookLast} data-testid="webhook-last">
            <Led tone={deliveryTone(w)} />
            <span>
              {w.last_delivery_at == null
                ? "No delivery yet. Send a test from your service."
                : [w.last_event, w.last_note].filter(Boolean).join(" · ") ||
                  "A signed delivery."}
            </span>
          </p>
          {w.unsigned_24h > 0 && (
            <p
              className={`${styles.hint} ${styles.wordDegraded}`}
              data-testid="webhook-unsigned"
            >
              {unsignedWords(w.unsigned_24h)}: refused. If that was your
              service, check its secret.
            </p>
          )}
          {w.last_shape.length > 0 && (
            <>
              <p className={styles.hint}>
                The fields it sent, with their types — the names an automation
                can use. Real values are not kept.
              </p>
              <ul
                className={styles.hookShape}
                data-testid="webhook-shape"
                aria-label="Fields of the last delivery"
              >
                {w.last_shape.map((f) => (
                  <li key={f.path} data-testid="webhook-field">
                    {f.path}
                    <span className={styles.hookType}> · {f.type}</span>
                  </li>
                ))}
              </ul>
            </>
          )}

          <h2 className={`${styles.hookSub} ${styles.more}`}>Used by</h2>
          {w.used_by.length === 0 ? (
            <p className={styles.hint} data-testid="webhook-unused">
              Not used yet. Pick it in an automation’s trigger.
            </p>
          ) : (
            <ul className={styles.hookList} data-testid="webhook-users">
              {w.used_by.map((u) => (
                <li key={u.id}>
                  <Link
                    className={styles.hookRow}
                    to={automationPath(u.id)}
                    data-testid="webhook-user"
                  >
                    <span className={styles.hookName}>{u.name}</span>
                  </Link>
                </li>
              ))}
            </ul>
          )}
        </section>
      </div>
      {confirmDelete && (
        <ConfirmDialog
          tag="Delete webhook"
          title={w.name}
          confirmLabel={busy === "delete" ? "Deleting…" : "Delete"}
          danger
          busy={busy === "delete"}
          onCancel={() => setConfirmDelete(false)}
          onConfirm={() => void remove()}
          returnFocusTo={returnTo}
          markers={{
            dialog: { "data-testid": "webhook-delete-dialog" },
            confirm: { "data-testid": "webhook-delete-confirm" },
          }}
        >
          <p>
            Its address stops working at once: deliveries to it are refused. A
            service still sending to it will see errors.
          </p>
        </ConfirmDialog>
      )}
    </div>
  );
}
