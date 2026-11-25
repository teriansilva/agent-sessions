/** Step 3 — What (#2221 P2b): start a session, message a session, or tell HQ — and,
 *  on a schedule or Run now, start a mission, as before. A webhook starts or messages an API session
 *  only. "Tell HQ" puts the message in the operator's notifications; HQ is told only that a message
 *  from this automation arrived (never what it says) and never acts on it.
 *
 *  A template message's fields each take a FIXED value or, on a webhook from the list, a value FROM
 *  THE DELIVERY: one field of it (never a list), read as a type (number, choice from a list, short
 *  text, yes/no). A value that fails its type refuses that delivery and nothing starts. Only the
 *  template's own typed fields can be filled this way — never a library field, never a secret.
 *
 *  The action is picked from the action gallery (#2248 P1, `GET /api/actions`): a compact,
 *  searchable list of what this trigger can start, the same list Library → Actions shows. */
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
  type messengers as messengersOf,
  mappableFields,
  sessionEngine,
  sessionPicker,
  specialistChoices,
  withAction,
  withEngine,
  type ReviewerOption,
  withFieldSource,
  type EditorForm,
  type MappingDraft,
} from "../../../lib/automations";
import type { Session, Template, TemplateVariable } from "../../../types/api";
import type { AutomationList, Autonomy, MappedType } from "../../../types/automations";
import type { ShapeField } from "../../../types/webhooks";
import styles from "../automations.module.css";
import { ActionPicker } from "../../actions/ActionBits";
import type { useActions } from "../../actions/useActions";
import { offeredActions } from "../../../lib/actions";
import { CallWebhookFields } from "../../actions/CallWebhookFields";
import { PublishArtifactFields } from "../../actions/PublishArtifactFields";
import { RunLocalFields } from "../../actions/RunLocalFields";
import type { ReactNode } from "react";
import { Fold, Seg, StepIssues, type StepProps } from "./bits";
import w from "./wizard.module.css";

type Kind = EditorForm["actionKind"];

/** The actions this wizard knows how to set up. The gallery (`GET /api/actions`) says which exist,
 *  what each is called and which trigger starts it; an action this build has no fields for is not
 *  offered (#2248 P1). */
const KNOWN: readonly Kind[] = Object.keys({
  start_mission: 1,
  start_session: 1,
  send_to_session: 1,
  tell_hq: 1,
  send_message: 1,
  call_webhook: 1,
  publish_artifact: 1,
  write_memory: 1,
  run_command: 1,
  run_script: 1,
} satisfies Record<Kind, 1>) as Kind[];

