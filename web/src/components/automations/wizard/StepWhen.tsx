/** Step 1 — When (#2221 P2b): on a schedule (or once), only when you press Run now, or when a
 *  webhook PICKED from Library → Webhooks delivers ("+ New webhook…" hands the draft to the create
 *  flow and comes back with it picked). */
import { SharedWebhookPanel } from "../../webhooks/WebhookBits";
import { WebhookPicker } from "../../webhooks/WebhookPicker";
import {
  WEEKDAYS,
  formTrigger,
  triggerWords,
  withTrigger,
  type EditorForm,
} from "../../../lib/automations";
import type { Automation, AutomationList, Weekday } from "../../../types/automations";
import type { Webhook } from "../../../types/webhooks";
import styles from "../automations.module.css";
import { Cards, TzField, type StepProps } from "./bits";

const DAY_LABEL: Record<Weekday, string> = {
  mon: "Mon",
  tue: "Tue",
  wed: "Wed",
  thu: "Thu",
  fri: "Fri",
  sat: "Sat",
  sun: "Sun",
};

type When = "schedule" | "webhook" | "manual";

export function StepWhen({
  form,
  set,
  update,
  f,
  list,
  zones,
  webhooks,
  nowS,
  saved,
  onNewWebhook,
}: StepProps & {
  list: AutomationList;
  zones: string[] | null;
  webhooks: { list: Webhook[] | null; error: string };
  nowS: number;
  saved: Automation | null;
  onNewWebhook: () => void;
}) {
  const when: When = form.triggerKind === "once" ? "schedule" : form.triggerKind;
  const draftTrigger = triggerWords(formTrigger(form));
  const picked = pickedWebhook(form.webhookId, webhooks.list, saved);
  const cadence =
    form.triggerKind === "once"
      ? "once"
      : form.cadenceKind === "interval"
        ? `interval-${form.unit}`
        : form.cadenceKind;
  return (
    <>
      <Cards<When>
        label="When it runs"
        value={when}
        onChange={(v) => update((x) => withTrigger(x, v))}
        testId="trigger-kind"
        options={[
          { value: "schedule", title: "On a schedule", sub: "once, or every day, week, hour…" },
          { value: "webhook", title: "When a webhook delivers", sub: "a service tells BattleLab" },
          { value: "manual", title: "Only when I press Run now", sub: "a button, nothing else" },
        ]}
      />

      {when === "schedule" && (
        <div className={styles.fields}>
          <div className={styles.field}>
            <label className={styles.label} htmlFor={f("repeats")}>
              Repeats
            </label>
            <select
              id={f("repeats")}
              className={styles.control}
              value={cadence}
              onChange={(e) => {
                const v = e.target.value;
                if (v === "once") return update((x) => withTrigger(x, "once"));
                update((x) => {
                  const s = withTrigger(x, "schedule");
                  if (!v.startsWith("interval-"))
                    return { ...s, cadenceKind: v as EditorForm["cadenceKind"] };
                  const hours = v === "interval-hours";
                  return {
                    ...s,
                    cadenceKind: "interval",
                    unit: hours ? "hours" : "minutes",
                    every: hours
                      ? Math.min(Math.max(1, s.every), 168)
                      : Math.max(list.limits.interval_min_minutes, s.every),
                  };
                });
              }}
              data-testid="cadence-kind"
            >
              <option value="once">Once, at a set time</option>
              <option value="interval-minutes">Every N minutes</option>
              <option value="interval-hours">Every N hours</option>
              <option value="daily">Daily</option>
              <option value="weekly">Weekly</option>
              <option value="monthly">Monthly</option>
            </select>
          </div>
          {form.triggerKind === "once" ? (
            <div className={styles.field}>
              <label className={styles.label} htmlFor={f("at")}>
                At
              </label>
              <input
                id={f("at")}
                className={styles.control}
                type="datetime-local"
                value={form.onceAt}
                onChange={(e) => set("onceAt", e.target.value.slice(0, 16))}
                data-testid="once-at"
              />
            </div>
          ) : form.cadenceKind === "interval" ? (
            <div className={styles.field}>
              <label className={styles.label} htmlFor={f("every")}>
                Every ({form.unit})
              </label>
              <input
                id={f("every")}
                className={styles.control}
                type="number"
                min={form.unit === "minutes" ? list.limits.interval_min_minutes : 1}
                max={form.unit === "minutes" ? 1440 : 168}
                value={form.every}
                onChange={(e) => set("every", Number(e.target.value))}
              />
            </div>
          ) : (
            <div className={styles.field}>
              <label className={styles.label} htmlFor={f("time")}>
                At
              </label>
              <input
                id={f("time")}
                className={styles.control}
                type="time"
                value={form.time}
                onChange={(e) => set("time", e.target.value)}
                data-testid="cadence-time"
              />
            </div>
          )}
          <TzField id={f("tz")} zones={zones} value={form.tz} onChange={(v) => set("tz", v)} />
          {form.triggerKind === "schedule" && form.cadenceKind === "weekly" && (
            <div className={`${styles.field} ${styles.full}`}>
              <span className={styles.label} id={f("days")}>
                On
              </span>
              <div className={styles.days} role="group" aria-labelledby={f("days")}>
                {WEEKDAYS.map((d) => (
                  <label key={d} className={styles.dayToggle}>
                    <input
                      type="checkbox"
                      checked={form.days.includes(d)}
                      onChange={(e) =>
                        set("days", e.target.checked ? [...form.days, d] : form.days.filter((x) => x !== d))
                      }
                    />
                    {DAY_LABEL[d]}
                  </label>
                ))}
              </div>
            </div>
          )}
          {form.triggerKind === "schedule" && form.cadenceKind === "monthly" && (
            <div className={styles.field}>
              <label className={styles.label} htmlFor={f("day")}>
                Day of the month
              </label>
              <input
                id={f("day")}
                className={styles.control}
                type="number"
                min={1}
                max={31}
                value={form.day}
                onChange={(e) => set("day", Number(e.target.value))}
              />
            </div>
          )}
        </div>
      )}

      {when === "webhook" && (
        <WebhookPicker
          id={f("webhook")}
          value={form.webhookId}
          list={webhooks.list}
          error={webhooks.error}
          nowS={nowS}
          onPick={(v) => set("webhookId", v)}
          onNew={onNewWebhook}
        />
      )}

      <p className={styles.callout} data-testid="trigger-callout">
        {when === "webhook"
          ? "It runs when the picked webhook delivers a signed event. Next, choose which of its events start it. There is no Run now: only a delivery starts it."
          : when === "manual"
            ? "It runs only when you press Run now."
            : `${draftTrigger.detail}. Every ${list.limits.interval_min_minutes} minutes is the shortest schedule. A time skipped by a clock change runs once, at the next valid minute. Runs missed while BattleLab was down collapse into one catch-up run.`}
      </p>
      {when !== "webhook" && (
        <p className={styles.hint} data-testid="events-skipped-note">
          {when === "manual" ? "Run now" : "A schedule"} has no events to choose, so step 2 is
          skipped.
        </p>
      )}

      {when === "webhook" &&
        (picked ? (
          <SharedWebhookPanel {...picked} nowS={nowS} />
        ) : (
          <p className={styles.hint} data-testid="webhook-pick-hint">
            Pick a webhook: its address and last delivery show here. Its secret is on its own page
            in Library → Webhooks.
          </p>
        ))}
    </>
  );
}

/** The picked webhook's facts for its panel: from the list, else from the saved row (the
 *  automation's own read carries the picked webhook's public view). `null` while none is picked. */
function pickedWebhook(
  webhookId: string,
  list: Webhook[] | null,
  saved: Automation | null,
): Omit<Parameters<typeof SharedWebhookPanel>[0], "nowS"> | null {
  if (!webhookId) return null;
  const w = list?.find((x) => x.id === webhookId);
  const fromRow = saved?.webhook?.webhook_id === webhookId ? saved.webhook : null;
  const src = w ?? fromRow;
  if (!src) return null;
  return {
    id: webhookId,
    name: src.name ?? webhookId,
    path: src.path ?? `/hooks/w/${webhookId}`,
    lastDeliveryAt: src.last_delivery_at,
    lastEvent: src.last_event,
    lastNote: src.last_note,
    unsigned24h: src.unsigned_24h,
  };
}
