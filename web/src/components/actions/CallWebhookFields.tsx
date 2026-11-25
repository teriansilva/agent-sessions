/** "Call a webhook" in the wizard's What step (#2248 P2): the https address, whether it may reach a
 *  private network address, and the JSON body's fields — each a fixed value or, on a webhook from
 *  the list, a typed value from the delivery. A delivery never changes the address.
 *
 *  `DestinationSecretOnce` shows the signing secret from the ONE answer that made it (a save that
 *  gave the destination its first secret, or "Make a new secret"). Its parent holds it in component
 *  state only — never in storage, router state or a cache — and the server can't show it twice. */
import { useRef } from "react";

import {
  BODY_FIELDS_MAX,
  MAPPED_TYPE_WORDS,
  type BodyFieldDraft,
  type EditorForm,
} from "../../lib/automations";
import type { MappedType } from "../../types/automations";
import type { ShapeField } from "../../types/webhooks";
import styles from "../automations/automations.module.css";
import { Seg } from "../automations/wizard/bits";
import w from "../automations/wizard/wizard.module.css";
import { CopyNote } from "../webhooks/WebhookBits";
import { useCopy } from "../webhooks/useCopy";

export function CallWebhookFields({
  form,
  update,
  f,
  listHook,
  shape,
  saved,
  onNewSecret,
  newSecretBusy,
}: {
  form: EditorForm;
  update: (fn: (f: EditorForm) => EditorForm) => void;
  f: (name: string) => string;
  listHook: boolean;
  shape: ShapeField[];
  /** The SAVED automation's destination, when it calls one: whether its secret exists. */
  saved: { url: string; secret_set: boolean } | null;
  onNewSecret: (() => void) | null;
  newSecretBusy: boolean;
}) {
  const setField = (i: number, patch: Partial<BodyFieldDraft>) =>
    update((x) => ({
      ...x,
      bodyFields: x.bodyFields.map((b, j) => (j === i ? { ...b, ...patch } : b)),
    }));
  return (
    <div className={styles.fields} data-testid="call-webhook-fields">
      <div className={`${styles.field} ${styles.full}`}>
        <label className={styles.label} htmlFor={f("destination")}>
          Address
        </label>
        <input
          id={f("destination")}
          className={`${styles.control} ${styles.mono}`}
          type="url"
          inputMode="url"
          value={form.destination}
          placeholder="https://hooks.example.org/battlelab"
          autoCapitalize="none"
          autoCorrect="off"
          spellCheck={false}
          maxLength={2000}
          onChange={(e) => update((x) => ({ ...x, destination: e.target.value }))}
          data-testid="webhook-destination"
        />
        <p className={styles.hint}>
          https only. Each run sends one POST here, signed with a secret made for this address when
          you save. A delivery never changes the address; it can only fill fields.
        </p>
      </div>
      <div className={`${styles.field} ${styles.full}`}>
        <label className={styles.check}>
          <input
            type="checkbox"
            checked={form.allowPrivate}
            onChange={(e) => update((x) => ({ ...x, allowPrivate: e.target.checked }))}
            data-testid="webhook-allow-private"
          />
          Allow a private or local network address (off unless you need it)
        </label>
        {form.allowPrivate && (
          <p className={`${styles.notice} ${styles.noticeWarn}`}>
            <strong>Private addresses allowed</strong>
            <span>
              This action may then reach a machine on your own network. Turning it on asks for your
              consent again.
            </span>
          </p>
        )}
      </div>
      {saved && (
        <div className={`${styles.field} ${styles.full}`} data-testid="webhook-secret-state">
          <p className={styles.hint}>
            {saved.url === form.destination.trim()
              ? saved.secret_set
                ? "This address has its signing secret. BattleLab can’t show it again; make a new one if you lost it."
                : "This address has no signing secret yet, so nothing is sent: make one."
              : "A new address gets a new signing secret when you save."}
          </p>
          {onNewSecret && saved.url === form.destination.trim() && (
            <button
              type="button"
              className={styles.ghost}
              onClick={onNewSecret}
              disabled={newSecretBusy}
              data-testid="webhook-new-secret"
            >
              {newSecretBusy ? "Making…" : "Make a new secret"}
            </button>
          )}
        </div>
      )}
      <div className={`${styles.field} ${styles.full}`}>
        <span className={styles.label}>Fields</span>
        <datalist id={f("body-shape")}>
          {shape
            .filter((s) => !s.path.includes("[]") && s.type !== "list" && s.type !== "object")
            .map((s) => (
              <option key={s.path} value={s.path}>
                {s.type}
              </option>
            ))}
        </datalist>
        <div data-testid="webhook-fields">
          {form.bodyFields.map((b, i) => (
            <div key={i} className={w.valueRow} data-testid="webhook-field">
              <div className={w.valueHead}>
                <input
                  className={`${styles.control} ${styles.mono}`}
                  value={b.name}
                  placeholder="name"
                  aria-label={`Field ${i + 1}: name`}
                  autoCapitalize="none"
                  autoCorrect="off"
                  spellCheck={false}
                  maxLength={32}
                  onChange={(e) => setField(i, { name: e.target.value })}
                  data-testid="webhook-field-name"
                />
                {listHook && (
                  <Seg<"fixed" | "delivery">
                    label={`Where field ${i + 1} comes from`}
                    value={b.map ? "delivery" : "fixed"}
                    onChange={(v) =>
                      setField(i, { map: v === "delivery" ? { path: "", type: "text", choices: [] } : null })
                    }
                    testId="webhook-field-source"
                    options={[
                      { value: "fixed", label: "Fixed value" },
                      { value: "delivery", label: "From the delivery" },
                    ]}
                  />
                )}
                <button
                  type="button"
                  className={w.remove}
                  aria-label={`Remove field ${i + 1}`}
                  onClick={() => update((x) => ({ ...x, bodyFields: x.bodyFields.filter((_, j) => j !== i) }))}
                  data-testid="webhook-field-remove"
                >
                  ×
                </button>
              </div>
              {b.map && listHook ? (
                <div className={w.mapFields}>
                  <input
                    className={`${styles.control} ${styles.mono}`}
                    list={f("body-shape")}
                    value={b.map.path}
                    maxLength={160}
                    placeholder="pull_request.number"
                    aria-label={`Field ${i + 1}: field of the delivery`}
                    autoCapitalize="none"
                    autoCorrect="off"
                    spellCheck={false}
                    onChange={(e) => setField(i, { map: { ...b.map!, path: e.target.value.trim() } })}
                    data-testid="webhook-field-path"
                  />
                  <select
                    className={styles.control}
                    value={b.map.type}
                    aria-label={`Field ${i + 1}: read as`}
                    onChange={(e) => setField(i, { map: { ...b.map!, type: e.target.value as MappedType } })}
                    data-testid="webhook-field-type"
                  >
                    {(Object.keys(MAPPED_TYPE_WORDS) as MappedType[])
                      .filter((t) => t !== "choice")
                      .map((t) => (
                        <option key={t} value={t}>
                          {MAPPED_TYPE_WORDS[t]}
                        </option>
                      ))}
                  </select>
                </div>
              ) : (
                <input
                  className={styles.control}
                  value={b.value}
                  aria-label={`Field ${i + 1}: value`}
                  maxLength={2000}
                  onChange={(e) => setField(i, { value: e.target.value })}
                  data-testid="webhook-field-value"
                />
              )}
            </div>
          ))}
        </div>
        <div className={w.actions}>
          <button
            type="button"
            className={styles.ghost}
            disabled={form.bodyFields.length >= BODY_FIELDS_MAX}
            onClick={() =>
              update((x) => ({ ...x, bodyFields: [...x.bodyFields, { name: "", value: "", map: null }] }))
            }
            data-testid="webhook-field-add"
          >
            Add a field
          </button>
        </div>
        <p className={styles.hint}>
          The body is JSON: <code>{"{event, automation, run, sent_at, fields}"}</code>. A value from the
          delivery keeps its type (a number stays a number); one that fails its type refuses that
          delivery, and nothing is sent.
        </p>
      </div>
    </div>
  );
}

/** The signing secret, from the one answer that made it. */
export function DestinationSecretOnce({ secret, onDone }: { secret: string; onDone: () => void }) {
  const el = useRef<HTMLElement>(null);
  const [copied, copy] = useCopy();
  return (
    <div
      className={`${styles.notice} ${styles.noticeWarn} ${styles.more}`}
      role="status"
      data-testid="destination-secret"
    >
      <strong>Shown once — copy it now</strong>
      <span>
        The receiver checks each request’s <code>X-BattleLab-Signature: sha256=…</code>: the
        HMAC-SHA256 of the raw body under this secret. BattleLab can’t show it again.
      </span>
      <code ref={el} className={styles.hookSecret} data-testid="destination-secret-value">
        {secret}
      </code>
      <div className={styles.noticeRow}>
        <button
          type="button"
          className={styles.ghost}
          onClick={() => void copy("secret", secret, el)}
          data-testid="destination-copy-secret"
        >
          Copy secret
        </button>
        <button type="button" className={styles.primary} onClick={onDone} data-testid="destination-secret-done">
          Done
        </button>
      </div>
      <CopyNote state={copied} what={{ secret: "Secret" }} />
    </div>
  );
}
