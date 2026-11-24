/** Step 2 — Which events (#2221 P2b). The presets the server recognises from the webhook's last
 *  delivery; "Advanced" holds the operator's own conditions on its fields (all must hold, a closed
 *  set of operators, no expressions). An unknown sender gets Advanced only; a webhook with no
 *  delivery yet says to send a test first and still allows Advanced. A schedule or Run now never
 *  reaches this step. */
import { useState } from "react";

import {
  CHOICES_MAX,
  CONDITIONS_MAX,
  OP_TAKES,
  OP_WORDS,
  chosenPreset,
  conditionValues,
  valueTypeOf,
  withPreset,
  type ConditionDraft,
  type ConditionValueType,
  type ValueDraft,
} from "../../../lib/automations";
import type { ConditionOp } from "../../../types/automations";
import type { ShapeField, WebhookDetail } from "../../../types/webhooks";
import styles from "../automations.module.css";
import { Fold, StepIssues, type StepProps } from "./bits";
import w from "./wizard.module.css";

export function StepEvents({
  form,
  update,
  f,
  detail,
  detailError,
  onReload,
  problems,
  warnings,
}: StepProps & {
  detail: WebhookDetail | null;
  detailError: string;
  onReload: () => void;
  problems: string[];
  warnings: string[];
}) {
  const presets = detail?.presets ?? [];
  const shape = detail?.last_shape ?? [];
  const chosen = chosenPreset(form, presets);
  const noDelivery = !!detail && (detail.last_delivery_at == null || shape.length === 0);
  // Open when the operator's own conditions are in use, or when there is nothing else to offer —
  // until the operator opens or closes it themselves.
  const [userOpen, setOpen] = useState<boolean | null>(null);
  const open = userOpen ?? (chosen === null || presets.length === 0);

  const setWhen = (when: ConditionDraft[]) => update((x) => ({ ...x, when }));
  const edit = (i: number, patch: Partial<ConditionDraft>) =>
    setWhen(form.when.map((c, j) => (j === i ? { ...c, ...patch } : c)));

  return (
    <>
      {detailError ? (
        <div className={`${styles.notice} ${styles.noticeBad}`} role="alert" data-testid="events-error">
          <strong>Couldn’t read the webhook’s last delivery</strong>
          <span>{detailError}</span>
          <div className={styles.noticeRow}>
            <button type="button" className={styles.ghost} onClick={onReload}>
              Retry
            </button>
          </div>
        </div>
      ) : !detail ? (
        <p className={styles.hint} role="status">
          Reading the webhook’s last delivery…
        </p>
      ) : noDelivery ? (
        <div className={`${styles.notice} ${styles.noticeWarn}`} data-testid="events-no-delivery">
          <strong>No delivery yet</strong>
          <span>
            “{detail.name}” has had no delivery yet, so BattleLab can’t offer its events. Send a test
            from your service, then check again — or set your own conditions under Advanced.
          </span>
          <div className={styles.noticeRow}>
            <button type="button" className={styles.ghost} onClick={onReload} data-testid="events-reload">
              Check again
            </button>
          </div>
        </div>
      ) : presets.length === 0 ? (
        <p className={styles.hint} data-testid="events-unknown-sender">
          BattleLab doesn’t recognise this sender’s events, so there are no presets. Set your own
          conditions on its fields under Advanced, or leave them empty to act on every signed
          delivery.
        </p>
      ) : null}

      {presets.length > 0 && (
        <fieldset className={w.radios} data-testid="events-presets">
          <legend className={styles.hint}>
            BattleLab recognises this sender’s events. Choose what should start it:
          </legend>
          {presets.map((p) => (
            <label key={p.id} className={w.radio}>
              <input
                type="radio"
                name={f("preset")}
                checked={chosen === p.id}
                onChange={() => update((x) => withPreset(x, p))}
                data-testid="events-preset"
                data-id={p.id}
              />
              {p.label}
            </label>
          ))}
          <label className={w.radio}>
            <input
              type="radio"
              name={f("preset")}
              checked={chosen === ""}
              onChange={() => update((x) => withPreset(x, null))}
              data-testid="events-every"
            />
            Every signed delivery
          </label>
        </fieldset>
      )}
      {presets.length === 0 && form.when.length === 0 && detail && (
        <p className={styles.hint} data-testid="events-every-note">
          With no conditions it acts on every signed delivery.
        </p>
      )}

      <Fold
        summary="Advanced: your own conditions on fields"
        open={open}
        onToggle={setOpen}
        testId="events-advanced"
      >
        <p className={styles.hint}>
          All must hold. Pick a field of the last delivery (or type one); each value is read as its
          own type — the field's, unless you change it.
        </p>
        <datalist id={f("shape")}>
          {shape.map((s) => (
            <option key={s.path} value={s.path}>
              {s.type}
            </option>
          ))}
        </datalist>
        {form.when.map((c, i) => (
          <ConditionRow
            key={i}
            i={i}
            c={c}
            f={f}
            shape={shape}
            onChange={(patch) => edit(i, patch)}
            onRemove={() => setWhen(form.when.filter((_, j) => j !== i))}
          />
        ))}
        <div className={w.actions}>
          <button
            type="button"
            className={styles.ghost}
            disabled={form.when.length >= CONDITIONS_MAX}
            onClick={() =>
              setWhen([...form.when, { path: "", op: "is", values: [{ text: "", type: "text", unset: true }] }])
            }
            data-testid="cond-add"
          >
            Add a condition
          </button>
        </div>
        <p className={styles.hint}>
          Operators: is · is not · is one of · contains · is yes / is no · is present / is absent.
          Nothing else — no expressions. A field through a list (like labels[].name) holds when any
          item does. At most {CONDITIONS_MAX}.
        </p>
      </Fold>

      <StepIssues problems={problems} warnings={warnings} />
    </>
  );
}

