/** Plan editing and the single Begin/Re-plan owner (#944).
 * Begin tracks an attached session first; only a mission without one may launch.
 * Launch confirmation binds the saved plan/cwd/objective digest. Every mutation
 * invalidates that confirmation, and a start is not retryable until detail reloads.
 * The controls portal into the header while this editor stays mounted across views. */
import { useCallback, useLayoutEffect, useMemo, useRef, useState } from "react";
import { createPortal } from "react-dom";
import { ApiError, api } from "../../lib/api";
import { objectivesDigestInput, sha256Hex } from "../../lib/digest";
import type { Mission } from "../../types/api";
import styles from "./mission.module.css";

const PLANNABLE = new Set(["draft", "planned"]);
type Confirmation = {
  planId: string;
  cwd: string;
  digest: string;
  input: string;
};

export function MissionPlanCard({
  mission,
  onChanged,
  onNote,
  actionSlot,
  onObjectives,
  onPlan,
}: {
  mission: Mission;
  onChanged: (opts?: { membershipChanged?: boolean }) => void;
  onNote: (msg: string) => void;
  actionSlot?: HTMLElement | null;
  onObjectives?: () => void;
  onPlan?: () => void;
}) {
  const plan = mission.plan ?? null;
  const [busy, setBusy] = useState<string | null>(null);
  const operation = useRef(false);
  const latestMission = useRef(mission);
  useLayoutEffect(() => {
    latestMission.current = mission;
  }, [mission]);
  const editor = useRef<HTMLDivElement>(null);
  const revision = useRef(0);
  const [confirming, setConfirming] = useState<Confirmation | null>(null);
  const [brief, setBrief] = useState(plan?.brief ?? "");
  const [planId, setPlanId] = useState(plan?.plan_id);
  const [error, setError] = useState<string | null>(null);
  const [awaitingRead, setAwaitingRead] = useState<Mission | null>(null);
  if (planId !== plan?.plan_id) {
    setPlanId(plan?.plan_id);
    setBrief(plan?.brief ?? "");
    setConfirming(null);
  }
  const objectives = useMemo(
    () => mission.objectives ?? [],
    [mission.objectives],
  );
  const input = objectivesDigestInput(objectives);
  const hasSession = (mission.sessions ?? []).some((s) => !s.removed_at);
  const draft = brief.trim();
  const unsaved = draft !== (plan?.brief ?? "");
  const waiting = awaitingRead === mission;
  const disabled = busy !== null || waiting;
  const active = mission.archived_at == null && PLANNABLE.has(mission.state);
  const validConfirmation =
    confirming !== null &&
    !hasSession &&
    !unsaved &&
    confirming.planId === plan?.plan_id &&
    confirming.cwd === (plan?.cwd ?? "") &&
    confirming.input === input;
  if (confirming && !validConfirmation) {
    setConfirming(null);
    setError("The plan or checklist changed. Review it and begin again.");
  }

  const reason = hasSession
    ? !mission.cwd
      ? "A resolved project folder is required before tracking this session."
      : "Begin tracks the attached session. No new agent will be started."
    : !plan
      ? "Re-plan to prepare a proposal before beginning."
      : !plan.project_id
        ? "Choose a project before beginning."
        : !plan.engine
          ? "Choose an agent before beginning."
          : !draft
            ? "Write and save the instructions before beginning."
            : unsaved
              ? "Save the instructions before beginning."
              : mission.objectives_state === "pending"
                ? "Wait for the objectives before beginning."
                : objectives.length === 0
                  ? "Add at least one objective before beginning."
                  : "Begin starts a new agent with the saved plan. Review the launch before confirming.";
  const ready = hasSession
    ? Boolean(mission.cwd)
    : Boolean(
        plan?.project_id &&
          plan.engine &&
          plan.brief?.trim() &&
          !unsaved &&
          mission.objectives_state !== "pending" &&
          objectives.length > 0,
      );

  const run = useCallback(
    async (
      kind: string,
      fn: () => Promise<unknown>,
      membershipChanged = false,
    ) => {
      if (operation.current) return;
      operation.current = true;
      revision.current += 1;
      setBusy(kind);
      setConfirming(null);
      setError(null);
      try {
        await fn();
      } catch (err) {
        const message =
          err instanceof ApiError && err.message
            ? err.message
            : "That action could not be completed. Check the current mission before trying again.";
        setError(message);
        onNote(message);
      } finally {
        // A lost start response is not permission to launch again. The hook's reload
        // fences earlier reads; only a new detail object releases these controls.
        setAwaitingRead(latestMission.current);
        onChanged({ membershipChanged });
        operation.current = false;
        setBusy(null);
      }
    },
    [onChanged, onNote],
  );

  const reviewPlan = () => {
    onPlan?.();
    requestAnimationFrame(() => {
      editor.current?.scrollIntoView({ block: "nearest" });
      const target = !plan?.project_id
        ? "#plan-project"
        : !plan.engine
          ? "#plan-engine"
          : "#plan-brief";
      editor.current?.querySelector<HTMLElement>(target)?.focus();
    });
  };

  const propose = () => void run("plan", () => api.planMission(mission.id));
  const edit = (body: {
    project_id?: string | null;
    engine?: string;
    brief?: string;
  }) => {
    if (!plan || disabled) return;
    void run("edit", () => api.editMissionPlan(mission.id, plan.plan_id, body));
  };
  const begin = async () => {
    if (!active || disabled || operation.current || !ready) return;
    if (hasSession) {
      // Never fall through to launch after a refused tracking transition.
      await run(
        "begin",
        async () => {
          if (mission.state === "draft")
            await api.setMissionState(mission.id, {
              from: "draft",
              to: "planned",
            });
          await api.setMissionState(mission.id, {
            from: "planned",
            to: "running",
          });
        },
        true,
      );
      return;
    }
    if (!plan) return;
    if (validConfirmation && confirming) {
      const approved = confirming;
      await run(
        "begin",
        async () => {
          const out = await api.dispatchMission(
            mission.id,
            approved.planId,
            approved.cwd,
            approved.digest,
          );
          if (out.state !== "running") {
            const why =
              out.reason || `Begin ended with mission state ${out.state}.`;
            setError(why);
            onNote(why);
          }
        },
        true,
      );
    } else {
      onPlan?.();
      const at = ++revision.current;
      const digest = await sha256Hex(input);
      if (at !== revision.current || operation.current) return;
      setConfirming({
        planId: plan.plan_id,
        cwd: plan.cwd ?? "",
        digest,
        input,
      });
    }
  };

  if (!active && busy === null && mission.state !== "dispatching") return null;
  const controls = (
    <div className={styles.startControls} data-testid="mission-start-controls">
      <div className={styles.startButtons}>
        <button
          type="button"
          className={styles.send}
          disabled={!active || disabled || !ready}
          onClick={() => void begin()}
          data-testid="mission-begin"
          aria-describedby="mission-start-reason"
        >
          {busy === "begin" || mission.state === "dispatching"
            ? "Starting…"
            : validConfirmation
              ? "Confirm begin"
              : "Begin"}
        </button>
        <button
          type="button"
          className={styles.planBtn}
          disabled={!active || disabled}
          onClick={propose}
          data-testid="mission-replan"
        >
          {busy === "plan" ? "Planning…" : "Re-plan"}
        </button>
        {validConfirmation ? (
          <button
            type="button"
            className={styles.planBtn}
            onClick={() => {
              revision.current += 1;
              setConfirming(null);
            }}
          >
            Cancel
          </button>
        ) : null}
      </div>
      <div
        id="mission-start-reason"
        className={styles.startReason}
        role="status"
      >
        {validConfirmation ? (
          <span data-testid="mission-dispatch-confirm">
            This starts {plan?.engine} in {plan?.cwd}, unattended, with the
            saved instructions and {objectives.length}{" "}
            {objectives.length === 1 ? "objective" : "objectives"}.
          </span>
        ) : waiting ? (
          "Checking the latest mission state…"
        ) : (
          reason
        )}
        {waiting ? (
          <button
            type="button"
            className={styles.linkButton}
            onClick={() => onChanged()}
          >
            Refresh mission
          </button>
        ) : null}
        {!hasSession &&
        (!plan || !plan.project_id || !plan.engine || unsaved || !draft) &&
        onPlan ? (
          <button
            type="button"
            className={styles.linkButton}
            onClick={reviewPlan}
          >
            Review plan
          </button>
        ) : null}
        {!hasSession && plan && objectives.length === 0 && onObjectives ? (
          <button
            type="button"
            className={styles.linkButton}
            onClick={onObjectives}
          >
            Add objectives
          </button>
        ) : null}
      </div>
      {error ? (
        <div
          className={styles.planError}
          role="alert"
          data-testid="mission-plan-error"
        >
          {error}
        </div>
      ) : null}
    </div>
  );
  return (
    <>
      {actionSlot ? createPortal(controls, actionSlot) : controls}
      {active ? (
        <div
          ref={editor}
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
          {!plan ? (
            <p className={styles.planWhy}>
              {hasSession
                ? "Begin follows the work already attached to this mission. Re-plan prepares a proposal for review."
                : "Re-plan prepares a project, agent and instructions for you to review."}
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
                      edit({ project_id: e.target.value || null })
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
                    onChange={(e) => edit({ engine: e.target.value })}
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
                value={brief}
                disabled={disabled}
                onChange={(e) => {
                  revision.current += 1;
                  setConfirming(null);
                  setBrief(e.target.value);
                }}
                onBlur={() => {
                  if (draft && unsaved) edit({ brief: draft });
                }}
                data-testid="mission-plan-brief"
              />
              {unsaved ? (
                <div
                  className={styles.planWhy}
                  data-testid="mission-plan-unsaved"
                >
                  {draft
                    ? "These instructions have not been saved. Leave the field to save before beginning."
                    : "A brief is required. Write one or Re-plan to prepare a proposal."}
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
                {onObjectives ? (
                  <button
                    type="button"
                    className={styles.linkButton}
                    onClick={onObjectives}
                  >
                    {objectives.length ? "Review objectives" : "Add objectives"}
                  </button>
                ) : null}
              </div>
            </>
          )}
        </div>
      ) : null}
    </>
  );
}
