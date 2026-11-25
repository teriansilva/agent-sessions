/** The start model: Begin and the launch confirmation, shared by the header and the plan card (#944,
 *  #967, #2152).
 *
 *  Begin tracks an attached session first; only a mission without one may launch. Launch confirmation
 *  binds the saved plan, cwd and objective digest. Every mutation invalidates that confirmation, and a
 *  start is not retryable until the detail reloads.
 *
 *  **Why a hook, owned by the mission body.** The header draws Begin; the plan card draws the reason
 *  Begin is disabled and the confirmation it armed. Those are ONE state, so one model owns it and
 *  neither component holds a copy of the other's truth (#967).
 *
 *  **HQ plans every mission (#2152).** The one-agent planner is retired: no Plan again, no Plan
 *  manually, no agent picker, no instructions box. What is left here is the LEGACY plan (D2): a
 *  mission that already holds a one-agent plan keeps it, read-only, with its Begin, until it is
 *  finished or archived. The card's state (`PlanPhase`):
 *
 *  - `ready` — a legacy one-agent plan: read-only, and Begin launches it as before.
 *  - `hq` — HQ planned it (a plan revision exists): the Team, Plan approval and Steps cards carry
 *    it; this model offers no Begin for it.
 *  - `unplanned` — no plan of either kind: the card offers Plan with HQ.
 */
import { useCallback, useLayoutEffect, useMemo, useRef, useState } from "react";
import { openSessionLimit, SESSION_LIMIT_REFUSAL } from "../../lib/sessionLimit";

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

/** Which of the plan card's states a mission is in (#967 P2b, #2152). */
export type PlanPhase = "ready" | "hq" | "unplanned";

/** What the card says about a mission with no plan of either kind (#2152). */
export const UNPLANNED_LEAD = "This mission has no plan";

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
  const [planId, setPlanId] = useState(plan?.plan_id);
  const [error, setError] = useState<string | null>(null);
  const [awaitingRead, setAwaitingRead] = useState<Mission | null>(null);
  /** The plan changed identity in THIS render: an armed launch belongs to the previous one. */
  if (planId !== plan?.plan_id) {
    setPlanId(plan?.plan_id);
    setConfirming(null);
  }
  const objectives = useMemo(
    () => mission?.objectives ?? [],
    [mission?.objectives],
  );
  const input = objectivesDigestInput(objectives);
  const hasSession = (mission?.sessions ?? []).some((s) => !s.removed_at);
  /** A legacy plan is read-only (#2152, D2): what is saved is what Begin launches. */
  const brief = (plan?.brief ?? "").trim();
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
    confirming.planId === plan?.plan_id &&
    confirming.cwd === (plan?.cwd ?? "") &&
    confirming.input === input;
  if (confirming && !validConfirmation) {
    setConfirming(null);
    setError("The plan or checklist changed. Review it and begin again.");
  }

  /* WHAT KIND OF PLAN IT HAS (#2152). `plan_revision` absent means the server could not read it:
     not known to be unplanned, so Plan with HQ is not offered on a guess. */
  const phase: PlanPhase = plan
    ? "ready"
    : mission?.plan_revision != null
      ? "hq"
      : "unplanned";
  const revisionKnown = mission?.plan_revision !== undefined;
  /** Plan with HQ may be offered: a draft, known to have no plan of either kind (D4: the server
   *  checks the same again, at staging, at the tap and in the store's transaction). */
  const canPlanWithHq =
    active && !hasSession && phase === "unplanned" && revisionKnown && mission?.state === "draft";
  /** A legacy plan whose saved fields can launch. */
  const planComplete = Boolean(plan?.project_id && plan.engine && brief);

  const reason = hasSession
    ? !mission?.cwd
      ? "A resolved project folder is required before tracking this session."
      : "Begin tracks the attached session. No new agent will be started."
    : !plan
      ? phase === "hq"
        ? "HQ planned this mission: approve the plan below."
        : UNPLANNED_LEAD
      : !plan.project_id
        ? "This plan has no project, so it can't begin. Ask HQ to plan the mission instead."
        : !plan.engine
          ? "This plan has no agent, so it can't begin. Ask HQ to plan the mission instead."
          : !brief
            ? "This plan has no instructions, so it can't begin."
            : mission?.state !== "planned"
              ? "The mission is not planned yet."
              : mission?.objectives_state === "pending"
                ? "Wait for the objectives before beginning."
                : objectives.length === 0
                  ? "Add at least one objective before beginning."
                  : "Begin starts a new agent with the saved plan. Review the launch before confirming.";
  // A LAUNCH needs a `planned` mission with nothing being planned: `claim_plan` moves only
  // `planned -> dispatching`. Tracking an attached session is a different transition
  // (`draft -> planned -> running`), which is why a draft that holds a session may still Begin.
  const ready = hasSession
    ? Boolean(mission?.cwd)
    : Boolean(
        mission?.plan_state !== "pending" &&
          mission?.state === "planned" &&
          planComplete &&
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

  /** Bring the plan card into view (the header's ⋯ → Review plan). */
  const reviewPlan = () => {
    onPlan?.();
    requestAnimationFrame(() => {
      // Optional-called, as the armed confirmation's is: not every DOM implements it.
      document.getElementById(PLAN_CARD_ID)?.scrollIntoView?.({ block: "nearest" });
    });
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
            if (out.refusal === SESSION_LIMIT_REFUSAL) openSessionLimit();
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
    validConfirmation,
    beginLabel,
    beginDisabled: !active || disabled || !ready,
    /** Begin belongs in the header: a legacy plan to launch, an attached session to track, or a
     *  launch already under way. An HQ-planned or unplanned mission has none (#2152). */
    offersBegin: Boolean(plan) || hasSession || mission?.state === "dispatching",
    /** What kind of plan it has (#2152). */
    phase,
    canPlanWithHq,
    planComplete,
    /** Links that bring the plan into view exist only where the body can show it. */
    canReviewPlan: Boolean(onPlan),
    onObjectives,
    begin,
    cancelConfirm,
    reviewPlan,
    refresh: () => onChanged(),
  };
}

export type MissionStart = ReturnType<typeof useMissionStart>;
