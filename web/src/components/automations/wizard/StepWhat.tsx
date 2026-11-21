/** Step 3 — What (#2221 P2b): start a session, message a session, a review loop, or tell HQ — and,
 *  on a schedule or Run now, start a mission, as before. A webhook starts or messages an API session
 *  only. "Tell HQ" puts the message in the operator's notifications; HQ is told only that a message
 *  from this automation arrived (never what it says) and never acts on it.
 *
 *  A template message's fields each take a FIXED value or, on a webhook from the list, a value FROM
 *  THE DELIVERY: one field of it (never a list), read as a type (number, choice from a list, short
 *  text, yes/no). A value that fails its type refuses that delivery and nothing starts. Only the
 *  template's own typed fields can be filled this way — never a library field, never a secret. */
import { useState } from "react";
import { Link } from "react-router-dom";

import { engineLabel, rosterReady, runsInTerminal } from "../../../app/engineRoster";
import { settingsPath } from "../../../routes/settingsTabs";
import { LegacyTerminalStamp, SwitchToApiButton } from "../SwitchToApi";
import {
  AUTONOMY_WORDS,
  MAPPED_TYPE_WORDS,
  CHOICES_MAX,
  agentPicker,
  fromListHook,
  legacyTrigger,
  mappableFields,
  reviewerOptions,
  sessionEngine,
  sessionPicker,
  withAction,
  withFieldSource,
  type EditorForm,
  type MappingDraft,
} from "../../../lib/automations";
import type { Session, Template, TemplateVariable } from "../../../types/api";
import type { AutomationList, Autonomy, MappedType } from "../../../types/automations";
import type { ShapeField } from "../../../types/webhooks";
import styles from "../automations.module.css";
import { Cards, Fold, Seg, StepIssues, type CardOption, type StepProps } from "./bits";
import { ReviewLoopFields } from "./ReviewLoopFields";
import w from "./wizard.module.css";

type Kind = EditorForm["actionKind"];

const CARD: Record<Kind, CardOption<Kind>> = {
  start_mission: { value: "start_mission", title: "Start a mission", sub: "HQ drafts it in a project" },
  start_session: { value: "start_session", title: "Start a session", sub: "one agent, one message" },
  send_to_session: { value: "send_to_session", title: "Message a session", sub: "an existing one" },
  forge_review_loop: {
    value: "forge_review_loop",
    title: "Review loop",
    sub: "a specialist reviews each issue and PR",
  },
  tell_hq: { value: "tell_hq", title: "Tell HQ", sub: "a message in your notifications; HQ may comment" },
};