const VALUE_TYPE_WORDS: Record<ConditionValueType, string> = {
  text: "text",
  number: "number",
  yes_no: "yes / no",
};

function ConditionRow({
  i,
  c,
  f,
  shape,
  onChange,
  onRemove,
}: {
  i: number;
  c: ConditionDraft;
  f: (name: string) => string;
  shape: ShapeField[];
  onChange: (patch: Partial<ConditionDraft>) => void;
  onRemove: () => void;
}) {
  const takes = OP_TAKES[c.op];
  const type = shape.find((s) => s.path === c.path)?.type;
  const n = i + 1;
  const shown = conditionValues(c);
  const setValue = (j: number, patch: Partial<ValueDraft>) =>
    onChange({ values: c.values.map((v, k) => (k === j ? { ...v, ...patch } : v)) });
  const blank = (): ValueDraft => ({ text: "", type: type ? valueTypeOf(type) : "text", unset: true });
  // Typing (or choosing yes/no) sets a value, even to empty; changing its type alone does not.
  const setText = (j: number, text: string) =>
    onChange({ values: c.values.map((v, k) => (k === j ? { text, type: v.type } : v)) });
  return (
    <div className={w.condRow} data-testid="cond-row">
      <div className={styles.field}>
        <label className={styles.label} htmlFor={f(`cond-path-${i}`)}>
          Field {n}
          {type ? ` (${type})` : ""}
        </label>
        <input
          id={f(`cond-path-${i}`)}
          className={`${styles.control} ${styles.mono}`}
          list={f("shape")}
          value={c.path}
          maxLength={160}
          placeholder="pull_request.draft"
          autoCapitalize="none"
          autoCorrect="off"
          spellCheck={false}
          onChange={(e) => {
            const path = e.target.value.trim();
            const t = shape.find((s) => s.path === path)?.type;
            // A field the last delivery had reads its values as ITS type.
            onChange(
              t
                ? { path, values: c.values.map((v) => ({ ...v, type: valueTypeOf(t) })) }
                : { path },
            );
          }}
          data-testid="cond-path"
        />
      </div>
      <div className={styles.field}>
        <label className={styles.label} htmlFor={f(`cond-op-${i}`)}>
          Operator
        </label>
        <select
          id={f(`cond-op-${i}`)}
          className={styles.control}
          value={c.op}
          onChange={(e) => {
            const op = e.target.value as ConditionOp;
            const next = OP_TAKES[op];
            // A list keeps every value; one value keeps the first; none keeps them for a switch back.
            const values = !next ? c.values : c.values.length ? c.values : [blank()];
            onChange({ op, values: next && next !== "list" ? values.slice(0, 1) : values });
          }}
          data-testid="cond-op"
        >
          {(Object.keys(OP_WORDS) as ConditionOp[]).map((op) => (
            <option key={op} value={op}>
              {OP_WORDS[op]}
            </option>
          ))}
        </select>
      </div>
      <button
        type="button"
        className={w.remove}
        aria-label={`Remove condition ${n}`}
        onClick={onRemove}
        data-testid="cond-remove"
      >
        ×
      </button>
      {takes && (
        <div className={w.values} role="group" aria-label={`Condition ${n}: ${takes === "list" ? "values" : "value"}`}>
          {shown.map((v, j) => {
            const id = f(`cond-value-${i}-${j}`);
            const label = takes === "list" ? `Value ${j + 1}` : "Value";
            return (
              <div key={j} className={w.valueLine} data-testid="cond-value-row">
                <div className={styles.field}>
                  <label className={styles.label} htmlFor={id}>
                    {label}
                  </label>
                  {v.type === "yes_no" && takes !== "text" ? (
                    <select
                      id={id}
                      className={styles.control}
                      value={v.text}
                      onChange={(e) => setText(j, e.target.value)}
                      data-testid="cond-value"
                    >
                      {v.text !== "yes" && v.text !== "no" && <option value={v.text}>Choose…</option>}
                      <option value="yes">yes</option>
                      <option value="no">no</option>
                    </select>
                  ) : (
                    <input
                      id={id}
                      className={styles.control}
                      value={v.text}
                      inputMode={v.type === "number" && takes !== "text" ? "decimal" : undefined}
                      autoCapitalize="none"
                      autoCorrect="off"
                      onChange={(e) => setText(j, e.target.value)}
                      data-testid="cond-value"
                    />
                  )}
                </div>
                {takes !== "text" && (
                  <select
                    className={styles.control}
                    value={v.type}
                    aria-label={`${label}: read as`}
                    onChange={(e) => setValue(j, { type: e.target.value as ConditionValueType })}
                    data-testid="cond-value-type"
                  >
                    {(Object.keys(VALUE_TYPE_WORDS) as ConditionValueType[]).map((t) => (
                      <option key={t} value={t}>
                        {VALUE_TYPE_WORDS[t]}
                      </option>
                    ))}
                  </select>
                )}
                {takes === "list" && (
                  <button
                    type="button"
                    className={w.remove}
                    aria-label={`Remove value ${j + 1} of condition ${n}`}
                    disabled={c.values.length <= 1}
                    onClick={() => onChange({ values: c.values.filter((_, k) => k !== j) })}
                    data-testid="cond-value-remove"
                  >
                    ×
                  </button>
                )}
              </div>
            );
          })}
          {takes === "list" && (
            <div className={w.actions}>
              <button
                type="button"
                className={styles.ghost}
                disabled={c.values.length >= CHOICES_MAX}
                onClick={() => onChange({ values: [...c.values, blank()] })}
                data-testid="cond-value-add"
              >
                Add a value
              </button>
            </div>
          )}
        </div>
      )}
    </div>
  );
}
