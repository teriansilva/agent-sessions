/** The plan card: whether the mission has a plan, and — for a legacy one-agent plan — why Begin is
 *  or is not available (#893 Phase 4, #944, #967, #2152).
 *
 * **HQ plans every mission (#2152).** The one-agent planner is retired, and with it this card's
 * Planning…, Couldn't plan, Plan manually, the agent picker and the instructions box. What is left
 * (mockup C):
 *
 * - **no plan** (a draft from the API, or one an old pending plan left): "This mission has no plan"
 *   and **Plan with HQ**, which opens HQ's page bound to THIS mission (the server validates the id,
 *   and HQ's card plans this draft; it never creates a second one). With no AI endpoint HQ can't
 *   plan, so the card says so and links to Settings → AI → HQ; the draft is kept.
 * - **a legacy one-agent plan** (D2): read-only — "One agent: {agent} in {project}", the saved
 *   instructions on request — with its Begin in the header, unchanged, until the mission is
 *   finished or archived.
 * - **HQ planned it** (a plan revision exists): nothing here. The Team, Plan approval and Steps
 *   cards below carry it, unchanged.
 *
 * The armed launch of a legacy plan (which agent, which directory, unattended) and its Cancel still
 * render here, next to the plan they launch; Begin itself lives in the header (`MissionHeaderActions`).
 * Nothing on the card starts an agent: its buttons are Plan with HQ, Cancel and Edit objectives. */
import { Bot, Settings as SettingsIcon } from "lucide-react";
import { useEffect, useRef } from "react";
import { Link, useNavigate } from "react-router-dom";

import { useConfig } from "../../app/config";
import { HQ_PATH, hqSeedState } from "../../lib/routes";
import { motionMode } from "../../theme/motion";
import { settingsPath } from "../../routes/settingsTabs";
import type { Mission } from "../../types/api";
import action from "../ui/actionButton.module.css";
import styles from "./mission.module.css";
import { checksWhat, worthALook } from "./confirmSet";
import { planThisMission } from "./hqPlanning";
import {
  LAUNCH_WARNING_ID,
  PLAN_CARD_ID,
  START_REASON_ID,
  UNPLANNED_LEAD,
  type MissionStart,
} from "./useMissionStart";

/** The first line of a legacy plan's instructions, for the read-only card. */
function excerpt(text: string, max = 160): string {
  const line = text.trim().split("\n")[0] ?? "";
  return line.length > max ? `${line.slice(0, max - 1)}…` : line;
}

