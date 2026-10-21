/** The proposal card, and the two properties that make it safe to press (#893 Phase 4).
 *
 *  1. **Nothing launches from the plan.** PLAN spends a model call and writes a row; DISPATCH is
 *     a different button, and it confirms first — it starts an agent with nobody watching it, in
 *     a real directory. NOT permission-bypassed: `mission_dispatch` passes `bypass=False`, because
 *     unattended bypass is a separate grant from the interactive one and stays approval-gated
 *     (#904 review 17). The confirmation says what it does, so it must not say more.
 *  2. **Every edit names the plan it edited.** The route replaces the whole row, so an edit
 *     without a comparand lets two tabs overwrite each other's acknowledged changes (#904
 *     review 6). The `plan_id` on the wire is asserted, not the rendering.
 */
import { act, cleanup, render, screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { beforeEach, expect, test, vi } from "vitest";

import { ApiError, api } from "../../lib/api";
import { objectivesDigestInput, sha256Hex } from "../../lib/digest";
import type { Mission, MissionPlan } from "../../types/api";

import { MissionHeaderActions } from "./MissionHeaderActions";
import { MissionPlanCard } from "./MissionPlanCard";
import { useMissionStart } from "./useMissionStart";

vi.mock("../../lib/api", async () => {
  const actual =
    await vi.importActual<typeof import("../../lib/api")>("../../lib/api");
  return {
    ...actual,
    api: {
      setMissionState: vi.fn(),
      planMission: vi.fn(),
      editMissionPlan: vi.fn(),
      firstMissionPlan: vi.fn(),
      dispatchMission: vi.fn(),
      patchMissionObjectives: vi.fn(),
      archiveMission: vi.fn(),
      unarchiveMission: vi.fn(),
    },
  };
});

function plan(over: Partial<MissionPlan> = {}): MissionPlan {
  return {
    plan_id: "pln_1",
    mission_id: "msn_1",
    project_id: "p1",
    cwd: "/repo/the-app",
    engine: "claude",
    engine_reason: "it is a python repo",
    brief: "open a PR that does the thing",
    created_at: 1,
    project_options: [
      { id: "p1", name: "the-app", cwd: "/repo/the-app" },
      { id: "p2", name: "the-docs", cwd: "/repo/the-docs" },
    ],
    engine_options: [
      { id: "claude", label: "claude" },
      { id: "codex", label: "codex" },
    ],
    ...over,
  };
}

function mission(over: Partial<Mission> = {}): Mission {
  return {
    id: "msn_1",
    title: "t",
    instruction: null,
    brief: null,
    project_id: null,
    cwd: null,
    engine: null,
    engine_source: null,
    state: "draft",
    playbook_id: null,
    created_at: 1,
    updated_at: 1,
    closed_at: null,
    archived_at: null,
    archiving_at: null,
    unarchiving_at: null,
    outcome: null,
    sessions: [],
    // A DISPATCHABLE MISSION KNOWS WHAT FINISHING MEANS (#904 review 4, finding 3). The default
    // fixture carries a settled checklist because that is a precondition of the launch, not
    // decoration — a fixture with none was how the card came to enable an empty-list dispatch.
    objectives: [{ key: "pr", title: "A PR is open", gate: true }],
    objectives_state: "done",
    ...over,
  } as Mission;
}

/** The card as the console mounts it (#967): beside the header, both reading ONE start model. Begin
 *  and Plan again are the header's; the reason, the confirmation and the brief are the card's. A test
 *  of either half alone would be a test of a state split the product no longer has. */
function Harness({
  mission: m,
  onChanged,
  onNote,
  onObjectives,
  onPlan,
}: {
  mission: Mission;
  onChanged: () => void;
  onNote: (msg: string) => void;
  onObjectives?: () => void;
  onPlan?: () => void;
}) {
  const start = useMissionStart(m, {
    onChanged,
    onNote,
    onObjectives,
    onPlan,
  });
  return (
    <>
      <MissionHeaderActions
        mission={m}
        start={start}
        onChanged={onChanged}
        onNote={onNote}
      />
      <MissionPlanCard mission={m} start={start} />
    </>
  );
}

function mount(m: Mission, onChanged = vi.fn(), onNote = vi.fn()) {
  render(<Harness mission={m} onChanged={onChanged} onNote={onNote} />);
  return { onChanged, onNote };
}

/** A COMPLETE plan starts folded to its one line (#967 P2b): the fields are behind Review plan. The
 *  tests below that edit a complete plan open it first and keep their assertions. */
async function openReview() {
  await userEvent.click(screen.getByTestId("mission-plan-review"));
}

/** Plan again lives behind the header's ⋯ (#967). */
async function openOverflow() {
  const t = screen.getByTestId("mission-overflow");
  if (t.getAttribute("aria-expanded") !== "true") await userEvent.click(t);
}

beforeEach(() => {
  vi.mocked(api.planMission)
    .mockReset()
    .mockResolvedValue(plan() as never);
  vi.mocked(api.editMissionPlan)
    .mockReset()
    .mockResolvedValue(plan() as never);
  vi.mocked(api.firstMissionPlan)
    .mockReset()
    .mockResolvedValue(plan() as never);
  vi.mocked(api.patchMissionObjectives)
    .mockReset()
    .mockResolvedValue({ objectives: [] } as never);
  vi.mocked(api.dispatchMission).mockReset().mockResolvedValue({
    state: "running",
    reason: "",
    session_key: "claude:a",
  });
});

test("an unplanned mission offers PLAN, and planning launches nothing", async () => {
  const { onChanged } = mount(mission());
  await openOverflow();
  await userEvent.click(screen.getByTestId("mission-replan"));
  await waitFor(() => expect(api.planMission).toHaveBeenCalledWith("msn_1"));
  // THE SEPARATION IS THE FEATURE: a proposal, and no launch.
  expect(api.dispatchMission).not.toHaveBeenCalled();
  expect(onChanged).toHaveBeenCalled();
});

test("a mission that has left the planning states offers nothing at all", () => {
  mount(mission({ state: "running" }));
  expect(screen.queryByTestId("mission-plan-card")).toBeNull();
});

test("…including one that still CARRIES a plan (#904 review 17)", () => {
  // The gate lived inside the `!plan` branch, so this — the ordinary case, since the plan row
  // outlives the states allowed to act on it — rendered the full card: EDIT and DISPATCH that
  // the server answers with a 409. The test above passed the whole time because it mounted a
  // mission with NO plan, which is the other branch entirely.
  for (const state of ["running", "done", "failed"] as const) {
    cleanup();
    mount(mission({ state, plan: plan() }));
    expect(screen.queryByTestId("mission-plan-card")).toBeNull();
    expect(screen.queryByTestId("mission-begin")).toBeNull();
  }
});

test("DISPATCH confirms, and says what it starts and where", async () => {
  mount(mission({ state: "planned", plan: plan() }));
  await userEvent.click(screen.getByTestId("mission-begin"));
  // The first tap starts nothing. The confirmation arms after an ASYNC digest (`sha256Hex`), so the
  // click can resolve before it renders on a loaded runner: wait for it, never read it synchronously.
  const confirm = await screen.findByTestId("mission-dispatch-confirm");
  expect(api.dispatchMission).not.toHaveBeenCalled();
  expect(confirm).toHaveTextContent("/repo/the-app");
  expect(confirm).toHaveTextContent("unattended");
  await userEvent.click(screen.getByTestId("mission-begin"));
  await waitFor(() =>
    // …AND THE DIRECTORY IT CONFIRMED, as a comparand. The server re-resolves the project and
    // refuses if what it resolves is not what the operator approved (#904 review 2, finding 6).
    // …AND THE CHECKLIST IT SHOWED, digested. Both are comparands the server re-computes: the
    // client can have its request refused, never choose what runs (#904 review 3, findings 5-6).
    expect(api.dispatchMission).toHaveBeenCalledWith(
      "msn_1",
      "pln_1",
      "/repo/the-app",
      expect.any(String),
    ),
  );
});

test("an EDIT names the plan it edited, and a project id rather than a path", async () => {
  mount(mission({ state: "planned", plan: plan() }));
  await openReview();
  await userEvent.selectOptions(
    screen.getByTestId("mission-plan-project"),
    "p2",
  );
  await waitFor(() => expect(api.editMissionPlan).toHaveBeenCalled());
  // THE COMPARAND, on the wire. Without it two tabs silently overwrite each other (#904 rev 6).
  expect(api.editMissionPlan).toHaveBeenCalledWith("msn_1", "pln_1", {
    project_id: "p2",
  });
  // …and never a path: `cwd` is not a field the client can send.
  const body = vi.mocked(api.editMissionPlan).mock.calls[0][2];
  expect(Object.keys(body)).not.toContain("cwd");
});

test("a plan that MOVED underneath the card re-reads instead of retrying", async () => {
  vi.mocked(api.editMissionPlan).mockRejectedValue(
    new ApiError(
      409,
      "the plan changed while you were editing it; read it again",
    ),
  );
  const { onChanged } = mount(mission({ state: "planned", plan: plan() }));
  await openReview();
  await userEvent.selectOptions(
    screen.getByTestId("mission-plan-engine"),
    "codex",
  );
  await waitFor(() =>
    expect(screen.getByTestId("mission-plan-error")).toHaveTextContent(
      "read it again",
    ),
  );
  // The re-read is the whole remedy: what is on screen is not what the server holds.
  expect(onChanged).toHaveBeenCalled();
  expect(api.editMissionPlan).toHaveBeenCalledTimes(1);
});

test("a dispatch that did not reach `running` reports the server's own reason", async () => {
  vi.mocked(api.dispatchMission).mockResolvedValue({
    state: "failed",
    reason: "the session never started: no store record",
    session_key: "claude:a",
  });
  const { onNote } = mount(mission({ state: "planned", plan: plan() }));
  await userEvent.click(screen.getByTestId("mission-begin"));
  // Armed first (async digest): a second tap that lands before the arm only arms again.
  await screen.findByTestId("mission-dispatch-confirm");
  await userEvent.click(screen.getByTestId("mission-begin"));
  await waitFor(() =>
    expect(onNote).toHaveBeenCalledWith(
      "the session never started: no store record",
    ),
  );
});

test("a suggestion with no stated reason says so rather than looking chosen", async () => {
  mount(mission({ state: "planned", plan: plan({ engine_reason: "" }) }));
  await openReview();
  expect(screen.getByTestId("mission-plan-reason")).toHaveTextContent(
    "you chose this",
  );
});

test("a plan the model could not complete cannot be dispatched, and says why", () => {
  mount(
    mission({
      state: "planned",
      plan: plan({ project_id: null, cwd: null, dropped: ["project"] }),
    }),
  );
  expect(screen.getByTestId("mission-begin")).toBeDisabled();
  expect(screen.getByTestId("mission-plan-no-project")).toHaveTextContent(
    "the model did not give one",
  );
});

test("a CLEARED brief disables DISPATCH rather than sending the old one", async () => {
  // #904 review 2, finding 5. `ready` came from the PERSISTED brief while the textarea rendered
  // the local draft. Clearing the box skips the blur save — an empty brief is not a save, it is
  // a deletion the server refuses — so the button stayed enabled on the old value, and two taps
  // ran text the operator believed they had removed.
  //
  // Red against a `ready` computed from the stored brief alone.
  mount(mission({ state: "planned", plan: plan() }));
  await openReview();
  const box = screen.getByTestId("mission-plan-brief");
  expect(screen.getByTestId("mission-begin")).not.toBeDisabled();

  await userEvent.clear(box);
  await userEvent.tab(); // blur: an empty brief is not saved

  expect(screen.getByTestId("mission-begin")).toBeDisabled();
  expect(screen.getByTestId("mission-plan-unsaved")).toHaveTextContent(
    /A brief is required/i,
  );
  expect(api.editMissionPlan).not.toHaveBeenCalled();
  expect(api.dispatchMission).not.toHaveBeenCalled();
});

test("an UNSAVED edit disables DISPATCH and says why", async () => {
  // The general form: DISPATCH runs what is STORED, so anything else on screen is a different
  // brief from the one that would be sent.
  mount(mission({ state: "planned", plan: plan() }));
  await openReview();
  await userEvent.type(screen.getByTestId("mission-plan-brief"), " and more");
  expect(screen.getByTestId("mission-begin")).toBeDisabled();
  expect(screen.getByTestId("mission-plan-unsaved")).toHaveTextContent(
    /have not been saved/i,
  );
});

test("the card says WHAT DONE MEANS, and says so when there is nothing", async () => {
  // #904 review 2, finding 8. On a phone the objectives are a separate stop, so without this an
  // operator could start an unattended agent having never seen — or noticed the absence of — the
  // checklist the supervisor will chase.
  mount(
    mission({
      state: "planned",
      plan: plan(),
      objectives: [
        { key: "pr", title: "A PR is open", gate: true },
        { key: "green", title: "Checks are green", gate: false },
      ] as never,
    }),
  );
  await openReview();
  const list = screen.getByTestId("mission-plan-objectives");
  expect(list).toHaveTextContent("2 objectives define what done means");

  cleanup();
  mount(mission({ state: "planned", plan: plan(), objectives: [] }));
  expect(screen.getByTestId("mission-plan-no-objectives")).toHaveTextContent(
    /0 objectives/i,
  );
});

test("an empty checklist links to the editable Objectives section", async () => {
  const onObjectives = vi.fn();
  render(
    <Harness
      mission={mission({
        state: "planned",
        plan: plan(),
        objectives: [],
        objectives_state: "skipped",
      })}
      onChanged={vi.fn()}
      onNote={vi.fn()}
      onObjectives={onObjectives}
    />,
  );
  expect(screen.getByTestId("mission-begin")).toBeDisabled();
  await userEvent.click(
    screen.getAllByRole("button", { name: "Add objectives" })[0],
  );
  expect(onObjectives).toHaveBeenCalledOnce();
});

test("an EMPTY checklist disables DISPATCH rather than warning beside it", async () => {
  // #904 review 4, finding 3. The card used to enable the launch next to a paragraph explaining
  // that the agent would "run with nothing to check it against" — a warning where #893's
  // acceptance invariant needs a gate. Production settling `skipped` (no AI endpoint) or `failed`
  // is the ordinary way to arrive here, so `objectives_state` alone is not the question: the
  // question is whether a checklist EXISTS.
  for (const state of ["done", "skipped", "failed"] as const) {
    mount(
      mission({
        state: "planned",
        plan: plan(),
        objectives: [],
        objectives_state: state,
      } as never),
    );
    const btn = screen.getByTestId("mission-begin");
    expect(btn).toBeDisabled();
    expect(screen.getByRole("status")).toHaveTextContent(
      /add at least one objective/i,
    );
    cleanup();
  }

  // …and a hand-written checklist on a mission whose production was SKIPPED dispatches. Keying
  // the gate on the producer's verdict instead would make DISPATCH unreachable on an install
  // with no AI endpoint configured, which is not the invariant #893 asked for.
  mount(
    mission({
      state: "planned",
      plan: plan(),
      objectives: [{ key: "pr", title: "A PR is open", gate: true }],
      objectives_state: "skipped",
    } as never),
  );
  expect(screen.getByTestId("mission-begin")).not.toBeDisabled();
});

test("DISPATCH waits for the objectives to be worked out", async () => {
  // #904 review 3, finding 5. A new mission starts with `objectives_state: "pending"` — the
  // producer is a background task — so a fast operator could launch before the mission knew what
  // finishing means, and the supervisor would then follow through against a checklist that
  // arrived afterwards.
  mount(
    mission({
      state: "planned",
      plan: plan(),
      objectives: [],
      objectives_state: "pending",
    } as never),
  );
  expect(screen.getByTestId("mission-begin")).toBeDisabled();
  expect(screen.getByTestId("mission-plan-no-objectives")).toHaveTextContent(
    /Preparing objectives/i,
  );

  cleanup();
  // …and once they have landed it is pressable again.
  mount(
    mission({
      state: "planned",
      plan: plan(),
      objectives: [{ key: "pr", title: "A PR is open", gate: true }],
      objectives_state: "done",
    } as never),
  );
  expect(screen.getByTestId("mission-begin")).not.toBeDisabled();
});

test("the SECOND tap cannot approve a checklist the FIRST tap never saw", async () => {
  // #904 review 9, finding 3. `confirming` was a boolean while the digest was recomputed from
  // live props, so a poll landing between the two taps left the button reading CONFIRM DISPATCH
  // and sent the NEW checklist's digest — the server's compare-and-set then passed for a set the
  // operator had never confirmed, which is the one thing that comparand exists to stop.
  //
  // Red against a boolean `confirming`: the second tap goes through and carries `b`'s digest.
  const withObjectives = (title: string) =>
    mission({
      state: "planned",
      plan: plan(),
      objectives: [
        { key: "pr", title, gate: true, met: false } as never,
      ] as never,
    });
  const onNote = vi.fn();
  const { rerender } = render(
    <Harness
      mission={withObjectives("A PR is open")}
      onChanged={vi.fn()}
      onNote={onNote}
    />,
  );

  await userEvent.click(screen.getByTestId("mission-begin"));
  // The arm lands after an ASYNC digest (`sha256Hex`), so the click can resolve first.
  await waitFor(() =>
    expect(screen.getByTestId("mission-begin")).toHaveTextContent(
      "Confirm begin",
    ),
  );

  // THE CHECKLIST CHANGES UNDER THE ARMED BUTTON — a poll, or another tab retitling it. The plan
  // is untouched, so the card is NOT remounted by its `plan_id` key.
  rerender(
    <Harness
      mission={withObjectives("A PR is open AND approved")}
      onChanged={vi.fn()}
      onNote={onNote}
    />,
  );

  // THE ARM IS RELEASED, and the operator is told why rather than left holding a tap that would
  // be refused.
  await waitFor(() =>
    expect(screen.getByTestId("mission-begin")).toHaveTextContent(/^Begin$/),
  );
  expect(screen.getByRole("alert")).toHaveTextContent(/checklist changed/i);

  // …and a tap now re-arms against what is on screen rather than dispatching.
  await userEvent.click(screen.getByTestId("mission-begin"));
  expect(api.dispatchMission).not.toHaveBeenCalled();
});

test("the digest DISPATCH sends is the one the confirmation was armed on", async () => {
  // The other half of finding 3, on the wire: even where the arm survives, the comparand must
  // name the set the first tap approved rather than whatever the props say at send time.
  const first = mission({
    state: "planned",
    plan: plan(),
    objectives: [
      { key: "pr", title: "A PR is open", gate: true, met: false } as never,
    ] as never,
  });
  const { rerender } = render(
    <Harness mission={first} onChanged={vi.fn()} onNote={vi.fn()} />,
  );
  await userEvent.click(screen.getByTestId("mission-begin"));
  // The arm has to have LANDED before the rerender below, or this tests the rerender, not the arm.
  await screen.findByTestId("mission-dispatch-confirm");

  // An objective becoming MET is progress, not a different checklist — the digest covers key,
  // title and gate only — so the arm survives this and the send must still happen.
  rerender(
    <Harness
      mission={{
        ...first,
        objectives: [
          { key: "pr", title: "A PR is open", gate: true, met: true } as never,
        ] as never,
      }}
      onChanged={vi.fn()}
      onNote={vi.fn()}
    />,
  );
  await userEvent.click(screen.getByTestId("mission-begin"));
  await waitFor(() => expect(api.dispatchMission).toHaveBeenCalled());
  // THE DIGEST OF WHAT WAS ON SCREEN WHEN IT WAS ARMED, computed through the SHARED encoder
  // rather than a hand-copied string — a second copy of the rule here is exactly the drift
  // `tests/fixtures/objectives_digest_cases.json` exists to stop. Not a shape assertion
  // either: a length check passes for any digest, including the wrong one.
  const expected = await sha256Hex(
    objectivesDigestInput([{ key: "pr", title: "A PR is open", gate: true }]),
  );
  expect(vi.mocked(api.dispatchMission).mock.calls[0][3]).toBe(expected);
});

for (const state of ["draft", "planned"] as const) {
  test(`Begin tracks a ${state} attached session without a proposal`, async () => {
    vi.mocked(api.setMissionState)
      .mockReset()
      .mockResolvedValue({} as never);
    mount(
      mission({
        state,
        cwd: "/repo",
        sessions: [{ session_key: "claude:a", removed_at: null }] as never,
      }),
    );
    await userEvent.click(screen.getByTestId("mission-begin"));
    await waitFor(() =>
      expect(api.setMissionState).toHaveBeenLastCalledWith("msn_1", {
        from: "planned",
        to: "running",
      }),
    );
    expect(api.setMissionState).toHaveBeenCalledTimes(
      state === "draft" ? 2 : 1,
    );
    expect(api.dispatchMission).not.toHaveBeenCalled();
  });
}

test("refused tracking stays visible, waits for reload and never launches", async () => {
  vi.mocked(api.setMissionState)
    .mockReset()
    .mockRejectedValue(new ApiError(409, "attached session is no longer live"));
  const original = mission({
    state: "planned",
    cwd: "/repo",
    sessions: [{ session_key: "claude:a", removed_at: null }] as never,
  });
  const { rerender } = render(
    <Harness mission={original} onChanged={vi.fn()} onNote={vi.fn()} />,
  );
  await userEvent.click(screen.getByTestId("mission-begin"));
  await waitFor(() =>
    expect(screen.getByRole("alert")).toHaveTextContent("no longer live"),
  );
  expect(screen.getByTestId("mission-begin")).toBeDisabled();
  expect(api.dispatchMission).not.toHaveBeenCalled();
  rerender(
    <Harness
      mission={{ ...original }}
      onChanged={vi.fn()}
      onNote={vi.fn()}
    />,
  );
  expect(screen.getByTestId("mission-begin")).toBeEnabled();
});

test("a detached-only roster uses the launch path, never tracking", async () => {
  vi.mocked(api.setMissionState).mockReset();
  mount(
    mission({
      state: "planned",
      plan: plan(),
      sessions: [{ session_key: "claude:old", removed_at: 123 }] as never,
    }),
  );
  await userEvent.click(screen.getByTestId("mission-begin"));
  // Armed first (async digest): a second tap that lands before the arm only arms again.
  await screen.findByTestId("mission-dispatch-confirm");
  await userEvent.click(screen.getByTestId("mission-begin"));
  await waitFor(() => expect(api.dispatchMission).toHaveBeenCalledOnce());
  expect(api.setMissionState).not.toHaveBeenCalled();
});

test("a poll during Begin cannot count as its post-result refresh", async () => {
  let finish!: () => void;
  const held = new Promise<void>((resolve) => (finish = resolve));
  vi.mocked(api.dispatchMission).mockImplementation(async () => {
    await held;
    throw new ApiError(503, "connection lost");
  });
  const original = mission({ state: "planned", plan: plan() });
  const { rerender } = render(
    <Harness mission={original} onChanged={vi.fn()} onNote={vi.fn()} />,
  );
  await userEvent.click(screen.getByTestId("mission-begin"));
  // Armed first (async digest): a second tap that lands before the arm only arms again.
  await screen.findByTestId("mission-dispatch-confirm");
  await userEvent.click(screen.getByTestId("mission-begin"));
  const polled = { ...original };
  rerender(
    <Harness mission={polled} onChanged={vi.fn()} onNote={vi.fn()} />,
  );
  await act(async () => finish());
  expect(screen.getByRole("alert")).toHaveTextContent("connection lost");
  expect(screen.getByTestId("mission-begin")).toBeDisabled();
  rerender(
    <Harness
      mission={{ ...polled }}
      onChanged={vi.fn()}
      onNote={vi.fn()}
    />,
  );
  expect(screen.getByTestId("mission-begin")).toBeEnabled();
});

// ---- #967 P2b: the plan card's states ---------------------------------------------------------------

const OPTIONS = {
  project_options: [
    { id: "p1", name: "the-app", cwd: "/repo/the-app" },
    { id: "p2", name: "the-docs", cwd: "/repo/the-docs" },
  ],
  engine_options: [
    { id: "claude", label: "claude" },
    { id: "codex", label: "codex" },
  ],
};

test("PLANNING: a quiet line, no fields, and Begin disabled with that line as its reason", async () => {
  mount(
    mission({
      state: "draft",
      plan: null,
      plan_state: "pending",
      plan_generation: 1,
    }),
  );
  expect(screen.getByTestId("mission-plan-card")).toHaveAttribute(
    "data-plan-state",
    "planning",
  );
  expect(screen.getByTestId("mission-plan-lead")).toHaveTextContent(
    "Planning… A plan is still being prepared.",
  );
  expect(screen.queryByTestId("mission-plan-fields")).toBeNull();
  expect(screen.queryByTestId("mission-plan-manually")).toBeNull();
  const begin = screen.getByTestId("mission-begin");
  expect(begin).toBeDisabled();
  expect(begin).toHaveAccessibleDescription(/A plan is still being prepared/);
  // Planning is work in hand, not a state of its own: the chip says so on the neutral dot.
  // No ellipsis in the chip: it read as CSS truncation, and was cut off on a phone (#967 P2b review).
  expect(screen.getByTestId("mission-state")).toHaveTextContent(/^planning$/);
  // An attempt is running, so Plan again is held rather than offered as a 409.
  await openOverflow();
  expect(screen.getByTestId("mission-replan")).toBeDisabled();
  expect(screen.getByTestId("mission-replan")).toHaveTextContent("Planning…");
});

test("a Plan again in flight holds Begin over the PREVIOUS plan, which DISPATCH would refuse", () => {
  // Red against a gate on "a plan exists": the old plan stays stored while attempt 2 runs, and the
  // server answers DISPATCH with 409 until it settles.
  mount(
    mission({
      state: "planned",
      plan: plan(),
      plan_state: "pending",
      plan_generation: 2,
    }),
  );
  expect(screen.getByTestId("mission-begin")).toBeDisabled();
  expect(screen.getByTestId("mission-plan-card")).toHaveAttribute(
    "data-plan-state",
    "planning",
  );
  expect(screen.queryByTestId("mission-plan-project")).toBeNull();
});

test("READY: one line naming the agent, the project and the count; Review plan unfolds the fields", async () => {
  mount(mission({ state: "planned", plan: plan(), plan_state: "ready" }));
  expect(screen.getByTestId("mission-plan-card")).toHaveAttribute(
    "data-plan-state",
    "ready",
  );
  expect(screen.getByTestId("mission-plan-lead")).toHaveTextContent(
    "Plan ready — claude in the-app · 1 objective",
  );
  expect(screen.queryByTestId("mission-plan-brief")).toBeNull();
  expect(screen.getByTestId("mission-begin")).toBeEnabled();

  const review = screen.getByTestId("mission-plan-review");
  expect(review).toHaveAttribute("aria-expanded", "false");
  await userEvent.click(review);
  expect(review).toHaveAttribute("aria-expanded", "true");
  expect(review).toHaveTextContent("Hide plan");
  expect(screen.getByTestId("mission-plan-brief")).toHaveValue(
    "open a PR that does the thing",
  );
  await userEvent.click(review);
  expect(screen.queryByTestId("mission-plan-brief")).toBeNull();
});

test("a plan that ARRIVES by poll lands folded, not opened by the draft it replaced", () => {
  // Red against a latch read in the render that syncs the plan: the brief draft still held the empty
  // pre-plan text for that one pass, the new plan looked unsaved, and "incomplete stays open" fired.
  const pending = mission({
    state: "draft",
    plan: null,
    plan_state: "pending",
    plan_generation: 1,
  });
  const { rerender } = render(
    <Harness mission={pending} onChanged={vi.fn()} onNote={vi.fn()} />,
  );
  expect(screen.getByTestId("mission-plan-card")).toHaveAttribute(
    "data-plan-state",
    "planning",
  );
  rerender(
    <Harness
      mission={{ ...pending, state: "planned", plan: plan(), plan_state: "ready" }}
      onChanged={vi.fn()}
      onNote={vi.fn()}
    />,
  );
  expect(screen.getByTestId("mission-plan-lead")).toHaveTextContent(
    "Plan ready — claude in the-app",
  );
  expect(screen.queryByTestId("mission-plan-fields")).toBeNull();
  expect(screen.getByTestId("mission-plan-review")).toHaveAttribute(
    "aria-expanded",
    "false",
  );
});

test("Review plan in the header's ⋯ unfolds the plan too", async () => {
  render(
    <Harness
      mission={mission({ state: "planned", plan: plan(), plan_state: "ready" })}
      onChanged={vi.fn()}
      onNote={vi.fn()}
      onPlan={vi.fn()}
    />,
  );
  expect(screen.queryByTestId("mission-plan-fields")).toBeNull();
  await openOverflow();
  await userEvent.click(screen.getByTestId("mission-review-plan"));
  expect(screen.getByTestId("mission-plan-fields")).toBeInTheDocument();
});

test("a plan that is MISSING something starts unfolded, and stays open once it is complete", async () => {
  const incomplete = mission({
    state: "planned",
    plan: plan({ engine: null, engine_reason: "" }),
    plan_state: "ready",
  });
  const { rerender } = render(
    <Harness mission={incomplete} onChanged={vi.fn()} onNote={vi.fn()} />,
  );
  expect(screen.getByTestId("mission-plan-engine")).toBeInTheDocument();
  expect(screen.getByTestId("mission-plan-lead")).toHaveTextContent("The plan");
  // The operator picks the agent; the read after the save carries a complete plan.
  rerender(
    <Harness
      mission={{ ...incomplete, plan: plan({ plan_id: "pln_2" }) }}
      onChanged={vi.fn()}
      onNote={vi.fn()}
    />,
  );
  expect(screen.getByTestId("mission-plan-lead")).toHaveTextContent(
    "Plan ready",
  );
  expect(screen.getByTestId("mission-plan-engine")).toBeInTheDocument();
});

test("COULD NOT PLAN: the reason from plan_detail, Plan manually, and Plan again still in ⋯", async () => {
  mount(
    mission({
      state: "draft",
      plan: null,
      plan_state: "failed",
      plan_detail: "the model call failed: endpoint returned HTTP 500",
      plan_options: OPTIONS,
    }),
  );
  expect(screen.getByTestId("mission-plan-card")).toHaveAttribute(
    "data-plan-state",
    "unplanned",
  );
  expect(screen.getByTestId("mission-plan-lead")).toHaveTextContent(
    "Couldn't plan: the model call failed: endpoint returned HTTP 500",
  );
  const begin = screen.getByTestId("mission-begin");
  expect(begin).toBeDisabled();
  expect(begin).toHaveAccessibleDescription(
    /Couldn't plan: the model call failed/,
  );
  expect(screen.getByTestId("mission-plan-manually")).toBeEnabled();
  await openOverflow();
  expect(screen.getByTestId("mission-replan")).toBeEnabled();
  expect(screen.getByTestId("mission-replan")).toHaveTextContent("Plan again");
});

test("SKIPPED says there is no AI endpoint; a mission from before planning on create says only 'No plan yet'", () => {
  mount(
    mission({
      plan: null,
      plan_state: "skipped",
      plan_detail: "no AI endpoint is configured, so nothing can be planned",
    }),
  );
  expect(screen.getByTestId("mission-plan-lead")).toHaveTextContent(
    "Couldn't plan: no AI endpoint is configured",
  );
  expect(screen.getByTestId("mission-plan-manually")).toBeInTheDocument();

  // A backfilled row (or one with no `plan_state` at all) was never planned automatically, so it is
  // not told it failed to be.
  cleanup();
  mount(mission({ plan: null }));
  expect(screen.getByTestId("mission-plan-lead")).toHaveTextContent(
    /^No plan yet$/,
  );
  expect(screen.getByTestId("mission-plan-manually")).toBeInTheDocument();
});

test("PLAN MANUALLY saves the FIRST plan through its own call, shows a refusal inline, then Begin enables", async () => {
  vi.mocked(api.firstMissionPlan)
    .mockReset()
    .mockRejectedValueOnce(new ApiError(422, "'codex' cannot be dispatched into"))
    .mockResolvedValueOnce(plan() as never);
  const unplanned = mission({
    state: "draft",
    plan: null,
    plan_state: "skipped",
    plan_detail: "no AI endpoint is configured, so nothing can be planned",
    project_id: "p2",
    instruction: "ship the fix",
    plan_options: OPTIONS,
  });
  const onChanged = vi.fn();
  const { rerender } = render(
    <Harness mission={unplanned} onChanged={onChanged} onNote={vi.fn()} />,
  );
  expect(screen.queryByTestId("mission-plan-manual")).toBeNull();
  await userEvent.click(screen.getByTestId("mission-plan-manually"));

  // The mission's own project and instruction are the starting point; the agent is the operator's.
  const project = screen.getByTestId("mission-manual-project");
  const engine = screen.getByTestId("mission-manual-engine");
  const save = screen.getByTestId("mission-manual-save");
  expect(project).toHaveValue("p2");
  expect(project).toHaveFocus();
  expect(screen.getByTestId("mission-manual-brief")).toHaveValue("ship the fix");
  expect(save).toBeDisabled();

  await userEvent.selectOptions(engine, "codex");
  await userEvent.click(save);
  expect(api.firstMissionPlan).toHaveBeenCalledWith("msn_1", {
    project_id: "p2",
    engine: "codex",
    brief: "ship the fix",
  });
  // Never the edit call: there is no plan to name.
  expect(api.editMissionPlan).not.toHaveBeenCalled();
  // THE SERVER'S SENTENCE, in the card, under the form that is still holding what was typed.
  await waitFor(() =>
    expect(screen.getByTestId("mission-plan-error")).toHaveTextContent(
      "'codex' cannot be dispatched into",
    ),
  );
  const form = screen.getByTestId("mission-plan-manual");
  expect(
    form.compareDocumentPosition(screen.getByTestId("mission-plan-error")) &
      Node.DOCUMENT_POSITION_FOLLOWING,
  ).toBeTruthy();
  expect(engine).toHaveValue("codex");
  expect(onChanged).toHaveBeenCalled();

  // The re-read lands and the save is pressed again, this time accepted.
  rerender(
    <Harness mission={{ ...unplanned }} onChanged={onChanged} onNote={vi.fn()} />,
  );
  await userEvent.click(screen.getByTestId("mission-manual-save"));
  await waitFor(() => expect(api.firstMissionPlan).toHaveBeenCalledTimes(2));

  // …and the read after it: planned, a plan, `ready`. The form folds into the ready line.
  rerender(
    <Harness
      mission={{
        ...unplanned,
        state: "planned",
        plan: plan({ project_id: "p2", cwd: "/repo/the-docs", engine: "codex" }),
        plan_state: "ready",
        plan_detail: null,
        plan_options: null,
      }}
      onChanged={onChanged}
      onNote={vi.fn()}
    />,
  );
  expect(screen.queryByTestId("mission-plan-manual")).toBeNull();
  expect(screen.getByTestId("mission-plan-lead")).toHaveTextContent(
    "Plan ready — codex in the-docs",
  );
  expect(screen.getByTestId("mission-begin")).toBeEnabled();
});

test("a DRAFT with no session cannot launch, even carrying a plan: DISPATCH moves only planned missions", () => {
  mount(mission({ state: "draft", plan: plan(), plan_state: "ready" }));
  const begin = screen.getByTestId("mission-begin");
  expect(begin).toBeDisabled();
  expect(begin).toHaveAccessibleDescription(/not planned yet/);
});

test("a Plan again that FAILED keeps the previous plan, says which plan it is, and it can still Begin", () => {
  mount(
    mission({
      state: "planned",
      plan: plan(),
      plan_state: "failed",
      plan_detail: "the model call failed: timeout",
    }),
  );
  expect(screen.getByTestId("mission-plan-kept")).toHaveTextContent(
    "Plan again did not finish (the model call failed: timeout). This is the previous plan.",
  );
  expect(screen.queryByTestId("mission-plan-manually")).toBeNull();
  expect(screen.getByTestId("mission-begin")).toBeEnabled();
});

// ---- #1061 Phase 3: the confirmation says what it will track, and why it is worth a look -------

test("the armed launch lists each objective as required or a goal, with what it checks", async () => {
  mount(
    mission({
      state: "planned",
      plan: plan(),
      objectives: [
        {
          key: "merged",
          title: "devopsagent/alpha is merged",
          gate: true,
          probe: "forge_merged",
          probe_args: { branch: "devopsagent/alpha" },
        },
        {
          key: "note:1",
          title: "Look for conflicts",
          gate: false,
          probe: "none",
          probe_args: null,
        },
      ] as never,
      objectives_fit: { dropped: 1, parameterised: 1 },
    }),
  );
  await userEvent.click(screen.getByTestId("mission-begin"));
  const list = await screen.findByTestId("mission-confirm-objectives");
  expect(list).toHaveTextContent(
    "Requireddevopsagent/alpha is mergedforge_merged · devopsagent/alpha",
  );
  expect(list).toHaveTextContent("GoalLook for conflictsnote · not checked");
  const why = screen.getByTestId("mission-confirm-why");
  expect(why).toHaveTextContent(
    "1 objective was fitted to targets named in your instruction",
  );
  expect(why).toHaveTextContent(
    "1 suggestion did not fit the checklist and was dropped.",
  );
  expect(api.dispatchMission).not.toHaveBeenCalled();
});

test("an ordinary set arms the same confirmation with no 'worth a look' line", async () => {
  mount(
    mission({
      state: "planned",
      plan: plan(),
      objectives_fit: { dropped: 0, parameterised: 0 },
    }),
  );
  await userEvent.click(screen.getByTestId("mission-begin"));
  await screen.findByTestId("mission-confirm-objectives");
  expect(screen.queryByTestId("mission-confirm-why")).toBeNull();
});
