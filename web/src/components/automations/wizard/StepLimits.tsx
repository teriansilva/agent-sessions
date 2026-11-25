/** Step 4 — Limits (#2221 P2b): the policy in one line, its fields folded behind it. What the line
 *  says is what is saved: the policy is always sent in full. */
import { useState } from "react";

import { blankForm, limitsWords } from "../../../lib/automations";
import type { AutomationList } from "../../../types/automations";
import styles from "../automations.module.css";
import { Fold, type StepProps } from "./bits";
import { fromLocalInput, toLocalInput } from "./time";

export function StepLimits({
  form,
  set,
  update,
  f,
  list,
}: StepProps & { list: AutomationList }) {
  const [open, setOpen] = useState(false);
  const blank = blankForm(form.tz);
  const isDefault =
    form.concurrency === blank.concurrency &&
    form.maxRunsPerDay === blank.maxRunsPerDay &&
    form.pauseAfterFailures === blank.pauseAfterFailures &&
    form.expiresAt == null;
  return (
    <>
      <p className={styles.callout} data-testid="limits-summary">
        {isDefault ? "Defaults: " : "Limits: "}
        {limitsWords(form)}.
      </p>
      <Fold summary="Change the limits" open={open} onToggle={setOpen} testId="limits-more">
        <div className={styles.fields}>
          <div className={styles.field}>
            <label className={styles.label} htmlFor={f("concurrency")}>
              If the previous run is still going
            </label>
            <select
              id={f("concurrency")}
              className={styles.control}
              value={form.concurrency === "skip" ? "skip" : `allow-${form.maxConcurrent}`}
              onChange={(e) => {
                const v = e.target.value;
                update((x) =>
                  v === "skip"
                    ? { ...x, concurrency: "skip" }
                    : { ...x, concurrency: "allow", maxConcurrent: Number(v.split("-")[1]) },
                );
              }}
              data-testid="limit-concurrency"
            >
              <option value="skip">Skip this run and record why</option>
              {Array.from({ length: list.limits.max_concurrent_max - 1 }, (_, i) => i + 2).map((n) => (
                <option key={n} value={`allow-${n}`}>
                  Run anyway, up to {n} at once
                </option>
              ))}
            </select>
          </div>
          <div className={styles.field}>
            <label className={styles.label} htmlFor={f("cap")}>
              At most, per day
            </label>
            <input
              id={f("cap")}
              className={styles.control}
              type="number"
              min={1}
              max={list.limits.max_runs_per_day_max}
              value={form.maxRunsPerDay}
              onChange={(e) => set("maxRunsPerDay", Number(e.target.value))}
              data-testid="limit-per-day"
            />
          </div>
          <div className={styles.field}>
            <label className={styles.label} htmlFor={f("pause")}>
              Pause after this many failures in a row
            </label>
            <input
              id={f("pause")}
              className={styles.control}
              type="number"
              min={1}
              max={list.limits.pause_after_failures_max}
              value={form.pauseAfterFailures}
              onChange={(e) => set("pauseAfterFailures", Number(e.target.value))}
              data-testid="limit-pause"
            />
          </div>
          <div className={styles.field}>
            <label className={styles.label} htmlFor={f("expires")}>
              Stop after (your time; empty = never)
            </label>
            <input
              id={f("expires")}
              className={styles.control}
              type="datetime-local"
              value={toLocalInput(form.expiresAt)}
              onChange={(e) => set("expiresAt", fromLocalInput(e.target.value))}
              data-testid="limit-expires"
            />
          </div>
        </div>
      </Fold>
    </>
  );
}
