/** The plan card: how planning stands, the plan itself, and why Begin is or is not available
 *  (#893 Phase 4, #944, #967).
 *
 * **Three states, one line each (#967 P2b, mockup B3).** A new mission plans itself, so the card leads
 * with where that stands:
 *
 * - **planning**: "Planning…" with a quiet working indicator. No fields: there is nothing to edit yet,
 *   and Begin is disabled with that sentence as its reason.
 * - **ready**: "Plan ready — {agent} in {project} · {n} objectives" and Review plan, which unfolds the
 *   editable project, agent and brief. A plan that is missing something starts unfolded.
 * - **could not plan**: "Couldn't plan: {reason}" and Plan manually, a ghost that opens a project
 *   picker, an agent picker and a brief, saved as the mission's FIRST plan. Plan again stays in ⋯.
 *
 * The state is decided in `useMissionStart` (`phase`, from `plan_state` and the plan), not here, so
 * the header's Begin and this card cannot disagree about it.
 *
 * **What the card still explains.** Why Begin is or is not available, the launch it is about to
 * confirm (with its Cancel), and the error from the last attempt. A disabled Begin points at the
 * reason with `aria-describedby`. For planning and could-not-plan the lead line IS that reason, so
 * it is said once rather than twice.
 *
 * Nothing on the card starts an agent: its buttons are Cancel, Review plan, Plan manually and the
 * manual plan's Save, and none of them launches anything. */
import { Pencil } from "lucide-react";
import { useEffect, useRef, useState, type FormEvent } from "react";

import type { Mission } from "../../types/api";
import action from "../ui/actionButton.module.css";
import styles from "./mission.module.css";
import {
  LAUNCH_WARNING_ID,
  PLAN_CARD_ID,
  START_REASON_ID,
  type MissionStart,
} from "./useMissionStart";

/** "Couldn't plan: the reason" with the reason in secondary text, as mockup B3 sets it. */
function Lead({ text }: { text: string }) {
  const at = text.indexOf(": ");
  if (at < 0) return <>{text}</>;
  return (
    <>
      {text.slice(0, at + 1)}{" "}
      <span className={styles.planDim}>{text.slice(at + 2)}</span>
    </>
  );
}

/** PLAN MANUALLY's form (#967 P2b). Mounted only while open, so Cancel discards what was typed.
 *
 *  The choices come from the mission detail's `plan_options`, which the server attaches exactly while
 *  a first plan may be saved, built by the same functions that validate the save. The client sends
 *  ids, never a path. A refusal (422) is the server's own sentence, shown under the form by the card. */
