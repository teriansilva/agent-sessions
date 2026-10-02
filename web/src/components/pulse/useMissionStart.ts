/** The start model: Begin, Plan again, Plan manually and the launch confirmation, shared by the header
 *  and the plan card (#944, #967).
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
 *  truth.
 *
 *  **A new mission plans itself (#967 P2).** So "is there a plan" is no longer the only question; the
 *  mission also says how planning STANDS (`plan_state`). This model turns the two into the card's three
 *  states (`PlanPhase`) and into Begin's gate:
 *
 *  - `planning` while an attempt is running. Begin is disabled, because DISPATCH refuses while
 *    `plan_state` is `pending`; a control that can only 409 is not offered as pressable.
 *  - `ready` once a plan exists. Begin needs the mission to be `planned` and the plan and its
 *    objectives to be complete, as before.
 *  - `unplanned` when there is no plan and nothing is running (`failed` or `skipped`). Plan manually
 *    saves a first plan through the same route an edit uses, without a `plan_id`.
 */
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

/** Which of the plan card's states a mission is in (#967 P2b). */
export type PlanPhase = "planning" | "ready" | "unplanned";

type PlanState = "pending" | "ready" | "failed" | "skipped";

/** The mission's planning state, read the way the server backfilled it.
 *
 *  The server always sends `plan_state` now. A row without one predates the column, and the v25
 *  upgrade answered that question once: `ready` if a plan was stored, `skipped` otherwise. Reading an
 *  absent field by the same rule keeps such a row, and a fixture written before the field existed,
 *  meaning what the server would have made of it, instead of inventing a fourth state. */
export function planStateOf(mission: Mission): PlanState {
  return mission.plan_state ?? (mission.plan ? "ready" : "skipped");
}