export function StepWhat({
  form,
  set,
  update,
  f,
  list,
  actions,
  stored,
  projects,
  messengers,
  sessions,
  templates,
  variables,
  switchButton,
  onPickFolder,
  shape,
  problems,
  warnings,
  savedDestination,
  onNewSecret,
  newSecretBusy,
  secretPanel,
  specialists,
  specialistsFailed,
}: StepProps & {
  /** The specialist read failed (not "there are none"). */
  specialistsFailed: boolean;
  /** "Run as a specialist" (#2222): the specialists an automation may start, or `null` while they
   *  load. */
  specialists: ReviewerOption[] | null;
  /** "Call a webhook" (#2248 P2): the SAVED destination's state, and making a new secret. */
  savedDestination: { url: string; secret_set: boolean } | null;
  onNewSecret: (() => void) | null;
  newSecretBusy: boolean;
  secretPanel: ReactNode;
  list: AutomationList;
  /** The action gallery, owned by the editor so its readiness holds the step (#2248 P1). */
  actions: ReturnType<typeof useActions>;
  /** What the SAVED automation targets ("" for none, or a new one): a terminal target stays
   *  visible in its picker, stamped, though a new automation is offered API agents only. */
  stored: { engine: string; sessionKey: string };
  projects: { id: string; name: string; default_folder?: string | null }[];
  /** The messengers "Send a message" may use, or `null` while they load (#2248 P2). */
  messengers: ReturnType<typeof messengersOf> | null;
  sessions: Session[] | null;
  templates: Template[] | null;
  variables: TemplateVariable[];
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
  const hook = form.triggerKind === "webhook";
  const listHook = fromListHook(form);
  // What it does comes from the action gallery (#2248 P1): the actions whose manifest says this
  // trigger starts them, in the gallery's order. The server's check at save is the authority.
  const offered = actions.gallery
    ? offeredActions(actions.gallery.actions, form.triggerKind, KNOWN)
    : [];
  const template = templates?.find((t) => t.id === form.templateId) ?? null;
  const mappable = listHook ? mappableFields(template) : [];
  // API first (#2244): the pickers offer API agents and API sessions; what the automation already
  // targets stays visible, whatever it runs in.
  const agents = agentPicker(engines, [stored.engine, form.engine], engineLabel);
  const keepSessions = [stored.sessionKey, form.sessionKey].filter(Boolean);
  const sessionChoices = sessionPicker(sessions ?? [], keepSessions);
  const sessionKnown = sessionChoices.some((s) => s.id === form.sessionKey);
  const terminal = (engine: string) => listHook && runsInTerminal(engine) === true;
  const messenger = messengers?.find((m) => m.instance.id === form.connector) ?? null;
  const msgWord =
    form.actionKind === "start_mission"
      ? "Instruction"
      : form.actionKind === "start_session"
        ? "First message"
        : form.actionKind === "write_memory"
          ? "Note"
          : "Message";
  const setMapping = (field: string, patch: Partial<MappingDraft>) =>
    update((x) => ({
      ...x,
      fromDelivery: { ...x.fromDelivery, [field]: { ...x.fromDelivery[field], ...patch } },
    }));

  return (
    <>
      {actions.error && (
        <div className={`${styles.notice} ${styles.noticeBad}`} role="alert" data-testid="action-kind-error">
          <strong>Couldn’t load the actions</strong>
          <span>
            {actions.error}. Nothing was changed
            {actions.gallery ? "; the list below is the last one read" : ""}. Next waits until they load.
          </span>
          <div className={styles.noticeRow}>
            <button type="button" className={styles.ghost} onClick={actions.reload} disabled={actions.loading}>
              {actions.loading ? "Retrying…" : "Retry"}
            </button>
          </div>
        </div>
      )}
      {actions.gallery && actions.loading && !actions.error && (
        <p className={styles.hint} role="status" data-testid="action-kind-checking">
          Checking the action list… Next waits for it.
        </p>
      )}
      {actions.gallery ? (
        <ActionPicker
          gallery={actions.gallery}
          offered={offered}
          value={form.actionKind}
          onChange={(v) => update((x) => withAction(x, v as Kind))}
          testId="action-kind"
        />
      ) : (
        !actions.error && (
          <p className={styles.hint} role="status">
            Loading actions…
          </p>
        )
      )}
      <p className={styles.hint} data-testid="action-why">
        {hook
          ? "A webhook starts or messages an API session, or tells HQ. Starting a mission from a webhook comes in a later release."
          : "On a schedule or Run now it can also start a mission, which a webhook can’t."}
      </p>

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
              onChange={(e) => update((x) => withEngine(x, e.target.value, specialists ?? []))}
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
          <SpecialistField
            form={form}
            update={update}
            f={f}
            options={specialists}
            failed={specialistsFailed}
          />
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
                        disabled={!!form.specialistId}
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

      {form.actionKind === "send_message" && (
        <div className={styles.fields} data-testid="send-message-fields">
          <div className={styles.field}>
            <label className={styles.label} htmlFor={f("connector")}>
              Send with
            </label>
            <select
              id={f("connector")}
              className={styles.control}
              value={form.connector}
              onChange={(e) => update((x) => ({ ...x, connector: e.target.value, recipient: "" }))}
              data-testid="send-connector"
            >
              <option value="">{messengers == null ? "Loading…" : "Choose a messenger…"}</option>
              {(messengers ?? []).map((m) => (
                <option key={m.instance.id} value={m.instance.id} disabled={!m.instance.send}>
                  {m.label}
                  {m.instance.tested?.login ? ` · ${m.instance.tested.login}` : ""}
                  {m.instance.send ? "" : " — can’t send now: set it up"}
                </option>
              ))}
              {form.connector && !messenger && <option value={form.connector}>{form.connector}</option>}
            </select>
            {messengers != null && !messengers.some((m) => m.instance.send) && (
              <p className={styles.hint} data-testid="send-connector-none">
                No messenger can send yet.{" "}
                <Link className={styles.link} to={settingsPath("ai-hq", "hq-connectors")}>
                  Set one up in Settings → HQ
                </Link>
              </p>
            )}
          </div>
          <div className={styles.field}>
            <label className={styles.label} htmlFor={f("recipient")}>
              {messenger?.instance.sender !== undefined ? "To" : "Chat"}
            </label>
            <select
              id={f("recipient")}
              className={styles.control}
              value={form.recipient}
              disabled={!form.connector}
              onChange={(e) => set("recipient", e.target.value)}
              data-testid="send-recipient"
            >
              <option value="">Choose who receives it…</option>
              {(messenger?.instance.recipients ?? []).map((r) => (
                <option key={r.id} value={r.id}>
                  {r.label}
                  {r.label !== r.id ? ` · ${r.id}` : ""}
                </option>
              ))}
              {form.recipient && !messenger?.instance.recipients?.some((r) => r.id === form.recipient) && (
                <option value={form.recipient}>{form.recipient}</option>
              )}
            </select>
          </div>
          <p className={`${styles.hint} ${styles.full}`} data-testid="send-message-what">
            Only a recipient you allowed for this messenger. A delivery never changes the messenger
            or who receives it{messenger?.instance.sender ? `; mail goes from ${messenger.instance.sender}` : ""}.
            Before each send it checks that sending is still on and the recipient is still allowed.
          </p>
        </div>
      )}

      {form.actionKind === "write_memory" && (
        <div className={styles.fields} data-testid="write-memory-fields">
          <div className={styles.field}>
            <label className={styles.label} htmlFor={f("note-project")}>
              Project
            </label>
            <select
              id={f("note-project")}
              className={styles.control}
              value={form.projectId}
              onChange={(e) => set("projectId", e.target.value)}
              data-testid="note-project"
            >
              <option value="">Choose a project…</option>
              {projects.map((p) => (
                <option key={p.id} value={p.id}>
                  {p.name}
                </option>
              ))}
              {form.projectId && !projects.some((p) => p.id === form.projectId) && (
                <option value={form.projectId}>{form.projectId}</option>
              )}
            </select>
          </div>
          <div className={styles.field}>
            <label className={styles.label} htmlFor={f("note-title")}>
              Title
            </label>
            <input
              id={f("note-title")}
              className={styles.control}
              value={form.noteTitle}
              maxLength={120}
              onChange={(e) => set("noteTitle", e.target.value)}
              data-testid="note-title"
            />
          </div>
          <p className={`${styles.hint} ${styles.full}`} data-testid="write-memory-what">
            BattleLab reviews the note before other sessions see it: one that looks like a credential
            or holds a stored secret is held back for you. The project’s memory must be on. Other
            agents read notes as hints, never as instructions.
          </p>
        </div>
      )}

      {form.actionKind === "publish_artifact" && (
        <PublishArtifactFields
          form={form}
          update={update}
          f={f}
          listHook={listHook}
          shape={shape}
          projects={projects}
        />
      )}

      {(form.actionKind === "run_command" || form.actionKind === "run_script") && (
        <RunLocalFields
          form={form}
          update={update}
          f={f}
          listHook={listHook}
          shape={shape}
          projects={projects}
        />
      )}

      {form.actionKind === "call_webhook" && (
        <>
          <CallWebhookFields
            form={form}
            update={update}
            f={f}
            listHook={listHook}
            shape={shape}
            saved={savedDestination}
            onNewSecret={onNewSecret}
            newSecretBusy={newSecretBusy}
          />
          {secretPanel}
        </>
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

      {form.actionKind !== "call_webhook" &&
        form.actionKind !== "publish_artifact" &&
        form.actionKind !== "run_command" &&
        form.actionKind !== "run_script" && (
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
          {form.actionKind === "write_memory" ? (
            <p className={styles.hint}>
              A note never carries a secret: other sessions read the project’s memory, so a template
              with a secret is refused.
            </p>
          ) : form.actionKind === "send_message" ? (
            <p className={styles.hint}>
              A message never carries a secret: it leaves BattleLab in clear text, so a template
              with a secret is refused. Mail takes its first line as the subject.
            </p>
          ) : form.actionKind === "tell_hq" ? (
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

/** "Run as a specialist" (#2222): the session runs as one specialist, pinned to its current version
 *  (its skills, model and effort). Only specialists on the session's agent are offered; a stored pin
 *  to an older version says so, and choosing it again pins the current one. */
function SpecialistField({
  form,
  update,
  f,
  options,
  failed,
}: Pick<StepProps, "form" | "update" | "f"> & { options: ReviewerOption[] | null; failed: boolean }) {
  const choices = specialistChoices(options ?? [], form.engine);
  const current = choices.find((o) => o.id === form.specialistId) ?? null;
  const stale = !!form.specialistId && (!current || current.version !== form.specialistVersion);
  const noteId = f("specialist-note");
  return (
    <div className={styles.field}>
      <label className={styles.label} htmlFor={f("specialist")}>
        Run as a specialist (optional)
      </label>
      <select
        id={f("specialist")}
        className={styles.control}
        value={form.specialistId}
        disabled={options === null || (!choices.length && !form.specialistId)}
        onChange={(e) => {
          const o = choices.find((c) => c.id === e.target.value) ?? null;
          update((x) => ({
            ...x,
            specialistId: o ? o.id : "",
            specialistVersion: o ? o.version : null,
            // A specialist's session never skips permission prompts.
            bypass: o ? false : x.bypass,
          }));
        }}
        aria-describedby={noteId}
        data-testid="session-specialist"
      >
        <option value="">
          {options === null
            ? "Loading specialists…"
            : choices.length || form.specialistId
              ? "None, the agent’s own defaults"
              : "None available"}
        </option>
        {stale && (
          <option value={form.specialistId}>
            {form.specialistId} v{form.specialistVersion ?? "?"} (pinned)
          </option>
        )}
        {choices.map((o) => (
          <option key={o.id} value={o.id} disabled={!!o.blocked && o.id !== form.specialistId}>
            {o.label}
            {o.blocked ? ` — ${o.blocked}` : ""}
          </option>
        ))}
      </select>
      <p id={noteId} className={styles.hint} data-testid="specialist-note">
        {failed
          ? "Couldn’t read your specialists. Reload the page to try again; the session can still run without one."
          : stale && current
          ? `Pinned to version ${form.specialistVersion}; the specialist is now at version ${current.version}. Choose it again to pin that one — it asks for your approval again.`
          : stale
            ? "Pinned to a version that is no longer offered here (archived, or on another agent). Choose another, or none."
            : form.engine && options !== null && !choices.length
              ? "No specialist runs on this agent. Pick its agent, or make one in Library → Specialists."
              : "Its skills, model and effort, pinned to this version. Editing it later asks for your approval again."}
      </p>
    </div>
  );
}
