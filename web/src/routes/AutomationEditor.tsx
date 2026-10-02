/** Missions → Automations → New / Edit (#1201 board 2): a numbered form — name, trigger, action,
 *  limits — beside what the automation will do with nobody watching.
 *
 *  **The consent is the server's.** A save of an enabled automation that widens what it may do is a
 *  422 carrying the new scope's lines, what widened, and a digest; the consent dialog shows exactly
 *  that and resends with `consent: true` and THAT digest. Enabling asks the same question about the
 *  saved scope. The side panel shows the SAVED scope in the server's words; while the form has
 *  unsaved changes it also says, in the form's own words, what the draft would change — a preview,
 *  never what is consented to.
 *
 *  **Agents come from the roster, gated by the server.** The agent picker lists the engines
 *  `GET /api/automations` reports with the unattended-start check's verdict; one that may not start
 *  unattended is shown disabled with its reason. The model is fixed to the agent's default until
 *  model selection lands (#1189): the server refuses anything else.
 *
 *  Loop and webhook triggers are later phases of #1201 and are not offered here. */
import { useCallback, useEffect, useId, useMemo, useRef, useState } from "react";
import { Link, useBlocker, useLocation, useNavigate, useParams } from "react-router-dom";

import { HudFrame } from "../components/hud/HudFrame";
import { ConfirmDialog } from "../components/templates/ConfirmDialog";
import { FolderPickerModal } from "../components/FolderPickerModal";
import { ConsentDialog } from "../components/automations/ConsentDialog";
import {
  InFlightNote,
  KillSwitchNotice,
  StateWord,
} from "../components/automations/AutomationBits";
import { MissionRailOnly } from "../components/automations/MissionRailOnly";
import { useProjects } from "../components/automations/useAutomations";
import { useEnableFlow } from "../components/automations/useEnableFlow";
import { useConfig } from "../app/config";
import { engineName, useEngineRoster } from "../app/engineRoster";
import { ApiError, api } from "../lib/api";
import {
  AUTONOMY_WORDS,
  SCOPE_MOVED_NOTE,
  WEEKDAYS,
  actionLabel,
  blankForm,
  bodyFromForm,
  consentFromError,
  errorWords,
  formFromAutomation,
  formProblems,
  formTrigger,
  inFlightNote,
  reconsent,
  triggerWords,
  type EditorForm,
} from "../lib/automations";
import { AUTOMATIONS_PATH, automationEditPath, automationPath } from "../lib/routes";
import type { Session, Template, TemplateVariable } from "../types/api";
import type {
  Automation,
  AutomationList,
  Autonomy,
  ConsentRequired,
  Weekday,
} from "../types/automations";
import styles from "../components/automations/automations.module.css";

const DAY_LABEL: Record<Weekday, string> = {
  mon: "Mon",
  tue: "Tue",
  wed: "Wed",
  thu: "Thu",
  fri: "Fri",
  sat: "Sat",
  sun: "Sun",
};

/** Every IANA zone the browser knows, when it can say; otherwise the field is free text. */
function timeZones(): string[] | null {
  const intl = Intl as unknown as { supportedValuesOf?: (k: string) => string[] };
  try {
    return intl.supportedValuesOf ? intl.supportedValuesOf("timeZone") : null;
  } catch {
    return null;
  }
}

/** A unix time as a `datetime-local` value in the viewer's zone, and back. */
function toLocalInput(ts: number | null): string {
  if (ts == null) return "";
  const d = new Date(ts * 1000);
  const p = (n: number) => String(n).padStart(2, "0");
  return `${d.getFullYear()}-${p(d.getMonth() + 1)}-${p(d.getDate())}T${p(d.getHours())}:${p(d.getMinutes())}`;
}
function fromLocalInput(v: string): number | null {
  if (!v) return null;
  const t = new Date(v).getTime();
  return Number.isFinite(t) ? t / 1000 : null;
}

function Seg<T extends string>({
  label,
  value,
  options,
  onChange,
  testId,
}: {
  label: string;
  value: T;
  options: { value: T; label: string }[];
  onChange: (v: T) => void;
  testId?: string;
}) {
  return (
    <div className={styles.segs} role="group" aria-label={label} data-testid={testId}>
      {options.map((o) => (
        <button
          key={o.value}
          type="button"
          className={styles.seg}
          aria-pressed={value === o.value}
          onClick={() => onChange(o.value)}
          data-value={o.value}
        >
          {o.label}
        </button>
      ))}
    </div>
  );
}

