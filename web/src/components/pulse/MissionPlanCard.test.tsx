/** The plan card (#893 Phase 4, #2152). HQ plans every mission: the card offers Plan with HQ for a
 *  mission with no plan, says so when HQ can't plan, steps aside for a mission HQ planned, and
 *  keeps a LEGACY one-agent plan read-only with its Begin. What it still guards:
 *
 *  1. **Nothing launches from the plan.** DISPATCH confirms first — it starts an agent with nobody watching it, in
 *     a real directory. NOT permission-bypassed: `mission_dispatch` passes `bypass=False`, because
 *     unattended bypass is a separate grant from the interactive one and stays approval-gated
 *     (#904 review 17). The confirmation says what it does, so it must not say more.
 *  2. **A legacy plan is read-only** (#2152, D2): no picker, no instructions box, no Plan again.
 */
import { act, cleanup, render, screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { beforeEach, expect, test, vi } from "vitest";

import { MemoryRouter, Route, Routes, useLocation } from "react-router-dom";

import { ConfigCtx } from "../../app/config";
import { ApiError, api } from "../../lib/api";
import { objectivesDigestInput, sha256Hex } from "../../lib/digest";
import type { AppConfig, Mission, MissionPlan } from "../../types/api";

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

const HQ_READY = { pulse: { configured: true } } as unknown as AppConfig;

/** The harness inside the app's providers: a router (Plan with HQ navigates to `/hq`) and a config
 *  (whether HQ has an AI endpoint). */
function Wrapped({
  config = HQ_READY,
  ...props
}: Parameters<typeof Harness>[0] & { config?: AppConfig | null }) {
  return (
    <ConfigCtx.Provider value={config}>
      <MemoryRouter initialEntries={["/missions"]}>
        <Routes>
          <Route path="/missions" element={<Harness {...props} />} />
          <Route path="/hq" element={<HqProbe />} />
        </Routes>
      </MemoryRouter>
    </ConfigCtx.Provider>
  );
}

function mount(
  m: Mission,
  onChanged = vi.fn(),
  onNote = vi.fn(),
  config: AppConfig | null = HQ_READY,
) {
  render(<Wrapped mission={m} onChanged={onChanged} onNote={onNote} config={config} />);
  return { onChanged, onNote };
}

/** HQ's page, as far as the card can see it: what Plan with HQ handed over. */
function HqProbe() {
  const loc = useLocation();
  return <pre data-testid="hq-probe">{JSON.stringify(loc.state)}</pre>;
}

/** Review plan and the rest live behind the header's ⋯ (#967). */
async function openOverflow() {
  const t = screen.getByTestId("mission-overflow");
  if (t.getAttribute("aria-expanded") !== "true") await userEvent.click(t);
}

beforeEach(() => {
  vi.mocked(api.patchMissionObjectives)
    .mockReset()
    .mockResolvedValue({ objectives: [] } as never);
  vi.mocked(api.dispatchMission).mockReset().mockResolvedValue({
    state: "running",
    reason: "",
    session_key: "claude:a",
  });
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

test("the card says WHAT DONE MEANS, and says so when there is nothing", async () => {
  // #904 review 2, finding 8: an operator must never start an unattended agent without having seen
  // — or noticed the absence of — the checklist the supervisor will chase.
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
  expect(screen.getByTestId("mission-plan-lead")).toHaveTextContent("2 objectives");

  cleanup();
  mount(mission({ state: "planned", plan: plan(), objectives: [] }));
  expect(screen.getByTestId("mission-plan-lead")).toHaveTextContent("0 objectives");
  expect(screen.getByTestId("mission-begin")).toHaveAccessibleDescription(
    /at least one objective/,
  );
});

test("an empty checklist links to the editable Objectives section", async () => {
  const onObjectives = vi.fn();
  render(
    <Wrapped
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
  expect(screen.getByTestId("mission-plan-lead")).toHaveTextContent(/preparing objectives/i);
  expect(screen.getByTestId("mission-begin")).toHaveAccessibleDescription(
    /Wait for the objectives/,
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
    <Wrapped
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
    <Wrapped
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
    <Wrapped mission={first} onChanged={vi.fn()} onNote={vi.fn()} />,
  );
  await userEvent.click(screen.getByTestId("mission-begin"));
  // The arm has to have LANDED before the rerender below, or this tests the rerender, not the arm.
  await screen.findByTestId("mission-dispatch-confirm");

  // An objective becoming MET is progress, not a different checklist — the digest covers key,
  // title and gate only — so the arm survives this and the send must still happen.
  rerender(
    <Wrapped
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
    <Wrapped mission={original} onChanged={vi.fn()} onNote={vi.fn()} />,
  );
  await userEvent.click(screen.getByTestId("mission-begin"));
  await waitFor(() =>
    expect(screen.getByRole("alert")).toHaveTextContent("no longer live"),
  );
  expect(screen.getByTestId("mission-begin")).toBeDisabled();
  expect(api.dispatchMission).not.toHaveBeenCalled();
  rerender(
    <Wrapped
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
    <Wrapped mission={original} onChanged={vi.fn()} onNote={vi.fn()} />,
  );
  await userEvent.click(screen.getByTestId("mission-begin"));
  // Armed first (async digest): a second tap that lands before the arm only arms again.
  await screen.findByTestId("mission-dispatch-confirm");
  await userEvent.click(screen.getByTestId("mission-begin"));
  const polled = { ...original };
  rerender(
    <Wrapped mission={polled} onChanged={vi.fn()} onNote={vi.fn()} />,
  );
  await act(async () => finish());
  expect(screen.getByRole("alert")).toHaveTextContent("connection lost");
  expect(screen.getByTestId("mission-begin")).toBeDisabled();
  rerender(
    <Wrapped
      mission={{ ...polled }}
      onChanged={vi.fn()}
      onNote={vi.fn()}
    />,
  );
  expect(screen.getByTestId("mission-begin")).toBeEnabled();
});

// ---- the legacy plan and the HQ states (#967 P2b, #2152) -------------------------------------------

test("a DRAFT with no session cannot launch, even carrying a plan: DISPATCH moves only planned missions", () => {
  mount(mission({ state: "draft", plan: plan(), plan_state: "ready" }));
  const begin = screen.getByTestId("mission-begin");
  expect(begin).toBeDisabled();
  expect(begin).toHaveAccessibleDescription(/not planned yet/);
});

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

// ---- #1577: the agents the picker leaves out ------------------------------------------------------


// ---- HQ plans every mission (#2152, mockup C) --------------------------------------------------------

test("NO PLAN: the card says so and Plan with HQ hands THIS mission to HQ; no Begin, nothing written", async () => {
  const { onChanged } = mount(mission({ title: "Review the MCP server", plan_revision: null }));
  const card = screen.getByTestId("mission-plan-card");
  expect(card).toHaveAttribute("data-plan-state", "unplanned");
  expect(screen.getByTestId("mission-plan-lead")).toHaveTextContent("This mission has no plan");
  expect(screen.getByTestId("mission-plan-why")).toHaveTextContent(/picks or writes a flow/);
  // Retired with the one-agent planner: none of these exist any more.
  for (const id of [
    "mission-begin",
    "mission-plan-manually",
    "mission-plan-review",
    "mission-plan-engine",
    "mission-plan-brief",
  ])
    expect(screen.queryByTestId(id)).toBeNull();
  await openOverflow();
  expect(screen.queryByTestId("mission-replan")).toBeNull();
  await userEvent.click(screen.getByTestId("mission-plan-with-hq"));
  const probe = await screen.findByTestId("hq-probe");
  expect(JSON.parse(probe.textContent || "null")).toEqual({
    hqCompose: true,
    hqSeed: { text: "Plan this mission: Review the MCP server", mission: "msn_1" },
  });
  expect(api.dispatchMission).not.toHaveBeenCalled();
  expect(onChanged).not.toHaveBeenCalled();
});

test("HQ UNAVAILABLE: no AI endpoint, so the card links to Settings → AI → HQ and keeps the draft", () => {
  mount(
    mission({ plan_revision: null }),
    vi.fn(),
    vi.fn(),
    { pulse: { configured: false } } as unknown as AppConfig,
  );
  expect(screen.getByTestId("mission-plan-card")).toHaveAttribute(
    "data-plan-state",
    "hq-unavailable",
  );
  expect(screen.getByTestId("mission-plan-lead")).toHaveTextContent(
    "HQ isn't set up to plan missions",
  );
  expect(screen.getByTestId("mission-plan-why")).toHaveTextContent(/The draft is kept/);
  expect(screen.getByTestId("mission-plan-hq-settings")).toHaveAttribute(
    "href",
    "/settings/ai-hq",
  );
  expect(screen.queryByTestId("mission-plan-with-hq")).toBeNull();
});

test("Plan with HQ is offered only for a draft KNOWN to have no plan", () => {
  // An unreadable plan revision (absent) is not "no plan": nothing is offered on a guess.
  mount(mission({}));
  expect(screen.queryByTestId("mission-plan-with-hq")).toBeNull();
  cleanup();
  mount(mission({ state: "planned", plan_revision: null }));
  expect(screen.queryByTestId("mission-plan-with-hq")).toBeNull();
});

test("HQ PLANNED it: the card steps aside for Team, Plan approval and Steps, and there is no Begin", () => {
  mount(mission({ state: "draft", plan_revision: 2 }));
  expect(screen.queryByTestId("mission-plan-card")).toBeNull();
  expect(screen.queryByTestId("mission-begin")).toBeNull();
});

test("a LEGACY one-agent plan is read-only, says so, and keeps its Begin", async () => {
  mount(mission({ state: "planned", plan: plan(), plan_revision: null }));
  const card = screen.getByTestId("mission-plan-card");
  expect(card).toHaveAttribute("data-plan-state", "legacy");
  expect(screen.getByTestId("mission-plan-lead")).toHaveTextContent(
    "One agent: claude in the-app · 1 objective",
  );
  expect(screen.getByTestId("mission-plan-legacy")).toHaveTextContent("Legacy plan");
  expect(screen.getByTestId("mission-plan-legacy-note")).toHaveTextContent(
    "New missions are planned by HQ",
  );
  // Read-only: the saved instructions are shown, never editable, and nothing picks an agent.
  expect(screen.getByTestId("mission-plan-brief").tagName).toBe("DETAILS");
  expect(card.querySelector("textarea, select")).toBeNull();
  expect(screen.queryByTestId("mission-plan-with-hq")).toBeNull();
  // …and Begin still launches it, through the same confirmation.
  await userEvent.click(screen.getByTestId("mission-begin"));
  await screen.findByTestId("mission-dispatch-confirm");
  await userEvent.click(screen.getByTestId("mission-begin"));
  await waitFor(() =>
    expect(api.dispatchMission).toHaveBeenCalledWith(
      "msn_1",
      "pln_1",
      "/repo/the-app",
      expect.any(String),
    ),
  );
});
