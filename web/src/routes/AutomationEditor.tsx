/** Library → Automations → New / Edit: THE automation wizard (#2221 P2b), the only way to create or
 *  edit one. Five steps, each one phone screen, in the app's one WizardShell (#1187):
 *
 *  1. **When** — a schedule (or once), Run now, or a webhook picked from Library → Webhooks.
 *  2. **Which events** — presets recognised from the webhook's last delivery, or (Advanced) the
 *     operator's own conditions on its fields. Skipped for a schedule or Run now, which have none.
 *  3. **What** — start a session, message a session or a review loop (a schedule may also start a
 *     mission). A template's fields take a fixed value or, from a webhook, a typed value from the
 *     delivery.
 *  4. **Limits** — the policy in one line, its fields folded behind it.
 *  5. **Review & enable** — the name, the server's consent lines, Save (always OFF for a new one)
 *     and the enable flow.
 *
 *  **The consent is the server's.** A save of an enabled automation that widens what it may do is a
 *  422 carrying the new scope's lines, what widened, and a digest; the consent dialog shows exactly
 *  that and resends with `consent: true` and THAT digest. Enabling asks the same question about the
 *  saved scope. Step 5 shows the SAVED scope in the server's words; while the form has unsaved
 *  changes it also says, in the form's own words, what the draft would change — a preview, never
 *  what is consented to.
 *
 *  **Agents come from the roster, gated by the server — API first (#2244).** The agent picker
 *  offers the API agents `GET /api/automations` admits for unattended work; a folded line names each
 *  installed API agent that is held, in plain words. A terminal agent (or session) is shown only
 *  when the saved automation already targets it, stamped, with "Switch to API client".
 *
 *  **"+ New webhook…"** opens the create flow with this draft in router state (the form only, never
 *  a secret) and comes back with the new webhook picked. A #2202 review loop saved with its own
 *  webhook (`scheme`) keeps it — saved back as it was, with its own Connect Forgejo panel — and is
 *  never converted here. */
import { useCallback, useEffect, useId, useMemo, useRef, useState } from "react";
import { Link, useLocation, useNavigate, useParams } from "react-router-dom";

import { FolderPickerModal } from "../components/FolderPickerModal";
import { ConsentDialog } from "../components/automations/ConsentDialog";
import { useNowS, useProjects } from "../components/automations/useAutomations";
import { useEnableFlow } from "../components/automations/useEnableFlow";
import { useSwitchFlow } from "../components/automations/useSwitchFlow";
import { StepEvents } from "../components/automations/wizard/StepEvents";
import { StepLimits } from "../components/automations/wizard/StepLimits";
import { StepReview } from "../components/automations/wizard/StepReview";
import { StepWhat } from "../components/automations/wizard/StepWhat";
import { useActions } from "../components/actions/useActions";
import { DestinationSecretOnce } from "../components/actions/CallWebhookFields";
import { galleryProblem } from "../lib/actions";
import { StepWhen } from "../components/automations/wizard/StepWhen";
import { timeZones } from "../components/automations/wizard/time";
import wiz from "../components/automations/wizard/wizard.module.css";
import { useWebhookDetail, useWebhooks } from "../components/webhooks/useWebhooks";
import { WizardShell } from "../components/wizard/WizardShell";
import { engineLabel, useEngineRoster } from "../app/engineRoster";
import { ApiError, api } from "../lib/api";
import {
  SCOPE_MOVED_NOTE,
  WIZARD_STEPS,
  apiSwitchFor,
  blankForm,
  bodyFromForm,
  consentFromError,
  deliveryChecks,
  errorWords,
  eventsSkipped,
  formFromAutomation,
  inFlightNote,
  messengers,
  reconsent,
  reviewerOptions,
  stepProblems,
  type EditorForm,
  type WizardStepId,
} from "../lib/automations";
import { AUTOMATIONS_PATH, WEBHOOK_NEW_PATH, automationEditPath, automationPath } from "../lib/routes";
import type { Session, Template, TemplateVariable } from "../types/api";
import type { Specialist } from "../types/specialists";
import type { ConnectorCatalog } from "../lib/connectors";
import type { Automation, AutomationList, ConsentRequired } from "../types/automations";
import type { NewWebhookHandoff } from "./WebhookNew";
import styles from "../components/automations/automations.module.css";