/** KEYED BY THE ROUTE'S ID (#1252 review): moving from one automation's editor to another's is a
 *  fresh mount with empty state, so nothing loaded — or saved — for A can land on B's page. */
export default function AutomationEditor() {
  const { id } = useParams<{ id?: string }>();
  return <Editor key={id ?? "new"} routeId={id} />;
}

function Editor({ routeId }: { routeId?: string }) {
  const id = routeId;
  /** Mounted: a create that finishes after the operator left must not pull them back (#1252). */
  const mounted = useRef(false);
  useEffect(() => {
    mounted.current = true;
    return () => {
      mounted.current = false;
    };
  }, []);
  const navigate = useNavigate();
  const location = useLocation();
  const config = useConfig();
  useEngineRoster();
  const projects = useProjects();
  const uid = useId();
  const zones = useMemo(() => timeZones(), []);

  const [list, setList] = useState<AutomationList | null>(null);
  const [saved, setSaved] = useState<Automation | null>(null);
  const [form, setForm] = useState<EditorForm | null>(null);
  const [pristine, setPristine] = useState<string>("");
  const [loadError, setLoadError] = useState<string | null>(null);
  const [missing, setMissing] = useState(false);
  const [templates, setTemplates] = useState<Template[] | null>(null);
  const [variables, setVariables] = useState<TemplateVariable[]>([]);
  const [sessions, setSessions] = useState<Session[] | null>(null);
  const [saving, setSaving] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [stale, setStale] = useState(false);
  // A create lands on the new automation's edit route (a remount), carrying its notice with it.
  const [inFlight, setInFlight] = useState("");
  const [notice, setNotice] = useState<string | null>(
    () => (location.state as { notice?: string } | null)?.notice ?? null,
  );
  const [consent, setConsent] = useState<ConsentRequired | null>(null);
  const [consentError, setConsentError] = useState<string | null>(null);
  const [picking, setPicking] = useState(false);
  /** Where focus returns when a dialog closes: the control that opened it, captured on open. */
  const [returnTo, setReturnTo] = useState<HTMLElement | null>(null);

  const load = useCallback(async () => {
    try {
      const l = await api.automations();
      setLoadError(null);
      setList(l);
      const tz = l.limits.timezone || "UTC";
      if (id) {
        const a = await api.automation(id);
        // A response for another automation is never this page's (belt and braces with the key).
        if (a.id !== id) return;
        const f = formFromAutomation(a, tz);
        setSaved(a);
        setForm(f);
        setPristine(JSON.stringify(f));
      } else {
        const f = blankForm(tz);
        setSaved(null);
        setForm(f);
        setPristine(JSON.stringify(f));
      }
      setStale(false);
    } catch (e) {
      setLoadError(errorWords(e, "Couldn’t load it."));
      if (e instanceof ApiError && e.status === 404) setMissing(true);
    }
  }, [id]);

  useEffect(() => {
    // The fetch's state lands after its await, never synchronously in this body.
    // eslint-disable-next-line react-hooks/set-state-in-effect
    void load();
  }, [load]);
  useEffect(() => {
    let live = true;
    api
      .templates()
      .then((r) => live && setTemplates(r.templates))
      .catch(() => live && setTemplates([]));
    api
      .templateVariables()
      .then((r) => live && setVariables(r.variables))
      .catch(() => undefined);
    api
      .sessions({ limit: 100 })
      .then((r) => live && setSessions(r.sessions))
      .catch(() => live && setSessions([]));
    return () => {
      live = false;
    };
  }, []);

  const dirty = form != null && JSON.stringify(form) !== pristine;
  const blocker = useBlocker(
    ({ currentLocation, nextLocation }) =>
      dirty && !saving && currentLocation.pathname !== nextLocation.pathname,
  );

  const enable = useEnableFlow(
    useCallback(
      (a: Automation | null) => {
        if (a) {
          setSaved(a);
          setNotice(`“${a.name}” is enabled.`);
          setInFlight(inFlightNote(a));
        } else void load();
      },
      [load],
    ),
  );

  const set = useCallback(<K extends keyof EditorForm>(k: K, v: EditorForm[K]) => {
    setForm((f) => (f ? { ...f, [k]: v } : f));
  }, []);

  const template = useMemo(
    () => (form && templates ? templates.find((t) => t.id === form.templateId) ?? null : null),
    [form, templates],
  );

  async function save(consentDigest?: string) {
    if (!form) return;
    setSaving(true);
    setError(null);
    setNotice(null);
    const body = bodyFromForm(form);
    try {
      let a: Automation;
      if (!saved) {
        if (id) {
          // An edit route whose automation has not loaded never becomes a create.
          setError("It is still loading — try again in a moment.");
          return;
        }
        a = await api.createAutomation(body);
      } else {
        // Save acts on the ROUTE's automation, and only if that is the one on screen.
        if (!id || saved.id !== id) {
          setError("This page is showing a different automation than its address — reload it.");
          return;
        }
        a = await api.patchAutomation(id, {
          ...body,
          revision: saved.revision,
          ...(consentDigest ? { consent: true, scope_digest: consentDigest } : {}),
        });
      }
      const f = formFromAutomation(a, list?.limits.timezone || "UTC");
      setSaved(a);
      setForm(f);
      setPristine(JSON.stringify(f));
      setConsent(null);
      setConsentError(null);
      const done = a.enabled
        ? `Saved. “${a.name}” keeps running with this scope.`
        : `Saved. “${a.name}” is off until you enable it.`;
      setNotice(done);
      setInFlight(inFlightNote(a));
      // Only while this editor is still the page: a create that lands after the operator moved on
      // is saved (and listed) but never navigates them back.
      if (!id && mounted.current)
        navigate(automationEditPath(a.id), { replace: true, state: { notice: done } });
    } catch (e) {
      const need = consentFromError(e);
      if (need && saved) {
        // The server's 422 (a widening) or 409 (the scope moved under an open dialog): ask again,
        // on the server's lines, and resend with the server's digest.
        if (!consentDigest) setReturnTo(document.activeElement as HTMLElement | null);
        // The server's `widened` wins; when a 409 carries none, keep naming what widened.
        setConsent((prev) => (consentDigest ? reconsent(need, prev) : need));
        setConsentError(consentDigest ? SCOPE_MOVED_NOTE : null);
      } else if (e instanceof ApiError && e.status === 409) {
        setConsent(null);
        setStale(true);
        setError(`${e.message}. It changed elsewhere since you opened it.`);
      } else {
        setError(errorWords(e, "Couldn’t save it."));
      }
    } finally {
      setSaving(false);
    }
  }

  async function verb(v: "pause" | "resume") {
    if (!saved) return;
    try {
      const a = await api.automationVerb(saved.id, v);
      setSaved(a);
      setNotice(v === "pause" ? "Paused." : "Resumed.");
      setInFlight(inFlightNote(a));
    } catch (e) {
      setError(errorWords(e, "That didn’t work."));
    }
  }

  if (loadError) {
    return (
      <div className={styles.page}>
        <MissionRailOnly />
        <div className={`${styles.notice} ${styles.noticeBad}`} role="alert">
          <strong data-testid="editor-load-error">
            {missing ? "This automation doesn’t exist" : "Couldn’t load this automation"}
          </strong>
          <span>{loadError}</span>
          <div className={styles.noticeRow}>
            <button type="button" className={styles.ghost} onClick={() => void load()}>
              Retry
            </button>
            <Link className={styles.ghost} to={AUTOMATIONS_PATH}>
              All automations
            </Link>
          </div>
        </div>
      </div>
    );
  }
  if (!form || !list) {
    return (
      <div className={styles.page}>
        <MissionRailOnly />
        <p className={styles.lede}>Loading…</p>
      </div>
    );
  }

  const playbooks = config?.mission_playbooks?.playbooks ?? [];
  // A saved checklist that no longer exists is shown AS missing, never as the default it is not
  // (#1252 review), and Save waits for a live choice. Only once the checklists have been read.
  const checklistMissing =
    form.actionKind === "start_mission" &&
    !!config?.mission_playbooks &&
    form.checklistId != null &&
    form.checklistId !== ":none" &&
    !playbooks.some((p) => p.id === form.checklistId);
  const problems = [
    ...formProblems(form),
    ...(checklistMissing
      ? ["The checklist it used was deleted — choose another checklist, the default, or none"]
      : []),
  ];
  const engines = list.engines ?? [];
  const liveProjects = (projects ?? []).filter((p) => !p.archived || p.id === form.projectId);
  const msgWord =
    form.actionKind === "start_mission"
      ? "Instruction"
      : form.actionKind === "start_session"
        ? "First message"
        : "Message";
  const sessionKnown = sessions?.some((s) => s.id === form.sessionKey);
  const draftTrigger = triggerWords(formTrigger(form));
  const f = (name: string) => `${uid}-${name}`;

  return (
    <div className={styles.page} data-testid="automation-editor">
      <MissionRailOnly />
      <div className={`${styles.stack} ${styles.editorGrid}`}>
        <section className={styles.panel} aria-labelledby={f("title")}>
          <HudFrame />
          <div className={styles.headText}>
            <span className={styles.kicker}>
              <Link className={styles.link} to={AUTOMATIONS_PATH}>
                Automations
              </Link>
              {saved ? " // edit" : " // new"}
            </span>
            <h1 id={f("title")} className={styles.title}>
              {saved ? saved.name : "New automation"}
            </h1>
            {saved && (
              <p className={styles.lede}>
                <StateWord state={saved.state} /> · revision {saved.revision}
                {saved.enabled
                  ? ". A change that widens what it may do on its own asks for your consent again; until then it keeps the old scope."
                  : ". It is off: nothing runs until you enable it."}
              </p>
            )}
          </div>
          {!list.loop.enabled && <KillSwitchNotice />}

          <form
            className={styles.form}
            onSubmit={(e) => {
              e.preventDefault();
              if (!problems.length && !saving) void save();
            }}
            aria-busy={saving || undefined}
          >
            {/* LOCKED WHILE A SAVE IS IN FLIGHT (#1252 review): an edit typed now would be replaced by
                the server's answer, so the form cannot be edited until it arrives. */}
            <fieldset className={styles.lock} disabled={saving} data-testid="editor-fields">
            {/* 01 · name ------------------------------------------------------------------------ */}
            <fieldset className={styles.section}>
              <legend className={styles.sectionTitle}>
                <span className={styles.sectionNum}>01</span>Name
              </legend>
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
            </fieldset>

            {/* 02 · trigger --------------------------------------------------------------------- */}
            <fieldset className={styles.section}>
              <legend className={styles.sectionTitle}>
                <span className={styles.sectionNum}>02</span>Trigger
              </legend>
              <Seg
                label="Trigger"
                value={form.triggerKind}
                onChange={(v) => set("triggerKind", v)}
                testId="trigger-kind"
                options={[
                  { value: "once", label: "Once" },
                  { value: "schedule", label: "Schedule" },
                  { value: "manual", label: "Run now only" },
                ]}
              />
              {form.triggerKind === "schedule" && (
                <div className={styles.fields3}>
                  <div className={styles.field}>
                    <label className={styles.label} htmlFor={f("repeats")}>
                      Repeats
                    </label>
                    <select
                      id={f("repeats")}
                      className={styles.control}
                      value={form.cadenceKind === "interval" ? `interval-${form.unit}` : form.cadenceKind}
                      onChange={(e) => {
                        const v = e.target.value;
                        if (v.startsWith("interval-")) {
                          setForm((x) =>
                            x
                              ? {
                                  ...x,
                                  cadenceKind: "interval",
                                  unit: v === "interval-hours" ? "hours" : "minutes",
                                  every: v === "interval-hours" ? Math.min(Math.max(1, x.every), 168) : Math.max(list.limits.interval_min_minutes, x.every),
                                }
                              : x,
                          );
                        } else set("cadenceKind", v as EditorForm["cadenceKind"]);
                      }}
                      data-testid="cadence-kind"
                    >
                      <option value="interval-minutes">Every N minutes</option>
                      <option value="interval-hours">Every N hours</option>
                      <option value="daily">Daily</option>
                      <option value="weekly">Weekly</option>
                      <option value="monthly">Monthly</option>
                    </select>
                  </div>
                  {form.cadenceKind === "interval" ? (
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
                  {form.cadenceKind === "weekly" && (
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
                                set(
                                  "days",
                                  e.target.checked ? [...form.days, d] : form.days.filter((x) => x !== d),
                                )
                              }
                            />
                            {DAY_LABEL[d]}
                          </label>
                        ))}
                      </div>
                    </div>
                  )}
                  {form.cadenceKind === "monthly" && (
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
              {form.triggerKind === "once" && (
                <div className={styles.fields}>
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
                  <TzField id={f("tz")} zones={zones} value={form.tz} onChange={(v) => set("tz", v)} />
                </div>
              )}
              <p className={styles.callout}>
                {form.triggerKind === "manual"
                  ? "It runs only when you press Run now."
                  : `${draftTrigger.detail}. Every ${list.limits.interval_min_minutes} minutes is the shortest schedule. A time skipped by a clock change runs once, at the next valid minute. Runs missed while BattleLab was down collapse into one catch-up run.`}{" "}
                Loop and webhook triggers come in a later release.
              </p>
            </fieldset>

            {/* 03 · action ---------------------------------------------------------------------- */}
            <fieldset className={styles.section}>
              <legend className={styles.sectionTitle}>
                <span className={styles.sectionNum}>03</span>Action
              </legend>
              <Seg
                label="Action"
                value={form.actionKind}
                onChange={(v) => set("actionKind", v)}
                testId="action-kind"
                options={[
                  { value: "start_mission", label: "Start a mission" },
                  { value: "start_session", label: "Start a session" },
                  { value: "send_to_session", label: "Send to a session" },
                ]}
              />

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
                      {liveProjects.map((p) => (
                        <option key={p.id} value={p.id}>
                          {p.name}
                          {p.default_folder ? ` · ${p.default_folder}` : ""}
                        </option>
                      ))}
                      {form.projectId && !liveProjects.some((p) => p.id === form.projectId) && (
                        <option value={form.projectId}>{form.projectId}</option>
                      )}
                    </select>
                  </div>
                  <div className={styles.field}>
                    <label className={styles.label} htmlFor={f("checklist")}>
                      Checklist
                    </label>
                    <select
                      id={f("checklist")}
                      className={styles.control}
                      value={form.checklistId ?? ""}
                      onChange={(e) => set("checklistId", e.target.value === "" ? null : e.target.value)}
                      aria-invalid={checklistMissing || undefined}
                      data-testid="mission-checklist"
                    >
                      {checklistMissing && (
                        <option value={form.checklistId ?? ""}>Missing checklist (deleted)</option>
                      )}
                      <option value="">The default checklist</option>
                      <option value=":none">No checklist</option>
                      {playbooks.map((p) => (
                        <option key={p.id} value={p.id}>
                          {p.label} · {p.objectives.length} {p.objectives.length === 1 ? "check" : "checks"}
                        </option>
                      ))}
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
                      An automated mission never runs with permission bypass, and it can never
                      raise your mission-control tier.
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
                      aria-describedby={f("engine-why")}
                      data-testid="session-engine"
                    >
                      <option value="">Choose an agent…</option>
                      {engines.map((e) => (
                        <option key={e.id} value={e.id} disabled={!e.ok}>
                          {engineName(e.id)}
                          {e.ok ? "" : " — can’t start unattended"}
                        </option>
                      ))}
                    </select>
                    <div id={f("engine-why")} className={styles.hint} data-testid="engine-reasons">
                      {engines.filter((e) => !e.ok).map((e) => (
                        <p key={e.id} className={styles.hint}>
                          {engineName(e.id)}: {e.reason}
                        </p>
                      ))}
                      {engines.length === 0 && "No agent can be started unattended on this server."}
                    </div>
                  </div>
                  <div className={styles.field}>
                    <label className={styles.label} htmlFor={f("model")}>
                      Model
                    </label>
                    <select id={f("model")} className={styles.control} disabled value="default" aria-describedby={f("model-hint")}>
                      <option value="default">The agent’s default</option>
                    </select>
                    <p id={f("model-hint")} className={styles.hint}>
                      Choosing a model per session comes in a later release; until then it runs the
                      agent’s default.
                    </p>
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
                      <button
                        type="button"
                        className={styles.ghost}
                        onClick={(e) => {
                          setReturnTo(e.currentTarget);
                          setPicking(true);
                        }}
                      >
                        Choose…
                      </button>
                    </div>
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
                          Sessions this starts will act without asking you, while you are away.
                          Turning it on asks for your consent again.
                        </span>
                      </p>
                    )}
                  </div>
                </div>
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
                    {(sessions ?? []).map((s) => (
                      <option key={s.id} value={s.id}>
                        {s.title || "(untitled)"} · {engineName(s.engine)}
                        {s.running ? " · running" : ""}
                      </option>
                    ))}
                    {form.sessionKey && !sessionKnown && (
                      <option value={form.sessionKey}>{form.sessionKey}</option>
                    )}
                  </select>
                  {form.sessionKey && (
                    <p className={`${styles.hint} ${styles.mono}`} data-testid="send-session-key">
                      {form.sessionKey}
                    </p>
                  )}
                  <p className={styles.hint}>
                    It types into that session as it is — with whatever permissions it already runs
                    with. If the session isn’t running then, the run is refused and recorded; nothing
                    is started in its place.
                  </p>
                </div>
              )}

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
                      onChange={(e) => setForm((x) => (x ? { ...x, templateId: e.target.value, values: {} } : x))}
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
                  </div>
                )}
              </div>
              {form.messageMode === "template" && template && (
                <>
                  <p className={styles.hint}>
                    The template’s revision is pinned. If it is edited later, this automation pauses
                    until you approve the new version.
                  </p>
                  <div className={styles.fields}>
                    {template.fields.map((fld) => {
                      const lib = variables.find((v) => v.name === fld.name);
                      return (
                        <div key={fld.name} className={styles.field}>
                          <label className={styles.label} htmlFor={f(`v-${fld.name}`)}>
                            {fld.label || fld.name}
                          </label>
                          <span className={styles.fieldSource}>
                            {fld.source === "library"
                              ? "from your variable library"
                              : fld.kind === "secret"
                                ? "a typed secret — an automation can’t keep one"
                                : "fixed value"}
                          </span>
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
                          ) : (
                            <input
                              id={f(`v-${fld.name}`)}
                              className={styles.control}
                              value={form.values[fld.name] ?? ""}
                              placeholder={fld.default || undefined}
                              disabled={fld.kind === "secret"}
                              onChange={(e) => set("values", { ...form.values, [fld.name]: e.target.value })}
                            />
                          )}
                        </div>
                      );
                    })}
                  </div>
                </>
              )}
              {form.actionKind !== "send_to_session" && (
                <p className={styles.hint}>
                  {form.actionKind === "start_mission" ? "Mission briefs" : "A new session’s first message"} never
                  carry secrets: a template with a secret can only be sent to a running session.
                </p>
              )}
            </fieldset>

            {/* 04 · limits ---------------------------------------------------------------------- */}
            <fieldset className={styles.section}>
              <legend className={styles.sectionTitle}>
                <span className={styles.sectionNum}>04</span>Limits
              </legend>
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
                      setForm((x) =>
                        x
                          ? v === "skip"
                            ? { ...x, concurrency: "skip" }
                            : { ...x, concurrency: "allow", maxConcurrent: Number(v.split("-")[1]) }
                          : x,
                      );
                    }}
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
                  />
                </div>
              </div>
            </fieldset>

            {problems.length > 0 && (
              <ul className={styles.problems} data-testid="editor-problems">
                {problems.map((p) => (
                  <li key={p}>{p}</li>
                ))}
              </ul>
            )}
            {error && (
              <div className={styles.error} role="alert" data-testid="editor-error">
                {error}{" "}
                {stale && (
                  <button type="button" className={styles.ghost} onClick={() => void load()}>
                    Reload (discards your changes)
                  </button>
                )}
              </div>
            )}
            {notice && (
              <div className={`${styles.notice} ${styles.noticeOk}`} role="status" data-testid="editor-notice">
                {notice}
                {inFlight && <InFlightNote text={inFlight} />}
              </div>
            )}
            <div className={styles.formActions}>
              <Link
                className={styles.ghost}
                to={saved ? automationPath(saved.id) : AUTOMATIONS_PATH}
                aria-disabled={saving || undefined}
                tabIndex={saving ? -1 : undefined}
                onClick={(e) => saving && e.preventDefault()}
              >
                {dirty ? "Cancel" : "Back"}
              </Link>
              {saved?.enabled && !saved.needs_reapproval && (
                <button
                  type="button"
                  className={styles.ghost}
                  onClick={() => void verb(saved.paused ? "resume" : "pause")}
                  data-testid="editor-pause"
                >
                  {saved.paused ? "Resume" : "Pause"}
                </button>
              )}
              {saved && (!saved.enabled || saved.needs_reapproval) && (
                <button
                  type="button"
                  className={styles.ghost}
                  disabled={dirty}
                  title={dirty ? "Save your changes first" : undefined}
                  onClick={() => enable.open(saved)}
                  data-testid="editor-enable"
                >
                  {saved.needs_reapproval ? "Review and approve…" : "Enable…"}
                </button>
              )}
              <button
                type="submit"
                className={styles.primary}
                disabled={saving || problems.length > 0 || (!!saved && !dirty)}
                data-testid="editor-save"
              >
                {saving ? "Saving…" : saved?.enabled ? "Save…" : "Save"}
              </button>
            </div>
            </fieldset>
            {saving && (
              <p className={styles.hint} role="status" data-testid="editor-saving">
                Saving — the form is locked until the server answers.
              </p>
            )}
          </form>
        </section>

        <aside className={styles.aside}>
          <section className={styles.panel} aria-labelledby={f("scope")} data-testid="editor-scope">
            <HudFrame />
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
                Save it to see its full scope in the server’s words. Nothing runs until you enable
                it, and enabling shows this scope again for your consent.
              </p>
            )}
            {saved && dirty && (
              <div className={`${styles.notice} ${styles.noticeWarn} ${styles.more}`} data-testid="editor-draft">
                <strong>Unsaved changes</strong>
                <span>
                  Above is what is saved. Your draft: {actionLabel(form.actionKind).toLowerCase()},{" "}
                  {draftTrigger.label.toLowerCase()} ({draftTrigger.detail})
                  {form.actionKind === "start_session" && form.bypass ? ", with permission bypass" : ""}.
                  {saved.enabled ? " If saving widens what it may do, you are asked to consent first." : ""}
                </span>
              </div>
            )}
            <p className={`${styles.hint} ${styles.more}`}>
              Anything that widens it — a new target, bypass on, more autonomy, a different schedule,
              a higher cap, new content — asks for your consent again.
            </p>
          </section>
        </aside>
      </div>

      {consent && saved && (
        <ConsentDialog
          name={form.name || saved.name}
          mode="save"
          consent={consent}
          busy={saving}
          error={consentError}
          onCancel={() => {
            setConsent(null);
            setConsentError(null);
          }}
          onConfirm={(digest) => void save(digest)}
          returnFocusTo={returnTo}
        />
      )}
      {enable.dialog}
      {picking && (
        <FolderPickerModal
          initialPath={form.folder || undefined}
          title="Where the session runs"
          onPick={(p) => {
            set("folder", p);
            setPicking(false);
          }}
          onCancel={() => setPicking(false)}
          returnFocusTo={returnTo}
        />
      )}
      {blocker.state === "blocked" && (
        <ConfirmDialog
          tag="Unsaved changes"
          title={form.name || "Automation"}
          cancelLabel="Keep editing"
          confirmLabel="Discard and leave"
          danger
          onCancel={() => blocker.reset()}
          onConfirm={() => blocker.proceed()}
        >
          <p>Your changes to this automation are not saved.</p>
        </ConfirmDialog>
      )}
    </div>
  );
}

function TzField({
  id,
  zones,
  value,
  onChange,
}: {
  id: string;
  zones: string[] | null;
  value: string;
  onChange: (v: string) => void;
}) {
  return (
    <div className={styles.field}>
      <label className={styles.label} htmlFor={id}>
        Time zone
      </label>
      {zones ? (
        <select id={id} className={styles.control} value={value} onChange={(e) => onChange(e.target.value)}>
          {!zones.includes(value) && <option value={value}>{value}</option>}
          {zones.map((z) => (
            <option key={z} value={z}>
              {z}
            </option>
          ))}
        </select>
      ) : (
        <input id={id} className={styles.control} value={value} onChange={(e) => onChange(e.target.value)} />
      )}
    </div>
  );
}