/** What the card says about a mission with no plan and no attempt running. */
export function unplannedLead(mission: Mission): string {
  const state = planStateOf(mission);
  const detail = mission.plan_detail?.trim();
  if (state === "failed")
    return `Couldn't plan: ${detail || "the planner did not produce a plan"}`;
  // `skipped` is settled for one reason only, an unconfigured endpoint, and says so in its detail. A
  // backfilled `skipped` has no detail: that mission was never planned automatically at all, so it
  // is not told it failed to be.
  if (state === "skipped" && detail)
    return "Couldn't plan: no AI endpoint is configured";
  return "No plan yet";
}

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
  /** The plan changed identity in THIS render, and the brief draft below still holds the previous
   *  plan's text until React re-renders with the sync applied. Anything derived from the draft is
   *  stale for this one pass, and nothing may latch on it. */
  const syncingPlan = planId !== plan?.plan_id;
  if (syncingPlan) {
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

  /* HOW PLANNING STANDS (#967 P2b). */
  const planState = mission ? planStateOf(mission) : null;
  const planPending = planState === "pending";
  const phase: PlanPhase = planPending
    ? "planning"
    : plan
      ? "ready"
      : "unplanned";
  const lead = mission && phase === "unplanned" ? unplannedLead(mission) : "";
  /** A Plan again that did not produce a plan leaves the previous one stored, and DISPATCH accepts it
   *  (`test_DISPATCH_is_ADMITTED_again_once_the_attempt_SETTLES`). The card says which plan it is
   *  showing rather than presenting an old proposal as the new one. */
  const detail = mission?.plan_detail?.trim();
  const keptNote =
    plan && planState === "skipped"
      ? "Plan again could not run: no AI endpoint is configured. This is the previous plan."
      : plan && planState === "failed"
        ? `Plan again did not finish${detail ? ` (${detail})` : ""}. This is the previous plan.`
        : null;
  /** Everything a launch needs is on the plan and saved. What decides whether the fields start
   *  folded: a complete plan is one line with Review plan (mockup B3), an incomplete one is open. */
  const planComplete = Boolean(
    plan?.project_id && plan.engine && plan.brief?.trim() && draft && !unsaved,
  );
  /** The editable fields are shown. Folded by default for a complete plan, and OPEN for one that
   *  needs something. Once open it stays open: finishing the last field must not fold the fields out
   *  from under the operator who is typing in them. Set during render, like `planId` above. */
  const [reviewing, setReviewing] = useState(false);
  if (!syncingPlan && phase === "ready" && !planComplete && !reviewing)
    setReviewing(true);

  const reason = hasSession
    ? !mission?.cwd
      ? "A resolved project folder is required before tracking this session."
      : "Begin tracks the attached session. No new agent will be started."
    : planPending
      ? "A plan is still being prepared."
      : !plan
        ? lead
        : !plan.project_id
          ? "Choose a project before beginning."
          : !plan.engine
            ? "Choose an agent before beginning."
            : !draft
              ? "Write and save the instructions before beginning."
              : unsaved
                ? "Save the instructions before beginning."
                : mission?.state !== "planned"
                  ? "The mission is not planned yet."
                  : mission?.objectives_state === "pending"
                    ? "Wait for the objectives before beginning."
                    : objectives.length === 0
                      ? "Add at least one objective before beginning."
                      : "Begin starts a new agent with the saved plan. Review the launch before confirming.";
  // A LAUNCH needs a `planned` mission with nothing being planned: `claim_plan` moves only
  // `planned -> dispatching`, and refuses while `plan_state` is `pending`. Tracking an attached
  // session is a different transition (`draft -> planned -> running`), which is why a draft that
  // holds a session may still Begin.
  const ready = hasSession
    ? Boolean(mission?.cwd)
    : Boolean(
        !planPending &&
          mission?.state === "planned" &&
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
        // The server's own `detail`: a 422 on a manual plan names the field it refused.
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
    setReviewing(true);
    onPlan?.();
    requestAnimationFrame(() => {
      const editor = document.getElementById(PLAN_CARD_ID);
      // Optional-called, as the armed confirmation's is: not every DOM implements it.
      editor?.scrollIntoView?.({ block: "nearest" });
      const target = !plan?.project_id
        ? "#plan-project"
        : !plan.engine
          ? "#plan-engine"
          : "#plan-brief";
      editor?.querySelector<HTMLElement>(target)?.focus();
    });
  };
  /** Fold a complete plan back to its one line. Not offered for an incomplete one. */
  const hidePlan = () => setReviewing(false);

  const propose = () => {
    if (!mission || planPending) return;
    void run("plan", async () => {
      await api.planMission(mission.id);
      // The operator asked for a new proposal to review, so it arrives open.
      setReviewing(true);
    });
  };
  const edit = (body: {
    project_id?: string | null;
    engine?: string;
    brief?: string;
  }) => {
    if (!mission || !plan || disabled) return;
    void run("edit", () => api.editMissionPlan(mission.id, plan.plan_id, body));
  };
  /** PLAN MANUALLY (#967 P2b): the first plan, with no `plan_id`, for a mission nothing is planning. */
  const planManually = (body: {
    project_id: string;
    engine: string;
    brief: string;
  }) => {
    if (!mission || plan || planPending || disabled) return;
    void run("manual", () => api.firstMissionPlan(mission.id, body));
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
    /** How planning stands, and the card state it maps to (#967 P2b). */
    planState,
    planPending,
    phase,
    /** The card's sentence for `unplanned`: "Couldn't plan: …" or "No plan yet". */
    unplannedLead: lead,
    keptNote,
    planComplete,
    /** The editable plan fields are on screen. */
    fieldsOpen: phase === "ready" && reviewing,
    /** Links that bring the plan into view exist only where the body can show it. */
    canReviewPlan: Boolean(onPlan),
    onObjectives,
    begin,
    propose,
    edit,
    planManually,
    changeBrief,
    cancelConfirm,
    reviewPlan,
    hidePlan,
    refresh: () => onChanged(),
  };
}

export type MissionStart = ReturnType<typeof useMissionStart>;