export function StepWhat({
  form,
  set,
  update,
  f,
  list,
  stored,
  projects,
  sessions,
  templates,
  variables,
  reviewers,
  reviewerChosen,
  specialistsLoaded,
  switchButton,
  onPickFolder,
  shape,
  problems,
  warnings,
}: StepProps & {
  list: AutomationList;
  /** What the SAVED automation targets ("" for none, or a new one): a terminal target stays
   *  visible in its picker, stamped, though a new automation is offered API agents only. */
  stored: { engine: string; sessionKey: string };
  projects: { id: string; name: string; default_folder?: string | null }[];
  sessions: Session[] | null;
  templates: Template[] | null;
  variables: TemplateVariable[];
  reviewers: ReturnType<typeof reviewerOptions>;
  reviewerChosen: ReturnType<typeof reviewerOptions>[number] | null;
  specialistsLoaded: boolean;
  /** "Switch to API client" for the SAVED automation, when it has one to switch to. */
  switchButton: Parameters<typeof SwitchToApiButton>[0] | null;
  onPickFolder: (opener: HTMLElement) => void;
  shape: ShapeField[];
  problems: string[];
  warnings: string[];
}) {
  // The model (fixed for now) and permission bypass are folded away; bypass on keeps them open.
  const [moreOpen, setMoreOpen] = useState<boolean | null>(null);
  const engines = list.engines ?? [];
  const maxConcurrent = list.limits.max_concurrent_max;
  const hook = form.triggerKind === "webhook";
  const listHook = fromListHook(form);
  // A review loop's own webhook starts only its loop — while it IS the trigger. On a schedule or Run
  // now the stored identity waits, for a switch back, and offers what those triggers can start.
  const legacy = legacyTrigger(form);
  const kinds: Kind[] = legacy
    ? ["forge_review_loop"]
    : hook
      ? ["start_session", "send_to_session", "forge_review_loop", "tell_hq"]
      : ["start_mission", "start_session", "send_to_session", "tell_hq"];
  const loop = form.actionKind === "forge_review_loop";
  const template = templates?.find((t) => t.id === form.templateId) ?? null;
  const mappable = listHook ? mappableFields(template) : [];
  // API first (#2244): the pickers offer API agents and API sessions; what the automation already
  // targets stays visible, whatever it runs in.
  const agents = agentPicker(engines, [stored.engine, form.engine], engineLabel);
  const keepSessions = [stored.sessionKey, form.sessionKey].filter(Boolean);
  const sessionChoices = sessionPicker(sessions ?? [], keepSessions);
  const sessionKnown = sessionChoices.some((s) => s.id === form.sessionKey);
  const terminal = (engine: string) => listHook && runsInTerminal(engine) === true;
  const msgWord =
    form.actionKind === "start_mission"
      ? "Instruction"
      : form.actionKind === "start_session"
        ? "First message"
        : "Message";
  const setMapping = (field: string, patch: Partial<MappingDraft>) =>
    update((x) => ({
      ...x,
      fromDelivery: { ...x.fromDelivery, [field]: { ...x.fromDelivery[field], ...patch } },
    }));

  return (
    <>
      <Cards<Kind>
        label="What it does"
        value={form.actionKind}
        onChange={(v) => update((x) => withAction(x, v, maxConcurrent))}
        testId="action-kind"
        options={kinds.map((k) => CARD[k])}
      />
      <p className={styles.hint} data-testid="action-why">
        {legacy
          ? "Its own Forgejo webhook starts only its review loop."
          : hook
            ? "A webhook starts or messages an API session, runs a review loop, or tells HQ. Starting a mission from a webhook comes in a later release."
            : "A review loop starts from a webhook: choose “When a webhook delivers” in step 1."}
      </p>

      {loop && (
        <ReviewLoopFields
          form={form}
          set={set}
          f={f}
          projects={projects}
          reviewers={reviewers}
          reviewerChosen={reviewerChosen}
          specialistsLoaded={specialistsLoaded}
        />
      )}

      {form.actionKind === "start_mission" && (
        <div className={styles.fields}>
          <div className={styles.field}>
            <label className={styles.label} htmlFor={f("project")}>
              Project
            </label>
            <select
              id={f("project")}
              className={styles.control}
              value={form.projectId}
              onChange={(e) => set("projectId", e.target.value)}
              data-testid="mission-project"
            >
              <option value="">Choose a project…</option>
              {projects.map((p) => (
                <option key={p.id} value={p.id}>
                  {p.name}
                  {p.default_folder ? ` · ${p.default_folder}` : ""}
                </option>
              ))}
              {form.projectId && !projects.some((p) => p.id === form.projectId) && (
                <option value={form.projectId}>{form.projectId}</option>
              )}
            </select>
          </div>
          <div className={`${styles.field} ${styles.full}`}>
            <label className={styles.label} htmlFor={f("autonomy")}>
              What the mission may do on its own
            </label>
            <select
              id={f("autonomy")}
              className={styles.control}
              value={form.autonomy}
              onChange={(e) => set("autonomy", e.target.value as Autonomy)}
              data-testid="mission-autonomy"
            >
              {(Object.keys(AUTONOMY_WORDS) as Autonomy[]).map((k) => (
                <option key={k} value={k}>
                  {AUTONOMY_WORDS[k]}
                </option>
              ))}
            </select>
            <p className={styles.hint}>
              An automated mission never runs with permission bypass, and it can never raise your
              mission-control tier.
            </p>
          </div>
        </div>
      )}

      {form.actionKind === "start_session" && (
        <div className={styles.fields}>
          <div className={styles.field}>
            <label className={styles.label} htmlFor={f("engine")}>
              Agent
            </label>
            <select
              id={f("engine")}
              className={styles.control}
              value={form.engine}
              onChange={(e) => set("engine", e.target.value)}
              aria-describedby={f("engine-note")}
              data-testid="session-engine"
            >
              <option value="">Choose an agent…</option>
              {agents.offered.map((id) => (
                <option key={id} value={id}>
                  {engineLabel(id)}
                </option>
              ))}
              {agents.kept.map((id) => (
                <option key={id} value={id} disabled={terminal(id)}>
                  {engineLabel(id)}
                  {terminal(id) ? " — terminal, not for a webhook" : ""}
                </option>
              ))}
            </select>
            {form.engine && runsInTerminal(form.engine) === true && <LegacyTerminalStamp />}
            {switchButton && <SwitchToApiButton {...switchButton} />}
            {agents.offered.length ? (
              <p id={f("engine-note")} className={styles.hint} data-testid="engine-api-note">
                New automations use API agents.
              </p>
            ) : (
              // Not while the roster is still loading: that is "not known yet", never "none".
              rosterReady() && <p id={f("engine-note")} className={styles.hint} data-testid="engine-none">
                No API agent can start a new automation on this server yet.{" "}
                <Link className={styles.link} to={settingsPath("agents")}>
                  Settings → Agents
                </Link>
              </p>
            )}
            {agents.why.length > 0 && (
              <details className={w.why} data-testid="engine-why">
                <summary className={w.whyHead}>Why aren’t other agents listed?</summary>
                <ul className={w.whyList}>
                  {agents.why.map((x) => (
                    <li key={x.id}>{x.line}</li>
                  ))}
                </ul>
              </details>
            )}
          </div>
          <div className={`${styles.field} ${styles.full}`}>
            <label className={styles.label} htmlFor={f("folder")}>
              Folder
            </label>
            <div className={styles.inline}>
              <input
                id={f("folder")}
                className={`${styles.control} ${styles.mono}`}
                value={form.folder}
                readOnly
                placeholder="Choose a folder…"
                data-testid="session-folder"
              />
              <button type="button" className={styles.ghost} onClick={(e) => onPickFolder(e.currentTarget)}>
                Choose…
              </button>
            </div>
          </div>
          <div className={styles.full}>
            <Fold
              summary="Model and permissions"
              open={moreOpen ?? form.bypass}
              onToggle={setMoreOpen}
              testId="session-more"
            >
              <div className={styles.fields}>
                  <div className={styles.field}>
                    <label className={styles.label} htmlFor={f("model")}>
                      Model
                    </label>
                    <select id={f("model")} className={styles.control} disabled value="default" aria-describedby={f("model-hint")}>
                      <option value="default">The agent’s default</option>
                    </select>
                    <p id={f("model-hint")} className={styles.hint}>
                      Choosing a model per session comes in a later release; until then it runs the agent’s
                      default.
                    </p>
                  </div>
                  <div className={`${styles.field} ${styles.full}`}>
                    <label className={styles.check}>
                      <input
                        type="checkbox"
                        checked={form.bypass}
                        onChange={(e) => set("bypass", e.target.checked)}
                        data-testid="session-bypass"
                      />
                      Permission bypass — the agent runs without asking before each action
                    </label>
                    {form.bypass && (
                      <p className={`${styles.notice} ${styles.noticeWarn}`}>
                        <strong>Unattended with bypass</strong>
                        <span>
                          Sessions this starts will act without asking you, while you are away. Turning it on
                          asks for your consent again.
                        </span>
                      </p>
                    )}
                  </div>
              </div>
            </Fold>
          </div>
        </div>
      )}

      {form.actionKind === "tell_hq" && (
        <p className={styles.hint} data-testid="tell-hq-what">
          The message goes in your notifications (the bell). HQ is told that a message from this automation
          arrived — never what it says — and may comment on it. HQ never acts on it by itself.
        </p>
      )}

      {form.actionKind === "send_to_session" && (
        <div className={styles.field}>
          <label className={styles.label} htmlFor={f("session")}>
            Session
          </label>
          <select
            id={f("session")}
            className={styles.control}
            value={form.sessionKey}
            onChange={(e) => set("sessionKey", e.target.value)}
            data-testid="send-session"
          >
            <option value="">Choose a session…</option>
            {sessionChoices.map((s) => (
              <option key={s.id} value={s.id} disabled={terminal(s.engine)}>
                {s.title || "(untitled)"} · {engineLabel(s.engine)}
                {s.running ? " · running" : ""}
                {terminal(s.engine) ? " — terminal, not for a webhook" : ""}
              </option>
            ))}
            {form.sessionKey && !sessionKnown && <option value={form.sessionKey}>{form.sessionKey}</option>}
          </select>
          {form.sessionKey && runsInTerminal(sessionEngine(form.sessionKey)) === true && <LegacyTerminalStamp />}
          {form.sessionKey && (
            <p className={`${styles.hint} ${styles.mono}`} data-testid="send-session-key">
              {form.sessionKey}
            </p>
          )}
          <p className={styles.hint} data-testid="send-session-api-note">
            New automations message API sessions.
          </p>
          <p className={styles.hint}>
            It types into that session as it is — with whatever permissions it already runs with. If
            the session isn’t running then, the run is refused and recorded; nothing is started in
            its place.
            {listHook && sessionEngine(form.sessionKey) === "" && " A webhook messages API sessions only."}
          </p>
        </div>
      )}

      {!loop && (
        <>
          <div className={styles.fields}>
            <div className={styles.field}>
              <label className={styles.label} htmlFor={f("mode")}>
                {msgWord}
              </label>
              <select
                id={f("mode")}
                className={styles.control}
                value={form.messageMode}
                onChange={(e) => set("messageMode", e.target.value as EditorForm["messageMode"])}
                data-testid="message-mode"
              >
                <option value="text">Plain text</option>
                <option value="template">Template</option>
              </select>
            </div>
            {form.messageMode === "template" && (
              <div className={styles.field}>
                <label className={styles.label} htmlFor={f("template")}>
                  Template
                </label>
                <select
                  id={f("template")}
                  className={styles.control}
                  value={form.templateId}
                  onChange={(e) =>
                    update((x) => ({ ...x, templateId: e.target.value, values: {}, fromDelivery: {} }))
                  }
                  data-testid="message-template"
                >
                  <option value="">Choose a template…</option>
                  {(templates ?? []).map((t) => (
                    <option key={t.id} value={t.id}>
                      {t.name}
                    </option>
                  ))}
                </select>
              </div>
            )}
            {form.messageMode === "text" && (
              <div className={`${styles.field} ${styles.full}`}>
                <label className={styles.label} htmlFor={f("text")}>
                  {form.actionKind === "start_mission" ? "What the mission should do" : "What to send"}
                </label>
                <textarea
                  id={f("text")}
                  className={styles.control}
                  value={form.text}
                  onChange={(e) => set("text", e.target.value)}
                  data-testid="message-text"
                />
                {listHook && (
                  <p className={styles.hint}>
                    Plain text is sent as written. To fill a value from the delivery, use a template.
                  </p>
                )}
              </div>
            )}
          </div>
          {form.messageMode === "template" && template && (
            <>
              <p className={styles.hint}>
                The template’s revision is pinned. If it is edited later, this automation pauses until
                you approve the new version.
                {listHook &&
                  " A value from the delivery is checked against its type on every delivery: one that fails refuses that delivery, and nothing starts."}
              </p>
              <datalist id={f("map-shape")}>
                {shape
                  .filter((s) => !s.path.includes("[]") && s.type !== "list" && s.type !== "object")
                  .map((s) => (
                    <option key={s.path} value={s.path}>
                      {s.type}
                    </option>
                  ))}
              </datalist>
              <div data-testid="template-values">
                {template.fields.map((fld) => {
                  const lib = variables.find((v) => v.name === fld.name);
                  const map = form.fromDelivery[fld.name];
                  const canMap = mappable.includes(fld.name);
                  const label = fld.label || fld.name;
                  return (
                    <div key={fld.name} className={w.valueRow} data-testid="template-field" data-field={fld.name}>
                      <div className={w.valueHead}>
                        <label className={styles.label} htmlFor={f(map ? `map-${fld.name}` : `v-${fld.name}`)}>
                          {label}
                        </label>
                        {canMap ? (
                          <Seg<"fixed" | "delivery">
                            label={`Where ${label} comes from`}
                            value={map ? "delivery" : "fixed"}
                            onChange={(v) => update((x) => withFieldSource(x, fld.name, v))}
                            testId="field-source"
                            options={[
                              { value: "fixed", label: "Fixed value" },
                              { value: "delivery", label: "From the delivery" },
                            ]}
                          />
                        ) : (
                          <span className={styles.fieldSource}>
                            {fld.source === "library"
                              ? "from your variable library"
                              : fld.kind === "secret"
                                ? "a typed secret — an automation can’t keep one"
                                : "fixed value"}
                          </span>
                        )}
                      </div>
                      {fld.source === "library" ? (
                        <input
                          id={f(`v-${fld.name}`)}
                          className={`${styles.control} ${styles.mono}`}
                          readOnly
                          value={
                            lib?.kind === "secret"
                              ? `{{${fld.name}}} → [secret: ${fld.name}]`
                              : lib
                                ? `{{${fld.name}}} → ${lib.value ?? ""}`
                                : `{{${fld.name}}} → not in the library`
                          }
                        />
                      ) : map ? (
                        <div className={w.mapFields}>
                          <input
                            id={f(`map-${fld.name}`)}
                            className={`${styles.control} ${styles.mono}`}
                            list={f("map-shape")}
                            value={map.path}
                            maxLength={160}
                            placeholder="pull_request.number"
                            aria-label={`${label}: field of the delivery`}
                            autoCapitalize="none"
                            autoCorrect="off"
                            spellCheck={false}
                            onChange={(e) => setMapping(fld.name, { path: e.target.value.trim() })}
                            data-testid="map-path"
                          />
                          <select
                            className={styles.control}
                            value={map.type}
                            aria-label={`${label}: read as`}
                            onChange={(e) => setMapping(fld.name, { type: e.target.value as MappedType })}
                            data-testid="map-type"
                          >
                            {(Object.keys(MAPPED_TYPE_WORDS) as MappedType[]).map((t) => (
                              <option key={t} value={t}>
                                {MAPPED_TYPE_WORDS[t]}
                              </option>
                            ))}
                          </select>
                          {map.type === "choice" && (
                            <div
                              className={`${w.values} ${styles.full}`}
                              role="group"
                              aria-label={`${label}: allowed values`}
                              data-testid="map-choices"
                            >
                              {(map.choices.length ? map.choices : [""]).map((c, j) => (
                                <div key={j} className={w.choiceLine}>
                                  <input
                                    className={styles.control}
                                    value={c}
                                    placeholder={`allowed value ${j + 1}`}
                                    aria-label={`${label}: allowed value ${j + 1}`}
                                    autoCapitalize="none"
                                    autoCorrect="off"
                                    onChange={(e) => {
                                      const next = map.choices.length ? [...map.choices] : [""];
                                      next[j] = e.target.value;
                                      setMapping(fld.name, { choices: next });
                                    }}
                                    data-testid="map-choice"
                                  />
                                  <button
                                    type="button"
                                    className={w.remove}
                                    aria-label={`Remove allowed value ${j + 1}`}
                                    disabled={map.choices.length <= 1}
                                    onClick={() =>
                                      setMapping(fld.name, { choices: map.choices.filter((_, k) => k !== j) })
                                    }
                                    data-testid="map-choice-remove"
                                  >
                                    ×
                                  </button>
                                </div>
                              ))}
                              <div className={w.actions}>
                                <button
                                  type="button"
                                  className={styles.ghost}
                                  disabled={map.choices.length >= CHOICES_MAX}
                                  onClick={() =>
                                    setMapping(fld.name, {
                                      choices: [...(map.choices.length ? map.choices : [""]), ""],
                                    })
                                  }
                                  data-testid="map-choice-add"
                                >
                                  Add a value
                                </button>
                              </div>
                            </div>
                          )}
                        </div>
                      ) : (
                        <input
                          id={f(`v-${fld.name}`)}
                          className={styles.control}
                          value={form.values[fld.name] ?? ""}
                          placeholder={fld.default || undefined}
                          disabled={fld.kind === "secret"}
                          onChange={(e) => set("values", { ...form.values, [fld.name]: e.target.value })}
                          data-testid="field-value"
                        />
                      )}
                    </div>
                  );
                })}
              </div>
            </>
          )}
          {form.actionKind === "tell_hq" ? (
            <p className={styles.hint}>
              A message to HQ never carries a secret: it is shown in your notifications, so a template
              with a secret is refused.
            </p>
          ) : form.actionKind !== "send_to_session" && (
            <p className={styles.hint}>
              {form.actionKind === "start_mission" ? "Mission briefs" : "A new session’s first message"} never
              carry secrets: a template with a secret can only be sent to a running session.
            </p>
          )}
        </>
      )}

      <StepIssues problems={problems} warnings={warnings} />
    </>
  );
}