function ManualPlan({
  mission,
  start,
  onCancel,
}: {
  mission: Mission;
  start: MissionStart;
  onCancel: () => void;
}) {
  const projects = mission.plan_options?.project_options ?? [];
  const engines = mission.plan_options?.engine_options ?? [];
  const [projectId, setProjectId] = useState(() =>
    projects.some((p) => p.id === mission.project_id)
      ? (mission.project_id ?? "")
      : "",
  );
  const [engine, setEngine] = useState("");
  const [brief, setBrief] = useState(() => mission.instruction ?? "");
  const first = useRef<HTMLSelectElement>(null);
  // The operator pressed Plan manually: take them to the first choice.
  useEffect(() => first.current?.focus(), []);

  const missing = !projectId
    ? "Choose a project to run in."
    : !engine
      ? "Choose the agent to run this plan."
      : !brief.trim()
        ? "Write the instructions for the agent."
        : null;
  const submit = (e: FormEvent) => {
    e.preventDefault();
    if (missing || start.disabled) return;
    start.planManually({ project_id: projectId, engine, brief: brief.trim() });
  };

  return (
    <form
      className={styles.planManual}
      onSubmit={submit}
      aria-label="Plan manually"
      data-testid="mission-plan-manual"
    >
      <div className={styles.planFields}>
        <label className={styles.planLabel} htmlFor="plan-manual-project">
          Run in project
          <select
            ref={first}
            id="plan-manual-project"
            className={styles.planField}
            value={projectId}
            disabled={start.disabled}
            onChange={(e) => setProjectId(e.target.value)}
            data-testid="mission-manual-project"
          >
            <option value="">Choose a project…</option>
            {projects.map((p) => (
              <option key={p.id} value={p.id}>
                {p.name}
              </option>
            ))}
          </select>
        </label>
        <label className={styles.planLabel} htmlFor="plan-manual-engine">
          Agent
          <select
            id="plan-manual-engine"
            className={styles.planField}
            value={engine}
            disabled={start.disabled}
            onChange={(e) => setEngine(e.target.value)}
            data-testid="mission-manual-engine"
          >
            <option value="">Choose an agent…</option>
            {engines.map((e) => (
              <option key={e.id} value={e.id}>
                {e.label}
              </option>
            ))}
          </select>
        </label>
      </div>
      {projects.length === 0 || engines.length === 0 ? (
        <p className={styles.planWhy} data-testid="mission-manual-no-options">
          {projects.length === 0
            ? "No project can be planned into yet. Add a project with a folder first."
            : "No installed agent can be started from a plan."}
        </p>
      ) : null}
      <label className={styles.planLabel} htmlFor="plan-manual-brief">
        Instructions for the agent
      </label>
      <textarea
        id="plan-manual-brief"
        className={styles.planBrief}
        rows={5}
        value={brief}
        disabled={start.disabled}
        onChange={(e) => setBrief(e.target.value)}
        data-testid="mission-manual-brief"
      />
      {missing ? (
        <p className={styles.planWhy} id="plan-manual-missing">
          {missing}
        </p>
      ) : null}
      <div className={styles.planStatusActions}>
        <button
          type="submit"
          className={action.primary}
          disabled={Boolean(missing) || start.disabled}
          aria-describedby={missing ? "plan-manual-missing" : undefined}
          data-testid="mission-manual-save"
        >
          {start.busy === "manual" ? "Saving…" : "Save plan"}
        </button>
        <button
          type="button"
          className={action.ghost}
          onClick={onCancel}
          data-testid="mission-manual-cancel"
        >
          Cancel
        </button>
      </div>
    </form>
  );
}