const HEADINGS: Record<WizardStepId, string> = {
  when: "When does it run?",
  events: "Which events start it?",
  what: "What does it do?",
  limits: "Limits",
  review: "Review and enable",
};

const REVIEW = WIZARD_STEPS.findIndex((s) => s.id === "review");

/** KEYED BY THE ROUTE'S ID (#1252 review): moving from one automation's editor to another's is a
 *  fresh mount with empty state, so nothing loaded — or saved — for A can land on B's page. */
export default function AutomationEditor() {
  const { id } = useParams<{ id?: string }>();
  return <Editor key={id ?? "new"} routeId={id} />;
}

/** "Call a webhook" (#2248 P2): a create that made a signing secret moves to the new automation's
 *  edit route, which mounts a NEW editor. The secret crosses that one hop in memory only, here,
 *  taken (and cleared) by the editor for that automation as it mounts — never router state, never
 *  storage. A reload or any other page never sees it. */
let createdSecret: { id: string; secret: string } | null = null;

function takeCreatedSecret(id: string | undefined): string | null {
  // Read here (a state initializer may run twice); cleared by the editor's first effect.
  return createdSecret && id && createdSecret.id === id ? createdSecret.secret : null;
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
  /** The specialist pool, for a review loop's reviewer. `null` until read; `[]` if it can't be. */
  const [specialists, setSpecialists] = useState<Specialist[] | null>(null);
  // The messengers a "Send a message" action may use (#2248 P2), from Settings → HQ connectors.
  const [connectors, setConnectors] = useState<ConnectorCatalog | null>(null);
  // "Call a webhook" (#2248 P2): the signing secret from the ONE answer that made it. Component
  // state only: never stored, never put in router state, gone on Done or when the page goes.
  const [newSecret, setNewSecret] = useState<string | null>(() => takeCreatedSecret(routeId));
  useEffect(() => {
    createdSecret = null;
  }, []);
  const [secretBusy, setSecretBusy] = useState(false);
  const nowS = useNowS();
  const [saving, setSaving] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [stale, setStale] = useState(false);
  // A create lands on the new automation's edit route (a remount), carrying its notice — and the
  // step it was saved from — with it.
  const [inFlight, setInFlight] = useState("");
  const [notice, setNotice] = useState<string | null>(
    () => (location.state as { notice?: string } | null)?.notice ?? null,
  );
  const [step, setStep] = useState<number>(() =>
    (location.state as { step?: string } | null)?.step === "review" ? REVIEW : 0,
  );
  const [consent, setConsent] = useState<ConsentRequired | null>(null);
  const [consentError, setConsentError] = useState<string | null>(null);
  const [picking, setPicking] = useState(false);
  const webhooks = useWebhooks();
  /** The action gallery the What step picks from (#2248 P1). Owned HERE, not by the step, so its
   *  readiness holds Next, the rail past What and the save (Hermes on PR #2266). */
  const actions = useActions();
  /** A draft handed back by the New webhook page (#2221), adopted once on the first load. */
  const handedBack = useRef(
    (location.state as { webhookPick?: { id: string; draft: EditorForm } } | null)?.webhookPick ?? null,
  );
  /** Set while handing the draft to the New webhook page: that leave is not a discard. The
   *  navigation runs after the render that drops the leave guard. */
  const [handoff, setHandoff] = useState<NewWebhookHandoff | null>(null);
  /** Where focus returns when a dialog closes: the control that opened it, captured on open. */
  const [returnTo, setReturnTo] = useState<HTMLElement | null>(null);

  /** The draft the New webhook page handed back, over the loaded form: the stored automation stays
   *  what is saved (so it reads as unsaved changes), and a legacy webhook is never converted. */
  const adoptHandedBack = useCallback((f: EditorForm): EditorForm => {
    const back = handedBack.current;
    handedBack.current = null;
    if (!back || typeof back.draft !== "object" || back.draft == null) return f;
    const next: EditorForm = { ...blankForm(f.tz), ...f, ...back.draft, legacyHook: f.legacyHook };
    if (back.id) next.webhookId = back.id;
    return next;
  }, []);
  useEffect(() => {
    // Adopted from the ref; drop it from history so a reload does not adopt it again.
    if ((location.state as { webhookPick?: unknown } | null)?.webhookPick)
      navigate(location.pathname, { replace: true, state: null });
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, []);
  useEffect(() => {
    if (handoff) navigate(WEBHOOK_NEW_PATH, { state: { webhookHandoff: handoff } });
  }, [handoff, navigate]);

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
        setForm(adoptHandedBack(f));
        setPristine(JSON.stringify(f));
      } else {
        const f = blankForm(tz);
        setSaved(null);
        setForm(adoptHandedBack(f));
        setPristine(JSON.stringify(f));
      }
      setStale(false);
    } catch (e) {
      setLoadError(errorWords(e, "Couldn’t load it."));
      if (e instanceof ApiError && e.status === 404) setMissing(true);
    }
  }, [id, adoptHandedBack]);

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
    Promise.resolve()
      .then(() => api.connectors())
      .then((r) => live && setConnectors(r))
      .catch(() => live && setConnectors({ catalog: [], instances: [], disclosure: "" }));
    Promise.resolve()
      .then(() => api.specialists())
      .then((r) => live && setSpecialists(Array.isArray(r?.specialists) ? r.specialists : []))
      .catch(() => live && setSpecialists([]));
    return () => {
      live = false;
    };
  }, []);

  // The picked webhook's last delivery: its shape and the presets recognised from it (step 2, and
  // step 3's fields from the delivery). Only a webhook from the list has one.
  const hookId = form && form.triggerKind === "webhook" && !form.legacyHook ? form.webhookId : "";
  const hook = useWebhookDetail(hookId);

  const dirty = form != null && JSON.stringify(form) !== pristine;

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

  // "Switch to API client" (#1728 Phase 2): the SAVED automation switches, through the same
  // validated save, so a switch never carries an unsaved draft along with it.
  const switchFlow = useSwitchFlow(
    useCallback(
      (a: Automation) => {
        const f = formFromAutomation(a, list?.limits.timezone || "UTC");
        setSaved(a);
        setForm(f);
        setPristine(JSON.stringify(f));
        const now = a.action?.kind === "start_session" ? engineLabel(a.action.engine) : "its API client";
        setNotice(
          a.enabled
            ? `Saved. “${a.name}” now runs on ${now}, with the scope you approved.`
            : `Saved. “${a.name}” now runs on ${now}. It is off until you enable it.`,
        );
        setInFlight(inFlightNote(a));
      },
      [list],
    ),
  );

  const set = useCallback(<K extends keyof EditorForm>(k: K, v: EditorForm[K]) => {
    setForm((f) => (f ? { ...f, [k]: v } : f));
  }, []);
  const update = useCallback((fn: (f: EditorForm) => EditorForm) => {
    setForm((f) => (f ? fn(f) : f));
  }, []);

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
      const { destination_secret: madeSecret, ...kept } = a;
      if (madeSecret) setNewSecret(madeSecret);
      a = kept;
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
      if (!id && mounted.current && madeSecret) createdSecret = { id: a.id, secret: madeSecret };
      if (!id && mounted.current)
        navigate(automationEditPath(a.id), { replace: true, state: { notice: done, step: "review" } });
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
        <p className={styles.lede}>Loading…</p>
      </div>
    );
  }

  const engines = list.engines ?? [];
  const savedSwitch = saved ? apiSwitchFor(saved.action, engines) : null;
  const liveProjects = (projects ?? []).filter((p) => !p.archived || p.id === form.projectId);
  const reviewers = reviewerOptions(specialists, engines, engineLabel);
  const reviewerChosen = reviewers.find((r) => r.id === form.reviewerId) ?? null;
  const f = (name: string) => `${uid}-${name}`;
  const props = { form, set, update, f };

  // "Call a webhook" (#2248 P2): a new signing secret for the SAVED destination, shown once.
  async function makeSecret() {
    if (!saved) return;
    setSecretBusy(true);
    setError(null);
    try {
      const r = await api.newDestinationSecret(saved.id);
      setNewSecret(r.secret);
      setSaved((x) => (x && x.id === saved.id ? { ...x, destination: r.destination } : x));
    } catch (e) {
      setError(errorWords(e, "Couldn’t make a new secret."));
    } finally {
      setSecretBusy(false);
    }
  }
  const secretPanel = newSecret ? (
    <DestinationSecretOnce secret={newSecret} onDone={() => setNewSecret(null)} />
  ) : null;

  // What holds each step: the form's own problems, plus what the last delivery's shape says.
  const shape = hook.detail?.last_shape ?? null;
  const byStep = stepProblems(form);
  const checks = deliveryChecks(form, shape);
  const skipped = eventsSkipped(form);
  const gallery = galleryProblem(actions);
  const problemsOf = (s: WizardStepId): string[] =>
    s === "events" && skipped
      ? []
      : [
          ...byStep[s],
          ...(s === "events" || s === "what" ? checks[s].problems : []),
          ...(s === "what" && gallery ? [gallery] : []),
        ];
  const allProblems = WIZARD_STEPS.flatMap((s) => problemsOf(s.id));
  const stepId = WIZARD_STEPS[step].id;
  const here = problemsOf(stepId);
  const isSkipped = (i: number) => WIZARD_STEPS[i].id === "events" && skipped;
  const go = (i: number) => {
    if (saving) return;
    setStep(i);
  };
  const neighbour = (dir: 1 | -1) => {
    let i = step + dir;
    while (i > 0 && i < WIZARD_STEPS.length - 1 && isSkipped(i)) i += dir;
    return i;
  };
  // A step can be jumped to when it is not skipped and every step before it is complete.
  const canJump = (i: number) =>
    !saving &&
    !isSkipped(i) &&
    WIZARD_STEPS.slice(0, i).every((s) => problemsOf(s.id).length === 0);

  const enableButton = saved && (!saved.enabled || saved.needs_reapproval) && (
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
  );

  const body = (() => {
    switch (stepId) {
      case "when":
        return (
          <StepWhen
            {...props}
            list={list}
            zones={zones}
            webhooks={webhooks}
            nowS={nowS}
            saved={saved}
            onNewWebhook={() =>
              setHandoff({ returnTo: location.pathname, draft: form })
            }
            onSavedWebhook={(w) => setSaved((x) => (x && x.id === saved?.id ? { ...x, webhook: w } : x))}
          />
        );
      case "events":
        return (
          <StepEvents
            {...props}
            detail={hook.detail}
            detailError={hook.error}
            onReload={hook.reload}
            problems={here}
            warnings={checks.events.warnings}
          />
        );
      case "what":
        return (
          <StepWhat
            {...props}
            list={list}
            actions={actions}
            stored={{
              engine: saved?.action?.kind === "start_session" ? saved.action.engine : "",
              sessionKey: saved?.action?.kind === "send_to_session" ? saved.action.session_key : "",
            }}
            projects={liveProjects}
            messengers={connectors ? messengers(connectors) : null}
            sessions={sessions}
            templates={templates}
            variables={variables}
            reviewers={reviewers}
            reviewerChosen={reviewerChosen}
            specialistsLoaded={specialists != null}
            switchButton={
              saved && savedSwitch
                ? {
                    target: savedSwitch,
                    blockedBy: dirty ? "Save or discard your changes first." : undefined,
                    onSwitch: () => switchFlow.open(saved, savedSwitch.engine),
                  }
                : null
            }
            onPickFolder={(opener) => {
              setReturnTo(opener);
              setPicking(true);
            }}
            shape={shape ?? []}
            problems={here}
            warnings={checks.what.warnings}
            savedDestination={saved?.destination ?? null}
            onNewSecret={saved?.destination ? () => void makeSecret() : null}
            newSecretBusy={secretBusy}
            secretPanel={secretPanel}
          />
        );
      case "limits":
        return <StepLimits {...props} list={list} />;
      case "review":
        return (
          <StepReview
            {...props}
            list={list}
            saved={saved}
            dirty={dirty}
            presets={hook.detail?.presets ?? []}
            projectName={(pid) => liveProjects.find((p) => p.id === pid)?.name ?? pid}
            problems={allProblems}
            error={
              error && (
                <div className={styles.error} role="alert" data-testid="editor-error">
                  {error}{" "}
                  {stale && (
                    <button type="button" className={styles.ghost} onClick={() => void load()}>
                      Reload (discards your changes)
                    </button>
                  )}
                </div>
              )
            }
            notice={notice}
            inFlight={inFlight}
            secretPanel={secretPanel}
            actions={
              <>
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
                {enableButton}
                <button
                  type="submit"
                  className={styles.primary}
                  disabled={saving || allProblems.length > 0 || (!!saved && !dirty)}
                  data-testid="editor-save"
                >
                  {saving ? "Saving…" : saved?.enabled ? "Save…" : saved ? "Save" : "Save (off)"}
                </button>
              </>
            }
          />
        );
    }
  })();

  return (
    <div className={wiz.root} data-testid="automation-editor">
      <WizardShell
        kicker={saved ? "Edit automation" : "New automation"}
        steps={WIZARD_STEPS.map((s, i) => (isSkipped(i) ? { ...s, label: `${s.label} (skipped)` } : s))}
        current={step}
        canJump={canJump}
        onJump={go}
        heading={HEADINGS[stepId]}
        onBack={step > 0 && !saving ? () => go(neighbour(-1)) : undefined}
        onNext={stepId === "review" ? undefined : () => go(neighbour(1))}
        nextDisabled={saving || here.length > 0}
        nextNote={here.length ? here[0] : undefined}
        secondary={
          <Link
            className={styles.ghost}
            to={saved ? automationPath(saved.id) : AUTOMATIONS_PATH}
            aria-disabled={saving || undefined}
            tabIndex={saving ? -1 : undefined}
            onClick={(e) => saving && e.preventDefault()}
            data-testid="editor-cancel"
          >
            {dirty ? "Cancel" : "Back to the list"}
          </Link>
        }
        leaveGuard={dirty && !saving && !handoff}
        leaveTitle={form.name || saved?.name || "Automation"}
        leaveMessage="Your changes to this automation are not saved."
      >
        <form
          className={wiz.step}
          data-step={stepId}
          onSubmit={(e) => {
            e.preventDefault();
            if (stepId === "review" && !allProblems.length && !saving) void save();
          }}
          aria-busy={saving || undefined}
        >
          {/* LOCKED WHILE A SAVE IS IN FLIGHT (#1252 review): an edit typed now would be replaced by
              the server's answer, so the form cannot be edited until it arrives. */}
          <fieldset className={wiz.lock} disabled={saving} data-testid="editor-fields">
            {body}
          </fieldset>
          {saving && (
            <p className={styles.hint} role="status" data-testid="editor-saving">
              Saving — the form is locked until the server answers.
            </p>
          )}
        </form>
      </WizardShell>

      {switchFlow.dialog}
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
    </div>
  );
}
