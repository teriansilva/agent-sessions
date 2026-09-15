/** The start model: Begin, Plan again and the launch confirmation, shared by the header and the plan
 *  card (#944, #967).
 *
 *  Begin tracks an attached session first; only a mission without one may launch. Launch confirmation
 *  binds the saved plan, cwd and objective digest. Every mutation invalidates that confirmation, and a
 *  start is not retryable until the detail reloads.
 *
 *  **Why a hook, owned by the mission body.** The header draws Begin and Plan again; the plan card
 *  draws the reason Begin is disabled, the confirmation it armed and the brief draft that decides
 *  whether it may be pressed. Those are ONE state. They used to live in `MissionPlanCard`, which then
 *  portalled its buttons into the header beside `MissionLifecycle`'s: two owners for one row, which is
 *  what spread the header over two lines (#967). Lifting the state here lets one component own the row
 *  (`MissionHeaderActions`) while the card keeps the words, and neither holds a copy of the other's
 *  truth. */
import { useCallback, useLayoutEffect, useMemo, useRef, useState } from "react";

import { ApiError, api } from "../../lib/api";
import { objectivesDigestInput, sha256Hex } from "../../lib/digest";
import type { Mission } from "../../types/api";

const PLANNABLE = new Set(["draft", "planned"]);

/** The reason sentence's element id. Begin points at it with `aria-describedby`, and it renders in the
 *  plan card, so a disabled Begin carries the same words a sighted operator reads beside the plan. */
export const START_REASON_ID = "mission-start-reason";

/** The armed launch's warning (which agent, which directory, unattended). While Begin is armed it
 *  describes Confirm begin, as the reason describes Begin otherwise (#967). */
export const LAUNCH_WARNING_ID = "mission-launch-warning";

/** The plan card's element id, so Review plan in the header's ⋯ can bring the editor into view. An id
 *  looked up when pressed, not a ref carried in this model: the model is read during render by two
 *  components, and a ref inside it would make each of those reads a ref read. One mission body is
 *  mounted at a time, so the id is unique. */
export const PLAN_CARD_ID = "mission-plan-card";

type Confirmation = {
  planId: string;
  cwd: string;
  digest: string;
  input: string;
};

export function useMissionStart(
  mission: Mission | null,
  {
    onChanged,
    onNote,
    onPlan,
    onObjectives,
  }: {
    onChanged: (opts?: { membershipChanged?: boolean }) => void;
    onNote: (msg: string) => void;
    /** Bring the plan into view (the thread, below 1400px). Absent in a standalone mount. */
    onPlan?: () => void;
    /** Open the editable Objectives section. */
    onObjectives?: () => void;
  },
) {
  const plan = mission?.plan ?? null;
  const [busy, setBusy] = useState<string | null>(null);
  const operation = useRef(false);
  const latestMission = useRef(mission);
  useLayoutEffect(() => {
    latestMission.current = mission;
  }, [mission]);
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
    () => mission?.objectives ?? [],
    [mission?.objectives],
  );
  const input = objectivesDigestInput(objectives);
  const hasSession = (mission?.sessions ?? []).some((s) => !s.removed_at);
  const draft = brief.trim();
  const unsaved = draft !== (plan?.brief ?? "");
  // `null === null` is not a read having landed: with no mission loaded there is nothing to wait for.
  const waiting = mission !== null && awaitingRead === mission;
  const disabled = busy !== null || waiting;
  const active =
    mission !== null &&
    mission.archived_at == null &&
    PLANNABLE.has(mission.state);
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

  // "Plan again" names the ⋯ item, which is where Re-plan moved (#967).
  const reason = hasSession
    ? !mission?.cwd
      ? "A resolved project folder is required before tracking this session."
      : "Begin tracks the attached session. No new agent will be started."
    : !plan
      ? "Use Plan again, under ⋯, to prepare a proposal before beginning."
      : !plan.project_id
        ? "Choose a project before beginning."
        : !plan.engine
          ? "Choose an agent before beginning."
          : !draft
            ? "Write and save the instructions before beginning."
            : unsaved
              ? "Save the instructions before beginning."
              : mission?.objectives_state === "pending"
                ? "Wait for the objectives before beginning."
                : objectives.length === 0
                  ? "Add at least one objective before beginning."
                  : "Begin starts a new agent with the saved plan. Review the launch before confirming.";
  const ready = hasSession
    ? Boolean(mission?.cwd)
    : Boolean(
        plan?.project_id &&
          plan.engine &&
          plan.brief?.trim() &&
          !unsaved &&
          mission?.objectives_state !== "pending" &&
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
      const editor = document.getElementById(PLAN_CARD_ID);
      editor?.scrollIntoView({ block: "nearest" });
      const target = !plan?.project_id
        ? "#plan-project"
        : !plan.engine
          ? "#plan-engine"
          : "#plan-brief";
      editor?.querySelector<HTMLElement>(target)?.focus();
    });
  };

  const propose = () => {
    if (!mission) return;
    void run("plan", () => api.planMission(mission.id));
  };
  const edit = (body: {
    project_id?: string | null;
    engine?: string;
    brief?: string;
  }) => {
    if (!mission || !plan || disabled) return;
    void run("edit", () => api.editMissionPlan(mission.id, plan.plan_id, body));
  };
  const changeBrief = (value: string) => {
    revision.current += 1;
    setConfirming(null);
    setBrief(value);
  };
  const cancelConfirm = () => {
    revision.current += 1;
    setConfirming(null);
  };
  const begin = async () => {
    if (!mission || !active || disabled || operation.current || !ready) return;
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

  const beginLabel =
    busy === "begin" || mission?.state === "dispatching"
      ? "Starting…"
      : validConfirmation
        ? "Confirm begin"
        : "Begin";

  return {
    plan,
    objectives,
    hasSession,
    active,
    ready,
    busy,
    disabled,
    waiting,
    error,
    reason,
    brief,
    draft,
    unsaved,
    validConfirmation,
    beginLabel,
    beginDisabled: !active || disabled || !ready,
    /** Links that bring the plan into view exist only where the body can show it. */
    canReviewPlan: Boolean(onPlan),
    onObjectives,
    begin,
    propose,
    edit,
    changeBrief,
    cancelConfirm,
    reviewPlan,
    refresh: () => onChanged(),
  };
}

export type MissionStart = ReturnType<typeof useMissionStart>;