export function MissionPlanCard({
  mission,
  start,
}: {
  mission: Mission;
  start: MissionStart;
}) {
  const {
    plan,
    active,
    disabled,
    hasSession,
    objectives,
    validConfirmation,
    waiting,
    unsaved,
    draft,
    phase,
  } = start;

  /** BRING THE ARMED LAUNCH TO THE OPERATOR (#967; Hermes on #976). Begin is armed from the fixed
   *  header, but this card scrolls with the thread, so with a long conversation scrolled to its end the
   *  warning (which agent, which directory, unattended) and its Cancel sat thousands of pixels out of
   *  view, and the second click could dispatch unseen. It used to sit beside Begin in the header.
   *
   *  So once the confirmation has RENDERED (an effect on the armed state, never a timeout) it is
   *  scrolled into view inside whichever pane scrolls, and focus moves to the confirmation block
   *  itself: not to Cancel and not to Confirm, so a repeated Enter can neither confirm nor silently
   *  cancel. The block's name and description are what a screen reader hears on that focus, which is
   *  why the warning is no longer inside the reason's live region: one announcement, not two. */
  const confirmRef = useRef<HTMLDivElement>(null);
  useEffect(() => {
    if (!validConfirmation) return;
    const el = confirmRef.current;
    if (!el) return;
    const reduce =
      window.matchMedia?.("(prefers-reduced-motion: reduce)")?.matches ?? false;
    // `nearest` moves the pane only as far as needed; `scroll-margin` keeps the block off its edge.
    el.scrollIntoView?.({
      block: "nearest",
      behavior: reduce ? "auto" : "smooth",
    });
    // `preventScroll`, or focus would jump the pane instantly and cut the smooth scroll short.
    el.focus({ preventScroll: true });
  }, [validConfirmation]);

  /** Plan manually is open. Closed again the moment the mission has a plan or starts planning, so a
   *  saved first plan folds the form into the ready line instead of leaving an empty form behind. */
  const [manualOpen, setManualOpen] = useState(false);
  if (manualOpen && phase !== "unplanned") setManualOpen(false);

  if (!active) return null;

  const planning = !hasSession && phase === "planning";
  const unplanned = !hasSession && phase === "unplanned";
  const manual = unplanned && manualOpen;
  /** For planning and could-not-plan the lead line is Begin's reason; for the rest the reason is its
   *  own line under the lead. While a read is awaited that line says so instead. */
  const leadIsReason = (planning || unplanned) && !waiting;

  const projectName = plan
    ? (plan.project_options?.find((p) => p.id === plan.project_id)?.name ??
      plan.cwd ??
      plan.project_id)
    : null;
  const engineLabel = plan
    ? (plan.engine_options?.find((e) => e.id === plan.engine)?.label ??
      plan.engine)
    : null;
  const objectivesText =
    mission.objectives_state === "pending"
      ? "preparing objectives"
      : `${objectives.length} ${objectives.length === 1 ? "objective" : "objectives"}`;

  const lead = planning ? (
    <>
      Planning…{" "}
      <span className={styles.planDim}>A plan is still being prepared.</span>
      <span className={styles.planDots} aria-hidden="true">
        <i />
        <i />
        <i />
      </span>
    </>
  ) : unplanned ? (
    <Lead text={start.unplannedLead} />
  ) : hasSession ? (
    "Ready to track the attached session"
  ) : start.planComplete ? (
    <>
      Plan ready —{" "}
      <span className={styles.planMono}>{engineLabel}</span> in{" "}
      <span className={styles.planMono}>{projectName}</span> · {objectivesText}
    </>
  ) : (
    "The plan"
  );

  const objectivesLine = (
    <div
      className={styles.planWhy}
      data-testid={
        objectives.length
          ? "mission-plan-objectives"
          : "mission-plan-no-objectives"
      }
    >
      {mission.objectives_state === "pending"
        ? "Preparing objectives…"
        : `${objectives.length} objectives define what done means.`}
      {start.onObjectives ? (
        <button
          type="button"
          className={styles.linkButton}
          onClick={start.onObjectives}
        >
          {objectives.length ? "Review objectives" : "Add objectives"}
        </button>
      ) : null}
    </div>
  );

  const errorBlock = start.error ? (
    <div
      className={styles.planError}
      role="alert"
      data-testid="mission-plan-error"
    >
      {start.error}
    </div>
  ) : null;

  return (
    <div
      id={PLAN_CARD_ID}
      className={styles.planCard}
      data-testid="mission-plan-card"
      data-plan-state={hasSession ? "session" : phase}
    >
      <div className={styles.planHead}>
        <div
          className={styles.planHeadText}
          {...(leadIsReason ? { id: START_REASON_ID, role: "status" } : {})}
          aria-busy={planning || undefined}
          data-testid="mission-plan-lead"
        >
          {lead}
        </div>
        {unplanned ? (
          <button
            type="button"
            className={action.ghost}
            aria-expanded={manualOpen}
            disabled={disabled}
            onClick={() => setManualOpen((open) => !open)}
            data-testid="mission-plan-manually"
          >
            <Pencil size={15} aria-hidden="true" />
            Plan manually
          </button>
        ) : null}
        {phase === "ready" && start.planComplete ? (
          <button
            type="button"
            className={styles.linkButton}
            aria-expanded={start.fieldsOpen}
            onClick={start.fieldsOpen ? start.hidePlan : start.reviewPlan}
            data-testid="mission-plan-review"
          >
            {start.fieldsOpen ? "Hide plan" : "Review plan"}
          </button>
        ) : null}
      </div>
      {start.keptNote ? (
        <p className={styles.planWhy} data-testid="mission-plan-kept">
          {start.keptNote}
        </p>
      ) : null}
      {validConfirmation ? (
        /* THE ARMED LAUNCH: the consequence and its way back, as one focusable, named block. The
           header's Confirm begin is described by the warning inside it. */
        <div
          ref={confirmRef}
          className={styles.planConfirm}
          tabIndex={-1}
          role="group"
          aria-label="Confirm the launch"
          aria-describedby={LAUNCH_WARNING_ID}
          data-testid="mission-launch-confirm"
        >
          <p
            id={LAUNCH_WARNING_ID}
            className={styles.planConfirmText}
            data-testid="mission-dispatch-confirm"
          >
            This starts {plan?.engine} in {plan?.cwd}, unattended, with the
            saved instructions and {objectives.length}{" "}
            {objectives.length === 1 ? "objective" : "objectives"}.
          </p>
          <ul className={styles.confirmObjectives}>
            {objectives.map((o) => (
              <li key={o.key}>
                {o.title}
                {o.gate ? " · Required" : ""}
              </li>
            ))}
          </ul>
          {/* The header keeps one primary, so the Cancel that pairs with "Confirm begin" lives with
              the confirmation it cancels, as a ghost. */}
          <div className={styles.planStatusActions}>
            <button
              type="button"
              className={action.ghost}
              onClick={start.cancelConfirm}
              data-testid="mission-begin-cancel"
            >
              Cancel
            </button>
          </div>
        </div>
      ) : leadIsReason ? null : (
        /* THE REASON, next to the plan it is about (#967). Begin's `aria-describedby` names this id. */
        <div id={START_REASON_ID} className={styles.planStatus} role="status">
          {waiting ? "Checking the latest mission state…" : start.reason}
          {waiting ? (
            <button
              type="button"
              className={styles.linkButton}
              onClick={start.refresh}
            >
              Refresh mission
            </button>
          ) : null}
          {!hasSession &&
          plan &&
          objectives.length === 0 &&
          start.onObjectives ? (
            <button
              type="button"
              className={styles.linkButton}
              onClick={start.onObjectives}
            >
              Add objectives
            </button>
          ) : null}
        </div>
      )}
      {manual ? null : errorBlock}
      {manual ? (
        <>
          <ManualPlan
            mission={mission}
            start={start}
            onCancel={() => setManualOpen(false)}
          />
          {errorBlock}
        </>
      ) : null}
      {phase === "ready" &&
      !start.fieldsOpen &&
      (mission.objectives_state === "pending" || objectives.length === 0)
        ? /* Folded, the lead carries the count. A checklist that is still coming, or empty, is a
             reason Begin waits, so it stays visible rather than behind Review plan. */
          objectivesLine
        : null}
      {plan && start.fieldsOpen ? (
        <div className={styles.planOpen} data-testid="mission-plan-fields">
          <div className={styles.planFields}>
            <label className={styles.planLabel} htmlFor="plan-project">
              Run in project
              <select
                id="plan-project"
                className={styles.planField}
                value={plan.project_id ?? ""}
                disabled={disabled}
                onChange={(e) =>
                  start.edit({ project_id: e.target.value || null })
                }
                data-testid="mission-plan-project"
              >
                <option value="">Choose a project…</option>
                {(plan.project_options ?? []).map((p) => (
                  <option key={p.id} value={p.id}>
                    {p.name}
                  </option>
                ))}
              </select>
            </label>
            <label className={styles.planLabel} htmlFor="plan-engine">
              Agent
              <select
                id="plan-engine"
                className={styles.planField}
                value={plan.engine ?? ""}
                disabled={disabled}
                onChange={(e) => start.edit({ engine: e.target.value })}
                data-testid="mission-plan-engine"
              >
                <option value="">Choose an agent…</option>
                {(plan.engine_options ?? []).map((e) => (
                  <option key={e.id} value={e.id}>
                    {e.label}
                  </option>
                ))}
              </select>
            </label>
          </div>
          {!plan.project_id ? (
            <div
              className={styles.planWhy}
              data-testid="mission-plan-no-project"
            >
              {plan.dropped?.includes("project")
                ? "the model did not give one"
                : "Pick a project to run in."}
            </div>
          ) : null}
          <div className={styles.planWhy} data-testid="mission-plan-reason">
            {plan.engine_reason ||
              (plan.engine
                ? "you chose this"
                : "Choose the agent to run this plan.")}
          </div>
          <label className={styles.planLabel} htmlFor="plan-brief">
            Instructions for the agent
          </label>
          <textarea
            id="plan-brief"
            className={styles.planBrief}
            rows={5}
            value={start.brief}
            disabled={disabled}
            onChange={(e) => start.changeBrief(e.target.value)}
            onBlur={() => {
              if (draft && unsaved) start.edit({ brief: draft });
            }}
            data-testid="mission-plan-brief"
          />
          {unsaved ? (
            <div className={styles.planWhy} data-testid="mission-plan-unsaved">
              {draft
                ? "These instructions have not been saved. Leave the field to save before beginning."
                : "A brief is required. Write one, or use Plan again to prepare a proposal."}
            </div>
          ) : null}
          {objectivesLine}
        </div>
      ) : null}
    </div>
  );
}
