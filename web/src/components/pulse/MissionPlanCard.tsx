/** Plan editing (#893 Phase 4) and the plan's own status line (#944, #967).
 *
 * The card edits the saved plan: its project, its agent and its brief, each edit naming the plan it
 * edits. It no longer draws Begin or Plan again. Those belong to the header, whose one owner is
 * `MissionHeaderActions`, and both components read the same `useMissionStart` model, which the mission
 * body owns. So the brief draft that decides whether Begin may be pressed is one state, not two copies.
 *
 * What the card keeps is everything that EXPLAINS: why Begin is or is not available, the launch it is
 * about to confirm (with its Cancel), and the error from the last attempt. That text used to sit under
 * the header's buttons, in a 460px box that jumped ahead with `order: -1`, and it is what wrapped the
 * header onto two lines. Here it sits beside the plan it describes, and a disabled Begin points at it
 * with `aria-describedby`.
 *
 * Nothing on the card starts anything: the Cancel of an armed launch is the only button it adds. */
import { useEffect, useRef } from "react";

import type { Mission } from "../../types/api";
import action from "../ui/actionButton.module.css";
import styles from "./mission.module.css";
import {
  LAUNCH_WARNING_ID,
  PLAN_CARD_ID,
  START_REASON_ID,
  type MissionStart,
} from "./useMissionStart";

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

  if (!active) return null;
  return (
    <div
      id={PLAN_CARD_ID}
      className={styles.planCard}
      data-testid="mission-plan-card"
    >
      <div className={styles.planLead}>
        {plan
          ? "The plan"
          : hasSession
            ? "Ready to track the attached session"
            : "No plan yet"}
      </div>
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
      ) : (
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
          (!plan || !plan.project_id || !plan.engine || unsaved || !draft) &&
          start.canReviewPlan ? (
            <button
              type="button"
              className={styles.linkButton}
              onClick={start.reviewPlan}
            >
              Review plan
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
      {start.error ? (
        <div
          className={styles.planError}
          role="alert"
          data-testid="mission-plan-error"
        >
          {start.error}
        </div>
      ) : null}
      {!plan ? (
        <p className={styles.planWhy}>
          {hasSession
            ? "Begin follows the work already attached to this mission. Plan again, under ⋯, prepares a proposal for review."
            : "Plan again, under ⋯, prepares a project, agent and instructions for you to review."}
        </p>
      ) : (
        <>
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
          {validConfirmation ? (
            <ul className={styles.confirmObjectives}>
              {objectives.map((o) => (
                <li key={o.key}>
                  {o.title}
                  {o.gate ? " · Required" : ""}
                </li>
              ))}
            </ul>
          ) : null}
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
        </>
      )}
    </div>
  );
}
