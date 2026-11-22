/** Step 5 — Review & enable (#2221 P2b): the name, the draft in the form's words, the SAVED scope in
 *  the server's words (`scope_lines`), then Save — a new automation is always saved OFF — and the
 *  existing enable / consent flow. Nothing here derives a scope: what is consented to is the
 *  server's. */
import type { ReactNode } from "react";

import { engineName } from "../../../app/engineRoster";
import { InFlightNote, KillSwitchNotice, StateWord } from "../AutomationBits";
import {
  actionLabel,
  eventsWords,
  formTrigger,
  limitsWords,
  triggerWords,
  type EditorForm,
} from "../../../lib/automations";
import type { Automation, AutomationList } from "../../../types/automations";
import type { WebhookPreset } from "../../../types/webhooks";
import styles from "../automations.module.css";
import { StepIssues, type StepProps } from "./bits";
import w from "./wizard.module.css";

export function StepReview({
  form,
  set,
  f,
  list,
  saved,
  dirty,
  presets,
  projectName,
  problems,
  error,
  notice,
  inFlight,
  actions,
  secretPanel,
}: StepProps & {
  list: AutomationList;
  saved: Automation | null;
  dirty: boolean;
  presets: WebhookPreset[];
  projectName: (id: string) => string;
  problems: string[];
  error: ReactNode;
  notice: string | null;
  inFlight: string;
  actions: ReactNode;
  /** "Call a webhook" (#2248 P2): the signing secret from the save that made it, shown once. */
  secretPanel?: ReactNode;
}) {
  const trigger = triggerWords(formTrigger(form));
  const events = eventsWords(form, presets);
  return (
    <>
      {saved && (
        <p className={styles.lede} data-testid="editor-state">
          <StateWord state={saved.state} /> · revision {saved.revision}
          {saved.enabled
            ? ". A change that widens what it may do on its own asks for your consent again; until then it keeps the old scope."
            : ". It is off: nothing runs until you enable it."}
        </p>
      )}
      {!list.loop.enabled && <KillSwitchNotice />}

      <div className={styles.field}>
        <label className={styles.label} htmlFor={f("name")}>
          Name
        </label>
        <input
          id={f("name")}
          className={styles.control}
          value={form.name}
          maxLength={80}
          onChange={(e) => set("name", e.target.value)}
          data-testid="automation-name"
        />
      </div>

      <dl className={w.summary} data-testid="wizard-summary">
        <dt>When</dt>
        <dd>
          {trigger.label} — {trigger.detail}
        </dd>
        {events && (
          <>
            <dt>Events</dt>
            <dd data-testid="summary-events">{events}</dd>
          </>
        )}
        <dt>What</dt>
        <dd>{whatWords(form, projectName)}</dd>
        <dt>Limits</dt>
        <dd>{limitsWords(form)}</dd>
      </dl>

      <section className={styles.panel} aria-labelledby={f("scope")} data-testid="editor-scope">
        <h2 id={f("scope")} className={styles.panelTitle}>
          What this will do, unattended
        </h2>
        {saved && !saved.scope ? (
          <p className={styles.hint} data-testid="editor-scope-unavailable">
            Its inputs can’t be checked right now, so its scope can’t be shown. Nothing new is
            approved until they can.
          </p>
        ) : saved && saved.scope_lines.length > 0 ? (
          <ul className={styles.scopeLines} data-testid="editor-scope-lines">
            {saved.scope_lines.map((l, i) => (
              <li key={i}>{l}</li>
            ))}
          </ul>
        ) : (
          <p className={styles.hint}>
            Save it to see its full scope in the server’s words. It is saved off: nothing runs until
            you enable it, and enabling shows this scope again for your consent.
          </p>
        )}
        {saved && dirty && (
          <div className={`${styles.notice} ${styles.noticeWarn} ${styles.more}`} data-testid="editor-draft">
            <strong>Unsaved changes</strong>
            <span>
              Above is what is saved. Your draft: {actionLabel(form.actionKind).toLowerCase()},{" "}
              {trigger.label.toLowerCase()} ({trigger.detail})
              {form.actionKind === "start_session" && form.bypass ? ", with permission bypass" : ""}.
              {saved.enabled ? " If saving widens what it may do, you are asked to consent first." : ""}
            </span>
          </div>
        )}
        <p className={`${styles.hint} ${styles.more}`}>
          Anything that widens it — a new target, bypass on, more autonomy, a different schedule, other
          events, a new value from the delivery, a higher cap, new content — asks for your consent
          again.
        </p>
      </section>

      <StepIssues problems={problems} testId="editor-problems" />
      {error}
      {notice && (
        <div className={`${styles.notice} ${styles.noticeOk}`} role="status" data-testid="editor-notice">
          {notice}
          {inFlight && <InFlightNote text={inFlight} />}
        </div>
      )}
      {secretPanel}
      <div className={w.actions}>{actions}</div>
    </>
  );
}

function whatWords(form: EditorForm, projectName: (id: string) => string): string {
  const label = actionLabel(form.actionKind);
  switch (form.actionKind) {
    case "start_mission":
      return form.projectId ? `${label} in ${projectName(form.projectId)}` : label;
    case "start_session":
      return [label, form.engine && engineName(form.engine), form.folder && `in ${form.folder}`]
        .filter(Boolean)
        .join(" · ");
    case "send_to_session":
      return form.sessionKey ? `${label} · ${form.sessionKey}` : label;
    case "forge_review_loop":
      return form.repo.trim() ? `${label} on ${form.repo.trim()}` : label;
    case "tell_hq":
      return `${label} · a message in your notifications`;
    case "send_message":
      return form.recipient ? `${label} · to ${form.recipient}` : label;
    case "publish_artifact":
      return form.artifactPath.trim() ? `${label} · ${form.artifactPath.trim()}` : label;
    case "call_webhook":
      return form.destination.trim() ? `${label} · ${form.destination.trim()}` : label;
  }
}