export function MissionPlanCard({
  mission,
  start,
}: {
  mission: Mission;
  start: MissionStart;
}) {
  const { plan, active, hasSession, objectives, validConfirmation, waiting, phase } = start;
  const navigate = useNavigate();
  const config = useConfig();
  /** HQ answers only with an AI endpoint (the flag HQ's own page reads). Unknown while loading. */
  const hqReady = config ? config.pulse?.configured !== false : true;

  /** BRING THE ARMED LAUNCH TO THE OPERATOR (#967; Hermes on #976). Begin is armed from the fixed
   *  header, but this card scrolls with the thread: once the confirmation has RENDERED it is scrolled
   *  into view and focus moves to the confirmation block itself (never to Confirm or Cancel, so a
   *  repeated Enter can neither confirm nor silently cancel). */
  const confirmRef = useRef<HTMLDivElement>(null);
  useEffect(() => {
    if (!validConfirmation) return;
    const el = confirmRef.current;
    if (!el) return;
    const reduce = motionMode() !== "full";
    el.scrollIntoView?.({
      block: "nearest",
      behavior: reduce ? "auto" : "smooth",
    });
    el.focus({ preventScroll: true });
  }, [validConfirmation]);

  if (!active) return null;
  // HQ planned it: the Team, Plan approval and Steps cards carry it (#2152).
  if (phase === "hq" && !hasSession) return null;

  const unplanned = !hasSession && phase === "unplanned";
  const projectName = plan
    ? (plan.project_options?.find((p) => p.id === plan.project_id)?.name ??
      plan.cwd ??
      plan.project_id)
    : null;

  const errorBlock = start.error ? (
    <div className={styles.planError} role="alert" data-testid="mission-plan-error">
      {start.error}
    </div>
  ) : null;

  if (unplanned) {
    /* NO PLAN (mockup C, first two boards): Plan with HQ, or why HQ can't plan yet. */
    return (
      <div
        id={PLAN_CARD_ID}
        className={styles.planCard}
        data-testid="mission-plan-card"
        data-plan-state={hqReady ? "unplanned" : "hq-unavailable"}
      >
        <div className={styles.planHead}>
          <div
            className={styles.planHeadText}
            id={START_REASON_ID}
            role="status"
            data-testid="mission-plan-lead"
          >
            {hqReady ? UNPLANNED_LEAD : "HQ isn't set up to plan missions"}
          </div>
        </div>
        <p className={styles.planWhy} data-testid="mission-plan-why">
          {hqReady
            ? "HQ plans missions: it picks or writes a flow for the goal and assembles the team."
            : "Planning needs HQ, and HQ needs an AI endpoint. The draft is kept; plan it once HQ is set up."}
        </p>
        <div className={styles.planStatusActions}>
          {hqReady ? (
            start.canPlanWithHq ? (
              <button
                type="button"
                className={action.primary}
                onClick={() =>
                  navigate(HQ_PATH, {
                    state: hqSeedState(planThisMission(mission), mission.id),
                  })
                }
                data-testid="mission-plan-with-hq"
              >
                <Bot size={15} aria-hidden="true" />
                Plan with HQ
              </button>
            ) : null
          ) : (
            <Link
              className={action.ghost}
              to={settingsPath("ai-hq")}
              data-testid="mission-plan-hq-settings"
            >
              <SettingsIcon size={15} aria-hidden="true" />
              Open Settings → AI → HQ
            </Link>
          )}
        </div>
        {errorBlock}
      </div>
    );
  }

  const objectivesText =
    mission.objectives_state === "pending"
      ? "preparing objectives"
      : `${objectives.length} ${objectives.length === 1 ? "objective" : "objectives"}`;

  return (
    <div
      id={PLAN_CARD_ID}
      className={styles.planCard}
      data-testid="mission-plan-card"
      data-plan-state={hasSession ? "session" : "legacy"}
    >
      <div className={styles.planHead}>
        <div className={styles.planHeadText} data-testid="mission-plan-lead">
          {hasSession ? (
            "Ready to track the attached session"
          ) : (
            <>
              One agent: <span className={styles.planMono}>{plan?.engine || "no agent"}</span> in{" "}
              <span className={styles.planMono}>{projectName || "no project"}</span> ·{" "}
              {objectivesText}
            </>
          )}
        </div>
        {!hasSession ? (
          <span className={styles.planChip} data-testid="mission-plan-legacy">
            Legacy plan
          </span>
        ) : null}
      </div>
      {!hasSession && start.brief ? (
        <details className={styles.planSaved} data-testid="mission-plan-brief">
          <summary>“{excerpt(start.brief)}”</summary>
          <div className={styles.planSavedText}>{start.brief}</div>
        </details>
      ) : null}
      {!hasSession ? (
        <p className={styles.planWhy} data-testid="mission-plan-legacy-note">
          Kept so this mission can still run. New missions are planned by HQ.
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
            This starts {plan?.engine} in {plan?.cwd}, unattended, with the saved instructions and{" "}
            {objectives.length} {objectives.length === 1 ? "objective" : "objectives"}.
          </p>
          {/* WHAT IT WILL TRACK (#1061 Phase 3): each objective, required or not, and what it checks. */}
          <ul className={styles.confirmObjectives} data-testid="mission-confirm-objectives">
            {objectives.map((o) => (
              <li key={o.key} className={styles.confirmRow}>
                <span className={o.gate ? styles.confirmGate : styles.confirmGoal}>
                  {o.gate ? "Required" : "Goal"}
                </span>
                <span className={styles.confirmTitle}>{o.title}</span>
                <span className={styles.confirmChecks}>{checksWhat(o)}</span>
              </li>
            ))}
          </ul>
          {(() => {
            const why = worthALook(mission.objectives_fit, objectives);
            return why.length ? (
              <div className={styles.confirmWhy} data-testid="mission-confirm-why">
                <strong>Worth a look before it starts.</strong> {why.join(" ")}
              </div>
            ) : null;
          })()}
          <div className={styles.planStatusActions}>
            <button
              type="button"
              className={action.ghost}
              onClick={start.cancelConfirm}
              data-testid="mission-begin-cancel"
            >
              Cancel
            </button>
            {start.onObjectives ? (
              <button
                type="button"
                className={action.ghost}
                onClick={() => {
                  start.cancelConfirm();
                  start.onObjectives?.();
                }}
                data-testid="mission-confirm-edit"
              >
                Edit objectives
              </button>
            ) : null}
          </div>
        </div>
      ) : (
        /* THE REASON, next to the plan it is about (#967). Begin's `aria-describedby` names this id. */
        <div id={START_REASON_ID} className={styles.planStatus} role="status">
          {waiting ? "Checking the latest mission state…" : start.reason}
          {waiting ? (
            <button type="button" className={styles.linkButton} onClick={start.refresh}>
              Refresh mission
            </button>
          ) : null}
          {!hasSession && objectives.length === 0 && start.onObjectives ? (
            <button type="button" className={styles.linkButton} onClick={start.onObjectives}>
              Add objectives
            </button>
          ) : null}
        </div>
      )}
      {errorBlock}
    </div>
  );
}
